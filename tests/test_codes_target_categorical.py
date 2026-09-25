"""Every seeded ``codes.csv`` row names a column the dictionary typed categorical.

metasalmon backlog **#95** (hub queue **B-95**), ruled **Q29** on 2026-09-05: a
column that has a code list is categorical by the specification's own
definition, and the code-row seeder is downstream of that decision. ``create_sdp()``
used to write ``column_role = "attribute"`` and ``codes.csv`` rows for the same
column in one call; the specification's validator
(``scripts/validate_package.py`` in ``smn-data-pkg``) reports each such row as
"targets a non-categorical or unknown column".

Ported as hub queue **B-125** from metasalmon pull request #112, whose R
counterpart is ``tests/testthat/test-codes-target-categorical.R``. Every role
expectation below was re-measured against an installed metasalmon 0.5.0 on
2026-09-16 and agrees with it.

:func:`_codes_rows_off_categorical` is that validator's ``validate_codes`` rule
applied in Python: every ``codes.csv`` row's ``(dataset_id, table_id,
column_name)`` must name a dictionary row whose ``column_role`` is
``categorical``. It is applied to the generator's own output on every bundled
example, so a regression in either the role heuristic or the seeder fails here
rather than in a sibling repository's validator. This package's
``validate_salmon_datapackage()`` does not report this class yet (metasalmon
backlog #48, hub B-48); when it does, the helper can go and these tests can
assert on the validator. *Retires when:* that lands.
"""

from __future__ import annotations

import datetime as _dt
import warnings
from pathlib import Path

import pandas as pd
import pytest

import metasalmonpy
from metasalmonpy import create_sdp, infer_salmon_datapackage_artifacts
from metasalmonpy.dictionary import infer_column_role
from metasalmonpy.metadata import CODE_LIST_LIMIT, code_list_values, read_sdp_csv

DATA = Path(metasalmonpy.__file__).parent / "data"


def bundled_example_data() -> list:
    """Every example data CSV this package ships, discovered rather than listed.

    R runs the two ``create_sdp()`` assertions below over **both** of its
    bundled examples — the 30-row sample and the 173-row official slice. This
    package ships only the sample, and whether it should carry the second is an
    **open, unruled** parity question with its own register row (``PARITY.md``
    row 46). So the list is discovered instead of hard-coded: the fixture covers
    whatever is bundled today, and picks up the 173-row example automatically
    if row 46 is ever resolved by vendoring it, rather than silently continuing
    to test one file. The starter dictionary that ships beside an example is
    excluded — it is metadata, not a data table.
    """
    return sorted(
        path
        for path in DATA.glob("nuseds-fraser-coho*.csv")
        if not path.name.endswith("-column_dictionary.csv")
    )


def _codes_rows_off_categorical(pkg_path: Path) -> dict:
    dictionary = read_sdp_csv(pkg_path / "metadata" / "column_dictionary.csv")
    codes = read_sdp_csv(pkg_path / "metadata" / "codes.csv")

    def key(frame: pd.DataFrame) -> list:
        return [
            "\r".join(
                "" if pd.isna(value) else str(value)
                for value in (row["dataset_id"], row["table_id"], row["column_name"])
            )
            for _, row in frame.iterrows()
        ]

    dictionary_keys = key(dictionary)
    categorical_keys = {
        dictionary_keys[position]
        for position, role in enumerate(dictionary["column_role"])
        if role == "categorical"
    }
    offending = codes.loc[
        [candidate not in categorical_keys for candidate in key(codes)]
    ]
    return {"dict": dictionary, "codes": codes, "offending": offending}


def _build_example_sdp(csv_path: Path, directory: Path) -> Path:
    """Build a package from one bundled example, the way a user would.

    ``pandas.read_csv`` rather than ``read_sdp_csv``: R's counterpart calls
    ``readr::read_csv()``, the **type-guessing** reader, and ``read_sdp_csv()``
    is this package's all-character *metadata* reader. Handing the role
    heuristic an all-character frame would make every column a string and
    silently defeat the identifier, temporal and measurement branches this test
    relies on running first.
    """
    dataset_id = csv_path.stem.replace("-", "_")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        create_sdp(
            pd.read_csv(csv_path),
            path=str(directory / dataset_id),
            dataset_id=dataset_id,
            table_id="escapement",
            seed_semantics=False,
            seed_verbose=False,
            check_updates=False,
            overwrite=True,
        )
    return directory / dataset_id


def test_infer_column_role_types_an_enumerable_string_column_categorical():
    enumerable = ["29F", "29G", "29J", "29K"] * 10
    assert infer_column_role("AREA", pd.Series(enumerable)) == "categorical"
    assert infer_column_role("SPECIES", pd.Series(["Coho"] * 12)) == "categorical"
    assert (
        infer_column_role("RUN_TYPE", pd.Series(["1", "FALL", None, "1"]))
        == "categorical"
    )
    assert (
        infer_column_role("ESTIMATE_STAGE", pd.Series(["FINAL", "NEAR FINAL"]))
        == "categorical"
    )

    # Method-named columns whose values enumerate are code lists too; their
    # procedures resolve through codes.csv$term_iri.
    assert (
        infer_column_role(
            "ESTIMATE_METHOD", pd.Series(["Fence", "Area Under the Curve", "Fence"])
        )
        == "categorical"
    )
    assert (
        infer_column_role(
            "ENUMERATION_METHODS", pd.Series(["Bank Walk", "Dead Pitch", None])
        )
        == "categorical"
    )

    # The identifier-qualifier branch reads the same predicate.
    assert (
        infer_column_role("stock_ID_quality", pd.Series(["high", "low", "high"]))
        == "categorical"
    )
    assert (
        infer_column_role("stock_ID_quality", pd.Series([2.0, 3.0, None]))
        == "attribute"
    )


def test_infer_column_role_keeps_free_text_and_wide_code_sets_as_attribute():
    # More distinct values than the seeder will list is not a code list.
    wide = pd.Series([f"WATERBODY {n:02d}" for n in range(1, CODE_LIST_LIMIT + 2)])
    assert infer_column_role("WATERBODY", wide) == "attribute"
    assert infer_column_role("counting_method", wide) == "attribute"

    # A column with no non-missing values has no code list either.
    assert infer_column_role("notes", pd.Series([None, None], dtype="object")) == "attribute"

    # The boundary is the seeder's own limit, on both sides of it.
    at_limit = pd.Series([f"code-{n:02d}" for n in range(1, CODE_LIST_LIMIT + 1)])
    assert infer_column_role("flag", at_limit) == "categorical"


def test_identifier_temporal_and_measurement_run_ahead_of_the_code_list_check():
    few = pd.Series(["A", "B", "A", "C"])
    assert infer_column_role("site_id", few) == "identifier"
    assert infer_column_role("sample_reference_number", few) == "identifier"
    assert (
        infer_column_role("survey_date", pd.Series(["2024-01-01", "2024-01-02"]))
        == "temporal"
    )
    assert (
        infer_column_role("ANALYSIS_YR", pd.Series(["2023", "2024", "2023"]))
        == "temporal"
    )
    assert (
        infer_column_role("run", pd.Series(["early", "late"], dtype="category"))
        == "categorical"
    )

    # A percent-like or unit-bearing text column is a measurement even when its
    # values repeat; the seeder still lists such values, and that residual
    # inconsistency is the seeder's half of #95, not this heuristic's.
    assert (
        infer_column_role("Environmental (%/month)", pd.Series(["0.00%", "4.56%"]))
        == "measurement"
    )
    assert (
        infer_column_role("spawner_count", pd.Series(["12", "15", "12"]))
        == "measurement"
    )


def test_the_role_heuristic_and_the_code_row_seeder_share_one_decision():
    resources = {
        "escapement": pd.DataFrame(
            {
                "POP_ID": [1, 2, 3],
                "AREA": ["29F", "29G", "29F"],
                "WATERBODY": [f"STREAM {n:02d}" for n in range(1, 4)],
                "ESTIMATE_METHOD": ["Fence", "Fence", "Area Under the Curve"],
                "NATURAL_ADULT_SPAWNERS": [10.0, 20.0, 30.0],
                "RELIABILITY": pd.Series(
                    ["LOW", "HIGH", "LOW"], dtype="category"
                ),
            }
        ),
        "stations": pd.DataFrame(
            {
                "STATION": [
                    f"station-{n:02d}" for n in range(1, CODE_LIST_LIMIT + 2)
                ],
                "STAGE": ["FINAL"] * (CODE_LIST_LIMIT + 1),
            }
        ),
    }

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        artifacts = infer_salmon_datapackage_artifacts(
            resources,
            dataset_id="b95-demo",
            seed_semantics=False,
            seed_verbose=False,
        )
    dictionary = artifacts["dict"]
    codes = artifacts["codes"]

    seeded = {
        f"{table}\r{column}"
        for table, column in zip(codes["table_id"], codes["column_name"])
    }
    categorical = {
        f"{table}\r{column}"
        for table, column, role in zip(
            dictionary["table_id"],
            dictionary["column_name"],
            dictionary["column_role"],
        )
        if role == "categorical"
    }
    # Every seeded column is categorical, and every categorical column is seeded.
    assert seeded == categorical
    assert set(codes["column_name"]) == {
        "AREA",
        "WATERBODY",
        "ESTIMATE_METHOD",
        "RELIABILITY",
        "STAGE",
    }

    def role_of(name):
        return dictionary.loc[
            dictionary["column_name"] == name, "column_role"
        ].iloc[0]

    assert role_of("STATION") == "attribute"
    assert role_of("POP_ID") == "identifier"
    assert role_of("NATURAL_ADULT_SPAWNERS") == "measurement"


@pytest.mark.parametrize(
    "example", bundled_example_data(), ids=lambda path: path.stem
)
def test_create_sdp_seeds_no_code_row_for_a_non_categorical_column(example, tmp_path):
    """R's plain assertion, over every bundled example.

    Every ``codes.csv`` row names a column the dictionary typed
    ``categorical``, which is ``nrow(got$offending) == 0`` in metasalmon's
    ``tests/testthat/test-codes-target-categorical.R``. Until hub B-188 this
    package could assert only the weaker "no seeded column is typed
    ``attribute``" (hub B-125), because ``START_DTT`` and ``END_DTT`` on the
    30-row sample were seeded and typed ``temporal``.
    """
    package_path = _build_example_sdp(example, tmp_path)
    got = _codes_rows_off_categorical(package_path)

    # The check cannot pass by seeding nothing.
    assert len(got["codes"]) > 0

    offending = sorted(set(got["offending"]["column_name"]))
    assert offending == [], (
        "codes.csv rows targeting non-categorical columns: " + ", ".join(offending)
    )


def test_the_30_row_sample_seeds_exactly_its_categorical_columns(tmp_path):
    """Seeded and categorical are the same twelve columns, and the dates are neither.

    ``pandas.read_csv`` leaves ``START_DTT`` and ``END_DTT`` as text, where
    ``readr::read_csv()`` gives R a ``Date`` that R's seeder never selects.
    They stay ``temporal``, because the temporal branch runs ahead of the
    code-list check in both implementations, and they carry no ``codes.csv``
    rows (hub B-188). On ``main`` at ``85ebbb0`` each carried fourteen.
    """
    package_path = _build_example_sdp(DATA / "nuseds-fraser-coho-sample.csv", tmp_path)
    got = _codes_rows_off_categorical(package_path)
    dictionary = got["dict"]

    seeded = sorted(set(got["codes"]["column_name"]))
    categorical = sorted(
        dictionary.loc[dictionary["column_role"] == "categorical", "column_name"]
    )
    assert seeded == categorical == [
        "AREA",
        "ENUMERATION_METHODS",
        "ESTIMATE_CLASSIFICATION",
        "ESTIMATE_METHOD",
        "ESTIMATE_STAGE",
        "FULL_CU_IN",
        "POPULATION",
        "RELIABILITY",
        "RUN_TYPE",
        "SPECIES",
        "WATERBODY",
        "WATERSHED_CDE",
    ]
    for name in ("START_DTT", "END_DTT"):
        role = dictionary.loc[dictionary["column_name"] == name, "column_role"].iloc[0]
        assert role == "temporal", name


# The type ``readr::read_csv()``, R's documented reader, gives a column of one
# date-shaped or date-time-shaped value. Measured 2026-09-24 under R 4.3.3,
# readr 2.2.0 and vroom 1.7.1, by reading ``x\n"<token>"`` with
# ``readr::read_csv(I(...))``. R's seeder never selects a ``Date`` or
# ``POSIXct`` column, because its guard is ``inherits(v, "factor") ||
# inherits(v, "character")``. So the same text read by ``pandas.read_csv`` must
# not seed a code list here. The date guess goes by shape alone: ``2001-02-30``
# and ``2001-13-06`` are guessed ``Date`` and then fail to parse. The date-time
# guess checks every field, so ``T25:00:00`` and ``2001-02-30T10:00:00`` stay
# character.
READR_GUESSES = [
    ("2001-11-06", "Date"),
    ("2001/11/06", "Date"),
    ("2001-11/06", "Date"),
    ("0999-06-05", "Date"),
    ("2001-02-30", "Date"),
    ("2001-13-06", "Date"),
    (" 2001-11-06", "Date"),
    ("2001-11-06T10:00:00", "POSIXct"),
    ("2001-11-06 10:00:00", "POSIXct"),
    ("2001-11-06T10:00", "POSIXct"),
    ("2001-11-06 10", "POSIXct"),
    ("2001-11-06T10:00:00Z", "POSIXct"),
    ("2001-11-06T10:00:00+02:00", "POSIXct"),
    ("2001-11-06T10:00:00-08", "POSIXct"),
    ("2001-11-06T10:00:00.123", "POSIXct"),
    ("20011106T100000", "POSIXct"),
    ("2001-11-06T10+02", "POSIXct"),
    ("2001-11-6", "character"),
    ("2001-1-06", "character"),
    ("2001.11.06", "character"),
    ("11/06/2001", "character"),
    ("Nov 6 2001", "character"),
    ("2001-11", "character"),
    ("12001-11-06", "character"),
    ("2001-11-06Z", "character"),
    ("2001-11-06T25:00:00", "character"),
    ("2001-11-06T23:60:00", "character"),
    ("2001-02-30T10:00:00", "character"),
    ("2001-13-06T10:00:00", "character"),
    ("2001/11/06 10:00:00", "character"),
    ("2001-11-06t10:00:00", "character"),
    ("2001-11-06T10:00:00 UTC", "character"),
    ("2001-11-06T1:00:00", "character"),
    ("2001-11-06T10:00:00,5", "character"),
]


@pytest.mark.parametrize(
    "token,readr_class", READR_GUESSES, ids=[token for token, _ in READR_GUESSES]
)
def test_text_readr_reads_as_a_date_or_date_time_seeds_no_code_list(token, readr_class):
    series = pd.Series([token, token])
    expected = [] if readr_class in ("Date", "POSIXct") else [token]
    assert code_list_values(series) == expected
    assert code_list_values(series.astype(object)) == expected


# readr guesses one type for the whole column, so a mixed column is a date only
# when every present value fits the same guess. Measured as above.
READR_COLUMN_GUESSES = [
    (["2001-11-06", "2001-11-06T10:00:00"], "POSIXct"),
    (["2001-11-06", "2001/11/07"], "Date"),
    (["2001-11-06", " "], "Date"),
    (["2001/11/07", "2001-11-06T10:00:00"], "character"),
    (["2001-02-30", "2001-11-06T10:00:00"], "character"),
    (["2001-11-06", "10:00"], "character"),
    (["2001-11-06", "unknown"], "character"),
]


@pytest.mark.parametrize(
    "tokens,readr_class",
    READR_COLUMN_GUESSES,
    ids=[" | ".join(tokens) for tokens, _ in READR_COLUMN_GUESSES],
)
def test_a_column_is_a_date_only_when_every_present_value_is(tokens, readr_class):
    expected = [] if readr_class in ("Date", "POSIXct") else tokens
    assert code_list_values(pd.Series(tokens)) == expected


def test_date_objects_seed_no_code_list_and_a_categorical_of_dates_still_does():
    # R holds these as Date and POSIXct, and its seeder never selects either.
    dates = [_dt.date(2001, 11, 6), None, _dt.date(2001, 11, 13)]
    assert code_list_values(pd.Series(dates, dtype=object)) == []
    stamps = [_dt.datetime(2001, 11, 6, 10, 30), pd.Timestamp("2001-11-13")]
    assert code_list_values(pd.Series(stamps, dtype=object)) == []
    assert code_list_values(pd.to_datetime(pd.Series(["2001-11-06", "2001-11-13"]))) == []
    # A factor passes R's guard whatever its levels are, and so does a
    # Categorical here, because declaring one is the caller's code-list intent.
    assert code_list_values(
        pd.Series(["2001-11-06", "2001-11-13"], dtype="category")
    ) == ["2001-11-06", "2001-11-13"]


def test_a_date_column_is_neither_seeded_nor_typed_categorical():
    # The role heuristic reads the seeder's predicate (hub B-125), so a column
    # of date text whose name has no time word is not left typed categorical
    # with no code list. R holds that column as a Date and types it temporal
    # by class. That role difference is not this test's subject.
    resources = {
        "surveys": pd.DataFrame(
            {
                "SURVEY_WAVE": ["2001-11-06", "2001-11-13", "2001-11-06"],
                "AREA": ["29F", "29G", "29F"],
            }
        )
    }
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        artifacts = infer_salmon_datapackage_artifacts(
            resources, dataset_id="b188-demo", seed_semantics=False, seed_verbose=False
        )
    dictionary = artifacts["dict"]
    assert set(artifacts["codes"]["column_name"]) == {"AREA"}
    role = dictionary.loc[dictionary["column_name"] == "SURVEY_WAVE", "column_role"].iloc[0]
    assert role != "categorical"
    assert infer_column_role("START_DTT", pd.Series(["2001-11-06", None])) == "temporal"
