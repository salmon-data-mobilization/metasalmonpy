"""Surgical metadata write-back for the Python review flow (stream S5).

Mirrors ``R/metadata-write.R``.

:func:`write_salmon_datapackage` rewrites a whole package from in-memory
objects. A review decision is a much smaller edit: change the decided cells in
the metadata CSVs and leave everything else -- above all the data CSV bytes --
exactly as it was. That byte assertion is the point of "surgical", and it is
the only one that fails if this ever regresses into a full rewrite.

ATOMICITY IS CROSS-FILE, NOT PER-FILE. One :func:`apply_sdp_semantics` call can
change ``metadata/column_dictionary.csv``, ``metadata/codes.csv``,
``metadata/tables.csv``, ``semantic_suggestions.csv`` AND the field entries
``datapackage.json`` duplicates. Replacing each atomically still leaves a window
where the CSV is new and the descriptor is old, and the rule that would catch
that drift (``datapackage_consistent_with_csv_metadata``) is one of the dead
rules in ``sdp.rules.yaml`` -- nothing would detect it. So every affected file
is rendered to bytes first and installed as one set by
``_commit_package_write()``, which stages every replacement before moving any
current file and restores the originals on failure.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import pandas as pd

from .metadata import read_sdp_csv
from .review_console import (
    SemanticReview,
    WRITABLE_FILES,
    _assert_review,
    _review_match_rows,
    _review_slot_id,
    _strip_review_iri,
    _text,
    review_decisions,
    review_target_keys,
)
from .semantics import _infer_term_type

__all__ = ["apply_sdp_semantics"]


def apply_sdp_semantics(
    path, review: SemanticReview, quiet: bool = False
) -> Path:
    """Write semantic review decisions into a package.

    Applies the decisions recorded by :func:`accept_suggestion` and
    :func:`reject_suggestion` to a written Salmon Data Package. Accepted IRIs
    are written with the ``REVIEW:`` prefix stripped; rejected slots are
    cleared; every other field, and every data CSV byte, is left untouched.

    Safe to re-run: applying the same review twice produces identical bytes.
    Only fields carrying a decision are written, so undecided slots keep their
    ``REVIEW:`` markers.

    The metadata CSVs, ``semantic_suggestions.csv`` and ``datapackage.json`` are
    installed as one transactional set -- the descriptor duplicates the
    dictionary's IRI fields, and a half-applied edit would leave the package
    quietly self-inconsistent.

    Parameters
    ----------
    path
        Path to the package directory.
    review
        A :class:`SemanticReview` carrying decisions.
    quiet
        Suppress the summary message.

    Returns
    -------
    pathlib.Path
        The package path.
    """
    # Imported here rather than at module scope: ``package_io`` is the larger
    # module and importing it eagerly would make this module a cycle in its
    # own import graph (``package_io`` reaches the review flow for the
    # ``semantic_suggestions.csv`` read-back).
    from .package_io import (
        _assert_managed_paths_contained,
        _commit_package_write,
        _datapackage_json_bytes,
        _descriptor_sync_fields,
        _metadata_csv_bytes,
        _metadata_path,
    )

    _assert_review(review)
    target = Path(path)
    if not target.is_dir():
        raise NotADirectoryError(
            f"path must be an existing Salmon Data Package directory: {target}"
        )

    decisions = review_decisions(review)
    if decisions.empty:
        if not quiet:
            print(
                "No decisions to apply. Record some with accept_suggestion() "
                "or reject_suggestion() first."
            )
        return target

    target_files = list(dict.fromkeys(decisions["target_file"].map(_text)))
    unsupported = [name for name in target_files if name not in WRITABLE_FILES]
    if unsupported:
        raise ValueError(
            "Cannot write decisions for these metadata files: "
            + ", ".join(unsupported)
        )

    # Containment BEFORE any read or write, matching
    # ``write_salmon_datapackage()``: a ``metadata/`` replaced by a symlink
    # would otherwise be read, and then written through, outside the package.
    managed = [_metadata_path(target, name) for name in WRITABLE_FILES] + [
        target / "semantic_suggestions.csv",
        target / "datapackage.json",
    ]
    _assert_managed_paths_contained(target, managed)

    frames = {}
    paths = {}
    for file_name in target_files:
        located = _metadata_path(target, file_name)
        if not located.is_file():
            raise FileNotFoundError(
                "Decisions target a metadata file the package does not have: "
                + file_name
            )
        frames[file_name] = read_sdp_csv(located)
        paths[file_name] = located

    applied = 0
    changed_columns = []

    for position in range(len(decisions)):
        row = decisions.iloc[position]
        file_name = _text(row["target_file"])
        field = _text(row["target_field"])
        frame = frames[file_name]
        keys = review_target_keys(file_name)

        if field not in frame.columns:
            frame[field] = pd.NA
        hits = _review_match_rows(frame, row, keys)
        if len(hits) != 1:
            raise ValueError(
                "A decision does not address exactly one metadata row: "
                + file_name
                + " · "
                + _text(row["target_row_key"])
                + f" matched {len(hits)} rows. Rebuild the review from the "
                "package you are writing to."
            )
        index = hits[0]

        accepted = _text(row["decision"]) == "accept"
        frame.at[index, field] = (
            _strip_review_iri(row["decision_iri"]) if accepted else pd.NA
        )

        # ``term_type`` is the dictionary's declaration of what kind of thing
        # ``term_iri`` names. ``apply_semantic_suggestions()`` infers it
        # whenever it writes a ``term_iri``; a reviewed write that skipped it
        # would leave the two disagreeing, and clearing the IRI without
        # clearing the type would leave a type describing nothing.
        if (
            file_name == "column_dictionary.csv"
            and field == "term_iri"
            and "term_type" in frame.columns
        ):
            if not accepted:
                frame.at[index, "term_type"] = pd.NA
            elif _text(row["iri"]) == _text(row["decision_iri"]):
                # ``term_type`` describes the candidate, and this decision IS
                # that candidate.
                frame.at[index, "term_type"] = row["term_type"]
            else:
                # A hand-supplied ``iri=`` rather than a shortlisted candidate:
                # the candidate row the decision was recorded on describes a
                # *different* term, so its type is not evidence about this one.
                frame.at[index, "term_type"] = "skos_concept"

        frames[file_name] = frame
        applied += 1
        if file_name == "column_dictionary.csv":
            changed_columns.append(
                {
                    "table_id": _text(row["table_id"]),
                    "column_name": _text(row["column_name"]),
                }
            )

    # In the order of the schema the settings select, as metasalmon's apply
    # aligns through `.ms_dictionary_cols()` and its siblings, which read the
    # session schema. Deferred, as `sdp_field_setters` already reaches back
    # into this module the same way.
    from .sdp_field_setters import _in_declared_order

    writes = {}
    for file_name, frame in frames.items():
        writes[paths[file_name]] = _metadata_csv_bytes(
            _in_declared_order(frame, file_name)
        )

    # The descriptor duplicates the dictionary's IRI fields, so it is part of
    # the same logical edit. Patched surgically rather than rebuilt: a rebuild
    # would have to re-derive every resource entry from metadata this call did
    # not read, and would silently discard descriptor content a user added.
    descriptor_path = target / "datapackage.json"
    if (
        descriptor_path.is_file()
        and "column_dictionary.csv" in frames
        and changed_columns
    ):
        descriptor = _read_descriptor(descriptor_path)
        descriptor = _descriptor_sync_fields(
            descriptor,
            frames["column_dictionary.csv"],
            pd.DataFrame(changed_columns),
        )
        writes[descriptor_path] = _datapackage_json_bytes(descriptor)

    suggestions_write = _record_decisions(target, decisions)
    if suggestions_write is not None:
        writes[target / "semantic_suggestions.csv"] = suggestions_write

    _commit_package_write(
        target, writes, managed_paths=list(writes), prune=False
    )

    if not quiet:
        plural = "" if applied == 1 else "s"
        print(f"Applied {applied} semantic review decision{plural} to {target}.")
        print(
            "  Check the result with "
            "validate_salmon_datapackage(path, require_iris=True)."
        )
    return target


def _read_descriptor(descriptor_path: Path) -> dict:
    try:
        with descriptor_path.open("r", encoding="utf-8") as stream:
            return json.load(stream)
    except json.JSONDecodeError as error:
        # The parser's message quotes the file's own bytes, so it is external
        # text: redacted at capture, per ``text_safety``'s placement rule.
        from .text_safety import redact_secrets

        raise ValueError(
            "Could not parse datapackage.json: " + redact_secrets(error)
        ) from None


#: The columns that say WHICH metadata cell a suggestion row is about. A
#: recorded hand-picked accept copies exactly these from the shortlist it
#: replaces, so ``review_semantics()`` addresses and displays it the same way it
#: addresses a retrieved candidate. Everything else describes the *candidate* and
#: is left empty, because nothing is known about a term the user typed.
_SLOT_ADDRESS_COLUMNS = (
    "dataset_id",
    "table_id",
    "column_name",
    "code_value",
    "dictionary_role",
    "target_scope",
    "target_sdp_file",
    "target_sdp_field",
    "target_row_key",
)

#: What ``source`` says on a recorded hand-picked accept. ``source`` otherwise
#: names the vocabulary a candidate was retrieved from, and leaving it empty
#: would let the row read as a candidate from an unnamed source rather than as
#: one the user supplied.
_HAND_PICKED_SOURCE = "user"


def _is_hand_picked(suggestions: pd.DataFrame) -> pd.Series:
    """Mark the rows of a suggestions table that record a hand-picked accept.

    Such a row is a reviewer's decision rather than retrieval output, which is
    why :func:`~metasalmonpy.term_requests.detect_semantic_term_gaps` drops it.
    ``source`` is compared trimmed and lower-cased, as that function normalises
    it, and an empty ``source`` is never a recorded accept. The counterpart of
    metasalmon's ``.ms_review_is_hand_picked()``.
    """
    if "source" not in suggestions:
        return pd.Series(False, index=suggestions.index)
    source = suggestions["source"].fillna("").astype(str).str.lower().str.strip()
    return source == _HAND_PICKED_SOURCE


def _with_hand_picked_accept(
    suggestions: pd.DataFrame, in_slot, accepted_iri: str
) -> pd.DataFrame:
    """Record an accepted IRI that no candidate row carries.

    The slot gains a NEW row rather than an existing candidate being relabelled.
    Marking a candidate ``accepted`` when its own ``iri`` was not the one accepted
    would be a worse record than the missing one: the shortlist rows are accurate
    as they stand -- none of them was selected -- and the thing with no row is the
    term the user supplied. So it gets one.

    It is inserted at the HEAD of its slot, not at the end of the file, because
    ``review_semantics()`` derives ``rank`` from file position and then drops
    everything past ``max_candidates`` (5 by default). Appended after a full
    shortlist the recorded accept would rank 6 and be filtered straight back out,
    losing the same decision one layer further on. At the head it ranks 1, which
    is also what makes a replayed ``accept_suggestion(..., rank=1)`` re-accept the
    term that was actually chosen. The retrieved candidates keep their relative
    order, so this is a position and not a re-ranking.
    """
    head = suggestions[in_slot].iloc[0]
    record = {name: pd.NA for name in suggestions.columns}
    for name in _SLOT_ADDRESS_COLUMNS:
        if name in suggestions.columns:
            record[name] = head[name]
    record["iri"] = accepted_iri
    record["source"] = _HAND_PICKED_SOURCE
    record["decision"] = "accepted"
    record["decision_reason"] = pd.NA

    at = suggestions.index.get_loc(suggestions.index[in_slot][0])
    return pd.concat(
        [
            suggestions.iloc[:at],
            pd.DataFrame([record], columns=suggestions.columns),
            suggestions.iloc[at:],
        ],
        ignore_index=True,
    )


def _record_decisions(target: Path, decisions: pd.DataFrame) -> Optional[bytes]:
    """The decision record on disk, rendered to bytes.

    ``apply_semantic_suggestions(strategy="reviewed")`` has always filtered a
    ``decision`` column that nothing wrote -- this is that missing producer, and
    it is what makes the decision survive in the package rather than only in
    the user's script.
    """
    from .package_io import _metadata_csv_bytes

    suggestions_path = target / "semantic_suggestions.csv"
    if not suggestions_path.is_file():
        return None
    suggestions = read_sdp_csv(suggestions_path)
    needed = ("target_sdp_file", "target_row_key", "target_sdp_field", "iri")
    if not all(name in suggestions.columns for name in needed):
        return None

    # Existing decisions are PRESERVED, not reset. Blanking the column first
    # made the file a record of the last review object rather than of the
    # package: a reviewer who rejected four slots on Monday and accepted two
    # more on Tuesday lost Monday's four, because the Tuesday review object did
    # not carry them. Every slot this call decides is rewritten whole
    # (including the ``not_selected`` siblings), so preserving costs nothing and
    # only slots nobody touched keep their earlier answer.
    if "decision" not in suggestions.columns:
        suggestions["decision"] = pd.NA
    # ``decision_reason`` is the "why", and it is the field this feature's whole
    # thesis is about. It lived only on the in-memory review object and printed
    # to the console; the bare word ``rejected`` was all that reached disk, so
    # the one thing a later reader most needs -- why no candidate fitted -- was
    # the one thing not recorded.
    if "decision_reason" not in suggestions.columns:
        suggestions["decision_reason"] = pd.NA

    for position in range(len(decisions)):
        row = decisions.iloc[position]
        # Recomputed per decision because a hand-picked accept INSERTS a row
        # below, which invalidates every earlier mask and index.
        slots = _review_slot_id(suggestions)
        in_slot = slots == _text(row["slot_id"])
        if not in_slot.any():
            continue
        if _text(row["decision"]) == "reject":
            suggestions.loc[in_slot, "decision"] = "rejected"
            suggestions.loc[in_slot, "decision_reason"] = _text(
                row["decision_reason"]
            )
            continue
        accepted_iri = _text(row["decision_iri"])
        accepted = in_slot & (
            suggestions["iri"].map(_strip_review_iri) == accepted_iri
        )
        suggestions.loc[in_slot, "decision"] = "not_selected"
        suggestions.loc[in_slot, "decision_reason"] = pd.NA
        if accepted.any():
            suggestions.loc[accepted, "decision"] = "accepted"
        else:
            # ``accept_suggestion(iri=...)`` is the supported escape hatch for a
            # term retrieval never surfaced, and the shortlist match was the only
            # way an ``accepted`` row was ever written. So a hand-picked IRI left
            # the mask empty: every candidate was marked ``not_selected``, nothing
            # was marked ``accepted``, and the acceptance survived only in the
            # user's script -- while the metadata CSV had it. That is the audit
            # trail this file exists to be, and the decision that cannot be
            # replayed by ``review_semantics(include_filled=True)``.
            suggestions = _with_hand_picked_accept(
                suggestions, in_slot, accepted_iri
            )

    # ``""`` and a missing value share the empty CSV field, so a reason that was
    # never given round-trips as absent rather than as an empty string.
    blank_reason = suggestions["decision_reason"].notna() & (
        suggestions["decision_reason"].map(_text) == ""
    )
    suggestions.loc[blank_reason, "decision_reason"] = pd.NA
    return _metadata_csv_bytes(suggestions)
