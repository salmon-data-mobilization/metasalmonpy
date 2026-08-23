from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Optional, Union

import pandas as pd

from .sdp_schema import sdp_profile_version


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


def csv_na_token() -> str:
    """The one missing-value token, used by every canonical read and write.

    Mirrors ``.ms_csv_na_token()`` (metasalmon 0.2.4). It exists as a function
    rather than a literal because the contract is only sound if both sides
    agree, and the sides live in different files: readr's own defaults do not
    agree with each other — it *writes* a missing value as the two characters
    ``NA`` and *reads* ``c("", "NA")`` as missing — so a value that is
    literally the string ``"NA"``, a real fisheries gear code, was written
    indistinguishably from a missing value and destroyed at write time, where
    no reader could recover it.

    The residual ambiguity is deliberate and shared with R: an empty string
    and a missing value share the empty field. CSV cannot distinguish them
    without quoting conventions readers disagree about, and the dictionary
    already treats blank as absent.

    One representation note that is this package's own (PARITY.md row 21):
    R's readers map the token to ``NA_character_``, while ``read_sdp_csv``
    keeps it as the empty string. Same token set, different in-memory
    spelling — which is why the reader passes ``na_values=[]`` rather than
    ``na_values=[csv_na_token()]``: mapping the token to ``NaN`` would change
    the representation row 21 records, not the bytes on disk this token
    governs.
    """
    return ""


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
    * **The empty field is the only missing token** — ``csv_na_token()``,
      the same single authority the writers render missing values through.
      metasalmon 0.2.4 established that ``"NA"`` is a real fisheries gear
      code, so a literal ``NA`` round-trips as the string it is rather than
      being destroyed at read time.

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
# ``spec_version`` to. metasalmon reads it from its vendored
# ``inst/extdata/schema/sdp.rules.yaml`` via ``.ms_sdp_profile_version()``.
#
# The 0.1.8 milestone landed that bundle here (from the upstream ``sdp-0.2.0``
# tag), so this is now the same read rather than a standalone constant — the
# retirement condition recorded against the constant at 0.1.7. A profile bump
# is a bundle swap; nothing in Python source states the version.
def __getattr__(name):
    """Resolve ``SDP_PROFILE_VERSION`` from the loaded bundle, at access time.

    It was a module constant evaluated at import until the 0.2.0 rung. The
    schema bundle is now loaded remote-first (``sdp_schema.load_sdp_schema``),
    and an import-time read would have turned every ``import metasalmonpy``
    into a network call — and pinned the answer before a caller could choose a
    source. The loader caches per process, so repeated access is free.
    """
    if name == "SDP_PROFILE_VERSION":
        return sdp_profile_version()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

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
    # sdp-0.3.0 placement fields: a dataset-wide protocol citation.
    "protocol_iri",
    "protocol_citation",
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
    # sdp-0.3.0 placement fields: a table-constant procedure and its protocol
    # live on tables.csv (a method describes how a value was produced; it was
    # never part of what the value *is*, so it left the column dictionary).
    "protocol_iri",
    "protocol_citation",
    "method_iri",
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
    # sdp-0.3.0: a statistical modifier (I-ADOPT StatisticalModifier) is part
    # of variable identity — a *mean* weight and a *maximum* weight are
    # different variables — so it belongs in the dictionary. ``method_iri``
    # never did, and moved to ``tables.csv`` / the data (see TABLE_META_COLUMNS).
    "statistical_modifier_iri",
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


# --- review placeholders and identifier titles ------------------------------
#
# In metasalmon these helpers live in ``R/dictionary-helpers.R``
# (``.ms_fill_review_placeholders_*``, ``.ms_titleize_identifier``); they sit
# here rather than in ``dictionary.py`` because the ``infer_*`` functions in
# this module apply them, and ``dictionary.py`` imports this module.


def scalar_text(value) -> str:
    """Mirror ``.ms_scalar_text()``: first element as trimmed text, NA -> ""."""
    if isinstance(value, (pd.Series, list, tuple)):
        value = value[0] if len(value) else None
    if value is None or (not isinstance(value, (list, dict)) and pd.isna(value)):
        return ""
    return str(value).strip(READR_TRIM_CHARS)


# ``.ms_is_review_placeholder()``: the three placeholder spellings the fill
# helpers write. Deliberately narrower than "anything starting MISSING" — a
# bare ``REVIEW:`` IRI marker has its own dedicated reporting paths
# (``validate_dictionary`` and ``_collect_review_iri_issues``).
_REVIEW_PLACEHOLDER_RE = re.compile(
    r"^\s*(MISSING METADATA|MISSING DESCRIPTION|REVIEW REQUIRED)\s*:",
    re.IGNORECASE,
)


def is_review_placeholder(value) -> bool:
    """Mirror ``.ms_is_review_placeholder()``."""
    text = scalar_text(value)
    return bool(text) and _REVIEW_PLACEHOLDER_RE.match(text) is not None


def _humanize_identifier(value) -> str:
    """Mirror ``.ms_humanize_identifier()``: separators to single spaces."""
    text = re.sub(r"[-_]+", " ", str(value))
    text = re.sub(r"\s+", " ", text)
    return text.strip(READR_TRIM_CHARS)


# ``tools::toTitleCase`` ported verbatim (R 4.5.2): the same word lists, the
# same token split (each delimiter is its own token), the same keep rules.
# The fill helpers write its output into ``metadata/dataset.csv`` and
# ``metadata/tables.csv``, so quirks like ``"under_over" -> "under over"``
# (both words are in the keep-as-is list) are part of the byte contract and
# pinned by tests against R-measured output — do not "fix" them here.
_TITLE_ALONE = frozenset({
    "2D", "3D", "AIC", "BayesX", "GoF", "HTML", "LaTeX", "MonetDB", "OpenBUGS",
    "TeX", "U.S.", "U.S.A.", "WinBUGS", "aka", "et", "al.", "ggplot2", "i.e.",
    "jar", "jars", "ncdf", "netCDF", "rgl", "rpart", "xls", "xlsx",
    # R's ``either`` list: words kept exactly as supplied.
    "all", "above", "after", "along", "also", "among", "any", "both", "can",
    "few", "it", "less", "log", "many", "may", "more", "over", "some", "their",
    "then", "this", "under", "until", "using", "von", "when", "where", "which",
    "will", "without", "yet", "you", "your",
})
_TITLE_LOWER_PATTERN = (
    r"^(a|an|and|are|as|at|be|but|by|en|for|if|in|is|nor|not|of|on|or|per|so"
    r"|the|to|v[.]?|via|vs[.]?|from|into|than|that|with)$"
)
_TITLE_LOWER_RE = re.compile(_TITLE_LOWER_PATTERN, re.IGNORECASE)
_TITLE_LOWER_CASE_SENSITIVE_RE = re.compile(_TITLE_LOWER_PATTERN)
_TITLE_DELIMITERS = ' -/"()\n\t'


def _split_title_tokens(text: str) -> list[str]:
    """R's ``C_splitString(x, " -/\\"()\\n\\t")``: delimiters are 1-char tokens."""
    tokens: list[str] = []
    current = ""
    for character in text:
        if character in _TITLE_DELIMITERS:
            if current:
                tokens.append(current)
                current = ""
            tokens.append(character)
        else:
            current += character
    if current:
        tokens.append(current)
    return tokens


def _title_case_word(word: str) -> str:
    first = word[:1]
    if len(word) >= 3 and first in ("'", '"'):
        return first + word[1:2].upper() + word[2:].lower()
    return first.upper() + word[1:].lower()


def _to_title_case(text) -> str:
    if text is None or pd.isna(text):
        return text
    tokens = _split_title_tokens(str(text))
    n = len(tokens)
    alone = [
        token in _TITLE_ALONE or re.match(r"^'.*'$", token) is not None
        for token in tokens
    ]
    havecaps = [
        re.match(r"^[^\W\d_].*[A-Z]", token) is not None for token in tokens
    ]
    lower = [bool(_TITLE_LOWER_RE.match(token)) for token in tokens]
    if lower:
        lower[0] = False
    # A word after ``foo: `` or ``foo- `` stays capitalized — unless the dash
    # stands alone and the word is one of the lowercase set (case-sensitive
    # here, exactly as in R).
    for index in range(n):
        if (
            re.search(r"[-:]$", tokens[index])
            and index + 2 < n
            and tokens[index + 1] == " "
            and re.match(r"^['0-9A-Za-z]", tokens[index + 2])
            and not (
                tokens[index] == "-"
                and _TITLE_LOWER_CASE_SENSITIVE_RE.match(tokens[index + 2])
            )
        ):
            lower[index + 2] = False
    for index in range(n - 1):
        if tokens[index] == '"':
            lower[index + 1] = False
    tokens = [
        token.lower() if flag else token for token, flag in zip(tokens, lower)
    ]
    out = []
    for token, is_alone, has_caps, is_lower in zip(tokens, alone, havecaps, lower):
        keep = has_caps or is_lower or len(token) == 1 or is_alone
        out.append(token if keep else _title_case_word(token))
    return "".join(out)


def titleize_identifier(value) -> str:
    """Mirror ``.ms_titleize_identifier()``: humanize, then title-case."""
    humanized = _humanize_identifier(value)
    if not humanized:
        return humanized
    return _to_title_case(humanized)


def _blank_mask(series: pd.Series) -> pd.Series:
    """R's ``is.na(x) | trimws(x) == ""`` over one metadata column."""
    return pd.Series(
        [
            pd.isna(value) or not str(value).strip(READR_TRIM_CHARS)
            for value in series
        ],
        index=series.index,
    )


def fill_review_placeholders_dataset_meta(dataset_meta: pd.DataFrame) -> pd.DataFrame:
    """Mirror ``.ms_fill_review_placeholders_dataset_meta()`` exactly.

    Prose and coverage were converged on current metasalmon by differential
    run (S10 chunk D), retiring PARITY.md row 48: R fills ``creator``,
    ``contact_name``, ``contact_email`` and ``license`` with ``MISSING
    METADATA:`` guidance, titleizes a blank ``title`` from ``dataset_id``, and
    writes dataset-specific ``MISSING DESCRIPTION:`` prose.
    """
    out = dataset_meta.copy()

    if "title" in out.columns:
        blank = _blank_mask(out["title"])
        if blank.any():
            out.loc[blank, "title"] = [
                titleize_identifier(value) for value in out.loc[blank, "dataset_id"]
            ]

    if "description" in out.columns:
        blank = _blank_mask(out["description"])
        if blank.any():
            out.loc[blank, "description"] = [
                "MISSING DESCRIPTION: describe the contents and purpose of "
                f"dataset '{value}'."
                for value in out.loc[blank, "dataset_id"]
            ]

    for column, placeholder in (
        ("creator", "MISSING METADATA: add creator, team, or originating program."),
        ("contact_name", "MISSING METADATA: add primary contact name or team."),
        ("contact_email", "MISSING METADATA: add primary contact email."),
        ("license", "MISSING METADATA: add dataset license (for example, CC-BY-4.0)."),
    ):
        if column in out.columns:
            blank = _blank_mask(out[column])
            if blank.any():
                out.loc[blank, column] = placeholder

    if "spec_version" in out.columns:
        blank = _blank_mask(out["spec_version"])
        if blank.any():
            out.loc[blank, "spec_version"] = sdp_profile_version()

    return out


def fill_review_placeholders_table_meta(table_meta: pd.DataFrame) -> pd.DataFrame:
    """Mirror ``.ms_fill_review_placeholders_table_meta()`` exactly."""
    out = table_meta.copy()

    if "table_label" in out.columns:
        blank = _blank_mask(out["table_label"])
        if blank.any():
            out.loc[blank, "table_label"] = [
                titleize_identifier(value) for value in out.loc[blank, "table_id"]
            ]

    if "description" in out.columns:
        blank = _blank_mask(out["description"])
        if blank.any():
            out.loc[blank, "description"] = [
                f"MISSING DESCRIPTION: describe what each row in table '{value}' "
                "represents."
                for value in out.loc[blank, "table_id"]
            ]

    if "observation_unit" in out.columns:
        blank = _blank_mask(out["observation_unit"])
        if blank.any():
            out.loc[blank, "observation_unit"] = [
                f"MISSING METADATA: describe the observation unit for table "
                f"'{value}'."
                for value in out.loc[blank, "table_id"]
            ]

    return out


def fill_review_placeholders_dictionary(dictionary: pd.DataFrame) -> pd.DataFrame:
    """Mirror ``.ms_fill_review_placeholders_dictionary()`` exactly."""
    out = dictionary.copy()

    if "column_description" in out.columns:
        blank = _blank_mask(out["column_description"])
        if blank.any():
            out.loc[blank, "column_description"] = [
                f"MISSING DESCRIPTION: define what '{column}' means in table "
                f"'{table}'."
                for column, table in zip(
                    out.loc[blank, "column_name"], out.loc[blank, "table_id"]
                )
            ]

    return out


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
                # R: ``file.path("data", paste0(tab_id, ".csv"))`` and a
                # titleized label; the returned frame is placeholder-filled,
                # so ``description``/``observation_unit`` come back as
                # ``MISSING ...:`` prose exactly as metasalmon returns them
                # (S10 chunk D differential; previously bare NA / table_id).
                "file_name": f"data/{table_id}.csv",
                "table_label": titleize_identifier(table_id),
                "description": pd.NA,
                "observation_unit": pd.NA,
                "observation_unit_iri": pd.NA,
                "primary_key": id_cols[0] if id_cols else pd.NA,
            }
        )
    return fill_review_placeholders_table_meta(
        normalize_table_meta(pd.DataFrame(rows))
    )


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

    # The returned frame is placeholder-filled, exactly as metasalmon's
    # ``infer_dataset_metadata_from_resources()`` returns it (S10 chunk D
    # differential): titleized ``title``, ``MISSING ...:`` prose for
    # description/creator/contacts/license, and the profile ``spec_version``.
    return fill_review_placeholders_dataset_meta(
        normalize_dataset_meta(
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
                    "spec_version": [pd.NA],
                }
            )
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
    "csv_na_token",
    "ensure_resource_mapping",
    "fill_review_placeholders_dataset_meta",
    "fill_review_placeholders_dictionary",
    "fill_review_placeholders_table_meta",
    "infer_codes_from_resources",
    "infer_dataset_metadata_from_resources",
    "infer_table_metadata_from_resources",
    "is_review_placeholder",
    "normalize_codes",
    "normalize_dataset_meta",
    "normalize_dictionary",
    "normalize_table_meta",
    "parse_logical",
    "read_sdp_csv",
    "scalar_text",
    "titleize_identifier",
]
