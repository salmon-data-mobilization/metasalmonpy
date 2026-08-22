#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path

try:
    from metasalmonpy.dictionary import CORE_SEMANTIC_FIELDS
    from metasalmonpy.metadata import read_sdp_csv
except Exception:  # pragma: no cover - script fallback when executed directly
    from dictionary import CORE_SEMANTIC_FIELDS
    from metadata import read_sdp_csv


def load_csv(path):
    # The shared SDP reader, not a bare pd.read_csv(): pandas' default NA
    # vocabulary destroyed a literal "NA" (a real fisheries code that appears
    # in code_value and IRI cells) and tokens like "null" on the way in, so
    # this script validated different values than the package reads. The empty
    # field is the only missing token (metasalmon 0.2.4) and it survives here
    # as the empty string, which the `== ""` checks below already handle.
    try:
        return read_sdp_csv(path)
    except Exception as exc:
        raise SystemExit(f"Failed to read {path}: {exc}")


def validate_dataset(df):
    required = {"dataset_id", "title", "description"}
    missing = required - set(df.columns)
    if missing:
        raise SystemExit(f"dataset.csv missing columns: {sorted(missing)}")
    if df.shape[0] != 1:
        raise SystemExit("dataset.csv must have exactly 1 row")
    if df["dataset_id"].isna().any() or (df["dataset_id"] == "").any():
        raise SystemExit("dataset_id must not be empty")
    return df["dataset_id"].iloc[0]


def validate_tables(df, dataset_id):
    required = {"dataset_id", "table_id", "file_name", "observation_unit"}
    missing = required - set(df.columns)
    if missing:
        raise SystemExit(f"tables.csv missing columns: {sorted(missing)}")
    if (df["dataset_id"] != dataset_id).any():
        raise SystemExit("tables.csv has dataset_id values that do not match dataset.csv")
    if df["table_id"].duplicated().any():
        dupes = df.loc[df["table_id"].duplicated(), "table_id"].tolist()
        raise SystemExit(f"tables.csv has duplicate table_id values: {dupes}")
    if (df["file_name"] == "").any():
        raise SystemExit("tables.csv has empty file_name values")
    return set(df["table_id"])


def validate_column_dictionary(df, dataset_id, table_ids, require_semantics: bool):
    required = {
        "dataset_id",
        "table_id",
        "column_name",
        "column_label",
        "column_description",
        "column_role",
        "value_type",
        "required",
    }
    missing = required - set(df.columns)
    if missing:
        raise SystemExit(f"column_dictionary.csv missing columns: {sorted(missing)}")
    if (df["dataset_id"] != dataset_id).any():
        raise SystemExit("column_dictionary.csv has dataset_id values that do not match dataset.csv")
    unknown_tables = set(df["table_id"]) - set(table_ids)
    if unknown_tables:
        raise SystemExit(f"column_dictionary.csv has unknown table_id values: {sorted(unknown_tables)}")
    for tbl, group in df.groupby("table_id"):
        if group["column_name"].duplicated().any():
            dupes = group.loc[group["column_name"].duplicated(), "column_name"].tolist()
            raise SystemExit(f"column_dictionary.csv has duplicate column_name in table {tbl}: {dupes}")
    # Basic measurement guardrail
    if require_semantics:
        measurement = df[df["column_role"] == "measurement"]
        if not measurement.empty:
            missing_fields = {}
            for field in CORE_SEMANTIC_FIELDS:
                if field not in df.columns:
                    missing_fields[field] = "column missing"
                else:
                    bad = measurement[field].isna() | (measurement[field] == "")
                    if bad.any():
                        missing_fields[field] = measurement.loc[bad, "column_name"].tolist()
            if missing_fields:
                raise SystemExit(f"measurement columns missing semantic fields: {missing_fields}")
    return df


def validate_codes(df, dataset_id, dict_df):
    required = {"dataset_id", "table_id", "column_name", "code_value", "code_label"}
    missing = required - set(df.columns)
    if missing:
        raise SystemExit(f"codes.csv missing columns: {sorted(missing)}")
    if (df["dataset_id"] != dataset_id).any():
        raise SystemExit("codes.csv has dataset_id values that do not match dataset.csv")
    categorical = set(dict_df.loc[dict_df["column_role"] == "categorical", "column_name"])
    code_columns = set(df["column_name"])
    unused = code_columns - categorical
    if unused:
        raise SystemExit(f"codes.csv has columns not marked categorical in column_dictionary: {sorted(unused)}")


def main():
    parser = argparse.ArgumentParser(description="Validate Salmon Data Package metadata CSVs.")
    parser.add_argument("--dataset", required=True, help="Path to dataset.csv")
    parser.add_argument("--tables", required=True, help="Path to tables.csv")
    parser.add_argument("--dictionary", required=True, help="Path to column_dictionary.csv")
    parser.add_argument("--codes", help="Path to codes.csv (optional)")
    parser.add_argument(
        "--require-semantics",
        action="store_true",
        help=(
            "Require measurement columns to have "
            + ", ".join(CORE_SEMANTIC_FIELDS)
            + " IRIs"
        )
    )
    args = parser.parse_args()

    dataset_path = Path(args.dataset)
    tables_path = Path(args.tables)
    dict_path = Path(args.dictionary)
    codes_path = Path(args.codes) if args.codes else None

    dataset_df = load_csv(dataset_path)
    tables_df = load_csv(tables_path)
    dict_df = load_csv(dict_path)
    dataset_id = validate_dataset(dataset_df)
    table_ids = validate_tables(tables_df, dataset_id)
    dict_df = validate_column_dictionary(dict_df, dataset_id, table_ids, require_semantics=args.require_semantics)
    if codes_path:
        codes_df = load_csv(codes_path)
        validate_codes(codes_df, dataset_id, dict_df)

    print("Validation passed.")


if __name__ == "__main__":
    main()
