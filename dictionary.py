from __future__ import annotations

import datetime as _dt
import re
import warnings
from collections.abc import Mapping
from typing import Optional, Sequence, Union

try:
    import pandas as pd
except ImportError as exc:  # pragma: no cover - import guard
    raise ImportError("metasalmonpy requires pandas; install via `pip install pandas`.") from exc

from .metadata import (
    ensure_resource_mapping,
    infer_codes_from_resources,
    infer_dataset_metadata_from_resources,
    infer_table_metadata_from_resources,
    normalize_dictionary,
    parse_logical,
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
    "method_iri",
]

# Core ontology fields used in strict semantic checks for measurements.
# - term/property/entity/unit are required for explicit I-ADOPT-style semantics.
# - constraint/method are optional qualifiers (e.g., age/phase/method tags).
CORE_SEMANTIC_FIELDS = ["term_iri", "property_iri", "entity_iri", "unit_iri"]
OPTIONAL_SEMANTIC_FIELDS = ["constraint_iri", "method_iri"]
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

    # Datetime: treat midnight-only timestamps as dates
    if pd.api.types.is_datetime64_any_dtype(dtype):
        non_null = s.dropna()
        if len(non_null) > 0:
            times = non_null.dt.time
            if times.nunique() == 1 and times.iloc[0] == _dt.time(0, 0):
                return "date"
        return "datetime"

    # Date detection for object columns of date objects
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


def infer_column_role(col_name: str, series: pd.Series) -> str:
    """Infer ``column_role`` from a column's name and contents.

    A node-for-node port of metasalmon v0.1.7's ``infer_column_role()``,
    including that release's terminal-ID-qualifier fix: a name whose last
    ID/key token is followed by a qualifier token (``quality``, ``confidence``,
    ``accuracy``, ``grade``, ``score``) describes the *quality of an
    identification*, not an identifier, so ``id_quality`` is an attribute.
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
        return "categorical" if _is_categorical(series) else "attribute"

    if re.search(r"^id$|_id$|^id_", name_lower):
        return "identifier"
    if re.search(r"^key$|_key$|^key_", name_lower):
        return "identifier"
    if any(token in ("id", "key") for token in name_tokens) or _name_looks_identifierish(
        name_tokens
    ):
        return "identifier"

    if (
        re.search(r"date|time|dtt|timestamp", name_lower)
        or pd.api.types.is_datetime64_any_dtype(series)
        or any(token in _TEMPORAL_TOKENS for token in name_tokens)
        or _values_look_yearish(series)
    ):
        return "temporal"

    # Preserve explicit factor/categorical intent from the source data.
    if _is_categorical(series):
        return "categorical"

    # Method/protocol-like fields are metadata, not measurements, even when
    # their names contain count/measure substrings (for example counting_method).
    if any(token in _METHOD_TOKENS for token in name_tokens):
        return "attribute"

    if _name_has_sample_size_hint(name_tokens) and _values_look_numericish(series):
        return "measurement"

    if _name_has_measurement_hint(name_lower, name_tokens) and _values_look_numericish(
        series
    ):
        return "measurement"

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
            "method_iri": [pd.NA] * n_cols,
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

    # Validate value types
    invalid_types = df["value_type"].dropna().loc[~df["value_type"].isin(VALID_VALUE_TYPES)]
    if not invalid_types.empty:
        bad_rows = invalid_types.index.tolist()
        raise ValueError(f"Invalid value_type in rows {bad_rows}: {invalid_types.tolist()}")

    # Validate roles
    if "column_role" in df.columns:
        invalid_roles = df["column_role"].dropna().loc[~df["column_role"].isin(VALID_COLUMN_ROLES)]
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

    if measurement_rows.any():
        missing_by_field = {}
        for field in semantic_fields:
            missing_field = measurement_rows & (df[field].isna() | (df[field] == ""))
            if missing_field.any():
                missing_by_field[field] = missing_field

        if require_iris:
            if missing_by_field:
                missing_parts = []
                for field, missing_field in missing_by_field.items():
                    idx = missing_field[missing_field].index
                    rows = (idx + 1).tolist()
                    columns = df.loc[idx, "column_name"].tolist()
                    if columns:
                        fields = [f"{name} (row {row})" for name, row in zip(columns, rows)]
                        missing_parts.append(f"{field}: {', '.join(fields)}")
                raise ValueError(
                    "Measurement columns require semantic fields; missing values in: "
                    + "; ".join(missing_parts)
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


def apply_salmon_dictionary(
    df: pd.DataFrame,
    dict_df: pd.DataFrame,
    codes: Optional[pd.DataFrame] = None,
    strict: bool = True,
) -> pd.DataFrame:
    """
    Rename columns, coerce types, and apply codes using a validated dictionary.
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
            if not col_codes.empty and new_name in result.columns:
                code_values = list(col_codes["code_value"])
                code_labels = list(col_codes.get("code_label", code_values))
                try:
                    result[new_name] = pd.Categorical(result[new_name], categories=code_values)
                    result[new_name] = result[new_name].rename_categories(dict(zip(code_values, code_labels)))
                except Exception:  # pragma: no cover - defensive
                    result[new_name] = result[new_name].astype("string")

        if row.get("column_role") == "categorical":
            # Ensure categorical dtype even if codes are not provided
            if code_values is None:
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
