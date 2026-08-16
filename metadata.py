from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Optional, Union

import pandas as pd


# readr's ``trim_ws = TRUE`` and R's ``trimws()`` strip exactly these
# characters. U+00A0 and U+3000 are deliberately absent: neither R function
# treats them as whitespace, so neither may this package.
READR_TRIM_CHARS = " \t\r\n"

# metasalmon calls ``grepl()`` WITHOUT ``perl = TRUE`` in every validator that
# uses a POSIX character class, so those classes are resolved by TRE, which is
# Unicode-aware in a UTF-8 locale. The exact membership below was enumerated by
# running ``grepl()`` over every codepoint up to U+2FFFF under metasalmon
# v0.1.7's R 4.5.2. Approximating either class with Python's ``\s``/``\S`` is
# wrong in BOTH directions and must never be done:
#
# * Python's ``\s`` is *wider* — it treats U+0085, U+00A0, U+2007, U+202F and
#   U+001C-001F as whitespace where TRE does not, so ``[^\s]+`` rejects values
#   R accepts.
# * Python's ``\s`` is also *narrower* — it does not include U+1680, U+205F or
#   U+3000 in the ASCII-only ``re.ASCII`` mode some call sites reach for.
#
# Where metasalmon *does* pass ``perl = TRUE`` (``measurement-decompositions.R``
# line 57) PCRE resolves ``[[:space:]]`` as plain ASCII ``\t-\r`` plus space —
# verified by the same enumeration — so an ASCII class is correct there and
# these constants must NOT be applied to it.
#
# Retirement condition: these constants stay for as long as metasalmon resolves
# POSIX classes through TRE. They are only removable if metasalmon itself
# switches those validators to ``perl = TRUE`` (or to explicit ranges), at which
# point the replacement must be re-enumerated against that release, not guessed.
#
# R ``[[:space:]]`` -- note the deliberate gaps: U+2007, U+00A0, U+0085 and
# U+202F are NOT whitespace to TRE.
R_SPACE_CLASS = (
    "\t-\r\x20\u1680\u2000-\u2006\u2008-\u200a"
    "\u2028\u2029\u205f\u3000"
)

# R ``[[:cntrl:]]`` -- C0 and C1 controls plus the Unicode line/paragraph
# separators.
R_CNTRL_CLASS = "\x00-\x1f\x7f-\x9f\u2028\u2029"


def _trim_cell(value):
    return value.strip(READR_TRIM_CHARS) if isinstance(value, str) else value


def read_sdp_csv(path: Union[str, Path], **kwargs) -> pd.DataFrame:
    """Read an SDP CSV exactly the way metasalmon's ``readr::read_csv`` does.

    Every metadata, dictionary, and vocabulary reader in this package goes
    through this one function, because the three behaviours it encodes are
    individually easy to get wrong and were previously inconsistent between
    modules:

    * **All-character columns**, mirroring R's
      ``col_types = cols(.default = col_character())``.
    * **``trim_ws = TRUE``** — ``READR_TRIM_CHARS`` are stripped from every
      header and every field, inside quotes as well as outside, *before* the
      missing-value token is matched. ``pandas`` does none of this, so
      ``a, b`` (a space after the comma) parsed as ``" b"`` here while R read
      ``"b"``.
    * **The empty field is the only missing token.** metasalmon 0.2.4
      established that ``"NA"`` is a real fisheries gear code, so a literal
      ``NA`` round-trips as the string it is rather than being destroyed at
      read time.

    ``skipinitialspace`` is what lets pandas see ``  "quoted, value"  `` as
    one quoted field the way readr's tokenizer does; the explicit strip
    afterwards finishes the job for tabs and trailing whitespace.
    """
    frame = pd.read_csv(
        path,
        dtype=str,
        keep_default_na=False,
        na_values=[],
        skipinitialspace=True,
        **kwargs,
    )
    frame.columns = [_trim_cell(column) for column in frame.columns]
    if not frame.empty:
        frame = frame.apply(lambda column: column.map(_trim_cell))
    return frame


# The SDP profile version this package writes and defaults a blank
# ``spec_version`` to. metasalmon v0.1.7 stopped hard-coding "sdp-0.1.0" and
# started reading it from its vendored ``inst/extdata/schema/sdp.rules.yaml``,
# whose ``version:`` key is ``sdp-0.2.0`` at that tag (confirmed by running
# ``.ms_sdp_profile_version()`` against the v0.1.7 extraction). This package
# does not vendor the rules bundle until the 0.1.8 milestone, so the version is
# a single named constant instead of three scattered literals.
#
# Retirement condition: replace this constant with a read of the vendored
# ``sdp.rules.yaml`` when the 0.1.8 milestone lands that bundle. Until then a
# profile bump must edit exactly this line.
SDP_PROFILE_VERSION = "sdp-0.2.0"

DATASET_META_COLUMNS = [
    "dataset_id",
    "title",
    "description",
    "creator",
    "contact_name",
    "contact_email",
    "license",
    "contact_org",
    "contact_position",
    "temporal_start",
    "temporal_end",
    "spatial_extent",
    "dataset_type",
    "source_citation",
    "update_frequency",
    "topic_categories",
    "keywords",
    "security_classification",
    "provenance_note",
    "created",
    "modified",
    "spec_version",
]

TABLE_META_COLUMNS = [
    "dataset_id",
    "table_id",
    "file_name",
    "table_label",
    "description",
    "observation_unit",
    "observation_unit_iri",
    "primary_key",
]

DICTIONARY_COLUMNS = [
    "dataset_id",
    "table_id",
    "column_name",
    "column_label",
    "column_description",
    "column_role",
    "value_type",
    "required",
    "unit_label",
    "unit_iri",
    "term_iri",
    "term_type",
    "property_iri",
    "entity_iri",
    "constraint_iri",
    "method_iri",
]

CODES_COLUMNS = [
    "dataset_id",
    "table_id",
    "column_name",
    "code_value",
    "code_label",
    "code_description",
    "vocabulary_iri",
    "term_iri",
    "term_type",
]


def align_columns(df: Optional[pd.DataFrame], columns: list[str]) -> Optional[pd.DataFrame]:
    if df is None:
        return None
    out = pd.DataFrame(df).copy()
    for col in columns:
        if col not in out.columns:
            out[col] = pd.NA
    return out[columns + [col for col in out.columns if col not in columns]]


def normalize_dataset_meta(dataset_meta: pd.DataFrame) -> pd.DataFrame:
    return align_columns(dataset_meta, DATASET_META_COLUMNS)  # type: ignore[return-value]


def normalize_table_meta(table_meta: pd.DataFrame) -> pd.DataFrame:
    return align_columns(table_meta, TABLE_META_COLUMNS)  # type: ignore[return-value]


def normalize_dictionary(dict_df: pd.DataFrame) -> pd.DataFrame:
    out = align_columns(dict_df, DICTIONARY_COLUMNS)
    if out is None:
        return pd.DataFrame(columns=DICTIONARY_COLUMNS)
    if "required" in out.columns:
        out["required"] = parse_logical(out["required"])
    return out


def normalize_codes(codes: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    return align_columns(codes, CODES_COLUMNS)


def parse_logical(values) -> pd.Series:
    if isinstance(values, pd.Series) and pd.api.types.is_bool_dtype(values):
        return values.copy()

    def one(value):
        if pd.isna(value):
            return pd.NA
        if isinstance(value, bool):
            return value
        token = str(value).strip().upper()
        if not token:
            return pd.NA
        if token == "TRUE":
            return True
        if token == "FALSE":
            return False
        return pd.NA

    return pd.Series(
        [one(v) for v in values],
        index=getattr(values, "index", None),
        dtype="boolean",
    )


def ensure_resource_mapping(resources, table_id: str = "table-1") -> dict[str, pd.DataFrame]:
    if isinstance(resources, pd.DataFrame):
        return {table_id: resources.copy()}
    if not isinstance(resources, Mapping):
        raise TypeError("resources must be a pandas DataFrame or a named mapping of DataFrames.")
    if len(resources) == 0:
        raise ValueError("resources cannot be empty.")
    if any(not str(name) for name in resources.keys()):
        raise ValueError("resources names must be non-empty table IDs.")
    out: dict[str, pd.DataFrame] = {}
    for name, value in resources.items():
        if not isinstance(value, pd.DataFrame):
            raise TypeError("All resources must be pandas DataFrames.")
        out[str(name)] = value.copy()
    if len(out) != len(resources):
        raise ValueError("resources names must be unique.")
    return out


def _is_usable_primary_key(series: pd.Series) -> bool:
    """Mirror metasalmon v0.1.7's primary-key candidate filter.

    0.1.7 stopped naming the first ID-shaped column as the primary key and
    started requiring the column to be able to *be* one: no missing value, no
    blank-after-trim value, and no duplicate. A declared primary key that the
    data contradicts fails the package's own validation on the next read.
    """
    values = pd.Series(series)
    if values.isna().any():
        return False
    text_values = [str(value) for value in values]
    if any(not value.strip() for value in text_values):
        return False
    return len(set(text_values)) == len(text_values)


def infer_table_metadata_from_resources(resources: Mapping[str, pd.DataFrame], dataset_id: str = "dataset-1") -> pd.DataFrame:
    rows = []
    for table_id, df in resources.items():
        id_cols = [
            col
            for col in df.columns
            if re.search(r"(^|_)id$|_id$|^id_", str(col).lower())
            and _is_usable_primary_key(df[col])
        ]
        rows.append(
            {
                "dataset_id": dataset_id,
                "table_id": table_id,
                "file_name": f"{table_id}.csv",
                "table_label": table_id,
                "description": pd.NA,
                "observation_unit": pd.NA,
                "observation_unit_iri": pd.NA,
                "primary_key": id_cols[0] if id_cols else pd.NA,
            }
        )
    return normalize_table_meta(pd.DataFrame(rows))


def infer_codes_from_resources(resources: Mapping[str, pd.DataFrame], dataset_id: str = "dataset-1") -> pd.DataFrame:
    rows = []
    for table_id, df in resources.items():
        for col in df.columns:
            series = df[col]
            if not (
                pd.api.types.is_object_dtype(series)
                or pd.api.types.is_string_dtype(series)
                or isinstance(series.dtype, pd.CategoricalDtype)
            ):
                continue
            values = pd.Series(series.dropna().astype(str).unique())
            if len(values) == 0 or len(values) > 30:
                continue
            for value in values:
                rows.append(
                    {
                        "dataset_id": dataset_id,
                        "table_id": table_id,
                        "column_name": col,
                        "code_value": value,
                        "code_label": value,
                        "code_description": pd.NA,
                        "vocabulary_iri": pd.NA,
                        "term_iri": pd.NA,
                        "term_type": pd.NA,
                    }
                )
    out = normalize_codes(pd.DataFrame(rows))
    return out if out is not None else pd.DataFrame(columns=CODES_COLUMNS)


def _parse_dates(series: pd.Series) -> pd.Series:
    if pd.api.types.is_datetime64_any_dtype(series):
        return pd.to_datetime(series.dropna(), errors="coerce").dropna().dt.date
    parsed = pd.to_datetime(series.dropna().astype(str), errors="coerce")
    return parsed.dropna().dt.date


def infer_dataset_metadata_from_resources(resources: Mapping[str, pd.DataFrame], dataset_id: str = "dataset-1") -> pd.DataFrame:
    dates = []
    lat_values = []
    lon_values = []
    keywords = []

    for df in resources.values():
        names = [str(col) for col in df.columns]
        keywords.extend(re.sub(r"_", " ", name).strip() for name in names)
        for col in names:
            lower = col.lower()
            if re.search(r"date|time|timestamp|dtt|obsdate|survey|year", lower):
                dates.extend(_parse_dates(df[col]).tolist())
            if re.search(r"(^|_)lat(itude)?($|_)", lower):
                lat_values.extend(pd.to_numeric(df[col], errors="coerce").dropna().tolist())
            if re.search(r"(^|_)lon|(^|_)long(itude)?($|_)", lower):
                lon_values.extend(pd.to_numeric(df[col], errors="coerce").dropna().tolist())

    temporal_start = min(dates).isoformat() if dates else pd.NA
    temporal_end = max(dates).isoformat() if dates else pd.NA
    spatial_extent = (
        f"lon={min(lon_values)}..{max(lon_values)}, lat={min(lat_values)}..{max(lat_values)}"
        if lat_values and lon_values
        else pd.NA
    )
    unique_keywords = []
    seen = set()
    for keyword in keywords:
        key = keyword.lower()
        if key and key not in seen:
            unique_keywords.append(keyword)
            seen.add(key)

    return normalize_dataset_meta(
        pd.DataFrame(
            {
                "dataset_id": [dataset_id],
                "title": [pd.NA],
                "description": [pd.NA],
                "creator": [pd.NA],
                "contact_name": [pd.NA],
                "contact_email": [pd.NA],
                "license": [pd.NA],
                "contact_org": [pd.NA],
                "contact_position": [pd.NA],
                "temporal_start": [temporal_start],
                "temporal_end": [temporal_end],
                "spatial_extent": [spatial_extent],
                "dataset_type": [pd.NA],
                "source_citation": [pd.NA],
                "update_frequency": [pd.NA],
                "topic_categories": [pd.NA],
                "keywords": ["; ".join(unique_keywords[:8])],
                "security_classification": [pd.NA],
                "provenance_note": [pd.NA],
                "created": [pd.NA],
                "modified": [pd.NA],
                "spec_version": [SDP_PROFILE_VERSION],
            }
        )
    )


__all__ = [
    "CODES_COLUMNS",
    "DATASET_META_COLUMNS",
    "DICTIONARY_COLUMNS",
    "READR_TRIM_CHARS",
    "R_CNTRL_CLASS",
    "R_SPACE_CLASS",
    "SDP_PROFILE_VERSION",
    "TABLE_META_COLUMNS",
    "align_columns",
    "ensure_resource_mapping",
    "infer_codes_from_resources",
    "infer_dataset_metadata_from_resources",
    "infer_table_metadata_from_resources",
    "normalize_codes",
    "normalize_dataset_meta",
    "normalize_dictionary",
    "normalize_table_meta",
    "parse_logical",
    "read_sdp_csv",
]
