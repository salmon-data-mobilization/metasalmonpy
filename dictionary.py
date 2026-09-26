from __future__ import annotations

import datetime as _dt
import re
import string
import warnings
from collections.abc import Mapping
from typing import Optional, Sequence, Union

try:
    import pandas as pd
except ImportError as exc:  # pragma: no cover - import guard
    raise ImportError("metasalmonpy requires pandas; install via `pip install pandas`.") from exc

from .metadata import (
    READR_TRIM_CHARS,
    _code_list_applies,
    ensure_resource_mapping,
    infer_codes_from_resources,
    infer_dataset_metadata_from_resources,
    infer_table_metadata_from_resources,
    normalize_dictionary,
    parse_logical,
    values_form_code_list,
)

VALID_VALUE_TYPES = {"string", "integer", "number", "boolean", "date", "datetime"}
VALID_COLUMN_ROLES = {"identifier", "attribute", "measurement", "temporal", "categorical"}
REQUIRED_COLUMNS = [
    "dataset_id",
    "table_id",
    "column_name",
    "column_label",
    "column_description",
    "column_role",
    "value_type",
    "required",
]
SEMANTIC_COLUMNS = [
    "unit_label",
    "unit_iri",
    "term_iri",
    "term_type",
    "property_iri",
    "entity_iri",
    "constraint_iri",
    "statistical_modifier_iri",
]

# Core ontology fields used in strict semantic checks for measurements.
# - term/property/entity/unit are required for explicit I-ADOPT-style semantics.
# - constraint/statistical modifier are optional qualifiers (e.g., age/phase
#   tags, or the aggregation that is part of the variable's identity).
CORE_SEMANTIC_FIELDS = ["term_iri", "property_iri", "entity_iri", "unit_iri"]
OPTIONAL_SEMANTIC_FIELDS = ["constraint_iri", "statistical_modifier_iri"]
MEASUREMENT_SEMANTIC_FIELDS = CORE_SEMANTIC_FIELDS + OPTIONAL_SEMANTIC_FIELDS


def _ensure_dataframe(df, name: str = "df") -> pd.DataFrame:
    if isinstance(df, pd.DataFrame):
        return df.copy()
    try:
        return pd.DataFrame(df)
    except Exception as exc:  # pragma: no cover - defensive
        raise TypeError(f"{name} must be a pandas DataFrame or convertible object") from exc


def infer_value_type(series: pd.Series) -> str:
    """
    Infer a value_type for a column.
    """
    s = pd.Series(series)
    dtype = s.dtype

    # A timestamp column is ``datetime``, whatever time of day it carries.
    #
    # **Behaviour change at the 0.2.0 rung, and a public one here** (PARITY.md
    # row 5 makes ``infer_value_type`` public API in this package). It used to
    # collapse a datetime column whose values were all midnight to ``"date"``.
    # metasalmon 0.2.0 fixed the mirror-image defect from the other side — its
    # ``inherits(col, "Date") || inherits(col, "POSIXt")`` tested the wider
    # class first, so ``"datetime"`` was never inferred at all and timestamps
    # round-tripped as dates. The rule both now use is the class, not the
    # values: ``POSIXt``/``datetime64`` is ``datetime``, ``Date``/
    # ``datetime.date`` is ``date``. A single midnight timestamp is a real
    # instant, and a heuristic that erases its time component silently
    # rewrites a user's data on the round trip.
    if pd.api.types.is_datetime64_any_dtype(dtype):
        return "datetime"

    # ``date`` has no pandas dtype, so R's ``Date`` class maps to an object
    # column of ``datetime.date``. This is also what ``resource_types``
    # produces for a declared ``date`` column, which is what makes the round
    # trip stable.
    non_null = s.dropna()
    if len(non_null) > 0:
        sample = non_null.iloc[0]
        if isinstance(sample, _dt.date) and not isinstance(sample, _dt.datetime):
            return "date"

    if pd.api.types.is_bool_dtype(dtype):
        return "boolean"
    if pd.api.types.is_integer_dtype(dtype):
        return "integer"
    if pd.api.types.is_numeric_dtype(dtype):
        return "number"
    return "string"


def _name_tokens(value) -> list[str]:
    """Mirror ``.ms_name_tokens``: split camelCase, then ``._-`` and whitespace."""
    text = "" if value is None else str(value)
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    text = re.sub(r"[._-]+", " ", text).lower()
    return [token for token in text.split() if token]


def _is_categorical(series: pd.Series) -> bool:
    """R's ``inherits(col, "factor")`` — pandas' Categorical is the counterpart."""
    return isinstance(getattr(series, "dtype", None), pd.CategoricalDtype)


def _character_values(series: pd.Series) -> list[str]:
    """Non-missing cells as trimmed text, the way ``as.character()`` feeds R."""
    values = pd.Series(series).dropna()
    return [
        text
        for text in (str(value).strip() for value in values)
        if text
    ]


def _values_look_yearish(series: pd.Series) -> bool:
    """Mirror ``.ms_values_look_yearish``."""
    values = _character_values(series)
    if not values:
        return False
    if not all(re.fullmatch(r"[12][0-9]{3}", value) for value in values):
        return False
    return all(1800 <= int(value) <= 2500 for value in values)


def _values_look_numericish(series: pd.Series, min_fraction: float = 0.8) -> bool:
    """Mirror ``.ms_values_look_numericish``."""
    if pd.api.types.is_bool_dtype(series):
        return False
    if pd.api.types.is_numeric_dtype(series):
        return True

    values = _character_values(series)
    values = [
        value
        for value in values
        if value.lower() not in ("na", "n/a", "nd", "null", "nil", "missing")
    ]
    if not values:
        return False

    parsed = 0
    for value in values:
        normalized = value.replace(",", "").replace("%", "")
        normalized = re.sub(r"^[<>]=?\s*", "", normalized).strip()
        try:
            float(normalized)
        except ValueError:
            continue
        parsed += 1
    return parsed / len(values) >= min_fraction


_MEASUREMENT_TOKENS = frozenset(
    """count counts total totals number numbers amount quantity measure
    measurement measurements abundance abundances spawner spawners recruit
    recruits escapement escapements biomass density densities rate rates ratio
    ratios proportion proportions percent percentage length lengths weight
    weights temperature temperatures temp depth depths width widths height
    heights level levels discharge flow flows mortality""".split()
)

_TEMPORAL_TOKENS = frozenset(
    "date dates time times timestamp timestamps datetime dtt year yr month day".split()
)

_METHOD_TOKENS = frozenset(
    """method methods protocol protocols procedure procedures technique
    techniques gear enumeration""".split()
)

_NUMBER_TOKENS = frozenset("number numbers no num".split())

_IDENTIFIER_CONTEXT_TOKENS = frozenset(
    """reference facility station site sample licence license permit record
    report release tag""".split()
)

_SAMPLE_SIZE_TOKENS = frozenset("size sizes".split())
_SAMPLE_CONTEXT_TOKENS = frozenset("sample samples partition partitions".split())

# metasalmon 0.1.7: an embedded ID token can describe what a qualifier is
# *about* rather than making the qualifier itself an identifier.
_IDENTIFIER_QUALIFIER_TOKENS = frozenset(
    "quality confidence accuracy grade score".split()
)

_MEASUREMENT_NAME_RE = re.compile(
    r"count|total|number|amount|quantity|measure|temp|temperature|depth|width"
    r"|height|level|discharge|flow|mortality"
)
_MEASUREMENT_UNIT_RE = re.compile(
    r"\([^)]*(%|‰|°c|deg\s*c|cms|m3/s|mm|cm|\bm\b|kg|g|mg/l|ug/l)[^)]*\)"
)


def _name_has_measurement_hint(name_lower: str, name_tokens: Sequence[str]) -> bool:
    """Mirror ``.ms_name_has_measurement_hint``."""
    return (
        any(token in _MEASUREMENT_TOKENS for token in name_tokens)
        or _MEASUREMENT_NAME_RE.search(name_lower) is not None
        or _MEASUREMENT_UNIT_RE.search(name_lower) is not None
    )


def _name_looks_identifierish(name_tokens: Sequence[str]) -> bool:
    """Mirror ``.ms_name_looks_identifierish``."""
    return any(token in _NUMBER_TOKENS for token in name_tokens) and any(
        token in _IDENTIFIER_CONTEXT_TOKENS for token in name_tokens
    )


def _name_has_sample_size_hint(name_tokens: Sequence[str]) -> bool:
    """Mirror ``.ms_name_has_sample_size_hint``."""
    return any(token in _SAMPLE_SIZE_TOKENS for token in name_tokens) and any(
        token in _SAMPLE_CONTEXT_TOKENS for token in name_tokens
    )


# Every ASCII punctuation character and nothing else. ``string.punctuation`` is
# the same 32 characters as the four ranges R's ``.ms_name_words()`` spells out
# (0x21-0x2F, 0x3A-0x40, 0x5B-0x60 and 0x7B-0x7E). A fixed ASCII set, so the
# split cannot move with the locale and a non-ASCII letter is never a boundary.
_NAME_WORD_BOUNDARY_RE = re.compile("[" + re.escape(string.punctuation) + "]+")

# The time words that keep year-shaped values deciding: the name-temporal
# tokens plus the plurals that check leaves out. The plurals are not added to
# ``_TEMPORAL_TOKENS`` itself, because that check also sees values off the year
# range, where a column counting days or years is not a date.
_YEAR_SHAPE_TIME_WORDS = _TEMPORAL_TOKENS | frozenset("years yrs months days".split())


def _name_words(name_tokens: Sequence[str]) -> list[str]:
    """Mirror ``.ms_name_words``: the name's tokens split again at punctuation.

    ``_name_tokens()`` splits only at whitespace, ``.``, ``_``, ``-`` and case
    changes, so ``Water depth(mm)`` gives the token ``depth(mm)`` and
    ``adult/count`` stays one token. This splits those tokens again at every
    other ASCII punctuation character.
    """
    return [
        word
        for token in name_tokens
        for word in _NAME_WORD_BOUNDARY_RE.split(str(token))
        if word
    ]


def _name_has_measurement_word(name_words: Sequence[str]) -> bool:
    """Mirror ``.ms_name_has_measurement_word``: whole-word evidence only.

    A measurement word, or a sample or partition size. It leaves out the two
    pattern tests in :func:`_name_has_measurement_hint`, because both match
    names that are not measurements: the substring pattern finds ``temp``
    inside ``temporal_start``, and the unit pattern accepts any parenthetical
    containing a ``g``, such as ``Cohort (Aug)``. Those are tolerable where the
    hint only chooses among non-temporal roles, and not where it overrides a
    temporal signal, which is the one job this predicate has (metasalmon
    backlog #53).
    """
    return any(word in _MEASUREMENT_TOKENS for word in name_words) or (
        _name_has_sample_size_hint(name_words)
    )


def infer_column_role(col_name: str, series: pd.Series) -> str:
    """Infer ``column_role`` from a column's name and contents.

    A node-for-node port of metasalmon's ``infer_column_role()``, including
    0.1.7's terminal-ID-qualifier fix: a name whose last ID/key token is
    followed by a qualifier token (``quality``, ``confidence``, ``accuracy``,
    ``grade``, ``score``) describes the *quality of an identification*, not an
    identifier, so ``id_quality`` is a qualifier rather than a key.

    **An enumerable string column is ``categorical``, not ``attribute``**
    (metasalmon backlog #95, ruled Q29 on 2026-09-05; hub queue B-125). Three
    branches answered ``attribute`` for a column the code-row seeder would then
    write a ``codes.csv`` row for -- the identifier-qualifier branch, the
    method-token branch and the final default -- and the specification's
    ``codes_required_for_categorical_columns`` rule rejects a code row whose
    column is anything else. All three now read
    :func:`~metasalmonpy.metadata.values_form_code_list`, which is the seeder's
    own criterion, so the dictionary row and the code rows cannot disagree.

    **Year-shaped values do not outrank a measurement name** (metasalmon
    backlog #53; hub queue B-240, the port of B-53). A column whose every value
    is a four-digit number from 1800 to 2500 is ``temporal`` unless its name's
    words, split at punctuation as well as whitespace, include a measurement
    word or a sample or partition size and no date or time word. Such a column
    is then typed exactly as it would be with values off the year range, so a
    ``spawner_count`` of 1850, 2003 and 1999 is a ``measurement``, while
    ``BY``, ``count_year`` and ``Escapement (yr)`` stay ``temporal``.
    """
    name_lower = str(col_name).lower()
    name_tokens = _name_tokens(col_name)

    identifier_positions = [
        index for index, token in enumerate(name_tokens) if token in ("id", "key")
    ]
    qualifier_positions = [
        index
        for index, token in enumerate(name_tokens)
        if token in _IDENTIFIER_QUALIFIER_TOKENS
    ]
    if (
        identifier_positions
        and qualifier_positions
        and max(qualifier_positions) > max(identifier_positions)
    ):
        # The identifier-qualifier branch reads the same code-list predicate as
        # the two below and as the seeder (metasalmon backlog #95).
        if _is_categorical(series) or values_form_code_list(series):
            return "categorical"
        return "attribute"

    if re.search(r"^id$|_id$|^id_", name_lower):
        return "identifier"
    if re.search(r"^key$|_key$|^key_", name_lower):
        return "identifier"
    if any(token in ("id", "key") for token in name_tokens) or _name_looks_identifierish(
        name_tokens
    ):
        return "identifier"

    # Check for date/time patterns in the name or the column type.
    if (
        re.search(r"date|time|dtt|timestamp", name_lower)
        or pd.api.types.is_datetime64_any_dtype(series)
        or any(token in _TEMPORAL_TOKENS for token in name_tokens)
    ):
        return "temporal"

    # Year-shaped values -- every value a four-digit number from 1800 to 2500 --
    # are the one temporal signal that reads nothing but the values, and a count
    # or escapement column whose values all fall in that range has exactly that
    # shape. Typed temporal, such a column was dropped from the whole semantic
    # pipeline (metasalmon backlog #53). So the value shape decides unless the
    # name's words include a measurement word and no date or time word; then the
    # column goes through the same checks below that it would with any other
    # values.
    #
    # Words, split at punctuation as well as whitespace (``_name_words()``), so
    # that ``Water depth(mm)`` and ``adult/count`` are measurement names, and so
    # that a time word hidden by punctuation still counts, as in
    # ``Escapement (yr)`` and ``count/year``, which the token check above does
    # not see. The time words include the plurals, so ``escapement_years``
    # stays temporal. Whole words, not ``_name_has_measurement_hint()``, whose
    # substring and unit patterns would retype ``temporal_start`` and
    # ``Cohort (Aug)``.
    #
    # The words decide only whether the year shape may decide. The checks below
    # keep the coarser tokens, so a column this lets through is typed exactly
    # as it would be with values off the year range. Those checks cannot read
    # the words without every check that outranks the measurement check
    # reading them too, and there the split breaks units and rates:
    # ``Discharge (m3/day)``, ``Escapement (fish/yr)`` and ``Rate (per day)``
    # would become temporal, and ``Fish (no./site)`` an identifier.
    if _values_look_yearish(series):
        name_words = _name_words(name_tokens)
        measurement_named = _name_has_measurement_word(name_words) and not any(
            word in _YEAR_SHAPE_TIME_WORDS for word in name_words
        )
        if not measurement_named:
            return "temporal"

    # Preserve explicit factor/categorical intent from the source data.
    if _is_categorical(series):
        return "categorical"

    # Method/protocol-like fields are metadata, not measurements, even when
    # their names contain count/measure substrings (for example counting_method).
    if any(token in _METHOD_TOKENS for token in name_tokens):
        # A method column whose values enumerate (ESTIMATE_METHOD,
        # ENUMERATION_METHODS) is a code list, and its procedures resolve
        # through ``codes.csv$term_iri``; a free-text method note stays an
        # attribute.
        return "categorical" if values_form_code_list(series) else "attribute"

    if _name_has_sample_size_hint(name_tokens) and _values_look_numericish(series):
        return "measurement"

    if _name_has_measurement_hint(name_lower, name_tokens) and _values_look_numericish(
        series
    ):
        return "measurement"

    # A string column whose non-missing values enumerate is a code list: the
    # seeder writes one ``codes.csv`` row per value, and the specification then
    # requires the column to be categorical (metasalmon backlog #95). The
    # identifier, temporal and measurement checks above deliberately run first,
    # so a key, a date, or a unit-bearing or percent-like text column keeps its
    # role even when its values happen to repeat.
    if values_form_code_list(series):
        return "categorical"

    return "attribute"


def infer_required_flag(col_name: str, series: pd.Series, column_role) -> Optional[bool]:
    """Mirror ``.ms_infer_required_flag`` (metasalmon v0.1.7).

    Only a resolved ``identifier`` is asserted required, and 0.1.7 added the
    nullability check: an identifier column that carries a missing or
    blank-after-trim value is left undecided rather than declared required,
    because declaring it required makes the package fail its own validation.
    The pre-0.1.7 name-based fallback is gone on purpose — an ID token can
    occur inside the name of a non-identifier qualifier.
    """
    if column_role is None or pd.isna(column_role) or not str(column_role).strip():
        return None
    if str(column_role) != "identifier":
        return None

    values = pd.Series(series)
    if values.isna().any():
        return None
    if any(not str(value).strip() for value in values.dropna()):
        return None
    return True


def infer_dictionary(
    df: Union[pd.DataFrame, Mapping[str, pd.DataFrame]],
    guess_types: bool = True,
    dataset_id: str = "dataset-1",
    table_id: str = "table-1",
    seed_semantics: bool = False,
    semantic_sources: Optional[Sequence[str]] = None,
    semantic_max_per_role: int = 1,
    seed_verbose: bool = True,
    seed_codes: Optional[pd.DataFrame] = None,
    seed_table_meta: Optional[pd.DataFrame] = None,
    seed_dataset_meta: Optional[pd.DataFrame] = None,
    llm_assess: bool = False,
    llm_provider: str = "openai",
    llm_model: Optional[str] = None,
    llm_api_key: Optional[str] = None,
    llm_base_url: Optional[str] = None,
    llm_reasoning_effort: Optional[str] = None,
    llm_top_n: int = 5,
    llm_context_files=None,
    llm_context_text=None,
    llm_timeout_seconds: int = 60,
    llm_request_fn=None,
) -> pd.DataFrame:
    """
    Build a starter dictionary DataFrame aligned with the SDP schema.
    """
    if llm_context_files is not None:
        from .llm_review import validate_context_files

        validate_context_files(llm_context_files)
    llm_requested = any(
        (
            llm_assess,
            llm_model is not None,
            llm_api_key is not None,
            llm_base_url is not None,
            llm_reasoning_effort is not None,
            llm_context_files is not None,
            llm_context_text is not None,
            llm_request_fn is not None,
        )
    )
    if not seed_semantics and llm_requested:
        warnings.warn(
            "LLM semantic-review options are ignored when seed_semantics=False.",
            UserWarning,
            stacklevel=2,
        )

    llm_options = {
        "llm_assess": llm_assess,
        "llm_provider": llm_provider,
        "llm_model": llm_model,
        "llm_api_key": llm_api_key,
        "llm_base_url": llm_base_url,
        "llm_reasoning_effort": llm_reasoning_effort,
        "llm_top_n": llm_top_n,
        "llm_context_files": llm_context_files,
        "llm_context_text": llm_context_text,
        "llm_timeout_seconds": llm_timeout_seconds,
        "llm_request_fn": llm_request_fn,
    }

    if isinstance(df, Mapping):
        resources = ensure_resource_mapping(df, table_id=table_id)
        parts = [
            infer_dictionary(
                resource_df,
                guess_types=guess_types,
                dataset_id=dataset_id,
                table_id=resource_table_id,
                seed_semantics=False,
                semantic_sources=semantic_sources,
                semantic_max_per_role=semantic_max_per_role,
                seed_verbose=seed_verbose,
            )
            for resource_table_id, resource_df in resources.items()
        ]
        dict_df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
        table_meta = seed_table_meta if seed_table_meta is not None else infer_table_metadata_from_resources(resources, dataset_id)
        codes = seed_codes if seed_codes is not None else infer_codes_from_resources(resources, dataset_id)
        dataset_meta = seed_dataset_meta if seed_dataset_meta is not None else infer_dataset_metadata_from_resources(resources, dataset_id)

        if seed_semantics:
            if seed_verbose:
                print("Seeding semantic suggestions during infer_dictionary().")
            from .semantics import suggest_semantics

            dict_df = suggest_semantics(
                resources,
                dict_df,
                sources=semantic_sources,
                max_per_role=semantic_max_per_role,
                codes=codes,
                table_meta=table_meta,
                dataset_meta=dataset_meta,
                **llm_options,
            )
            dict_df.attrs["inferred_table_meta"] = table_meta
            dict_df.attrs["inferred_codes"] = codes
            dict_df.attrs["inferred_dataset_meta"] = dataset_meta
            dict_df.attrs["inferred_resources"] = list(resources.keys())
        return dict_df

    data = _ensure_dataframe(df, "df")
    col_names = list(data.columns)
    n_cols = len(col_names)

    dict_df = pd.DataFrame(
        {
            "dataset_id": [dataset_id] * n_cols,
            "table_id": [table_id] * n_cols,
            "column_name": col_names,
            "column_label": col_names,
            "column_description": [pd.NA] * n_cols,
            "column_role": [pd.NA] * n_cols,
            "value_type": [pd.NA] * n_cols,
            "unit_label": [pd.NA] * n_cols,
            "unit_iri": [pd.NA] * n_cols,
            "term_iri": [pd.NA] * n_cols,
            "term_type": [pd.NA] * n_cols,
            "required": [pd.NA] * n_cols,
            "property_iri": [pd.NA] * n_cols,
            "entity_iri": [pd.NA] * n_cols,
            "constraint_iri": [pd.NA] * n_cols,
            "statistical_modifier_iri": [pd.NA] * n_cols,
        }
    )

    if guess_types:
        for idx, col_name in enumerate(col_names):
            col = data[col_name]
            dict_df.at[idx, "value_type"] = infer_value_type(col)
            role = infer_column_role(col_name, col)
            dict_df.at[idx, "column_role"] = role
            required = infer_required_flag(col_name, col, role)
            if required is not None:
                dict_df.at[idx, "required"] = required

    if seed_semantics:
        if seed_verbose:
            print("Seeding semantic suggestions during infer_dictionary().")
        from .semantics import suggest_semantics

        dict_df = suggest_semantics(
            data,
            dict_df,
            sources=semantic_sources,
            max_per_role=semantic_max_per_role,
            codes=seed_codes,
            table_meta=seed_table_meta,
            dataset_meta=seed_dataset_meta,
            **llm_options,
        )
        if seed_table_meta is not None:
            dict_df.attrs["seed_table_meta"] = seed_table_meta
        if seed_codes is not None:
            dict_df.attrs["seed_codes"] = seed_codes
        if seed_dataset_meta is not None:
            dict_df.attrs["seed_dataset_meta"] = seed_dataset_meta

    return dict_df


def _collapse_inline(values, trunc: Optional[int] = None) -> str:
    """cli's inline vector collapse: ``8``, ``8 and 9``, ``7, 8, and 9``.

    With ``trunc``, a vector longer than ``trunc`` is shortened the way cli's
    default ``vec-trunc`` of 20 shortens one: the first ``trunc - 2`` values, an
    ellipsis, and the last two, so 25 values read ``1, ..., 18, ..., 24, and
    25``. Measured against cli 3.6.6.
    """
    texts = [str(value) for value in values]
    if trunc is not None and len(texts) > trunc:
        texts = texts[: trunc - 2] + ["..."] + texts[-2:]
    if len(texts) <= 1:
        return "".join(texts)
    if len(texts) == 2:
        return f"{texts[0]} and {texts[1]}"
    return ", ".join(texts[:-1]) + f", and {texts[-1]}"


def validate_dictionary(dict_df: pd.DataFrame, require_iris: bool = False) -> pd.DataFrame:
    """
    Validate dictionary structure and value constraints.
    """
    if not isinstance(dict_df, pd.DataFrame):
        raise TypeError("dict must be a pandas DataFrame")

    missing_cols = [c for c in REQUIRED_COLUMNS if c not in dict_df.columns]
    if missing_cols:
        raise ValueError(f"Dictionary missing required columns: {missing_cols}")

    df = normalize_dictionary(dict_df)

    # Ensure optional semantic columns exist
    for col in SEMANTIC_COLUMNS:
        if col not in df.columns:
            df[col] = pd.NA

    # A blank enum cell is ABSENT, not invalid -- the same reading R has, and
    # the same reading the ``required`` check three blocks below already
    # applies. R's `.ms_read_metadata_csv()` reads an empty CSV cell as NA and
    # its validator exempts NA explicitly (`& !is.na(dict$value_type)`), so a
    # blank `value_type` or `column_role` never reaches R's enum check at all.
    # `read_sdp_csv()` keeps the empty string, so it did reach this one and was
    # rejected as an invalid token in BOTH modes. That contradicted the
    # blank-schema-required contract ported in the same change (B-124): those
    # two fields are `constraints.required` non-keys, which warn by default and
    # error under `require_iris=True`. Measured 2026-09-16 on identical package
    # directories -- R returned with the warning, this package raised
    # "Invalid value_type in rows [3]: ['']" (Codex review of metasalmonpy #29).
    # The blank is not unchecked: `_collect_blank_required_metadata_fields()`
    # reports it, and strict validation still refuses it.
    #
    # `READR_TRIM_CHARS`, not a parameterless `.str.strip()`, and the
    # difference is the whole correctness of this helper. `read_sdp_csv()`
    # trims exactly what `readr`'s `trim_ws = TRUE` trims -- ASCII space, tab,
    # CR, LF -- so a cell holding only U+00A0 or U+3000 SURVIVES the read in
    # both implementations and is a real token, not an absent one. A
    # parameterless `.str.strip()` removes Unicode whitespace too, which made
    # such a cell look absent here while
    # `_collect_blank_required_metadata_fields()` (which does use
    # `READR_TRIM_CHARS`) correctly saw it as present -- so it fell through
    # both checks and validated clean in every mode. Measured 2026-09-16 on
    # identical package directories: metasalmon 0.5.0 raised
    # `Invalid value_type in rows 4: <U+3000>.` for all four combinations of
    # {value_type, column_role} x {U+00A0, U+3000} in both modes, and this
    # package accepted all four silently (Codex review of metasalmonpy #29,
    # second round). Matching the reader's trim set is what keeps "absent"
    # meaning the same thing in the two collectors and in R.
    # *Retires when:* `read_sdp_csv()` stops being the single reader, at which
    # point "absent" has to be re-derived from whatever replaces it.
    def _present(series: pd.Series) -> pd.Series:
        return series.notna() & (series.astype(str).str.strip(READR_TRIM_CHARS) != "")

    # Validate value types
    declared_types = df["value_type"].loc[_present(df["value_type"])]
    invalid_types = declared_types.loc[~declared_types.isin(VALID_VALUE_TYPES)]
    if not invalid_types.empty:
        bad_rows = invalid_types.index.tolist()
        raise ValueError(f"Invalid value_type in rows {bad_rows}: {invalid_types.tolist()}")

    # Validate roles
    if "column_role" in df.columns:
        declared_roles = df["column_role"].loc[_present(df["column_role"])]
        invalid_roles = declared_roles.loc[~declared_roles.isin(VALID_COLUMN_ROLES)]
        if not invalid_roles.empty:
            bad_rows = invalid_roles.index.tolist()
            raise ValueError(f"Invalid column_role in rows {bad_rows}: {invalid_roles.tolist()}")

    # Required flag must be boolean
    if not pd.api.types.is_bool_dtype(df["required"]):
        parsed_required = parse_logical(df["required"])
        invalid_required = parsed_required.isna() & df["required"].notna() & (df["required"].astype(str).str.strip() != "")
        if invalid_required.any():
            raise ValueError("required must be boolean")
        df["required"] = parsed_required.astype("boolean")

    # Measurement guardrail: required in strict mode, optional with warning otherwise
    measurement_rows = (df["column_role"] == "measurement") & ~df["column_role"].isna()
    semantic_fields = CORE_SEMANTIC_FIELDS

    # REVIEW-prefixed IRI markers: draft semantic assignments written for
    # human review. Strict validation blocks on them; the default mode warns.
    # Mirrors metasalmon's validate_dictionary() — previously this lived only
    # in validate_salmon_datapackage()'s strict sweep, so validating a
    # dictionary frame directly never saw them (S10 chunk D).
    iri_fields = [
        field
        for field in (
            "term_iri",
            "property_iri",
            "entity_iri",
            "unit_iri",
            "constraint_iri",
            "statistical_modifier_iri",
        )
        if field in df.columns
    ]
    review_re = re.compile(r"^\s*REVIEW\s*:", re.IGNORECASE)
    review_summary = []
    for field in iri_fields:
        rows = [
            position + 1
            for position, value in enumerate(df[field])
            if not pd.isna(value) and review_re.match(str(value))
        ]
        if rows:
            names = df["column_name"].iloc[[row - 1 for row in rows]].tolist()
            review_summary.append(
                f"{field}: "
                + ", ".join(
                    f"{name} (rows {row})" for name, row in zip(names, rows)
                )
            )
    if review_summary:
        if require_iris:
            raise ValueError(
                "Validation cannot pass while REVIEW-prefixed IRI values "
                "remain. Resolve these fields before final validation: "
                + "; ".join(review_summary)
            )
        warnings.warn(
            "REVIEW-prefixed IRI values were found. These are draft semantic "
            "assignments written for human review: "
            + "; ".join(review_summary)
            + ". Before final validation or publication, replace or confirm "
            "the IRI and remove the REVIEW prefix.",
            UserWarning,
        )

    if measurement_rows.any():
        missing_by_field = {}
        for field in semantic_fields:
            missing_field = measurement_rows & (df[field].isna() | (df[field] == ""))
            if missing_field.any():
                missing_by_field[field] = missing_field

        if require_iris:
            # R aborts on the FIRST field with missing rows, naming that
            # field and its 1-based rows — "Measurement columns require
            # term_iri; missing in rows 8." — with cli's inline collapse for
            # several rows. Field order is the CORE_SEMANTIC_FIELDS order.
            for field in semantic_fields:
                missing_field = missing_by_field.get(field)
                if missing_field is None:
                    continue
                rows = (missing_field[missing_field].index + 1).tolist()
                raise ValueError(
                    f"Measurement columns require {field}; missing in rows "
                    f"{_collapse_inline(rows)}."
                )

        elif missing_by_field:
            lines = []
            for field, missing_mask in missing_by_field.items():
                idx = missing_mask[missing_mask].index
                rows = idx + 1
                columns = df.loc[idx, "column_name"].tolist()
                row_with_columns = [
                    f"{name} (row {row})" for name, row in zip(columns, rows.tolist())
                ]
                lines.append(f"{field}: {', '.join(row_with_columns)}")

            message = (
                "Hey, you definitely should fill those out before publishing. "
                "Missing semantic fields for measurement columns: "
                + " | ".join(lines)
                + "\nNext step: run suggest_semantics() to generate semantic candidates, "
                + "then set "
                + ", ".join(CORE_SEMANTIC_FIELDS)
                + " for your measurement fields.\n"
                + "See docs for I-ADOPT guidance: "
                + "https://salmon-data-mobilization.github.io/metasalmon/"
                + "articles/reusing-standards-salmon-data-terms.html"
            )
            warnings.warn(message, UserWarning)

    duplicates = df[df.duplicated(subset=["dataset_id", "table_id", "column_name"], keep=False)]
    if not duplicates.empty:
        names = duplicates["column_name"].dropna().astype(str).unique().tolist()
        raise ValueError(f"Duplicate column names found in dictionary: {names}")

    return df


def _coerce_series(series: pd.Series, target: str, strict: bool = True) -> pd.Series:
    try:
        if target == "integer":
            return pd.to_numeric(series, errors="raise").astype("Int64")
        if target == "number":
            return pd.to_numeric(series, errors="raise")
        if target == "boolean":
            return series.astype(bool)
        if target == "date":
            return pd.to_datetime(series, errors="raise").dt.date
        if target == "datetime":
            return pd.to_datetime(series, errors="raise")
        return series.astype("string")
    except Exception as exc:
        if strict:
            raise ValueError(f"Failed to coerce column to {target}: {exc}") from exc
        warnings.warn(f"Coercion to {target} failed; keeping as string", RuntimeWarning)
        return series.astype("string")


def _apply_dictionary_present(series: pd.Series) -> pd.Series:
    """R's ``.ms_apply_dictionary_present()``: not missing and not blank.

    A blank cell is a missing value to every reader this package uses, so a
    code list turning one into a missing value loses nothing and is not
    reported. Blank means empty after trimming ``READR_TRIM_CHARS``, the set R's
    ``trimws()`` strips, for the reason ``validate_dictionary()`` gives.
    """
    return series.notna() & (series.astype(str).str.strip(READR_TRIM_CHARS) != "")


def _report_unlisted_code_values(column: str, series: pd.Series, code_values: Sequence) -> pd.Series:
    """Warn naming each present value the code list does not name; return the listed mask.

    The codes step of metasalmon's ``apply_salmon_dictionary()`` (hub items
    B-55 and B-241): each distinct value that is present in the column and
    absent from its code list is named, under either value of ``strict``,
    before it becomes missing. The caller blanks exactly the rows this mask
    leaves out, so what is named is what is blanked.
    """
    listed = series.isin(list(code_values))
    unlisted = series[_apply_dictionary_present(series) & ~listed].drop_duplicates().tolist()
    if unlisted:
        count = len(unlisted)
        noun, verb = ("value", "it becomes") if count == 1 else ("values", "they become")
        shown = _collapse_inline(
            [repr(str(value)) if isinstance(value, str) else str(value) for value in unlisted],
            trunc=20,
        )
        warnings.warn(
            f"Column {column!r} has {count} {noun} not in its code list; {verb} missing: {shown}",
            RuntimeWarning,
            stacklevel=3,
        )
    return listed


def _apply_code_labels(series: pd.Series, code_values: Sequence, code_labels: Sequence) -> pd.Categorical:
    """R's ``factor(x, levels = code_value, labels = code_label)``, for the codes step.

    Each value becomes the label of its code, and the categories are the
    distinct labels in code-list order, which is what R's levels are (hub item
    B-274). Where pandas and R part, this follows R:

    * two codes sharing a label share one category, as ``factor()`` merges a
      repeated label into one level. ``rename_categories`` refuses that, which
      is why it is not used here;
    * a repeated code value takes the label of its first row, as ``match()``
      finds it, and the later row's label is still a category, unused;
    * a code value that is missing or blank is no code at all, as ``factor()``
      drops a missing level and R's reader reads a blank one as missing;
    * a code whose label is missing or blank becomes a missing value. R gives
      it a missing level, which prints and writes as ``NA``, and pandas holds no
      missing category. Neither reports it;
    * an ordered Categorical stays ordered, as ``factor()`` takes ``ordered``
      from ``is.ordered(x)``.

    The caller has already blanked every present value the code list does not
    name, so every present value here has a code; one that does not is an
    error, which the caller reports. A blank value is missing, as everywhere in
    this module.
    """
    if not isinstance(series, pd.Series):
        raise TypeError("the data has more than one column with this name")
    rows = pd.DataFrame({"value": list(code_values), "label": list(code_labels)}, dtype=object)
    rows = rows[_apply_dictionary_present(rows["value"])]
    codes = rows.drop_duplicates(subset="value")
    categories = rows.loc[_apply_dictionary_present(rows["label"]), "label"].drop_duplicates().tolist()
    ordered = bool(series.cat.ordered) if _is_categorical(series) else False

    values = series.where(_apply_dictionary_present(series)).astype(object)
    code_of_value = pd.Index(codes["value"].tolist(), dtype=object).get_indexer(pd.Index(values, dtype=object))
    unmatched = int((values.notna().to_numpy() & (code_of_value < 0)).sum())
    if unmatched:
        raise ValueError(f"{unmatched} of its values matched no code value")
    # Each code's label as a position among the categories, or -1 for a code
    # with no label. The -1 appended last is what a missing value's -1 picks.
    label_of_code = pd.Index(categories, dtype=object).get_indexer(codes["label"].tolist())
    lookup = pd.Series([*label_of_code, -1], dtype="int64")
    return pd.Categorical.from_codes(
        lookup.iloc[code_of_value].to_numpy(), categories=categories, ordered=ordered
    )


def apply_salmon_dictionary(
    df: pd.DataFrame,
    dict_df: pd.DataFrame,
    codes: Optional[pd.DataFrame] = None,
    strict: bool = True,
) -> pd.DataFrame:
    """
    Rename columns, coerce types, and apply codes using a validated dictionary.

    A column a code list applies to becomes a Categorical whose categories are
    the list's labels, from ``code_label``, as metasalmon's ``factor(levels =
    code_value, labels = code_label)`` gives them: two codes sharing a label
    share a category, and a code with no label becomes missing. Without a
    ``code_label`` column the code values are the labels.

    A value that is not in its column's code list has no category, so it
    becomes missing. Each such value is named in a ``RuntimeWarning``, whatever
    ``strict`` is, because ``strict`` governs type coercion. Missing and blank
    values are not reported. As in metasalmon, a code list applies to a text or
    Categorical column; a numeric, logical or date column keeps its values.
    """
    data = _ensure_dataframe(df, "df")
    dictionary = validate_dictionary(dict_df, require_iris=False)

    result = data.copy()

    table_ids = dictionary["table_id"].dropna().unique().tolist()
    if len(table_ids) > 1:
        warnings.warn(f"Dictionary contains multiple tables; applying first: {table_ids[0]}", RuntimeWarning)
    table_id = table_ids[0] if table_ids else None
    table_dict = dictionary[dictionary["table_id"] == table_id] if table_id is not None else dictionary

    # Rename columns using column_label
    rename_map = {
        row.column_label: row.column_name
        for _, row in table_dict.iterrows()
        if row.column_name in result.columns and pd.notna(row.column_label) and row.column_label != ""
    }
    if rename_map:
        # Inverse map: new_name: old_name
        inverse = {v: k for k, v in rename_map.items()}
        result = result.rename(columns=inverse)

    # Coerce types and apply codes
    codes_df = None
    if codes is not None:
        codes_df = _ensure_dataframe(codes, "codes")

    for _, row in table_dict.iterrows():
        original_name = row.column_name
        new_name = row.column_label
        target_type = row.value_type

        if original_name not in data.columns:
            continue

        series = result[new_name] if new_name in result.columns else data[original_name]

        if pd.notna(target_type):
            series = _coerce_series(series, target=str(target_type), strict=strict)
            result[new_name] = series

        code_values = None
        if codes_df is not None and "column_name" in codes_df.columns and original_name in codes_df["column_name"].values:
            col_codes = codes_df
            if table_id is not None:
                col_codes = col_codes[col_codes["table_id"] == table_id]
            col_codes = col_codes[col_codes["column_name"] == original_name]
            if not col_codes.empty and new_name in result.columns and _code_list_applies(result[new_name]):
                code_values = list(col_codes["code_value"])
                code_labels = list(col_codes.get("code_label", code_values))
                # A value the code list does not name has no category, so it
                # becomes missing. That happened silently until hub item B-241,
                # the mirror of metasalmon's B-55: it is now named whatever
                # ``strict`` is, and blanked here, before the labels, so that
                # what is named is what is blanked on either path below. The
                # labels are built only for values that have a code, and the
                # fallback would otherwise keep a value the warning has just
                # said becomes missing.
                #
                # A column name the data repeats makes ``result[new_name]`` a
                # DataFrame, which is kept out of the report rather than
                # failing inside it, and reaches the fallback. Retires with hub
                # item B-397, when a repeated column name is refused, or read
                # as R's ``[[`` reads it.
                if isinstance(result[new_name], pd.Series):
                    listed = _report_unlisted_code_values(original_name, result[new_name], code_values)
                    result[new_name] = result[new_name].where(listed)
                # The labels, as R's factor(levels = code_value, labels =
                # code_label) gives them (hub item B-274). This used to call
                # rename_categories on the Series rather than on its ``.cat``
                # accessor, so it always raised, and an ``except`` marked
                # defensive turned every coded column into text in silence. A
                # failure that is left is reported, and the column kept as text.
                try:
                    result[new_name] = _apply_code_labels(result[new_name], code_values, code_labels)
                except Exception as exc:
                    warnings.warn(
                        f"Column {original_name!r} keeps its values as text, because the labels "
                        f"in its code list could not be applied: {exc}",
                        RuntimeWarning,
                        stacklevel=2,
                    )
                    result[new_name] = result[new_name].astype("string")

        if row.get("column_role") == "categorical" and code_values is None:
            # Ensure categorical dtype when no code list applies. A column one
            # did apply to carries its labels as its categories already, and
            # rebuilding them from the code values would blank every labelled
            # value (hub item B-274).
            code_values = pd.unique(result[new_name].dropna())
            try:
                result[new_name] = pd.Categorical(result[new_name], categories=code_values)
            except Exception:  # pragma: no cover - defensive
                result[new_name] = result[new_name].astype("string")

    required_cols = table_dict.loc[table_dict["required"] == True, "column_name"].tolist()
    missing_required = [c for c in required_cols if c not in data.columns]
    if missing_required:
        warnings.warn(f"Missing required columns in data: {missing_required}", RuntimeWarning)

    return result


__all__ = [
    "apply_salmon_dictionary",
    "infer_column_role",
    "infer_dictionary",
    "infer_value_type",
    "validate_dictionary",
]
