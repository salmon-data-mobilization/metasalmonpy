from __future__ import annotations

from collections.abc import Mapping
from typing import Callable, Optional, Sequence
import warnings

try:
    import pandas as pd
except ImportError as exc:  # pragma: no cover - import guard
    raise ImportError("metasalmonpy requires pandas; install via `pip install pandas`.") from exc

import re

from .metadata import normalize_codes, normalize_dataset_meta, normalize_dictionary, normalize_table_meta
from .term_search import find_terms
from .dwc_dp import suggest_dwc_mappings

ROLE_MAP = {
    "term_iri": "variable",
    "property_iri": "property",
    "entity_iri": "entity",
    "unit_iri": "unit",
    "constraint_iri": "constraint",
    "method_iri": "method",
}


def _is_missing(value) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except Exception:
        pass
    return isinstance(value, str) and value.strip() == ""


def _first_non_empty(*values) -> str:
    for value in values:
        if isinstance(value, (list, tuple)):
            nested = _first_non_empty(*value)
            if nested:
                return nested
        elif not _is_missing(value):
            return str(value)
    return ""


def _clean_query(value: str) -> str:
    value = re.sub(r"[._]+", " ", str(value))
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _measurement_query(row, role: str, base_query: str) -> tuple[str, str]:
    column_text = _clean_query(
        _first_non_empty(
            row.get("column_label"),
            row.get("column_name"),
            base_query,
        )
    )
    normalized = f"{column_text} {base_query}".lower()
    count_like = bool(
        re.search(r"\b(count|total|number|abundance|spawner)s?\b", normalized)
    )
    if role == "unit":
        if not _is_missing(row.get("unit_label")):
            return _clean_query(row.get("unit_label")), "unit_label"
        return ("count", "count_like_fallback") if count_like else ("", "missing_unit_context")
    if role == "property" and re.search(r"\bspawner", normalized):
        return "spawner abundance", "role_shaping"
    if role == "property" and count_like:
        return "count", "role_shaping"
    return base_query, "column_context"


def _split_role_hints(value) -> list[str]:
    if _is_missing(value):
        return []
    return [part.strip() for part in re.split(r"\|", str(value)) if part.strip()]


def _role_hint_status(role: str, role_hints) -> str:
    hints = _split_role_hints(role_hints)
    if not hints:
        return "unknown"
    if role in hints:
        return "match"
    if role == "variable" and "property" in hints:
        return "mismatch_property"
    if role == "property" and "variable" in hints:
        return "mismatch_variable"
    return "unknown"


def _role_hint_bonus(status: str) -> float:
    return {"match": 0.35, "mismatch_property": -0.35, "mismatch_variable": -0.35}.get(status, 0.0)


def _role_hint_explanation(status: str, role: str):
    if status == "match":
        return f"Candidate carries a {role} role hint."
    if status == "mismatch_property":
        return "Candidate carries a property hint; kept lower for variable destination."
    if status == "mismatch_variable":
        return "Candidate carries a variable hint; kept lower for property destination."
    return pd.NA


def _scalar_text(value) -> str:
    return "" if _is_missing(value) else str(value).strip()


def _is_review_placeholder(value) -> bool:
    return bool(
        re.match(
            r"^\s*(MISSING METADATA|MISSING DESCRIPTION|REVIEW REQUIRED)\s*:",
            _scalar_text(value),
            flags=re.IGNORECASE,
        )
    )


def _semantic_tokens(*values) -> set[str]:
    text = " ".join(_scalar_text(value) for value in values)
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    tokens = re.sub(r"[^a-z0-9]+", " ", text.lower()).split()
    stop_words = {
        "the", "and", "for", "with", "from", "into", "column", "field",
        "data", "dataset", "table", "metadata", "missing", "review",
        "required", "code", "codes", "value", "values", "record", "records",
        "attribute", "attributes", "category", "categories", "variable",
        "variables", "measurement", "measurements", "type",
    }
    return {token for token in tokens if token not in stop_words and len(token) >= 3}


def _numeric_score(suggestion) -> Optional[float]:
    if "score" not in suggestion or _is_missing(suggestion.get("score")):
        return None
    try:
        return float(suggestion.get("score"))
    except (TypeError, ValueError):
        return None


def _non_measurement_suggestion_is_compatible(suggestion, dict_row) -> bool:
    role = _scalar_text(dict_row.get("column_role")).lower()
    if role not in {"attribute", "categorical"}:
        return True

    label = _scalar_text(suggestion.get("label"))
    if not label or _is_review_placeholder(label):
        return False
    if _scalar_text(suggestion.get("role_hint_status")).lower() in {
        "mismatch_property",
        "mismatch_variable",
    }:
        return False

    match_type = _scalar_text(suggestion.get("match_type")).lower()
    if match_type and "label" not in match_type:
        return False
    score = _numeric_score(suggestion)
    if score is not None and score < 0.75:
        return False

    query_tokens = _semantic_tokens(
        suggestion.get("search_query"),
        suggestion.get("target_label"),
        suggestion.get("column_label"),
        suggestion.get("column_name"),
    )
    label_tokens = _semantic_tokens(label)
    return bool(query_tokens and label_tokens and query_tokens & label_tokens)


def _measurement_query_looks_physical(*values) -> bool:
    text = " ".join(_scalar_text(value) for value in values)
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text).lower()
    return bool(
        re.search(
            r"\b(water|level|discharge|flow|temperature|temp|rain|rainfall|"
            r"snow|snowfall|precip|gust|wind|speed|depth|width|height|meter|"
            r"metre|celsius)\b",
            text,
        )
    )


def _normalize_measurement_unit(value) -> str:
    text = _scalar_text(value)
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text).lower()
    text = text.replace("°", " degree ").replace("³", "3")
    text = re.sub(r"[^a-z0-9/ ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    aliases = {
        "cubic meter per second": "cubic meter per second",
        "cubic metre per second": "cubic meter per second",
        "m3/s": "cubic meter per second",
        "cms": "cubic meter per second",
        "cumec": "cubic meter per second",
        "cumecs": "cubic meter per second",
        "degree celsius": "degree celsius",
        "degrees celsius": "degree celsius",
        "deg c": "degree celsius",
        "celsius": "degree celsius",
        "kilometer per hour": "kilometer per hour",
        "kilometre per hour": "kilometer per hour",
        "km/h": "kilometer per hour",
        "kph": "kilometer per hour",
        "square meter": "square meter",
        "square metre": "square meter",
        "square meters": "square meter",
        "square metres": "square meter",
        "sq m": "square meter",
        "m2": "square meter",
    }
    if text in aliases:
        return aliases[text]
    if re.fullmatch(r"millimet(er|re)s?", text):
        return "millimeter"
    if re.fullmatch(r"centimet(er|re)s?", text):
        return "centimeter"
    if re.fullmatch(r"met(er|re)s?", text):
        return "meter"
    return text


def _measurement_has_paired_unit_column(dict_row, dictionary) -> bool:
    column_name = _scalar_text(dict_row.get("column_name"))
    if not re.search(r"value$", column_name, flags=re.IGNORECASE):
        return False

    matches = pd.Series(True, index=dictionary.index)
    for key in ("dataset_id", "table_id"):
        key_value = _scalar_text(dict_row.get(key))
        if key_value and key in dictionary.columns:
            matches &= dictionary[key].apply(_scalar_text) == key_value
    sibling = re.sub(r"value$", "unit", column_name, flags=re.IGNORECASE)
    return bool(
        (
            matches
            & dictionary["column_name"].apply(_scalar_text).str.lower().eq(
                sibling.lower()
            )
        ).any()
    )


def _measurement_suggestion_is_compatible(
    suggestion,
    dict_row,
    dictionary,
) -> bool:
    target_field = _scalar_text(suggestion.get("target_sdp_field"))
    if (
        target_field != "unit_iri"
        and _measurement_has_paired_unit_column(dict_row, dictionary)
    ):
        return False

    evidence_text = " ".join(
        _scalar_text(value)
        for value in (
            suggestion.get("search_query"),
            suggestion.get("target_label"),
            suggestion.get("target_description"),
            dict_row.get("column_label"),
            dict_row.get("column_description"),
            dict_row.get("column_name"),
        )
    ).lower()
    if target_field == "constraint_iri" and not re.search(
        r"\b(origin|life[ -]?stage|stage|run|season|age|sex|maturity|status|"
        r"class|type|phase|terminal|ocean|freshwater|wild|hatchery|population|"
        r"stock|species group|reporting unit|benchmark)\b",
        evidence_text,
    ):
        return False
    if target_field == "method_iri" and not re.search(
        r"\b(method|protocol|procedure|gear|estimated|estimate|estimation|"
        r"enumerat|calculated|derived|modelled|modeled|assay|technique|"
        r"field method|lab method|survey method)\b",
        evidence_text,
    ):
        return False

    query_text = " ".join(
        _scalar_text(value)
        for value in (
            suggestion.get("search_query"),
            suggestion.get("target_label"),
            suggestion.get("column_label"),
            suggestion.get("column_name"),
        )
    )
    if not _measurement_query_looks_physical(query_text):
        return True

    label = _scalar_text(suggestion.get("label"))
    if not label or _is_review_placeholder(label):
        return False
    score = _numeric_score(suggestion)
    if target_field == "unit_iri":
        query_unit = _normalize_measurement_unit(suggestion.get("search_query"))
        label_unit = _normalize_measurement_unit(label)
        return bool(
            query_unit
            and label_unit
            and query_unit == label_unit
            and (score is None or score >= 0.75)
        )

    iri = _scalar_text(suggestion.get("iri")).lower()
    ontology = _scalar_text(suggestion.get("ontology")).lower()
    source = _scalar_text(suggestion.get("source")).lower()
    if (
        "rs.tdwg.org/dwc/terms/" in iri
        or ontology in {"dwc", "darwin core"}
        or source in {"dwc", "tdwg"}
    ):
        return False
    match_type = _scalar_text(suggestion.get("match_type")).lower()
    if match_type and not re.search(r"label|unit", match_type):
        return False
    if score is not None and score < 0.75:
        return False
    query_tokens = _semantic_tokens(query_text)
    label_tokens = _semantic_tokens(label)
    return bool(query_tokens and label_tokens and query_tokens & label_tokens)


def _filter_auto_apply_suggestions(
    dictionary: pd.DataFrame,
    suggestions: pd.DataFrame,
) -> pd.DataFrame:
    if suggestions.empty:
        return suggestions

    keep = []
    for _, suggestion in suggestions.iterrows():
        target_field = _scalar_text(suggestion.get("target_sdp_field"))
        if not target_field:
            keep.append(True)
            continue

        matches = (
            dictionary["column_name"].apply(_scalar_text)
            == _scalar_text(suggestion.get("column_name"))
        )
        for key in ("dataset_id", "table_id"):
            key_value = _scalar_text(suggestion.get(key))
            if key_value and key in dictionary.columns:
                matches &= dictionary[key].apply(_scalar_text) == key_value
        rows = dictionary.loc[matches]
        compatible = False
        for _, dict_row in rows.iterrows():
            role = _scalar_text(dict_row.get("column_role")).lower()
            if role in {"identifier", "temporal"}:
                continue
            if role == "measurement":
                if _measurement_suggestion_is_compatible(
                    suggestion,
                    dict_row,
                    dictionary,
                ):
                    compatible = True
                    break
            elif _non_measurement_suggestion_is_compatible(
                suggestion,
                dict_row,
            ):
                compatible = True
                break
        keep.append(compatible)
    return suggestions.loc[keep].copy()


def _table_target_query_context(row) -> tuple[str, str, str]:
    values = {
        "observation_unit": row.get("observation_unit"),
        "description": row.get("description"),
        "table_label": row.get("table_label"),
        "table_id": row.get("table_id"),
    }
    parts = {
        key: _scalar_text(value)
        for key, value in values.items()
        if not _is_review_placeholder(value)
    }
    basis = next((key for key, value in parts.items() if value), "")
    query = parts.get(basis, "")
    context = " ".join(value for value in parts.values() if value)
    return basis, query, context


def _table_text_tokens(value) -> set[str]:
    tokens = re.sub(r"[^a-z0-9]+", " ", _scalar_text(value).lower()).split()
    stop_words = {
        "the", "and", "for", "with", "from", "into", "table", "tables",
        "data", "dataset", "metadata", "missing", "review", "required",
        "describe", "what", "each", "row", "rows", "main", "records",
        "record", "values", "value", "observation", "unit", "identifier",
        "code", "field",
    }
    return {
        token
        for token in tokens
        if token not in stop_words and (len(token) >= 3 or token in {"cu", "id"})
    }


def _table_suggestion_is_compatible(suggestion, table_row) -> bool:
    query_basis = _scalar_text(suggestion.get("target_query_basis"))
    query_context = _scalar_text(suggestion.get("target_query_context"))
    if not query_basis or not query_context:
        derived_basis, _, derived_context = _table_target_query_context(table_row)
        query_basis = query_basis or derived_basis
        query_context = query_context or derived_context
    if query_basis not in {
        "observation_unit",
        "description",
        "table_label",
        "table_id",
    }:
        return False

    label = _scalar_text(suggestion.get("label"))
    if (
        not label
        or _is_review_placeholder(label)
        or re.search(r"\b(missing|metadata|review required)\b", label, re.IGNORECASE)
    ):
        return False
    match_type = _scalar_text(suggestion.get("match_type")).lower()
    if not match_type.startswith("label"):
        return False
    score = _numeric_score(suggestion)
    if score is not None and score < 0.75:
        return False
    return bool(_table_text_tokens(query_context) & _table_text_tokens(label))


def suggest_semantics(
    df,
    dict_df: pd.DataFrame,
    sources: Optional[Sequence[str]] = None,
    include_dwc: bool = False,
    max_per_role: int = 3,
    search_fn: Callable = find_terms,
    codes: Optional[pd.DataFrame] = None,
    table_meta: Optional[pd.DataFrame] = None,
    dataset_meta: Optional[pd.DataFrame] = None,
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
    Suggest semantic annotations for SDP metadata targets.

    Candidate retrieval covers dictionary columns, controlled codes, table
    observation units, and dataset keywords. Measurement columns are expanded
    into variable, property, entity, unit, constraint, and method roles.

    Parameters
    ----------
    df
        A DataFrame, a named mapping of DataFrames, or ``None`` when only
        supplied metadata targets are being reviewed.
    dict_df
        SDP column dictionary.
    sources
        Retrieval sources. ``None`` uses role-aware defaults; any explicit
        value is a strict allowlist for initial and retry retrieval.
    llm_assess
        Enable opt-in LLM assessment. Context alone never enables a provider
        request.
    llm_context_files
        Local context file paths. Parsed DataFrames or document objects are
        rejected.

    Returns
    -------
    pandas.DataFrame
        A normalized dictionary carrying ``semantic_suggestions`` in
        ``DataFrame.attrs`` and, when requested, the stable 30-column
        ``semantic_llm_assessments`` table.
    """
    from .llm_review import (
        assess_semantic_suggestions,
        make_source_policy,
        policy_sources,
        validate_context_files,
    )

    if llm_context_files is not None:
        validate_context_files(llm_context_files)
    if (llm_context_files is not None or llm_context_text is not None) and not llm_assess:
        warnings.warn(
            "LLM context is ignored unless llm_assess=True.",
            UserWarning,
            stacklevel=2,
        )
    source_policy = make_source_policy(sources)
    if isinstance(df, Mapping):
        if not df:
            raise ValueError("df cannot be an empty resource mapping.")
        if any(not isinstance(value, pd.DataFrame) for value in df.values()):
            raise TypeError("All df resources must be pandas DataFrames.")
        resource_lookup = {str(key): value for key, value in df.items()}
        default_df = next(iter(resource_lookup.values()))
    elif isinstance(df, pd.DataFrame):
        resource_lookup = None
        default_df = df
    elif df is None:
        resource_lookup = None
        default_df = None
    else:
        raise TypeError(
            "df must be a pandas DataFrame, a named mapping of DataFrames, or None."
        )

    dictionary = normalize_dictionary(pd.DataFrame(dict_df))
    codes_df = normalize_codes(codes)
    table_df = normalize_table_meta(table_meta) if table_meta is not None else pd.DataFrame()
    dataset_df = normalize_dataset_meta(dataset_meta) if dataset_meta is not None else pd.DataFrame()

    if dictionary.empty and (codes_df is None or codes_df.empty) and table_df.empty and dataset_df.empty:
        dictionary.attrs["semantic_suggestions"] = pd.DataFrame()
        if llm_assess:
            from .llm_review import normalize_assessment_rows

            dictionary.attrs["semantic_llm_assessments"] = (
                normalize_assessment_rows()
            )
        if include_dwc:
            dictionary.attrs["dwc_mappings"] = pd.DataFrame()
        return dictionary

    targets = []

    for _, row in dictionary.iterrows():
        if row.get("column_role") != "measurement":
            continue

        description = row.get("column_description")
        if (
            isinstance(description, str)
            and description.upper().startswith(("MISSING ", "REVIEW:"))
        ):
            description = pd.NA
        query = _clean_query(
            _first_non_empty(
                description,
                row.get("column_label"),
                row.get("column_name"),
            )
        )
        for col_name, role_name in ROLE_MAP.items():
            if col_name not in dictionary.columns:
                continue
            if not _is_missing(row[col_name]):
                continue
            role_query, query_basis = _measurement_query(
                row,
                role_name,
                query,
            )
            targets.append(
                {
                    "dataset_id": row.get("dataset_id"),
                    "table_id": row.get("table_id"),
                    "column_name": row.get("column_name"),
                    "code_value": pd.NA,
                    "dictionary_role": role_name,
                    "search_role": role_name,
                    "target_scope": "column",
                    "target_sdp_file": "column_dictionary.csv",
                    "target_sdp_field": col_name,
                    "target_row_key": f"{row.get('dataset_id')}/{row.get('table_id')}/{row.get('column_name')}",
                    "target_label": row.get("column_label"),
                    "target_description": row.get("column_description"),
                    "search_query": role_query,
                    "target_query_basis": query_basis,
                    "target_query_context": query,
                    "column_label": row.get("column_label"),
                    "column_description": row.get("column_description"),
                    "unit_label": row.get("unit_label"),
                    "code_label": pd.NA,
                    "code_description": pd.NA,
                }
            )

    controlled_columns = set()
    if codes_df is not None and not codes_df.empty:
        controlled_columns = set(
            zip(
                codes_df["dataset_id"].astype(str),
                codes_df["table_id"].astype(str),
                codes_df["column_name"].astype(str),
            )
        )
    for _, row in dictionary.iterrows():
        column_role = _first_non_empty(row.get("column_role")).lower()
        key = (
            str(row.get("dataset_id")),
            str(row.get("table_id")),
            str(row.get("column_name")),
        )
        eligible = column_role == "categorical" or (
            column_role == "attribute" and key in controlled_columns
        )
        if not eligible or not _is_missing(row.get("term_iri")):
            continue
        query = _clean_query(
            _first_non_empty(
                row.get("column_description"),
                row.get("column_label"),
                row.get("column_name"),
            )
        )
        if not query:
            continue
        targets.append(
            {
                "dataset_id": row.get("dataset_id"),
                "table_id": row.get("table_id"),
                "column_name": row.get("column_name"),
                "code_value": pd.NA,
                "dictionary_role": "variable",
                "search_role": "variable",
                "target_scope": "column",
                "target_sdp_file": "column_dictionary.csv",
                "target_sdp_field": "term_iri",
                "target_row_key": (
                    f"{row.get('dataset_id')}/{row.get('table_id')}/"
                    f"{row.get('column_name')}"
                ),
                "target_label": row.get("column_label"),
                "target_description": row.get("column_description"),
                "search_query": query,
                "target_query_basis": "controlled_non_measurement",
                "target_query_context": query,
                "column_label": row.get("column_label"),
                "column_description": row.get("column_description"),
                "unit_label": row.get("unit_label"),
                "code_label": pd.NA,
                "code_description": pd.NA,
            }
        )

    if codes_df is not None and not codes_df.empty:
        for _, row in codes_df.iterrows():
            if not _is_missing(row.get("term_iri")):
                continue
            dataset_id = row.get("dataset_id")
            table_id = row.get("table_id")
            column_name = row.get("column_name")
            parent = dictionary[
                (dictionary["dataset_id"] == dataset_id)
                & (dictionary["table_id"] == table_id)
                & (dictionary["column_name"] == column_name)
            ]
            parent_row = parent.iloc[0] if not parent.empty else {}
            parent_role = parent_row.get("column_role", pd.NA) if hasattr(parent_row, "get") else pd.NA
            role_set = ["constraint", "entity", "method"] if parent_role == "measurement" else ["entity"]
            query = _clean_query(
                _first_non_empty(
                    row.get("code_description"),
                    row.get("code_label"),
                    row.get("code_value"),
                    parent_row.get("column_description", pd.NA) if hasattr(parent_row, "get") else pd.NA,
                    parent_row.get("column_label", pd.NA) if hasattr(parent_row, "get") else pd.NA,
                    column_name,
                )
            )
            if not query:
                continue
            for role_name in role_set:
                targets.append(
                    {
                        "dataset_id": dataset_id,
                        "table_id": table_id,
                        "column_name": column_name,
                        "code_value": row.get("code_value"),
                        "dictionary_role": role_name,
                        "target_scope": "code",
                        "target_sdp_file": "codes.csv",
                        "target_sdp_field": "term_iri",
                        "target_row_key": f"{dataset_id}/{table_id}/{column_name}/{row.get('code_value')}",
                        "target_label": _first_non_empty(row.get("code_label"), row.get("code_value")),
                        "target_description": row.get("code_description"),
                        "search_query": query,
                        "column_label": parent_row.get("column_label", column_name) if hasattr(parent_row, "get") else column_name,
                        "column_description": parent_row.get("column_description", pd.NA) if hasattr(parent_row, "get") else pd.NA,
                        "code_label": row.get("code_label"),
                        "code_description": row.get("code_description"),
                    }
                )

    if not table_df.empty:
        for _, row in table_df.iterrows():
            if not _is_missing(row.get("observation_unit_iri")):
                continue
            query_basis, raw_query, query_context = _table_target_query_context(row)
            query = _clean_query(raw_query)
            if not query:
                continue
            targets.append(
                {
                    "dataset_id": row.get("dataset_id"),
                    "table_id": row.get("table_id"),
                    "column_name": pd.NA,
                    "code_value": pd.NA,
                    "dictionary_role": "entity",
                    "target_scope": "table",
                    "target_sdp_file": "tables.csv",
                    "target_sdp_field": "observation_unit_iri",
                    "target_row_key": f"{row.get('dataset_id')}/{row.get('table_id')}",
                    "target_label": row.get("table_label"),
                    "target_description": row.get("description"),
                    "search_query": query,
                    "target_query_basis": query_basis,
                    "target_query_context": query_context,
                    "column_label": pd.NA,
                    "column_description": pd.NA,
                    "code_label": pd.NA,
                    "code_description": pd.NA,
                }
            )

    if not dataset_df.empty:
        for _, row in dataset_df.iterrows():
            if not _is_missing(row.get("keywords")):
                continue
            query = _clean_query(_first_non_empty(row.get("description"), row.get("title"), row.get("dataset_id")))
            if not query:
                continue
            targets.append(
                {
                    "dataset_id": row.get("dataset_id"),
                    "table_id": pd.NA,
                    "column_name": pd.NA,
                    "code_value": pd.NA,
                    "dictionary_role": "entity",
                    "target_scope": "dataset",
                    "target_sdp_file": "dataset.csv",
                    "target_sdp_field": "keywords",
                    "target_row_key": row.get("dataset_id"),
                    "target_label": row.get("title"),
                    "target_description": row.get("description"),
                    "search_query": query,
                    "column_label": pd.NA,
                    "column_description": pd.NA,
                    "code_label": pd.NA,
                    "code_description": pd.NA,
                }
            )

    targets_df = pd.DataFrame(targets)
    suggestion_rows = []
    leading = [
        "column_name",
        "dictionary_role",
        "table_id",
        "dataset_id",
        "target_row_key",
        "target_label",
        "target_description",
        "target_scope",
        "target_sdp_file",
        "target_sdp_field",
        "search_query",
        "column_label",
        "column_description",
        "code_value",
        "code_label",
        "code_description",
    ]

    for target in targets:
        if not str(target.get("search_query") or "").strip():
            continue
        target_sources = policy_sources(
            source_policy,
            str(target["dictionary_role"]),
        )
        res = search_fn(
            target["search_query"],
            role=target["dictionary_role"],
            sources=target_sources,
        )
        if res is None or res.empty:
            continue
        res = res.copy()
        if "role_hints" not in res.columns:
            res["role_hints"] = pd.NA
        res = res.drop_duplicates(subset=[col for col in ["source", "iri"] if col in res.columns], keep="first")
        res["role_hint_status"] = res["role_hints"].apply(lambda value: _role_hint_status(str(target["dictionary_role"]), value))
        res["role_hint_bonus"] = res["role_hint_status"].apply(_role_hint_bonus)
        res["role_hint_explanation"] = res["role_hint_status"].apply(lambda status: _role_hint_explanation(status, str(target["dictionary_role"])))
        if "score" in res.columns:
            res["score"] = pd.to_numeric(res["score"], errors="coerce").fillna(0) + res["role_hint_bonus"]
            res = res.sort_values(["score", "source", "ontology", "label", "iri"], ascending=[False, True, True, True, True])
        else:
            res = res.sort_values(["role_hint_bonus", "source", "ontology", "label", "iri"], ascending=[False, True, True, True, True])
        res = res.head(max_per_role).copy()
        res["retrieval_query"] = target["search_query"]
        res["retrieval_pass"] = 1
        for key, value in target.items():
            res[key] = value
        suggestion_rows.append(res)

    if suggestion_rows:
        suggestions_df = pd.concat(suggestion_rows, ignore_index=True)
        optional = [
            col
            for col in [
                "score",
                "alignment_only",
                "agreement_sources",
                "zooma_confidence",
                "zooma_annotator",
                "role_hints",
                "role_hint_status",
                "role_hint_bonus",
                "role_hint_explanation",
            ]
            if col in suggestions_df.columns
        ]
        core = ["label", "iri", "source", "ontology", "role", "match_type", "definition"]
        ordered = [col for col in leading + core + optional if col in suggestions_df.columns]
        suggestions_df = suggestions_df[ordered + [col for col in suggestions_df.columns if col not in ordered]]
    else:
        suggestions_df = pd.DataFrame(
            columns=leading + ["label", "iri", "source", "ontology", "role", "match_type", "definition"]
        )

    if not suggestions_df.empty:
        suggestions_df["_candidate_label_norm"] = suggestions_df["label"].fillna("").astype(str).str.strip().str.lower()
        group_cols = [
            "dataset_id",
            "table_id",
            "column_name",
            "code_value",
            "target_scope",
            "target_sdp_file",
            "_candidate_label_norm",
        ]
        suggestions_df["_collision_key"] = suggestions_df[group_cols].apply(
            lambda row: "\r".join("<NA>" if _is_missing(value) else str(value) for value in row),
            axis=1,
        )
        collision_roles = suggestions_df.groupby("_collision_key")["dictionary_role"].agg(
            lambda values: "|".join(sorted(set(values.dropna().astype(str))))
        )
        suggestions_df["collision_roles"] = suggestions_df["_collision_key"].map(collision_roles)
        suggestions_df["role_collision"] = suggestions_df["collision_roles"].apply(
            lambda value: {"variable", "property"}.issubset(set(str(value).split("|")))
        )
        suggestions_df["role_collision_note"] = pd.NA
        variable_collision = suggestions_df["role_collision"] & (suggestions_df["dictionary_role"] == "variable")
        property_collision = suggestions_df["role_collision"] & (suggestions_df["dictionary_role"] == "property")
        suggestions_df.loc[variable_collision, "role_collision_note"] = (
            "Label appears for variable and property candidates; this row targets variable semantics for "
            + suggestions_df.loc[variable_collision, "target_sdp_field"].astype(str)
            + "."
        )
        suggestions_df.loc[property_collision, "role_collision_note"] = (
            "Label appears for variable and property candidates; this row targets property semantics for "
            + suggestions_df.loc[property_collision, "target_sdp_field"].astype(str)
            + "."
        )
        suggestions_df = suggestions_df.drop(columns=["_candidate_label_norm", "_collision_key"])

    if llm_assess:
        if targets_df.empty:
            from .llm_review import normalize_assessment_rows

            assessments = normalize_assessment_rows()
        else:
            suggestions_df, assessments = assess_semantic_suggestions(
                targets_df,
                suggestions_df,
                dictionary,
                source_policy=source_policy,
                search_fn=search_fn,
                max_per_role=max(max_per_role, llm_top_n),
                provider=llm_provider,
                model=llm_model,
                api_key=llm_api_key,
                base_url=llm_base_url,
                reasoning_effort=llm_reasoning_effort,
                context_files=llm_context_files,
                context_text=llm_context_text,
                timeout_seconds=llm_timeout_seconds,
                request_fn=llm_request_fn,
            )
        dictionary.attrs["semantic_llm_assessments"] = assessments

    dictionary.attrs["semantic_suggestions"] = suggestions_df
    if include_dwc:
        dictionary.attrs["dwc_mappings"] = suggest_dwc_mappings(
            dictionary
        ).attrs.get("dwc_mappings", pd.DataFrame())
    return dictionary


def apply_semantic_suggestions(
    dict_df: pd.DataFrame,
    suggestions: Optional[pd.DataFrame] = None,
    strategy: str = "top",
    columns: Optional[Sequence[str]] = None,
    roles: Optional[Sequence[str]] = None,
    min_score: Optional[float] = None,
    min_llm_confidence: Optional[float] = None,
    overwrite: bool = False,
    verbose: bool = True,
) -> pd.DataFrame:
    """
    Apply selected column-level semantic suggestions to a dictionary.

    Parameters
    ----------
    dict_df
        Dictionary to update.
    suggestions
        Candidate table. When omitted, uses
        ``dict_df.attrs["semantic_suggestions"]``.
    strategy
        ``"top"`` chooses the first filtered candidate; ``"llm"`` requires a
        reviewed candidate with decision ``accept``.
    roles
        Optional semantic-role filter. Package auto-prefill should remain
        limited to variable, property, entity, and unit roles.
    overwrite
        Replace existing IRIs when ``True``. Existing values are preserved by
        default.

    Returns
    -------
    pandas.DataFrame
        Updated normalized dictionary.
    """
    if strategy not in {"top", "llm"}:
        raise ValueError("Unsupported strategy: use 'top' or 'llm'.")
    out = normalize_dictionary(dict_df)
    if suggestions is None:
        suggestions = dict_df.attrs.get("semantic_suggestions")
    if suggestions is None:
        raise ValueError("No semantic suggestions supplied.")
    suggestions_df = pd.DataFrame(suggestions).copy()
    if suggestions_df.empty:
        if verbose:
            print("No semantic suggestions to apply.")
        return out
    required = {"column_name", "dictionary_role", "iri"}
    missing = required - set(suggestions_df.columns)
    if missing:
        raise ValueError(f"Suggestions are missing required columns: {sorted(missing)}")
    if min_score is not None and "score" not in suggestions_df.columns:
        raise ValueError("min_score requires scored suggestions.")
    if strategy == "llm":
        required_llm = {"llm_selected", "llm_decision", "llm_confidence"}
        missing_llm = required_llm - set(suggestions_df.columns)
        if missing_llm:
            raise ValueError(
                "strategy='llm' requires reviewed suggestions with columns: "
                f"{sorted(missing_llm)}"
            )

    role_to_field = {
        "variable": "term_iri",
        "property": "property_iri",
        "entity": "entity_iri",
        "unit": "unit_iri",
        "constraint": "constraint_iri",
        "method": "method_iri",
    }
    if roles is not None:
        invalid = set(roles) - set(role_to_field)
        if invalid:
            raise ValueError(f"Unsupported roles: {sorted(invalid)}")

    suggestions_df["_row_id"] = range(len(suggestions_df))
    suggestions_df = suggestions_df[~suggestions_df["iri"].isna() & (suggestions_df["iri"] != "")]
    if "target_scope" in suggestions_df.columns:
        suggestions_df = suggestions_df[suggestions_df["target_scope"].isna() | (suggestions_df["target_scope"] == "column")]
    if "target_sdp_file" in suggestions_df.columns:
        suggestions_df = suggestions_df[
            suggestions_df["target_sdp_file"].isna() | (suggestions_df["target_sdp_file"] == "column_dictionary.csv")
        ]
    if columns is not None:
        suggestions_df = suggestions_df[suggestions_df["column_name"].isin(columns)]
    if roles is not None:
        suggestions_df = suggestions_df[suggestions_df["dictionary_role"].isin(roles)]
    if min_score is not None:
        suggestions_df = suggestions_df[pd.to_numeric(suggestions_df["score"], errors="coerce") >= min_score]
    if strategy == "llm":
        suggestions_df = suggestions_df[
            suggestions_df["llm_selected"].fillna(False).astype(bool)
            & (suggestions_df["llm_decision"] == "accept")
        ]
        if min_llm_confidence is not None:
            suggestions_df = suggestions_df[
                pd.to_numeric(
                    suggestions_df["llm_confidence"],
                    errors="coerce",
                )
                >= min_llm_confidence
            ]
    suggestions_df = _filter_auto_apply_suggestions(out, suggestions_df)
    suggestions_df = suggestions_df[suggestions_df["dictionary_role"].isin(role_to_field)]
    if suggestions_df.empty:
        if verbose:
            print("No semantic suggestions met the requested filters.")
        return out

    match_keys = [key for key in ["dataset_id", "table_id", "column_name", "dictionary_role"] if key in suggestions_df.columns]
    selected = suggestions_df.sort_values("_row_id").drop_duplicates(
        subset=match_keys,
        keep="first",
    )
    applied = 0
    skipped_existing = 0
    unmatched = 0

    for _, suggestion in selected.iterrows():
        field = role_to_field[str(suggestion["dictionary_role"])]
        matches = out["column_name"] == suggestion["column_name"]
        for key in ["dataset_id", "table_id"]:
            if key in out.columns and key in suggestion and not _is_missing(suggestion[key]):
                matches = matches & (out[key] == suggestion[key])
        row_ids = out.index[matches].tolist()
        if not row_ids:
            unmatched += 1
            continue
        if overwrite:
            out.loc[row_ids, field] = suggestion["iri"]
            applied += len(row_ids)
            continue
        missing_now = out.loc[row_ids, field].apply(_is_missing)
        fill_rows = missing_now[missing_now].index
        if len(fill_rows) > 0:
            out.loc[fill_rows, field] = suggestion["iri"]
            applied += len(fill_rows)
        skipped_existing += int((~missing_now).sum())

    if verbose:
        print(f"Applied {applied} semantic suggestion fields using the {strategy} strategy.")
        if skipped_existing:
            print(f"{skipped_existing} fields were left alone because the dictionary already had an IRI.")
        if unmatched:
            print(f"{unmatched} suggestions did not match any dictionary row.")
    return out


__all__ = ["apply_semantic_suggestions", "suggest_semantics"]
