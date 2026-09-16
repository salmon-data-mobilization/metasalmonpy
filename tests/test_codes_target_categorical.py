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

import warnings
from pathlib import Path

import pandas as pd
import pytest

import metasalmonpy
from metasalmonpy import create_sdp, infer_salmon_datapackage_artifacts
from metasalmonpy.dictionary import infer_column_role
from metasalmonpy.metadata import CODE_LIST_LIMIT, read_sdp_csv

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
def test_create_sdp_seeds_no_code_row_for_a_column_typed_attribute(example, tmp_path):
    """The assertion hub B-125's ``retires_when`` names, over every bundled example.

    Measured on the 30-row sample: **twelve** columns were typed ``attribute``
    and carried seeded ``codes.csv`` rows before this port (AREA, POPULATION,
    SPECIES, RUN_TYPE, WATERBODY, WATERSHED_CDE, RELIABILITY, FULL_CU_IN,
    ENUMERATION_METHODS, ESTIMATE_METHOD, ESTIMATE_CLASSIFICATION,
    ESTIMATE_STAGE); **zero** after it.
    """
    package_path = _build_example_sdp(example, tmp_path)
    got = _codes_rows_off_categorical(package_path)

    # The check cannot pass by seeding nothing.
    assert len(got["codes"]) > 0

    roles = {
        name: got["dict"]
        .loc[got["dict"]["column_name"] == name, "column_role"]
        .iloc[0]
        for name in sorted(set(got["codes"]["column_name"]))
    }
    seeded_attributes = sorted(
        name for name, role in roles.items() if role == "attribute"
    )
    assert seeded_attributes == [], (
        "codes.csv rows targeting columns typed attribute: "
        + ", ".join(seeded_attributes)
    )


def test_the_only_seeded_non_categorical_columns_are_the_known_date_residual(tmp_path):
    """What is left over, pinned rather than left to be rediscovered.

    R's helper asserts that **no** ``codes.csv`` row targets a non-categorical
    column, and R reaches that on this example. This package does not, for two
    columns, and the cause is **not** the ported role heuristic:

    * ``readr::read_csv()`` parses ``START_DTT`` / ``END_DTT`` into a ``Date``
      vector, and R's seeder selects on ``inherits(v, "factor") ||
      inherits(v, "character")``, so a ``Date`` never reaches it;
    * ``pandas.read_csv`` leaves them as strings, and even parsed to
      ``datetime.date`` they stay **object** dtype -- which the seeder's own
      dtype test accepts. So they are seeded, while the temporal branch (which
      runs ahead of the code-list check, in both implementations) types them
      ``temporal``.

    Measured 2026-09-16 on the pristine tree: these same two were seeded and
    typed ``temporal`` **before** this port, so B-125 neither caused nor
    widened it, and B-125's ``retires_when`` speaks of a column typed
    *attribute*, which this is not. Filed as a candidate rather than absorbed
    (see ``.hub/workpads/B-125.md``). This test exists so the residual is a
    pinned, named two rather than an open-ended "some". *Retires when:* the
    seeder's dtype test stops accepting a date-like object column, at which
    point this expects an empty set and R's plain assertion can replace it.
    """
    example = DATA / "nuseds-fraser-coho-sample.csv"
    package_path = _build_example_sdp(example, tmp_path)
    got = _codes_rows_off_categorical(package_path)

    roles = {
        name: got["dict"]
        .loc[got["dict"]["column_name"] == name, "column_role"]
        .iloc[0]
        for name in sorted(set(got["codes"]["column_name"]))
    }
    non_categorical = sorted(
        name for name, role in roles.items() if role != "categorical"
    )
    assert non_categorical == ["END_DTT", "START_DTT"]
    assert {roles[name] for name in non_categorical} == {"temporal"}

    # Everything else is categorical, which is the half B-125 owns.
    assert sorted(name for name, role in roles.items() if role == "categorical") == [
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
