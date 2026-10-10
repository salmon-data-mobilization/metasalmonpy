"""The assessment ingester (hub item B-327, the mirror of metasalmon's B-326).

Reads a harness's assessments in the frozen 30-column row, validates each row
against the packet it answers, refuses any selection the packet did not offer,
runs the retry bookkeeping -- a retry is a second harness pass: a usable retry
query is retrieved here and, when it widens the shortlist, a continuation packet
is written and nothing from that target is merged or escalated until the harness
has answered it -- escalates a final ``reject_shortlist`` to
``request_new_term``, runs the bundle validators, merges the result into the
suggestions and persists it, so that
:func:`~metasalmonpy.semantic_llm_assessments` returns it for a package path.

A port of ``R/semantic-review-ingest.R`` function by function; the design is
sections 3 and 4 of metasalmon's
``knowledge/plans/2026-09-25-s16-review-packet-contract.md``. For every shared
conformance case under ``tests/data/semantic_review/v1/`` the record, findings,
suggestions and status this module produces are the ones metasalmon records,
compared after reading as the fixture README defines.

**It never makes a model call, and it reaches the network only through the
``search_fn`` it is given, for a retry.** It never touches the metadata CSVs:
applying a choice stays :func:`~metasalmonpy.review_semantics` ->
:func:`~metasalmonpy.accept_suggestion` -> :func:`~metasalmonpy.apply_sdp_semantics`.
"""

from __future__ import annotations

import math
import os
import re
import warnings
from collections.abc import Mapping
from pathlib import Path
from typing import Callable, Optional, Union

import pandas as pd

from .metadata import R_SPACE_CLASS
from .term_search import find_terms
from .semantic_review_json import (
    read_semantic_review_json,
    semantic_review_canonical_bytes,
    semantic_review_packet_id,
)
from .semantic_review_packet import (
    BUNDLE_SLOT_FIELDS,
    CANDIDATE_FIELDS,
    DECISION_ALIASES,
    DECISION_VOCABULARY,
    DICTIONARY_FIELDS,
    IDENTITY_COLUMNS,
    PACKAGE_OWNED_COLUMNS,
    PACKET_VERSION,
    SEMANTIC_TARGET_COLUMNS,
    _assert_readable,
    _group_key,
    _is_missing,
    _plain,
    _r_character,
    _records,
    _review_file,
    _review_input,
    _slot_id,
    _target_address,
    _text_scalar,
    _trim_string,
    assemble_packet,
    candidate_object,
)

__all__ = ["SemanticReviewError", "ingest_semantic_assessments"]

_R_TRIM = " \t\r\n"


# -----------------------------------------------------------------------------
# File-level errors
# -----------------------------------------------------------------------------

#: The stable codes a file-level problem aborts with, in the order the
#: execplan's section 3.3 lists them.
FILE_ERROR_CODES = (
    "packet_version",
    "packet_integrity",
    "packet_unbound",
    "packet_mismatch",
    "header",
    "unknown_target",
    "duplicate_target",
    "provenance",
    "no_pass_2",
)


class SemanticReviewError(ValueError):
    """A file-level problem with a review session: the ingest aborts and writes nothing.

    ``code`` is one of :data:`FILE_ERROR_CODES`, the stable code both packages
    raise for the same problem (R carries it as the condition's ``code`` field
    and as the class ``metasalmon_semantic_review_<code>``).
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _abort(code: str, message: str):
    raise SemanticReviewError(code, message)


# -----------------------------------------------------------------------------
# Small readers
# -----------------------------------------------------------------------------


def _non_empty_string(value) -> Optional[str]:
    """``.ms_llm_non_empty_string()``: the first scalar, trimmed as R trims, or ``None``."""
    from .llm_review import _non_empty_string as non_empty_string

    value = _plain(value)
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and not math.isnan(value):
        value = _r_character(value)
    return non_empty_string(value)


_OPTIONAL_NOTE_NONE = frozenset({"false", "none", "n/a", "na", "null", "nil"})


def _optional_note(value) -> Optional[str]:
    """``.ms_llm_optional_note()``: a note, or ``None`` for a blank or a placeholder word."""
    text = _non_empty_string(value)
    if text is None or text.lower() in _OPTIONAL_NOTE_NONE:
        return None
    return text


def _append_note(rationale: Optional[str], note: str) -> Optional[str]:
    """``.ms_llm_append_note()``: the non-empty parts joined with one space."""
    parts = [part for part in (rationale, note) if part]
    return " ".join(parts) if parts else None


def _scalar_numeric(value) -> Optional[float]:
    """``.ms_llm_scalar_numeric()``: ``as.numeric()`` of a text cell, ``None`` for NA."""
    value = _plain(value)
    if _is_missing(value):
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip(_R_TRIM)
    if not re.fullmatch(r"[+-]?(?:\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?|Inf|inf|NaN|nan)", text):
        if re.fullmatch(r"[+-]?0[xX][0-9a-fA-F]+", text):
            return float(int(text, 16))
        return None
    number = float(text)
    return None if math.isnan(number) else number


def _cell(value, kind: str = "character"):
    """``.ms_semantic_review_cell()``: a JSON scalar back to a typed cell, ``None`` for null."""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    value = _plain(value)
    if _is_missing(value):
        return None
    if kind == "character":
        return _r_character(value)
    if kind == "numeric":
        return _scalar_numeric(value)
    if kind == "integer":
        number = _scalar_numeric(value)
        return None if number is None else int(math.trunc(number))
    if kind == "logical":
        if isinstance(value, bool):
            return value
        text = str(value).strip(_R_TRIM)
        if text in ("TRUE", "true", "T", "True"):
            return True
        if text in ("FALSE", "false", "F", "False"):
            return False
        return None
    raise ValueError(f"Unknown cell kind {kind!r}.")


def _sidecar_path(csv_path: Union[str, Path]) -> Path:
    """``<csv>.packet-id``: where a harness names the packet it judged."""
    return Path(f"{csv_path}.packet-id")


def _read_sidecar(csv_path) -> Optional[str]:
    """``.ms_semantic_review_read_sidecar()``: the sidecar's first non-blank line, trimmed.

    A leading byte-order mark is stripped. Absent or blank is ``None``.
    """
    if csv_path is None:
        return None
    path = _sidecar_path(csv_path)
    if not path.is_file():
        return None
    from .llm_review import _r_read_lines

    for line in _r_read_lines(path):
        if line.startswith("﻿"):
            line = line[1:]
        line = line.strip(_R_TRIM)
        if line:
            return line
    return None


def _check_binding(current_id: str, argument_id: Optional[str], sidecar_id: Optional[str]) -> None:
    """``.ms_semantic_review_check_binding()``: the file must name the packet being ingested.

    Named by the sidecar or by the caller (the argument wins); without this a
    stale file could be recorded under a newer packet's provenance.
    """
    named = argument_id if argument_id is not None else sidecar_id
    if named is None or not str(named):
        _abort(
            "packet_unbound",
            "Nothing names the packet these assessments were made against. The harness "
            "writes the packet's packet_id in a one-line sidecar beside its CSV, "
            "<csv>.packet-id, or the caller passes packet_id.",
        )
    if str(named) != current_id:
        _abort(
            "packet_mismatch",
            "The assessments were made against a different packet from the one being "
            "ingested. The file is stale: write a new packet and have the harness judge "
            "that one, or ingest the packet the file names.",
        )


#: The harness-owned free-text columns: every value is redacted at capture,
#: before it reaches the record (PARITY.md row 37).
REDACTED_COLUMNS = (
    "llm_provider",
    "llm_model",
    "llm_rationale",
    "llm_missing_context",
    "llm_bundle_summary",
    "llm_retry_query",
    "llm_new_term_label",
    "llm_new_term_definition",
    "llm_new_term_namespace",
    "llm_error",
)


def _scrub_harness_text(rows: list) -> list:
    """``.ms_semantic_review_redact()``: every harness free-text cell through
    :func:`~metasalmonpy.text_safety.redact_secrets`, in place.

    Named without "redact" because it is not a redactor: the package has one,
    and ``tests/test_text_safety.py`` holds it to one.

    Returns the names of the columns in which anything changed.
    """
    from .text_safety import redact_secrets

    changed = []
    for column in REDACTED_COLUMNS:
        column_changed = False
        for row in rows:
            value = row.get(column)
            if value is None:
                continue
            redacted = redact_secrets(value)
            if redacted != value:
                row[column] = redacted
                column_changed = True
        if column_changed:
            changed.append(column)
    return changed


# -----------------------------------------------------------------------------
# Reading the packet back
# -----------------------------------------------------------------------------


def _read_packet(packet, review_dir: Path) -> dict:
    """``.ms_semantic_review_read_packet()``: the packet named, or the latest pass in ``review/``."""
    if packet is None:
        pass_1 = _review_file(review_dir, "packet", 1)
        pass_2 = _review_file(review_dir, "packet", 2)
        _assert_readable(review_dir, [pass_1, pass_2])
        path = pass_2 if pass_2.exists() else pass_1
        if not path.exists():
            raise FileNotFoundError(
                f"No semantic review packet in {review_dir}. Write one with "
                "write_semantic_review_packet() first."
            )
        return {"packet": read_semantic_review_json(path), "path": path}
    if isinstance(packet, (str, os.PathLike)):
        path = Path(packet)
        if not path.exists():
            raise FileNotFoundError(f"Packet file {path} does not exist.")
        return {"packet": read_semantic_review_json(path), "path": path}
    if isinstance(packet, Mapping):
        return {"packet": dict(packet), "path": None}
    raise TypeError("packet must be a packet file path or a parsed packet.")


def _check_packet(packet: Mapping, expected_id: Optional[str] = None) -> str:
    """``.ms_semantic_review_check_packet()``: version, integrity, then the expected id."""
    version = _text_scalar(packet.get("packet_version"))
    if version != PACKET_VERSION:
        _abort("packet_version", f"The packet's version is not {PACKET_VERSION!r}.")
    stored = _text_scalar(packet.get("packet_id"))
    recomputed = semantic_review_packet_id(packet)
    if stored is None or stored != recomputed:
        _abort(
            "packet_integrity",
            "The packet's contents no longer match its packet_id. A packet is never "
            "edited; write a new one with write_semantic_review_packet().",
        )
    if expected_id is not None and str(expected_id) != recomputed:
        _abort("packet_mismatch", "The packet's packet_id is not the one this ingest expected.")
    return recomputed


def _target_from_slot(slot: Mapping) -> dict:
    """``.ms_semantic_review_target_from_slot()``: the 19 target columns, the slot id and current value."""
    target = slot.get("target") or {}
    out = {column: _cell(target.get(column)) for column in SEMANTIC_TARGET_COLUMNS}
    out["slot_id"] = _cell(slot.get("slot_id"))
    out["current_value"] = _cell(slot.get("current_value"))
    return out


_CANDIDATE_FIELD_KINDS = {
    "role_hint_bonus": "numeric",
    "retrieval_pass": "integer",
    "alignment_only": "logical",
    "role_collision": "logical",
}


def _candidates_from_slot(slot: Mapping, target: Mapping) -> list:
    """``.ms_semantic_review_candidates_from_slot()``: the candidate rows the package held.

    The target's 19 columns stamped on every row, the named fields, ``score``
    from ``lexical_score``, and every ``extra`` member as its own column.
    """
    rows = []
    for candidate in slot.get("candidates") or []:
        row = {column: target.get(column) for column in SEMANTIC_TARGET_COLUMNS}
        for field in CANDIDATE_FIELDS:
            row[field] = _cell(candidate.get(field), _CANDIDATE_FIELD_KINDS.get(field, "character"))
        row["score"] = _cell(candidate.get("lexical_score"), "numeric")
        extra = candidate.get("extra")
        if isinstance(extra, Mapping):
            for name, value in extra.items():
                row[name] = _plain(value[0] if isinstance(value, list) and value else value)
                if isinstance(value, list) and not value:
                    row[name] = None
        rows.append(row)
    return rows


def _context_from_unit(unit: Mapping) -> pd.DataFrame:
    """``.ms_semantic_review_context_from_unit()``: the unit's excerpts as a context pool."""
    excerpts = unit.get("context_excerpts") or []
    return pd.DataFrame(
        {
            "source": [_cell(item.get("source")) for item in excerpts],
            "chunk_id": [_cell(item.get("chunk_id")) for item in excerpts],
            "text": [_cell(item.get("excerpt")) for item in excerpts],
            "context_score": [_cell(item.get("context_score"), "numeric") for item in excerpts],
        },
        columns=["source", "chunk_id", "text", "context_score"],
    )


def _dict_row_from_unit(unit: Mapping) -> dict:
    """``.ms_semantic_review_dict_row_from_unit()``: the dictionary row the validators read."""
    dictionary = unit.get("dictionary") or {}
    row = {field: _cell(dictionary.get(field)) for field in DICTIONARY_FIELDS}
    current = unit.get("current_slots") or {}
    for role, field in BUNDLE_SLOT_FIELDS.items():
        row[field] = _cell(current.get(role))
    return row


def _assessment_from_object(value: Optional[Mapping]) -> Optional[dict]:
    """``.ms_semantic_review_assessment_row_from_object()``: a persisted row back to a typed row."""
    from .llm_review import LLM_ASSESSMENT_COLUMNS

    if not value:
        return None
    kinds = {
        "llm_confidence": "numeric",
        "llm_selected_candidate_index": "integer",
        "llm_exploration_used": "logical",
        "llm_exploration_candidate_gain": "integer",
    }
    return {column: _cell(value.get(column), kinds.get(column, "character")) for column in LLM_ASSESSMENT_COLUMNS}


def _identity_key(row: Mapping) -> str:
    """``.ms_semantic_review_identity_key()``: the nine identity columns, trimmed, empty equal to null."""
    parts = []
    for column in IDENTITY_COLUMNS:
        text = _r_character(row.get(column))
        parts.append("" if text is None else text.strip(_R_TRIM))
    return "\r".join(parts)


def _slots(packet: Mapping) -> list:
    """``.ms_semantic_review_slots()``: every slot of the packet, in packet order, with its unit."""
    slots = []
    for unit_index, unit in enumerate(packet.get("units") or []):
        for slot_index, slot in enumerate(unit.get("slots") or []):
            target = _target_from_slot(slot)
            slots.append(
                {
                    "unit_index": unit_index,
                    "slot_index": slot_index,
                    "unit_kind": _cell(unit.get("unit_kind")),
                    "unit_key": _cell(unit.get("unit_key")),
                    "key": _identity_key(target),
                    "role": _cell(slot.get("dictionary_role")),
                    "target": target,
                    "candidates": _candidates_from_slot(slot, target),
                    "reassess": slot.get("reassess") is True,
                    "previous_assessment": _assessment_from_object(slot.get("previous_assessment")),
                }
            )
    return slots


# -----------------------------------------------------------------------------
# Reading the assessment file
# -----------------------------------------------------------------------------


def _read_assessments(assessments, pass_number: int, review_dir: Path) -> dict:
    """``.ms_semantic_review_read_assessments()``: every cell text, the empty field missing.

    A CSV is read with the package's own metadata reader (all character, a
    leading byte-order mark dropped, whitespace trimmed); a data frame is read
    cell by cell as text.
    """
    from .metadata import read_sdp_csv

    default_path = _review_file(review_dir, "assessments", pass_number)
    if assessments is None:
        _assert_readable(review_dir, [default_path, _sidecar_path(default_path)])
        if not default_path.exists():
            raise FileNotFoundError(
                f"No assessment file at {default_path}. The harness writes it there; pass "
                "assessments= to read another path or a data frame."
            )
        assessments = default_path
    if isinstance(assessments, pd.DataFrame):
        columns = [str(column) for column in assessments.columns]
        rows = []
        for record in _records(assessments):
            row = {}
            for column, value in zip(columns, record.values()):
                text = _r_character(value)
                text = None if text is None else text.strip(_R_TRIM)
                row[column] = text if text else None
            rows.append(row)
        return {"columns": columns, "rows": rows, "path": None}
    if not isinstance(assessments, (str, os.PathLike)):
        raise TypeError("assessments must be a CSV path or a data frame.")
    path = Path(assessments)
    if not path.exists():
        raise FileNotFoundError(f"Assessment file {path} does not exist.")
    try:
        frame = read_sdp_csv(path)
    except pd.errors.EmptyDataError:
        frame = pd.DataFrame()
    columns = [str(column) for column in frame.columns]
    rows = [
        {column: (value if isinstance(value, str) and value != "" else None) for column, value in zip(columns, record.values())}
        for record in _records(frame)
    ]
    return {"columns": columns, "rows": rows, "path": path}


def _check_header(columns: list) -> None:
    """The header must be the 30 assessment columns in order."""
    from .llm_review import LLM_ASSESSMENT_COLUMNS

    if list(columns) != list(LLM_ASSESSMENT_COLUMNS):
        _abort(
            "header",
            "The assessment file's header is not the thirty assessment columns in order. "
            "The packet's output.columns member lists them; write the file with a CSV "
            "library from that list.",
        )


# -----------------------------------------------------------------------------
# Rows
# -----------------------------------------------------------------------------


def _empty_assessment(target: Mapping, config: Mapping, error: Optional[str] = None) -> dict:
    """``.ms_llm_review_empty_assessment()``: an unjudged row carrying the target's identity."""
    from .llm_review import LLM_ASSESSMENT_COLUMNS

    row = {column: None for column in LLM_ASSESSMENT_COLUMNS}
    for column in IDENTITY_COLUMNS:
        row[column] = target.get(column)
    row["llm_provider"] = config.get("provider")
    row["llm_model"] = config.get("model")
    row["llm_exploration_used"] = False
    row["llm_exploration_candidate_gain"] = 0
    row["llm_error"] = _non_empty_string(error)
    return row


def _success_assessment(target: Mapping, candidates: list, context: pd.DataFrame, config: Mapping,
                        validated: Mapping) -> dict:
    """``.ms_llm_review_success_assessment()``: a judged row; the IRI and label come from the packet."""
    row = _empty_assessment(target, config)
    index = validated["selected_candidate_index"]
    sources = []
    if context is not None and len(context):
        for source in context["source"]:
            if source not in sources:
                sources.append(source)
    row.update(
        {
            "llm_decision": validated["decision"],
            "llm_confidence": validated["confidence"],
            "llm_selected_candidate_index": index,
            "llm_selected_iri": candidates[index - 1].get("iri") if index is not None else None,
            "llm_selected_label": candidates[index - 1].get("label") if index is not None else None,
            "llm_rationale": validated["rationale"],
            "llm_missing_context": validated["missing_context"],
            "llm_bundle_summary": validated["bundle_summary"],
            "llm_retry_query": validated["retry_query"],
            "llm_new_term_label": validated["suggested_label"],
            "llm_new_term_definition": validated["suggested_definition"],
            "llm_new_term_namespace": validated["suggested_namespace"],
            "llm_context_sources": "; ".join(sources) if sources else None,
            "llm_error": None,
        }
    )
    return row


def _error_row(target: Mapping, config: Mapping, error: str) -> dict:
    """``.ms_semantic_review_error_row()``: an error row, its text on one line and redacted.

    The text is this package's own wording where it is not R's; the fixture
    README says an error row is compared for presence, not words.
    """
    from .text_safety import redact_secrets

    text = re.sub(f"[{R_SPACE_CLASS}]+", " ", str(error)).strip(_R_TRIM)
    return _empty_assessment(target, config, error=redact_secrets(text))


def _read_decision(value) -> Optional[str]:
    """``.ms_semantic_review_read_decision()``: a harness's decision as the validator reads it.

    Trimmed, lowercased and with the alias resolved, so ``propose_new_term`` is
    the ``request_new_term`` it names. Row validation and the downgrade count
    both read the decision through this, so the two cannot disagree about what
    the harness wrote (hub item B-425, the mirror of metasalmon's B-424: the
    count there exempted the alias with a named subset that never compared
    equal, and this package reproduced that until both were fixed).
    """
    decision = _non_empty_string(value)
    decision = decision.lower() if decision is not None else None
    if decision is not None:
        decision = DECISION_ALIASES.get(decision, decision)
    return decision


def _note(row: dict, note: str) -> dict:
    """``.ms_semantic_review_note()``: append a package note to the rationale."""
    row = dict(row)
    row["llm_rationale"] = _append_note(_non_empty_string(row.get("llm_rationale")), note)
    return row


class _AssessmentRefused(Exception):
    """``.ms_validate_llm_assessment()``'s abort: the row becomes an error row."""


def _validate_assessment(result: Mapping, candidates: list) -> dict:
    """``.ms_validate_llm_assessment()`` over a harness row's text cells.

    An unknown decision or a confidence outside [0, 1] refuses the row, with
    no clamping; a non-accept decision has its index cleared *before* any
    range check (so a rejection carrying a stray index is not downgraded and
    its gap is escalated); an accept without an index, or with one out of
    range, is downgraded to review with a note; a fractional index is refused,
    never truncated; and a ``retry_search`` without a query is downgraded.
    """
    decision = _non_empty_string(result.get("decision"))
    decision = decision.lower() if decision is not None else None
    if decision is not None:
        decision = DECISION_ALIASES.get(decision, decision)
    if decision is None or decision not in DECISION_VOCABULARY:
        raise _AssessmentRefused(
            "LLM assessment must return decision = accept, review, retry_search, "
            "request_new_term, or reject_shortlist."
        )
    selected = result.get("selected_candidate_index")
    selected_index = None if selected is None or selected == "" else _scalar_numeric(selected)

    confidence = _scalar_numeric(result.get("confidence"))
    if confidence is None or confidence < 0 or confidence > 1:
        raise _AssessmentRefused("LLM assessment confidence must be numeric and between 0 and 1.")

    rationale = _non_empty_string(result.get("rationale"))
    retry_query = _optional_note(result.get("retry_query"))
    bundle_summary = _optional_note(result.get("bundle_summary"))
    suggested_label = _optional_note(result.get("suggested_label"))
    suggested_definition = _optional_note(result.get("suggested_definition"))
    suggested_namespace = _optional_note(result.get("suggested_namespace"))

    if decision == "accept" and selected_index is None:
        decision = "review"
        rationale = _append_note(
            rationale, "Model returned accept without selecting a candidate; downgraded to review."
        )
    if decision != "accept":
        selected_index = None
    elif math.isfinite(selected_index) and selected_index != math.trunc(selected_index):
        raise _AssessmentRefused(
            "LLM assessment selected_candidate_index must be a whole number, not "
            f"{_format_index(selected_index)}."
        )
    elif not math.isfinite(selected_index) or selected_index < 1 or selected_index > len(candidates):
        decision = "review"
        selected_index = None
        rationale = _append_note(
            rationale, "Model returned an out-of-range candidate index; downgraded to review."
        )
    else:
        selected_index = int(selected_index)
    if decision == "retry_search" and retry_query is None:
        decision = "review"
        rationale = _append_note(
            rationale,
            "Model requested retry_search without providing a retry query; downgraded to review.",
        )
    return {
        "decision": decision,
        "selected_candidate_index": selected_index,
        "confidence": confidence,
        "rationale": rationale,
        "missing_context": _optional_note(result.get("missing_context")),
        "bundle_summary": bundle_summary,
        "retry_query": retry_query,
        "suggested_label": suggested_label,
        "suggested_definition": suggested_definition,
        "suggested_namespace": suggested_namespace,
    }


def _format_index(value: float) -> str:
    """``format(x, digits = 15)`` for the fractional-index message."""
    text = f"{value:.15g}"
    return text


def _validate_row(row: Mapping, slot: Mapping, config: Mapping, context: pd.DataFrame) -> dict:
    """``.ms_semantic_review_validate_row()``: one harness row against one packet slot.

    In the order section 3.3 of the execplan fixes. Returns an error row, a
    downgraded row with a note, or the validated answer.
    """
    target = {column: slot["target"].get(column) for column in SEMANTIC_TARGET_COLUMNS}
    candidates = slot["candidates"]

    def text(column):
        return _non_empty_string(row.get(column))

    # 1. A harness-declared error is an error row, redacted.
    declared = text("llm_error")
    if declared is not None:
        return _error_row(target, config, declared)

    # 2. The decision, lowercased and trimmed, with the alias read.
    decision = _read_decision(row.get("llm_decision"))
    echo = text("llm_selected_iri")
    candidate_iris = [
        ("" if iri is None else iri.strip(_R_TRIM))
        for iri in (_r_character(candidate.get("iri")) for candidate in candidates)
    ]

    # 5. An accept whose echoed IRI is not a candidate for the target is an
    # error: an IRI absent from the packet is never applied.
    if decision == "accept" and echo is not None and echo not in [iri for iri in candidate_iris if iri]:
        return _error_row(
            target, config,
            "The echoed IRI is not a candidate the packet offered for this target: " + echo,
        )

    # 2-4, 6-8 and 12 are the shared validator.
    try:
        validated = _validate_assessment(
            {
                "decision": text("llm_decision"),
                "selected_candidate_index": text("llm_selected_candidate_index"),
                "confidence": text("llm_confidence"),
                "rationale": text("llm_rationale"),
                "missing_context": text("llm_missing_context"),
                "bundle_summary": text("llm_bundle_summary"),
                "retry_query": text("llm_retry_query"),
                "suggested_label": text("llm_new_term_label"),
                "suggested_definition": text("llm_new_term_definition"),
                "suggested_namespace": text("llm_new_term_namespace"),
            },
            candidates,
        )
    except _AssessmentRefused as refused:
        return _error_row(target, config, str(refused))

    if validated["decision"] == "accept":
        index = validated["selected_candidate_index"]
        if echo is None:
            # 9. An accept with no echoed IRI downgrades to review.
            validated["decision"] = "review"
            validated["selected_candidate_index"] = None
            validated["rationale"] = _append_note(
                validated["rationale"],
                "Harness returned accept without echoing the candidate's IRI; downgraded to review.",
            )
        elif echo != candidate_iris[index - 1]:
            # 10. An echo naming a different candidate from the index is an error.
            return _error_row(
                target, config,
                f"The echoed IRI names a different candidate from index {index}: {echo} is not "
                f"{candidate_iris[index - 1]}",
            )
    if validated["decision"] == "accept" and (
        validated["suggested_label"] is not None
        or validated["suggested_definition"] is not None
        or validated["suggested_namespace"] is not None
    ):
        # 11. An accept with any new-term field is an error.
        return _error_row(target, config, "An accept must not carry a new-term label, definition or namespace.")

    return _success_assessment(target, candidates, context, config, validated)


def _row_config(row: Mapping, provider, model) -> dict:
    """``.ms_semantic_review_row_config()``: the harness's provider and model, unless overridden."""
    return {
        "provider": provider["value"] if provider["given"] else _non_empty_string(row.get("llm_provider")),
        "model": model["value"] if model["given"] else _non_empty_string(row.get("llm_model")),
    }


# -----------------------------------------------------------------------------
# Retry bookkeeping and the second pass
# -----------------------------------------------------------------------------


def _source_policy_from_packet(packet: Mapping) -> dict:
    """``.ms_semantic_review_source_policy_from_packet()``."""
    from .llm_review import make_source_policy

    policy = (((packet.get("pins") or {}).get("retrieval") or {}).get("source_policy")) or {}
    mode = _cell(policy.get("mode"))
    allowlist = [str(value) for value in (policy.get("explicit_allowlist") or []) if value is not None]
    if mode == "explicit":
        built = make_source_policy(allowlist)
        built["given"] = allowlist
        return built
    return make_source_policy(None)


def _top_n_from_packet(packet: Mapping) -> int:
    """``.ms_semantic_review_top_n_from_packet()``: the packet's depth, 5 when unusable."""
    top_n = _cell((((packet.get("pins") or {}).get("retrieval") or {}).get("top_n")), "integer")
    return 5 if top_n is None or top_n < 1 else top_n


def _classify_retry(row: dict, target: Mapping) -> tuple:
    """``.ms_semantic_review_classify_retry()``: a duplicate keeps its reason, an
    identifier-like query gets ``identifier_like_query`` (decision 12 of the
    execplan: there is no model call to replace it), and a usable query is
    returned for retrieval."""
    from .llm_review import _classify_retry_query

    query = _non_empty_string(row.get("llm_retry_query"))
    classification = _classify_retry_query(query, target.get("search_query"))
    disposition = classification["disposition"]
    if disposition == "duplicate_original_query":
        row = dict(row)
        row["llm_retry_query_rejection_reason"] = classification["rejection_reason"]
        row["llm_rationale"] = _append_note(
            _non_empty_string(row.get("llm_rationale")),
            "Retry query matched the original query after case and whitespace normalization; "
            "the retry was not issued.",
        )
        return row, None
    if disposition == "identifier_like":
        row = dict(row)
        row["llm_retry_query_rejection_reason"] = "identifier_like_query"
        row = _note(
            row,
            "Retry query looks like an identifier rather than a lexical query; the retry was not issued.",
        )
        return row, None
    if disposition == "use_query":
        return row, classification["query"]
    return row, None


def _retrieve_retry(slot: Mapping, query: str, source_policy: dict, top_n: int, search_fn: Callable) -> dict:
    """``.ms_semantic_review_retrieve_retry()``: retrieve a usable retry query and merge it.

    At depth ``top_n`` under the packet's source policy, merged into the slot's
    shortlist with metasalmon's merge. Returns the merged rows and the number of
    candidates gained.
    """
    from .semantics import (
        _merge_semantic_target_candidates,
        _retrieve_semantic_target_candidates,
        _semantic_candidate_identity,
    )

    target = {column: slot["target"].get(column) for column in SEMANTIC_TARGET_COLUMNS}
    extra = _retrieve_semantic_target_candidates(
        target, source_policy, top_n, search_fn, query=query, retrieval_pass=2
    )
    existing = pd.DataFrame(slot["candidates"])
    merged = _merge_semantic_target_candidates(existing, extra, top_n)
    role = _trim_string(target.get("dictionary_role"))
    existing_ids = set(_semantic_candidate_identity(existing, role=role))
    merged_ids = _semantic_candidate_identity(merged, role=role)
    return {
        "candidates": _records(merged),
        "gain": sum(1 for identity in merged_ids if identity not in existing_ids),
    }


def _add_exploration(row: dict, used: bool, queries, gain) -> dict:
    """``.ms_llm_add_exploration_metadata()``."""
    row = dict(row)
    row["llm_exploration_used"] = bool(used)
    if isinstance(queries, str):
        queries = [queries]
    queries = [query for query in (queries or []) if query is not None]
    row["llm_exploration_queries"] = " | ".join(queries) if queries else None
    row["llm_exploration_candidate_gain"] = 0 if gain is None else int(gain)
    return row


def _escalate(row: dict) -> dict:
    """``.ms_llm_escalate_unresolved_rejection()`` with no earlier answer: a final
    ``reject_shortlist`` becomes ``request_new_term`` with the selection
    cleared, so the likely ontology gap is surfaced."""
    if _non_empty_string(row.get("llm_decision")) != "reject_shortlist":
        return row
    row = dict(row)
    post_rationale = _non_empty_string(row.get("llm_rationale"))
    row["llm_decision"] = "request_new_term"
    row["llm_selected_candidate_index"] = None
    row["llm_selected_iri"] = None
    row["llm_selected_label"] = None
    row["llm_escalated_from"] = "reject_shortlist"
    parts = [post_rationale] if post_rationale is not None else []
    parts.append(
        "Shortlist rejected and exploration found no acceptable candidate; escalated to "
        "request_new_term so the likely ontology gap is surfaced."
    )
    row["llm_rationale"] = " ".join(parts)
    return row


def _assessment_object(row: Mapping) -> dict:
    """``.ms_semantic_review_assessment_object()``: a row as the pass-2 packet carries it."""
    from .llm_review import LLM_ASSESSMENT_COLUMNS

    frame = _assessment_frame([row])
    record = _records(frame)[0]
    out = {}
    for column in LLM_ASSESSMENT_COLUMNS:
        value = _plain(record.get(column))
        out[column] = None if _is_missing(value) else value
    return out


def _findings_objects(findings: pd.DataFrame) -> list:
    """``.ms_semantic_review_findings_objects()``."""
    return [
        {column: _r_character(record.get(column)) for column in FINDINGS_COLUMNS}
        for record in _records(findings)
    ]


def _findings_unit_key(findings: pd.DataFrame) -> list:
    """``.ms_semantic_review_findings_unit_key()``: the bundle a finding belongs to."""
    return [
        "bundle:" + "/".join(
            _r_character(record.get(column)) or "" for column in ("dataset_id", "table_id", "column_name")
        )
        for record in _records(findings)
    ]


def _pass_2_packet(packet: Mapping, slots: list, rows: list, pending: list, findings: pd.DataFrame) -> dict:
    """``.ms_semantic_review_pass_2_packet()``: the continuation packet.

    The same schema with ``pass: 2`` and the parent's id. It holds only the
    units with a slot that gained candidates; a bundle marks the roles to
    reassess; every slot carries its full pass-1 row and every bundle its
    pass-1 findings, so pass 2 can be re-ingested with the same result. Context
    excerpts are reused verbatim.
    """
    pending_units = sorted({slots[item["slot"]]["unit_index"] for item in pending})
    finding_keys = _findings_unit_key(findings)
    units = []
    for unit_index in pending_units:
        unit = dict(packet["units"][unit_index])
        new_slots = []
        for slot_index, slot_object in enumerate(unit.get("slots") or []):
            slot_object = dict(slot_object)
            position = next(
                number
                for number, slot in enumerate(slots)
                if slot["unit_index"] == unit_index and slot["slot_index"] == slot_index
            )
            gained = [item for item in pending if item["slot"] == position]
            slot_object["reassess"] = bool(gained)
            if gained:
                slot_object["candidates"] = [
                    candidate_object(row, index)
                    for index, row in enumerate(gained[0]["candidates"], start=1)
                ]
            slot_object["previous_assessment"] = _assessment_object(rows[position])
            new_slots.append(slot_object)
        unit["slots"] = new_slots
        if _cell(unit.get("unit_kind")) == "bundle":
            unit_key = _cell(unit.get("unit_key"))
            unit_findings = findings.iloc[[i for i, key in enumerate(finding_keys) if key == unit_key]]
            unit["previous_findings"] = _findings_objects(unit_findings)
        units.append(unit)
    return assemble_packet(
        pass_number=2,
        parent_packet_id=_text_scalar(packet.get("packet_id")),
        pins=packet.get("pins"),
        context=packet.get("context"),
        units=units,
        function="ingest_semantic_assessments",
    )


# -----------------------------------------------------------------------------
# Validators
# -----------------------------------------------------------------------------

#: ``.ms_semantic_review_findings_cols()``: the findings row.
FINDINGS_COLUMNS = (
    "dataset_id",
    "table_id",
    "column_name",
    "code",
    "severity",
    "role",
    "before_decision",
    "after_decision",
    "message",
)


def _empty_findings() -> pd.DataFrame:
    return pd.DataFrame({column: pd.Series(dtype="object") for column in FINDINGS_COLUMNS})


def _validate_bundle(unit: Mapping, unit_slots: list, unit_rows: list) -> dict:
    """``.ms_semantic_review_validate_bundle()``: the bundle validators, rebuilt from the packet.

    Runs :func:`~metasalmonpy.llm_review._apply_validators` -- metasalmon's
    ``.ms_semantic_apply_bundle_validators()`` since hub B-360 -- on the rows
    given, one per slot in slot order, against the unit's own dictionary row,
    current slots, candidates and excerpts.
    """
    from .llm_review import _apply_validators

    targets = pd.DataFrame(
        [{column: slot["target"].get(column) for column in SEMANTIC_TARGET_COLUMNS} for slot in unit_slots],
        columns=list(SEMANTIC_TARGET_COLUMNS),
    )
    candidate_rows = [row for slot in unit_slots for row in slot["candidates"]]
    suggestions = pd.DataFrame(candidate_rows) if candidate_rows else pd.DataFrame(columns=list(SEMANTIC_TARGET_COLUMNS))
    dictionary = pd.DataFrame([_dict_row_from_unit(unit)])
    rows = [dict(row) for row in unit_rows]
    validated_rows, findings = _apply_validators(rows, targets, dictionary, suggestions, _context_from_unit(unit))
    frame = pd.DataFrame(findings, columns=list(FINDINGS_COLUMNS)) if findings else _empty_findings()
    return {"rows": validated_rows, "findings": frame}


def _union_findings(*frames) -> pd.DataFrame:
    """``.ms_semantic_review_union_findings()``: findings only grow; the first of a kind stands."""
    kept = [frame for frame in frames if frame is not None and len(frame)]
    if not kept:
        return _empty_findings()
    combined = pd.concat([frame[list(FINDINGS_COLUMNS)] for frame in kept], ignore_index=True)
    keys = [
        "\r".join(
            "NA" if text is None else text
            for text in (_r_character(record.get(column)) for column in ("dataset_id", "table_id", "column_name", "code", "role"))
        )
        for record in _records(combined)
    ]
    seen = set()
    keep = []
    for key in keys:
        keep.append(key not in seen)
        seen.add(key)
    return combined[keep].reset_index(drop=True)


# -----------------------------------------------------------------------------
# Frames and persistence
# -----------------------------------------------------------------------------


#: The four typed columns of the assessment row; the other 26 are text.
_TYPED_ASSESSMENT_COLUMNS = frozenset(
    {"llm_confidence", "llm_selected_candidate_index", "llm_exploration_used", "llm_exploration_candidate_gain"}
)


def _assessment_frame(rows: list) -> pd.DataFrame:
    """Rows as the stable 30-column record (``.ms_llm_normalize_assessment_rows()``).

    The four typed columns are typed by
    :func:`~metasalmonpy.llm_review.normalize_assessment_rows`; the 26 text
    columns are held as ``object`` with ``None`` for a missing value, whatever
    the rows hold, so an error row, a downgraded row, an escalated row and a
    success row carry the same dtypes (pandas would otherwise infer a string
    dtype for a column that happens to hold only text and ``object`` for one
    that holds only ``None``).
    """
    from .llm_review import normalize_assessment_rows

    frame = normalize_assessment_rows(list(rows))
    for column in frame.columns:
        if column in _TYPED_ASSESSMENT_COLUMNS:
            continue
        frame[column] = pd.Series(
            [None if _is_missing(value) else str(value) for value in frame[column].tolist()],
            index=frame.index,
            dtype="object",
        )
    return frame


def _character_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """``.ms_semantic_review_character_frame()``: one value, one rendering.

    Numbers through the shared number-token formatter, logicals as
    ``TRUE``/``FALSE``, integers as their digits, a missing value as ``None``
    (the empty field once written).
    """
    out = {}
    for column in frame.columns:
        out[column] = [_r_character(value) if not isinstance(value, list) else "; ".join(str(item) for item in value) for value in frame[column].tolist()]
    return pd.DataFrame(out, columns=list(frame.columns), dtype="object")


def _csv_bytes(frame: pd.DataFrame) -> bytes:
    """``.ms_sdp_extension_csv_bytes(na = "")``: a CSV, the empty field for a missing value."""
    from .metadata import csv_na_token

    if len(frame.columns) == 0:
        return b""
    return frame.to_csv(index=False, na_rep=csv_na_token()).encode("utf-8")


def _record_bytes(rows: pd.DataFrame) -> bytes:
    return _csv_bytes(_character_frame(_assessment_frame(_records(rows))))


def _findings_bytes(findings: pd.DataFrame) -> bytes:
    frame = findings if findings is not None and len(findings) else _empty_findings()
    return _csv_bytes(_character_frame(frame[list(FINDINGS_COLUMNS)]))


def _read_csv_rows(path: Path) -> pd.DataFrame:
    """A persisted review CSV, every cell text and the empty field missing."""
    from .metadata import read_sdp_csv

    try:
        frame = read_sdp_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()
    return frame.apply(lambda column: column.map(lambda value: None if value == "" else value)) if len(frame) else frame


def read_record(review_dir: Path) -> Optional[pd.DataFrame]:
    """``.ms_semantic_review_read_record()``: the persisted record, typed, or ``None``."""
    path = _review_file(review_dir, "record")
    _assert_readable(review_dir, [path])
    if not path.exists():
        return None
    return _assessment_frame(_records(_read_csv_rows(path)))


def read_findings(review_dir: Path) -> pd.DataFrame:
    """``.ms_semantic_review_read_findings()``: the persisted findings, or an empty frame."""
    path = _review_file(review_dir, "findings")
    _assert_readable(review_dir, [path])
    if not path.exists():
        return _empty_findings()
    found = _read_csv_rows(path)
    if len(found) == 0:
        return _empty_findings()
    for column in FINDINGS_COLUMNS:
        if column not in found.columns:
            found[column] = None
    return found[list(FINDINGS_COLUMNS)].reset_index(drop=True)


def findings_attr(findings: pd.DataFrame) -> dict:
    """The validator findings as a record carries them in ``attrs``: column by column.

    ``attrs["semantic_validator_findings"]`` holds the nine findings columns,
    in order, each as a list of text or ``None`` -- ``DataFrame.to_dict("list")``
    of the findings frame -- so ``pd.DataFrame(record.attrs["semantic_validator_findings"])``
    is that frame, with its columns even when there are no findings. Not the
    frame itself: ``pd.concat()`` compares its inputs' ``attrs`` whenever every
    input has some, and a DataFrame there has no truth value, so two records
    that each carried their findings frame could not be concatenated (hub item
    B-425; the hazard B-370 removed from the retriever). Lists compare, so two
    records concatenate, and keep the findings when theirs are equal.
    metasalmon attaches the findings tibble as the record's attribute, where
    no comparison happens (PARITY.md row 65, item (f)).
    """
    frame = findings if findings is not None and len(findings.columns) else _empty_findings()
    return {
        column: [_r_character(value) for value in frame[column].tolist()] if column in frame.columns else [None] * len(frame)
        for column in FINDINGS_COLUMNS
    }


def _merge_llm_assessments(candidates: list, assessments: pd.DataFrame, top_n: int) -> pd.DataFrame:
    """``.ms_semantic_merge_llm_assessments()``: candidates joined with their target's assessment.

    ``llm_candidate_rank`` is a candidate's position within its target, capped
    at ``top_n`` (missing beyond it); the assessment's columns join on the nine
    identity columns, a missing value matching a missing value as dplyr's join
    does; and ``llm_selected`` holds only for an ``accept`` whose index names
    the rank (PARITY.md row 31).
    """
    from .llm_review import LLM_ASSESSMENT_COLUMNS

    frame = pd.DataFrame(candidates) if not isinstance(candidates, pd.DataFrame) else candidates.copy()
    if len(frame) == 0:
        return frame
    frame = frame.reset_index(drop=True)
    records = _records(frame)
    ranks = []
    seen: dict = {}
    for record in records:
        key = _group_key(record)
        seen[key] = seen.get(key, 0) + 1
        ranks.append(seen[key] if seen[key] <= top_n else None)
    frame["llm_candidate_rank"] = pd.array(ranks, dtype="Int64")
    joined = [column for column in LLM_ASSESSMENT_COLUMNS if column not in IDENTITY_COLUMNS]
    by_key: dict = {}
    for record in _records(assessments):
        by_key.setdefault(_join_key(record), record)
    matched = [by_key.get(_join_key(record), {}) for record in records]
    for column in joined:
        values = [row.get(column) for row in matched]
        if column in ("llm_selected_candidate_index", "llm_exploration_candidate_gain"):
            frame[column] = pd.array([None if _is_missing(value) else int(value) for value in values], dtype="Int64")
        elif column == "llm_confidence":
            frame[column] = pd.Series([math.nan if _is_missing(value) else float(value) for value in values], index=frame.index, dtype="float64")
        elif column == "llm_exploration_used":
            frame[column] = pd.Series([None if _is_missing(value) else bool(value) for value in values], index=frame.index, dtype="object")
        else:
            frame[column] = pd.Series([None if _is_missing(value) else value for value in values], index=frame.index, dtype="object")
    selected = []
    for rank, row in zip(ranks, matched):
        index = row.get("llm_selected_candidate_index")
        selected.append(
            not _is_missing(index)
            and rank is not None
            and int(index) == rank
            and row.get("llm_decision") == "accept"
        )
    frame["llm_selected"] = selected
    return frame


def _join_key(record: Mapping) -> tuple:
    return tuple(_r_character(record.get(column)) for column in IDENTITY_COLUMNS)


def _rewrite_suggestions(
    path: Path, merged: pd.DataFrame, targets: list, assessments: Optional[pd.DataFrame] = None
) -> Optional[dict]:
    """``.ms_semantic_review_rewrite_suggestions()``: the undecided slots' rows replaced.

    For the targets whose slots are still undecided, their rows in
    ``semantic_suggestions.csv`` are replaced by the packet's shortlist carrying
    the merged assessment columns. A slot with a recorded decision keeps its
    rows, as does a hand-picked row; a target the packet does not hold is
    untouched. A target whose packet shortlist came back empty keeps its rows,
    which take the assessment just made (``assessments``, aligned with
    ``targets``). Rows are
    replaced target by target, never slot by slot: a code value of a
    measurement column has three targets in one slot, and a pass that
    finalizes one of them must not drop the rows of another, whether it is
    still awaiting its second pass or was finalized a pass earlier (hub item
    B-425, the mirror of B-424). A package whose every lookup found nothing has
    no shortlist file, and a retry that gained candidates gives it one.
    """
    suggestions_path = path / "semantic_suggestions.csv"
    merged_text = _character_frame(merged) if len(merged) else pd.DataFrame()
    existing = _read_csv_rows(suggestions_path) if suggestions_path.is_file() else pd.DataFrame()
    if len(existing) == 0:
        if len(merged_text) == 0:
            return None
        out = merged_text.copy()
        for column in ("decision", "decision_reason"):
            if column not in out.columns:
                out[column] = None
        return {"path": suggestions_path, "rows": out, "bytes": _csv_bytes(out)}
    for column in ("target_sdp_file", "target_row_key", "target_sdp_field", "decision", "decision_reason"):
        if column not in existing.columns:
            existing[column] = None
    existing_records = _records(existing)
    existing_slots = [_slot_id(record) for record in existing_records]
    existing_targets = [_target_address(record) for record in existing_records]
    decided = {
        slot
        for slot, record in zip(existing_slots, existing_records)
        if record.get("decision") is not None and str(record.get("decision")).strip(_R_TRIM)
    }
    merged_records = _records(merged_text)
    merged_targets = [_target_address(record) for record in merged_records]
    target_addresses = [_target_address(target) for target in targets]
    replace = list(
        dict.fromkeys(
            address for address, target in zip(target_addresses, targets) if target.get("slot_id") not in decided
        )
    )
    verdicts = _records(_character_frame(assessments)) if assessments is not None and len(assessments) else []
    pieces = []
    seen = []
    for address in dict.fromkeys(existing_targets):
        replacement = [dict(record) for record, owner in zip(merged_records, merged_targets) if owner == address]
        # An empty shortlist replaces nothing. Dropping the target's rows would
        # leave a crosswalk-filled slot, whose IRI is not blank, where neither
        # review_semantics() nor blank-slot discovery can find it again.
        if address in replace and replacement:
            # Prefill provenance belongs to the package, not to the harness or
            # its retrieved shortlist. Keep the target's original stamp when an
            # assessment refreshes candidates, so an undecided crosswalk IRI
            # stays reviewable (hub B-426, the mirror of metasalmon's B-120).
            original = [record for record, owner in zip(existing_records, existing_targets) if owner == address]
            for column in ("prefill_origin", "prefill_iri"):
                if column not in existing.columns:
                    continue
                values = list(dict.fromkeys(
                    record.get(column) for record in original
                    if record.get(column) is not None and str(record.get(column)) != ""
                ))
                if len(values) == 1:
                    for record in replacement:
                        record[column] = values[0]
            pieces.extend(replacement)
            seen.append(address)
        elif address in replace and verdicts:
            kept = [record for record, owner in zip(existing_records, existing_targets) if owner == address]
            pieces.extend(_restamp_assessment(kept, verdicts[target_addresses.index(address)]))
        else:
            pieces.extend(record for record, owner in zip(existing_records, existing_targets) if owner == address)
    for address in replace:
        if address not in seen:
            pieces.extend(record for record, owner in zip(merged_records, merged_targets) if owner == address)
    columns = list(existing.columns) + [
        column for column in dict.fromkeys(key for record in pieces for key in record) if column not in existing.columns
    ]
    out = pd.DataFrame([{column: record.get(column) for column in columns} for record in pieces], columns=columns, dtype="object")
    return {"path": suggestions_path, "rows": out, "bytes": _csv_bytes(out)}


def _restamp_assessment(rows: list, verdict: dict) -> list:
    """``.ms_semantic_review_restamp_assessment()``: kept rows take the current verdict.

    The rows an empty shortlist left in place carry the assessment just made
    instead of whichever one they last carried: ``review_semantics()`` reads
    the verdict from these rows, so a superseded verdict, a stale accept above
    all, would otherwise outlive the harness's current one. The packet offered
    nothing to select, so no row is selected and none holds a shortlist rank.
    """
    from .llm_review import LLM_ASSESSMENT_COLUMNS

    stamped = []
    for record in rows:
        record = dict(record)
        for column in LLM_ASSESSMENT_COLUMNS:
            if column not in IDENTITY_COLUMNS:
                record[column] = verdict.get(column)
        record["llm_candidate_rank"] = None
        record["llm_selected"] = "FALSE"
        stamped.append(record)
    return stamped


# -----------------------------------------------------------------------------
# The ingester
# -----------------------------------------------------------------------------


def ingest_semantic_assessments(
    x,
    assessments=None,
    packet=None,
    packet_id: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    search_fn: Callable = find_terms,
    review_dir: Optional[Union[str, os.PathLike]] = None,
    quiet: bool = False,
) -> dict:
    """Ingest a harness's semantic assessments.

    The second half of the review-packet contract: reads the CSV a harness
    wrote against a packet from :func:`~metasalmonpy.write_semantic_review_packet`,
    in the frozen 30-column assessment row, and turns it into the package's
    record. It refuses a selection whose IRI the packet did not offer (recorded
    as an error row, never applied), runs the retry bookkeeping, escalates a
    final ``reject_shortlist`` to ``request_new_term`` so the ontology gap is
    surfaced, runs the bundle validators, merges the result into the
    suggestions and persists it under ``review/``, so that
    :func:`~metasalmonpy.semantic_llm_assessments` returns it for a package path.

    **A retry is a second harness pass.** A ``retry_search`` with a usable query
    is retrieved here through ``search_fn``, and when the search widens the
    shortlist a continuation packet (``review/semantic-review-packet-pass-2.json``)
    is written holding the widened shortlist; nothing from that target is merged
    or escalated until the harness has answered it. Calling this again after the
    harness answers the second packet completes the session. There is no third
    pass. **A rejected shortlist earns no second pass**: it escalates at once.

    **This never calls a model**, and it reaches the network only through
    ``search_fn``, for a retry. It never touches the metadata CSVs.

    Harness text is redacted at capture: every harness-owned free-text value
    is redacted before it reaches the record, the harness's file is never
    copied into ``review/``, and a file the harness wrote at the packet's
    default location is replaced with its redacted form after a successful
    ingest, with a warning.

    A file-level problem aborts the ingest and writes nothing, raising
    :class:`~metasalmonpy.SemanticReviewError` with a stable ``code``:
    ``packet_version``, ``packet_integrity`` (the packet no longer matches its
    ``packet_id``), ``packet_unbound`` (nothing names the packet the file was
    made against), ``packet_mismatch``, ``header``, ``unknown_target``,
    ``duplicate_target``, ``provenance`` and ``no_pass_2``. A row-level problem
    makes that row an error row or downgrades it with a note, and processing
    continues.

    Parameters
    ----------
    x
        The package directory or the in-memory dictionary the packet was built from.
    assessments
        The harness's file: a CSV path or a data frame. Defaults to
        ``review/semantic-assessments-pass-<n>.csv`` for the packet's pass.
    packet
        The packet the file answers: a path or a parsed packet. Defaults to the
        latest pass in ``review/``.
    packet_id
        The ``packet_id`` the assessments were made against. Required unless
        the harness wrote it in the ``<csv>.packet-id`` sidecar beside its CSV;
        a data frame carries no sidecar, so it needs this argument. A packet
        other than the one named is refused with code ``packet_mismatch``.
    provider, model
        Optional overrides for the ``llm_provider`` and ``llm_model`` columns of
        every row.
    search_fn
        The search function used for a retry. Defaults to
        :func:`~metasalmonpy.find_terms`.
    review_dir
        Where the session lives. Defaults to ``review/`` under the package
        directory; required for in-memory input.
    quiet
        Suppress the summary.

    Returns
    -------
    dict
        ``status`` (``"complete"`` or ``"awaiting_pass_2"``), ``pass``,
        ``packet_id``, ``next_packet`` (the continuation packet's path, or
        ``None``), ``assessments`` (the record, carrying the validator findings
        column by column in ``attrs["semantic_validator_findings"]``, as
        ``semantic_review_ingest.findings_attr()`` builds them, so that two
        records concatenate),
        ``findings`` (the same findings as a frame),
        ``suggestions``, ``targets``, ``dictionary`` (with
        ``semantic_suggestions``, ``semantic_targets`` and
        ``semantic_llm_assessments`` in ``attrs``, so
        :func:`~metasalmonpy.detect_semantic_term_gaps` and the accessors work
        unchanged) and ``summary`` (counts of decisions, errors, downgrades,
        escalations and retries).
    """
    from .review_console import _review_source_frames
    from .semantics import _search_once_per_call

    review_input = _review_input(x, review_dir)
    review_dir = review_input["review_dir"]
    provider_override = {"given": provider is not None, "value": None if provider is None else _non_empty_string(provider)}
    model_override = {"given": model is not None, "value": None if model is None else _non_empty_string(model)}

    found = _read_packet(packet, review_dir)
    packet = found["packet"]
    current_id = _check_packet(packet, expected_id=packet_id)
    pass_number = _cell(packet.get("pass"), "integer")
    if pass_number not in (1, 2):
        _abort("packet_version", "The packet's pass must be 1 or 2.")

    # Provenance: a pass-2 packet must descend from the pass-1 packet and the
    # record in this session, and a pass-2 ingest needs a pass-2 packet.
    pass_1_path = _review_file(review_dir, "packet", 1)
    pass_2_path = _review_file(review_dir, "packet", 2)
    pass_1_slots: list = []
    pass_1_keys: list = []
    if pass_number == 2:
        if not pass_2_path.exists():
            _abort("no_pass_2", f"No continuation packet exists in {review_dir}: nothing asked for a second pass.")
        if not pass_1_path.exists() or read_record(review_dir) is None:
            _abort("provenance", f"A pass-2 ingest needs the pass-1 packet and its record in {review_dir}.")
        parent = _text_scalar(packet.get("parent_packet_id"))
        _assert_readable(review_dir, [pass_1_path])
        pass_1_packet = read_semantic_review_json(pass_1_path)
        if parent is None or parent != semantic_review_packet_id(pass_1_packet):
            _abort("provenance", f"The pass-2 packet does not descend from the pass-1 packet in {review_dir}.")
        # The whole session's slots: the result describes every pass-1 target,
        # and a reassessed slot whose pass-2 answer is missing or unusable
        # falls back to its pass-1 candidates.
        pass_1_slots = _slots(pass_1_packet)
        pass_1_keys = [slot["key"] for slot in pass_1_slots]
    elif isinstance(assessments, (str, os.PathLike)) and Path(assessments).name == _review_file(".", "assessments", 2).name:
        _abort("no_pass_2", "A pass-2 assessment file was given, but the packet being ingested is pass 1.")

    read = _read_assessments(assessments, pass_number, review_dir)
    _check_binding(current_id, packet_id, _read_sidecar(read["path"]))
    _check_header(read["columns"])
    # Redacted at capture: nothing below sees the unredacted text.
    harness = [dict(row) for row in read["rows"]]
    redacted_columns = _scrub_harness_text(harness)

    slots = _slots(packet)
    slot_keys = [slot["key"] for slot in slots]
    row_keys = [_identity_key(row) for row in harness]
    unknown = [key for key in row_keys if key not in set(slot_keys)]
    if unknown:
        _abort(
            "unknown_target",
            f"{len(unknown)} assessment row{'s' if len(unknown) != 1 else ''} name a target the "
            "packet does not hold. The identity columns must be copied from the packet exactly.",
        )
    if len(set(row_keys)) != len(row_keys):
        _abort("duplicate_target", "The assessment file holds more than one row for the same target.")

    # Harness values in package-owned columns are overwritten, with one warning.
    written = [
        column
        for column in PACKAGE_OWNED_COLUMNS
        if any(row.get(column) is not None and str(row.get(column)).strip(_R_TRIM) for row in harness)
    ]
    if written:
        warnings.warn(
            "The assessment file wrote package-owned columns, which the package overwrites: "
            + ", ".join(written),
            UserWarning,
            stacklevel=2,
        )

    # Row validation, one row per packet slot; a slot with no row is an error
    # row at pass 1. At pass 2 a reassessed slot whose answer is missing or
    # unusable keeps its pass-1 row and its pass-1 candidates (execplan
    # section 4, item 3), the session still completes, and the fallback is
    # counted and said.
    row_for_slot = {key: position for position, key in enumerate(row_keys)}
    rows: list = [None] * len(slots)
    fallback = [False] * len(slots)
    errors = downgrades = kept_pass_1 = 0
    for i, slot in enumerate(slots):
        unit = packet["units"][slot["unit_index"]]
        context = _context_from_unit(unit)
        target = {column: slot["target"].get(column) for column in SEMANTIC_TARGET_COLUMNS}
        if pass_number == 2 and not slot["reassess"]:
            rows[i] = slot["previous_assessment"]
            continue
        position = row_for_slot.get(slot["key"])
        if position is None:
            config = {"provider": provider_override["value"], "model": model_override["value"]}
            if pass_number == 2:
                rows[i] = _note(
                    slot["previous_assessment"],
                    "No pass-2 assessment was supplied; the pass-1 answer stands and there is no third pass.",
                )
                fallback[i] = True
                kept_pass_1 += 1
            else:
                rows[i] = _error_row(target, config, "No assessment was supplied for this target.")
                errors += 1
            continue
        row = harness[position]
        config = _row_config(row, provider_override, model_override)
        if config["provider"] is None or config["model"] is None:
            errors += 1
            if pass_number == 2:
                # Unusable at pass 2 for the same reason as any other unusable
                # answer: the pass-1 row and candidates stand.
                fallback[i] = True
                kept_pass_1 += 1
                rows[i] = _note(
                    slot["previous_assessment"],
                    "Pass-2 answer was unusable, so the pass-1 answer stands: llm_provider and "
                    "llm_model must be non-empty.",
                )
            else:
                rows[i] = _error_row(target, config, "llm_provider and llm_model must be non-empty.")
            continue
        validated = _validate_row(row, slot, config, context)
        if validated.get("llm_error") is not None:
            errors += 1
            if pass_number == 2:
                validated = _note(
                    slot["previous_assessment"],
                    "Pass-2 answer was unusable, so the pass-1 answer stands: " + validated["llm_error"],
                )
                fallback[i] = True
                kept_pass_1 += 1
        else:
            # A downgrade is a recorded decision other than the one the harness
            # wrote, read with the alias resolved: ``propose_new_term``
            # recorded as ``request_new_term`` is the harness's own decision,
            # not a downgrade (hub B-425, the mirror of B-424).
            if validated["llm_decision"] != _read_decision(row.get("llm_decision")):
                downgrades += 1
            if pass_number == 2:
                previous = slot["previous_assessment"] or {}
                validated = _add_exploration(
                    validated,
                    used=previous.get("llm_exploration_used") is True,
                    queries=previous.get("llm_exploration_queries"),
                    gain=previous.get("llm_exploration_candidate_gain") or 0,
                )
                if validated["llm_decision"] == "retry_search":
                    validated = _note(validated, "Retry requested at pass 2 was not issued; there is no third pass.")
        rows[i] = validated
    if pass_number == 2 and any(fallback):
        # The pass-1 row's index maps onto the pass-1 candidates, so a fallback
        # slot merges those and not the widened shortlist it was shown.
        for i in (position for position, flag in enumerate(fallback) if flag):
            if slots[i]["key"] in pass_1_keys:
                slots[i]["candidates"] = pass_1_slots[pass_1_keys.index(slots[i]["key"])]["candidates"]

    # Retry bookkeeping (pass 1 only): classify, retrieve, merge, count the gain.
    pending: list = []
    retries = 0
    if pass_number == 1:
        source_policy = _source_policy_from_packet(packet)
        top_n = _top_n_from_packet(packet)
        search_once = _search_once_per_call(search_fn)
        for i, slot in enumerate(slots):
            if rows[i].get("llm_decision") != "retry_search":
                continue
            rows[i], query = _classify_retry(rows[i], slot["target"])
            if query is None:
                continue
            retries += 1
            retrieved = _retrieve_retry(slot, query, source_policy, top_n, search_once)
            rows[i] = _add_exploration(rows[i], used=True, queries=query, gain=retrieved["gain"])
            if retrieved["gain"] > 0:
                pending.append({"slot": i, "candidates": retrieved["candidates"]})
    awaiting = {item["slot"] for item in pending}
    is_final = [i not in awaiting for i in range(len(slots))]

    # Escalation: a final reject_shortlist becomes request_new_term.
    escalations = 0
    for i in range(len(slots)):
        if is_final[i] and rows[i].get("llm_decision") == "reject_shortlist":
            rows[i] = _escalate(rows[i])
            escalations += 1

    # Validators, for every bundle unit, rebuilt from the packet.
    previous_findings = read_findings(review_dir) if pass_number == 2 else _empty_findings()
    new_findings = []
    for unit_index, unit in enumerate(packet["units"]):
        if _cell(unit.get("unit_kind")) != "bundle":
            continue
        members = [i for i, slot in enumerate(slots) if slot["unit_index"] == unit_index]
        validated = _validate_bundle(unit, [slots[i] for i in members], [rows[i] for i in members])
        for member, row in zip(members, validated["rows"]):
            rows[member] = row
        new_findings.append(validated["findings"])
    findings = _union_findings(previous_findings, *new_findings)

    # The record: at pass 1 one row per target; at pass 2 the record on disk
    # with the reassessed rows replaced.
    final_rows = _assessment_frame(rows)
    if pass_number == 2:
        record_rows = _records(read_record(review_dir))
        record_keys = [_identity_key(row) for row in record_rows]
        for i, key in enumerate(slot_keys):
            replacement = _records(final_rows.iloc[[i]])[0]
            if key in record_keys:
                record_rows[record_keys.index(key)] = replacement
            else:
                record_rows.append(replacement)
        record = _assessment_frame(record_rows)
    else:
        record = final_rows

    # The merge: the final targets' candidates with their assessment columns.
    # At pass 2 that is every reassessed slot of the continuation packet (a
    # fallback slot with its pass-1 candidates); the slots final at pass 1
    # were merged then.
    top_n = _top_n_from_packet(packet)
    merge_slots = [i for i in range(len(slots)) if is_final[i] and (pass_number == 1 or slots[i]["reassess"])]
    candidates = [row for i in merge_slots for row in slots[i]["candidates"]]
    merged = (
        _merge_llm_assessments(candidates, final_rows.iloc[merge_slots], top_n)
        if candidates
        else pd.DataFrame()
    )
    targets = [slots[i]["target"] for i in range(len(slots))]
    final_targets = [targets[i] for i in merge_slots]

    # What the result describes is the whole session, never the continuation
    # subset.
    if pass_number == 2:
        session_targets = [slot["target"] for slot in pass_1_slots]
        session_candidates = []
        for j, key in enumerate(pass_1_keys):
            here = slot_keys.index(key) if key in slot_keys else None
            if here is not None and slots[here]["reassess"]:
                session_candidates.extend(slots[here]["candidates"])
            else:
                session_candidates.extend(pass_1_slots[j]["candidates"])
        session_merged = (
            _merge_llm_assessments(session_candidates, record, top_n)
            if session_candidates
            else pd.DataFrame()
        )
    else:
        session_targets = targets
        session_merged = merged

    # Persistence, as one atomic set after the containment check. The harness
    # file is never copied into review/; when the harness wrote it at the
    # packet's default location there, it is replaced with its redacted form.
    writes: dict = {}
    harness_path = _review_file(review_dir, "assessments", pass_number)
    same_file = read["path"] is not None and os.path.abspath(read["path"]) == os.path.abspath(harness_path)
    if same_file and redacted_columns:
        writes[harness_path] = _csv_bytes(
            pd.DataFrame([{column: row.get(column) for column in read["columns"]} for row in harness], columns=read["columns"], dtype="object")
        )
    writes[_review_file(review_dir, "record")] = _record_bytes(record)
    writes[_review_file(review_dir, "findings")] = _findings_bytes(findings)
    next_packet = None
    if pending:
        continuation = _pass_2_packet(packet, slots, rows, pending, findings)
        writes[pass_2_path] = semantic_review_canonical_bytes(continuation)
        next_packet = str(pass_2_path)
    suggestions_out = None
    if review_input["kind"] == "package" and final_targets:
        rewrite = _rewrite_suggestions(review_input["path"], merged, final_targets, final_rows.iloc[merge_slots])
        if rewrite is not None:
            writes[rewrite["path"]] = rewrite["bytes"]
            suggestions_out = rewrite["rows"]
    review_dir.mkdir(parents=True, exist_ok=True)
    from .package_io import _assert_managed_paths_contained
    from .sdp_methods import _atomic_write_set

    _assert_managed_paths_contained(review_dir, [str(path) for path in writes if Path(path).parent == review_dir])
    if review_input["kind"] == "package":
        _assert_managed_paths_contained(review_input["path"], [str(path) for path in writes])
    _atomic_write_set({Path(path): data for path, data in writes.items()})
    if redacted_columns:
        message = (
            "Secrets were redacted from the harness's assessments before they reached the record: "
            + ", ".join(redacted_columns)
            + "."
        )
        if same_file:
            message += f" The harness file in {review_dir} was replaced with its redacted form."
        warnings.warn(message, UserWarning, stacklevel=2)

    record.attrs["semantic_validator_findings"] = findings_attr(findings)
    suggestions = suggestions_out if suggestions_out is not None else session_merged
    if review_input["kind"] == "package":
        dictionary = _review_source_frames(str(review_input["path"])).get("column_dictionary.csv")
        dictionary = pd.DataFrame() if dictionary is None else dictionary
    else:
        dictionary = review_input["dict"].copy()
    target_frame = pd.DataFrame(
        [{column: target.get(column) for column in SEMANTIC_TARGET_COLUMNS} for target in session_targets],
        columns=list(SEMANTIC_TARGET_COLUMNS),
    )
    dictionary.attrs["semantic_suggestions"] = suggestions
    dictionary.attrs["semantic_targets"] = target_frame
    dictionary.attrs["semantic_llm_assessments"] = record

    status = "awaiting_pass_2" if pending else "complete"
    decisions = {decision: 0 for decision in DECISION_VOCABULARY}
    for value in final_rows["llm_decision"]:
        if value in decisions:
            decisions[value] += 1
    summary = {
        "decisions": decisions,
        "errors": errors,
        "downgrades": downgrades,
        "escalations": escalations,
        "retries": retries,
        "awaiting_pass_2": len(pending),
        "kept_pass_1": kept_pass_1,
    }
    if not quiet:
        print(f"Ingested {len(final_rows)} assessment{'s' if len(final_rows) != 1 else ''} for pass {pass_number}: status {status!r}.")
        print(
            f"  {errors} error row{'s' if errors != 1 else ''}, {downgrades} downgrade{'s' if downgrades != 1 else ''}, "
            f"{escalations} escalation{'s' if escalations != 1 else ''}, {retries} retr{'y' if retries == 1 else 'ies'}."
        )
        if kept_pass_1:
            print(
                f"  {kept_pass_1} reassessed target{'s' if kept_pass_1 != 1 else ''} kept "
                f"{'its' if kept_pass_1 == 1 else 'their'} pass-1 answer because the pass-2 answer was "
                "missing or unusable; there is no third pass."
            )
        if next_packet is not None:
            print(f"  Continuation packet written to {next_packet}; have the harness answer it, then ingest again.")
    return {
        "status": status,
        "pass": pass_number,
        "packet_id": current_id,
        "next_packet": next_packet,
        "assessments": record,
        "findings": findings,
        "suggestions": suggestions,
        "targets": target_frame,
        "dictionary": dictionary,
        "summary": summary,
    }
