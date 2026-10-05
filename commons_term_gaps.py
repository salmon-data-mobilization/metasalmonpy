"""Read and draft requests from the Salmon Knowledge Commons gap register.

The register describes concepts, not SDP columns. Keep its lifecycle and
provenance separate from candidate-search evidence (hub B-278/B-279).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pandas as pd

from .text_safety import redact_secrets


COMMONS_FIELDS = (
    "concept", "title", "context", "registry", "status", "mint_target",
    "state", "proposal", "rejected_because", "evidence_needed", "blocked_by",
    "conflicts", "note", "card_status", "verified",
)
COMMONS_COLUMNS = ["commons_" + field for field in COMMONS_FIELDS] + ["commons_hold_reason"]
_ENUMS = {
    "context": {"biology", "ecology", "population-structure", "assessment", "management-governance", "data-informatics"},
    "registry": {"smn", "gcdfo", "psc-cv", "external"},
    "status": {"no-term", "wrong-granularity", "contested"},
    "mint_target": {"smn", "gcdfo", "psc-cv", "new-scheme", "do-not-mint", "undecided"},
    "state": {"open", "proposed", "rejected"},
    "card_status": {"draft", "stable", "deprecated"},
}
_NULLABLE_TEXT = ("proposal", "rejected_because", "evidence_needed", "conflicts")
_URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def _commons_error(detail: object) -> ValueError:
    """Capture an external parser diagnostic only after credential redaction."""
    return ValueError("Invalid `commons_gaps` export. " + redact_secrets(detail))


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise _commons_error(f"Duplicate JSON object key: {key}")
        result[key] = value
    return result


def _validate_commons_gap(row: object) -> dict:
    if type(row) is not dict or set(row) != set(COMMONS_FIELDS):
        raise _commons_error("Each record must be an object with the emitted commons fields.")
    for field, allowed in _ENUMS.items():
        value = row[field]
        if type(value) is not str or value not in allowed:
            raise _commons_error(f"Invalid enum or type in {field}.")
    for field in ("concept", "title", "note"):
        value = row[field]
        minimum = 40 if field == "note" else 1
        if type(value) is not str or len(value) < minimum:
            raise _commons_error(f"Invalid string in {field}.")
    for field in _NULLABLE_TEXT:
        value = row[field]
        minimum = 1 if field == "proposal" else 40
        if value is not None and (type(value) is not str or len(value) < minimum):
            raise _commons_error(f"Invalid nullable string in {field}.")
    if row["proposal"] is not None and not _URI_SCHEME.match(row["proposal"]):
        raise _commons_error("A proposal must be an absolute URI.")
    if row["state"] in {"proposed", "rejected"} and row["proposal"] is None:
        raise _commons_error("Proposed and rejected gaps require a proposal URI.")
    if row["state"] == "rejected" and (
        row["rejected_because"] is None or row["evidence_needed"] is None
    ):
        raise _commons_error("Rejected gaps require rejection reasons and evidence needed.")
    if type(row["verified"]) is not bool:
        raise _commons_error("verified must be a JSON boolean.")
    blocked = row["blocked_by"]
    if type(blocked) is not list or any(type(item) is not str or not item for item in blocked):
        raise _commons_error("blocked_by must be an array of nonempty concept strings.")
    return row


def _commons_hold_reason(row: dict) -> str:
    """A lifecycle hold is never removed by renderer scope controls.

    Retires when a reviewed successor routing contract replaces these
    conditions in both packages.
    """
    reasons = []
    if row["state"] == "proposed":
        reasons.append("proposed")
    if row["state"] == "rejected":
        reasons.append("rejected")
    if row["mint_target"] == "do-not-mint":
        reasons.append("do-not-mint")
    if row["blocked_by"]:
        reasons.append("blocked")
    if row["conflicts"] is not None:
        reasons.append("conflicted")
    if row["status"] == "contested":
        reasons.append("contested")
    if row["evidence_needed"] is not None:
        reasons.append("evidence-needed")
    if row["mint_target"] in {"psc-cv", "new-scheme", "undecided"}:
        reasons.append("unsupported-target")
    if row["card_status"] == "deprecated":
        reasons.append("deprecated-card")
    return "; ".join(reasons)


def _read_commons_term_gaps(path: object, gap_columns: list[str]) -> pd.DataFrame:
    if not isinstance(path, (str, os.PathLike)) or not str(path):
        raise _commons_error("commons_gaps must name one existing JSON export file.")
    try:
        source = Path(path)
        if not source.is_file():
            raise _commons_error("commons_gaps must name one existing JSON export file.")
        with source.open(encoding="utf-8") as stream:
            document = json.load(stream, object_pairs_hook=_no_duplicate_keys)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise _commons_error(error) from None
    if type(document) is not dict or set(document) != {"gaps"} or type(document["gaps"]) is not list:
        raise _commons_error("Expected an object containing the top-level gaps array.")
    rows = [_validate_commons_gap(row) for row in document["gaps"]]
    output = []
    for row in rows:
        record = {column: pd.NA for column in gap_columns}
        for field in COMMONS_FIELDS:
            record["commons_" + field] = row[field]
        hold = _commons_hold_reason(row)
        record["commons_hold_reason"] = hold
        record["search_query"] = row["title"]
        record["target_label"] = row["title"]
        record["gap_detection_basis"] = "commons_register"
        record["placement_recommendation"] = "skip" if hold else row["mint_target"]
        output.append(record)
    frame = pd.DataFrame(output, columns=gap_columns + COMMONS_COLUMNS)
    # An empty export still has a string placement column, just as the R
    # companion returns character(0) rather than a logical empty vector.
    frame["placement_recommendation"] = frame["placement_recommendation"].astype("string")
    return frame


def _source_row(gaps: pd.DataFrame, position: int) -> dict:
    row = {}
    for field in COMMONS_FIELDS:
        value = gaps.iloc[position]["commons_" + field]
        if field in _NULLABLE_TEXT and (value is None or value is pd.NA or (
            isinstance(value, float) and pd.isna(value)
        )):
            value = None
        elif field == "verified" and type(value).__module__ == "numpy" and type(value).__name__ in {"bool", "bool_"}:
            value = bool(value)
        row[field] = value
    return _validate_commons_gap(row)


def _render_commons_term_requests(
    gaps: pd.DataFrame,
    *,
    issue_labels,
    smn_template: str,
    smn_repo: str,
    gcdfo_template: str,
    gcdfo_repo: str,
) -> pd.DataFrame:
    if not set(COMMONS_COLUMNS[:-1]).issubset(gaps.columns):
        raise _commons_error("Rendered commons rows must retain all source fields.")
    if gaps.empty:
        return pd.DataFrame()
    rows = [_source_row(gaps, position) for position in range(len(gaps))]
    holds = [_commons_hold_reason(row) for row in rows]
    scopes = ["skip" if hold else row["mint_target"] for row, hold in zip(rows, holds)]
    output = gaps.copy()
    output["commons_hold_reason"] = holds
    output["request_scope"] = scopes
    output["profile_name"] = pd.NA
    output["request_title"] = [
        ("Hold commons ontology gap: " if scope == "skip" else "Commons ontology gap: ") + row["title"]
        for row, scope in zip(rows, scopes)
    ]
    output["ontology_repo"] = [
        smn_repo if scope == "smn" else gcdfo_repo if scope == "gcdfo" else pd.NA
        for scope in scopes
    ]

    def show(value: object) -> str:
        if value is None or value == []:
            return "None"
        if isinstance(value, list):
            return ", ".join(value)
        return str(value)

    bodies = []
    for row, scope, hold in zip(rows, scopes, holds):
        template = smn_template if scope == "smn" else gcdfo_template if scope == "gcdfo" else "Not routed"
        repository = smn_repo if scope == "smn" else gcdfo_repo if scope == "gcdfo" else None
        body = (
            "## Commons ontology gap\n\n"
            f"Concept card: `{row['concept']}.md` in salmon-knowledge-commons.\n"
            f"Concept title: {row['title']}\nContext: {row['context']}\n\n"
            f"## Draft gap evidence\n\n{row['note']}\n\n"
            "## Curator choices required\n\nCurator definition required.\n"
            "Curator term type required.\nNo term IRI, definition or type is selected by this request.\n\n"
            "## Provenance and lifecycle\n\n"
            f"- Registry with gap: {row['registry']}\n- Proposed mint target: {row['mint_target']}"
            f"\n- State: {row['state']}\n- Gap status: {row['status']}"
            f"\n- Card status: {row['card_status']}\n- Verified: {str(row['verified']).lower()}"
            f"\n- Existing proposal: {show(row['proposal'])}"
            f"\n- Rejection reason: {show(row['rejected_because'])}"
            f"\n- Evidence needed: {show(row['evidence_needed'])}"
            f"\n- Blocked by: {show(row['blocked_by'])}\n- Conflicts: {show(row['conflicts'])}"
            f"\n- Hold reasons: {hold or 'None'}"
            f"\n- Request route: {scope}\n- Template: {template}"
            f"\n- Repository: {'https://github.com/' + repository if repository else 'Not routed'}\n"
        )
        bodies.append(body)
    output["request_body"] = bodies
    output["issue_labels"] = [issue_labels for _ in rows]
    return output
