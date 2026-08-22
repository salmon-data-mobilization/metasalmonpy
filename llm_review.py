from __future__ import annotations

import hashlib
import html
import json
import os
import re
import warnings
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Optional, Sequence

import pandas as pd
import requests

from .term_search import _normalize_explicit_sources, sources_for_role
from .text_safety import redact_secrets


LLM_ASSESSMENT_COLUMNS = [
    "dataset_id",
    "table_id",
    "column_name",
    "code_value",
    "dictionary_role",
    "target_scope",
    "target_sdp_file",
    "target_sdp_field",
    "search_query",
    "llm_provider",
    "llm_model",
    "llm_decision",
    "llm_confidence",
    "llm_selected_candidate_index",
    "llm_selected_iri",
    "llm_selected_label",
    "llm_rationale",
    "llm_missing_context",
    "llm_bundle_summary",
    "llm_retry_query",
    "llm_new_term_label",
    "llm_new_term_definition",
    "llm_new_term_namespace",
    "llm_context_sources",
    "llm_exploration_used",
    "llm_exploration_queries",
    "llm_exploration_candidate_gain",
    "llm_error",
    "llm_escalated_from",
    "llm_retry_query_rejection_reason",
]

TARGET_JOIN_COLUMNS = [
    "dataset_id",
    "table_id",
    "column_name",
    "code_value",
    "dictionary_role",
    "target_scope",
    "target_sdp_file",
    "target_sdp_field",
    "search_query",
]

ALLOWED_DECISIONS = {
    "accept",
    "review",
    "retry_search",
    "request_new_term",
    "reject_shortlist",
}
AUTO_APPLY_ROLES = {"variable", "property", "entity", "unit"}
# The dictionary slots, in order — the authority every other role surface is
# checked against (mirrors .ms_semantic_bundle_slot_fields). sdp-0.3.0
# replaced the dictionary method slot with statistical_modifier_iri.
BUNDLE_SLOT_FIELDS = {
    "variable": "term_iri",
    "property": "property_iri",
    "entity": "entity_iri",
    "unit": "unit_iri",
    "constraint": "constraint_iri",
    "statistical_modifier": "statistical_modifier_iri",
}
# `method` stays a bundle role with NO slot field: codes-scope targets still
# search shared-vocabulary procedures for codes.csv term_iri, so the bundle
# payload names the role (always already_filled_or_not_requested for column
# bundles) without offering a dictionary field to write
# (mirrors .ms_semantic_bundle_roles).
BUNDLE_ROLES = tuple(BUNDLE_SLOT_FIELDS) + ("method",)


def _missing(value) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return isinstance(value, str) and not value.strip()


def _text(value, default=None):
    return default if _missing(value) else str(value).strip()


def _target_key(row) -> tuple:
    return tuple(_text(row.get(column), "") for column in TARGET_JOIN_COLUMNS)


def normalize_assessment_rows(rows=None) -> pd.DataFrame:
    """Return assessment rows with the stable 30-column public schema."""
    frame = pd.DataFrame(rows).copy() if rows is not None else pd.DataFrame()
    defaults = {
        "llm_confidence": pd.NA,
        "llm_selected_candidate_index": pd.NA,
        "llm_exploration_used": False,
        "llm_exploration_candidate_gain": 0,
    }
    for column in LLM_ASSESSMENT_COLUMNS:
        if column not in frame:
            frame[column] = defaults.get(column, pd.NA)
    if not frame.empty:
        frame["llm_confidence"] = pd.to_numeric(
            frame["llm_confidence"], errors="coerce"
        )
        frame["llm_selected_candidate_index"] = pd.to_numeric(
            frame["llm_selected_candidate_index"], errors="coerce"
        ).astype("Int64")
        frame["llm_exploration_used"] = (
            frame["llm_exploration_used"].fillna(False).astype(bool)
        )
        frame["llm_exploration_candidate_gain"] = pd.to_numeric(
            frame["llm_exploration_candidate_gain"], errors="coerce"
        ).fillna(0).astype("Int64")
    return frame[LLM_ASSESSMENT_COLUMNS]


def make_source_policy(sources: Optional[Sequence[str]]) -> dict:
    if sources is None:
        return {"explicit": False, "sources": None}
    normalized = _normalize_explicit_sources(sources)
    return {"explicit": True, "sources": normalized}


def policy_sources(policy: dict, role: str) -> tuple[str, ...]:
    if policy["explicit"]:
        return tuple(policy["sources"])
    return tuple(sources_for_role(role))


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        text = data.strip()
        if text:
            self.parts.append(text)


def _decode_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _read_docx(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml").decode("utf-8", errors="replace")
    return " ".join(
        html.unescape(text)
        for text in re.findall(r"<w:t[^>]*>(.*?)</w:t>", xml, flags=re.S)
    )


def _read_context_file(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".html", ".htm"}:
        parser = _TextExtractor()
        parser.feed(_decode_text(path))
        return "\n".join(parser.parts)
    if suffix == ".docx":
        return _read_docx(path)
    if suffix in {".xls", ".xlsx", ".xlsm"}:
        workbook = pd.read_excel(path, sheet_name=None)
        return "\n\n".join(
            f"Sheet: {name}\n{frame.to_csv(index=False)}"
            for name, frame in workbook.items()
        )
    if suffix == ".ipynb":
        notebook = json.loads(_decode_text(path))
        cells = notebook.get("cells", [])
        return "\n\n".join(
            "".join(cell.get("source", []))
            for cell in cells
            if cell.get("cell_type") in {"markdown", "code"}
        )
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise ImportError(
                "PDF context requires the optional pypdf dependency."
            ) from exc
        return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    return _decode_text(path)


def _normalize_context_files(context_files) -> list[Path]:
    if context_files is None:
        return []
    if isinstance(context_files, (str, os.PathLike)):
        values = [context_files]
    elif isinstance(context_files, Sequence) and not isinstance(
        context_files, (pd.DataFrame, pd.Series)
    ):
        values = list(context_files)
    else:
        raise TypeError(
            "llm_context_files must contain local file paths, not parsed "
            "data frames, XML objects, or other in-memory objects."
        )
    paths = []
    for value in values:
        if not isinstance(value, (str, os.PathLike)):
            raise TypeError(
                "llm_context_files must contain only local file paths."
            )
        path = Path(value).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"LLM context file does not exist: {path}")
        paths.append(path)
    return paths


def validate_context_files(context_files) -> list[Path]:
    """Validate that context inputs are existing local paths."""
    return _normalize_context_files(context_files)


def load_context_chunks(
    context_files=None,
    context_text=None,
    chunk_size: int = 1400,
) -> pd.DataFrame:
    paths = _normalize_context_files(context_files)
    records = []
    labels = {}
    for path in paths:
        base = path.name
        labels[base] = labels.get(base, 0) + 1
        source = base if labels[base] == 1 else f"{base} [{labels[base]}]"
        records.append((source, _read_context_file(path)))
    if context_text is not None:
        values = (
            [context_text]
            if isinstance(context_text, str)
            else list(context_text)
        )
        for index, value in enumerate(values, start=1):
            if not isinstance(value, str):
                raise TypeError("llm_context_text must contain only strings.")
            records.append((f"inline-context-{index}", value))

    chunks = []
    for source, value in records:
        text = re.sub(r"\s+", " ", value).strip()
        if not text:
            continue
        for index, start in enumerate(range(0, len(text), chunk_size), start=1):
            chunks.append(
                {
                    "source": source,
                    "chunk_id": f"{source}#{index}",
                    "text": text[start : start + chunk_size],
                }
            )
    return pd.DataFrame(chunks, columns=["source", "chunk_id", "text"])


def _relevant_context(chunks: pd.DataFrame, targets: pd.DataFrame) -> pd.DataFrame:
    if chunks.empty:
        return chunks
    target_text = " ".join(
        str(value)
        for column in (
            "target_label",
            "target_description",
            "search_query",
            "target_query_context",
        )
        if column in targets
        for value in targets[column].dropna()
    ).lower()
    tokens = {
        token
        for token in re.findall(r"[a-z0-9]+", target_text)
        if len(token) > 2
    }
    ranked = chunks.copy()
    ranked["_score"] = ranked["text"].str.lower().map(
        lambda value: sum(token in value for token in tokens)
    )
    return (
        ranked.sort_values(
            ["_score", "source", "chunk_id"],
            ascending=[False, True, True],
        )
        .head(8)
        .drop(columns="_score")
        .reset_index(drop=True)
    )


def resolve_llm_config(
    provider: str,
    model: Optional[str],
    api_key: Optional[str],
    base_url: Optional[str],
    reasoning_effort: Optional[str],
    timeout_seconds: int,
    request_fn,
) -> dict:
    provider = str(provider).lower()
    presets = {
        "openai": {
            "model": "gpt-5-mini",
            "base_url": "https://api.openai.com/v1",
            "key_env": "OPENAI_API_KEY",
        },
        "openrouter": {
            "model": "openrouter/free",
            "base_url": "https://openrouter.ai/api/v1",
            "key_env": "OPENROUTER_API_KEY",
        },
        "openai_compatible": {
            "model": None,
            "base_url": None,
            "key_env": "METASALMON_LLM_API_KEY",
        },
        "chapi": {
            "model": "ollama2.mistral:7b",
            "base_url": "https://chapi-dev.intra.azure.cloud.dfo-mpo.gc.ca/api",
            "key_env": "CHAPI_API_KEY",
        },
    }
    if provider not in presets:
        raise ValueError(
            "llm_provider must be openai, openrouter, openai_compatible, or chapi."
        )
    preset = presets[provider]
    resolved = {
        "provider": provider,
        "model": model
        or os.getenv(f"{provider.upper()}_MODEL")
        or preset["model"],
        "base_url": base_url
        or os.getenv(f"{provider.upper()}_BASE_URL")
        or preset["base_url"],
        "api_key": api_key or os.getenv(preset["key_env"]),
        "reasoning_effort": reasoning_effort,
        "timeout_seconds": timeout_seconds,
        "request_fn": request_fn,
    }
    if not resolved["model"]:
        raise ValueError("llm_model is required for this provider.")
    if request_fn is None and not resolved["api_key"]:
        raise ValueError(
            f"No API key is configured for llm_provider={provider!r}."
        )
    if request_fn is None and not resolved["base_url"]:
        raise ValueError(
            "llm_base_url is required for an OpenAI-compatible provider."
        )
    return resolved


def _clean_json_text(value: str) -> str:
    text = str(value).strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _response_data(result):
    if isinstance(result, requests.Response):
        result.raise_for_status()
        result = result.json()
    if isinstance(result, dict) and result.get("data") is not None:
        return result["data"]
    if isinstance(result, dict) and "choices" in result:
        content = (
            result.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )
        return json.loads(_clean_json_text(content))
    if isinstance(result, dict) and "content" in result and len(result) <= 3:
        content = result.get("content")
        if isinstance(content, str):
            return json.loads(_clean_json_text(content))
    return result


def request_json(messages: list[dict], config: dict):
    request_fn = config["request_fn"]
    if request_fn is not None:
        return _response_data(request_fn(messages, config))
    body = {
        "model": config["model"],
        "messages": messages,
        "response_format": {"type": "json_object"},
    }
    if config.get("reasoning_effort"):
        body["reasoning_effort"] = config["reasoning_effort"]
    response = requests.post(
        f"{str(config['base_url']).rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {config['api_key']}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=config["timeout_seconds"],
    )
    return _response_data(response)


def _candidate_id(row, role: str, position: int) -> str:
    source = _text(row.get("source"), "unknown")
    iri = _text(row.get("iri"))
    if iri:
        return f"{source}::{iri}"
    evidence = "|".join(
        [
            role,
            source,
            _text(row.get("ontology"), ""),
            _text(row.get("label"), ""),
            _text(row.get("definition"), ""),
        ]
    )
    return f"{source}::blank::{hashlib.sha256(evidence.encode()).hexdigest()[:16]}"


def _candidate_payload(candidates: pd.DataFrame, role: str) -> list[dict]:
    rows = []
    for position, (_, row) in enumerate(candidates.iterrows(), start=1):
        rows.append(
            {
                "candidate_id": _candidate_id(row, role, position),
                "index": position,
                "label": _text(row.get("label")),
                "iri": _text(row.get("iri")),
                "source": _text(row.get("source")),
                "ontology": _text(row.get("ontology")),
                "ontology_type": _text(
                    row.get("ontology_type"),
                    _text(row.get("term_type"), _text(row.get("role"))),
                ),
                "definition": _text(row.get("definition")),
                "role_hints": _text(row.get("role_hints")),
                "score": (
                    float(row.get("score"))
                    if not _missing(row.get("score"))
                    else None
                ),
            }
        )
    return rows


def _candidates_for_target(
    suggestions: pd.DataFrame,
    target,
) -> pd.DataFrame:
    if suggestions.empty:
        return suggestions.copy()
    key = _target_key(target)
    mask = suggestions.apply(lambda row: _target_key(row) == key, axis=1)
    return suggestions.loc[mask].reset_index(drop=True)


def _base_assessment(target, config: dict, context: pd.DataFrame) -> dict:
    return {
        **{
            column: target.get(column, pd.NA)
            for column in TARGET_JOIN_COLUMNS
            if column != "search_query"
        },
        "search_query": target.get("search_query", pd.NA),
        "llm_provider": config["provider"],
        "llm_model": config["model"],
        "llm_decision": pd.NA,
        "llm_confidence": pd.NA,
        "llm_selected_candidate_index": pd.NA,
        "llm_selected_iri": pd.NA,
        "llm_selected_label": pd.NA,
        "llm_rationale": pd.NA,
        "llm_missing_context": pd.NA,
        "llm_bundle_summary": pd.NA,
        "llm_retry_query": pd.NA,
        "llm_new_term_label": pd.NA,
        "llm_new_term_definition": pd.NA,
        "llm_new_term_namespace": pd.NA,
        "llm_context_sources": (
            "; ".join(dict.fromkeys(context["source"]))
            if not context.empty
            else pd.NA
        ),
        "llm_exploration_used": False,
        "llm_exploration_queries": pd.NA,
        "llm_exploration_candidate_gain": 0,
        "llm_error": pd.NA,
        "llm_escalated_from": pd.NA,
        "llm_retry_query_rejection_reason": pd.NA,
    }


def _validate_item(item, candidates: pd.DataFrame, role: str) -> dict:
    if not isinstance(item, dict):
        raise ValueError("Assessment item must be a JSON object.")
    decision = str(item.get("decision") or "").strip().lower()
    if decision == "propose_new_term":
        decision = "request_new_term"
    if decision not in ALLOWED_DECISIONS:
        raise ValueError(f"Unsupported LLM decision for {role}: {decision!r}.")

    payload = _candidate_payload(candidates, role)
    selected_index = item.get("selected_candidate_index")
    selected_id = _text(item.get("selected_candidate_id"))
    if selected_id:
        matching = [
            candidate["index"]
            for candidate in payload
            if candidate["candidate_id"] == selected_id
        ]
        if len(matching) != 1:
            raise ValueError(
                f"Unknown selected_candidate_id for role {role}: {selected_id}"
            )
        selected_index = matching[0]
    if selected_index is not None and not _missing(selected_index):
        try:
            selected_index = int(selected_index)
        except (TypeError, ValueError) as exc:
            raise ValueError("selected_candidate_index must be an integer.") from exc
        if selected_index < 1 or selected_index > len(candidates):
            decision = "review"
            selected_index = None
    else:
        selected_index = None
    if decision == "accept" and selected_index is None:
        decision = "review"

    confidence = item.get("confidence")
    try:
        confidence = float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        confidence = None
    if confidence is not None:
        confidence = max(0.0, min(1.0, confidence))
    return {
        "decision": decision,
        "confidence": confidence,
        "selected_index": selected_index,
        "rationale": _text(item.get("rationale")),
        "missing_context": _text(item.get("missing_context")),
        "retry_query": _text(item.get("retry_query")),
        "new_term_label": _text(
            item.get("suggested_label"), _text(item.get("new_term_label"))
        ),
        "new_term_definition": _text(
            item.get("suggested_definition"),
            _text(item.get("new_term_definition")),
        ),
        "new_term_namespace": _text(
            item.get("suggested_namespace"),
            _text(item.get("new_term_namespace")),
        ),
    }


def _assessment_from_item(
    target,
    candidates: pd.DataFrame,
    item,
    config: dict,
    context: pd.DataFrame,
    bundle_summary=None,
) -> dict:
    validated = _validate_item(item, candidates, str(target["dictionary_role"]))
    row = _base_assessment(target, config, context)
    selected = validated["selected_index"]
    row.update(
        {
            "llm_decision": validated["decision"],
            "llm_confidence": validated["confidence"],
            "llm_selected_candidate_index": selected,
            "llm_selected_iri": (
                candidates.iloc[selected - 1].get("iri")
                if selected is not None
                else pd.NA
            ),
            "llm_selected_label": (
                candidates.iloc[selected - 1].get("label")
                if selected is not None
                else pd.NA
            ),
            "llm_rationale": validated["rationale"],
            "llm_missing_context": validated["missing_context"],
            "llm_bundle_summary": bundle_summary,
            "llm_retry_query": validated["retry_query"],
            "llm_new_term_label": validated["new_term_label"],
            "llm_new_term_definition": validated["new_term_definition"],
            "llm_new_term_namespace": validated["new_term_namespace"],
        }
    )
    return row


def _error_assessment(target, error, config, context) -> dict:
    row = _base_assessment(target, config, context)
    # Redacted where the provider's text is CAPTURED, not where it is shown.
    # This row is returned on the exported ``semantic_llm_assessments``
    # attribute and written to CSV, so display-time redaction would be too
    # late — metasalmon 0.2.0 made the same correction on its bundle-review
    # path for the same reason.
    row["llm_error"] = redact_secrets(error)
    return row


def _target_payload(target, candidates, context) -> dict:
    role = str(target["dictionary_role"])
    return {
        "target": {
            column: None if _missing(target.get(column)) else target.get(column)
            for column in target.index
        },
        "candidates": _candidate_payload(candidates, role),
        "context": context.to_dict("records"),
    }


def _generic_messages(target, candidates, context) -> list[dict]:
    payload = _target_payload(target, candidates, context)
    return [
        {
            "role": "system",
            "content": (
                "Judge only the supplied ontology candidates. Return JSON with "
                "decision, confidence, selected_candidate_index, rationale, "
                "missing_context, retry_query, suggested_label, "
                "suggested_definition, and suggested_namespace. decision must "
                "be accept, review, retry_search, request_new_term, or "
                "reject_shortlist. Never invent an IRI."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(payload, default=str, ensure_ascii=True),
        },
    ]


def _bundle_payload(
    targets: pd.DataFrame,
    suggestions: pd.DataFrame,
    context: pd.DataFrame,
    source_policy: dict,
    dictionary: pd.DataFrame,
) -> dict:
    first = targets.iloc[0]
    dictionary_row = _dictionary_row(first, dictionary)
    target_by_role = {
        str(target["dictionary_role"]): target
        for _, target in targets.iterrows()
    }
    current_slots = {
        role: (
            None
            if _missing(dictionary_row.get(field))
            else dictionary_row.get(field)
        )
        for role, field in BUNDLE_SLOT_FIELDS.items()
    }
    slots = []
    for role in BUNDLE_ROLES:
        field = BUNDLE_SLOT_FIELDS.get(role)
        target = target_by_role.get(role)
        if target is None:
            slots.append(
                {
                    "role": role,
                    # `method` has no dictionary slot field (sdp-0.3.0 dropped
                    # method_iri; the role survives only for codes-scope
                    # searches), so an unrequested role may have no field.
                    "target_sdp_field": field,
                    "status": "already_filled_or_not_requested",
                    "current_value": current_slots.get(role),
                    "candidates": [],
                }
            )
            continue
        candidates = _candidates_for_target(suggestions, target)
        slots.append(
            {
                "role": role,
                "status": "review",
                "current_value": current_slots.get(role),
                "search_query": target.get("search_query"),
                "target_sdp_field": target.get("target_sdp_field"),
                "candidates": _candidate_payload(candidates, role),
            }
        )
    dictionary_context = {
        column: (
            None
            if _missing(dictionary_row.get(column))
            else dictionary_row.get(column)
        )
        for column in (
            "dataset_id",
            "table_id",
            "column_name",
            "column_label",
            "column_description",
            "column_role",
            "value_type",
            "unit_label",
            "term_type",
        )
    }
    return {
        "bundle": {
            "dataset_id": first.get("dataset_id"),
            "table_id": first.get("table_id"),
            "column_name": first.get("column_name"),
            "column_label": first.get("column_label"),
            "column_description": first.get("column_description"),
            "unit_label": first.get("unit_label"),
        },
        "dictionary_context": dictionary_context,
        "current_slots": current_slots,
        "source_policy": {
            "mode": "strict_allowlist"
            if source_policy["explicit"]
            else "role_aware_defaults",
            "sources": list(source_policy["sources"] or ()),
        },
        "slots": slots,
        "context": context.to_dict("records"),
    }


def _bundle_messages(payload) -> list[dict]:
    # The opening "Judge ..." instruction is a role-contract surface: it must
    # name exactly the dictionary slots (mirrors
    # .ms_semantic_bundle_system_prompt, which sdp-0.3.0 shipped still naming
    # the removed `method` slot — the defect the role-contract guard pins).
    return [
        {
            "role": "system",
            "content": (
                "Review this measurement as one I-ADOPT semantic bundle. "
                "Judge variable, property, entity, unit, constraint, and "
                "statistical_modifier together before finalizing any slot. "
                "A method is never a dictionary slot: procedures are "
                "usedProcedure-style context recorded on tables.csv or "
                "resolved through codes.csv, so method targets appear only "
                "for code values. "
                "statistical_modifier is part of variable identity (I-ADOPT "
                "StatisticalModifier); accept one only when the column is an "
                "aggregation or summary such as a mean, maximum, total, or "
                "peak. "
                "Return JSON with bundle_summary and exactly one item in slots "
                "for each supplied slot whose status is review. Do not assess "
                "slots already marked as filled. Each item uses role, decision, "
                "confidence, selected_candidate_id, rationale, missing_context, "
                "retry_query, suggested_label, suggested_definition, and "
                "suggested_namespace. Candidate ontology types are native and "
                "must not be rewritten. Never invent an IRI."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(payload, default=str, ensure_ascii=True),
        },
    ]


def _assess_generic(target, candidates, context, config) -> dict:
    try:
        result = request_json(
            _generic_messages(target, candidates, context),
            config,
        )
        return _assessment_from_item(
            target,
            candidates,
            result,
            config,
            context,
        )
    except Exception as exc:
        return _error_assessment(target, exc, config, context)


def _assess_bundle(
    targets,
    suggestions,
    context,
    config,
    source_policy,
    dictionary,
) -> list[dict]:
    try:
        result = request_json(
            _bundle_messages(
                _bundle_payload(
                    targets,
                    suggestions,
                    context,
                    source_policy,
                    dictionary,
                )
            ),
            config,
        )
    except Exception as exc:
        return [
            _error_assessment(target, exc, config, context)
            for _, target in targets.iterrows()
        ]
    if not isinstance(result, dict) or not isinstance(
        result.get("slots"), list
    ):
        return [
            _assess_generic(
                target,
                _candidates_for_target(suggestions, target),
                context,
                config,
            )
            for _, target in targets.iterrows()
        ]

    items_by_role = {}
    duplicated = set()
    for item in result["slots"]:
        role = _text(item.get("role")) if isinstance(item, dict) else None
        if role in items_by_role:
            duplicated.add(role)
        elif role:
            items_by_role[role] = item

    rows = []
    for _, target in targets.iterrows():
        role = str(target["dictionary_role"])
        candidates = _candidates_for_target(suggestions, target)
        item = items_by_role.get(role)
        if item is None or role in duplicated:
            rows.append(_assess_generic(target, candidates, context, config))
            continue
        try:
            rows.append(
                _assessment_from_item(
                    target,
                    candidates,
                    item,
                    config,
                    context,
                    bundle_summary=_text(result.get("bundle_summary")),
                )
            )
        except Exception:
            rows.append(_assess_generic(target, candidates, context, config))
    return rows


def _normalize_query(value) -> str:
    return re.sub(r"\s+", " ", _text(value, "")).strip().casefold()


def _classify_retry_query(retry_query, original_query) -> Optional[str]:
    normalized = _normalize_query(retry_query)
    if not normalized:
        return "missing_retry_query"
    if normalized == _normalize_query(original_query):
        return "duplicate_original_query"
    if re.fullmatch(r"(?:https?://\S+|[A-Za-z]+:[^\s]+)", normalized):
        return "identifier_like_query"
    return None


def _generated_retry_query(target, row, config) -> Optional[str]:
    messages = [
        {
            "role": "system",
            "content": (
                "Return JSON with alternate_queries containing one short "
                "plain-language ontology search phrase. Do not return an IRI, "
                "CURIE, DOI, or the original query."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "dictionary_role": target.get("dictionary_role"),
                    "target_label": target.get("target_label"),
                    "target_description": target.get("target_description"),
                    "original_query": target.get("search_query"),
                    "rejected_retry_query": row.get("llm_retry_query"),
                    "rationale": row.get("llm_rationale"),
                },
                default=str,
            ),
        },
    ]
    try:
        result = request_json(messages, config)
    except Exception:
        return None
    if not isinstance(result, dict):
        return None
    values = (
        result.get("alternate_queries")
        or result.get("queries")
        or result.get("suggested_queries")
        or []
    )
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return None
    original = _normalize_query(target.get("search_query"))
    for value in values:
        query = re.sub(r"\s+", " ", _text(value, "")).strip()
        if (
            query
            and _normalize_query(query) != original
            and _classify_retry_query(query, target.get("search_query")) is None
        ):
            return query
    return None


def _retry_candidates(
    target,
    query: str,
    search_fn: Callable,
    source_policy: dict,
    max_per_role: int,
) -> pd.DataFrame:
    role = str(target["dictionary_role"])
    result = search_fn(
        query,
        role=role,
        sources=policy_sources(source_policy, role),
    )
    if result is None or result.empty:
        return pd.DataFrame()
    result = result.copy()
    result["retrieval_query"] = query
    result["retrieval_pass"] = 2
    for column, value in target.items():
        result[column] = value
    result["search_query"] = target["search_query"]
    dedupe = [column for column in ("source", "iri", "label") if column in result]
    if dedupe:
        result = result.drop_duplicates(dedupe)
    if "score" in result:
        result["score"] = pd.to_numeric(result["score"], errors="coerce")
        result = result.sort_values("score", ascending=False, na_position="last")
    return result.head(max_per_role)


def _merge_retry_candidates(
    suggestions,
    retry_rows,
    target,
    max_per_role,
) -> tuple[pd.DataFrame, int]:
    before = _candidates_for_target(suggestions, target)
    before_ids = {
        _candidate_id(row, str(target["dictionary_role"]), position)
        for position, (_, row) in enumerate(before.iterrows(), start=1)
    }
    combined = pd.concat([suggestions, retry_rows], ignore_index=True, sort=False)
    key_mask = combined.apply(lambda row: _target_key(row) == _target_key(target), axis=1)
    target_rows = combined.loc[key_mask].copy()
    other_rows = combined.loc[~key_mask].copy()
    dedupe = [
        column for column in ("source", "iri", "label") if column in target_rows
    ]
    if dedupe:
        target_rows = target_rows.drop_duplicates(dedupe, keep="first")
    if "score" in target_rows:
        target_rows["score"] = pd.to_numeric(
            target_rows["score"], errors="coerce"
        )
        target_rows = target_rows.sort_values(
            "score", ascending=False, na_position="last"
        )
    target_rows = target_rows.head(max_per_role)
    after_ids = {
        _candidate_id(row, str(target["dictionary_role"]), position)
        for position, (_, row) in enumerate(target_rows.iterrows(), start=1)
    }
    gain = len(after_ids - before_ids)
    return (
        pd.concat([other_rows, target_rows], ignore_index=True, sort=False),
        gain,
    )


def _restore_target_candidates(current, original, target) -> pd.DataFrame:
    current_mask = current.apply(
        lambda row: _target_key(row) == _target_key(target),
        axis=1,
    )
    original_rows = _candidates_for_target(original, target)
    return pd.concat(
        [current.loc[~current_mask], original_rows],
        ignore_index=True,
        sort=False,
    )


def _reject_duplicate_retry(row) -> None:
    row["llm_retry_query_rejection_reason"] = "duplicate_original_query"
    note = (
        "Retry query matched the original query after case and whitespace "
        "normalization; the retry was not issued."
    )
    rationale = _text(row.get("llm_rationale"), "")
    row["llm_rationale"] = f"{rationale} {note}".strip()


def _escalate_reject_shortlist(row, initial=None) -> None:
    if _text(row.get("llm_decision")) != "reject_shortlist":
        return
    before = _text((initial or {}).get("llm_rationale"), "")
    after = _text(row.get("llm_rationale"), "")
    parts = []
    if before:
        parts.append(f"Initial shortlist rejection: {before}")
    if after and after != before:
        parts.append(f"Post-retry shortlist rejection: {after}")
    if not parts and after:
        parts.append(f"Shortlist rejection: {after}")
    parts.append(
        "Shortlist rejected and exploration found no acceptable candidate; "
        "escalated to request_new_term so the likely ontology gap is surfaced."
    )
    row["llm_decision"] = "request_new_term"
    row["llm_escalated_from"] = "reject_shortlist"
    row["llm_rationale"] = " ".join(parts)


def _apply_bundle_retry(
    targets,
    suggestions,
    rows,
    context,
    config,
    source_policy,
    dictionary,
    search_fn,
    max_per_role,
) -> tuple[pd.DataFrame, list[dict]]:
    original_suggestions = suggestions.copy()
    valid_retry = False
    gains = {}
    retry_queries = {}
    initial_rows = {
        str(row["dictionary_role"]): row for row in rows
    }
    for row in rows:
        if _text(row.get("llm_decision")) != "retry_search":
            continue
        target = targets.loc[
            targets.apply(lambda value: _target_key(value) == _target_key(row), axis=1)
        ].iloc[0]
        reason = _classify_retry_query(
            row.get("llm_retry_query"),
            target.get("search_query"),
        )
        if reason == "duplicate_original_query":
            _reject_duplicate_retry(row)
            continue
        if reason == "identifier_like_query":
            query = _generated_retry_query(target, row, config)
            if query is None:
                continue
        elif reason:
            continue
        else:
            query = str(row["llm_retry_query"])
        role = str(target["dictionary_role"])
        gains[role] = 0
        retry_queries[role] = query
        row["llm_exploration_used"] = True
        row["llm_exploration_queries"] = query
        row["llm_exploration_candidate_gain"] = 0
        retry = _retry_candidates(
            target,
            query,
            search_fn,
            source_policy,
            max_per_role,
        )
        if retry.empty:
            continue
        merged_suggestions, gain = _merge_retry_candidates(
            suggestions,
            retry,
            target,
            max_per_role,
        )
        gains[role] = gain
        row["llm_exploration_candidate_gain"] = gain
        if gain > 0:
            suggestions = merged_suggestions
        valid_retry = valid_retry or gain > 0

    if valid_retry:
        reassessed = _assess_bundle(
            targets,
            suggestions,
            context,
            config,
            source_policy,
            dictionary,
        )
        reassessed_by_role = {
            str(row["dictionary_role"]): row for row in reassessed
        }
        rows = []
        for role, original in initial_rows.items():
            replacement = reassessed_by_role.get(role)
            if (
                role in gains
                and gains[role] > 0
                and replacement is not None
                and _missing(replacement.get("llm_error"))
            ):
                replacement["llm_exploration_used"] = True
                replacement["llm_exploration_queries"] = retry_queries[role]
                replacement["llm_exploration_candidate_gain"] = gains[role]
                rows.append(replacement)
            else:
                rows.append(original)
                if role in gains and gains[role] > 0:
                    target = targets.loc[
                        targets["dictionary_role"].astype(str) == role
                    ].iloc[0]
                    suggestions = _restore_target_candidates(
                        suggestions,
                        original_suggestions,
                        target,
                    )

    for row in rows:
        role = str(row["dictionary_role"])
        initial = initial_rows.get(role, {})
        _escalate_reject_shortlist(row, initial)
    return suggestions, rows


def _apply_generic_retry(
    target,
    suggestions,
    row,
    context,
    config,
    source_policy,
    search_fn,
    max_per_role,
) -> tuple[pd.DataFrame, dict]:
    if _text(row.get("llm_decision")) != "retry_search":
        return suggestions, row
    reason = _classify_retry_query(
        row.get("llm_retry_query"),
        target.get("search_query"),
    )
    if reason == "duplicate_original_query":
        _reject_duplicate_retry(row)
        return suggestions, row
    if reason == "identifier_like_query":
        query = _generated_retry_query(target, row, config)
        if query is None:
            return suggestions, row
    elif reason:
        return suggestions, row
    else:
        query = str(row["llm_retry_query"])

    extra = _retry_candidates(
        target,
        query,
        search_fn,
        source_policy,
        max_per_role,
    )
    row["llm_exploration_used"] = True
    row["llm_exploration_queries"] = query
    if extra.empty:
        row["llm_exploration_candidate_gain"] = 0
        return suggestions, row

    merged, gain = _merge_retry_candidates(
        suggestions,
        extra,
        target,
        max_per_role,
    )
    row["llm_exploration_candidate_gain"] = gain
    if gain <= 0:
        return suggestions, row

    candidates = _candidates_for_target(merged, target)
    reassessed = _assess_generic(target, candidates, context, config)
    if not _missing(reassessed.get("llm_error")):
        return suggestions, row
    reassessed["llm_exploration_used"] = True
    reassessed["llm_exploration_queries"] = query
    reassessed["llm_exploration_candidate_gain"] = gain
    return merged, reassessed


VALIDATOR_FINDING_COLUMNS = [
    "dataset_id",
    "table_id",
    "column_name",
    "code",
    "severity",
    "role",
    "before_decision",
    "after_decision",
    "message",
]


def _dictionary_row(target, dictionary) -> dict:
    matches = dictionary[
        (dictionary["dataset_id"].astype(str) == str(target.get("dataset_id")))
        & (dictionary["table_id"].astype(str) == str(target.get("table_id")))
        & (
            dictionary["column_name"].astype(str)
            == str(target.get("column_name"))
        )
    ]
    return matches.iloc[0].to_dict() if not matches.empty else {}


def _field_anchors(target, dictionary_row: dict) -> tuple[list[str], list[str]]:
    weak = {
        "age",
        "code",
        "count",
        "length",
        "method",
        "number",
        "phase",
        "rate",
        "sex",
        "total",
        "unit",
        "value",
        "weight",
    }
    identifiers = []
    phrases = []
    column_name = _text(
        dictionary_row.get("column_name"),
        _text(target.get("column_name")),
    )
    if column_name:
        normalized_identifier = column_name.strip().lower()
        if re.fullmatch(r"[a-z0-9_]+", normalized_identifier):
            identifiers.append(normalized_identifier)
    for value in (
        dictionary_row.get("column_label"),
        target.get("column_label"),
        column_name,
    ):
        phrase = re.sub(r"[^a-z0-9]+", " ", _text(value, "").lower()).strip()
        tokens = phrase.split()
        if phrase and not (len(tokens) < 2 and (len(phrase) < 6 or phrase in weak)):
            phrases.append(phrase)
    return list(dict.fromkeys(identifiers)), list(dict.fromkeys(phrases))


def _anchored_context_text(target, dictionary_row: dict, context) -> list[str]:
    if context is None or context.empty or "text" not in context:
        return []
    identifiers, phrases = _field_anchors(target, dictionary_row)
    selected = []
    for value in context["text"].dropna().astype(str):
        lowered = value.lower()
        normalized = re.sub(r"[^a-z0-9]+", " ", lowered).strip()
        identifier_match = any(
            re.search(rf"(?<![a-z0-9_]){re.escape(anchor)}(?![a-z0-9_])", lowered)
            for anchor in identifiers
        )
        phrase_match = any(
            normalized == anchor or normalized.startswith(f"{anchor} ")
            for anchor in phrases
        )
        if identifier_match or phrase_match:
            selected.append(value)
    return selected


def _evidence_text(target, dictionary, context=None) -> str:
    dictionary_row = _dictionary_row(target, dictionary)
    values = [
        target.get("target_query_context"),
        target.get("column_label"),
        target.get("column_description"),
        dictionary_row.get("column_name"),
        dictionary_row.get("column_label"),
        dictionary_row.get("column_description"),
        dictionary_row.get("unit_label"),
        *_anchored_context_text(target, dictionary_row, context),
    ]
    return " ".join(_text(value, "") for value in values).lower()


def _strip_negated_evidence(text: str, evidence_pattern: str) -> str:
    negation = (
        r"no|not|without|unknown|unspecified|missing|does not|did not|"
        r"is not|was not"
    )
    return re.sub(
        rf"\b(?:{negation})\b.{{0,60}}\b(?:{evidence_pattern})\b",
        " ",
        text,
    )


def _has_method_evidence(text: str) -> bool:
    terms = (
        r"protocol|gear|instrument|assay|technique|field method|lab method|"
        r"laboratory method|survey method|measurement method|estimation method|"
        r"field procedure|lab procedure|laboratory procedure|"
        r"measurement procedure|operational procedure"
    )
    positive = _strip_negated_evidence(text, terms)
    return bool(
        re.search(
            rf"\b(?:{terms})\b|"
            r"\b(?:measured|sampled|surveyed|enumerated|counted|weighed)\b"
            r".{0,80}\b(?:using|with|via)\b|"
            r"\b(?:using|with)\b.{0,80}\b(?:board|scale|net|sonar|weir|"
            r"camera|call?iper|ruler|sensor|model)\b|"
            r"\b(?:estimated|calculated|derived|modelled|modeled)\s+"
            r"(?:using|with|from)\b",
            positive,
        )
    )


def _has_modifier_evidence(text: str) -> bool:
    # Underscores are not word boundaries, so `mean_weight` needs splitting;
    # lowercased here because R's helper folds case itself
    # (mirrors .ms_semantic_validator_has_modifier_evidence).
    return bool(
        re.search(
            r"\b(?:mean|average|median|max|maximum|min|minimum|total|"
            r"cumulative|sum|peak|aggregate|aggregated)\b",
            re.sub(r"[_.]", " ", text.lower()),
        )
    )


def _has_constraint_evidence(text: str) -> bool:
    terms = (
        r"origin|life[ -]?cycle|life[ -]?stage|stage|run|season|age|sex|"
        r"maturity|phase|terminal|ocean|freshwater|wild|hatchery|population|"
        r"stock|species group|reporting unit|benchmark"
    )
    return bool(re.search(rf"\b(?:{terms})\b", _strip_negated_evidence(text, terms)))


def _candidate_type(candidate: dict) -> str:
    return " ".join(
        _text(candidate.get(field), "")
        for field in ("term_type", "native_type", "resource_kind", "type_iris")
    ).lower()


def _split_role_hints(value) -> set[str]:
    if _missing(value):
        return set()
    return {
        hint.strip().lower()
        for hint in re.split(r"[|,;]", str(value))
        if hint.strip()
    }


def _role_type_message(role: str, candidate: dict) -> Optional[str]:
    hints = _split_role_hints(candidate.get("role_hints"))
    if hints and role not in hints:
        return (
            f"Candidate role hints are incompatible with the {role} slot: "
            f"{', '.join(sorted(hints))}."
        )
    iri = _text(candidate.get("iri"), "").lower()
    native_type = _candidate_type(candidate)
    if re.search(
        r"object\s*property|datatype\s*property|annotation\s*property|"
        r"rdf\s*property|owl#(?:object|datatype|annotation)property",
        native_type,
    ):
        return (
            "Candidate is an ontology relation predicate, not a value that "
            f"can populate the {role} semantic slot."
        )
    combined = f"{iri} {native_type}"
    explicit_role = None
    if re.search(r"/vocab/unit/|\b(?:unit|unit of measure)\b", combined):
        explicit_role = "unit"
    elif re.search(r"/vocab/quantitykind/|\b(?:quantity kind|quantitykind)\b", combined):
        explicit_role = "property"
    elif re.search(r"\b(?:method|procedure)\b", native_type):
        explicit_role = "method"
    elif re.search(r"\bconstraint\b", native_type):
        explicit_role = "constraint"
    if explicit_role and explicit_role != role:
        return (
            f"Candidate native type is explicitly {explicit_role}-like and "
            f"is incompatible with the {role} slot."
        )
    return None


def _dimension(*values) -> Optional[str]:
    text = " ".join(_text(value, "") for value in values).lower()
    text = (
        text.replace("\u2212", "-")
        .replace("\u207b", "-")
        .replace("\u00b2", "2")
        .replace("\u00b3", "3")
        .replace("\u00b7", " ")
    )
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    if re.fullmatch(r".*(?:cubic metres? per second|m3/s|cumecs?|cms).*", text):
        return "flow"
    if re.fullmatch(r".*(?:kilometres? per hour|metres? per second|km/h|m/s|kph).*", text):
        return "speed"
    denominator = re.search(
        r"(?:\bper\s+|/\s*|[-_]per[-_])"
        r"(?:s|sec|second|min|minute|h|hr|hour|d|day|week|month|yr|year|season)\b"
        r"|(?:s|sec|second|min|minute|h|hr|hour|d|day|week|month|yr|year)"
        r"\s*\^?\s*-\s*1\b",
        text,
    )
    if denominator:
        if re.search(
            r"\b(?:frequency|occurrences?|individuals?|fish|events?)\s+per\b|"
            r"\b(?:survival|exploitation|harvest|mortality) rate\b",
            text,
        ):
            return "rate"
        return None
    rules = [
        ("temperature", r"\b(?:temperature|celsius|fahrenheit|kelvin|deg c)\b"),
        ("mass", r"\b(?:mass|weight|kilograms?|grams?|tonnes?|pounds?|lbs?|kg|kilogm)\b"),
        ("length", r"\b(?:length|width|depth|height|fork length|millimetres?|centimetres?|metres?|mm|cm)\b"),
        ("count", r"\b(?:count|abundance|number|numerosity|individuals?|num)\b"),
        (
            "dimensionless",
            r"\b(?:dimensionless|unitless|percentage|percent|proportion|ratio|"
            r"fraction|decimal)\b|/vocab/unit/(?:percent|one)\b",
        ),
    ]
    matched = [name for name, pattern in rules if re.search(pattern, text)]
    return matched[0] if len(matched) == 1 else None


def _selected_candidate(row, target, suggestions) -> Optional[dict]:
    raw_index = row.get("llm_selected_candidate_index")
    if _missing(raw_index):
        return None
    candidates = _candidates_for_target(suggestions, target)
    try:
        index = int(raw_index)
    except (TypeError, ValueError):
        return None
    if not 1 <= index <= len(candidates):
        return None
    return candidates.iloc[index - 1].to_dict()


def _finding(row, code: str, message: str) -> dict:
    return {
        "dataset_id": row.get("dataset_id"),
        "table_id": row.get("table_id"),
        "column_name": row.get("column_name"),
        "code": code,
        "severity": "warning",
        "role": row.get("dictionary_role"),
        "before_decision": "accept",
        "after_decision": "review",
        "message": message,
    }


def _downgrade(row, findings: list[dict]) -> None:
    row["llm_decision"] = "review"
    row["llm_selected_candidate_index"] = pd.NA
    row["llm_selected_iri"] = pd.NA
    row["llm_selected_label"] = pd.NA
    rationale = _text(row.get("llm_rationale"), "")
    notes = " ".join(
        f"[{finding['code']}] {finding['message']}" for finding in findings
    )
    row["llm_rationale"] = f"{rationale} {notes}".strip()


def _apply_validators(
    rows,
    targets,
    dictionary,
    suggestions,
    context,
) -> tuple[list[dict], list[dict]]:
    by_role = {str(row["dictionary_role"]): row for row in rows}
    target_by_role = {
        str(target["dictionary_role"]): target
        for _, target in targets.iterrows()
    }
    selected = {
        role: _selected_candidate(row, target_by_role[role], suggestions)
        for role, row in by_role.items()
        if _text(row.get("llm_decision")) == "accept"
    }
    selected_iris = {
        role: _text(candidate.get("iri"))
        for role, candidate in selected.items()
        if candidate is not None
    }
    all_findings = []
    for role, row in by_role.items():
        if _text(row.get("llm_decision")) != "accept":
            continue
        target = target_by_role[role]
        candidate = selected.get(role)
        if candidate is None:
            continue
        evidence = _evidence_text(target, dictionary, context)
        row_findings = []
        if role == "method" and not _has_method_evidence(evidence):
            row_findings.append(
                _finding(
                    row,
                    "SEM_METHOD_EVIDENCE_REQUIRED",
                    "The accepted method candidate lacks explicit field, "
                    "protocol, gear, instrument, or estimation-procedure evidence.",
                )
            )
        if role == "statistical_modifier" and not _has_modifier_evidence(
            evidence
        ):
            # The dictionary's statistical modifier is part of variable
            # identity, so an accept needs the column itself to name an
            # aggregation. Without this, an unsupported modifier silently
            # changes what the variable means. Sits BESIDE the surviving
            # method validator, not in place of it: the code-level method
            # role outlives sdp-0.3.0 (mirrors
            # .ms_validate_semantic_modifier_evidence).
            row_findings.append(
                _finding(
                    row,
                    "SEM_MODIFIER_EVIDENCE_REQUIRED",
                    "The accepted statistical-modifier candidate lacks "
                    "explicit aggregation evidence (mean, median, maximum, "
                    "minimum, total, or peak).",
                )
            )
        if role == "constraint" and not _has_constraint_evidence(evidence):
            row_findings.append(
                _finding(
                    row,
                    "SEM_CONSTRAINT_EVIDENCE_REQUIRED",
                    "The accepted constraint candidate lacks an explicit "
                    "qualifier such as origin, life stage, phase, season, age, "
                    "sex, stock, or reporting unit.",
                )
            )
        role_message = _role_type_message(role, candidate)
        if role_message:
            row_findings.append(
                _finding(row, "SEM_ROLE_TYPE_MISMATCH", role_message)
            )

        if role in {"property", "unit"}:
            dictionary_row = _dictionary_row(target, dictionary)
            expected = _dimension(
                dictionary_row.get("unit_label"),
                dictionary_row.get("unit_iri"),
            )
            actual = _dimension(
                candidate.get("label"),
                candidate.get("definition"),
                candidate.get("iri"),
            )
            if expected and actual and expected != actual:
                row_findings.append(
                    _finding(
                        row,
                        "SEM_DIMENSION_MISMATCH",
                        f"Candidate appears {actual}-dimensional, but the "
                        f"dictionary unit is {expected}-dimensional.",
                    )
                )

        property_candidate = selected.get("property")
        unit_candidate = selected.get("unit")
        if role in {"property", "unit"} and property_candidate and unit_candidate:
            property_dimension = _dimension(
                property_candidate.get("label"),
                property_candidate.get("definition"),
                property_candidate.get("iri"),
            )
            unit_dimension = _dimension(
                unit_candidate.get("label"),
                unit_candidate.get("definition"),
                unit_candidate.get("iri"),
            )
            if (
                property_dimension
                and unit_dimension
                and property_dimension != unit_dimension
            ):
                row_findings.append(
                    _finding(
                        row,
                        "SEM_PROPERTY_UNIT_DIMENSION_MISMATCH",
                        f"The accepted property is {property_dimension}-"
                        f"dimensional, while the accepted unit is "
                        f"{unit_dimension}-dimensional; both require review as "
                        "a pair.",
                    )
                )

        if (
            role == "constraint"
            and _text(candidate.get("iri"))
            == "https://w3id.org/smn/CatchContext"
            and selected_iris.get("variable")
            == "https://w3id.org/smn/CatchAbundance"
            and not _has_constraint_evidence(evidence)
        ):
            row_findings.append(
                _finding(
                    row,
                    "SEM_REDUNDANT_CATCH_CONTEXT",
                    "CatchContext duplicates the accepted CatchAbundance "
                    "framing when no additional constraint evidence is present.",
                )
            )

        if row_findings:
            _downgrade(row, row_findings)
            all_findings.extend(row_findings)
    return rows, all_findings


def _merge_assessments_into_suggestions(
    suggestions: pd.DataFrame,
    assessments: pd.DataFrame,
) -> pd.DataFrame:
    out = suggestions.copy()
    if out.empty:
        for column in [
            "llm_decision",
            "llm_confidence",
            "llm_selected",
            "llm_candidate_rank",
        ]:
            out[column] = pd.Series(dtype="object")
        return out
    assessment_map = {
        _target_key(row): row for _, row in assessments.iterrows()
    }
    out["llm_candidate_rank"] = (
        out.groupby(TARGET_JOIN_COLUMNS, dropna=False).cumcount() + 1
    )
    review_columns = [
        column
        for column in LLM_ASSESSMENT_COLUMNS
        if column.startswith("llm_")
    ]
    for column in review_columns:
        out[column] = out.apply(
            lambda row: assessment_map.get(_target_key(row), {}).get(
                column, pd.NA
            ),
            axis=1,
        )
    out["llm_selected"] = out.apply(
        lambda row: bool(
            not _missing(row.get("llm_selected_candidate_index"))
            and int(row["llm_selected_candidate_index"])
            == int(row["llm_candidate_rank"])
            and _text(row.get("llm_decision")) == "accept"
        ),
        axis=1,
    )
    return out


def assess_semantic_suggestions(
    targets: pd.DataFrame,
    suggestions: pd.DataFrame,
    dictionary: pd.DataFrame,
    *,
    source_policy: dict,
    search_fn: Callable,
    max_per_role: int,
    provider: str,
    model: Optional[str],
    api_key: Optional[str],
    base_url: Optional[str],
    reasoning_effort: Optional[str],
    context_files,
    context_text,
    timeout_seconds: int,
    request_fn,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Assess deterministic candidates while preserving them on any failure."""
    config = resolve_llm_config(
        provider,
        model,
        api_key,
        base_url,
        reasoning_effort,
        timeout_seconds,
        request_fn,
    )
    chunks = load_context_chunks(context_files, context_text)
    rows = []
    handled = set()
    validator_findings = []

    column_targets = targets[
        (targets["target_scope"] == "column")
        & targets["column_name"].notna()
    ]
    bundle_keys = [
        "dataset_id",
        "table_id",
        "column_name",
    ]
    for key, group in column_targets.groupby(bundle_keys, dropna=False):
        roles = set(group["dictionary_role"].astype(str))
        if not roles.intersection(BUNDLE_SLOT_FIELDS):
            continue
        dictionary_row = _dictionary_row(group.iloc[0], dictionary)
        if _text(dictionary_row.get("column_role"), "").lower() != "measurement":
            continue
        context = _relevant_context(chunks, group)
        bundle_rows = _assess_bundle(
            group,
            suggestions,
            context,
            config,
            source_policy,
            dictionary,
        )
        suggestions, bundle_rows = _apply_bundle_retry(
            group,
            suggestions,
            bundle_rows,
            context,
            config,
            source_policy,
            dictionary,
            search_fn,
            max_per_role,
        )
        bundle_rows, bundle_findings = _apply_validators(
            bundle_rows,
            group,
            dictionary,
            suggestions,
            context,
        )
        validator_findings.extend(bundle_findings)
        rows.extend(bundle_rows)
        handled.update(_target_key(target) for _, target in group.iterrows())

    for _, target in targets.iterrows():
        if _target_key(target) in handled:
            continue
        candidates = _candidates_for_target(suggestions, target)
        context = _relevant_context(chunks, pd.DataFrame([target]))
        row = _assess_generic(target, candidates, context, config)
        initial_row = row.copy()
        suggestions, row = _apply_generic_retry(
            target,
            suggestions,
            row,
            context,
            config,
            source_policy,
            search_fn,
            max_per_role,
        )
        _escalate_reject_shortlist(row, initial_row)
        rows.append(row)

    assessments = normalize_assessment_rows(rows)
    assessments.attrs["semantic_validator_findings"] = pd.DataFrame(
        validator_findings,
        columns=VALIDATOR_FINDING_COLUMNS,
    )
    return (
        _merge_assessments_into_suggestions(suggestions, assessments),
        assessments,
    )


__all__ = [
    "AUTO_APPLY_ROLES",
    "LLM_ASSESSMENT_COLUMNS",
    "assess_semantic_suggestions",
    "load_context_chunks",
    "make_source_policy",
    "normalize_assessment_rows",
    "policy_sources",
    "validate_context_files",
]
