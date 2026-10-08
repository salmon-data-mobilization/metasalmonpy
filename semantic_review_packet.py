"""The semantic review packet (hub item B-327, the mirror of metasalmon's B-326).

Brett ruled on 2026-09-25 (hub Q67) that the model call leaves both packages:
judgement runs in the user's own harness, and the seam is a file, because a
harness cannot hand a closure to a package process it did not start. The packet
this module writes is what the package already computes for a review -- the
targets in the frozen 19-column row, each target's ranked shortlist by index,
the bundle groups with their current slots, the scored context excerpts, the
review instructions, the decision vocabulary and the 30-column assessment
schema -- rendered to deterministic bytes by :mod:`.semantic_review_json`. The
assessment file a harness writes back is read by
:func:`~metasalmonpy.semantic_review_ingest.ingest_semantic_assessments`.

This is a port of ``R/semantic-review-packet.R`` function by function, and the
design is metasalmon's ``knowledge/plans/2026-09-25-s16-review-packet-contract.md``
(sections 2, 5 and 7). The contract is shared: the packet schema and the review
instructions live in ``data/semantic-review/`` byte-identically with
metasalmon's ``inst/extdata/semantic-review/``, and the conformance cases under
``tests/data/semantic_review/v1/`` are metasalmon's own, vendored unchanged. For
every case, the packet written here is byte-identical to metasalmon's with the
``producer`` member aside, which ``packet_id`` excludes.

**Every ordering is by code point, never by locale** (PARITY.md row 3): units
sort by key, ``extra`` keys and the pinned source lists sort with ``sorted()``,
and candidates keep retrieval order.

**No model is ever called here, and no socket is opened except through the
``search_fn`` a caller gives**, for a package path's re-retrieval.
``tests/test_semantic_review_packet.py`` blocks the socket API to prove it.
"""

from __future__ import annotations

import math
import numbers
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Callable, Optional, Sequence, Union

import pandas as pd

from .metadata import scalar_text
from .semantic_review_json import (
    semantic_review_canonical_bytes,
    semantic_review_packet_id,
)
from .semantics import _SEMANTIC_TARGET_COLUMNS
from .term_search import find_terms

__all__ = ["write_semantic_review_packet"]


# -----------------------------------------------------------------------------
# The contract's constants
# -----------------------------------------------------------------------------

PACKET_VERSION = "semantic-review-packet/1.0"
DECISION_VOCABULARY = ("accept", "review", "retry_search", "request_new_term", "reject_shortlist")
DECISION_ALIASES = {"propose_new_term": "request_new_term"}

#: ``.ms_semantic_target_cols()``: the frozen 19-column target row, in order.
SEMANTIC_TARGET_COLUMNS = _SEMANTIC_TARGET_COLUMNS

#: ``.ms_semantic_target_group_cols()``: what groups a target's candidates.
TARGET_GROUP_COLUMNS = (
    "dataset_id",
    "table_id",
    "column_name",
    "code_value",
    "dictionary_role",
    "target_scope",
    "target_sdp_file",
    "target_sdp_field",
)

#: ``.ms_semantic_assessment_join_cols()``: the identity a harness copies back.
IDENTITY_COLUMNS = TARGET_GROUP_COLUMNS + ("search_query",)

#: ``.ms_semantic_bundle_roles()``, in order: the six dictionary slots plus the
#: code-level ``method`` role.
BUNDLE_ROLES = (
    "variable",
    "property",
    "entity",
    "unit",
    "constraint",
    "statistical_modifier",
    "method",
)

#: ``.ms_semantic_bundle_slot_fields()``.
BUNDLE_SLOT_FIELDS = {
    "variable": "term_iri",
    "property": "property_iri",
    "entity": "entity_iri",
    "unit": "unit_iri",
    "constraint": "constraint_iri",
    "statistical_modifier": "statistical_modifier_iri",
}

#: ``.ms_semantic_review_dictionary_fields()``.
DICTIONARY_FIELDS = (
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

#: ``.ms_semantic_review_candidate_fields()``: the named candidate members that
#: are read straight from a column, in the packet's order.
CANDIDATE_FIELDS = (
    "label",
    "iri",
    "source",
    "ontology",
    "role",
    "match_type",
    "definition",
    "term_type",
    "resource_kind",
    "type_iris",
    "role_hints",
    "role_hint_status",
    "role_hint_bonus",
    "alignment_only",
    "agreement_sources",
    "retrieval_query",
    "retrieval_pass",
    "role_collision",
    "role_collision_note",
)

#: ``.ms_semantic_review_candidate_excluded_cols()``: columns of a candidate row
#: that are not candidate evidence -- the target it was stamped with, the named
#: fields, the score, the review console's decision columns and internal keys.
#: Every other column goes to ``extra``, except ``.``-prefixed and ``llm_``
#: columns.
CANDIDATE_EXCLUDED_COLUMNS = frozenset(
    SEMANTIC_TARGET_COLUMNS
    + CANDIDATE_FIELDS
    + (
        "score",
        "decision",
        "decision_reason",
        "llm_selected",
        "llm_candidate_rank",
        "candidate_label_norm",
        "collision_roles",
    )
)

#: The context chunking parameters the packet records (``.ms_chunk_context_text()``
#: and the review unit's excerpt limit).
CONTEXT_CHUNKING = {
    "chunk_chars": 2200,
    "overlap_chars": 200,
    "max_excerpts": 4,
    "token_min_chars": 3,
}

_CODE_SCOPES = ("factor", "all", "none")


def _review_file(review_dir: Union[str, Path], what: str, pass_number: int = 1) -> Path:
    """``.ms_semantic_review_file()``: where a session's files live (decision 2)."""
    names = {
        "packet": "semantic-review-packet.json" if pass_number == 1 else "semantic-review-packet-pass-2.json",
        "assessments": f"semantic-assessments-pass-{pass_number}.csv",
        "record": "semantic-llm-assessments.csv",
        "findings": "semantic-validator-findings.csv",
    }
    if what not in names:
        raise ValueError(f"Unknown semantic review file kind {what!r}.")
    return Path(review_dir) / names[what]


def _session_files(review_dir: Union[str, Path]) -> list:
    """``.ms_semantic_review_session_files()``: the files a session may hold."""
    return [
        _review_file(review_dir, "packet", 1),
        _review_file(review_dir, "packet", 2),
        _review_file(review_dir, "assessments", 1),
        _review_file(review_dir, "assessments", 2),
        _review_file(review_dir, "record"),
        _review_file(review_dir, "findings"),
    ]


def _assert_readable(review_dir: Union[str, Path], paths) -> None:
    """``.ms_semantic_review_assert_readable()``: refuse a review file reached through a link.

    ``review/`` and the files in it are read back by the ingester and by
    :func:`~metasalmonpy.review_console.semantic_llm_assessments`, and a package
    written by someone else could make ``review/``, or one file in it, a link
    to another package's private review record. The containment guard checks
    the directory itself and every component under it; a path that does not
    exist yet passes, because there is nothing to follow.
    """
    from .package_io import _assert_managed_paths_contained

    existing = [str(path) for path in paths if os.path.lexists(str(path))]
    if existing:
        _assert_managed_paths_contained(Path(review_dir), existing)


def _data_file(name: str) -> Path:
    return Path(__file__).resolve().parent / "data" / "semantic-review" / name


def semantic_review_instructions() -> str:
    """The vendored review instructions, as the packet embeds them.

    Read the way ``.ms_read_text_utf8()`` reads them -- lines joined with LF,
    so the file's final newline is not part of the text -- because the
    instructions are hashed into ``packet_id``.
    """
    from .llm_review import _read_text_file

    path = _data_file("semantic-review-instructions-v1.txt")
    if not path.is_file():
        raise FileNotFoundError(
            "The vendored semantic review instructions are missing from the installed package."
        )
    return _read_text_file(path)


def semantic_review_schema_path() -> Path:
    """The vendored packet schema (the packet, the assessment row and the findings row)."""
    path = _data_file("semantic-review-packet-v1.schema.json")
    if not path.is_file():
        raise FileNotFoundError(
            "The vendored semantic review packet schema is missing from the installed package."
        )
    return path


def output_columns() -> list:
    """``.ms_semantic_review_output_columns()``: the 30 columns, an owner and a requiredness each.

    ``required`` is one of ``always``, ``accept``, ``retry_search`` and
    ``never``; a harness reads it as "on which decision must I fill this".
    """
    from .llm_review import LLM_ASSESSMENT_COLUMNS

    columns = list(LLM_ASSESSMENT_COLUMNS)
    owner = {column: "harness" for column in columns}
    required = {column: "never" for column in columns}
    for column in columns[:13]:
        required[column] = "always"
    required["llm_selected_candidate_index"] = "accept"
    required["llm_selected_iri"] = "accept"
    required["llm_retry_query"] = "retry_search"
    for column in PACKAGE_OWNED_COLUMNS:
        owner[column] = "package"
    owner["llm_error"] = "harness_or_package"
    return [
        {"name": column, "owner": owner[column], "required": required[column]}
        for column in columns
    ]


#: The columns the package fills and overwrites (``owner = "package"``).
PACKAGE_OWNED_COLUMNS = (
    "llm_selected_label",
    "llm_context_sources",
    "llm_exploration_used",
    "llm_exploration_queries",
    "llm_exploration_candidate_gain",
    "llm_escalated_from",
    "llm_retry_query_rejection_reason",
)


# -----------------------------------------------------------------------------
# Cells, as R reads them
# -----------------------------------------------------------------------------


def _is_missing(value) -> bool:
    """``is.na()`` for one cell: ``None``, ``NaN``, ``pd.NA`` and ``NaT``."""
    if value is None:
        return True
    if value is pd.NA or value is pd.NaT:
        return True
    if isinstance(value, float):
        return math.isnan(value)
    if isinstance(value, numbers.Number) and not isinstance(value, numbers.Integral):
        try:
            return math.isnan(float(value))
        except (TypeError, ValueError):
            return False
    return False


def _plain(value):
    """A numpy scalar as the Python scalar it holds; anything else unchanged."""
    item = getattr(value, "item", None)
    if item is not None and type(value).__module__ == "numpy":
        try:
            return item()
        except (TypeError, ValueError):
            return value
    return value


def _r_character(value) -> Optional[str]:
    """``as.character()`` of one cell: ``None`` when missing.

    A logical renders ``TRUE``/``FALSE`` and a number through the shared
    number-token formatter; R renders a double in a text field its own way
    (``1e+05``), which no text field of a candidate row holds in practice.
    """
    value = _plain(value)
    if _is_missing(value):
        return None
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, numbers.Integral):
        return str(int(value))
    if isinstance(value, numbers.Real):
        from .resource_types import format_number_token

        return format_number_token(float(value))
    return str(value)


def _scalar(value):
    """``.ms_semantic_review_scalar()``: one cell as a JSON scalar.

    Missing and the empty string are ``None``; anything else is itself (a
    number stays a number, a logical a logical). A list-valued cell yields its
    first element, as ``value[[1]]`` does.
    """
    if isinstance(value, (list, tuple)):
        if not value:
            return None
        value = value[0]
    value = _plain(value)
    if _is_missing(value):
        return None
    if isinstance(value, str) and value == "":
        return None
    return value


def _text_scalar(value) -> Optional[str]:
    """``.ms_semantic_review_text_scalar()``: :func:`_scalar` as text."""
    scalar = _scalar(value)
    if scalar is None:
        return None
    return _r_character(scalar)


def _number(value) -> Optional[float]:
    """``suppressWarnings(as.numeric(.ms_semantic_review_scalar(value)))``."""
    scalar = _scalar(value)
    if scalar is None:
        return None
    if isinstance(scalar, bool):
        return 1.0 if scalar else 0.0
    if isinstance(scalar, numbers.Real):
        return float(scalar)
    try:
        return float(str(scalar).strip(" \t\r\n"))
    except ValueError:
        return None


def _flag(value) -> Optional[bool]:
    """A logical member: a logical is itself; ``true``/``t``/``1`` and
    ``false``/``f``/``0`` are read from text, and anything else is ``None``."""
    scalar = _scalar(value)
    if scalar is None:
        return None
    if isinstance(scalar, bool):
        return scalar
    lowered = (_r_character(scalar) or "").strip(" \t\r\n").lower()
    if lowered in ("true", "t", "1"):
        return True
    if lowered in ("false", "f", "0"):
        return False
    return None


def _trim_string(value, default=None):
    """``.ms_semantic_trim_string()``: the value as trimmed text, else ``default``."""
    text = _r_character(_scalar(value) if not isinstance(value, str) else value)
    if text is None:
        return default
    text = text.strip(" \t\r\n")
    return text if text else default


def _group_key(row: Mapping, columns=TARGET_GROUP_COLUMNS) -> str:
    """``.ms_semantic_key_df()`` for one row: ``<NA>`` for a missing value, ``\\r``-joined."""
    parts = []
    for column in columns:
        text = _r_character(row.get(column))
        parts.append("<NA>" if text is None else text)
    return "\r".join(parts)


def _slot_id(row: Mapping) -> str:
    """``.ms_review_slot_id()`` for one row: ``as.character()`` of the three cells, ``|``-joined.

    Deliberately R's rendering (a missing cell is ``NA``, nothing is trimmed)
    rather than :func:`~metasalmonpy.review_console._review_slot_id`'s, because
    the slot id is written into the packet's bytes. The two agree on every slot
    a package actually addresses, whose three cells are present and trimmed.
    """
    return "|".join(
        "NA" if text is None else text
        for text in (
            _r_character(row.get("target_sdp_file")),
            _r_character(row.get("target_row_key")),
            _r_character(row.get("target_sdp_field")),
        )
    )


_ADDRESS_COLUMNS = ("target_sdp_file", "target_row_key", "target_sdp_field")


def _target_address(row: Mapping) -> str:
    """``.ms_semantic_review_target_address()``: a target's slot and its role.

    A code value of a measurement column gets a constraint, an entity and a
    method target, and all three write into the code's one ``codes.csv``
    ``term_iri``, so the slot alone does not name a target (hub item B-425,
    the mirror of metasalmon's B-424): three target units shared one key, which
    aborted an in-memory build, and a package path kept only the first role's
    shortlist. The slot and the role do name it. The slot is read from its three
    address fields whenever a row carries them, and from a precomputed
    ``slot_id`` only when it does not (the review queue's rows); a missing role
    renders ``NA``, as R's ``paste()`` renders it. The target unit key is this
    address, so the packet's bytes read this one rendering.
    """
    if not all(column in row for column in _ADDRESS_COLUMNS) and "slot_id" in row:
        slot = _r_character(row.get("slot_id"))
        slot = "NA" if slot is None else slot
    else:
        slot = _slot_id(row)
    role = _r_character(row.get("dictionary_role"))
    return slot + "|" + ("NA" if role is None else role)


def _decided_slots(records: list) -> set:
    """``.ms_semantic_review_decided_slots()``: the slots carrying a recorded decision.

    Read the way the review console reads one
    (:func:`~metasalmonpy.review_console._recorded_decision`): the console takes
    such a slot out of the queue, so no target of it is recovered either,
    whichever role recorded the decision.
    """
    from .review_console import _recorded_decision

    return {
        _slot_id(record)
        for record in records
        if "decision" in record and _recorded_decision(record.get("decision")) is not None
    }


def _records(frame: Optional[pd.DataFrame]) -> list:
    """One dict per row, keys in column order."""
    if frame is None or len(frame) == 0:
        return []
    return [dict(zip(frame.columns, row)) for row in frame.itertuples(index=False, name=None)]


def _target_rows(frame: Optional[pd.DataFrame]) -> list:
    """Targets restricted to the 19 target columns (a missing one added as ``None``)."""
    rows = []
    for record in _records(frame):
        rows.append({column: record.get(column) for column in SEMANTIC_TARGET_COLUMNS})
    return rows


# -----------------------------------------------------------------------------
# Input resolution
# -----------------------------------------------------------------------------


def _review_input(x, review_dir, arg: str = "x") -> dict:
    """``.ms_semantic_review_input()``: a package path or an in-memory dictionary.

    An artifact mapping in the ``infer_salmon_datapackage_artifacts()`` shape
    counts as in-memory input through its ``dict``.
    """
    if isinstance(x, (str, os.PathLike)):
        path = Path(x)
        if not path.is_dir():
            raise FileNotFoundError(f"Package directory {path} does not exist.")
        return {
            "kind": "package",
            "path": path,
            "review_dir": Path(review_dir) if review_dir is not None else path / "review",
        }
    if isinstance(x, pd.DataFrame):
        dictionary = x
    elif isinstance(x, Mapping) and isinstance(x.get("dict"), pd.DataFrame):
        dictionary = x["dict"]
    else:
        raise TypeError(
            f"{arg} must be a package path or a dictionary carrying semantic_targets "
            "and semantic_suggestions. Pass the directory create_sdp() wrote, or the "
            "dictionary suggest_semantics() returned."
        )
    if review_dir is None:
        raise ValueError(
            "review_dir is required for in-memory input: the packet and the "
            "harness's answers need a directory that outlives this call."
        )
    return {"kind": "memory", "dict": dictionary, "object": x, "review_dir": Path(review_dir)}


def _current_values(targets: list, frames: Mapping) -> list:
    """``.ms_semantic_review_current_values()``: each target's slot, read as the console reads it.

    ``None`` when the frame, its keys or the field are absent or the row match
    is not exactly one row; the empty string when the slot is empty.
    """
    from .review_console import _review_match_rows, review_target_keys

    values = []
    for target in targets:
        target_file = scalar_text(target.get("target_sdp_file"))
        target_field = scalar_text(target.get("target_sdp_field"))
        frame = frames.get(target_file)
        keys = review_target_keys(target_file)
        if not isinstance(frame, pd.DataFrame) or keys is None or target_field not in frame.columns:
            values.append(None)
            continue
        hits = _review_match_rows(frame, target, keys)
        if len(hits) != 1:
            values.append(None)
            continue
        value = _r_character(frame.at[hits[0], target_field])
        values.append("" if value is None else value)
    return values


def _memory_targets(dictionary: pd.DataFrame, top_n: int) -> dict:
    """``.ms_semantic_review_memory_targets()``: the dictionary's own attributes.

    Every target is included, and each keeps the first ``top_n`` of its
    candidates in the order the ranked producer emitted them. Hand-picked rows
    (``source = "user"``) are a reviewer's decision, not a candidate, and are
    dropped. No retrieval runs.
    """
    from .metadata_write import _is_hand_picked

    targets_frame = dictionary.attrs.get("semantic_targets")
    if targets_frame is None:
        raise ValueError(
            "The dictionary carries no semantic_targets attribute. Run "
            "suggest_semantics() first; it attaches the targets and the shortlist."
        )
    suggestions = dictionary.attrs.get("semantic_suggestions")
    suggestions = pd.DataFrame() if suggestions is None else pd.DataFrame(suggestions)
    targets = _target_rows(pd.DataFrame(targets_frame))
    for target in targets:
        target["slot_id"] = _slot_id(target)
    candidates = []
    if len(suggestions) and targets:
        kept = suggestions[~_is_hand_picked(suggestions).to_numpy(dtype=bool)]
        target_keys = {_group_key(target) for target in targets}
        ranks: dict = {}
        for record in _records(kept):
            key = _group_key(record)
            if key not in target_keys:
                continue
            ranks[key] = ranks.get(key, 0) + 1
            if ranks[key] <= top_n:
                candidates.append(record)
    return {"targets": targets, "candidates": candidates, "failed_sources": [], "not_covered": pd.DataFrame()}


# -----------------------------------------------------------------------------
# Unit assembly
# -----------------------------------------------------------------------------


def _bundle_dictionary_rows(target: Mapping, dictionary: Optional[pd.DataFrame]) -> list:
    """``.ms_semantic_bundle_dictionary_row()``: the dictionary rows naming the target's column.

    Each of ``dataset_id``, ``table_id`` and ``column_name`` the dictionary
    carries constrains the match when the target's own value (trimmed) is
    present; the dictionary's value is compared as it is.
    """
    records = _records(dictionary)
    if not records:
        return []
    columns = list(dictionary.columns)
    keep = [True] * len(records)
    for column in ("dataset_id", "table_id", "column_name"):
        if column not in columns:
            continue
        value = _trim_string(target.get(column))
        if value is None:
            continue
        for position, record in enumerate(records):
            text = _r_character(record.get(column))
            keep[position] = keep[position] and text is not None and text == value
    return [record for record, kept in zip(records, keep) if kept]


def _dictionary_object(rows: list) -> dict:
    """``.ms_semantic_review_dictionary_object()``: the nine fields of a unique dictionary row."""
    row = rows[0] if len(rows) == 1 else None
    return {
        field: (_scalar(row.get(field)) if row is not None and field in row else None)
        for field in DICTIONARY_FIELDS
    }


def _current_slots(rows: list) -> dict:
    """``.ms_semantic_review_current_slots()``: the six slot values of a unique dictionary row."""
    row = rows[0] if len(rows) == 1 else None
    return {
        role: (_scalar(row.get(field)) if row is not None and field in row else None)
        for role, field in BUNDLE_SLOT_FIELDS.items()
    }


def _candidate_type(row: Mapping) -> str:
    """``.ms_semantic_validator_candidate_type()``: the row's type fields, as validator text."""
    from .llm_review import _candidate_type as validator_candidate_type

    return validator_candidate_type(
        {field: row.get(field) for field in ("term_type", "native_type", "resource_kind", "type_iris") if field in row}
    )


def _extra_value(value):
    """One ``extra`` member: a list-valued cell of several values joined with ``"; "``."""
    if isinstance(value, (list, tuple)):
        if len(value) > 1:
            return "; ".join("NA" if text is None else text for text in (_r_character(item) for item in value))
        value = value[0] if value else None
    return _scalar(value)


def candidate_object(row: Mapping, index: int) -> dict:
    """``.ms_semantic_review_candidate_object()``: one candidate, members in the packet's order.

    There is no candidate id: selection is by ``index``, because the two
    languages fingerprint a blank-IRI candidate differently.
    """
    def text(column):
        return _text_scalar(row.get(column)) if column in row else None

    def number(column):
        return _number(row.get(column)) if column in row else None

    def flag(column):
        return _flag(row.get(column)) if column in row else None

    retrieval_pass = number("retrieval_pass")
    extra_columns = sorted(
        column
        for column in row
        if column not in CANDIDATE_EXCLUDED_COLUMNS
        and not column.startswith(".")
        and not column.startswith("llm_")
    )
    return {
        "index": int(index),
        "label": text("label"),
        "iri": text("iri"),
        "source": text("source"),
        "ontology": text("ontology"),
        "role": text("role"),
        "match_type": text("match_type"),
        "definition": text("definition"),
        "term_type": text("term_type"),
        "resource_kind": text("resource_kind"),
        "type_iris": text("type_iris"),
        "native_type": _text_scalar(_candidate_type(row)),
        "role_hints": text("role_hints"),
        "role_hint_status": text("role_hint_status"),
        "role_hint_bonus": number("role_hint_bonus"),
        "lexical_score": number("score"),
        "alignment_only": flag("alignment_only"),
        "agreement_sources": text("agreement_sources"),
        "retrieval_query": text("retrieval_query"),
        "retrieval_pass": None if retrieval_pass is None else int(math.trunc(retrieval_pass)),
        "role_collision": flag("role_collision"),
        "role_collision_note": text("role_collision_note"),
        "extra": {column: _extra_value(row.get(column)) for column in extra_columns},
    }


def _identity_object(target: Mapping) -> dict:
    return {column: _text_scalar(target.get(column)) for column in IDENTITY_COLUMNS}


def _target_object(target: Mapping) -> dict:
    return {column: _text_scalar(target.get(column)) for column in SEMANTIC_TARGET_COLUMNS}


def _slot_object(target: Mapping, candidate_rows: list) -> dict:
    """``.ms_semantic_review_slot_object()``."""
    return {
        "dictionary_role": _text_scalar(target.get("dictionary_role")),
        "target_sdp_field": _text_scalar(target.get("target_sdp_field")),
        "status": "review",
        "current_value": _text_scalar(target.get("current_value")),
        "slot_id": _text_scalar(target.get("slot_id")),
        "identity": _identity_object(target),
        "target": _target_object(target),
        "candidates": [candidate_object(row, index) for index, row in enumerate(candidate_rows, start=1)],
    }


def _excerpt_objects(chunks: list) -> list:
    """``.ms_semantic_review_excerpt_objects()``."""
    return [
        {
            "source": _r_character(chunk.get("source")),
            "chunk_id": _r_character(chunk.get("chunk_id")),
            "context_score": chunk.get("context_score", 0),
            "excerpt": _r_character(chunk.get("text")),
        }
        for chunk in chunks
    ]


def _unit_context(targets: list, candidate_groups: Mapping, pool: pd.DataFrame, max_chunks: int = 4) -> list:
    """``.ms_semantic_review_unit_context()``: one unit's excerpts.

    Each target scores the pool with its own candidates
    (``.ms_prepare_context_chunks()``); the picks are joined in target order,
    deduplicated on source and chunk id, and cut to four. A target whose
    scoring had nothing to score by contributes the head of the pool unscored,
    and its excerpts carry a missing score -- or ``0`` when no target scored.
    """
    from .llm_review import _score_context_chunks

    if pool is None or len(pool) == 0:
        return []
    picks = []
    any_scored = False
    for target in targets:
        candidates = pd.DataFrame(candidate_groups.get(_group_key(target), []))
        ranked = _score_context_chunks(pool, target, candidates, max_chunks)
        scored = "context_score" in ranked.columns
        any_scored = any_scored or scored
        for record in _records(ranked):
            if not scored:
                record["context_score"] = None
            picks.append(record)
    if not picks:
        return []
    seen = set()
    unique = []
    for record in picks:
        key = (record.get("source"), record.get("chunk_id"))
        if key in seen:
            continue
        seen.add(key)
        if not any_scored:
            record["context_score"] = 0
        unique.append(record)
    return unique[:max_chunks]


def _bundle_review_target_keys(targets: list, dictionary: Optional[pd.DataFrame]) -> set:
    """``.ms_semantic_bundle_review_targets()``: the targets judged as one measurement bundle.

    A column-scope target with no code value, in a bundle role, whose
    dictionary row is unique and is a measurement.
    """
    keys = set()
    for target in targets:
        if _trim_string(target.get("target_scope")) != "column":
            continue
        if _trim_string(target.get("code_value")) is not None:
            continue
        if _trim_string(target.get("dictionary_role")) not in BUNDLE_ROLES:
            continue
        rows = _bundle_dictionary_rows(target, dictionary)
        if len(rows) != 1:
            continue
        role = _trim_string(rows[0].get("column_role"))
        if role is not None and role.lower() == "measurement":
            keys.add(_group_key(target))
    return keys


def assemble_units(targets: list, candidates: list, dictionary: Optional[pd.DataFrame], pool: pd.DataFrame) -> list:
    """``.ms_semantic_review_units()``: one bundle unit per measurement column, one target unit otherwise.

    A target with no candidates is a unit like any other. Units sort by key in
    code-point order; candidates keep retrieval order.
    """
    if not targets:
        return []
    target_keys = [_group_key(target) for target in targets]
    candidate_groups: dict = {}
    for key in target_keys:
        candidate_groups.setdefault(key, [])
    for row in candidates:
        key = _group_key(row)
        if key in candidate_groups:
            candidate_groups[key].append(row)
    bundle_keys = _bundle_review_target_keys(targets, dictionary)
    is_bundle = [key in bundle_keys for key in target_keys]

    units = []
    order_of_bundles: dict = {}
    for position, target in enumerate(targets):
        if is_bundle[position]:
            key = _group_key(target, ("dataset_id", "table_id", "column_name"))
            order_of_bundles.setdefault(key, []).append(position)
    role_rank = {role: rank for rank, role in enumerate(BUNDLE_ROLES)}
    for members in order_of_bundles.values():
        member_targets = sorted(
            (targets[position] for position in members),
            key=lambda target: role_rank.get(_r_character(target.get("dictionary_role")), len(BUNDLE_ROLES)),
        )
        dictionary_rows = _bundle_dictionary_rows(member_targets[0], dictionary)
        unit_key = "bundle:" + "/".join(
            _text_scalar(member_targets[0].get(column)) or ""
            for column in ("dataset_id", "table_id", "column_name")
        )
        units.append(
            {
                "unit_kind": "bundle",
                "unit_key": unit_key,
                "dictionary": _dictionary_object(dictionary_rows),
                "current_slots": _current_slots(dictionary_rows),
                "slots": [
                    _slot_object(target, candidate_groups.get(_group_key(target), []))
                    for target in member_targets
                ],
                "context_excerpts": _excerpt_objects(_unit_context(member_targets, candidate_groups, pool)),
            }
        )
    for position, target in enumerate(targets):
        if is_bundle[position]:
            continue
        if _trim_string(target.get("target_scope")) == "column":
            dictionary_rows = _bundle_dictionary_rows(target, dictionary)
        else:
            dictionary_rows = []
        # The slot and the role: a code value of a measurement column has three
        # targets in one slot, each its own unit (hub item B-425).
        unit_key = "target:" + _target_address(target)
        units.append(
            {
                "unit_kind": "target",
                "unit_key": unit_key,
                "dictionary": _dictionary_object(dictionary_rows),
                "current_slots": {},
                "slots": [_slot_object(target, candidate_groups.get(target_keys[position], []))],
                "context_excerpts": _excerpt_objects(_unit_context([target], candidate_groups, pool)),
            }
        )
    keys = [unit["unit_key"] for unit in units]
    if len(set(keys)) != len(keys):
        raise ValueError("Semantic review units must have unique keys; found duplicates.")
    return [unit for _, unit in sorted(zip(keys, units), key=lambda pair: pair[0])]


# -----------------------------------------------------------------------------
# Packet assembly
# -----------------------------------------------------------------------------


def _producer(function: str) -> dict:
    from . import __version__

    return {"implementation": "metasalmonpy", "version": __version__, "function": function}


def source_policy_payload(source_policy: dict) -> dict:
    """``.ms_semantic_bundle_source_policy_payload()``, as the packet records it.

    ``explicit_allowlist`` is the list the caller gave, as given
    (``.ms_semantic_source_policy()`` keeps it that way); the searches read
    the normalised list :func:`~metasalmonpy.llm_review.make_source_policy`
    builds.
    """
    from .llm_review import policy_sources

    explicit = bool(source_policy.get("explicit"))
    given = source_policy.get("given")
    if given is None:
        given = list(source_policy.get("sources") or ())
    return {
        "mode": "explicit" if explicit else "role_defaults",
        "explicit_allowlist": list(given) if explicit else [],
        "effective_sources_by_role": {
            role: (list(given) if explicit else list(policy_sources(source_policy, role)))
            for role in BUNDLE_ROLES
        },
    }


def _source_policy(sources) -> dict:
    """The source policy, keeping the caller's list as given for the packet."""
    from .llm_review import make_source_policy

    if sources is None:
        return make_source_policy(None)
    given = [sources] if isinstance(sources, str) else list(sources)
    policy = make_source_policy(given)
    policy["given"] = [str(source) for source in given]
    return policy


def _pins(source_policy: dict, sources_used, failed_sources, top_n: int, code_scope: str) -> dict:
    """``.ms_semantic_review_pins()``, stated honestly.

    Nothing pins an ontology today, so ``pinned`` is ``false``; the sources
    searched and the ones that failed are recorded in code-point order. The
    SDP profile version comes from the vendored bundle, never from the
    remote-first loader. ``ranking_identity`` is ``null`` from this package:
    it has no rerank stage (PARITY.md row 39).
    """
    from .sdp_schema import _load_vendored_sdp_schema

    def ordered(values):
        return sorted({text for text in (_r_character(value) for value in values) if text is not None})

    return {
        "sdp_profile": {
            "version": _text_scalar(_load_vendored_sdp_schema().get("version")),
            "source": "vendored",
        },
        "ontologies": {
            "pinned": False,
            "sources": ordered(sources_used),
            "failed_sources": ordered(failed_sources),
        },
        "ranking_identity": None,
        "retrieval": {
            "top_n": int(top_n),
            "code_scope": code_scope,
            "source_policy": source_policy_payload(source_policy),
        },
    }


def _context_object(inputs: list) -> dict:
    """``.ms_semantic_review_context_object()``: the inputs with their SHA-256."""
    return {
        "inputs": [
            {"source": item["source"], "kind": item["kind"], "sha256": item["sha256"]}
            for item in inputs
        ],
        "chunking": dict(CONTEXT_CHUNKING),
    }


def assemble_packet(pass_number: int, parent_packet_id, pins: dict, context: dict, units: list,
                    function: str = "write_semantic_review_packet") -> dict:
    """``.ms_semantic_review_assemble_packet()``: the packet, with its ``packet_id``."""
    packet = {
        "packet_version": PACKET_VERSION,
        "packet_id": None,
        "pass": int(pass_number),
        "parent_packet_id": parent_packet_id,
        "producer": _producer(function),
        "pins": pins,
        "instructions": semantic_review_instructions(),
        "decision_vocabulary": list(DECISION_VOCABULARY),
        "decision_aliases": dict(DECISION_ALIASES),
        "output": {
            "file": "review/" + _review_file(".", "assessments", pass_number).name,
            "columns": output_columns(),
        },
        "context": context,
        "units": list(units),
    }
    packet["packet_id"] = semantic_review_packet_id(packet)
    return packet


def _unit_counts(units: list) -> dict:
    return {"units": len(units), "targets": sum(len(unit["slots"]) for unit in units)}


def write_files(root: Path, writes: Mapping) -> list:
    """Install a set of files atomically after the containment check.

    ``.ms_semantic_review_write_files()`` over ``.ms_sdp_extension_atomic_write_set()``:
    every file is staged first and installed only when all were staged.
    """
    from .package_io import _assert_managed_paths_contained
    from .sdp_methods import _atomic_write_set

    paths = [str(path) for path in writes]
    _assert_managed_paths_contained(Path(root), paths)
    _atomic_write_set({Path(path): data for path, data in writes.items()})
    return paths


# -----------------------------------------------------------------------------
# The package path
# -----------------------------------------------------------------------------


def _contained_resource(root: Path, file_name) -> Optional[Path]:
    """``.ms_semantic_review_contained_resource()``: a data resource read only from inside the package.

    ``tables.csv`` is external text, and a package written by someone else could
    otherwise point the reader at any file the analyst can open. A name is read
    only when it is a plain relative path -- no absolute path, no drive letter,
    no ``.`` or ``..`` component -- with no symbolic link anywhere in it.
    """
    text = scalar_text(file_name)
    if not text:
        return None
    normalized = text.replace("\\", "/")
    if normalized.startswith("/") or (len(normalized) > 1 and normalized[1] == ":" and normalized[0].isalpha()):
        return None
    parts = [part for part in normalized.split("/") if part]
    if not parts or any(part in (".", "..") for part in parts):
        return None
    current = Path(root)
    for part in parts:
        current = current / part
        if current.is_symlink():
            return None
    if not current.exists() or current.is_dir():
        return None
    return current


def _package_resources(path: Path, table_meta: pd.DataFrame, dictionary: pd.DataFrame) -> dict:
    """``.ms_semantic_review_package_resources()``: the data resources, contained."""
    import warnings

    from .package_io import _read_resource_csv

    resources: dict = {}
    if table_meta is None or len(table_meta) == 0 or not {"table_id", "file_name"} <= set(table_meta.columns):
        return resources
    refused = []
    for record in _records(table_meta):
        table_id = scalar_text(record.get("table_id"))
        file_name = scalar_text(record.get("file_name"))
        if not table_id or not file_name:
            continue
        contained = _contained_resource(path, file_name)
        if contained is None:
            refused.append(file_name)
            continue
        table_dictionary = dictionary[dictionary["table_id"].map(scalar_text) == table_id] if "table_id" in dictionary.columns else dictionary
        resources[table_id] = _read_resource_csv(contained, table_dictionary)
    if refused:
        warnings.warn(
            "Some data resources named in tables.csv are not plain files inside the package "
            "and were not read: " + ", ".join(refused) + ". Blank slots are still recovered "
            "from the metadata; code-level slots of these tables may be missed.",
            UserWarning,
            stacklevel=3,
        )
    return resources


def _blank_slots(path: Path, frames: Mapping, suggestions: Optional[pd.DataFrame], code_scope: str) -> dict:
    """``.ms_semantic_review_blank_slots()``: blank slots with no candidates, recovered by discovery.

    ``semantic_suggestions.csv`` holds only candidate rows and a package does
    not persist its targets, so a slot ``create_sdp()`` left blank because
    retrieval found nothing is invisible to the review queue. A blank slot is
    exactly what discovery sees, so the discovery ``create_sdp()`` ran is run
    again over the package's own frames -- ``semantics._semantic_discover_targets()``,
    the discovery :func:`~metasalmonpy.suggest_semantics` runs, called directly
    so the in-package model call stays off this function's call graph -- and
    restricted to the targets of writable IRI slots that are blank (not
    ``REVIEW:``-marked) and carry no recorded decision, and that have no
    suggestion row of their own: a slot another role of which has rows can
    still hold a role that found nothing (hub item B-425). The one thing
    that cannot be recovered is the code scope the caller chose at creation:
    the packet records the scope used here, and a code-level slot outside it is
    reported as not covered rather than silently dropped.
    """
    from .metadata import normalize_codes, normalize_dataset_meta, normalize_dictionary, normalize_table_meta, read_sdp_csv
    from .package_io import _select_semantic_seed_codes
    from .review_console import WRITABLE_FILES
    from .semantics import _semantic_discover_targets

    dictionary = frames.get("column_dictionary.csv")
    empty = {"targets": [], "not_covered": pd.DataFrame()}
    if not isinstance(dictionary, pd.DataFrame) or len(dictionary) == 0:
        return empty
    dictionary = normalize_dictionary(dictionary)
    codes = frames.get("codes.csv")
    codes = normalize_codes(codes) if isinstance(codes, pd.DataFrame) else None
    table_meta = frames.get("tables.csv")
    table_meta = normalize_table_meta(table_meta) if isinstance(table_meta, pd.DataFrame) else pd.DataFrame()
    dataset_path = path / "metadata" / "dataset.csv"
    if not dataset_path.is_file():
        dataset_path = path / "dataset.csv"
    dataset_meta = normalize_dataset_meta(read_sdp_csv(dataset_path)) if dataset_path.is_file() else pd.DataFrame()
    resources = _package_resources(path, table_meta, dictionary)
    dataset_id = None
    for frame in (dataset_meta, dictionary):
        if isinstance(frame, pd.DataFrame) and "dataset_id" in frame.columns and len(frame):
            dataset_id = _trim_string(frame["dataset_id"].iloc[0])
            if dataset_id is not None:
                break
    # Known and decided are asked of the target, not the slot. A code value of a
    # measurement column has three targets in one slot, and a role that found
    # nothing at creation has no row even when another role of the slot does;
    # asked of the slot, that role was never recovered (hub item B-425). A slot
    # with a recorded decision is decided for every role.
    known = set()
    decided = set()
    if suggestions is not None and len(suggestions):
        suggestion_records = _records(suggestions)
        known = {_target_address(record) for record in suggestion_records}
        decided = _decided_slots(suggestion_records)

    def discover(scope: str) -> list:
        scoped_codes = None
        if codes is not None and len(codes):
            scoped_codes = _select_semantic_seed_codes(codes, resources, scope, dataset_id)
        targets = _target_rows(
            pd.DataFrame(_semantic_discover_targets(dictionary, scoped_codes, table_meta, dataset_meta))
        )
        for target in targets:
            target["slot_id"] = _slot_id(target)
        currents = _current_values(targets, frames)
        kept = []
        for target, current in zip(targets, currents):
            target["current_value"] = current
            writable = _r_character(target.get("target_sdp_file")) in WRITABLE_FILES
            iri_field = (_r_character(target.get("target_sdp_field")) or "").endswith("_iri")
            blank = current is not None and not current.strip(" \t\r\n")
            no_row = _target_address(target) not in known and target["slot_id"] not in decided
            if writable and iri_field and blank and no_row:
                kept.append(target)
        return kept

    in_scope = discover(code_scope)
    not_covered = pd.DataFrame()
    if code_scope != "all":
        in_scope_ids = {target["slot_id"] for target in in_scope}
        wider = [
            target
            for target in discover("all")
            if _r_character(target.get("target_scope")) == "code" and target["slot_id"] not in in_scope_ids
        ]
        columns = ["slot_id", "column_name", "code_value", "dictionary_role", "target_sdp_file", "target_sdp_field"]
        not_covered = pd.DataFrame(
            [{column: target.get(column) for column in columns} for target in wider],
            columns=columns,
        )
    return {"targets": in_scope, "not_covered": not_covered}


def _package_targets(path: Path, frames: Mapping, top_n: int, source_policy: dict, search_fn: Callable,
                     code_scope: str) -> dict:
    """``.ms_semantic_review_package_targets()``: the review queue plus the recovered blank slots.

    Exactly the queue :func:`~metasalmonpy.review_console.review_semantics`
    shows -- never a fresh discovery, which would drop every slot
    ``create_sdp()`` pre-filled with a ``REVIEW:`` marker -- each target
    re-retrieved at depth ``top_n`` with its recorded query, once per distinct
    query, role and source set.
    """
    from .review_console import _review_queue, semantic_suggestions
    from .semantics import (
        _retrieve_semantic_target_candidates,
        _search_once_per_call,
        _semantic_flag_role_collisions,
    )
    from .term_search import _search_failed_sources

    existing = semantic_suggestions(str(path))
    if existing is None or len(existing) == 0:
        queue = {"review": pd.DataFrame(), "suggestions": pd.DataFrame(), "source_row": []}
    else:
        queue = _review_queue(str(path), include_filled=False, columns=None)
    review = queue["review"]
    targets: list = []
    if len(review):
        rows = _records(queue["suggestions"].iloc[list(queue["source_row"])])
        seen = set()
        # One target per slot and role, never one per slot: the console lists a
        # code's constraint, entity and method candidates in one slot, and each
        # role is its own target with its own shortlist (hub item B-425).
        for record, (slot_id, role, current) in zip(
            rows, zip(review["slot_id"], review["role"], review["current_value"])
        ):
            address = _target_address({"slot_id": slot_id, "dictionary_role": role})
            if address in seen:
                continue
            seen.add(address)
            # The CSV reader keeps an empty field as "", where metasalmon's
            # reads it as NA (PARITY.md row 21); a target is built from the
            # suggestion row as R builds it, an empty field missing.
            target = {
                column: (None if isinstance(record.get(column), str) and record.get(column) == "" else record.get(column))
                for column in SEMANTIC_TARGET_COLUMNS
            }
            targets.append(target)
            target["slot_id"] = _slot_id(target)
            target["current_value"] = None if _is_missing(current) else current
        if "search_role" not in queue["suggestions"].columns or all(
            _is_missing(target.get("search_role")) for target in targets
        ):
            for target in targets:
                target["search_role"] = target.get("dictionary_role")
    blank = _blank_slots(path, frames, queue["suggestions"], code_scope)
    known = {_target_address(target) for target in targets}
    targets.extend(target for target in blank["targets"] if _target_address(target) not in known)
    if not targets:
        return {"targets": targets, "candidates": [], "failed_sources": [], "not_covered": blank["not_covered"]}

    failed: list = []

    def recording(query, role=None, sources=None):
        result = search_fn(query, role=role, sources=sources)
        attrs = getattr(result, "attrs", None)
        diagnostics = attrs.get("diagnostics") if isinstance(attrs, Mapping) else None
        for source in _search_failed_sources(diagnostics):
            if source not in failed:
                failed.append(source)
        return result

    search_once = _search_once_per_call(recording)
    shortlists = []
    for target in targets:
        retrieved = _retrieve_semantic_target_candidates(
            {column: target.get(column) for column in SEMANTIC_TARGET_COLUMNS},
            source_policy,
            top_n,
            search_once,
            retrieval_pass=1,
        )
        if len(retrieved):
            shortlists.append(retrieved)
    candidates: list = []
    if shortlists:
        combined = pd.concat(shortlists, ignore_index=True, sort=False)
        candidates = _records(_semantic_flag_role_collisions(combined))
    return {"targets": targets, "candidates": candidates, "failed_sources": failed, "not_covered": blank["not_covered"]}


# -----------------------------------------------------------------------------
# The exporter
# -----------------------------------------------------------------------------


def write_semantic_review_packet(
    x,
    context_files=None,
    context_text=None,
    top_n: int = 5,
    sources: Optional[Sequence[str]] = None,
    search_fn: Callable = find_terms,
    code_scope: str = "factor",
    review_dir: Optional[Union[str, os.PathLike]] = None,
    overwrite: bool = False,
    quiet: bool = False,
) -> dict:
    """Write a semantic review packet for a harness to judge.

    Model judgement runs outside metasalmonpy (ruled 2026-09-25, hub Q67). This
    writes the deterministic file a harness reads: every semantic slot that
    still needs a decision, each slot's ranked candidate shortlist with the
    evidence the package's own validators read (label, IRI, source, ontology,
    native type, role hints, term type, resource kind, type IRIs, definition
    and scores), the measurement bundles with their current slots, the scored
    excerpts from your context documents, the review instructions, the
    decision vocabulary and the 30-column assessment schema. The harness
    answers in a CSV next to the packet, and
    :func:`~metasalmonpy.ingest_semantic_assessments` reads it back.

    **This never calls a model.** For a package path it retrieves each slot's
    shortlist again through ``search_fn``, which is the only way it reaches the
    network; for in-memory input it makes no network call at all.

    For a package path the packet holds exactly the queue
    :func:`~metasalmonpy.review_semantics` would show -- slots in
    ``column_dictionary.csv``, ``codes.csv`` or ``tables.csv`` whose IRI field
    is blank or ``REVIEW:``-marked and that carry no recorded decision -- plus
    the blank slots discovery recovers because retrieval found nothing for them
    at creation. For an in-memory dictionary it holds every target in the
    ``semantic_targets`` attribute, including targets with no candidates, with
    the first ``top_n`` candidates of each from ``semantic_suggestions``.

    The packet holds excerpts from your own context documents. It is written
    under ``review/``, which no publication path reads, and it is never
    published. Recovering blank slots for a package path reads its data
    resources only when ``tables.csv`` names a plain relative path inside the
    package; any other row is skipped with a warning.

    Parameters
    ----------
    x
        A package directory written by :func:`~metasalmonpy.create_sdp`, or a
        dictionary carrying the ``semantic_targets`` and
        ``semantic_suggestions`` attributes :func:`~metasalmonpy.suggest_semantics`
        attaches (an artifact mapping is read through its ``dict``).
    context_files
        Local file paths whose text is chunked and scored against each unit,
        under the same path-only contract as ``llm_context_files``: a parsed
        object is refused.
    context_text
        Inline context text: a string or a sequence of strings.
    top_n
        The shortlist shown per slot and, for a package path, the retrieval
        depth. Default 5.
    sources
        Vocabulary sources. ``None`` searches each role's defaults
        (:func:`~metasalmonpy.sources_for_role`); a list is a strict allowlist.
    search_fn
        The search function, used only for a package path. Defaults to
        :func:`~metasalmonpy.find_terms`.
    code_scope
        Which ``codes.csv`` values get a slot when a blank slot is recovered by
        discovery for a package path: ``"factor"`` (the default, as
        ``create_sdp()`` seeds), ``"all"`` or ``"none"``. Nothing in a package
        records the scope it was created with, so the packet records the one
        used here, and a blank code-level slot outside it is reported in
        ``not_covered`` rather than silently dropped.
    review_dir
        Where to write the packet. Defaults to ``review/`` under the package
        directory; required for in-memory input.
    overwrite
        A review session that already holds answers or a record refuses to be
        overwritten unless this is ``True``, in which case the session's files
        are deleted first.
    quiet
        Suppress the summary.

    Returns
    -------
    dict
        ``path`` (the packet file), ``packet_id`` (the SHA-256 the ingester
        checks), ``pass`` (always 1 here), ``units`` and ``targets`` (counts),
        and ``not_covered``: the blank code-level slots the chosen
        ``code_scope`` left out, one row each (empty for in-memory input).
    """
    from .llm_review import collect_context
    from .review_console import _review_source_frames

    review_input = _review_input(x, review_dir)
    if code_scope not in _CODE_SCOPES:
        raise ValueError(f"code_scope must be one of {', '.join(_CODE_SCOPES)}; got {code_scope!r}.")
    try:
        top_n_value = int(top_n[0] if isinstance(top_n, (list, tuple)) else top_n)
    except (TypeError, ValueError):
        top_n_value = 0
    if top_n_value < 1:
        raise ValueError("top_n must be a positive whole number.")
    source_policy = _source_policy(sources)
    review_dir = review_input["review_dir"]

    existing = [path for path in _session_files(review_dir) if path.exists()]
    answered = [path for path in existing if path != _review_file(review_dir, "packet", 1)]
    if answered and overwrite is not True:
        raise FileExistsError(
            f"A semantic review session already exists in {review_dir}: "
            + ", ".join(path.name for path in answered)
            + ". Pass overwrite=True to delete this session's files and start again."
        )

    pool, context_inputs = collect_context(context_files, context_text)

    source = review_input["path"] if review_input["kind"] == "package" else review_input["object"]
    frames = _review_source_frames(str(source) if review_input["kind"] == "package" else source)
    dictionary = frames.get("column_dictionary.csv")
    if review_input["kind"] == "package":
        found = _package_targets(review_input["path"], frames, top_n_value, source_policy, search_fn, code_scope)
    else:
        found = _memory_targets(review_input["dict"], top_n_value)
        for target, current in zip(found["targets"], _current_values(found["targets"], frames)):
            target["current_value"] = current
    targets = found["targets"]
    candidates = found["candidates"]
    if review_input["kind"] == "package":
        # The sources each searched role reads under the policy, as
        # ``.ms_sources_for_target_role()`` gives them.
        by_role = source_policy_payload(source_policy)
        sources_used = []
        for target in targets:
            role = target.get("search_role")
            role = target.get("dictionary_role") if _is_missing(role) else role
            if source_policy["explicit"]:
                sources_used.extend(by_role["explicit_allowlist"])
            else:
                from .term_search import sources_for_role

                sources_used.extend(sources_for_role(_r_character(role) or ""))
    else:
        sources_used = [row.get("source") for row in candidates if not _is_missing(row.get("source"))]

    units = assemble_units(targets, candidates, dictionary, pool)
    packet = assemble_packet(
        pass_number=1,
        parent_packet_id=None,
        pins=_pins(source_policy, sources_used, found["failed_sources"], top_n_value, code_scope),
        context=_context_object(context_inputs),
        units=units,
    )

    review_dir.mkdir(parents=True, exist_ok=True)
    if overwrite is True and existing:
        from .package_io import _assert_managed_paths_contained

        _assert_managed_paths_contained(review_dir, [str(path) for path in existing])
        for path in existing:
            path.unlink(missing_ok=True)
    packet_path = _review_file(review_dir, "packet", 1)
    write_files(review_dir, {packet_path: semantic_review_canonical_bytes(packet)})

    counts = _unit_counts(units)
    not_covered = found.get("not_covered")
    not_covered = pd.DataFrame() if not_covered is None else not_covered
    if not quiet:
        print(f"Wrote semantic review packet {packet_path}.")
        print(
            f"  {counts['units']} unit{'s' if counts['units'] != 1 else ''} holding "
            f"{counts['targets']} target{'s' if counts['targets'] != 1 else ''}; "
            f"packet_id {packet['packet_id']}."
        )
        if len(not_covered):
            print(
                f"  {len(not_covered)} blank code-level slot{'s' if len(not_covered) != 1 else ''} "
                f"{'is' if len(not_covered) == 1 else 'are'} outside code_scope={code_scope!r} and "
                "not in the packet; see not_covered."
            )
        print(
            f"  Have your harness write {packet['output']['file']} next to it, then call "
            "ingest_semantic_assessments()."
        )
    return {
        "path": str(packet_path),
        "packet_id": packet["packet_id"],
        "pass": 1,
        "units": counts["units"],
        "targets": counts["targets"],
        "not_covered": not_covered,
    }
