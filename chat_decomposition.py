from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence

import pandas as pd

from .semantics import suggest_semantics
from .term_search import find_terms


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _missing(value) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return isinstance(value, str) and not value.strip()


def _json_value(value):
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if _missing(value):
        return None
    if hasattr(value, "item"):
        try:
            return value.item()
        except (TypeError, ValueError):
            pass
    return value


def _session_root(path=None) -> Path:
    if path is not None:
        return Path(path).expanduser()
    return Path.home() / ".local" / "state" / "metasalmonpy" / "chat-decomposition"


def _session_dir(session_id: str, root=None) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", str(session_id)):
        raise ValueError("session_id must contain only letters, numbers, '.', '-', and '_'.")
    return _session_root(root) / str(session_id)


def _save_session(state: dict, transcript: list[dict], session_dir: Path) -> None:
    session_dir.mkdir(parents=True, exist_ok=True)
    payloads = {
        "state.json": state,
        "transcript.json": transcript,
    }
    for name, payload in payloads.items():
        destination = session_dir / name
        temporary = destination.with_suffix(f"{destination.suffix}.tmp")
        temporary.write_text(
            json.dumps(_json_value(payload), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(destination)


def _load_session(session_id: str, root=None) -> tuple[dict, list[dict], Path]:
    directory = _session_dir(session_id, root)
    state_path = directory / "state.json"
    transcript_path = directory / "transcript.json"
    if not state_path.exists() or not transcript_path.exists():
        raise FileNotFoundError(f"No persisted decomposition session found for {session_id!r}.")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
    return state, transcript, directory


def _target_row(
    dictionary: pd.DataFrame,
    column_name: str,
    table_id: Optional[str],
    dataset_id: Optional[str],
) -> dict:
    if not isinstance(dictionary, pd.DataFrame) or dictionary.empty:
        raise ValueError("dict_df must contain at least one dictionary row.")
    if "column_name" not in dictionary:
        raise ValueError("dict_df must contain a column_name column.")

    keep = dictionary["column_name"].astype(str) == str(column_name)
    if table_id is not None:
        if "table_id" not in dictionary:
            raise ValueError("table_id was supplied but dict_df has no table_id column.")
        keep &= dictionary["table_id"].astype(str) == str(table_id)
    if dataset_id is not None:
        if "dataset_id" not in dictionary:
            raise ValueError("dataset_id was supplied but dict_df has no dataset_id column.")
        keep &= dictionary["dataset_id"].astype(str) == str(dataset_id)

    matched = dictionary.loc[keep]
    if matched.empty:
        raise ValueError(
            f"Could not find {column_name!r} with the supplied dataset/table filters."
        )
    if len(matched) > 1:
        raise ValueError(
            "chat_decomposition matched more than one row; pass table_id and/or "
            "dataset_id to disambiguate the column."
        )
    row = matched.iloc[0]
    role = row.get("column_role")
    if not _missing(role) and str(role).strip().lower() != "measurement":
        raise ValueError("chat_decomposition currently supports measurement rows only.")
    return _json_value(row.to_dict())


def _same(value, expected) -> bool:
    return not _missing(value) and str(value) == str(expected)


def _candidate_rows(
    dictionary: pd.DataFrame,
    target: dict,
    df,
    suggestions,
    sources: Optional[Sequence[str]],
    search_fn: Callable,
    max_per_role: int,
) -> list[dict]:
    if suggestions is None:
        reviewed = suggest_semantics(
            df,
            dictionary,
            sources=sources,
            max_per_role=max_per_role,
            search_fn=search_fn,
        )
        suggestions = reviewed.attrs.get("semantic_suggestions", pd.DataFrame())
    candidates = pd.DataFrame(suggestions).copy()
    if candidates.empty:
        return []

    keep = pd.Series(True, index=candidates.index)
    for column in ("dataset_id", "table_id", "column_name"):
        if column in candidates and target.get(column) is not None:
            keep &= candidates[column].apply(
                lambda value: _missing(value) or _same(value, target[column])
            )
    if "dictionary_role" in candidates:
        keep &= candidates["dictionary_role"].apply(
            lambda value: _missing(value) or str(value).lower() == "variable"
        )
    if "target_scope" in candidates:
        keep &= candidates["target_scope"].apply(
            lambda value: _missing(value) or str(value).lower() == "column"
        )
    if "target_sdp_field" in candidates:
        keep &= candidates["target_sdp_field"].apply(
            lambda value: _missing(value) or str(value) == "term_iri"
        )

    candidates = candidates.loc[keep].head(max(0, int(max_per_role))).copy()
    return [_json_value(row) for row in candidates.to_dict(orient="records")]


def _candidate_type(candidate: dict) -> Optional[str]:
    for field in ("term_type", "native_type", "resource_kind", "type_iris"):
        value = candidate.get(field)
        if not _missing(value):
            return str(value)
    return None


def _new_term_request(state: dict, why: str) -> dict:
    target = state["target"]
    label = (
        target.get("column_label")
        or target.get("column_name")
        or "proposed measurement variable"
    )
    description = target.get("column_description")
    definition = (
        str(description)
        if description
        else f"Proposed whole-variable term for {label}."
    )
    return {
        "result": "propose_new_term",
        "dataset_id": target.get("dataset_id"),
        "table_id": target.get("table_id"),
        "column_name": target.get("column_name"),
        "target_sdp_file": "column_dictionary.csv",
        "target_sdp_field": "term_iri",
        "proposed_label": label,
        "proposed_definition": definition,
        "suggested_namespace": None,
        "why_existing_terms_failed": why,
    }


def _candidate_patch(state: dict, index: int, rationale: str) -> dict:
    target = state["target"]
    candidate = state["candidates"][index - 1]
    return {
        "result": "patch",
        "dataset_id": target.get("dataset_id"),
        "table_id": target.get("table_id"),
        "column_name": target.get("column_name"),
        "target_sdp_file": "column_dictionary.csv",
        "target_sdp_field": "term_iri",
        "value": candidate.get("iri"),
        "label": candidate.get("label"),
        "ontology_type": _candidate_type(candidate),
        "selected_candidate_index": index,
        "rationale": rationale,
    }


def _chat_choice(
    state: dict,
    provider: str,
    model: Optional[str],
    api_key: Optional[str],
    base_url: Optional[str],
    timeout_seconds: int,
    request_fn,
) -> Optional[dict]:
    from .llm_review import request_json, resolve_llm_config

    config = resolve_llm_config(
        provider=provider,
        model=model,
        api_key=api_key,
        base_url=base_url,
        reasoning_effort=None,
        timeout_seconds=timeout_seconds,
        request_fn=request_fn,
    )
    messages = [
        {
            "role": "system",
            "content": (
                "Review one measurement-variable ontology shortlist. Preserve each "
                "candidate's native ontology type. Return JSON with decision "
                "(accept, review, or request_new_term), selected_candidate_index "
                "(one-based or null), and rationale."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "target": state["target"],
                    "candidates": state["candidates"],
                    "review_notes": state["review_notes"],
                },
                sort_keys=True,
            ),
        },
    ]
    response = request_json(messages, config)
    return response if isinstance(response, dict) else None


def _recompute(
    state: dict,
    chat_provider: Optional[str],
    chat_model: Optional[str],
    chat_api_key: Optional[str],
    chat_base_url: Optional[str],
    chat_timeout_seconds: int,
    chat_request_fn,
) -> None:
    state["updated_at"] = _now()
    if state.get("force_new_term") or not state["candidates"]:
        state["decision"] = "request_new_term"
        state["proposed_patch"] = _new_term_request(
            state,
            "No acceptable shortlist candidate is currently selected.",
        )
        return

    index = state.get("manual_candidate_index")
    rationale = "Selected from the deterministic shortlist."
    if index is None and (chat_provider is not None or chat_request_fn is not None):
        provider = chat_provider or "openai"
        response = _chat_choice(
            state,
            provider=provider,
            model=chat_model,
            api_key=chat_api_key,
            base_url=chat_base_url,
            timeout_seconds=chat_timeout_seconds,
            request_fn=chat_request_fn,
        )
        if response:
            decision = str(response.get("decision", "review")).strip().lower()
            rationale = str(response.get("rationale") or "Chat-assisted shortlist review.")
            if decision in {"request_new_term", "propose_new_term"}:
                state["decision"] = "request_new_term"
                state["proposed_patch"] = _new_term_request(state, rationale)
                return
            raw_index = response.get("selected_candidate_index")
            try:
                candidate_index = int(raw_index)
            except (TypeError, ValueError):
                candidate_index = 0
            if 1 <= candidate_index <= len(state["candidates"]):
                index = candidate_index

    index = index or 1
    state["decision"] = "accept" if state.get("manual_candidate_index") else "review"
    state["proposed_patch"] = _candidate_patch(state, index, rationale)


def _preview(state: dict) -> str:
    patch = state["proposed_patch"]
    if patch["result"] == "propose_new_term":
        return (
            "Preview: request a new whole-variable term for "
            f"{patch['proposed_label']!r}."
        )
    return (
        f"Preview: candidate {patch['selected_candidate_index']} "
        f"{patch.get('label')!r} -> {patch.get('value')}."
    )


def chat_decomposition(
    dict_df: pd.DataFrame,
    column_name: str,
    df=None,
    table_id: Optional[str] = None,
    dataset_id: Optional[str] = None,
    suggestions: Optional[pd.DataFrame] = None,
    sources: Optional[Sequence[str]] = None,
    search_fn: Callable = find_terms,
    max_per_role: int = 5,
    session_id: Optional[str] = None,
    session_root=None,
    round_size: int = 3,
    chat_provider: Optional[str] = None,
    chat_model: Optional[str] = None,
    chat_api_key: Optional[str] = None,
    chat_base_url: Optional[str] = None,
    chat_timeout_seconds: int = 60,
    chat_request_fn=None,
    commands: Optional[Sequence[str]] = None,
    input_fn: Callable = input,
    output_fn: Callable = print,
) -> dict:
    """
    Start or resume an explicit measurement-variable decomposition review.

    This first interactive slice reviews the whole-variable ``term_iri`` only.
    It persists structured state separately from the transcript and never
    approves or submits an ontology term request automatically.

    Returns
    -------
    dict
        Persisted session state, transcript path, and proposed dictionary patch.
    """
    del round_size  # Reserved for grouped decomposition questions in later slices.

    if session_id is not None:
        state, transcript, directory = _load_session(session_id, session_root)
    else:
        target = _target_row(dict_df, column_name, table_id, dataset_id)
        session_id = uuid.uuid4().hex
        directory = _session_dir(session_id, session_root)
        state = {
            "session_id": session_id,
            "mode": "measurement_variable_review",
            "target": target,
            "candidates": _candidate_rows(
                dict_df,
                target,
                df,
                suggestions,
                sources,
                search_fn,
                max_per_role,
            ),
            "manual_candidate_index": None,
            "force_new_term": False,
            "review_notes": [],
            "decision": "review",
            "approval": {"status": "pending", "approved_at": None},
            "created_at": _now(),
            "updated_at": _now(),
            "proposed_patch": None,
        }
        transcript = []

    _recompute(
        state,
        chat_provider,
        chat_model,
        chat_api_key,
        chat_base_url,
        chat_timeout_seconds,
        chat_request_fn,
    )
    intro = (
        f"metasalmonpy decomposition session {state['session_id']} for "
        f"{state['target'].get('column_name')}.\n{_preview(state)}"
    )
    output_fn(intro)
    transcript.append({"at": _now(), "role": "assistant", "content": intro})
    _save_session(state, transcript, directory)

    command_iter = iter(commands) if commands is not None else None
    while state["approval"]["status"] != "approved":
        if command_iter is None:
            action = input_fn("Next action > ")
        else:
            action = next(command_iter, "/quit")
        action = "" if action is None else str(action).strip()
        transcript.append({"at": _now(), "role": "user", "content": action})

        if action == "/quit":
            _save_session(state, transcript, directory)
            break
        if action in {"/help", ""}:
            output_fn(
                "Actions: /choose n, /newterm, /preview, /approve, /quit."
            )
            continue
        if action == "/preview":
            output_fn(_preview(state))
            continue
        if action.startswith("/choose"):
            pieces = action.split(maxsplit=1)
            try:
                index = int(pieces[1]) if len(pieces) == 2 else 0
            except ValueError:
                index = 0
            if not 1 <= index <= len(state["candidates"]):
                output_fn("Choose a valid candidate index, for example /choose 2.")
                continue
            state["manual_candidate_index"] = index
            state["force_new_term"] = False
        elif action == "/newterm":
            state["force_new_term"] = True
            state["manual_candidate_index"] = None
        elif action == "/approve":
            state["approval"] = {"status": "approved", "approved_at": _now()}
            _save_session(state, transcript, directory)
            break
        elif action.startswith("/"):
            output_fn("Unknown action. Use /help to list available actions.")
            continue
        else:
            state["review_notes"].append(action)

        _recompute(
            state,
            chat_provider,
            chat_model,
            chat_api_key,
            chat_base_url,
            chat_timeout_seconds,
            chat_request_fn,
        )
        output_fn(_preview(state))
        transcript.append(
            {"at": _now(), "role": "assistant", "content": _preview(state)}
        )
        _save_session(state, transcript, directory)

    approved = (
        state["proposed_patch"]
        if state["approval"]["status"] == "approved"
        else None
    )
    return {
        "session_id": state["session_id"],
        "session_dir": directory,
        "approval_status": state["approval"]["status"],
        "proposed_patch": state["proposed_patch"],
        "approved_patch": approved,
        "state": state,
        "transcript": transcript,
    }


__all__ = ["chat_decomposition"]
