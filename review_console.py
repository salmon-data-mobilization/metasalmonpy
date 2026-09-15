"""Python-native semantic review (mirrors ``R/review-console.R``, stream S5).

The documented review workflow used to leave Python: open
``metadata/column_dictionary.csv`` in a spreadsheet, read
``semantic_suggestions.csv`` as a shortlist, copy an IRI across by hand. The
only record of that decision was the mutated CSV -- the one unreproducible link
in a chain that is otherwise byte-reproducible and guarded.

THE DESIGN DECISION THAT DEFINES THIS MODULE (metasalmon execplan decision log,
2026-08-11; restated by Brett 2026-08-25): the console prints the exact
``accept_suggestion(...)`` call and the user pastes it into a script. There is
no TUI, no ``input()`` loop, no menu. **The paste IS the audit trail** -- an
interactive prompt would make the decision as unreproducible as the spreadsheet
it replaces.

That makes the printed call load-bearing rather than decorative. A printed call
that does not parse, or that names a column that does not exist, is the defect
this feature could most easily ship with, so :func:`_review_call_args` computes
the argument set by *resolving it* and ``tests/test_review_console.py``
``exec()``s the printed string and checks it produces the decision it claims.

**The printed call is Python, not R.** R prints
``review <- accept_suggestion(review, "x", "variable", rank = 1)``; this prints
``review = accept_suggestion(review, "x", "variable", rank=1)``. That is the
mirror contract's "simple language difference that does not materially change
behaviour": a Python package that printed R syntax would print a line its own
user cannot paste, which is the one property the feature exists to have.

WHY THIS MODULE DOES NOT ESCAPE ITS EXTERNAL TEXT. Ontology labels,
definitions and LLM rationales are third-party text, and ``AGENTS.md`` requires
that such text never reach a template layer. This module satisfies that by not
putting the text on a template path at all: :func:`_render_review_lines`
returns a plain list of strings that ``print()`` and ``__str__`` emit verbatim,
with no ``%``, ``str.format`` or f-string interpolation of external values
anywhere on that path. Escaping there would be actively wrong -- a definition
containing ``{reach}`` would print as ``{{reach}}``, corrupting exactly the text
the rule exists to protect. Escaping is the mechanism for the *message* path,
and there Python needs no escape at all: an exception message is a finished
string and this module applies no formatter to external values, which is
exactly what ``PARITY.md`` row 37 records as the reason the escape half of
``R/cli-safety.R`` has no counterpart here. The plain-text branch still gets
its own pinned test ("a definition containing braces prints literally"),
because a static guard cannot see a path it does not model.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence, Union

import pandas as pd

from .metadata import read_sdp_csv, scalar_text
from .semantics import _infer_term_type

__all__ = [
    "SemanticReview",
    "accept_suggestion",
    "reject_suggestion",
    "review_semantics",
    "semantic_llm_assessments",
    "semantic_suggestions",
]


# ---------------------------------------------------------------------------
# Addresses
# ---------------------------------------------------------------------------

#: The ``decision`` values :func:`apply_sdp_semantics` writes into
#: ``semantic_suggestions.csv``, and how each maps back onto a review row.
#: ``not_selected`` is the sibling of an accept within the same slot: the slot
#: IS decided, but this candidate is not the one, so it carries no decision of
#: its own and the accepted row in the same slot supplies it.
RECORDED_DECISIONS = {
    "accepted": "accept",
    "accept": "accept",
    "rejected": "reject",
}

#: Which metadata columns identify the row a target writes into. Anything not
#: listed here has no write-back address and is refused rather than silently
#: shown as acceptable.
_TARGET_KEYS = {
    "column_dictionary.csv": ("dataset_id", "table_id", "column_name"),
    "codes.csv": ("dataset_id", "table_id", "column_name", "code_value"),
    "tables.csv": ("dataset_id", "table_id"),
}

#: The metadata files a review decision can be written back into.
WRITABLE_FILES = ("column_dictionary.csv", "codes.csv", "tables.csv")

#: The review frame's columns, in order. Mirrors the R tibble column for column
#: so a reader of either implementation sees one shape.
REVIEW_COLUMNS = [
    "slot_id",
    "dataset_id",
    "table_id",
    "column_name",
    "code_value",
    "role",
    "target_file",
    "target_field",
    "target_row_key",
    "current_value",
    "rank",
    "label",
    "iri",
    "source",
    "ontology",
    "definition",
    "score",
    "term_type",
    "llm_decision",
    "llm_confidence",
    "llm_rationale",
    "decision",
    "decision_iri",
    "decision_reason",
]


def review_target_keys(target_file: str) -> Optional[Sequence[str]]:
    """The identifying columns for one target metadata file, or ``None``."""
    return _TARGET_KEYS.get(str(target_file))


def _text(value) -> str:
    return scalar_text(value)


def _strip_review_iri(value) -> str:
    """The IRI without its ``REVIEW:`` marker.

    ``REVIEW:`` never survives a decision: the marker means "not confident",
    and accepting is the statement that removes it.
    """
    text = _text(value)
    if text.upper().startswith("REVIEW:"):
        return text[len("REVIEW:"):].strip()
    return text


def _is_review_iri(value) -> bool:
    return _text(value).upper().startswith("REVIEW:")


def _review_slot_id(frame: pd.DataFrame) -> "pd.Series":
    """One (target file, target row, target field) triple per row.

    ``target_row_key`` is already built by the suggestion producer from the
    identifying columns, so this is a re-spelling of an address that producer
    chose, not a new one.
    """
    return (
        frame["target_sdp_file"].map(_text)
        + "|"
        + frame["target_row_key"].map(_text)
        + "|"
        + frame["target_sdp_field"].map(_text)
    )


def _review_is_unfilled(value) -> bool:
    """A slot is unfilled when it is blank or still carries ``REVIEW:``.

    ``REVIEW:``-prefixed values are non-blank, which is precisely why the
    review queue cannot just test for emptiness -- and why
    :func:`apply_sdp_semantics` has to overwrite rather than fill.
    """
    text = _text(value)
    return not text or _is_review_iri(text)


def _review_match_rows(
    frame: Optional[pd.DataFrame], row: Mapping, keys: Optional[Sequence[str]]
) -> list:
    """Positions in ``frame`` matching one suggestion row on its keys.

    A key the suggestion leaves blank is not constrained -- ``tables.csv``
    targets carry no ``column_name``, and requiring one would match nothing.
    """
    if not isinstance(frame, pd.DataFrame) or frame.empty or not keys:
        return []
    matches = pd.Series(True, index=frame.index)
    for key in keys:
        if key not in frame.columns or key not in row:
            continue
        value = _text(row[key])
        if not value:
            continue
        matches &= frame[key].map(_text) == value
    return list(frame.index[matches])


# ---------------------------------------------------------------------------
# Accessors for the suggestion attributes
# ---------------------------------------------------------------------------


def _semantic_attribute_from(x, attribute: str, arg: str = "x") -> dict:
    """The one place that decides what an accessor accepts.

    ``x`` is a dictionary carrying the attribute in ``df.attrs``, an
    :func:`infer_salmon_datapackage_artifacts` / :func:`create_sdp`-shaped
    mapping, or a path to a written package.
    """
    if isinstance(x, (str, Path)):
        return {"kind": "path", "value": None, "path": Path(x)}
    if isinstance(x, pd.DataFrame):
        return {"kind": "object", "value": x.attrs.get(attribute), "path": None}
    if isinstance(x, Mapping):
        if x.get(attribute) is not None:
            return {"kind": "object", "value": x.get(attribute), "path": None}
        nested = x.get("dict")
        if isinstance(nested, pd.DataFrame):
            return {
                "kind": "object",
                "value": nested.attrs.get(attribute),
                "path": None,
            }
        return {"kind": "object", "value": None, "path": None}
    raise TypeError(
        f"{arg} must be a dictionary, an artifact mapping, or a package path. "
        "Pass the DataFrame returned by suggest_semantics(), the mapping "
        "returned by create_sdp()-style inference, or the package directory."
    )


def semantic_suggestions(x) -> Optional[pd.DataFrame]:
    """Semantic suggestions attached to a dictionary or written package.

    :func:`suggest_semantics` and :func:`infer_dictionary` attach their
    candidate shortlist to the returned dictionary as the
    ``semantic_suggestions`` entry of ``df.attrs``, and :func:`create_sdp`
    writes the same table to ``semantic_suggestions.csv`` inside the package.
    This is the supported way to read either, so review tooling never has to
    reach into ``attrs`` itself.

    Parameters
    ----------
    x
        A dictionary carrying the attribute, a mapping in the shape returned by
        :func:`infer_salmon_datapackage_artifacts` / :func:`create_sdp`-style
        inference, or a path to a written Salmon Data Package.

    Returns
    -------
    pandas.DataFrame or None
        Candidate rows, or ``None`` when none are attached. The ``None`` is
        deliberate -- these accessors replace a raw ``attrs`` lookup and must
        answer an ``is None`` test the same way it did.
    """
    found = _semantic_attribute_from(x, "semantic_suggestions")
    if found["kind"] == "path":
        suggestions_path = found["path"] / "semantic_suggestions.csv"
        if not suggestions_path.is_file():
            return None
        return read_sdp_csv(suggestions_path)
    if found["value"] is None:
        return None
    return pd.DataFrame(found["value"])


def semantic_llm_assessments(x) -> Optional[pd.DataFrame]:
    """Target-level LLM assessments attached to a dictionary.

    The companion accessor to :func:`semantic_suggestions`, for the
    ``semantic_llm_assessments`` attribute that
    ``suggest_semantics(llm_assess=True)`` attaches. Reading LLM review is
    never itself an LLM call: this only reports assessments that already exist.

    A package path always returns ``None`` -- assessments are not written into
    the package, so a package on disk cannot carry them.
    """
    found = _semantic_attribute_from(x, "semantic_llm_assessments")
    if found["kind"] == "path" or found["value"] is None:
        return None
    return pd.DataFrame(found["value"])


# ---------------------------------------------------------------------------
# The review object
# ---------------------------------------------------------------------------


class SemanticReview:
    """A re-runnable semantic review queue.

    One row per candidate, in the order the ranked producer emitted them.
    Immutable from the caller's point of view: :func:`accept_suggestion` and
    :func:`reject_suggestion` return a new review rather than mutating this
    one, which is what makes a review script replayable.

    ``print(review)`` renders the console view; ``review["decision"]`` and
    ``review.rows`` reach the underlying frame.
    """

    __slots__ = ("_rows", "path")

    def __init__(self, rows: pd.DataFrame, path: Optional[str] = None):
        self._rows = rows
        self.path = path

    # -- frame access -------------------------------------------------------

    @property
    def rows(self) -> pd.DataFrame:
        """The review frame. A copy, so a caller cannot edit a decision in."""
        return self._rows.copy()

    def to_frame(self) -> pd.DataFrame:
        """Alias for :attr:`rows`, for callers who prefer the verb."""
        return self.rows

    def __getitem__(self, key):
        return self._rows[key]

    def __len__(self) -> int:
        return len(self._rows)

    def __contains__(self, key) -> bool:
        return key in self._rows.columns

    @property
    def empty(self) -> bool:
        return self._rows.empty

    @property
    def columns(self):
        return self._rows.columns

    # -- rendering ----------------------------------------------------------

    def render_lines(self, object_name: Optional[str] = None) -> list:
        """The console view as a plain list of strings.

        ``__str__`` emits exactly what this returns, so tests assert against
        these lines rather than against captured terminal output.
        """
        if object_name is None:
            object_name = _review_object_name(self)
        return _render_review_lines(self, object_name)

    def __str__(self) -> str:
        return "\n".join(self.render_lines()) + "\n"

    def __repr__(self) -> str:
        return self.__str__()

    def _replace(self, rows: pd.DataFrame) -> "SemanticReview":
        return SemanticReview(rows, self.path)


def _binding_name_for(matches, default: str) -> str:
    """The name of an ``__main__`` binding holding this value, or ``default``.

    Mirrors ``.ms_binding_name_for()``, which scans ``globalenv()``. The
    printed call is meant to be pasted, so ``accept_suggestion(review, ...)``
    against a review the user bound to ``rev`` would fail with a ``NameError``
    -- getting this wrong is not cosmetic. Sorted, so the choice is
    reproducible when more than one name holds the same object.
    """
    module = sys.modules.get("__main__")
    namespace = getattr(module, "__dict__", None)
    if not isinstance(namespace, dict):
        return default
    for name in sorted(namespace):
        if name.startswith("__"):
            continue
        try:
            if matches(namespace[name]):
                return name
        except Exception:
            continue
    return default


def _review_object_name(review: "SemanticReview", default: str = "review") -> str:
    return _binding_name_for(
        lambda value: isinstance(value, SemanticReview) and value is review,
        default,
    )


# ---------------------------------------------------------------------------
# Building the queue
# ---------------------------------------------------------------------------


def _review_source_frames(x) -> dict:
    """The metadata frames a review reads its current values from.

    Accepts the same three shapes the accessors do, so :func:`review_semantics`
    never has to know whether it was handed a package or an in-memory
    dictionary.
    """
    if isinstance(x, (str, Path)):
        target = Path(x)
        if not target.is_dir():
            raise FileNotFoundError(f"Directory {target} does not exist.")

        def read_one(file_name: str):
            located = target / "metadata" / file_name
            if not located.is_file():
                located = target / file_name
            if not located.is_file():
                return None
            # The raw reader, as R's ``.ms_review_source_frames()`` uses: this
            # only reads a current value, and normalising would reorder a frame
            # nothing here writes back.
            return read_sdp_csv(located)

        return {
            "column_dictionary.csv": read_one("column_dictionary.csv"),
            "codes.csv": read_one("codes.csv"),
            "tables.csv": read_one("tables.csv"),
        }
    if isinstance(x, pd.DataFrame):
        return {"column_dictionary.csv": x}
    if isinstance(x, Mapping):
        frames = {}
        for key, file_name in (
            ("dict", "column_dictionary.csv"),
            ("codes", "codes.csv"),
            ("table_meta", "tables.csv"),
        ):
            value = x.get(key)
            frames[file_name] = value if isinstance(value, pd.DataFrame) else None
        return frames
    return {}


def _review_seed_recorded_decisions(
    rows: pd.DataFrame, suggestions: pd.DataFrame
) -> pd.DataFrame:
    """Replay the decisions recorded in the package onto a fresh review.

    DECISIONS ALREADY ON DISK ARE READ BACK. :func:`apply_sdp_semantics`
    records each decision in ``semantic_suggestions.csv``, and until this
    existed nothing read it: a reject CLEARS the field, a blank field reads as
    undecided, and so the next :func:`review_semantics` asked the same question
    again with no sign the user had ever answered it. A reviewer who works
    through sixteen slots, rejects four and comes back tomorrow was shown those
    four as if they were new. The round trip is the point of persisting the
    decision at all.
    """
    if "decision" not in suggestions.columns or rows.empty:
        return rows
    decisions = list(suggestions["decision"].map(_text))
    if "decision_reason" in suggestions.columns:
        reasons = list(suggestions["decision_reason"])
    else:
        reasons = [pd.NA] * len(decisions)

    # ``rows`` is built from ``suggestions`` row for row and this runs before
    # any subsetting, so the positions still line up here.
    for position in range(len(rows)):
        recorded = RECORDED_DECISIONS.get(decisions[position].lower())
        if recorded is None:
            continue
        index = rows.index[position]
        rows.at[index, "decision"] = recorded
        reason = reasons[position]
        rows.at[index, "decision_reason"] = pd.NA if pd.isna(reason) else reason
        if recorded == "accept":
            rows.at[index, "decision_iri"] = _strip_review_iri(
                rows.at[index, "iri"]
            )
    return rows


def review_semantics(
    x,
    include_filled: bool = False,
    max_candidates: Optional[int] = 5,
    columns: Optional[Iterable[str]] = None,
) -> SemanticReview:
    """Review semantic suggestions in the console.

    Builds a re-runnable review queue from suggestions that already exist. One
    entry per unfilled semantic slot, each with its ranked shortlist and the
    exact :func:`accept_suggestion` call that decides it -- printing that call
    is the feature: paste it into a script and the decision becomes
    reproducible, which the spreadsheet workflow this replaces never was.

    **This never contacts a network or an LLM.** It reads the
    ``semantic_suggestions`` attribute (or ``semantic_suggestions.csv``) that
    :func:`suggest_semantics` / :func:`create_sdp` already produced. When those
    suggestions carry LLM review -- only possible if they were generated with
    ``llm_assess=True`` -- this surfaces it; it never generates it.

    Parameters
    ----------
    x
        A written package path, a dictionary carrying the
        ``semantic_suggestions`` attribute, or the artifact mapping returned by
        :func:`infer_salmon_datapackage_artifacts`.
    include_filled
        When ``True``, also queue slots that already hold a final
        (non-``REVIEW:``) IRI, and slots that already carry a decision.
    max_candidates
        Maximum candidates shown per slot. ``None`` shows all.
    columns
        Optional column names restricting the queue. A value matching no
        column is an error that names the columns that do exist -- filtering
        first and then reporting an empty queue told a user who mistyped a
        column name that their package was finished.

    Returns
    -------
    SemanticReview
    """
    suggestions = semantic_suggestions(x)
    if suggestions is None or suggestions.empty:
        raise ValueError(
            "No semantic suggestions to review. Run suggest_semantics(), or "
            "create_sdp() with seed_semantics=True, first."
        )
    suggestions = suggestions.reset_index(drop=True)

    required = [
        "column_name",
        "dictionary_role",
        "iri",
        "label",
        "target_sdp_file",
        "target_sdp_field",
        "target_row_key",
    ]
    missing = [name for name in required if name not in suggestions.columns]
    if missing:
        raise ValueError(
            "Suggestions are missing required columns: " + ", ".join(missing)
        )

    for name in (
        "dataset_id",
        "table_id",
        "code_value",
        "source",
        "ontology",
        "definition",
    ):
        if name not in suggestions.columns:
            suggestions[name] = pd.NA
    if "score" not in suggestions.columns:
        suggestions["score"] = pd.NA

    # Only slots with a write-back address, and only IRI fields.
    # ``dataset.csv`` targets a comma-joined ``keywords`` list rather than a
    # single IRI, so it has no "accept this candidate" semantics; queueing it
    # would show a row that cannot be decided.
    target_files = suggestions["target_sdp_file"].map(_text)
    target_fields = suggestions["target_sdp_field"].map(_text)
    keep = (
        target_files.isin(WRITABLE_FILES)
        & target_fields.str.endswith("_iri")
        & (suggestions["iri"].map(_text) != "")
    )
    dropped = sorted(
        {
            f"{file_name} · {field}"
            for file_name, field in zip(
                target_files[~keep], target_fields[~keep]
            )
        }
    )
    suggestions = suggestions[keep].reset_index(drop=True)
    if dropped:
        print(
            "Some suggestions target fields this review cannot decide, and are "
            "not queued:"
        )
        for entry in dropped:
            print(f"  * {entry}")
        print("  Edit those in the metadata CSVs directly.")

    if columns is not None:
        wanted = [str(name) for name in columns]
        known = {
            name
            for name in suggestions["column_name"].map(_text)
            if name
        }
        unknown = [name for name in wanted if name not in known]
        if unknown:
            raise ValueError(
                "No suggestions target "
                + ("this column: " if len(unknown) == 1 else "these columns: ")
                + ", ".join(unknown)
                + ". Columns with suggestions: "
                + ", ".join(sorted(known)[:20])
                + "."
            )
        suggestions = suggestions[
            suggestions["column_name"].map(_text).isin(wanted)
        ].reset_index(drop=True)

    frames = _review_source_frames(x)
    review_path = str(x) if isinstance(x, (str, Path)) else None

    slot_ids = list(_review_slot_id(suggestions))
    # Deliberately NO re-ranking. The incoming row order is the ranked order
    # the retrieval step produced, and that ordering is already deterministic
    # and registered in the determinism guard. Re-sorting here would create a
    # SECOND ordering of the same candidates that could disagree with the top-1
    # the seeded auto-apply already wrote into the dictionary -- two renderings
    # of one decision. ``rank`` is therefore a position, not a sort.
    seen: dict = {}
    ranks = []
    for slot in slot_ids:
        position = seen.get(slot, 0) + 1
        seen[slot] = position
        ranks.append(position)

    current_values = []
    for position in range(len(suggestions)):
        row = suggestions.iloc[position]
        target_file = _text(row["target_sdp_file"])
        target_field = _text(row["target_sdp_field"])
        frame = frames.get(target_file)
        keys = review_target_keys(target_file)
        if (
            not isinstance(frame, pd.DataFrame)
            or keys is None
            or target_field not in frame.columns
        ):
            current_values.append(pd.NA)
            continue
        hits = _review_match_rows(frame, row, keys)
        if len(hits) != 1:
            current_values.append(pd.NA)
            continue
        current_values.append(_text(frame.at[hits[0], target_field]))

    def column(name):
        return suggestions[name].map(_text) if name in suggestions else pd.NA

    rows = pd.DataFrame(
        {
            "slot_id": slot_ids,
            "dataset_id": column("dataset_id"),
            "table_id": column("table_id"),
            "column_name": column("column_name"),
            "code_value": column("code_value"),
            "role": column("dictionary_role"),
            "target_file": column("target_sdp_file"),
            "target_field": column("target_sdp_field"),
            "target_row_key": column("target_row_key"),
            "current_value": current_values,
            "rank": ranks,
            "label": column("label"),
            "iri": column("iri"),
            "source": column("source"),
            "ontology": column("ontology"),
            "definition": column("definition"),
            "score": pd.to_numeric(suggestions["score"], errors="coerce"),
            # Computed here, from the candidate row, with the same helper
            # ``apply_semantic_suggestions()`` uses -- so the reviewed write
            # and the seeded write cannot disagree about the same candidate's
            # ``term_type``.
            "term_type": [
                _infer_term_type(suggestions.iloc[position])
                for position in range(len(suggestions))
            ],
            "llm_decision": column("llm_decision"),
            "llm_confidence": pd.to_numeric(
                suggestions["llm_confidence"], errors="coerce"
            )
            if "llm_confidence" in suggestions
            else pd.NA,
            "llm_rationale": column("llm_rationale"),
            "decision": pd.NA,
            "decision_iri": pd.NA,
            "decision_reason": pd.NA,
        },
        columns=REVIEW_COLUMNS,
    )
    rows = rows.astype({"rank": "int64"})
    rows = _review_seed_recorded_decisions(rows, suggestions)

    if not include_filled:
        # A slot with an unknown current value (no frame to read, or an
        # ambiguous row match) is kept: dropping it would hide work, and the
        # console labels it "current: <unknown>" so the user can see why.
        unfilled = rows["current_value"].map(
            lambda value: pd.isna(value) or _review_is_unfilled(value)
        )
        # A recorded decision takes a slot out of the queue even though
        # rejecting leaves the field blank -- "blank" and "undecided" are
        # different states, and only ``include_filled=True`` shows the decided
        # ones again.
        decided_slots = set(rows.loc[rows["decision"].notna(), "slot_id"])
        rows = rows[unfilled & ~rows["slot_id"].isin(decided_slots)]

    if max_candidates is not None:
        rows = rows[rows["rank"] <= int(max_candidates)]

    return SemanticReview(rows.reset_index(drop=True), review_path)


# ---------------------------------------------------------------------------
# Console rendering
# ---------------------------------------------------------------------------


def _term_browse_url(iri, source=None, ontology=None) -> Optional[str]:
    """A browsable URL for a term, or ``None`` when there is nothing safe.

    Refuses every scheme that is not http/https: an ontology IRI is external
    text, and turning a ``javascript:`` or ``file:`` string into something a
    reader will click would make third-party data actionable.
    """
    from urllib.parse import quote

    text = _text(iri)
    if not text:
        return None
    source_text = _text(source).lower()
    ontology_text = _text(ontology).lower()

    if source_text == "ols" and ontology_text and ontology_text != "ols":
        # OLS4 resolves a term by ontology plus double-encoded IRI.
        encoded = quote(quote(text, safe=""), safe="")
        url = (
            "https://www.ebi.ac.uk/ols4/ontologies/"
            f"{ontology_text}/classes/{encoded}"
        )
    else:
        # smn, gcdfo and nvs IRIs are already the canonical browsable form.
        url = text

    if not url.lower().startswith(("http://", "https://")):
        return None
    return url


def _review_iri_display(iri, source, ontology) -> str:
    """The IRI as it should appear on screen, never hiding the URL."""
    text = _text(iri)
    url = _term_browse_url(iri, source, ontology)
    if url is None or url == text:
        return text
    return f"{text}  {url}"


def _review_rule(text: str, width: int = 78) -> str:
    prefix = f"── {text} "
    return prefix + "─" * max(3, width - len(prefix))


def _review_wrap(text, indent: str = "       ", width: int = 78) -> list:
    """Wrap a definition without breaking a long IRI or a brace run."""
    import textwrap

    value = _text(text)
    if not value:
        return []
    wrapped = textwrap.wrap(value, width=max(20, width - len(indent)))
    return [indent + line for line in wrapped]


def _match_slot_rows(
    rows: pd.DataFrame,
    column: Optional[str],
    role: str,
    table: Optional[str] = None,
    code_value: Optional[str] = None,
) -> pd.DataFrame:
    """Missing-safe slot matching.

    Every comparison is guarded because ``tables.csv`` slots carry NO column
    name. The R original's first version did the unguarded comparison and the
    visible symptom was a printed call that could not run --
    ``accept_suggestion(review, "NA", "entity", ...)`` for the table slot, and
    a spurious ``table="spawners"`` on an unrelated dictionary slot that a
    phantom missing row had made look ambiguous. ``column=None`` selects the
    column-less (table-scope) slots deliberately.
    """
    if rows.empty:
        return rows
    has_column = rows["column_name"].map(lambda value: bool(_text(value)))
    if column is None:
        keep = ~has_column
    else:
        keep = has_column & (rows["column_name"].map(_text) == _text(column))
    keep &= rows["role"].map(_text) == _text(role)
    if table is not None:
        keep &= rows["table_id"].map(_text) == _text(table)
    if code_value is not None:
        keep &= rows["code_value"].map(_text) == _text(code_value)
    return rows[keep]


def _review_call_args(rows: pd.DataFrame, slot_id: str) -> dict:
    """The minimal argument set that resolves to exactly this slot.

    Computed by *doing* the resolution, not by guessing. ``table`` and
    ``code_value`` are added only when (column, role) alone is ambiguous, so
    the common single-table case prints the short call. A column-less slot has
    no positional spelling at all, so it prints named arguments and ``table``
    is mandatory rather than a disambiguator.
    """
    row = rows[rows["slot_id"] == slot_id].iloc[0]
    column = _text(row["column_name"])
    args = {"column": column or None, "role": _text(row["role"])}

    def resolved(extra):
        return set(
            _match_slot_rows(
                rows,
                args["column"],
                args["role"],
                extra.get("table"),
                extra.get("code_value"),
            )["slot_id"]
        )

    extra: dict = {}
    if args["column"] is None or len(resolved(extra)) > 1:
        table_value = _text(row["table_id"])
        if table_value:
            extra["table"] = table_value
    if len(resolved(extra)) > 1:
        code_value = _text(row["code_value"])
        if code_value:
            extra["code_value"] = code_value
    args.update(extra)
    return args


def _quote(value) -> str:
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'


def _decision_call(
    fn: str,
    rows: pd.DataFrame,
    slot_id: str,
    rank=None,
    object_name: str = "review",
) -> str:
    """The pasteable call.

    Positional ``column``/``role`` when there is a column, all named when
    there is not -- so the printed string always parses to the same slot it was
    printed under.
    """
    args = _review_call_args(rows, slot_id)
    if args["column"] is None:
        parts = [object_name, f"role={_quote(args['role'])}"]
    else:
        parts = [object_name, _quote(args["column"]), _quote(args["role"])]
    if rank is not None:
        parts.append(f"rank={int(rank)}")
    if "table" in args:
        parts.append(f"table={_quote(args['table'])}")
    if "code_value" in args:
        parts.append(f"code_value={_quote(args['code_value'])}")
    return f"{fn}({', '.join(parts)})"


def _accept_call(rows, slot_id, rank, object_name="review") -> str:
    return _decision_call("accept_suggestion", rows, slot_id, rank, object_name)


def _reject_call(rows, slot_id, object_name="review") -> str:
    return _decision_call("reject_suggestion", rows, slot_id, None, object_name)


def _format_score(value) -> str:
    if pd.isna(value):
        return ""
    rounded = round(float(value), 2)
    if rounded == int(rounded):
        return f"score {int(rounded)}"
    return f"score {rounded:g}"


def _render_review_lines(review: SemanticReview, object_name: str) -> list:
    rows = review.rows
    if rows.empty:
        return [
            "Nothing left to review.",
            "",
            "Every slot that had a shortlist is either filled or already decided.",
            "Pass include_filled=True to see the decided ones.",
            "Run review_metadata(path) for the fields that still block validation.",
        ]

    slot_ids = list(dict.fromkeys(rows["slot_id"]))
    lines: list = []

    for slot in slot_ids:
        slot_rows = rows[rows["slot_id"] == slot]
        head = slot_rows.iloc[0]

        heading_parts = [
            _text(head["table_id"]),
            _text(head["column_name"]),
            _text(head["code_value"]),
            _text(head["role"]),
        ]
        lines.append(
            _review_rule(" · ".join(part for part in heading_parts if part))
        )

        current = head["current_value"]
        if pd.isna(current):
            current_text = "<unknown>"
        elif not _text(current):
            current_text = "<blank>"
        else:
            current_text = str(current)
        lines.append(
            "   field:   "
            + _text(head["target_file"])
            + " · "
            + _text(head["target_field"])
        )
        lines.append("   current: " + current_text)

        decided = slot_rows[slot_rows["decision"].notna()]
        if not decided.empty:
            decided_row = decided.iloc[0]
            if _text(decided_row["decision"]) == "accept":
                lines.append(
                    "   DECIDED: accept → "
                    + _text(decided_row["decision_iri"])
                )
            else:
                reason = _text(decided_row["decision_reason"])
                lines.append(
                    "   DECIDED: reject (clears the field)"
                    + (f" — {reason}" if reason else "")
                )
        lines.append("")

        for position in range(len(slot_rows)):
            candidate = slot_rows.iloc[position]
            score_text = _format_score(candidate["score"])
            marker = "*" if _text(candidate["decision"]) == "accept" else " "
            lines.append(
                "  "
                + marker
                + f"[{int(candidate['rank'])}] "
                + _text(candidate["label"])
                + "   "
                + _text(candidate["source"])
                + (f"   {score_text}" if score_text else "")
            )
            lines.extend(_review_wrap(candidate["definition"]))
            lines.append(
                "       "
                + _review_iri_display(
                    candidate["iri"], candidate["source"], candidate["ontology"]
                )
            )
            llm_decision = _text(candidate["llm_decision"])
            if llm_decision:
                confidence = candidate["llm_confidence"]
                lines.append(
                    "       llm: "
                    + llm_decision
                    + (
                        ""
                        if pd.isna(confidence)
                        else f" (confidence {float(confidence):g})"
                    )
                )
                lines.extend(
                    _review_wrap(
                        candidate["llm_rationale"], indent="            "
                    )
                )
            lines.append(
                "       "
                + object_name
                + " = "
                + _accept_call(rows, slot, candidate["rank"], object_name)
            )
            lines.append("")

        lines.append(
            "       "
            + object_name
            + " = "
            + _reject_call(rows, slot, object_name)
            + "   # no candidate fits"
        )
        lines.append("")

    if review.path is None:
        apply_call = f"apply_sdp_semantics(<package path>, {object_name})"
    else:
        apply_call = f"apply_sdp_semantics({_quote(review.path)}, {object_name})"

    n_decided = len(set(rows.loc[rows["decision"].notna(), "slot_id"]))
    lines.extend(
        [
            _review_rule("next"),
            f"   {n_decided} of {len(slot_ids)} slot"
            + ("" if len(slot_ids) == 1 else "s")
            + " decided.",
            "   Paste the calls above into your script, then write the decisions:",
            f"   {apply_call}",
            "",
        ]
    )
    return lines


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


def _assert_review(review) -> SemanticReview:
    if not isinstance(review, SemanticReview):
        raise TypeError(
            "review must be a SemanticReview object. Build one with "
            "review_semantics()."
        )
    return review


def _resolve_slot(
    rows: pd.DataFrame,
    column: Optional[str],
    role: str,
    table: Optional[str] = None,
    code_value: Optional[str] = None,
) -> str:
    """Resolve (column, role[, table][, code_value]) to exactly one slot.

    Caller text is concatenated into the message rather than formatted into it,
    so a column literally named ``rate{pct`` appears as itself. R has to escape
    the same text because cli treats a message as a glue template; Python does
    not, which is the *Inapplicable* half of ``PARITY.md`` row 37, and escaping
    here would mangle the value rather than protect it.
    """
    column_text = _text(column) if column is not None else None
    if column_text == "":
        column_text = None
    role_text = _text(role)
    table_text = _text(table) if table is not None else None
    code_text = _text(code_value) if code_value is not None else None

    matched = _match_slot_rows(rows, column_text, role_text, table_text, code_text)

    if matched.empty:
        available = list(
            dict.fromkeys(
                (
                    _text(row["column_name"])
                    or f"<table {_text(row['table_id'])}>"
                )
                + " · "
                + _text(row["role"])
                for _, row in rows.iterrows()
            )
        )
        asked = (column_text or "<no column>") + " · " + role_text
        raise ValueError(
            "No review slot matches that column and role. Asked for: "
            + asked
            + ". Available: "
            + "; ".join(available[:20])
            + "."
        )

    slots = list(dict.fromkeys(matched["slot_id"]))
    if len(slots) > 1:
        ambiguous = list(
            dict.fromkeys(
                "table=" + _quote(_text(row["table_id"]))
                + (
                    ""
                    if not _text(row["code_value"])
                    else ", code_value=" + _quote(_text(row["code_value"]))
                )
                for _, row in matched.iterrows()
            )
        )
        raise ValueError(
            "That column and role match more than one review slot. Add one of "
            "these arguments to say which: "
            + "; ".join(ambiguous)
            + "."
        )
    return slots[0]


def accept_suggestion(
    review: SemanticReview,
    column: Optional[str] = None,
    role: Optional[str] = None,
    rank: int = 1,
    table: Optional[str] = None,
    code_value: Optional[str] = None,
    iri: Optional[str] = None,
) -> SemanticReview:
    """Record that a candidate is the right term for a slot.

    Pipe-friendly: takes a review and returns a new one, so a whole review is
    an ordinary, re-runnable script. Nothing is written until
    :func:`apply_sdp_semantics` is called.

    This is the call :func:`review_semantics` prints. Pasting the printed line
    is the intended workflow, and it is what makes the decision reproducible:
    the script is the audit trail.

    Parameters
    ----------
    review
        A :class:`SemanticReview` from :func:`review_semantics`.
    column
        Column name of the slot. Omit it for a table-level slot
        (``tables.csv`` -- ``observation_unit_iri``), which has no column; pass
        ``table`` instead. :func:`review_semantics` prints the right spelling
        either way.
    role
        Semantic role of the slot (``"variable"``, ``"property"``,
        ``"entity"``, ``"unit"``, ``"constraint"``,
        ``"statistical_modifier"``).
    rank
        Rank of the candidate to accept, as printed in the shortlist.
    table
        Table identifier; needed only when the column name appears in more than
        one table.
    code_value
        Code value; needed only for code-level slots.
    iri
        Optional IRI to accept instead of a shortlisted candidate -- for the
        case where the right term exists but retrieval did not surface it.

    Returns
    -------
    SemanticReview
        The review, with the decision recorded.
    """
    _assert_review(review)
    if role is None:
        raise TypeError("accept_suggestion() requires role.")
    rows = review.rows
    slot = _resolve_slot(rows, column, role, table, code_value)
    in_slot = rows["slot_id"] == slot

    if iri is not None:
        accepted_iri = _text(iri)
        if not accepted_iri:
            raise ValueError("iri must be a non-empty IRI.")
        target_index = rows.index[in_slot][0]
    else:
        hits = list(rows.index[in_slot & (rows["rank"] == int(rank))])
        if len(hits) != 1:
            available = ", ".join(
                str(value) for value in rows.loc[in_slot, "rank"]
            )
            raise ValueError(
                "No candidate with that rank in this slot. Ranks available: "
                f"{available}. To accept a term that is not shortlisted, pass "
                "iri instead."
            )
        target_index = hits[0]
        accepted_iri = _text(rows.at[target_index, "iri"])

    accepted_iri = _strip_review_iri(accepted_iri)

    rows.loc[in_slot, ["decision", "decision_iri", "decision_reason"]] = pd.NA
    rows.at[target_index, "decision"] = "accept"
    rows.at[target_index, "decision_iri"] = accepted_iri
    return review._replace(rows)


def reject_suggestion(
    review: SemanticReview,
    column: Optional[str] = None,
    role: Optional[str] = None,
    table: Optional[str] = None,
    code_value: Optional[str] = None,
    reason: Optional[str] = None,
) -> SemanticReview:
    """Record that no candidate fits a slot, and clear the field.

    The companion to :func:`accept_suggestion`. ``reason`` is free text
    recorded with the rejection; it reaches
    ``semantic_suggestions.csv``'s ``decision_reason`` column when
    :func:`apply_sdp_semantics` runs, and :func:`review_metadata` reads it back
    onto the gap the rejection left -- otherwise that gap comes back looking
    exactly like one nobody ever considered.
    """
    _assert_review(review)
    if role is None:
        raise TypeError("reject_suggestion() requires role.")
    rows = review.rows
    slot = _resolve_slot(rows, column, role, table, code_value)
    in_slot = rows["slot_id"] == slot

    rows.loc[in_slot, "decision"] = "reject"
    rows.loc[in_slot, "decision_iri"] = pd.NA
    rows.loc[in_slot, "decision_reason"] = (
        pd.NA if reason is None else _text(reason)
    )
    return review._replace(rows)


def review_decisions(review: SemanticReview) -> pd.DataFrame:
    """One row per decided slot, in review order. The write-back's only input.

    A reject marks every row in the slot; the first is kept so a slot
    contributes exactly one write.
    """
    rows = review.rows
    decided = rows[rows["decision"].notna()]
    if decided.empty:
        return decided
    return decided.drop_duplicates(subset=["slot_id"], keep="first")
