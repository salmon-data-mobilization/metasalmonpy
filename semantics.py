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
    # sdp-0.3.0 removed the dictionary method slot; the statistical modifier
    # is part of variable identity (I-ADOPT StatisticalModifier) and is the
    # sixth dictionary slot. The code-level `method` role survives for
    # codes.csv term_iri targets only (S10 chunk B).
    "statistical_modifier_iri": "statistical_modifier",
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


def _table_context_has(row, dictionary, pattern: str) -> bool:
    """Mirror R's ``context_has(table_context(row, dict), pattern)``.

    True when any OTHER column of the same dataset/table names the pattern in
    its name, label, or description.
    """
    if dictionary is None or len(dictionary) == 0:
        return False
    context = dictionary
    for key in ("dataset_id", "table_id"):
        if key not in context.columns:
            return False
        context = context[
            context[key].apply(_scalar_text) == _scalar_text(row.get(key))
        ]
    if "column_name" in context.columns:
        context = context[
            context["column_name"].apply(_scalar_text)
            != _scalar_text(row.get("column_name"))
        ]
    for column in ("column_name", "column_label", "column_description"):
        if column not in context.columns:
            continue
        for value in context[column]:
            text = _scalar_text(value)
            if text and re.search(pattern, text, re.IGNORECASE):
                return True
    return False


def _measurement_query(
    row, role: str, base_query: str, dictionary=None
) -> tuple[str, str]:
    column_text = _clean_query(
        _first_non_empty(
            row.get("column_label"),
            row.get("column_name"),
            base_query,
        )
    )
    normalized = f"{column_text} {base_query}".lower()
    count_like = _is_count_like_measurement(row, normalized)
    if role == "unit":
        if not _is_missing(row.get("unit_label")):
            return _clean_query(row.get("unit_label")), "unit_label"
        return ("count", "count_like_fallback") if count_like else ("", "missing_unit_context")
    if role == "constraint":
        # Mirror of R's constraint branch (present since era 0.1.7 but never
        # ported — an undocumented divergence the chunk-B differential caught:
        # the raw description leaked into the constraint search).
        base_lower = base_query.lower()
        if re.search(r"\bnatural\b", base_lower):
            return "natural origin", "role_shaping"
        if re.search(r"\bhatchery\b", base_lower):
            return "hatchery origin", "role_shaping"
        return base_query, "column_context"
    if role == "entity":
        # Mirror of R's entity branch (same story as the constraint branch).
        if _table_context_has(row, dictionary, "stock"):
            return "stock", "role_shaping"
        if _table_context_has(row, dictionary, "population"):
            return "population", "role_shaping"
        if re.search(r"spawner", base_query.lower()):
            return "population", "role_shaping"
        return base_query, "column_context"
    if role == "statistical_modifier":
        # Part of variable identity, and deliberately conservative: a
        # statistical-modifier target is emitted only when the column text
        # names an aggregation, so plain measurements do not gain a slot.
        # Checked across name, label, AND description -- the aggregation is
        # often only in the column name (`mean_temperature`), and underscores
        # do not form \b word boundaries, so they are split first (mirrors
        # metasalmon's measurement_role_query; a placeholder description
        # contributes nothing, exactly as R's desc_query does).
        description = row.get("column_description")
        modifier_text = re.sub(
            r"[_.]",
            " ",
            " ".join(
                (
                    "" if _is_review_placeholder(description)
                    else _scalar_text(description),
                    _scalar_text(row.get("column_label")),
                    _scalar_text(row.get("column_name")),
                )
            ).lower(),
        )
        if re.search(r"\b(total|cumulative|sum)\b", modifier_text):
            return "total", "aggregation_evidence"
        if re.search(r"\b(mean|average)\b", modifier_text):
            return "mean", "aggregation_evidence"
        if re.search(r"\bmax(imum)?\b", modifier_text):
            return "maximum", "aggregation_evidence"
        if re.search(r"\bmin(imum)?\b", modifier_text):
            return "minimum", "aggregation_evidence"
        if re.search(r"\bpeak\b", modifier_text):
            return "peak", "aggregation_evidence"
        return "", "no_aggregation_evidence"
    if role in ("variable", "property") and count_like:
        # R evaluates the count-like *test* over name + label + base query, but
        # shapes the query from `base_lower` -- the base query alone. Feeding
        # the wider text to the shaper would let a column name ("smolt count")
        # override a description that never mentions the life stage.
        return _count_like_role_query(role, base_query.lower()), "role_shaping"
    return base_query, "column_context"


# metasalmon v0.1.7 widened the organism vocabulary: smolt, fry and juvenile
# are biology-bearing tokens, so "total smolts" is a count-like measurement and
# keeps its life-stage word instead of collapsing to a bare "count".
_ORGANISM_RE = re.compile(
    r"\b(spawner|spawners|fish|salmon|organism|organisms|recruit|recruits"
    r"|smolt|smolts|fry|juvenile|juveniles|population|populations"
    r"|adult|adults)\b"
)
_EXPLICIT_COUNT_RE = re.compile(r"\b(count|counts|number|numbers|num|abundance)\b")
_INTEGERISH_VALUE_TYPES = ("integer", "int", "number", "numeric", "double")


def _is_count_like_measurement(row, text: str) -> bool:
    """Mirror ``is_count_like_measurement`` (metasalmon v0.1.7)."""
    if not text.strip():
        return False
    value_type = _scalar_text(row.get("value_type")).lower()
    has_explicit_count = _EXPLICIT_COUNT_RE.search(text) is not None
    has_total = re.search(r"\btotal\b", text) is not None
    has_organism = _ORGANISM_RE.search(text) is not None
    looks_integer = value_type in _INTEGERISH_VALUE_TYPES
    return (
        has_explicit_count
        or (has_total and has_organism)
        or (re.search(r"\babundance\b", text) is not None
            and (has_organism or looks_integer))
        or (looks_integer and has_organism)
    )


def _count_like_role_query(role: str, text: str) -> str:
    """Mirror the count-like branch of ``measurement_role_query`` (v0.1.7).

    0.1.7 added the effective-female-spawner special case and the recruit /
    smolt / fry whole-variable queries, so a life stage survives into the
    search instead of every count-like column asking for "count".
    """
    if re.search(r"spawner", text):
        if role == "variable":
            if (
                re.search(r"\beffective\b", text) and re.search(r"\bfemale\b", text)
            ) or re.search(r"\beggs?\s+not\s+spawned\b", text):
                return "effective female spawner abundance"
            if re.search(r"adult", text):
                return "adult spawner count"
        return "spawner abundance"

    if role == "variable":
        if re.search(r"\brecruits?\b", text):
            return "recruit abundance"
        if re.search(r"\bsmolts?\b", text):
            return "smolt abundance"
        if re.search(r"\bfry\b", text):
            return "fry abundance"

    if re.search(r"\babundance\b", text):
        return "abundance"
    return "count"


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
    if target_field == "statistical_modifier_iri" and not re.search(
        # Mirror .ms_measurement_supports_statistical_modifier_slot: applying
        # a modifier changes what the variable means, so the column itself
        # must name an aggregation. (sdp-0.3.0 removed the dictionary
        # method_iri slot this branch used to gate.)
        r"\b(mean|average|max(imum)?|min(imum)?|total|cumulative|sum|peak|"
        r"median|aggregate|aggregated|index)\b",
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
    into variable, property, entity, unit, constraint, and
    statistical_modifier roles; the code-level method role survives for
    codes.csv term_iri targets.

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
        ``semantic_llm_assessments`` table. The dictionary also carries a
        ``semantic_targets`` attribute with the discovered search targets
        (one row per unfilled semantic field);
        :func:`~metasalmonpy.term_requests.detect_semantic_term_gaps` reads it
        to report targets whose retrieval returned zero candidates, which by
        construction have no suggestion rows at all.
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
        dictionary.attrs["semantic_targets"] = pd.DataFrame()
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
                dictionary=dictionary,
            )
            if not role_query.strip():
                # Mirror R's discovery: a role whose query shaper found no
                # evidence (an empty unit context, a measurement that names no
                # aggregation) emits NO target. This matters twice: the LLM
                # bundle never reviews an evidence-free slot, and the
                # `semantic_targets` attribute -- which gap detection reads to
                # find zero-candidate targets -- must not carry targets that
                # were never searched.
                continue
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
        # metasalmon v0.1.7 made an explicit source list a strict allowlist on
        # the way *out* as well as the way in: results are filtered to the
        # allowed sources, so an injected search_fn cannot widen a deliberately
        # bounded source set.
        if source_policy["explicit"]:
            if "source" not in res.columns:
                continue
            allowed = {str(name).strip().lower() for name in target_sources}
            candidate_sources = res["source"].map(
                lambda value: "" if _is_missing(value) else str(value).strip().lower()
            )
            res = res[candidate_sources.isin(allowed) & (candidate_sources != "")]
            if res.empty:
                continue
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
    # The discovered targets ride along so `detect_semantic_term_gaps()` can
    # see the targets retrieval found NOTHING for. Without them, a concept
    # absent from every vocabulary -- the strongest possible term-gap evidence
    # -- contributed zero suggestion rows and therefore zero gaps (hub backlog
    # #97; the mirror had the same defect, measured 2026-08-21).
    dictionary.attrs["semantic_targets"] = targets_df
    if include_dwc:
        dictionary.attrs["dwc_mappings"] = suggest_dwc_mappings(
            dictionary
        ).attrs.get("dwc_mappings", pd.DataFrame())
    return dictionary


_OWL_OBJECT_PROPERTY_KINDS = ("objectproperty", "owl_object_property")
_OWL_CLASS_KINDS = ("class", "owlclass", "owl_class")
_SKOS_CONCEPT_KINDS = ("concept", "skosconcept", "skos_concept")


def _infer_term_type(suggestion) -> Optional[str]:
    """Mirror ``infer_term_type`` inside ``apply_semantic_suggestions`` (v0.1.7).

    0.1.7 stopped stamping every accepted whole-variable term as
    ``skos_concept`` and started preserving the candidate's native ontology
    type. An OWL class picked as the variable term keeps ``owl_class``, which
    is what the EML exporter checks against the term's own type evidence --
    the flat ``skos_concept`` made an OWL variable term unexportable.
    """
    declared = _scalar_text(suggestion.get("term_type"))
    if declared:
        return declared
    if str(suggestion.get("dictionary_role")) != "variable":
        return None

    type_iris = _scalar_text(suggestion.get("type_iris")).lower()
    resource_kind = _scalar_text(suggestion.get("resource_kind")).lower()
    if (
        re.search(r"owl[#/]objectproperty", type_iris)
        or resource_kind in _OWL_OBJECT_PROPERTY_KINDS
    ):
        return "owl_object_property"
    if re.search(r"owl[#/]class", type_iris) or resource_kind in _OWL_CLASS_KINDS:
        return "owl_class"
    if re.search(r"skos[/#]concept", type_iris) or resource_kind in _SKOS_CONCEPT_KINDS:
        return "skos_concept"
    return "skos_concept"


def _fill_missing_term_type(out: pd.DataFrame, row_ids, term_type: str) -> None:
    targets = [
        row_id for row_id in row_ids if _is_missing(out.at[row_id, "term_type"])
        or str(out.at[row_id, "term_type"]) == ""
    ]
    if targets:
        out.loc[targets, "term_type"] = term_type


def _fill_missing_unit_label(out: pd.DataFrame, row_ids, suggestion) -> None:
    label = suggestion.get("label")
    if _is_missing(label):
        return
    targets = [
        row_id for row_id in row_ids if _is_missing(out.at[row_id, "unit_label"])
        or str(out.at[row_id, "unit_label"]) == ""
    ]
    if targets:
        out.loc[targets, "unit_label"] = label


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
        ``"top"`` keeps the original lexical ranking and chooses the first
        filtered candidate. ``"reviewed"`` applies only rows whose ``decision``
        is ``accepted`` (or the equivalent ``accept``). ``"llm"`` requires a
        reviewed candidate with decision ``accept``.

        When reviewed or LLM-reviewed selections contain multiple constraints
        for one measurement, their IRIs are deduplicated in first-occurrence
        order and written to ``constraint_iri`` as the SDP-compatible
        semicolon-separated value. Other roles continue to select one value
        per column-role pair, and ``"top"`` stays single-winner for every role.
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
    if strategy not in {"top", "reviewed", "llm"}:
        raise ValueError(
            "Unsupported strategy: use 'top', 'reviewed' or 'llm'."
        )
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
    if strategy == "reviewed" and "decision" not in suggestions_df.columns:
        raise ValueError(
            "strategy='reviewed' requires explicit review decisions. Supply a "
            "decision column whose accepted rows use 'accepted' or 'accept'."
        )

    role_to_field = {
        "variable": "term_iri",
        "property": "property_iri",
        "entity": "entity_iri",
        "unit": "unit_iri",
        "constraint": "constraint_iri",
        "statistical_modifier": "statistical_modifier_iri",
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
    if strategy == "reviewed":
        decisions = (
            suggestions_df["decision"]
            .map(lambda value: "" if _is_missing(value) else str(value).strip().lower())
        )
        suggestions_df = suggestions_df[decisions.isin(["accepted", "accept"])]
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
    selected = suggestions_df.sort_values("_row_id")

    # The lexical "top" strategy still chooses one winner per role. Explicitly
    # reviewed and LLM-reviewed bundles can instead accept more than one
    # constraint for the same measurement: an effective-female-spawner count is
    # qualified by BOTH a spawner-stage and a sex constraint, and dropping
    # either one silently changes what the column means. Preserve reviewed
    # order, remove exact duplicates, and use the SDP column_dictionary.csv
    # semicolon representation.
    if strategy in {"reviewed", "llm"}:
        selected = selected.copy()
        constraint_rows = selected["dictionary_role"] == "constraint"
        if constraint_rows.any():
            groups = selected.loc[constraint_rows].groupby(
                match_keys, sort=False, dropna=False
            )
            for _, group in groups:
                unique_iris: list = []
                for value in group["iri"]:
                    if value not in unique_iris:
                        unique_iris.append(value)
                selected.at[group.index[0], "iri"] = "; ".join(
                    str(value) for value in unique_iris
                )

    selected = selected.drop_duplicates(subset=match_keys, keep="first")
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
        term_type_guess = (
            _infer_term_type(suggestion) if field == "term_iri" else None
        )
        has_term_type = "term_type" in out.columns

        if overwrite:
            out.loc[row_ids, field] = suggestion["iri"]
            # An overwrite replaces the term, so its type is replaced with it.
            if term_type_guess and has_term_type:
                out.loc[row_ids, "term_type"] = term_type_guess
            if field == "unit_iri" and "unit_label" in out.columns:
                _fill_missing_unit_label(out, row_ids, suggestion)
            applied += len(row_ids)
            continue

        missing_now = out.loc[row_ids, field].apply(_is_missing)
        fill_rows = missing_now[missing_now].index
        if len(fill_rows) > 0:
            out.loc[fill_rows, field] = suggestion["iri"]
            if term_type_guess and has_term_type:
                _fill_missing_term_type(out, fill_rows, term_type_guess)
            if field == "unit_iri" and "unit_label" in out.columns:
                _fill_missing_unit_label(out, fill_rows, suggestion)
            applied += len(fill_rows)

        # A row that already carried this exact IRI still gets its type filled
        # in; a row carrying a *different* IRI keeps whatever type describes it.
        if term_type_guess and has_term_type:
            existing_rows = [
                row_id
                for row_id in missing_now[~missing_now].index
                if not _is_missing(out.at[row_id, field])
                and out.at[row_id, field] == suggestion["iri"]
            ]
            if existing_rows:
                _fill_missing_term_type(out, existing_rows, term_type_guess)
        skipped_existing += int((~missing_now).sum())

    if verbose:
        print(f"Applied {applied} semantic suggestion fields using the {strategy} strategy.")
        if skipped_existing:
            print(f"{skipped_existing} fields were left alone because the dictionary already had an IRI.")
        if unmatched:
            print(f"{unmatched} suggestions did not match any dictionary row.")
    return out


__all__ = ["apply_semantic_suggestions", "suggest_semantics"]
