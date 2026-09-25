"""Free-text metadata review and editing (mirrors ``R/sdp-field-setters.R``).

The semantic review in :mod:`metasalmonpy.review_console` made the *semantic*
decision scriptable. This module is the other half, and it is the larger one,
for a reason the R side measured rather than guessed: after a complete console
review, ``validate_salmon_datapackage(require_iris=True)`` still failed. Two
causes, and one mechanism closes both.

1. ``MISSING DESCRIPTION:`` / ``MISSING METADATA:`` placeholders are refused by
   strict validation, and the only way to replace them was a spreadsheet.
2. :func:`review_semantics` shows SHORTLISTS, NOT GAPS. A slot that retrieval
   returned nothing for never enters the queue at all, so a user could finish
   the entire console review and still be missing a required IRI -- and nothing
   in the review would have said so.

:func:`review_metadata` closes both because it does not read a suggestion list.
It reads the package against the rules that actually decide strict validation:
the Frictionless schema's ``constraints.required`` (which this package parsed
nowhere and consumed nowhere before the S5 port), the prose placeholder markers,
the unresolved ``REVIEW:`` IRI marker, the measurement-column IRI requirement,
and the table observation-unit IRI requirement. A field that no retrieval ever
touched is as visible to it as one with five candidates.

"The rules that actually decide strict validation" is the whole claim, so every
rule it omits is a defect and not a scoping choice: the scan reported a clean
package for an unresolved ``REVIEW:`` IRI until 2026-09-15, because the prose
test cannot see the marker. ``_is_unresolved_iri()`` records why that needed a
second test rather than a wider first one.

Under the shipped schema settings it reads the BUNDLED schema rather than the
remote one (``_schema_source()``), because a function documented as never
contacting a network cannot reach one, and ``load_sdp_schema()``'s default
source fetches before it falls back. Under a schema the settings select, it
reads that one, as the package writers do.

THE CONTRACT IT IS JUDGED AGAINST: every row :func:`review_metadata` reports
prints a runnable ``set_sdp_*()`` call that fixes it, and when the last row is
gone strict validation passes. ``tests/test_sdp_field_setters.py`` asserts both
halves -- including by EXECUTING the printed calls, because if a program's
output is meant to be run, the tests have to run it. A printed call that names a
column that does not exist passes every substring assertion ever written.

THE PRINTED CALL IS THE CONTRACT, exactly as in
:mod:`metasalmonpy.review_console`: the console prints a line, the user edits
the placeholder value and pastes it, and the paste is the audit trail. No
prompt loop, no TUI. And as there, the rendering path is plain strings rather
than a formatter, so a placeholder message containing ``{...}`` prints literally
-- see that module's header for why escaping HERE would be the bug rather than
the fix.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Sequence

import pandas as pd

from .metadata import (
    align_columns,
    is_review_placeholder,
    read_sdp_csv,
)
from .review_console import (
    _binding_name_for,
    _is_review_iri,
    _quote,
    _review_rule,
    _review_wrap,
    _text,
)
from .sdp_schema import (
    _sdp_schema_options_are_default,
    sdp_schema_field_description,
    sdp_schema_field_names,
    sdp_schema_required_field_names,
)

__all__ = [
    "MetadataReview",
    "review_metadata",
    "set_sdp_code",
    "set_sdp_column",
    "set_sdp_dataset",
    "set_sdp_table",
]


# ---------------------------------------------------------------------------
# What "unfilled" means, and where the requirement comes from
# ---------------------------------------------------------------------------

#: Which SDP metadata file each schema table describes. One spelling of a
#: mapping that would otherwise be re-derived in every function here.
METADATA_SCHEMA_TABLES = {
    "dataset.csv": "dataset",
    "tables.csv": "tables",
    "column_dictionary.csv": "column_dictionary",
    "codes.csv": "codes",
}

#: The columns that ADDRESS a row rather than describe it. Deliberately not
#: settable: changing one would not fill a gap, it would silently re-point the
#: row at a different table or column and orphan whatever referred to it.
#: ``validate_salmon_datapackage()`` is the channel for a blank key, because a
#: package with one is broken rather than incomplete.
#:
#: Retires when a setter can address a row without a key -- which it cannot, so
#: read this as permanent rather than as an unexplained exclusion.
METADATA_KEY_FIELDS = {
    "dataset.csv": ("dataset_id",),
    "tables.csv": ("dataset_id", "table_id", "file_name"),
    "column_dictionary.csv": ("dataset_id", "table_id", "column_name"),
    "codes.csv": ("dataset_id", "table_id", "column_name", "code_value"),
}

#: Measurement columns must carry all four I-ADOPT IRIs before
#: ``validate_dictionary(require_iris=True)`` will pass. That requirement is
#: stated in the dictionary validator rather than in the schema (the schema
#: calls them ``conditional``), so it is enumerated here and pinned by a test
#: that drives the validator itself -- an enumeration that drifts from the
#: validator would make :func:`review_metadata` report a clean package that
#: still fails.
MEASUREMENT_IRI_FIELDS = ("term_iri", "property_iri", "entity_iri", "unit_iri")


def _schema_source() -> Optional[str]:
    """The ``source`` that every schema read in this module passes.

    UNDER THE SHIPPED SCHEMA SETTINGS, ``"vendored"``: the bundled copy, read
    outside the loader's cache. :func:`review_metadata` documents that it never
    contacts a network, and ``load_sdp_schema()``'s default ``"auto"`` source
    fetches the bundle over HTTP before falling back to that same bundled copy.
    So on a fresh process a documented-local scan reached the network, and
    waited out a timeout when there was none (hub B-175).

    UNDER ANY OTHER SCHEMA SETTING, ``None``: the schema the loader resolves.
    That is the schema the package writers read, so it is the field contract the
    package was written to. Until hub B-215 this was ``"vendored"`` under every
    setting, so ``set_sdp_schema_source()``, ``set_sdp_schema_base_url()`` and
    their environment variables reached the writers and nothing here. The scan
    then omitted a requirement the selected schema declares, and the setters
    refused a field the package carries. The loader answers from the session
    cache once a writer has loaded that schema, and otherwise loads it once, as
    a writer would. Selecting a schema is the opt-in to reading it.

    The setters read the same source deliberately. They are local edits to local
    files, and the gap scan and the printed call have to agree about which fields
    exist: a call :func:`review_metadata` prints must be one
    ``_set_sdp_metadata()`` accepts. The validator's blank-required collector
    reads it too, which keeps "the last row is gone" and "strict validation
    passes" one statement.

    Resolved at call time, like the settings it reads. The first branch retires
    when ``load_sdp_schema()`` stops fetching on its default source, because the
    default is then already offline.
    """
    return "vendored" if _sdp_schema_options_are_default() else None


def _declared_metadata_fields(file_name: str) -> list:
    """The fields the selected schema declares for one metadata file, in order.

    Read through :func:`_schema_source`, so under the shipped settings it is
    the bundled schema, read offline.
    """
    return sdp_schema_field_names(
        METADATA_SCHEMA_TABLES[file_name], source=_schema_source()
    )


def _in_declared_order(frame: pd.DataFrame, file_name: str) -> pd.DataFrame:
    """One metadata frame, aligned to the fields the selected schema declares.

    The declared fields come first, in the schema's order, and a declared
    field the frame lacks is added empty. Any other column follows. This is
    metasalmon's `.ms_align_cols(df, .ms_dataset_meta_cols())` and its
    siblings. The setters, ``write_salmon_datapackage()`` and
    ``apply_sdp_semantics()`` all write through it, so a package keeps its
    bytes from one of them to the next (hub B-215). Under the bundled schema
    the declared order is the order of the static column lists in
    ``metadata.py``, so under the shipped settings no written byte changes.
    """
    return align_columns(frame, _declared_metadata_fields(file_name))


_PLACEHOLDER_PREFIX = re.compile(
    r"^\s*(MISSING METADATA|MISSING DESCRIPTION|REVIEW REQUIRED)\s*:\s*",
    re.IGNORECASE,
)

GAP_COLUMNS = [
    "file",
    "table_id",
    "column_name",
    "code_value",
    "field",
    "current_value",
    "reason",
    "hint",
    "note",
]


def _is_unfilled_metadata(value) -> bool:
    """A metadata value the user still has to supply.

    Three ways to be unfilled and they are not interchangeable: absent, blank,
    or *stating in its own text* that it is missing. The third is why a
    blankness test is not enough -- a ``MISSING METADATA:`` placeholder is a
    non-empty string, and strict validation refuses it precisely because it is
    not a value.

    This is the PROSE test, and it is deliberately blind to the ``REVIEW:`` IRI
    marker: ``_is_unresolved_iri()`` is the fourth way an IRI field can be
    unfilled, and it needs its own test for the reason recorded there.
    """
    return not _text(value) or is_review_placeholder(value)


def _is_unresolved_iri(value) -> bool:
    """An IRI field still carrying the ``REVIEW:`` marker.

    The marker is the fill helpers' "inferred, not confirmed" value, and it is a
    FOURTH way to be unfilled that the prose test cannot see: it is non-blank,
    and it is not one of the three ``MISSING …`` / ``REVIEW REQUIRED:``
    spellings, so :func:`_is_unfilled_metadata` passes straight over it. Strict
    validation does not -- ``_collect_review_iri_issues()`` refuses a marker in
    any ``*_iri`` column of ``tables.csv``, and
    ``validate_dictionary(require_iris=True)`` refuses one in any of the
    dictionary's six. Without this test the scan could report "No outstanding
    metadata." for a package ``validate_salmon_datapackage(require_iris=True)``
    then rejected, which breaks the one handoff :func:`review_metadata` exists to
    provide. A user who leaves part of the semantic queue undecided reaches
    exactly that state.

    WHERE THIS TEST IS APPLIED is a second question and ``_REVIEW_IRI_FILES``
    answers it, because the gates do not all sweep the same files. Which fields
    within those files is a third, and the scan reaches only the
    SCHEMA-DECLARED ones; ``test_an_undeclared_iri_column_is_still_missed`` pins
    the gap that leaves.

    A SECOND test rather than a wider ``is_review_placeholder()``. That one
    mirrors R's ``.ms_is_review_placeholder()``, and its narrowness is
    load-bearing for five other callers: the license gate in ``package_io``
    reads it *because* a bare ``REVIEW:`` IRI is not prose, the placeholder
    sweep in ``validate_salmon_datapackage()`` excludes the marker because it has
    its own dedicated reporting path, and :func:`_gap_row` uses it to decide
    whether a value's own text is a usable hint -- an IRI's is not. Widening it
    would have been the smaller diff and would have pushed the marker into three
    channels built to exclude it.

    Retires when: nothing. The two tests answer different questions about
    different kinds of field, which is why they are two.
    """
    return _is_review_iri(value)


#: The metadata files whose ``*_iri`` markers this scan reports. Three gates
#: sweep the marker and they do NOT sweep the same files, which is why this list
#: cannot be derived from any one of them -- measured 2026-09-16, by marking one
#: field per file and asking each gate. Reading
#: ``validate_salmon_datapackage(require_iris=True)`` then the EDH XML gate
#: (``_collect_review_issues()``):
#:
#: * ``tables.csv`` -- refuses, refuses
#: * ``column_dictionary.csv`` -- refuses (via ``validate_dictionary()``), refuses
#: * ``codes.csv`` -- PASSES, refuses
#: * ``dataset.csv`` -- passes, passes
#:
#: So ``codes.csv`` is here deliberately and is the one entry that is NOT a
#: ``validate_salmon_datapackage()`` blocker. It is reported because
#: ``create_sdp()`` tells the user in as many words that every ``REVIEW:`` entry
#: "must be confirmed or edited", the EDH XML gate refuses one, and
#: ``read_salmon_datapackage()`` warns about one -- so a scan that stayed silent
#: would be the only voice in the package saying the marker is fine. The cost is
#: one extra printed ``set_sdp_code()`` call; the cost of the other choice is a
#: user publishing an unconfirmed draft IRI. ``dataset.csv`` is excluded because
#: no gate refuses a marker there at all, so reporting one would be this scan
#: claiming a block that does not exist.
#:
#: The asymmetry in the middle column is a defect in the VALIDATOR, not here, and
#: it is the same in metasalmon: ``AGENTS.md`` says strict validation fails if any
#: ``REVIEW:`` marker remains, and for ``codes.csv`` and ``dataset.csv`` it does
#: not. Out of scope for the port that added this list, reported with it.
#:
#: Retires when the three gates sweep the same files. At that point this list is
#: derivable from any one of them and should be deleted rather than maintained.
#: ``test_which_files_a_review_marker_actually_blocks`` is what fails if a gate
#: changes which files it sweeps without this list moving with it.
_REVIEW_IRI_FILES = ("tables.csv", "column_dictionary.csv", "codes.csv")

#: The prompt a printed call carries for one IRI field. ``observation_unit_iri``
#: reads better as prose than as its own column name; every other field is
#: literal. One spelling, so the blank branch and the unresolved-marker branch
#: cannot print two different hints for one field.
_IRI_FIELD_HINTS = {"observation_unit_iri": "IRI for what one row represents"}


def _iri_hint(field: str) -> str:
    return _IRI_FIELD_HINTS.get(field, "IRI for " + field)


def _strip_placeholder_prefix(value) -> str:
    """The instruction inside a placeholder, without its marker.

    The placeholder writers put a genuinely useful hint there ("add creator,
    team, or originating program"), so it becomes the prompt in the printed call
    rather than being thrown away and replaced with a generic one.
    """
    text = _PLACEHOLDER_PREFIX.sub("", _text(value))
    return re.sub(r"\s*\.\s*$", "", text.strip())


def settable_required_fields(file_name: str) -> Sequence[str]:
    """The schema-required fields of one metadata file a setter can fill.

    ``constraints.required`` minus the addressing keys. The keys are required
    too, and are deliberately excluded: a blank key is a structural defect
    ``validate_salmon_datapackage()`` reports in every mode, where a blank
    non-key field is incomplete metadata a setter can fill.
    """
    table_name = METADATA_SCHEMA_TABLES[file_name]
    keys = set(METADATA_KEY_FIELDS.get(file_name, ()))
    return [
        name
        for name in sdp_schema_required_field_names(
            table_name, source=_schema_source()
        )
        if name not in keys
    ]


# ---------------------------------------------------------------------------
# The gap scan
# ---------------------------------------------------------------------------


def _gap_row(
    file_name: str,
    field: str,
    current,
    reason: str,
    table_id=None,
    column_name=None,
    code_value=None,
    hint: Optional[str] = None,
) -> dict:
    """One gap row. ``hint`` becomes the placeholder inside the printed call."""
    if hint is None:
        placeholder_hint = _strip_placeholder_prefix(current)
        if placeholder_hint and is_review_placeholder(current):
            hint = placeholder_hint
        else:
            hint = sdp_schema_field_description(
                METADATA_SCHEMA_TABLES[file_name], field, source=_schema_source()
            )
    hint = _text(hint) or field
    return {
        "file": file_name,
        "table_id": _text(table_id),
        "column_name": _text(column_name),
        "code_value": _text(code_value),
        "field": field,
        "current_value": _text(current),
        "reason": reason,
        "hint": hint,
        "note": "",
    }


def _slot_key(file, table_id, column_name, code_value, field) -> str:
    """The key a gap row and a suggestion row share.

    ``target_row_key`` is the producer's own address for the same slot, but a
    gap is found by scanning the metadata rather than the suggestions, so the
    two meet on the identifying columns instead.
    """
    return "|".join(
        _text(part) for part in (file, table_id, column_name, code_value, field)
    )


def _annotate_decisions(gaps: list, target: Path) -> list:
    """Annotate IRI gaps a reviewer already looked at.

    ``reject_suggestion(reason=...)`` records WHY no candidate fitted, and
    :func:`apply_sdp_semantics` persists it -- but a rejection leaves the field
    blank, so the gap comes back here looking exactly like one nobody has ever
    considered. Reading the reason back is what makes the record worth keeping:
    the next person sees "rejected: none of these describe a stream name"
    instead of re-deriving it.
    """
    suggestions_path = target / "semantic_suggestions.csv"
    if not gaps or not suggestions_path.is_file():
        return gaps
    suggestions = read_sdp_csv(suggestions_path)
    needed = ("target_sdp_file", "target_sdp_field", "decision")
    if not all(name in suggestions.columns for name in needed):
        return gaps
    rejected = suggestions[suggestions["decision"].map(_text) == "rejected"]
    if rejected.empty:
        return gaps

    lookup = {}
    for _, row in rejected.iterrows():
        key = _slot_key(
            row.get("target_sdp_file"),
            row.get("table_id"),
            row.get("column_name"),
            row.get("code_value"),
            row.get("target_sdp_field"),
        )
        lookup[key] = _text(row.get("decision_reason"))

    for gap in gaps:
        key = _slot_key(
            gap["file"],
            gap["table_id"],
            gap["column_name"],
            gap["code_value"],
            gap["field"],
        )
        if key not in lookup:
            continue
        reason = lookup[key]
        gap["note"] = (
            "you rejected every candidate here; no reason was recorded"
            if not reason
            else "you rejected every candidate here: " + reason
        )
    return gaps


def _gaps_for_file(frame: pd.DataFrame, file_name: str) -> list:
    """Every field of one metadata file that still blocks strict validation."""
    gaps: list = []
    if frame is None or frame.empty:
        return gaps

    required = set(settable_required_fields(file_name))
    keys = set(METADATA_KEY_FIELDS.get(file_name, ()))
    # Schema order, so the printed calls name their arguments in the order the
    # spec declares them rather than in whichever order the scan happened to
    # run.
    scan_fields = [
        name
        for name in sdp_schema_field_names(
            METADATA_SCHEMA_TABLES[file_name], source=_schema_source()
        )
        if name in frame.columns
    ]

    for index in frame.index:
        row = frame.loc[index]
        address = {
            "table_id": _text(row.get("table_id")),
            "column_name": _text(row.get("column_name")),
            "code_value": _text(row.get("code_value")),
        }

        def add(field, reason, hint=None, row=row, address=address):
            gaps.append(
                _gap_row(
                    file_name,
                    field,
                    row.get(field),
                    reason,
                    hint=hint,
                    **address,
                )
            )

        for field in scan_fields:
            value = row.get(field)
            # A placeholder anywhere is refused, whether or not the schema calls
            # the field required: ``observation_unit`` is optional and still
            # gets one.
            if is_review_placeholder(value):
                add(field, "placeholder")
                continue
            # An unresolved ``REVIEW:`` marker in a declared ``*_iri`` field is
            # refused too, by ``_collect_review_iri_issues()``. Handled HERE
            # rather than in the two branches below so it reaches
            # ``constraint_iri``, ``statistical_modifier_iri`` and a code's
            # ``term_iri`` as well -- the validator sweeps every ``*_iri`` column
            # of these files, and the two have to keep agreeing about what
            # blocks. Scoped to the SCHEMA-DECLARED fields because every row here
            # must print a runnable call and ``_set_sdp_metadata()`` refuses an
            # undeclared field; an undeclared ``*_iri`` column was never in this
            # scan at all and stays the validator's to report.
            if (
                file_name in _REVIEW_IRI_FILES
                and str(field).endswith("_iri")
                and _is_unresolved_iri(value)
            ):
                add(field, "iri", hint=_iri_hint(field))
                continue
            if field in keys:
                continue
            if field in required and _is_unfilled_metadata(value):
                add(field, "required")

        if file_name == "tables.csv" and "observation_unit_iri" in frame.columns:
            # Blank or a prose placeholder: the schema calls this
            # ``recommended``, and strict validation refuses a blank one anyway
            # (``_collect_missing_table_observation_unit_iri_issues()``). The
            # schema is not the authority on what blocks; the validator is. A
            # value still carrying ``REVIEW:`` was already reported above, so
            # the prose test is what belongs here -- using the marker test in
            # both places would report one field twice.
            if _is_unfilled_metadata(row.get("observation_unit_iri")):
                add(
                    "observation_unit_iri",
                    "iri",
                    hint=_iri_hint("observation_unit_iri"),
                )

        if (
            file_name == "column_dictionary.csv"
            and _text(row.get("column_role")) == "measurement"
        ):
            for field in MEASUREMENT_IRI_FIELDS:
                if field not in frame.columns:
                    continue
                if _is_unfilled_metadata(row.get(field)):
                    add(field, "iri", hint=_iri_hint(field))
    return gaps


class MetadataReview:
    """Every field a package still needs, with the call that fills it.

    ``print(review)`` renders the console view; ``review.rows`` reaches the
    underlying frame, one row per unfilled field.
    """

    __slots__ = ("_rows", "path")

    def __init__(self, rows: pd.DataFrame, path: Optional[str] = None):
        self._rows = rows
        self.path = path

    @property
    def rows(self) -> pd.DataFrame:
        return self._rows.copy()

    def to_frame(self) -> pd.DataFrame:
        return self.rows

    def __getitem__(self, key):
        return self._rows[key]

    def __len__(self) -> int:
        return len(self._rows)

    @property
    def empty(self) -> bool:
        return self._rows.empty

    @property
    def columns(self):
        return self._rows.columns

    def render_lines(self, path_expr: Optional[str] = None) -> list:
        """The console view as a plain list of strings."""
        if path_expr is None:
            path_expr = _metadata_path_expr(self.path)
        return _render_metadata_lines(self, path_expr)

    def __str__(self) -> str:
        return "\n".join(self.render_lines()) + "\n"

    def __repr__(self) -> str:
        return self.__str__()


def _metadata_path_expr(path: Optional[str]) -> str:
    """How the printed call should spell the package path.

    The name the user bound the path to when there is one, so the printed line
    can be pasted; the quoted literal otherwise.
    """
    default = "path" if path is None else _quote(path)
    if path is None:
        return default
    return _binding_name_for(
        lambda value: isinstance(value, str) and value == path, default
    )


def review_metadata(path) -> MetadataReview:
    """Report the metadata a package still needs, with the call that fills it.

    Lists every field that still blocks
    ``validate_salmon_datapackage(path, require_iris=True)``, and prints the
    exact :func:`set_sdp_dataset` / :func:`set_sdp_table` /
    :func:`set_sdp_column` / :func:`set_sdp_code` call that fills it. Replace
    the ``<...>`` placeholder in the printed call with the real value and paste
    it -- the paste is the audit trail, just as it is for
    :func:`accept_suggestion`.

    This is the companion to :func:`review_semantics`, and it sees something
    that review structurally cannot: **a slot with no candidates at all**.
    :func:`review_semantics` builds its queue from retrieved suggestions, so a
    field nothing was found for never appears there. :func:`review_metadata`
    builds its list from the package's own required-field rules, so an empty
    shortlist and a full one look the same to it.

    What it reports:

    * unresolved ``MISSING DESCRIPTION:`` / ``MISSING METADATA:`` /
      ``REVIEW REQUIRED:`` placeholders in any metadata field;
    * schema-required fields (``constraints.required``) that are blank -- a
      column the file does not have counts as blank in every row;
    * any *schema-declared* ``*_iri`` field of ``tables.csv``,
      ``column_dictionary.csv`` or ``codes.csv`` still carrying an unresolved
      ``REVIEW:`` marker. These also appear in :func:`review_semantics`, which
      has their candidates; they are listed here too because this is the scan
      that promises to name everything blocking strict validation, and a package
      left part-decided is the common case. ``_REVIEW_IRI_FILES`` records which
      gate refuses a marker in which file -- they differ, and ``codes.csv`` is
      reported although ``validate_salmon_datapackage()`` does not yet refuse
      it;
    * measurement columns missing ``term_iri``, ``property_iri``,
      ``entity_iri`` or ``unit_iri``;
    * ``tables.csv`` rows with a blank ``observation_unit_iri``.

    It never contacts an LLM, and under the shipped schema settings it never
    contacts a network: the SDP schema it reads ``constraints.required`` from
    is the copy bundled with metasalmonpy. When ``set_sdp_schema_source()`` or
    ``set_sdp_schema_base_url()``, or ``METASALMONPY_SDP_SCHEMA_SOURCE`` or
    ``METASALMONPY_SDP_SCHEMA_BASE_URL``, selects a different schema, it reads
    that one, as the package writers do: from this process's cache once they
    have loaded it, and otherwise by loading it as they would.

    Returns
    -------
    MetadataReview
        One row per unfilled field, empty when nothing is outstanding.
    """
    from .package_io import _metadata_path

    target = Path(path)
    if not target.is_dir():
        raise NotADirectoryError(
            f"path must be an existing Salmon Data Package directory: {target}"
        )

    gaps: list = []
    for file_name, table_name in METADATA_SCHEMA_TABLES.items():
        located = _metadata_path(target, file_name)
        if not located.is_file():
            continue
        # A column the file does not have is blank in every row -- the rule the
        # validator applies, so the two keep agreeing about what blocks.
        # Aligned to the schema here so the scan sees one shape per file and an
        # absent required column is a gap whose printed call fills it:
        # ``_set_sdp_metadata()`` adds the column it is asked to write.
        frame = align_columns(
            read_sdp_csv(located),
            sdp_schema_field_names(table_name, source=_schema_source()),
        )
        gaps.extend(_gaps_for_file(frame, file_name))

    gaps = _annotate_decisions(gaps, target)
    rows = pd.DataFrame(gaps, columns=GAP_COLUMNS)
    return MetadataReview(rows, str(target))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _value_template(hint) -> str:
    """The value slot in a printed call.

    Deliberately NOT a plausible value: a call pasted without editing must fail
    loudly, not quietly write "..." into ``creator`` and let strict validation
    pass on a package that says nothing. :func:`_assert_metadata_value` refuses
    anything of this shape and says so.
    """
    text = re.sub(r"\s+", " ", _text(hint))
    text = re.sub(r'[<>"\\]', "", text)
    if len(text) > 60:
        text = text[:57] + "..."
    return "<" + text + ">"


def _is_value_template(value) -> bool:
    return bool(re.match(r"^<.*>$", _text(value)))


def _setter_call(rows: pd.DataFrame, path_expr: str) -> list:
    """Which setter owns a file, and which arguments address the row.

    Returned as one line per argument rather than as a single string, and
    DELIBERATELY not wrapped: text wrapping normalises whitespace, so wrapping a
    call would silently rewrite the text inside its own string literals -- a
    printed call is code, and the only safe way to break code across lines is
    at an argument boundary.
    """
    head = rows.iloc[0]
    file_name = _text(head["file"])
    table_id = _text(head["table_id"])
    column_name = _text(head["column_name"])
    code_value = _text(head["code_value"])

    if file_name == "dataset.csv":
        fn, args = "set_sdp_dataset", []
    elif file_name == "tables.csv":
        fn, args = "set_sdp_table", [_quote(table_id)]
    elif file_name == "column_dictionary.csv":
        fn, args = "set_sdp_column", [
            _quote(column_name),
            "table=" + _quote(table_id),
        ]
    elif file_name == "codes.csv":
        fn, args = "set_sdp_code", [
            _quote(column_name),
            _quote(code_value),
            "table=" + _quote(table_id),
        ]
    else:
        return []

    values = [
        _text(row["field"]) + "=" + _quote(_value_template(row["hint"]))
        for _, row in rows.iterrows()
    ]
    lines = [fn + "(" + ", ".join([path_expr] + args) + ","]
    for position, value in enumerate(values):
        lines.append(
            "  " + value + ("," if position < len(values) - 1 else "")
        )
    lines.append(")")
    return lines


def _row_id(row) -> str:
    return "|".join(
        _text(row[key])
        for key in ("file", "table_id", "column_name", "code_value")
    )


_REASON_NOTES = {
    "placeholder": "placeholder text, refused by strict validation",
    "required": "required by the SDP schema and blank",
    "iri": "required IRI and not yet decided",
}


def _render_metadata_lines(review: MetadataReview, path_expr: str) -> list:
    rows = review.rows
    if rows.empty:
        return [
            "No outstanding metadata.",
            "",
            "Every required field is filled and no placeholders remain.",
            "Check it with validate_salmon_datapackage(path, require_iris=True).",
            "",
        ]

    lines: list = []
    all_ids = [_row_id(rows.loc[index]) for index in rows.index]
    for row_id in dict.fromkeys(all_ids):
        group = rows[[value == row_id for value in all_ids]]
        head = group.iloc[0]
        heading_parts = [
            _text(head["file"]),
            _text(head["table_id"]),
            _text(head["column_name"]),
            _text(head["code_value"]),
        ]
        lines.append(
            _review_rule(" · ".join(part for part in heading_parts if part))
        )

        for _, row in group.iterrows():
            reason = _text(row["reason"])
            lines.append(
                "   "
                + _text(row["field"])
                + ": "
                + _REASON_NOTES.get(reason, reason)
            )
            current = _text(row["current_value"])
            if current:
                lines.extend(_review_wrap(current, indent="      "))
            note = _text(row["note"])
            if note:
                lines.extend(_review_wrap(note, indent="      "))
        lines.append("")
        lines.extend("   " + line for line in _setter_call(group, path_expr))
        lines.append("")

    iri_rows = int((rows["reason"] == "iri").sum())
    total = len(rows)
    lines.extend(
        [
            _review_rule("next"),
            "   "
            + str(total)
            + " field"
            + ("" if total == 1 else "s")
            + " still block"
            + ("s" if total == 1 else "")
            + " strict validation.",
            "   Replace each <...> with the real value, then paste the calls above.",
        ]
    )
    if iri_rows:
        lines.append(
            "   "
            + str(iri_rows)
            + " of them "
            + ("is an IRI" if iri_rows == 1 else "are IRIs")
            + " -- review_semantics() shows candidates for any that have them."
        )
    lines.append(
        "   Then: validate_salmon_datapackage("
        + path_expr
        + ", require_iris=True)"
    )
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# The setters
# ---------------------------------------------------------------------------


def _assert_metadata_value(value, field: str) -> object:
    if isinstance(value, (list, tuple, set, dict, pd.Series)):
        raise ValueError(f"{field} must be a single value.")
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return pd.NA
    text = _text(value)
    if _is_value_template(text):
        raise ValueError(
            f"{field} still holds the placeholder from review_metadata(): "
            + text
            + ". Replace the <...> text with the real value before running "
            "the call."
        )
    if not text:
        raise ValueError(
            f"{field} must not be blank. Pass None to clear a field on purpose."
        )
    return text


def _resolve_metadata_row(
    frame: pd.DataFrame, file_name: str, keys: dict
) -> object:
    """Resolve the addressed row, or raise naming the disambiguating argument.

    Every comparison is missing-safe for the reason ``_match_slot_rows()``
    records: a comparison against a missing value must drop the row rather than
    keep a phantom, and the visible symptom of getting it wrong is an error
    message that names rows the package does not have.
    """
    keep = pd.Series(True, index=frame.index)
    for key, value in keys.items():
        if value is None:
            continue
        if key not in frame.columns:
            raise ValueError(
                f"{file_name} has no {key} column to match on."
            )
        keep &= frame[key].map(_text) == _text(value)
    hits = list(frame.index[keep])

    if len(hits) == 1:
        return hits[0]

    asked = ", ".join(
        f"{key}=" + _quote(_text(value)) for key, value in keys.items()
    )
    if not hits:
        available = list(
            dict.fromkeys(
                " · ".join(
                    _text(frame.at[index, key])
                    for key in keys
                    if key in frame.columns
                )
                for index in frame.index
            )
        )
        raise ValueError(
            f"No {file_name} row matches that address. Asked for: "
            + asked
            + ". Available: "
            + "; ".join(available[:20])
            + "."
        )
    raise ValueError(
        f"That address matches {len(hits)} rows in {file_name}. Asked for: "
        + asked
        + ". Add table= to say which."
    )


def _descriptor_mirrored_fields(file_name: str) -> Sequence[str]:
    """Which descriptor keys one metadata field mirrors.

    A field with no entry has no descriptor twin and needs no patch --
    ``observation_unit_iri`` is the clearest case: strict validation requires it
    and ``datapackage.json`` has nowhere to put it.
    """
    from .package_io import _descriptor_field_keys

    if file_name == "dataset.csv":
        return (
            "title",
            "description",
            "creator",
            "contact_name",
            "contact_email",
            "contact_org",
            "license",
            "temporal_start",
            "temporal_end",
        )
    if file_name == "tables.csv":
        return ("table_label", "description", "primary_key")
    if file_name == "column_dictionary.csv":
        return (
            "column_label",
            "value_type",
            "column_description",
            "required",
        ) + tuple(_descriptor_field_keys())
    return ()


def _descriptor_sync_metadata_row(
    descriptor, file_name: str, frame: pd.DataFrame, index, fields
):
    """Patch the descriptor for the row that changed, narrowly.

    NOT a rebuild: a rebuild would discard descriptor content a user added by
    hand, which is the decision :func:`apply_sdp_semantics` already made and
    this follows. Where the descriptor holds a COMPOSITE of several CSV fields
    -- ``contributors`` is built from ``creator``, ``contact_name``,
    ``contact_email`` and ``contact_org`` together -- the whole composite is
    rebuilt from the row, because there is no narrower unit. The CSVs are
    canonical and ``datapackage.json`` is the derived export, so resyncing it
    from the row is the documented direction of truth.
    """
    from .package_io import (
        _descriptor_apply_dataset_meta,
        _descriptor_apply_resource_meta,
        _descriptor_field_entry,
        _descriptor_field_keys,
    )

    if not isinstance(descriptor, dict):
        return None
    mirrored = [
        name for name in fields if name in _descriptor_mirrored_fields(file_name)
    ]
    if not mirrored:
        return None

    row = frame.loc[index]
    if file_name == "dataset.csv":
        return _descriptor_apply_dataset_meta(descriptor, row)

    table_id = _text(row.get("table_id"))
    for resource in descriptor.get("resources") or []:
        if not isinstance(resource, dict):
            continue
        if _text(resource.get("name")) != table_id:
            continue
        if file_name == "tables.csv":
            _descriptor_apply_resource_meta(resource, row)
            continue
        schema = resource.get("schema")
        if not isinstance(schema, dict) or not isinstance(
            schema.get("fields"), list
        ):
            continue
        column_name = _text(row.get("column_name"))
        rebuilt = _descriptor_field_entry(row)
        base_keys = {
            "column_label": "title",
            "value_type": "type",
            "column_description": "description",
        }
        for field in schema["fields"]:
            if not isinstance(field, dict):
                continue
            if _text(field.get("name")) != column_name:
                continue
            for key, descriptor_key in base_keys.items():
                if key in mirrored:
                    # Present-but-null is the shape the writer emits for an
                    # absent value, so assign None rather than deleting.
                    field[descriptor_key] = rebuilt.get(descriptor_key)
            if "required" in mirrored:
                if "constraints" in rebuilt:
                    field["constraints"] = rebuilt["constraints"]
                else:
                    field.pop("constraints", None)
            for key, descriptor_key in _descriptor_field_keys().items():
                if key not in mirrored:
                    continue
                if descriptor_key in rebuilt:
                    field[descriptor_key] = rebuilt[descriptor_key]
                else:
                    field.pop(descriptor_key, None)
    return descriptor


def _set_sdp_metadata(
    path, file_name: str, keys: dict, values: dict, quiet: bool = False
) -> Path:
    """The engine every ``set_sdp_*()`` wrapper delegates to.

    One logical edit changes a metadata CSV AND the descriptor keys that
    duplicate it, so both are rendered to bytes first and installed as ONE
    transactional set -- per-file atomicity would still leave a window where the
    CSV is new and the descriptor is old, and
    ``datapackage_consistent_with_csv_metadata`` is one of the dead rules in
    ``sdp.rules.yaml``, so nothing would ever detect it.
    """
    from .package_io import (
        _assert_managed_paths_contained,
        _commit_package_write,
        _datapackage_json_bytes,
        _metadata_csv_bytes,
        _metadata_path,
    )

    target = Path(path)
    if not target.is_dir():
        raise NotADirectoryError(
            f"path must be an existing Salmon Data Package directory: {target}"
        )

    values = {name: value for name, value in values.items() if value is not None}
    if not values:
        raise ValueError(
            'Nothing to set. Name at least one field, for example '
            'creator="...".'
        )

    table_name = METADATA_SCHEMA_TABLES[file_name]
    declared = sdp_schema_field_names(table_name, source=_schema_source())
    unknown = [name for name in values if name not in declared]
    if unknown:
        raise ValueError(
            f"{file_name} has no such field"
            + ("" if len(unknown) == 1 else "s")
            + ": "
            + ", ".join(unknown)
            + ". Available: "
            + ", ".join(declared)
            + "."
        )
    protected = [
        name for name in values if name in METADATA_KEY_FIELDS.get(file_name, ())
    ]
    if protected:
        raise ValueError(
            ("This field addresses" if len(protected) == 1 else "These fields address")
            + " the row and cannot be set: "
            + ", ".join(protected)
            + ". Rebuild the package to change how a row is identified."
        )

    # Containment BEFORE any read: a ``metadata/`` replaced by a symlink would
    # otherwise be read, and then written through, outside the package.
    managed = [_metadata_path(target, file_name), target / "datapackage.json"]
    _assert_managed_paths_contained(target, managed)

    located = _metadata_path(target, file_name)
    if not located.is_file():
        raise FileNotFoundError(
            f"This package has no {file_name}. Rebuild it with create_sdp() or "
            "write_salmon_datapackage()."
        )
    frame = read_sdp_csv(located)

    # Address FIRST, value second: a call pasted without editing its ``<...>``
    # placeholder must prove the address resolves before it is refused, so a
    # printed call that names a column the package does not have fails on the
    # address rather than being masked by the placeholder guard.
    index = _resolve_metadata_row(frame, file_name, keys)

    for field, raw in values.items():
        value = _assert_metadata_value(raw, field)
        if field not in frame.columns:
            frame[field] = pd.NA
        frame.at[index, field] = value

    # Ordered by ``declared``, the names the field check above was made
    # against, as metasalmon's setter orders by them since B-175. Under a
    # selected schema that is the selected order; under the bundled one it is
    # the order the static column lists also give.
    writes = {located: _metadata_csv_bytes(align_columns(frame, declared))}

    descriptor_path = target / "datapackage.json"
    if descriptor_path.is_file():
        from .metadata_write import _read_descriptor

        descriptor = _read_descriptor(descriptor_path)
        patched = _descriptor_sync_metadata_row(
            descriptor, file_name, frame, index, list(values)
        )
        if patched is not None:
            writes[descriptor_path] = _datapackage_json_bytes(patched)

    _commit_package_write(
        target, writes, managed_paths=list(writes), prune=False
    )

    if not quiet:
        fields = ", ".join(values)
        plural = "" if len(values) == 1 else "s"
        print(f"Set {len(values)} field{plural} in {file_name}: {fields}.")
    return target


def _setter_extras(extras: dict) -> dict:
    """``**kwargs`` is the schema escape hatch.

    The named arguments cover the fields users actually fill, and every other
    declared field stays reachable without this module having to re-spell a
    schema that is loaded at runtime and can move under it.
    """
    return dict(extras)


def set_sdp_dataset(
    path,
    *,
    title=None,
    description=None,
    creator=None,
    contact_name=None,
    contact_email=None,
    contact_org=None,
    license=None,
    quiet: bool = False,
    **extras,
) -> Path:
    """Fill in ``metadata/dataset.csv``'s free-text metadata.

    The scriptable replacement for opening ``metadata/*.csv`` in a spreadsheet.
    Addresses one row and writes the named fields into it, keeping
    ``datapackage.json`` in step in the same transactional write. Every field
    the SDP schema declares for the file can be set: the ones most often
    unfilled are named arguments for discoverability, and the rest are passed
    through ``**extras`` and checked against the schema, so a misspelled
    ``licence=`` is an error rather than a silent no-op.

    These are the calls :func:`review_metadata` prints. Replace the ``<...>``
    placeholder with the real value and paste -- pasting one unedited is refused
    with a message saying so, because a package whose ``creator`` reads
    ``<add creator, team, or originating program>`` would pass strict validation
    while saying nothing.

    Pass ``None`` for a field you are not setting; pass ``pandas.NA`` to clear
    one deliberately. A blank string is refused as ambiguous.

    The fields a setter accepts come from the same SDP schema
    :func:`review_metadata` reads, so a call it prints is one the setter
    accepts. Under the shipped schema settings that is the copy bundled with
    metasalmonpy, and neither contacts a network.
    """
    return _set_sdp_metadata(
        path,
        "dataset.csv",
        keys={},
        values={
            "title": title,
            "description": description,
            "creator": creator,
            "contact_name": contact_name,
            "contact_email": contact_email,
            "contact_org": contact_org,
            "license": license,
            **_setter_extras(extras),
        },
        quiet=quiet,
    )


def set_sdp_table(
    path,
    table,
    *,
    table_label=None,
    description=None,
    observation_unit=None,
    observation_unit_iri=None,
    quiet: bool = False,
    **extras,
) -> Path:
    """Fill in one ``metadata/tables.csv`` row. See :func:`set_sdp_dataset`."""
    return _set_sdp_metadata(
        path,
        "tables.csv",
        keys={"table_id": table},
        values={
            "table_label": table_label,
            "description": description,
            "observation_unit": observation_unit,
            "observation_unit_iri": observation_unit_iri,
            **_setter_extras(extras),
        },
        quiet=quiet,
    )


def set_sdp_column(
    path,
    column,
    *,
    table=None,
    column_label=None,
    column_description=None,
    unit_label=None,
    term_iri=None,
    property_iri=None,
    entity_iri=None,
    unit_iri=None,
    quiet: bool = False,
    **extras,
) -> Path:
    """Fill in one ``metadata/column_dictionary.csv`` row.

    ``table`` is needed only when the column name appears in more than one
    table; :func:`review_metadata` always prints it. Use the IRI arguments for a
    slot retrieval found no candidate for; use :func:`accept_suggestion` when
    there is a shortlist to choose from. See :func:`set_sdp_dataset`.
    """
    return _set_sdp_metadata(
        path,
        "column_dictionary.csv",
        keys={"table_id": table, "column_name": column},
        values={
            "column_label": column_label,
            "column_description": column_description,
            "unit_label": unit_label,
            "term_iri": term_iri,
            "property_iri": property_iri,
            "entity_iri": entity_iri,
            "unit_iri": unit_iri,
            **_setter_extras(extras),
        },
        quiet=quiet,
    )


def set_sdp_code(
    path,
    column,
    code_value,
    *,
    table=None,
    code_label=None,
    code_description=None,
    term_iri=None,
    vocabulary_iri=None,
    quiet: bool = False,
    **extras,
) -> Path:
    """Fill in one ``metadata/codes.csv`` row. See :func:`set_sdp_dataset`."""
    return _set_sdp_metadata(
        path,
        "codes.csv",
        keys={
            "table_id": table,
            "column_name": column,
            "code_value": code_value,
        },
        values={
            "code_label": code_label,
            "code_description": code_description,
            "term_iri": term_iri,
            "vocabulary_iri": vocabulary_iri,
            **_setter_extras(extras),
        },
        quiet=quiet,
    )
