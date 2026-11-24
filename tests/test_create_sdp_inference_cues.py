"""Three dataset-level inference cues ``create_sdp()`` did not read.

Hub item B-441, the metasalmonpy half of metasalmon's B-440. These tests are
the twins of metasalmon's ``tests/testthat/test-create-sdp-inference-cues.R``,
one for one and in the same order. Every frame here is small and synthetic;
none is a user's data.

1. A unit named in a column-name suffix (``fish_length_mm``, ``fish_weight_g``,
   ``depth_m``) was never read, so a numeric column whose name mentions fish
   was taken for a count and seeded the QUDT ``COUNT`` unit.
2. An administrative date column (``process_date``) fed temporal coverage, so
   ``temporal_end`` landed years after the observations stopped.
3. A spatial extent whose longitudes are all positive was written silently,
   even where the data are almost certainly in the western hemisphere.
"""

from __future__ import annotations

import functools
import warnings

import pandas as pd

from metasalmonpy import create_sdp
from metasalmonpy import semantics as sem
from metasalmonpy.dictionary import infer_dictionary
from metasalmonpy.metadata import (
    infer_dataset_metadata_from_resources,
    is_review_placeholder,
    read_sdp_csv,
)
from metasalmonpy.package_io import _collect_review_placeholder_issues
from metasalmonpy.semantics import suggest_semantics


# ---------------------------------------------------------------------------
# 1. Units named by a column-name suffix
# ---------------------------------------------------------------------------


def _unit_cue_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "fish_length_mm": [512, 600.5, 455],
            "fish_weight_g": [1500.2, 2100, 980],
            "depth_m": [12.5, 30, 45.1],
            "fish_count": [3, 5, 2],
        }
    )


def _no_candidates(query, role=None, sources=None):
    return pd.DataFrame()


def _unit_cue_targets(df: pd.DataFrame, force_measurement=()) -> pd.DataFrame:
    """The targets ``suggest_semantics()`` discovers, with no search answering.

    A column the name heuristics type ``attribute`` gets no unit target at all,
    so a test of the unit reader marks it a measurement, as a user would.
    """
    dictionary = infer_dictionary(df, dataset_id="d1", table_id="trawl")
    forced = dictionary["column_name"].isin(list(force_measurement))
    dictionary.loc[forced, "column_role"] = "measurement"
    suggested = suggest_semantics(
        {"trawl": df}, dictionary, search_fn=_no_candidates
    )
    return suggested.attrs["semantic_targets"]


def _target_query(targets: pd.DataFrame, column: str, field: str):
    hit = targets.loc[
        (targets["column_name"] == column)
        & (targets["target_sdp_field"] == field),
        "search_query",
    ]
    return None if hit.empty else hit.iloc[0]


def test_a_unit_suffix_in_a_column_name_becomes_the_unit_search_query():
    targets = _unit_cue_targets(_unit_cue_frame())

    assert _target_query(targets, "fish_length_mm", "unit_iri") == "millimeter"
    assert _target_query(targets, "fish_weight_g", "unit_iri") == "gram"
    assert _target_query(targets, "depth_m", "unit_iri") == "meter"
    # Positive control: a real count keeps its count unit.
    assert _target_query(targets, "fish_count", "unit_iri") == "count"


def test_a_length_or_mass_column_is_not_searched_as_a_count():
    targets = _unit_cue_targets(_unit_cue_frame())

    for column in ("fish_length_mm", "fish_weight_g"):
        assert _target_query(targets, column, "term_iri") != "count", column
        assert _target_query(targets, column, "property_iri") != "count", column
    # The unit token is not part of the variable query either.
    assert _target_query(targets, "fish_length_mm", "term_iri") == "fish length"

    # Without a suffix, a fish length still names no count unit.
    bare = _unit_cue_targets(pd.DataFrame({"fish_length": [512, 600.5, 455]}))
    assert _target_query(bare, "fish_length", "unit_iri") is None


def test_a_bare_one_letter_suffix_is_read_as_a_unit_only_beside_a_quantity_it_measures():
    targets = _unit_cue_targets(
        pd.DataFrame(
            {
                # `m` and `g` alone are too ambiguous (males, group): no length
                # or mass word, no unit.
                "spawners_m": [10, 12, 9],
                "catch_g": [4.5, 2.1, 3.3],
                # `weight` is not a length, so `_m` is not metres.
                "weight_m": [1.2, 3.4, 5.6],
            }
        )
    )

    assert _target_query(targets, "spawners_m", "unit_iri") != "meter"
    assert _target_query(targets, "catch_g", "unit_iri") is None
    assert _target_query(targets, "weight_m", "unit_iri") != "meter"


def test_a_compound_unit_is_not_read_from_its_last_token():
    # `mg/kg` is a concentration and `per kg` a rate, so reading the
    # denominator alone would draft kilogram for something that is not a mass
    # (Codex review of metasalmon #285). They stay unresolved, as before.
    compound = ("mercury_mg/kg", "mercury_mg_kg", "eggs_per_kg", "fish_length_per_m")
    targets = _unit_cue_targets(
        pd.DataFrame(
            {
                "mercury_mg/kg": [0.12, 0.30, 0.08],
                "mercury_mg_kg": [0.12, 0.30, 0.08],
                "eggs_per_kg": [1500, 2100, 1800],
                "fish_length_per_m": [0.2, 0.4, 0.3],
            }
        ),
        force_measurement=compound,
    )

    for column in compound:
        assert _target_query(targets, column, "unit_iri") is None, column


_QUDT = {"count": "COUNT", "millimeter": "MilliM", "gram": "GM", "meter": "M"}
_QUDT_LABELS = {"count": "Count", "millimeter": "Millimetre", "gram": "Gram", "meter": "Metre"}


def _fake_unit_search(query, role=None, sources=None):
    if role != "unit" or query not in _QUDT:
        return pd.DataFrame()
    return pd.DataFrame(
        [
            {
                "label": _QUDT_LABELS[query],
                "iri": f"http://qudt.org/vocab/unit/{_QUDT[query]}",
                "source": "qudt",
                "ontology": "qudt",
                "role": "unit",
                "match_type": "label",
                "definition": pd.NA,
            }
        ]
    )


def test_create_sdp_seeds_millimetre_gram_and_metre_from_unit_suffixes_not_count(
    tmp_path, monkeypatch
):
    # `package_io.create_sdp` imports `suggest_semantics` inside its body, so
    # binding `search_fn` on the module is the injection point
    # (metasalmon mocks `find_terms` for the same purpose).
    monkeypatch.setattr(
        sem,
        "suggest_semantics",
        functools.partial(sem.suggest_semantics, search_fn=_fake_unit_search),
    )
    package_path = create_sdp(
        {"trawl": _unit_cue_frame()},
        path=str(tmp_path / "pkg"),
        dataset_id="d1",
        seed_verbose=False,
        check_updates=False,
    )

    dictionary = read_sdp_csv(package_path / "metadata" / "column_dictionary.csv")

    def unit_of(column):
        return dictionary.loc[dictionary["column_name"] == column, "unit_iri"].iloc[0]

    assert unit_of("fish_length_mm") == "REVIEW:http://qudt.org/vocab/unit/MilliM"
    assert unit_of("fish_weight_g") == "REVIEW:http://qudt.org/vocab/unit/GM"
    assert unit_of("depth_m") == "REVIEW:http://qudt.org/vocab/unit/M"
    assert unit_of("fish_count") == "REVIEW:http://qudt.org/vocab/unit/COUNT"


# ---------------------------------------------------------------------------
# 2. Temporal coverage ignores administrative dates
# ---------------------------------------------------------------------------


def _dates(*values):
    return pd.to_datetime(list(values)).date


def test_an_administrative_date_column_does_not_stretch_temporal_coverage():
    df = pd.DataFrame(
        {
            "survey_date": _dates("1977-07-01", "1988-08-15", "1999-09-30"),
            "process_date": _dates("2016-02-01", "2016-05-01", "2017-03-15"),
        }
    )

    meta = infer_dataset_metadata_from_resources({"trawl": df}, "d1")

    assert meta["temporal_start"].iloc[0] == "1977-07-01"
    assert meta["temporal_end"].iloc[0] == "1999-09-30"


def test_common_administrative_date_spellings_are_all_left_out_of_coverage():
    admin_names = (
        "process_date", "processed_dtt", "date_processed", "ProcessingDate",
        "entry_date", "date_entered", "created_on", "CREATED_DTT", "DateCreated",
        "modified_date", "last_modified", "mod_date", "updated_at", "UPDATE_DTT",
        "load_date", "import_timestamp", "qc_date", "verified_date",
    )
    for admin in admin_names:
        df = pd.DataFrame(
            {
                "sample_date": _dates("1985-06-01", "1990-06-01"),
                admin: _dates("2015-01-01", "2020-01-01"),
            }
        )

        meta = infer_dataset_metadata_from_resources({"t": df}, "d1")

        assert meta["temporal_end"].iloc[0] == "1990-06-01", admin


def test_observation_dates_still_set_coverage_and_admin_dates_alone_set_none():
    # Positive control: nothing administrative, coverage unchanged.
    obs = pd.DataFrame(
        {
            "sample_date": _dates("2001-05-01", "2003-09-30"),
            "release_time": pd.to_datetime(
                ["2002-01-01 10:00", "2004-02-02 11:00"], utc=True
            ),
        }
    )
    meta_obs = infer_dataset_metadata_from_resources({"t": obs}, "d1")
    assert meta_obs["temporal_start"].iloc[0] == "2001-05-01"
    assert meta_obs["temporal_end"].iloc[0] == "2004-02-02"

    # Fish entering fresh water is an observation, not data entry.
    river = pd.DataFrame(
        {
            "river_entry_date": _dates("1995-07-01", "1996-08-01"),
            "data_entry_date": _dates("2016-01-01", "2017-01-01"),
        }
    )
    meta_river = infer_dataset_metadata_from_resources({"t": river}, "d1")
    assert meta_river["temporal_start"].iloc[0] == "1995-07-01"
    assert meta_river["temporal_end"].iloc[0] == "1996-08-01"

    # An entry date is not when the data were observed, so it is no coverage.
    admin_only = pd.DataFrame({"entry_date": _dates("2016-01-01", "2017-01-01")})
    meta_admin = infer_dataset_metadata_from_resources({"t": admin_only}, "d1")
    assert pd.isna(meta_admin["temporal_start"].iloc[0])
    assert pd.isna(meta_admin["temporal_end"].iloc[0])


# ---------------------------------------------------------------------------
# 3. Positive longitudes get a review note, never a silent sign flip
# ---------------------------------------------------------------------------


def test_all_positive_longitudes_in_the_mirrored_north_american_window_get_a_review_note():
    df = pd.DataFrame({"lon": [122.5, 130.1, 137.9], "lat": [48.2, 55.0, 60.0]})

    meta = infer_dataset_metadata_from_resources({"trawl": df}, "d1")
    extent = meta["spatial_extent"].iloc[0]

    assert is_review_placeholder(extent)
    # The inferred extent is kept, unflipped, so the reviewer sees what was
    # read. metasalmon writes the upper latitude as `60`; this package has
    # always written it `60.0`, so the shared prefix is what is compared.
    assert "lon=122.5..137.9, lat=48.2..60" in extent
    assert "-122.5" not in extent
    assert "negative" in extent


def test_the_longitude_review_note_reaches_dataset_csv_and_strict_validation_refuses_it(
    tmp_path,
):
    df = pd.DataFrame(
        {
            "station": ["A", "B", "C"],
            "lon": [122.5, 130.1, 137.9],
            "lat": [48.2, 55.0, 60.0],
        }
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        package_path = create_sdp(
            {"trawl": df},
            path=str(tmp_path / "pkg"),
            dataset_id="d1",
            seed_semantics=False,
            seed_verbose=False,
            check_updates=False,
        )
    dataset_csv = read_sdp_csv(package_path / "metadata" / "dataset.csv")

    assert dataset_csv["spatial_extent"].iloc[0].startswith("REVIEW REQUIRED: ")
    # The sweep strict validation runs over dataset.csv names the field.
    issues = _collect_review_placeholder_issues(dataset_csv, "metadata/dataset.csv")
    assert any("spatial_extent" in message for message in issues)


def test_negative_or_eastern_hemisphere_shaped_longitudes_are_written_as_before():
    # This package renders an integer-valued latitude as `60.0`, where
    # metasalmon writes `60`; that rendering predates B-441 and is unchanged
    # by it, so the expected strings are this package's own.
    western = pd.DataFrame({"lon": [-137.9, -122.5], "lat": [48.2, 60.0]})
    meta_w = infer_dataset_metadata_from_resources({"t": western}, "d1")
    assert meta_w["spatial_extent"].iloc[0] == "lon=-137.9..-122.5, lat=48.2..60.0"

    # Norway's coast: positive and correct, and outside the mirrored window.
    norway = pd.DataFrame({"lon": [5.3, 29.9], "lat": [58.1, 70.9]})
    meta_n = infer_dataset_metadata_from_resources({"t": norway}, "d1")
    assert meta_n["spatial_extent"].iloc[0] == "lon=5.3..29.9, lat=58.1..70.9"

    # A mixed-sign extent says nothing about a sign error.
    mixed = pd.DataFrame({"lon": [-10.2, 15.4], "lat": [50.0, 60.0]})
    meta_m = infer_dataset_metadata_from_resources({"t": mixed}, "d1")
    assert not is_review_placeholder(meta_m["spatial_extent"].iloc[0])
