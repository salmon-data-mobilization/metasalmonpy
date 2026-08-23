"""S10 chunk D — validation hardening, differentially verified against R.

Every expected message, issue row, and filled placeholder in this file was
measured by running metasalmon (pristine ``git archive`` of ``main`` @
``9d8f12596157dbf1c0b49f55d6e07cbece8abd70``, 2026-08-22) over the same
fixture packages this file builds, then transcribed verbatim. The five-column
issue frames are compared field-for-field, so a wording drift on either side
fails here rather than dissolving into "roughly the same report".

This is the differential fixture PARITY.md row 41's retirement condition asks
for: both implementations now report the same typed issue set for the same
deliberately broken package, which also lifts the standing prohibition on
cross-implementation issue-count verification.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pandas as pd
import pytest

import metasalmonpy
from metasalmonpy import (
    validate_salmon_datapackage,
    write_salmon_datapackage,
)
from metasalmonpy.metadata import (
    fill_review_placeholders_dataset_meta,
    fill_review_placeholders_dictionary,
    fill_review_placeholders_table_meta,
    normalize_dataset_meta,
    normalize_dictionary,
    normalize_table_meta,
    read_sdp_csv,
    titleize_identifier,
)
from metasalmonpy.package_io import (
    _collect_package_validation_issues,
    _detect_wide_columns,
    read_salmon_datapackage,
)

DATA = Path(metasalmonpy.__file__).parent / "data"
TABLE = "nuseds_fraser_coho"


def _build_example(path) -> Path:
    """The R round-trip helper's build: shipped example data + metadata."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        write_salmon_datapackage(
            resources={TABLE: read_sdp_csv(DATA / "nuseds-fraser-coho-sample.csv")},
            dataset_meta=read_sdp_csv(DATA / "dataset.csv"),
            table_meta=read_sdp_csv(DATA / "tables.csv"),
            dict_df=read_sdp_csv(DATA / "column_dictionary.csv"),
            codes=read_sdp_csv(DATA / "codes.csv"),
            path=str(path),
            overwrite=True,
        )
    return Path(path)


def _edit_csv(path: Path, editor) -> None:
    frame = editor(read_sdp_csv(path))
    frame.to_csv(path, index=False, na_rep="")


def _collect(path: Path) -> pd.DataFrame:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        package = read_salmon_datapackage(str(path))
        return _collect_package_validation_issues(package, path=path)


def _row(issue_type, message, table_id=None, column_name=None, value=None):
    return {
        "issue_type": issue_type,
        "table_id": table_id,
        "column_name": column_name,
        "value": value,
        "message": message,
    }


def _assert_rows(frame: pd.DataFrame, expected: list[dict]) -> None:
    assert list(frame.columns) == [
        "issue_type",
        "table_id",
        "column_name",
        "value",
        "message",
    ]
    got = [
        {key: (None if pd.isna(val) else val) for key, val in row.items()}
        for _, row in frame.iterrows()
    ]
    assert got == expected


# --- the eight typed categories, message-for-message --------------------------

# Each entry: fixture name -> (corruption, expected R-measured issue rows).
# The corruption takes the built package root.


def _corrupt_dataset_two_rows(root):
    _edit_csv(root / "metadata" / "dataset.csv", lambda df: df.iloc[[0, 0]])


def _corrupt_tables_empty(root):
    _edit_csv(root / "metadata" / "tables.csv", lambda df: df.iloc[0:0])


def _corrupt_dict_empty(root):
    _edit_csv(root / "metadata" / "column_dictionary.csv", lambda df: df.iloc[0:0])


def _corrupt_dup_table_id(root):
    _edit_csv(root / "metadata" / "tables.csv", lambda df: df.iloc[[0, 0]])


def _corrupt_dict_ghost_table(root):
    def editor(df):
        extra = df.iloc[[0]].copy()
        extra["table_id"] = "ghost"
        extra["column_name"] = "GHOST_COL"
        return pd.concat([df, extra], ignore_index=True)

    _edit_csv(root / "metadata" / "column_dictionary.csv", editor)


def _corrupt_codes_ghost_table(root):
    def editor(df):
        extra = df.iloc[[0]].copy()
        extra["table_id"] = "ghost"
        return pd.concat([df, extra], ignore_index=True)

    _edit_csv(root / "metadata" / "codes.csv", editor)


def _corrupt_missing_resource(root):
    (root / "data" / "nuseds-fraser-coho-sample.csv").unlink()


def _corrupt_pk_ghost_col(root):
    def editor(df):
        df["primary_key"] = "POP_ID,GHOST"
        return df

    _edit_csv(root / "metadata" / "tables.csv", editor)


def _corrupt_pk_duplicates(root):
    _edit_csv(
        root / "data" / "nuseds-fraser-coho-sample.csv",
        lambda df: pd.concat([df.iloc[[0]], df], ignore_index=True),
    )


def _corrupt_pk_missing_values(root):
    def editor(df):
        df.loc[1, "POP_ID"] = ""
        return df

    _edit_csv(root / "data" / "nuseds-fraser-coho-sample.csv", editor)


def _corrupt_value_type_date(root):
    def editor(df):
        # Backlog #98's Oracle DD-MON-YY bytes under a declared value_type of
        # ``date``; readr::parse_date and parse_date_token both reject them.
        df.loc[0, "START_DTT"] = "06-NOV-01"
        df.loc[1, "START_DTT"] = "03-NOV-18"
        return df

    _edit_csv(root / "data" / "nuseds-fraser-coho-sample.csv", editor)


def _corrupt_dict_col_missing_in_data(root):
    def editor(df):
        extra = df.iloc[[0]].copy()
        extra["column_name"] = "GHOST_COL"
        extra["value_type"] = "string"
        return pd.concat([df, extra], ignore_index=True)

    _edit_csv(root / "metadata" / "column_dictionary.csv", editor)


def _corrupt_extra_data_col(root):
    _edit_csv(
        root / "metadata" / "column_dictionary.csv",
        lambda df: df[df["column_name"] != "WATERSHED_CDE"],
    )


def _corrupt_codes_col_not_in_dict(root):
    def editor(df):
        extra = df.iloc[[0]].copy()
        extra["column_name"] = "GHOST_COL"
        extra["code_value"] = "X"
        return pd.concat([df, extra], ignore_index=True)

    _edit_csv(root / "metadata" / "codes.csv", editor)


def _corrupt_codes_missing_value(root):
    def editor(df):
        mask = (df["column_name"] == "RUN_TYPE") & (df["code_value"] == "FALL")
        df.loc[mask, "code_value"] = "SPRING"
        return df

    _edit_csv(root / "metadata" / "codes.csv", editor)


def _corrupt_codes_value_type(root):
    def editor(df):
        extra = df.iloc[[0]].copy()
        extra["column_name"] = "ANALYSIS_YR"
        extra["code_value"] = "1.5"
        extra["code_label"] = "bad year"
        return pd.concat([df, extra], ignore_index=True)

    _edit_csv(root / "metadata" / "codes.csv", editor)


def _corrupt_composite_intent(root):
    def editor(df):
        df["route"] = "composite"
        return df

    _edit_csv(root / "metadata" / "dataset.csv", editor)


_ANALYSIS_YEARS = (
    "2001, 2018, 2016, 2024, 2003, 1997, 1996, 2007, 2000, 2004, 2011, "
    "1998, 1999, 2008"
)

CATEGORY_CASES = {
    "dataset-two-rows": (
        _corrupt_dataset_two_rows,
        [_row("dataset", "dataset.csv should contain exactly one row; found 2.")],
    ),
    "tables-empty": (
        _corrupt_tables_empty,
        [
            _row("tables", "No rows found in tables.csv."),
            _row(
                "dictionary",
                "column_dictionary.csv references table_id values not present "
                f"in tables.csv: {TABLE}.",
            ),
            _row(
                "codes",
                "codes.csv references table_id values not present in "
                f"tables.csv: {TABLE}.",
            ),
        ],
    ),
    "dict-empty": (
        _corrupt_dict_empty,
        [
            _row("dictionary", "No rows found in column_dictionary.csv."),
            _row(
                "dictionary",
                f"No dictionary rows found for table '{TABLE}'.",
                table_id=TABLE,
            ),
        ],
    ),
    "dup-table-id": (
        _corrupt_dup_table_id,
        [_row("tables", f"Duplicate table_id values in tables.csv: {TABLE}.")],
    ),
    "dict-ghost-table": (
        _corrupt_dict_ghost_table,
        [
            _row(
                "dictionary",
                "column_dictionary.csv references table_id values not present "
                "in tables.csv: ghost.",
            )
        ],
    ),
    "codes-ghost-table": (
        _corrupt_codes_ghost_table,
        [
            _row(
                "codes",
                "codes.csv references table_id values not present in "
                "tables.csv: ghost.",
            )
        ],
    ),
    "missing-resource": (
        _corrupt_missing_resource,
        [
            _row(
                "resource",
                f"Table '{TABLE}' points to resource "
                "'data/nuseds-fraser-coho-sample.csv', but that file could "
                "not be loaded.",
                table_id=TABLE,
            )
        ],
    ),
    "pk-ghost-col": (
        _corrupt_pk_ghost_col,
        [
            _row(
                "primary_key",
                f"Table '{TABLE}' primary_key references columns not present "
                "in data: GHOST.",
                table_id=TABLE,
                column_name="GHOST",
            )
        ],
    ),
    "pk-duplicates": (
        _corrupt_pk_duplicates,
        [
            _row(
                "tables",
                f"Table '{TABLE}' declares primary key 'POP_ID' but 1 row "
                "repeats it.",
                table_id=TABLE,
            )
        ],
    ),
    "pk-missing-values": (
        _corrupt_pk_missing_values,
        [
            _row(
                "tables",
                f"Table '{TABLE}' declares primary key 'POP_ID' but column "
                "POP_ID contains missing values.",
                table_id=TABLE,
            )
        ],
    ),
    "value-type-date": (
        _corrupt_value_type_date,
        [
            _row(
                "columns",
                f"Table '{TABLE}' column 'START_DTT' declares value_type "
                "'date' but 2 values did not satisfy it (unparseable as that "
                "type): 06-NOV-01, 03-NOV-18.",
                table_id=TABLE,
                column_name="START_DTT",
                value="06-NOV-01, 03-NOV-18",
            )
        ],
    ),
    "dict-col-missing-in-data": (
        _corrupt_dict_col_missing_in_data,
        [
            _row(
                "columns",
                f"Table '{TABLE}' is missing dictionary columns in data: "
                "GHOST_COL.",
                table_id=TABLE,
                column_name="GHOST_COL",
            )
        ],
    ),
    "extra-data-col": (
        _corrupt_extra_data_col,
        [
            _row(
                "columns",
                f"Table '{TABLE}' has data columns not listed in "
                "column_dictionary.csv: WATERSHED_CDE.",
                table_id=TABLE,
                column_name="WATERSHED_CDE",
            )
        ],
    ),
    "codes-col-not-in-dict": (
        _corrupt_codes_col_not_in_dict,
        [
            _row(
                "codes",
                f"codes.csv references table '{TABLE}' column 'GHOST_COL', "
                "but that column is not in column_dictionary.csv.",
                table_id=TABLE,
                column_name="GHOST_COL",
            ),
            _row(
                "codes",
                f"codes.csv references table '{TABLE}' column 'GHOST_COL', "
                "but that column is not present in data.",
                table_id=TABLE,
                column_name="GHOST_COL",
            ),
        ],
    ),
    "codes-missing-value": (
        _corrupt_codes_missing_value,
        [
            _row(
                "codes",
                f"Table '{TABLE}' column 'RUN_TYPE' has data values not "
                "listed in codes.csv: FALL.",
                table_id=TABLE,
                column_name="RUN_TYPE",
                value="FALL",
            )
        ],
    ),
    "codes-value-type": (
        _corrupt_codes_value_type,
        [
            _row(
                "codes",
                f"Table '{TABLE}' column 'ANALYSIS_YR' declares value_type "
                "'integer' but 1 codes.csv value did not satisfy it (not a "
                "whole number): 1.5.",
                table_id=TABLE,
                column_name="ANALYSIS_YR",
            ),
            _row(
                "codes",
                f"Table '{TABLE}' column 'ANALYSIS_YR' has data values not "
                f"listed in codes.csv: {_ANALYSIS_YEARS}.",
                table_id=TABLE,
                column_name="ANALYSIS_YR",
                value=_ANALYSIS_YEARS,
            ),
        ],
    ),
    "composite-intent": (
        _corrupt_composite_intent,
        [
            _row(
                "composite_intent",
                "Explicit composite route intent detected in route "
                "(composite), but no populated WSP composite signal columns "
                "were found in cu_timeseries. Populate at least one of: "
                "SPN_ABD_WILD, SPN_TREND_WILD, RAPID_STATUS.",
                table_id="cu_timeseries",
                column_name="SPN_ABD_WILD, SPN_TREND_WILD, RAPID_STATUS",
                value="composite",
            )
        ],
    ),
}


@pytest.mark.parametrize("name", sorted(CATEGORY_CASES))
def test_every_issue_category_matches_r_field_for_field(name, tmp_path):
    corrupt, expected = CATEGORY_CASES[name]
    root = _build_example(tmp_path / name)
    corrupt(root)
    _assert_rows(_collect(root), expected)
    # And the validator enforces them: one abort, the frame on the error.
    with pytest.raises(ValueError) as excinfo, warnings.catch_warnings():
        warnings.simplefilter("ignore")
        validate_salmon_datapackage(str(root))
    total = len(expected)
    assert str(excinfo.value).startswith(
        f"Salmon Data Package validation failed with {total} structural "
        f"issue{'' if total == 1 else 's'}."
    )
    _assert_rows(excinfo.value.issues, expected)


def test_the_collector_accumulates_before_the_single_abort(tmp_path):
    """R's stacked fixture: five issues from four categories, one abort."""
    root = _build_example(tmp_path / "stacked")
    _corrupt_dataset_two_rows(root)
    _corrupt_pk_ghost_col(root)

    def dict_editor(df):
        extra = df.iloc[[0]].copy()
        extra["column_name"] = "GHOST_COL"
        extra["value_type"] = "string"
        return pd.concat(
            [df[df["column_name"] != "WATERSHED_CDE"], extra], ignore_index=True
        )

    _edit_csv(root / "metadata" / "column_dictionary.csv", dict_editor)

    def data_editor(df):
        df.loc[0, "START_DTT"] = "06-NOV-01"
        return df

    _edit_csv(root / "data" / "nuseds-fraser-coho-sample.csv", data_editor)

    expected = [
        _row("dataset", "dataset.csv should contain exactly one row; found 2."),
        _row(
            "columns",
            f"Table '{TABLE}' column 'START_DTT' declares value_type 'date' "
            "but 1 value did not satisfy it (unparseable as that type): "
            "06-NOV-01.",
            table_id=TABLE,
            column_name="START_DTT",
            value="06-NOV-01",
        ),
        _row(
            "columns",
            f"Table '{TABLE}' is missing dictionary columns in data: GHOST_COL.",
            table_id=TABLE,
            column_name="GHOST_COL",
        ),
        _row(
            "columns",
            f"Table '{TABLE}' has data columns not listed in "
            "column_dictionary.csv: WATERSHED_CDE.",
            table_id=TABLE,
            column_name="WATERSHED_CDE",
        ),
        _row(
            "primary_key",
            f"Table '{TABLE}' primary_key references columns not present in "
            "data: GHOST.",
            table_id=TABLE,
            column_name="GHOST",
        ),
    ]
    _assert_rows(_collect(root), expected)

    with pytest.raises(ValueError) as excinfo, warnings.catch_warnings():
        warnings.simplefilter("ignore")
        validate_salmon_datapackage(str(root))
    message = str(excinfo.value)
    assert message.startswith(
        "Salmon Data Package validation failed with 5 structural issues."
    )
    # Accumulate-then-report: every message is in the one abort.
    for row in expected:
        assert row["message"] in message


# --- 0.2.6 tidy checks ---------------------------------------------------------


def test_wide_column_detection_matches_r():
    """20 probes, each pinned to ``.ms_detect_wide_columns()``'s output."""
    battery = {
        # bare year-like names, three or more
        ("1998", "1999", "2000"): ["1998", "1999", "2000"],
        ("X1998", "x1999", "2000"): ["2000", "X1998", "x1999"],
        ("1998", "1999"): [],
        # a shared stem with numeric tails
        ("count_1998", "count_1999", "count_2000", "site"): [
            "count_1998",
            "count_1999",
            "count_2000",
        ],
        ("count_1998", "count_1999", "site", "x"): [],
        ("count1", "count2", "count3"): ["count1", "count2", "count3"],
        ("area.1", "area.2", "area.3"): ["area.1", "area.2", "area.3"],
        ("a-1", "a-2", "a-3"): ["a-1", "a-2", "a-3"],
        ("x2", "x3", "site"): [],
        ("total_10", "total_20", "total_30", "mean_1", "mean_2"): [
            "total_10",
            "total_20",
            "total_30",
        ],
        # year-like takes priority over stems, sorted C-collation
        ("2000", "1999", "1998", "count_1", "count_2", "count_3"): [
            "1998",
            "1999",
            "2000",
        ],
        (" 1998 ", " 1999 ", "2000"): ["1998", "1999", "2000"],
        ("2100", "2099", "1899"): ["1899", "2099", "2100"],
        ("y1998", "y1999", "y2000"): ["y1998", "y1999", "y2000"],
        ("POP_ID", "ANALYSIS_YR", "SPECIES"): [],
        ("v_1", "v_2", "v_3", "v_4"): ["v_1", "v_2", "v_3", "v_4"],
        ("s-01", "s-02", "s-03"): ["s-01", "s-02", "s-03"],
        ("m2", "m3", "m4"): ["m2", "m3", "m4"],
        (): [],
    }
    for names, expected in battery.items():
        assert _detect_wide_columns(list(names)) == expected, names
    # NA and blank names drop out before the three-column threshold.
    assert _detect_wide_columns(["a_1", float("nan"), "a_2", "", "a_3"]) == [
        "a_1",
        "a_2",
        "a_3",
    ]


def test_wide_columns_warn_and_point_at_melt(tmp_path):
    root = _build_example(tmp_path / "wide")
    renames = {
        "WATERSHED_CDE": "count_1998",
        "FULL_CU_IN": "count_1999",
        "WATERBODY": "count_2000",
    }
    _edit_csv(
        root / "data" / "nuseds-fraser-coho-sample.csv",
        lambda df: df.rename(columns=renames),
    )

    def dict_editor(df):
        df["column_name"] = df["column_name"].replace(renames)
        return df

    _edit_csv(root / "metadata" / "column_dictionary.csv", dict_editor)

    # A warning, never an error: validation passes in BOTH modes. R points at
    # tidyr::pivot_longer(); the pandas counterpart is melt() (PARITY.md
    # row 49 records the deliberate difference).
    with pytest.warns(UserWarning, match=r"may not be tidy.*pandas\.melt\(\)"):
        result = validate_salmon_datapackage(str(root), require_iris=False)
    assert len(result["issues"]) == 0
    with pytest.warns(UserWarning, match="count_1998, count_1999, count_2000"):
        validate_salmon_datapackage(str(root), require_iris=True)


def test_unresolved_placeholders_warn_in_default_mode(tmp_path):
    root = _build_example(tmp_path / "placeholders")

    def dataset_editor(df):
        df["creator"] = (
            "MISSING METADATA: add creator, team, or originating program."
        )
        return df

    _edit_csv(root / "metadata" / "dataset.csv", dataset_editor)

    def tables_editor(df):
        df["description"] = (
            "MISSING DESCRIPTION: describe what each row in table "
            f"'{TABLE}' represents."
        )
        return df

    _edit_csv(root / "metadata" / "tables.csv", tables_editor)

    def codes_editor(df):
        df.loc[0, "code_description"] = "REVIEW REQUIRED: confirm this code."
        return df

    _edit_csv(root / "metadata" / "codes.csv", codes_editor)

    # Default mode: a warning naming file$column, C-collation sorted, and the
    # validation still passes — R warns and returns.
    with pytest.warns(
        UserWarning,
        match=(
            r"3 metadata fields still hold a placeholder: "
            r"codes\.csv\$code_description, dataset\.csv\$creator, "
            r"tables\.csv\$description"
        ),
    ):
        result = validate_salmon_datapackage(str(root), require_iris=False)
    assert len(result["issues"]) == 0

    # Strict mode: the same three placeholders are errors, R's message texts.
    with pytest.raises(ValueError) as excinfo:
        validate_salmon_datapackage(str(root), require_iris=True)
    message = str(excinfo.value)
    assert message.startswith(
        "Final validation failed with 3 unresolved review issues."
    )
    assert (
        "metadata/dataset.csv row 1 (dataset_id=nuseds_fraser_coho_sample) "
        "field creator still contains an unresolved review placeholder "
        "(MISSING METADATA: add creator, team, or originating program.). "
        "Replace it before final validation." in message
    )
    assert "Resolve placeholder metadata" in message


# --- strict-path parity ----------------------------------------------------------


def test_review_dictionary_iris_warn_default_and_block_strict(tmp_path):
    root = _build_example(tmp_path / "review-dict")

    def editor(df):
        mask = df["column_name"] == "NATURAL_SPAWNERS_TOTAL"
        df.loc[mask, "term_iri"] = (
            "REVIEW:https://w3id.org/gcdfo/salmon#SpawnerAbundance"
        )
        return df

    _edit_csv(root / "metadata" / "column_dictionary.csv", editor)

    with pytest.warns(UserWarning, match="REVIEW-prefixed IRI values were found"):
        validate_salmon_datapackage(str(root), require_iris=False)

    with pytest.raises(ValueError) as excinfo:
        validate_salmon_datapackage(str(root), require_iris=True)
    assert str(excinfo.value) == (
        "Validation cannot pass while REVIEW-prefixed IRI values remain. "
        "Resolve these fields before final validation: "
        "term_iri: NATURAL_SPAWNERS_TOTAL (rows 8)"
    )


def test_strict_missing_measurement_term_iri_uses_r_message(tmp_path):
    root = _build_example(tmp_path / "missing-term")

    def editor(df):
        mask = df["column_name"] == "NATURAL_SPAWNERS_TOTAL"
        df.loc[mask, "term_iri"] = ""
        return df

    _edit_csv(root / "metadata" / "column_dictionary.csv", editor)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        validate_salmon_datapackage(str(root), require_iris=False)
        with pytest.raises(ValueError) as excinfo:
            validate_salmon_datapackage(str(root), require_iris=True)
    # R: "Measurement columns require term_iri; missing in rows 8."
    assert str(excinfo.value) == (
        "Measurement columns require term_iri; missing in rows 8."
    )


def test_bad_placement_iri_warns_default_and_blocks_strict(tmp_path):
    root = _build_example(tmp_path / "bad-placement")

    def editor(df):
        df["method_iri"] = "methods/weir-count"
        return df

    _edit_csv(root / "metadata" / "tables.csv", editor)

    with pytest.warns(
        UserWarning,
        match=r"validate_semantics\(\) reported 1 semantic issue",
    ):
        result = validate_salmon_datapackage(str(root), require_iris=False)
    semantic_issues = result["semantic_validation"]["issues"]
    assert len(semantic_issues) == 1
    assert "method_iri is not an absolute IRI" in semantic_issues["message"].iloc[0]

    with pytest.raises(ValueError, match="is not an absolute IRI"):
        validate_salmon_datapackage(str(root), require_iris=True)


def test_blank_observation_unit_iri_blocks_strict_only(tmp_path):
    root = _build_example(tmp_path / "blank-obs")

    def editor(df):
        df["observation_unit_iri"] = ""
        return df

    _edit_csv(root / "metadata" / "tables.csv", editor)

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        validate_salmon_datapackage(str(root), require_iris=False)

    with pytest.raises(ValueError) as excinfo:
        validate_salmon_datapackage(str(root), require_iris=True)
    message = str(excinfo.value)
    assert message.startswith(
        "Final validation failed with 1 unresolved review issue."
    )
    assert (
        f"metadata/tables.csv row 1 (table_id={TABLE}, "
        "file_name=data/nuseds-fraser-coho-sample.csv) field "
        "observation_unit_iri is blank. Final validation requires a resolved "
        "table observation-unit IRI." in message
    )


# --- placeholder fill (PARITY.md row 48, retired) --------------------------------


def test_placeholder_fill_matches_r_prose_exactly():
    """Byte differential over identical blank input, all frames.

    The written package this input produces came out byte-identical to
    metasalmon's — every metadata CSV, the data resource, and
    ``datapackage.json`` — in the chunk D differential.
    """
    dataset = fill_review_placeholders_dataset_meta(
        normalize_dataset_meta(pd.DataFrame({"dataset_id": ["demo-fill"]}))
    )
    row = dataset.iloc[0]
    assert row["title"] == "Demo Fill"
    assert row["description"] == (
        "MISSING DESCRIPTION: describe the contents and purpose of dataset "
        "'demo-fill'."
    )
    assert row["creator"] == (
        "MISSING METADATA: add creator, team, or originating program."
    )
    assert row["contact_name"] == (
        "MISSING METADATA: add primary contact name or team."
    )
    assert row["contact_email"] == "MISSING METADATA: add primary contact email."
    assert row["license"] == (
        "MISSING METADATA: add dataset license (for example, CC-BY-4.0)."
    )
    assert row["spec_version"] == "sdp-0.3.0"

    tables = fill_review_placeholders_table_meta(
        normalize_table_meta(
            pd.DataFrame({"table_id": ["counts"], "file_name": ["counts.csv"]})
        )
    )
    table_row = tables.iloc[0]
    assert table_row["table_label"] == "Counts"
    assert table_row["description"] == (
        "MISSING DESCRIPTION: describe what each row in table 'counts' "
        "represents."
    )
    assert table_row["observation_unit"] == (
        "MISSING METADATA: describe the observation unit for table 'counts'."
    )

    dictionary = fill_review_placeholders_dictionary(
        normalize_dictionary(
            pd.DataFrame(
                {
                    "dataset_id": ["demo-fill"],
                    "table_id": ["counts"],
                    "column_name": ["year"],
                    "value_type": ["string"],
                }
            )
        )
    )
    assert dictionary["column_description"].iloc[0] == (
        "MISSING DESCRIPTION: define what 'year' means in table 'counts'."
    )


def test_titleize_identifier_matches_tools_toTitleCase():
    """Pinned to ``.ms_titleize_identifier()`` over 23 probes.

    The oddities are R's, on purpose: ``tools::toTitleCase`` keeps its
    ``either`` words exactly as supplied (``under_over``), never lowers the
    first word but also never uppercases a single character (``a-tale-...``,
    ``x``), and leaves words already carrying capitals alone.
    """
    expected = {
        "demo-1": "Demo 1",
        "fraser_coho-2023": "Fraser Coho 2023",
        "nuseds_fraser_coho_sample": "Nuseds Fraser Coho Sample",
        "counts-of-fish": "Counts of Fish",
        "with": "With",
        "via-route": "Via Route",
        "3d-model": "3d Model",
        "et-al": "et Al",
        "under_over": "under over",
        "x-1998": "x 1998",
        "a-tale-of-two-rivers": "a Tale of Two Rivers",
        "cu_timeseries": "Cu Timeseries",
        "the-counts": "The Counts",
        "UPPER-case": "UPPER Case",
        "mixedCase_id": "mixedCase Id",
        "counts--of__fish": "Counts of Fish",
        " padded-id ": "Padded Id",
        "salmon.data": "Salmon.data",
        "run/timing": "Run/Timing",
        "x": "x",
        "1998": "1998",
        "escapement estimates": "Escapement Estimates",
        "all-of-them": "all of Them",
    }
    for raw, want in expected.items():
        assert titleize_identifier(raw) == want, raw


def test_infer_metadata_returns_placeholder_filled_frames():
    from metasalmonpy.metadata import (
        infer_dataset_metadata_from_resources,
        infer_table_metadata_from_resources,
    )

    resources = {"fish_counts": pd.DataFrame({"count": [1, 2]})}
    tables = infer_table_metadata_from_resources(resources, "demo-1")
    table_row = tables.iloc[0]
    assert table_row["file_name"] == "data/fish_counts.csv"
    assert table_row["table_label"] == "Fish Counts"
    assert table_row["description"] == (
        "MISSING DESCRIPTION: describe what each row in table 'fish_counts' "
        "represents."
    )
    assert table_row["observation_unit"] == (
        "MISSING METADATA: describe the observation unit for table "
        "'fish_counts'."
    )

    dataset = infer_dataset_metadata_from_resources(resources, "demo-1")
    row = dataset.iloc[0]
    assert row["title"] == "Demo 1"
    assert row["creator"] == (
        "MISSING METADATA: add creator, team, or originating program."
    )
    assert row["license"] == (
        "MISSING METADATA: add dataset license (for example, CC-BY-4.0)."
    )
    assert row["spec_version"] == "sdp-0.3.0"


# --- descriptor fixes found by the byte differential -----------------------------


def test_descriptor_blank_required_and_blank_label_match_r(tmp_path):
    """A blank ``required`` must NOT claim the column is required.

    ``iterrows()`` hands a boolean-dtype NA back as a truthy float ``nan``,
    so every blank ``required`` wrote ``constraints: {"required": true}`` —
    the inverted claim — until the chunk D descriptor byte differential
    caught it on the shipped example's RUN_TYPE row. R emits the block only
    for a genuine TRUE, and renders a blank ``column_label`` as an explicit
    ``"title": null`` rather than omitting the key.
    """
    import json

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        write_salmon_datapackage(
            resources={"counts": pd.DataFrame({"year": ["1998"]})},
            dataset_meta=pd.DataFrame({"dataset_id": ["demo-fill"]}),
            table_meta=pd.DataFrame(
                {"table_id": ["counts"], "file_name": ["counts.csv"]}
            ),
            dict_df=pd.DataFrame(
                {
                    "dataset_id": ["demo-fill"],
                    "table_id": ["counts"],
                    "column_name": ["year"],
                    "column_label": [pd.NA],
                    "column_description": [pd.NA],
                    "column_role": ["temporal"],
                    "value_type": ["string"],
                    "required": [pd.NA],
                }
            ),
            path=str(tmp_path / "blank-required"),
            overwrite=True,
        )
    descriptor = json.loads(
        (tmp_path / "blank-required" / "datapackage.json").read_text("utf-8")
    )
    field = descriptor["resources"][-1]["schema"]["fields"][0]
    assert "constraints" not in field
    assert "title" in field and field["title"] is None
