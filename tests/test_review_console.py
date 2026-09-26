"""The Python-native semantic review (stream S5 port, metasalmon 0.5.0).

The load-bearing test in this file is
``test_every_printed_accept_call_produces_the_decision_it_claims``: the printed
call is the feature, so the tests ``exec()`` it. A printed call that names a
column the package does not have passes every substring assertion ever written
and fails the moment anyone pastes it.
"""

from __future__ import annotations

import functools
import re
import warnings
from pathlib import Path

import pandas as pd
import pytest

from metasalmonpy import (
    accept_suggestion,
    apply_sdp_semantics,
    create_sdp,
    reject_suggestion,
    review_semantics,
    semantic_llm_assessments,
    semantic_suggestions,
)
from metasalmonpy.metadata import read_sdp_csv
from metasalmonpy.package_io import _suggestions_csv_bytes
from metasalmonpy.review_console import (
    SemanticReview,
    _accept_call,
    _is_review_iri,
    _reject_call,
    _strip_review_iri,
)

SPAWNER_IRI = "https://w3id.org/smn/SpawnerAbundance"
WATERCOURSE_IRI = "https://w3id.org/smn/WatercourseDesignation"


def _suggestion_row(**overrides) -> dict:
    row = {
        "dataset_id": "demo-1",
        "table_id": "spawners",
        "column_name": "spawner_count",
        "code_value": pd.NA,
        "dictionary_role": "variable",
        "target_scope": "column",
        "target_sdp_file": "column_dictionary.csv",
        "target_sdp_field": "term_iri",
        "target_row_key": "demo-1/spawners/spawner_count",
        "label": "Spawner Abundance",
        "iri": SPAWNER_IRI,
        "source": "smn",
        "ontology": "smn",
        "definition": "The number of mature salmon returning to spawn.",
        "score": 4.9,
    }
    row.update(overrides)
    return row


def _dictionary_with(suggestions: list) -> pd.DataFrame:
    dictionary = pd.DataFrame(
        [
            {
                "dataset_id": "demo-1",
                "table_id": "spawners",
                "column_name": "spawner_count",
                "column_role": "measurement",
                "term_iri": pd.NA,
            }
        ]
    )
    dictionary.attrs["semantic_suggestions"] = pd.DataFrame(suggestions)
    return dictionary


# ---------------------------------------------------------------------------
# The accessors
# ---------------------------------------------------------------------------


def test_semantic_suggestions_reads_a_dictionary_attribute():
    dictionary = _dictionary_with([_suggestion_row()])
    found = semantic_suggestions(dictionary)
    assert found is not None
    assert found["iri"].iloc[0] == SPAWNER_IRI


def test_semantic_suggestions_is_none_when_nothing_is_attached():
    # Deliberately None rather than an empty frame: these accessors replace a
    # raw ``attrs`` lookup and must answer an ``is None`` test the same way.
    assert semantic_suggestions(pd.DataFrame({"column_name": ["x"]})) is None


def test_semantic_suggestions_reads_an_artifact_mapping():
    dictionary = _dictionary_with([_suggestion_row()])
    artifacts = {"dict": dictionary, "semantic_suggestions": None}
    assert semantic_suggestions(artifacts)["iri"].iloc[0] == SPAWNER_IRI


def test_semantic_suggestions_reads_a_written_package(seeded_package):
    found = semantic_suggestions(str(seeded_package))
    assert found is not None
    assert "target_sdp_field" in found.columns


def test_semantic_suggestions_is_none_for_a_package_without_the_file(tmp_path):
    (tmp_path / "empty").mkdir()
    assert semantic_suggestions(str(tmp_path / "empty")) is None


def test_semantic_llm_assessments_is_none_for_a_package_path(seeded_package):
    # Assessments are not written into the package, so a package on disk
    # cannot carry them.
    assert semantic_llm_assessments(str(seeded_package)) is None


def test_an_accessor_refuses_a_shape_it_cannot_read():
    with pytest.raises(TypeError, match="dictionary, an artifact mapping"):
        semantic_suggestions(42)


# ---------------------------------------------------------------------------
# Building the queue
# ---------------------------------------------------------------------------


def test_review_semantics_queues_one_row_per_candidate():
    dictionary = _dictionary_with(
        [
            _suggestion_row(),
            _suggestion_row(label="Escapement", iri="https://example.org/Esc"),
        ]
    )
    review = review_semantics(dictionary)
    assert isinstance(review, SemanticReview)
    assert len(review) == 2
    # One slot, ranks in the order the producer emitted them: rank is a
    # position, not a sort.
    assert review["slot_id"].nunique() == 1
    assert list(review["rank"]) == [1, 2]
    assert list(review["label"]) == ["Spawner Abundance", "Escapement"]


def test_review_semantics_never_calls_retrieval(monkeypatch):
    """The sentinel that pins "this never contacts a network or an LLM"."""
    from metasalmonpy import term_search

    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("review_semantics() reached retrieval")

    monkeypatch.setattr(term_search, "find_terms", explode)
    review = review_semantics(_dictionary_with([_suggestion_row()]))
    assert len(review) == 1


def test_review_semantics_refuses_a_package_with_no_suggestions(tmp_path):
    (tmp_path / "bare").mkdir()
    with pytest.raises(ValueError, match="No semantic suggestions to review"):
        review_semantics(str(tmp_path / "bare"))


def test_a_columns_typo_is_an_error_naming_the_columns_that_exist():
    # Filtering first and then reporting an empty queue told a user who
    # mistyped a column name that their package was finished.
    dictionary = _dictionary_with([_suggestion_row()])
    with pytest.raises(ValueError) as caught:
        review_semantics(dictionary, columns=["TYPO"])
    message = str(caught.value)
    assert "TYPO" in message
    assert "spawner_count" in message


def test_a_columns_value_that_exists_filters_rather_than_erroring():
    dictionary = _dictionary_with(
        [
            _suggestion_row(),
            _suggestion_row(
                column_name="stream_name",
                target_row_key="demo-1/spawners/stream_name",
            ),
        ]
    )
    review = review_semantics(dictionary, columns=["stream_name"])
    assert set(review["column_name"]) == {"stream_name"}


def test_a_filled_slot_is_out_of_the_default_queue():
    dictionary = _dictionary_with([_suggestion_row()])
    dictionary.loc[0, "term_iri"] = SPAWNER_IRI
    assert len(review_semantics(dictionary)) == 0
    assert len(review_semantics(dictionary, include_filled=True)) == 1


def test_a_review_marked_slot_is_still_unfilled():
    # ``REVIEW:``-prefixed values are non-blank, which is why the queue cannot
    # just test for emptiness.
    dictionary = _dictionary_with([_suggestion_row()])
    dictionary.loc[0, "term_iri"] = "REVIEW:" + SPAWNER_IRI
    assert len(review_semantics(dictionary)) == 1


def test_max_candidates_limits_the_shortlist():
    dictionary = _dictionary_with(
        [_suggestion_row(iri=f"https://example.org/{n}") for n in range(8)]
    )
    assert len(review_semantics(dictionary, max_candidates=3)) == 3
    assert len(review_semantics(dictionary, max_candidates=None)) == 8


def test_a_target_this_review_cannot_decide_is_not_queued(capsys):
    # ``dataset.csv`` targets a comma-joined keywords list, so it has no
    # "accept this candidate" semantics at all.
    dictionary = _dictionary_with(
        [
            _suggestion_row(),
            _suggestion_row(
                target_sdp_file="dataset.csv",
                target_sdp_field="keywords",
                target_row_key="demo-1",
            ),
        ]
    )
    review = review_semantics(dictionary)
    assert set(review["target_file"]) == {"column_dictionary.csv"}
    assert "dataset.csv" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Rendering, and the printed call
# ---------------------------------------------------------------------------


def test_an_empty_queue_says_so_without_claiming_success():
    dictionary = _dictionary_with([_suggestion_row()])
    dictionary.loc[0, "term_iri"] = SPAWNER_IRI
    text = str(review_semantics(dictionary))
    assert "Nothing left to review." in text
    assert "review_metadata(path)" in text


def test_a_definition_containing_braces_prints_literally():
    # The plain-text branch's own pinned test: no formatter runs on external
    # text, so a brace is a brace. A static guard cannot see this path.
    dictionary = _dictionary_with(
        [_suggestion_row(definition="Counts in {reach} over %(year)s")]
    )
    text = str(review_semantics(dictionary))
    assert "{reach}" in text
    assert "%(year)s" in text
    assert "{{reach}}" not in text


def test_every_printed_accept_call_produces_the_decision_it_claims():
    """The printed call is the contract, so the test runs it.

    Two real defects in the R original were found by exactly this test: a
    table-level slot printed ``accept_suggestion(review, "NA", "entity", ...)``
    naming a column that does not exist, and a phantom missing row made an
    unrelated dictionary slot print a spurious ``table=``.
    """
    dictionary = _dictionary_with(
        [
            # A measurement column's own slots.
            _suggestion_row(),
            _suggestion_row(
                dictionary_role="entity",
                target_sdp_field="entity_iri",
            ),
            # A second column.
            _suggestion_row(
                column_name="stream_name",
                target_row_key="demo-1/spawners/stream_name",
            ),
            # A table-level slot: no column at all.
            _suggestion_row(
                column_name=pd.NA,
                dictionary_role="entity",
                target_sdp_file="tables.csv",
                target_sdp_field="observation_unit_iri",
                target_row_key="demo-1/spawners",
            ),
            # Two code-level slots on a column that has no column-level slot of
            # the same role, which is the shape the seeder emits for a
            # categorical column.
            _suggestion_row(
                column_name="stream_name",
                code_value="Goldstream",
                dictionary_role="entity",
                target_sdp_file="codes.csv",
                target_sdp_field="term_iri",
                target_row_key="demo-1/spawners/stream_name/Goldstream",
            ),
            _suggestion_row(
                column_name="stream_name",
                code_value="Craigflower",
                dictionary_role="entity",
                target_sdp_file="codes.csv",
                target_sdp_field="term_iri",
                target_row_key="demo-1/spawners/stream_name/Craigflower",
            ),
        ]
    )
    review = review_semantics(dictionary)
    printed = [
        line.strip()[len("review = "):]
        for line in review.render_lines(object_name="review")
        if line.strip().startswith("review = accept_suggestion(")
    ]
    assert len(printed) == len(review)

    namespace = {
        "accept_suggestion": accept_suggestion,
        "review": review,
    }
    for call in printed:
        result = eval(call, namespace)  # noqa: S307 - the call is the contract
        decided = result.rows[result.rows["decision"].notna()]
        assert len(decided) == 1, call
        # Exactly one slot decided, and it is a real slot.
        assert decided["slot_id"].iloc[0] in set(review["slot_id"])
        assert decided["decision_iri"].iloc[0] == SPAWNER_IRI


def test_every_printed_reject_call_runs():
    dictionary = _dictionary_with(
        [
            _suggestion_row(),
            _suggestion_row(
                column_name=pd.NA,
                dictionary_role="entity",
                target_sdp_file="tables.csv",
                target_sdp_field="observation_unit_iri",
                target_row_key="demo-1/spawners",
            ),
        ]
    )
    review = review_semantics(dictionary)
    printed = [
        line.strip().split("# ")[0].strip()[len("review = "):]
        for line in review.render_lines(object_name="review")
        if line.strip().startswith("review = reject_suggestion(")
    ]
    assert len(printed) == 2
    namespace = {"reject_suggestion": reject_suggestion, "review": review}
    for call in printed:
        result = eval(call, namespace)  # noqa: S307
        assert (result.rows["decision"] == "reject").any(), call


# ---------------------------------------------------------------------------
# A measurement column with a code list: hub queue B-242, the mirror half of
# metasalmon's B-151. Each test mirrors one in metasalmon's
# tests/testthat/test-review-console.R.
# ---------------------------------------------------------------------------

MEASUREMENT_COLUMN_SLOT = (
    "column_dictionary.csv|demo-1/spawners/spawner_count|entity_iri"
)


def _measurement_code_review() -> SemanticReview:
    """A measurement column whose codes share its roles.

    A **measurement** column's own ``entity_iri`` and ``constraint_iri``
    targets share their roles with its codes' ``codes.csv`` targets, which a
    measurement parent gives the roles constraint, entity and method -- see
    ``semantics.py``'s ``["constraint", "entity", "method"]`` role set. So one
    (column, role) pair names the column's own slot AND a slot per code. An
    omitted ``code_value`` matches every code, so the column's own slot printed
    ``accept_suggestion(review, "spawner_count", "entity", rank=1,
    table="spawners")``, which matched three slots and raised: the printed call
    that cannot run. Built the way discovery builds it: the three roles of one
    code share that code's slot. Mirrors R's ``measurement_code_review()``.
    """

    def code_rows(code: str) -> list:
        return [
            _suggestion_row(
                code_value=code,
                dictionary_role=role,
                target_scope="code",
                target_sdp_file="codes.csv",
                target_sdp_field="term_iri",
                target_row_key=f"demo-1/spawners/spawner_count/{code}",
                label=f"{role} term for {code}",
                iri=f"https://example.org/{role}/{code}",
            )
            for role in ("constraint", "entity", "method")
        ]

    dictionary = _dictionary_with(
        [
            _suggestion_row(
                dictionary_role="entity",
                target_sdp_field="entity_iri",
                label="Spawner",
                iri="https://w3id.org/smn/Spawner",
            ),
            _suggestion_row(
                dictionary_role="constraint",
                target_sdp_field="constraint_iri",
                label="Wild origin",
                iri="https://example.org/constraint/column",
            ),
            *code_rows("-9"),
            *code_rows("-99"),
        ]
    )
    return review_semantics(dictionary)


def _refusal_options(refused: pytest.ExceptionInfo) -> list:
    """The arguments an ambiguity refusal tells the caller to add, one each."""
    message = str(refused.value)
    return message.split("to say which: ", 1)[1].rstrip(".").split("; ")


def _assert_printed_calls_decide_their_own_slots(
    review: SemanticReview, must_run=None
):
    """Run every printed call and check the slot and rank it was printed UNDER.

    Not only that it decides something: a call that resolves to a sibling slot
    passes a "one decision was recorded" check and writes the wrong field. The
    text run is the rendered line, so it is what a user pastes.

    The calls of every slot in ``must_run`` (all slots by default) have to run.
    A slot left out of it may refuse as ambiguous, which is what a call does
    when nothing in its arguments can tell its slot apart, but no call may ever
    decide a slot other than the one it was printed under. Mirrors R's
    ``expect_printed_calls_decide_their_own_slots()``.
    """
    rendered = {
        re.sub(r"\s+#.*$", "", line.strip())
        for line in review.render_lines(object_name="review")
    }
    namespace = {
        "accept_suggestion": accept_suggestion,
        "reject_suggestion": reject_suggestion,
        "review": review,
    }
    rows = review.rows
    required = set(rows["slot_id"]) if must_run is None else set(must_run)

    def run(call, slot):
        try:
            return eval(call, namespace).rows  # noqa: S307 - the call is the contract
        except ValueError as refused:
            assert slot not in required, f"{call} -> {refused}"
            assert "more than one review slot" in str(refused), call
            return None

    for _, row in rows.iterrows():
        call = _accept_call(rows, row["slot_id"], row["rank"])
        assert f"review = {call}" in rendered, call
        decided = run(call, row["slot_id"])
        if decided is None:
            continue
        accepted = decided[decided["decision"].notna()]
        assert len(accepted) == 1, call
        assert accepted["slot_id"].iloc[0] == row["slot_id"], call
        assert int(accepted["rank"].iloc[0]) == int(row["rank"]), call
    for slot in dict.fromkeys(rows["slot_id"]):
        call = _reject_call(rows, slot)
        assert f"review = {call}" in rendered, call
        decided = run(call, slot)
        if decided is None:
            continue
        assert set(decided.loc[decided["decision"].notna(), "slot_id"]) == {slot}, call


def test_a_measurement_column_with_a_code_list_prints_a_call_that_reaches_its_own_slot():
    review = _measurement_code_review()
    lines = [line.strip() for line in review.render_lines(object_name="review")]
    assert (
        'review = accept_suggestion(review, "spawner_count", "entity", rank=1, '
        'table="spawners", code_value="")'
    ) in lines
    _assert_printed_calls_decide_their_own_slots(review)


def test_a_blank_code_value_selects_the_columns_own_slot_and_an_omitted_one_still_matches_every_code():
    review = _measurement_code_review()
    for blank in ("", pd.NA, float("nan")):
        decided = accept_suggestion(
            review, "spawner_count", "entity", rank=1, code_value=blank
        ).rows
        assert set(decided.loc[decided["decision"].notna(), "slot_id"]) == {
            MEASUREMENT_COLUMN_SLOT
        }, repr(blank)
    # An omitted code_value still matches every code, as it always has. A call
    # printed for a code slot when that slot was the only one for its column and
    # role carries no code_value, and reading the omission as "no code value"
    # would re-point that pasted call at the column's own slot, or at nothing.
    # So the bare call still refuses rather than guessing, and so does an
    # explicit None, which is the omitted default, as R's NULL is.
    with pytest.raises(ValueError, match="more than one review slot"):
        accept_suggestion(review, "spawner_count", "entity", rank=1)
    with pytest.raises(ValueError, match="more than one review slot"):
        accept_suggestion(review, "spawner_count", "entity", rank=1, code_value=None)


def test_doing_what_the_ambiguity_refusal_says_reaches_every_slot_it_matched():
    # The column's own option used to be a bare table="spawners", which repeated
    # the ambiguity instead of settling it, so a user who followed the message
    # could not reach that slot at all.
    review = _measurement_code_review()
    with pytest.raises(ValueError, match="more than one review slot") as refused:
        accept_suggestion(review, "spawner_count", "entity", rank=1)
    namespace = {"accept_suggestion": accept_suggestion, "review": review}
    reached = set()
    for option in _refusal_options(refused):
        call = f'accept_suggestion(review, "spawner_count", "entity", rank=1, {option})'
        try:
            decided = eval(call, namespace).rows  # noqa: S307
        except ValueError:
            reached.add(None)
            continue
        reached |= set(decided.loc[decided["decision"].notna(), "slot_id"])
    assert reached == set(review.rows.loc[review.rows["role"] == "entity", "slot_id"])


def _vocabulary_code_review() -> SemanticReview:
    """A code slot whose ``codes.csv`` row has no code value.

    The codes schema lets a row leave ``code_value`` empty when it supplies
    ``vocabulary_iri``, and discovery gave it a code-level target, with the
    three roles a measurement parent gives its codes. Its ``code_value`` is as
    empty as the column's own slot's, so only the file tells the two apart.

    Since hub item B-277 such a row gets no target and :func:`review_semantics`
    queues no slot for it, so this is a review an earlier version built, or one
    rebuilt by hand. It is made the way that version made it, by queueing the
    code rows under a placeholder value and emptying ``code_value`` afterwards,
    because the current :func:`review_semantics` drops them. Mirrors R's
    ``vocabulary_code_review()``.
    """
    dictionary = _dictionary_with(
        [
            _suggestion_row(
                dictionary_role="entity",
                target_sdp_field="entity_iri",
                label="Spawner",
                iri="https://w3id.org/smn/Spawner",
            ),
            _suggestion_row(
                dictionary_role="constraint",
                target_sdp_field="constraint_iri",
                label="Wild origin",
                iri="https://example.org/constraint/column",
            ),
            *[
                _suggestion_row(
                    code_value="placeholder",
                    dictionary_role=role,
                    target_scope="code",
                    target_sdp_file="codes.csv",
                    target_sdp_field="term_iri",
                    target_row_key="demo-1/spawners/spawner_count/NA",
                    label=f"{role} term for the vocabulary",
                    iri=f"https://example.org/{role}/vocabulary",
                )
                for role in ("constraint", "entity", "method")
            ],
        ]
    )
    review = review_semantics(dictionary)
    rows = review.rows
    # The review reads every code value as text, so an empty one is "".
    rows.loc[rows["target_file"] == "codes.csv", "code_value"] = ""
    return review._replace(rows)


def test_a_blank_code_value_never_selects_a_code_slot_whose_codes_row_has_no_code_value():
    review = _vocabulary_code_review()
    rows = review.rows
    column_slots = set(rows.loc[rows["target_file"] != "codes.csv", "slot_id"])

    # The column's own slots print calls that run and decide them. The code
    # slot's own calls may still refuse, because no argument tells a code slot
    # with no code value apart from the column's own slot (metasalmon's
    # .hub/workpads/B-151.md). Hub item B-277 closed that by giving such a row
    # no slot at all, so only a review built before it holds one. What no call
    # may do is decide the other slot.
    _assert_printed_calls_decide_their_own_slots(review, must_run=column_slots)
    for blank in ("", pd.NA, float("nan")):
        decided = accept_suggestion(
            review, "spawner_count", "entity", rank=1, code_value=blank
        ).rows
        assert set(decided.loc[decided["decision"].notna(), "slot_id"]) == {
            MEASUREMENT_COLUMN_SLOT
        }, repr(blank)

    # And the refusal offers the column's own slot an option that reaches it,
    # while the code slot keeps the bare table= it always had: no argument
    # settles that slot, and dropping its option would leave the message naming
    # one slot where two matched.
    with pytest.raises(ValueError, match="more than one review slot") as refused:
        accept_suggestion(review, "spawner_count", "entity", rank=1)
    assert _refusal_options(refused) == [
        'table="spawners", code_value=""',
        'table="spawners"',
    ]
    decided = accept_suggestion(
        review, "spawner_count", "entity", rank=1, table="spawners", code_value=""
    ).rows
    assert set(decided.loc[decided["decision"].notna(), "slot_id"]) == {
        MEASUREMENT_COLUMN_SLOT
    }


# ---------------------------------------------------------------------------
# A codes.csv row with no code value gets no semantic target: hub queue B-277,
# the mirror half of metasalmon's B-276, ruled by Brett 2026-09-25. So the
# review queues no slot for it, including from suggestions recorded before the
# ruling, and a coded row of the same column keeps its slot. Each test mirrors
# one in metasalmon's tests/testthat/test-review-console.R.
# ---------------------------------------------------------------------------

#: An empty code value, spelled each way it can arrive: the four the ruling
#: names, and blank text, which R's predicate reads as empty too. A test that
#: builds a frame by hand holds each one as itself, in an object column,
#: because pandas 3 reads a column of text and missing values as strings and
#: turns ``None`` and ``pd.NA`` into NaN.
EMPTY_CODE_VALUES = {
    "NaN": float("nan"),
    "pd.NA": pd.NA,
    "None": None,
    "empty text": "",
    "blank text": "  ",
}

CODED_SLOT = "codes.csv|demo-1/spawners/spawner_count/-9|term_iri"


def _keyed_code_values(keys) -> set:
    """The code value each target row key or slot id is keyed on.

    A code's key is ``dataset/table/column/code``, and a column's or a table's
    has no fourth part. So the empty value's old spellings, ``nan`` where R
    spells ``NA``, or nothing at all, show up here, where a count of
    ``codes.csv`` slots would not tell them from a real code.
    """
    found = set()
    for key in keys:
        text = str(key)
        if "|" in text:
            text = text.split("|")[1]
        parts = text.split("/", 3)
        if len(parts) == 4:
            found.add(parts[3])
    return found


@pytest.mark.parametrize(
    "empty", list(EMPTY_CODE_VALUES.values()), ids=list(EMPTY_CODE_VALUES)
)
def test_review_semantics_queues_no_slot_for_a_codes_row_with_no_code_value_from_suggestions_recorded_before_b277(
    empty,
):
    def code_rows(code, key):
        return [
            _suggestion_row(
                code_value=code,
                dictionary_role=role,
                target_scope="code",
                target_sdp_file="codes.csv",
                target_sdp_field="term_iri",
                target_row_key=f"demo-1/spawners/spawner_count/{key}",
                label=f"{role} term for {key}",
                iri=f"https://example.org/{role}/{key}",
            )
            for role in ("constraint", "entity", "method")
        ]

    # Keyed the way the codes loop keyed each spelling before B-277, by
    # formatting it: ``nan``, ``<NA>``, ``None``, nothing, or the blank itself.
    # The review finds the row by its file and its code value, not its key.
    suggestions = [
        _suggestion_row(
            dictionary_role="entity",
            target_sdp_field="entity_iri",
            label="Spawner",
            iri="https://w3id.org/smn/Spawner",
        ),
        _suggestion_row(
            dictionary_role="constraint",
            target_sdp_field="constraint_iri",
            label="Wild origin",
            iri="https://example.org/constraint/column",
        ),
        *code_rows(empty, f"{empty}"),
        *code_rows("-9", "-9"),
    ]
    dictionary = _dictionary_with(suggestions)
    dictionary.attrs["semantic_suggestions"] = pd.DataFrame(suggestions, dtype=object)
    review = review_semantics(dictionary)
    rows = review.rows

    assert set(rows.loc[rows["target_file"] == "codes.csv", "slot_id"]) == {CODED_SLOT}
    assert _keyed_code_values(rows["slot_id"]) == {"-9"}
    _assert_printed_calls_decide_their_own_slots(review)
    for blank in ("", pd.NA, float("nan")):
        decided = accept_suggestion(
            review, "spawner_count", "entity", rank=1, code_value=blank
        ).rows
        assert set(decided.loc[decided["decision"].notna(), "slot_id"]) == {
            MEASUREMENT_COLUMN_SLOT
        }, repr(blank)
    decided = accept_suggestion(
        review, "spawner_count", "entity", rank=1, code_value="-9"
    ).rows
    assert set(decided.loc[decided["decision"].notna(), "slot_id"]) == {CODED_SLOT}


def _measurement_code_package(tmp_path, monkeypatch, codes, name) -> Path:
    """Build a package through the real pipeline, ``codes`` seeded onto the
    measurement column ``spawner_count``, with retrieval stubbed.

    ``semantic_code_scope="all"`` is the documented option that gives a numeric
    column's codes semantic targets; the default, ``"factor"``, gives them
    none. Mirrors R's ``measurement_code_package()``.
    """
    from metasalmonpy import semantics as sem

    def hits(query, role=None, sources=None):
        return pd.DataFrame(
            {
                "label": [f"Term {i} for {role}" for i in (1, 2)],
                "iri": [f"https://example.org/candidates/{role}Term{i}" for i in (1, 2)],
                "source": ["smn", "smn"],
                "ontology": ["smn", "smn"],
                "role": [role, role],
                "match_type": ["label_exact", "label_exact"],
                "definition": ["A term.", "A term."],
                "score": [4.5, 3.5],
            }
        )

    monkeypatch.setattr(
        sem,
        "suggest_semantics",
        functools.partial(sem.suggest_semantics, search_fn=hits),
    )
    return Path(
        create_sdp(
            {
                "spawners": pd.DataFrame(
                    {
                        "stream_name": ["Bear Creek", "Elk River"] * 6,
                        "spawner_count": [120, 340, -9, 88, 17, -99, 5, 9, 10, 11, 12, 13],
                    }
                )
            },
            path=tmp_path / name,
            dataset_id="demo-1",
            table_id="spawners",
            semantic_max_per_role=2,
            seed_semantics=True,
            seed_codes=codes,
            semantic_code_scope="all",
            seed_verbose=False,
            check_updates=False,
        )
    )


def _assert_column_call_writes_the_dictionary(
    path: Path, review: SemanticReview, blank_code_value: bool = True
):
    """Paste the column's own entity call, apply it, and check it wrote the
    column's ``entity_iri`` and left ``codes.csv`` alone. The call names
    ``code_value=""`` exactly when a code slot shares the column's entity role;
    with none queued, the short call is the one that selects the column's slot.
    Mirrors R's ``expect_column_call_writes_the_dictionary()``."""

    def read(file_name: str) -> pd.DataFrame:
        return pd.read_csv(
            path / "metadata" / file_name, dtype=str, keep_default_na=False
        )

    rows = review.rows
    codes_before = read("codes.csv")
    call = _accept_call(rows, MEASUREMENT_COLUMN_SLOT, 1)
    if blank_code_value:
        assert 'code_value=""' in call
    else:
        assert "code_value" not in call, call
    decided = eval(  # noqa: S307
        call, {"accept_suggestion": accept_suggestion, "review": review}
    )
    apply_sdp_semantics(str(path), decided, quiet=True)

    chosen = rows.loc[
        (rows["slot_id"] == MEASUREMENT_COLUMN_SLOT) & (rows["rank"] == 1), "iri"
    ].iloc[0]
    dictionary = read("column_dictionary.csv")
    written = dictionary.loc[
        dictionary["column_name"] == "spawner_count", "entity_iri"
    ].iloc[0]
    assert written == _strip_review_iri(chosen)
    pd.testing.assert_frame_equal(read("codes.csv"), codes_before)


def test_a_measurement_column_with_a_code_list_round_trips_from_create_sdp_to_disk(
    tmp_path, monkeypatch
):
    # The same collision reached through the real pipeline rather than a
    # hand-built frame.
    codes = pd.DataFrame(
        {
            "dataset_id": ["demo-1", "demo-1"],
            "table_id": ["spawners", "spawners"],
            "column_name": ["spawner_count", "spawner_count"],
            "code_value": ["-9", "-99"],
            "code_label": ["Not surveyed", "Survey abandoned"],
            "code_description": [
                "The reach was not surveyed.",
                "The survey was abandoned.",
            ],
        }
    )
    path = _measurement_code_package(tmp_path, monkeypatch, codes, "measurement-codes")
    review = review_semantics(str(path))
    rows = review.rows

    code_slots = {
        f"codes.csv|demo-1/spawners/spawner_count/{code}|term_iri"
        for code in ("-9", "-99")
    }
    # The collision is real: the column's own slot and both code slots answer
    # to (spawner_count, entity), and the code slots to constraint as well.
    assert {MEASUREMENT_COLUMN_SLOT} | code_slots <= set(
        rows.loc[rows["role"] == "entity", "slot_id"]
    )
    assert code_slots <= set(rows.loc[rows["role"] == "constraint", "slot_id"])

    _assert_printed_calls_decide_their_own_slots(review)
    _assert_column_call_writes_the_dictionary(path, review)


@pytest.mark.parametrize(
    "empty", list(EMPTY_CODE_VALUES.values()), ids=list(EMPTY_CODE_VALUES)
)
def test_a_measurement_column_whose_codes_row_names_a_vocabulary_round_trips_from_create_sdp_to_disk(
    tmp_path, monkeypatch, empty
):
    # The vocabulary-backed row through the real pipeline: the column's only
    # codes.csv row supplies vocabulary_iri and no code value. Such a row gets
    # no semantic target (hub item B-277, ruled 2026-09-25), so create_sdp()
    # writes no suggestion for it and the review queues no slot for it, however
    # its empty code value is spelled.
    codes = pd.DataFrame(
        {
            "dataset_id": ["demo-1"],
            "table_id": ["spawners"],
            "column_name": ["spawner_count"],
            "code_value": [empty],
            "code_label": ["Count categories"],
            "code_description": [
                "Counts are recorded against a published category vocabulary."
            ],
            "vocabulary_iri": ["https://example.org/vocab/count-categories"],
        },
        dtype=object,
    )
    path = _measurement_code_package(tmp_path, monkeypatch, codes, "vocabulary-codes")

    # No key is formed for the row, so none spells its empty value ``nan``
    # where R would spell it ``NA``: the nan slot key, ruled 2026-09-25.
    written = semantic_suggestions(str(path))
    assert not (written["target_sdp_file"] == "codes.csv").any()
    assert _keyed_code_values(written["target_row_key"]) == set()

    review = review_semantics(str(path))
    rows = review.rows
    assert MEASUREMENT_COLUMN_SLOT in set(rows.loc[rows["role"] == "entity", "slot_id"])
    assert not (rows["target_file"] == "codes.csv").any()
    assert _keyed_code_values(rows["slot_id"]) == set()

    # Every call printed for the column runs and decides its own slot.
    _assert_printed_calls_decide_their_own_slots(review)
    _assert_column_call_writes_the_dictionary(path, review, blank_code_value=False)


@pytest.mark.parametrize(
    "empty", list(EMPTY_CODE_VALUES.values()), ids=list(EMPTY_CODE_VALUES)
)
def test_a_coded_row_beside_a_vocabulary_row_keeps_its_slot_through_create_sdp_and_the_review(
    tmp_path, monkeypatch, empty
):
    codes = pd.DataFrame(
        {
            "dataset_id": ["demo-1", "demo-1"],
            "table_id": ["spawners", "spawners"],
            "column_name": ["spawner_count", "spawner_count"],
            "code_value": [empty, "-9"],
            "code_label": ["Count categories", "Not surveyed"],
            "code_description": [
                "Counts are recorded against a published category vocabulary.",
                "The reach was not surveyed.",
            ],
            "vocabulary_iri": ["https://example.org/vocab/count-categories", pd.NA],
        },
        dtype=object,
    )
    path = _measurement_code_package(tmp_path, monkeypatch, codes, "vocabulary-and-code")

    written = semantic_suggestions(str(path))
    written_codes = written[written["target_sdp_file"] == "codes.csv"]
    assert set(written_codes["target_row_key"]) == {"demo-1/spawners/spawner_count/-9"}
    assert _keyed_code_values(written["target_row_key"]) == {"-9"}

    review = review_semantics(str(path))
    rows = review.rows
    assert set(rows.loc[rows["target_file"] == "codes.csv", "slot_id"]) == {CODED_SLOT}
    assert {MEASUREMENT_COLUMN_SLOT, CODED_SLOT} <= set(
        rows.loc[rows["role"] == "entity", "slot_id"]
    )

    _assert_printed_calls_decide_their_own_slots(review)
    _assert_column_call_writes_the_dictionary(path, review)


def test_a_semantic_suggestions_csv_written_before_b277_queues_no_slot_for_a_codes_row_with_no_code_value(
    tmp_path, monkeypatch
):
    codes = pd.DataFrame(
        {
            "dataset_id": ["demo-1", "demo-1"],
            "table_id": ["spawners", "spawners"],
            "column_name": ["spawner_count", "spawner_count"],
            "code_value": [pd.NA, "-9"],
            "code_label": ["Count categories", "Not surveyed"],
            "code_description": [
                "Counts are recorded against a published category vocabulary.",
                "The reach was not surveyed.",
            ],
            "vocabulary_iri": ["https://example.org/vocab/count-categories", pd.NA],
        },
        dtype=object,
    )
    path = _measurement_code_package(
        tmp_path, monkeypatch, codes, "vocabulary-codes-old-csv"
    )

    # Write back the rows an earlier version wrote for the vocabulary row: the
    # coded row's three roles, with the code value empty. This package keyed
    # them ``.../nan``, or ``.../`` for empty text; metasalmon keyed the same
    # row ``.../NA``. One is recorded as rejected, which a later review replays.
    suggestions_path = path / "semantic_suggestions.csv"
    written = read_sdp_csv(suggestions_path)
    template = written[
        (written["target_sdp_file"] == "codes.csv") & (written["code_value"] == "-9")
    ]
    assert len(template) > 0
    earlier = []
    for key in ("nan", "", "NA"):
        rows = template.copy()
        rows["code_value"] = pd.NA
        rows["target_row_key"] = f"demo-1/spawners/spawner_count/{key}"
        rows["code_label"] = "Count categories"
        rows["code_description"] = (
            "Counts are recorded against a published category vocabulary."
        )
        rows["decision"] = "rejected" if key == "nan" else pd.NA
        earlier.append(rows)
    suggestions_path.write_bytes(
        _suggestions_csv_bytes(pd.concat([written, *earlier], ignore_index=True))
    )
    on_disk = semantic_suggestions(str(path))
    old_rows = on_disk[
        (on_disk["target_sdp_file"] == "codes.csv") & (on_disk["code_value"] == "")
    ]
    assert set(old_rows["target_row_key"]) == {
        f"demo-1/spawners/spawner_count/{key}" for key in ("nan", "", "NA")
    }
    assert (old_rows["decision"] == "rejected").any()

    review = review_semantics(str(path))
    queued = review.rows
    assert set(queued.loc[queued["target_file"] == "codes.csv", "slot_id"]) == {CODED_SLOT}
    _assert_printed_calls_decide_their_own_slots(review)

    # Nor as a decided slot: the recorded rejection is not replayed for it.
    everything = review_semantics(str(path), include_filled=True).rows
    assert set(
        everything.loc[everything["target_file"] == "codes.csv", "slot_id"]
    ) == {CODED_SLOT}
    assert _keyed_code_values(everything["slot_id"]) == {"-9"}


def test_a_table_level_slot_never_names_a_column_that_does_not_exist():
    dictionary = _dictionary_with(
        [
            _suggestion_row(
                column_name=pd.NA,
                dictionary_role="entity",
                target_sdp_file="tables.csv",
                target_sdp_field="observation_unit_iri",
                target_row_key="demo-1/spawners",
            )
        ]
    )
    text = str(review_semantics(dictionary))
    assert '"NA"' not in text
    assert 'role="entity"' in text
    assert 'table="spawners"' in text


def test_a_column_in_one_table_only_prints_the_short_call():
    text = str(review_semantics(_dictionary_with([_suggestion_row()])))
    assert 'accept_suggestion(review, "spawner_count", "variable", rank=1)' in text
    assert "table=" not in text


def test_an_ambiguous_column_gains_the_table_argument():
    dictionary = _dictionary_with(
        [
            _suggestion_row(table_id="a", target_row_key="demo-1/a/spawner_count"),
            _suggestion_row(table_id="b", target_row_key="demo-1/b/spawner_count"),
        ]
    )
    text = str(review_semantics(dictionary))
    assert 'table="a"' in text
    assert 'table="b"' in text


def test_the_printed_call_names_the_variable_the_user_bound(monkeypatch):
    import sys

    dictionary = _dictionary_with([_suggestion_row()])
    review = review_semantics(dictionary)
    main = sys.modules["__main__"]
    monkeypatch.setattr(main, "my_review", review, raising=False)
    # ``accept_suggestion(review, ...)`` against a review bound to
    # ``my_review`` would fail with a NameError, so this is not cosmetic.
    assert "my_review = accept_suggestion(my_review," in str(review)


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


def test_accept_strips_the_review_marker():
    dictionary = _dictionary_with([_suggestion_row(iri="REVIEW:" + SPAWNER_IRI)])
    review = accept_suggestion(
        review_semantics(dictionary), "spawner_count", "variable", rank=1
    )
    assert review["decision_iri"].dropna().iloc[0] == SPAWNER_IRI


def test_accept_takes_an_iri_that_was_never_shortlisted():
    review = accept_suggestion(
        review_semantics(_dictionary_with([_suggestion_row()])),
        "spawner_count",
        "variable",
        iri="https://example.org/Handpicked",
    )
    assert review["decision_iri"].dropna().iloc[0] == "https://example.org/Handpicked"


# An ``iri=`` that is empty once its ``REVIEW:`` marker is stripped (hub item
# B-220, the mirror of metasalmon's B-219). The non-empty check read ``iri``
# before the strip, so the bare marker passed it and the accept recorded an IRI
# that named no term. ``apply_sdp_semantics()`` then cleared the field and
# wrote an ``accepted`` row with an empty ``iri`` into
# ``semantic_suggestions.csv``.
#
# One case per spelling ``_strip_review_iri()`` removes, because the check has
# to agree with the strip. Which spellings count as the marker is hub question
# Q-63, so this list is what the strip removes today, not a ruling, and it
# follows the strip: when Q-63 is ruled, a spelling the ruling drops leaves the
# list and one it adds joins it. Each case asserts that premise first, so a
# change to the strip fails here and names the spelling rather than leaving a
# test that checks nothing.
MARKER_ONLY_IRIS = {
    "the bare marker": "REVIEW:",
    "the marker as metasalmon writes it": "REVIEW: ",
    "lower case": "review:",
    "mixed case": "Review:",
    "leading spaces": "  REVIEW:",
    # ``scalar_text()`` trims spaces, tabs and newlines. A form feed survives
    # the trim, and only the strip's ``str.strip()`` removes it.
    "a form feed after the colon": "REVIEW:\f",
    # The strip compares ``str.upper()``, which folds a dotless i onto I.
    "a dotless i": "REV\u0131EW:",
}


@pytest.mark.parametrize(
    "marker", list(MARKER_ONLY_IRIS.values()), ids=list(MARKER_ONLY_IRIS)
)
def test_accept_refuses_an_iri_that_is_only_the_review_marker(marker):
    assert _strip_review_iri(marker) == ""

    review = review_semantics(_dictionary_with([_suggestion_row()]))
    with pytest.raises(ValueError, match="non-empty IRI") as refused:
        accept_suggestion(review, "spawner_count", "variable", iri=marker)
    assert "nothing follows the marker" in str(refused.value)


def test_accept_takes_a_marked_iri_and_records_it_without_the_marker():
    review = accept_suggestion(
        review_semantics(_dictionary_with([_suggestion_row()])),
        "spawner_count",
        "variable",
        iri="review: https://w3id.org/smn/WaterTemperature",
    )
    assert review["decision_iri"].dropna().tolist() == [
        "https://w3id.org/smn/WaterTemperature"
    ]


# The routes B-220 left open to a decision that names no term (hub item B-247,
# the mirror of metasalmon's B-246). A shortlisted candidate whose ``iri`` is
# only the marker was queued, because the queue tested only that the text of
# ``iri`` was not empty, so ``rank=`` accepted it with an empty
# ``decision_iri``, and a recorded accept of one replayed the same way. The
# strip removes one marker, so a doubled one left a marker in the decision on
# either route. No producer writes such a candidate; a hand-edited or external
# suggestions table does. The spellings are ``MARKER_ONLY_IRIS`` above, which
# follows the strip, and each test asserts its premise first for the same
# reason those tests do.


def _decided_iris(review: SemanticReview) -> list:
    rows = review.rows
    return rows.loc[rows["decision"].notna(), "decision_iri"].tolist()


@pytest.mark.parametrize(
    "marker", list(MARKER_ONLY_IRIS.values()), ids=list(MARKER_ONLY_IRIS)
)
def test_a_candidate_that_is_only_the_marker_is_not_queued_so_rank_cannot_accept_it(
    marker, capsys
):
    assert _strip_review_iri(marker) == ""

    alone = review_semantics(_dictionary_with([_suggestion_row(iri=marker)]))
    assert len(alone) == 0
    with pytest.raises(ValueError, match="No review slot matches"):
        accept_suggestion(alone, "spawner_count", "variable", rank=1)
    capsys.readouterr()

    # Ahead of a real candidate it does not take rank 1 from it, and dropping it
    # is not reported as a field the review cannot decide.
    review = review_semantics(
        _dictionary_with(
            [_suggestion_row(label="Marker only", iri=marker), _suggestion_row()]
        )
    )
    assert "cannot decide" not in capsys.readouterr().out
    review = accept_suggestion(review, "spawner_count", "variable", rank=1)
    assert _decided_iris(review) == [SPAWNER_IRI]


@pytest.mark.parametrize(
    "marker", list(MARKER_ONLY_IRIS.values()), ids=list(MARKER_ONLY_IRIS)
)
def test_a_recorded_accept_of_a_candidate_that_is_only_the_marker_replays_no_empty_iri(
    marker,
):
    assert _strip_review_iri(marker) == ""

    dictionary = _dictionary_with(
        [
            _suggestion_row(label="Marker only", iri=marker, decision="accepted"),
            _suggestion_row(decision="not_selected"),
        ]
    )
    rebuilt = review_semantics(dictionary, include_filled=True)
    assert not (
        (rebuilt.rows["decision"] == "accept") & (rebuilt.rows["decision_iri"] == "")
    ).any()
    # That accept named no term, so it decided nothing: the slot is asked again.
    queued = review_semantics(dictionary)
    assert queued["iri"].tolist() == [SPAWNER_IRI]
    assert queued["decision"].isna().all()


# What each of these leaves once ``_strip_review_iri()`` has run is still read
# as a marker by ``_is_review_iri()``. The spellings are this package's own:
# metasalmon's twin of "in two cases" puts a space before each colon, which
# ``_strip_review_iri()`` does not read as the marker today. Which spellings
# count is Q-63's.
DOUBLED_MARKER_IRIS = {
    "twice, with nothing after": "REVIEW: REVIEW:",
    "twice, with no space between": "REVIEW:REVIEW:",
    "twice, in two cases": "review: Review:",
    "twice, before a term": "REVIEW: REVIEW: https://w3id.org/smn/WaterTemperature",
}


@pytest.mark.parametrize(
    "doubled", list(DOUBLED_MARKER_IRIS.values()), ids=list(DOUBLED_MARKER_IRIS)
)
def test_no_accept_records_an_iri_that_is_still_a_marker_once_one_is_stripped(
    doubled,
):
    assert _is_review_iri(_strip_review_iri(doubled))

    review = review_semantics(_dictionary_with([_suggestion_row()]))
    with pytest.raises(ValueError, match="not a REVIEW: marker"):
        accept_suggestion(review, "spawner_count", "variable", iri=doubled)

    # On a shortlisted candidate it is not queued, so neither ``rank=`` nor a
    # recorded accept of it can put it in a decision.
    suggestions = [
        _suggestion_row(label="Doubled marker", iri=doubled),
        _suggestion_row(),
    ]
    review = accept_suggestion(
        review_semantics(_dictionary_with(suggestions)),
        "spawner_count",
        "variable",
        rank=1,
    )
    decided = _decided_iris(review)
    assert not any(_is_review_iri(value) for value in decided)
    assert decided == [SPAWNER_IRI]

    suggestions[0]["decision"] = "accepted"
    suggestions[1]["decision"] = "not_selected"
    rebuilt = review_semantics(_dictionary_with(suggestions), include_filled=True)
    replayed = rebuilt.rows.loc[rebuilt.rows["decision"] == "accept", "decision_iri"]
    assert not any(_is_review_iri(value) for value in replayed)


# A review the current ``review_semantics()`` did not build can still hold such
# a candidate: one saved by an earlier version, or edited by hand. ``rank=``
# refuses it there too, rather than trusting the queue to have left it out.
NAMES_NO_TERM_IRIS = {**MARKER_ONLY_IRIS, **DOUBLED_MARKER_IRIS}


@pytest.mark.parametrize(
    "value", list(NAMES_NO_TERM_IRIS.values()), ids=list(NAMES_NO_TERM_IRIS)
)
def test_rank_refuses_a_candidate_in_the_review_whose_iri_names_no_term(value):
    stripped = _strip_review_iri(value)
    assert not stripped or _is_review_iri(stripped)

    review = review_semantics(_dictionary_with([_suggestion_row()]))
    rows = review.rows
    rows.at[rows.index[0], "iri"] = value
    edited = SemanticReview(rows, review.path)
    with pytest.raises(ValueError, match="names no term"):
        accept_suggestion(edited, "spawner_count", "variable", rank=1)


# Rejecting a slot does not depend on any candidate's IRI, so a recorded reject
# is replayed from a row whose IRI names no term, which the queue otherwise
# leaves out. Without that, a slot whose only candidate is such a row lost its
# rejection, and the reason, from ``include_filled=True``.
EMPTY_IRIS = {"an empty string": "", "a missing value": pd.NA}
NO_TERM_OR_EMPTY_IRIS = {**NAMES_NO_TERM_IRIS, **EMPTY_IRIS}


@pytest.mark.parametrize(
    "value", list(NO_TERM_OR_EMPTY_IRIS.values()), ids=list(NO_TERM_OR_EMPTY_IRIS)
)
def test_a_recorded_reject_is_replayed_from_a_candidate_whose_iri_names_no_term(
    value,
):
    dictionary = _dictionary_with(
        [
            _suggestion_row(
                iri=value,
                decision="rejected",
                decision_reason="no candidate describes a wild-origin count",
            )
        ]
    )
    revisited = review_semantics(dictionary, include_filled=True)
    assert revisited["decision"].tolist() == ["reject"]
    assert revisited["decision_reason"].tolist() == [
        "no candidate describes a wild-origin count"
    ]
    assert len(review_semantics(dictionary)) == 0
    with pytest.raises(ValueError, match="names no term"):
        accept_suggestion(revisited, "spawner_count", "variable", rank=1)


# The console prints a call only where the call runs. A candidate whose IRI
# names no term is refused by ``accept_suggestion()``, so it gets no accept
# call, while its slot keeps its reject call and every other candidate keeps
# its own.
CONSOLE_NO_TERM_IRIS = {
    "the bare marker": "REVIEW:",
    "a doubled marker": "REVIEW: REVIEW:",
    **EMPTY_IRIS,
}


@pytest.mark.parametrize(
    "value", list(CONSOLE_NO_TERM_IRIS.values()), ids=list(CONSOLE_NO_TERM_IRIS)
)
def test_the_console_prints_no_accept_call_for_a_candidate_whose_iri_names_no_term(
    value,
):
    abundance = "https://w3id.org/smn/Abundance"
    dictionary = _dictionary_with(
        [
            _suggestion_row(
                iri=value,
                decision="rejected",
                decision_reason="no candidate describes a wild-origin count",
            ),
            _suggestion_row(
                dictionary_role="property",
                target_sdp_field="property_iri",
                label="Abundance",
                iri=abundance,
            ),
        ]
    )
    review = review_semantics(dictionary, include_filled=True)
    lines = review.render_lines(object_name="review")
    assert any("DECIDED: reject" in line for line in lines)
    assert any("names no term" in line for line in lines)

    namespace = {
        "accept_suggestion": accept_suggestion,
        "reject_suggestion": reject_suggestion,
        "review": review,
    }
    accepts = [
        line.strip()[len("review = "):]
        for line in lines
        if line.strip().startswith("review = accept_suggestion(")
    ]
    assert len(accepts) == 1
    for call in accepts:
        decided = eval(call, namespace).rows  # noqa: S307 - the call is the contract
        accepted = decided.loc[decided["decision"] == "accept", "decision_iri"]
        assert accepted.tolist() == [abundance]
    rejects = [
        line.strip().split("# ")[0].strip()[len("review = "):]
        for line in lines
        if line.strip().startswith("review = reject_suggestion(")
    ]
    assert len(rejects) == 2
    for call in rejects:
        assert isinstance(eval(call, namespace), SemanticReview)  # noqa: S307


# A row with no IRI targets a field the review does decide, so it is not one of
# the fields "this review cannot decide", and editing the metadata CSV by hand
# is not what it needs. The shape B-220 left in a package: a hand-picked
# ``accepted`` row with an empty ``iri`` at the head of its slot.
@pytest.mark.parametrize("empty", list(EMPTY_IRIS.values()), ids=list(EMPTY_IRIS))
def test_review_semantics_does_not_report_a_row_with_no_iri_as_a_field_it_cannot_decide(
    empty, capsys
):
    suggestions = [
        _suggestion_row(iri=empty, source="user", decision="accepted"),
        _suggestion_row(decision="not_selected"),
    ]
    review = review_semantics(_dictionary_with(suggestions))
    assert "cannot decide" not in capsys.readouterr().out
    assert review["iri"].tolist() == [SPAWNER_IRI]
    assert review["decision"].isna().all()

    # A field the review cannot decide is still reported, and alone.
    suggestions.append(
        _suggestion_row(
            target_scope="dataset",
            target_sdp_file="dataset.csv",
            target_sdp_field="keywords",
            target_row_key="demo-1",
        )
    )
    review_semantics(_dictionary_with(suggestions))
    reported = capsys.readouterr().out
    assert "cannot decide" in reported
    assert "dataset.csv" in reported
    assert "column_dictionary.csv" not in reported


def test_accept_refuses_a_rank_that_is_not_there():
    review = review_semantics(_dictionary_with([_suggestion_row()]))
    with pytest.raises(ValueError, match="No candidate with that rank"):
        accept_suggestion(review, "spawner_count", "variable", rank=9)


def test_accept_replaces_an_earlier_accept_in_the_same_slot():
    dictionary = _dictionary_with(
        [_suggestion_row(), _suggestion_row(iri="https://example.org/Other")]
    )
    review = review_semantics(dictionary)
    review = accept_suggestion(review, "spawner_count", "variable", rank=1)
    review = accept_suggestion(review, "spawner_count", "variable", rank=2)
    decided = review.rows[review.rows["decision"].notna()]
    assert len(decided) == 1
    assert decided["decision_iri"].iloc[0] == "https://example.org/Other"


def test_reject_records_the_reason_on_every_row_of_the_slot():
    review = reject_suggestion(
        review_semantics(_dictionary_with([_suggestion_row(), _suggestion_row(iri="x")])),
        "spawner_count",
        "variable",
        reason="none describe a spawner count",
    )
    assert set(review["decision"]) == {"reject"}
    assert set(review["decision_reason"]) == {"none describe a spawner count"}


def test_a_decision_leaves_the_original_review_untouched():
    # Pipe-friendly means immutable: re-running a review script must not
    # depend on how many times it ran before.
    review = review_semantics(_dictionary_with([_suggestion_row()]))
    accept_suggestion(review, "spawner_count", "variable", rank=1)
    assert review["decision"].isna().all()


def test_an_unknown_slot_lists_the_ones_that_exist():
    review = review_semantics(_dictionary_with([_suggestion_row()]))
    with pytest.raises(ValueError) as caught:
        accept_suggestion(review, "nope", "variable")
    assert "spawner_count" in str(caught.value)


def test_a_column_named_with_a_brace_survives_the_error_message():
    dictionary = _dictionary_with(
        [
            _suggestion_row(
                column_name="rate{pct",
                target_row_key="demo-1/spawners/rate{pct",
            )
        ]
    )
    review = review_semantics(dictionary)
    with pytest.raises(ValueError) as caught:
        accept_suggestion(review, "missing", "variable")
    assert "rate{pct" in str(caught.value)


def test_a_non_review_object_is_refused():
    with pytest.raises(TypeError, match="SemanticReview"):
        accept_suggestion(pd.DataFrame(), "x", "variable")


# ---------------------------------------------------------------------------
# apply_sdp_semantics
# ---------------------------------------------------------------------------


def _stub_search(query, role=None, sources=None):
    return pd.DataFrame(
        {
            "label": ["Spawner Abundance"],
            "iri": [SPAWNER_IRI],
            "source": ["smn"],
            "ontology": ["smn"],
            "role": [role],
            "match_type": ["label"],
            "definition": ["Mature salmon returning to spawn."],
            "score": [4.9],
        }
    )


@pytest.fixture
def seeded_package(tmp_path, monkeypatch):
    """A written package with ``semantic_suggestions.csv``, retrieval stubbed."""
    from metasalmonpy import semantics as sem

    monkeypatch.setattr(
        sem,
        "suggest_semantics",
        functools.partial(sem.suggest_semantics, search_fn=_stub_search),
    )
    return create_sdp(
        {
            "spawners": pd.DataFrame(
                {
                    "stream_name": ["Goldstream", "Craigflower"],
                    "spawner_count": [120, 44],
                }
            )
        },
        path=tmp_path / "pkg",
        dataset_id="demo-1",
        seed_semantics=True,
        seed_verbose=False,
        check_updates=False,
    )


def _dictionary_csv(package: Path) -> pd.DataFrame:
    return pd.read_csv(package / "metadata" / "column_dictionary.csv")


def test_apply_writes_an_accepted_iri_without_the_marker(seeded_package):
    review = accept_suggestion(
        review_semantics(str(seeded_package)),
        "spawner_count",
        "variable",
        rank=1,
    )
    apply_sdp_semantics(str(seeded_package), review, quiet=True)
    row = _dictionary_csv(seeded_package)
    written = row.loc[row["column_name"] == "spawner_count", "term_iri"].iloc[0]
    assert written == SPAWNER_IRI
    assert not written.startswith("REVIEW:")


def test_apply_sets_term_type_alongside_term_iri(seeded_package):
    review = accept_suggestion(
        review_semantics(str(seeded_package)),
        "spawner_count",
        "variable",
        rank=1,
    )
    apply_sdp_semantics(str(seeded_package), review, quiet=True)
    row = _dictionary_csv(seeded_package)
    assert (
        row.loc[row["column_name"] == "spawner_count", "term_type"].iloc[0]
        == "skos_concept"
    )


def test_a_hand_supplied_iri_does_not_inherit_the_candidate_term_type(
    seeded_package,
):
    # ``term_type`` describes the candidate; a hand-supplied ``iri=`` names a
    # different term, so the candidate's type is not evidence about it.
    review = accept_suggestion(
        review_semantics(str(seeded_package)),
        "spawner_count",
        "variable",
        iri="https://example.org/Handpicked",
    )
    apply_sdp_semantics(str(seeded_package), review, quiet=True)
    row = _dictionary_csv(seeded_package)
    assert (
        row.loc[row["column_name"] == "spawner_count", "term_type"].iloc[0]
        == "skos_concept"
    )


def test_apply_clears_a_rejected_field(seeded_package):
    review = reject_suggestion(
        review_semantics(str(seeded_package)),
        "spawner_count",
        "variable",
        reason="none fit",
    )
    apply_sdp_semantics(str(seeded_package), review, quiet=True)
    row = _dictionary_csv(seeded_package)
    assert pd.isna(row.loc[row["column_name"] == "spawner_count", "term_iri"].iloc[0])
    assert pd.isna(row.loc[row["column_name"] == "spawner_count", "term_type"].iloc[0])


def test_apply_leaves_undecided_slots_marked(seeded_package):
    review = accept_suggestion(
        review_semantics(str(seeded_package)),
        "spawner_count",
        "variable",
        rank=1,
    )
    apply_sdp_semantics(str(seeded_package), review, quiet=True)
    row = _dictionary_csv(seeded_package)
    still = row.loc[row["column_name"] == "spawner_count", "property_iri"].iloc[0]
    assert str(still).startswith("REVIEW:")


def test_apply_does_not_touch_the_data_csv_bytes(seeded_package):
    data = seeded_package / "data" / "spawners.csv"
    before = data.read_bytes()
    review = accept_suggestion(
        review_semantics(str(seeded_package)),
        "spawner_count",
        "variable",
        rank=1,
    )
    apply_sdp_semantics(str(seeded_package), review, quiet=True)
    assert data.read_bytes() == before


def test_applying_the_same_review_twice_produces_identical_bytes(seeded_package):
    review = accept_suggestion(
        review_semantics(str(seeded_package)),
        "spawner_count",
        "variable",
        rank=1,
    )
    apply_sdp_semantics(str(seeded_package), review, quiet=True)
    snapshot = {
        path: path.read_bytes()
        for path in [
            seeded_package / "metadata" / "column_dictionary.csv",
            seeded_package / "semantic_suggestions.csv",
            seeded_package / "datapackage.json",
        ]
    }
    apply_sdp_semantics(str(seeded_package), review, quiet=True)
    for path, payload in snapshot.items():
        assert path.read_bytes() == payload, path.name


def test_apply_syncs_the_descriptor_field(seeded_package):
    import json

    review = accept_suggestion(
        review_semantics(str(seeded_package)),
        "spawner_count",
        "variable",
        rank=1,
    )
    apply_sdp_semantics(str(seeded_package), review, quiet=True)
    descriptor = json.loads((seeded_package / "datapackage.json").read_text())
    resource = next(
        entry
        for entry in descriptor["resources"]
        if entry.get("name") == "spawners"
    )
    field = next(
        entry
        for entry in resource["schema"]["fields"]
        if entry["name"] == "spawner_count"
    )
    assert field["term_iri"] == SPAWNER_IRI


def test_apply_with_no_decisions_says_so(seeded_package, capsys):
    apply_sdp_semantics(str(seeded_package), review_semantics(str(seeded_package)))
    assert "No decisions to apply." in capsys.readouterr().out


# ---------------------------------------------------------------------------
# The decision round trip -- decision_reason, and replay on queue rebuild
# ---------------------------------------------------------------------------


def _suggestions_csv(package: Path) -> pd.DataFrame:
    return pd.read_csv(package / "semantic_suggestions.csv")


def test_a_rejection_reason_reaches_disk(seeded_package):
    review = reject_suggestion(
        review_semantics(str(seeded_package)),
        "spawner_count",
        "variable",
        reason="no term describes a spawner count",
    )
    apply_sdp_semantics(str(seeded_package), review, quiet=True)
    written = _suggestions_csv(seeded_package)
    assert "decision_reason" in written.columns
    row = written[
        (written["column_name"] == "spawner_count")
        & (written["dictionary_role"] == "variable")
    ]
    assert row["decision"].iloc[0] == "rejected"
    assert row["decision_reason"].iloc[0] == "no term describes a spawner count"


def test_a_reason_that_was_never_given_round_trips_as_absent(seeded_package):
    # ``""`` and a missing value share the empty CSV field, so an ungiven
    # reason must read back as absent rather than as an empty string.
    review = reject_suggestion(
        review_semantics(str(seeded_package)), "spawner_count", "variable"
    )
    apply_sdp_semantics(str(seeded_package), review, quiet=True)
    written = _suggestions_csv(seeded_package)
    row = written[
        (written["column_name"] == "spawner_count")
        & (written["dictionary_role"] == "variable")
    ]
    assert pd.isna(row["decision_reason"].iloc[0])


def test_an_accept_records_the_chosen_row_and_its_siblings(seeded_package):
    review = accept_suggestion(
        review_semantics(str(seeded_package)),
        "spawner_count",
        "variable",
        rank=1,
    )
    apply_sdp_semantics(str(seeded_package), review, quiet=True)
    written = _suggestions_csv(seeded_package)
    slot = written[
        (written["column_name"] == "spawner_count")
        & (written["dictionary_role"] == "variable")
    ]
    assert set(slot["decision"]) <= {"accepted", "not_selected"}
    assert "accepted" in set(slot["decision"])


HANDPICKED_IRI = "https://example.org/Handpicked"


def _handpicked_slot(package: Path) -> pd.DataFrame:
    written = _suggestions_csv(package)
    return written[
        (written["column_name"] == "spawner_count")
        & (written["dictionary_role"] == "variable")
    ]


def test_an_accept_outside_the_shortlist_reaches_the_decision_record(
    seeded_package,
):
    """``accept_suggestion(iri=...)`` is a supported escape hatch, so its
    decision has to survive in the package like any other.

    The shortlist match was the only way an ``accepted`` row was ever written, so
    a hand-picked IRI produced an empty mask: every candidate was marked
    ``not_selected``, nothing was marked ``accepted``, and the acceptance existed
    only in the user's script. ``semantic_suggestions.csv`` is documented as the
    evidence trail, and this is the one decision it lost.
    """
    review = accept_suggestion(
        review_semantics(str(seeded_package)),
        "spawner_count",
        "variable",
        iri=HANDPICKED_IRI,
    )
    apply_sdp_semantics(str(seeded_package), review, quiet=True)

    # The metadata CSV always received it; that half was never broken.
    dictionary = _dictionary_csv(seeded_package)
    assert (
        dictionary.loc[
            dictionary["column_name"] == "spawner_count", "term_iri"
        ].iloc[0]
        == HANDPICKED_IRI
    )

    slot = _handpicked_slot(seeded_package)
    accepted = slot[slot["decision"] == "accepted"]
    assert len(accepted) == 1
    assert accepted["iri"].iloc[0] == HANDPICKED_IRI
    # The retrieved candidates are still recorded, and still say they were not
    # the one chosen.
    retrieved = slot[slot["iri"] == SPAWNER_IRI]
    assert set(retrieved["decision"]) == {"not_selected"}
    # And the row says the term came from the user rather than from retrieval,
    # so nobody reads it as a candidate some source returned.
    assert accepted["source"].iloc[0] == "user"


def test_a_hand_picked_accept_replays_on_rebuild(seeded_package):
    """The replay is what ``include_filled=True`` is for, and it is the half a
    lost ``accepted`` row takes with it."""
    review = accept_suggestion(
        review_semantics(str(seeded_package)),
        "spawner_count",
        "variable",
        iri=HANDPICKED_IRI,
    )
    apply_sdp_semantics(str(seeded_package), review, quiet=True)

    decided_slot = "column_dictionary.csv|demo-1/spawners/spawner_count|term_iri"
    rebuilt = review_semantics(str(seeded_package), include_filled=True)
    replayed = rebuilt.rows[
        (rebuilt.rows["slot_id"] == decided_slot)
        & (rebuilt.rows["decision"].notna())
    ]
    assert len(replayed) == 1
    assert replayed["decision"].iloc[0] == "accept"
    assert replayed["decision_iri"].iloc[0] == HANDPICKED_IRI
    assert f"DECIDED: accept → {HANDPICKED_IRI}" in str(rebuilt)

    # A decided slot stays out of the default queue, hand-picked or not.
    assert decided_slot not in set(review_semantics(str(seeded_package))["slot_id"])


def test_a_hand_picked_accept_is_visible_inside_max_candidates(seeded_package):
    """Position is the whole reason the recorded row is not simply appended.

    ``review_semantics()`` derives ``rank`` from file position and then drops
    everything past ``max_candidates``, so a recorded accept written after a full
    shortlist would be filtered straight back out -- the same decision lost
    again, one layer further on.
    """
    review = accept_suggestion(
        review_semantics(str(seeded_package)),
        "spawner_count",
        "variable",
        iri=HANDPICKED_IRI,
    )
    apply_sdp_semantics(str(seeded_package), review, quiet=True)

    rebuilt = review_semantics(
        str(seeded_package), include_filled=True, max_candidates=1
    )
    slot = rebuilt.rows[
        rebuilt.rows["slot_id"]
        == "column_dictionary.csv|demo-1/spawners/spawner_count|term_iri"
    ]
    assert len(slot) == 1
    assert slot["iri"].iloc[0] == HANDPICKED_IRI
    assert slot["rank"].iloc[0] == 1


def test_applying_a_hand_picked_accept_twice_produces_identical_bytes(
    seeded_package,
):
    """Re-runnable, like every other decision: the second pass finds the row the
    first pass recorded and matches it rather than recording a second one."""
    review = accept_suggestion(
        review_semantics(str(seeded_package)),
        "spawner_count",
        "variable",
        iri=HANDPICKED_IRI,
    )
    apply_sdp_semantics(str(seeded_package), review, quiet=True)
    once = (seeded_package / "semantic_suggestions.csv").read_bytes()
    apply_sdp_semantics(str(seeded_package), review, quiet=True)
    assert (seeded_package / "semantic_suggestions.csv").read_bytes() == once
    assert len(_handpicked_slot(seeded_package)) == 2


# ---------------------------------------------------------------------------
# One decision, one ``term_type`` (hub queue B-222, the mirror of metasalmon's
# B-221)
#
# ``term_type`` says what kind of thing ``term_iri`` names.
# ``apply_sdp_semantics()`` takes it from the review row a decision sits on when
# that row carries the accepted IRI, and writes ``skos_concept`` when it does
# not. ``accept_suggestion(iri=...)`` put every decision on the slot's first
# row, so naming the IRI of a lower-ranked candidate wrote ``skos_concept``
# whatever that candidate was. The review rebuilt from the package replays the
# same decision on the candidate's own row, and re-applying it wrote the
# candidate's type: one decision, two ``term_type``s, and
# ``column_dictionary.csv`` changed between two applies of it.
# ---------------------------------------------------------------------------

OWL_CLASS_IRI = "https://example.org/ols/SpawnerCount"
OWL_CLASS_TYPE_IRI = "http://www.w3.org/2002/07/owl#Class"


@pytest.fixture
def typed_package(tmp_path, monkeypatch):
    """Build packages whose ``spawner_count`` variable slot holds two candidates.

    An ``smn`` term and then ``OWL_CLASS_IRI``. ``type_iris`` is each one's type
    evidence, and a candidate with none reads as ``skos_concept``. Each call
    writes a fresh package.
    """
    from metasalmonpy import semantics as sem

    original = sem.suggest_semantics
    built = []

    def build(type_iris=(None, OWL_CLASS_TYPE_IRI)) -> Path:
        hits = pd.DataFrame(
            {
                "label": ["Spawner Abundance", "Spawner count"],
                "iri": [SPAWNER_IRI, OWL_CLASS_IRI],
                "source": ["smn", "ols"],
                "ontology": ["smn", "ols"],
                "match_type": ["label_exact", "label_exact"],
                "definition": [
                    "Mature salmon returning to spawn.",
                    "A count of spawners.",
                ],
                "score": [4.9, 3.2],
                "type_iris": list(type_iris),
            }
        )

        def search(query, role=None, sources=None):
            if role != "variable":
                return pd.DataFrame()
            return hits.assign(role=role)

        monkeypatch.setattr(
            sem, "suggest_semantics", functools.partial(original, search_fn=search)
        )
        built.append(
            create_sdp(
                {"spawners": pd.DataFrame({"spawner_count": [120, 340]})},
                path=tmp_path / f"typed-{len(built)}",
                dataset_id="demo-1",
                semantic_max_per_role=2,
                seed_semantics=True,
                seed_verbose=False,
                check_updates=False,
            )
        )
        return Path(built[-1])

    return build


def _variable_slot(review: SemanticReview) -> pd.DataFrame:
    rows = review.rows
    return rows[(rows["column_name"] == "spawner_count") & (rows["role"] == "variable")]


def _written_term(package: Path) -> tuple:
    """The ``spawner_count`` row's ``term_iri`` and ``term_type``, as written."""
    dictionary = _dictionary_csv(package)
    row = dictionary[dictionary["column_name"] == "spawner_count"].iloc[0]
    return row["term_iri"], row["term_type"]


def _managed_bytes(package: Path) -> dict:
    """The bytes of every file an apply manages, as metasalmon's
    ``managed_digests()`` reads them."""
    targets = [
        package / "metadata" / "column_dictionary.csv",
        package / "metadata" / "tables.csv",
        package / "datapackage.json",
        package / "semantic_suggestions.csv",
    ]
    return {path.name: path.read_bytes() for path in targets if path.is_file()}


def _assert_same_bytes(before: dict, after: dict) -> None:
    assert sorted(after) == sorted(before)
    for name, payload in before.items():
        assert after[name] == payload, name


def test_hand_picking_a_lower_ranked_owl_class_candidates_iri_writes_its_term_type_and_a_rebuild_reapplies_the_same_bytes(
    typed_package,
):
    package = typed_package()
    review = review_semantics(str(package))
    # The premise, asserted rather than assumed: the IRI is a candidate's below
    # rank 1, that candidate is an ``owl_class``, and the rank-1 candidate is not.
    slot = _variable_slot(review)
    assert list(slot.loc[slot["iri"] == OWL_CLASS_IRI, "rank"]) == [2]
    assert list(slot.loc[slot["iri"] == OWL_CLASS_IRI, "term_type"]) == ["owl_class"]
    assert list(slot.loc[slot["rank"] == 1, "term_type"]) == ["skos_concept"]

    apply_sdp_semantics(
        str(package),
        accept_suggestion(review, "spawner_count", "variable", iri=OWL_CLASS_IRI),
        quiet=True,
    )
    assert _written_term(package) == (OWL_CLASS_IRI, "owl_class")
    first_apply = _managed_bytes(package)

    # The rebuilt review carries the decision, on the candidate's own row, so
    # re-applying it is a second apply of the same decision.
    rebuilt = review_semantics(str(package), include_filled=True)
    replayed = _variable_slot(rebuilt)
    replayed = replayed[replayed["decision"].notna()]
    assert list(replayed["decision"]) == ["accept"]
    assert list(replayed["decision_iri"]) == [OWL_CLASS_IRI]
    assert list(replayed["rank"]) == [2]

    apply_sdp_semantics(str(package), rebuilt, quiet=True)
    # ``datapackage.json`` carries ``term_type`` too, so it has to hold still as
    # well as ``column_dictionary.csv``.
    _assert_same_bytes(first_apply, _managed_bytes(package))


def _decided(review: SemanticReview) -> list:
    """Where a review's decisions sit: slot, rank, decision and IRI of each."""
    rows = review.rows
    decided = rows[rows["decision"].notna()]
    return list(
        zip(
            decided["slot_id"],
            decided["rank"],
            decided["decision"],
            decided["decision_iri"],
        )
    )


def test_accept_iri_naming_a_shortlisted_candidate_records_what_rank_records(
    typed_package,
):
    review = review_semantics(str(typed_package()))
    by_rank = accept_suggestion(review, "spawner_count", "variable", rank=2)
    # The marker is stripped before the IRI is compared, so a marked spelling of
    # the candidate's IRI is the same decision.
    for iri in (OWL_CLASS_IRI, "REVIEW: " + OWL_CLASS_IRI):
        by_iri = accept_suggestion(review, "spawner_count", "variable", iri=iri)
        # Where the decision sits, first: ``assert_frame_equal`` raises a
        # ``TypeError`` rather than a diff where a ``pd.NA`` faces a value
        # (pandas 3.0.6), so this is the assertion that says what differs.
        assert _decided(by_iri) == _decided(by_rank), iri
        pd.testing.assert_frame_equal(by_iri.rows, by_rank.rows)


@pytest.mark.parametrize(
    "decide", [{"iri": OWL_CLASS_IRI}, {"rank": 2}], ids=["by-iri", "by-rank"]
)
def test_a_candidate_whose_stored_iri_carries_the_review_marker_writes_its_own_term_type(
    typed_package, decide
):
    # Whether the decision row IS the accepted candidate is decided by comparing
    # IRIs. The writer compared the stored IRI, marker and all, with a decision
    # recorded without the marker, so a marked candidate never matched and wrote
    # ``skos_concept`` by ``rank=`` as much as by ``iri=``.
    marked_iri = "REVIEW: " + OWL_CLASS_IRI
    package = typed_package()
    suggestions_path = package / "semantic_suggestions.csv"
    suggestions = read_sdp_csv(suggestions_path)
    suggestions.loc[suggestions["iri"] == OWL_CLASS_IRI, "iri"] = marked_iri
    suggestions.to_csv(suggestions_path, index=False)
    review = review_semantics(str(package))
    # The premise: the rank-2 candidate is stored marked, and is an ``owl_class``.
    slot = _variable_slot(review)
    assert list(slot.loc[slot["rank"] == 2, "iri"]) == [marked_iri]
    assert list(slot.loc[slot["rank"] == 2, "term_type"]) == ["owl_class"]

    apply_sdp_semantics(
        str(package),
        accept_suggestion(review, "spawner_count", "variable", **decide),
        quiet=True,
    )
    assert _written_term(package) == (OWL_CLASS_IRI, "owl_class")

    first_apply = _managed_bytes(package)
    apply_sdp_semantics(
        str(package), review_semantics(str(package), include_filled=True), quiet=True
    )
    _assert_same_bytes(first_apply, _managed_bytes(package))


def test_an_iri_no_candidate_carries_still_writes_skos_concept_whatever_the_first_candidate_is(
    typed_package,
):
    # B-176's case, ported here by B-216, which this must not move. Nothing is
    # known about a term the reviewer typed, so the type of the row its decision
    # is recorded on is not evidence about it. Every candidate here is an
    # ``owl_class``, so a writer that took the first row's type regardless would
    # write ``owl_class``.
    package = typed_package(type_iris=(OWL_CLASS_TYPE_IRI, OWL_CLASS_TYPE_IRI))
    review = review_semantics(str(package))
    assert set(_variable_slot(review)["term_type"]) == {"owl_class"}
    assert HANDPICKED_IRI not in set(review.rows["iri"])

    apply_sdp_semantics(
        str(package),
        accept_suggestion(review, "spawner_count", "variable", iri=HANDPICKED_IRI),
        quiet=True,
    )
    assert _written_term(package) == (HANDPICKED_IRI, "skos_concept")


def test_a_decision_survives_being_put_down_and_picked_up_again(seeded_package):
    """The round trip is the point of persisting the decision at all.

    A reviewer who works through sixteen slots, rejects four and comes back
    tomorrow was shown those four as if they were new: a reject CLEARS the
    field, and a blank field reads as undecided.
    """
    review = reject_suggestion(
        review_semantics(str(seeded_package)),
        "spawner_count",
        "variable",
        reason="none describe a spawner count",
    )
    apply_sdp_semantics(str(seeded_package), review, quiet=True)

    rebuilt = review_semantics(str(seeded_package))
    decided_slot = "column_dictionary.csv|demo-1/spawners/spawner_count|term_iri"
    assert decided_slot not in set(rebuilt["slot_id"])

    with_filled = review_semantics(str(seeded_package), include_filled=True)
    replayed = with_filled.rows[with_filled.rows["slot_id"] == decided_slot]
    assert set(replayed["decision"]) == {"reject"}
    assert (
        replayed["decision_reason"].iloc[0] == "none describe a spawner count"
    )
    assert "DECIDED: reject" in str(with_filled)


def test_an_accepted_decision_replays_with_its_iri(seeded_package):
    review = accept_suggestion(
        review_semantics(str(seeded_package)),
        "spawner_count",
        "variable",
        rank=1,
    )
    apply_sdp_semantics(str(seeded_package), review, quiet=True)
    replayed = review_semantics(str(seeded_package), include_filled=True)
    row = replayed.rows[
        (replayed.rows["column_name"] == "spawner_count")
        & (replayed.rows["role"] == "variable")
        & (replayed.rows["decision"].notna())
    ]
    assert row["decision"].iloc[0] == "accept"
    assert row["decision_iri"].iloc[0] == SPAWNER_IRI
    assert "DECIDED: accept" in str(replayed)


def test_tuesdays_acceptance_does_not_erase_mondays_rejection(seeded_package):
    # Blanking the column first made the file a record of the last review
    # object rather than of the package.
    monday = reject_suggestion(
        review_semantics(str(seeded_package)),
        "spawner_count",
        "variable",
        reason="monday",
    )
    apply_sdp_semantics(str(seeded_package), monday, quiet=True)

    tuesday = accept_suggestion(
        review_semantics(str(seeded_package)),
        "spawner_count",
        "property",
        rank=1,
    )
    apply_sdp_semantics(str(seeded_package), tuesday, quiet=True)

    written = _suggestions_csv(seeded_package)
    monday_row = written[
        (written["column_name"] == "spawner_count")
        & (written["dictionary_role"] == "variable")
    ]
    assert monday_row["decision"].iloc[0] == "rejected"
    assert monday_row["decision_reason"].iloc[0] == "monday"


def test_a_reviewed_accept_the_lexical_gate_would_drop_still_lands(
    seeded_package,
):
    """metasalmon backlog #118, end to end through the review flow.

    ``stream_name`` is an attribute column and ``Spawner Abundance`` shares no
    token with it, so the unattended auto-apply gate refuses the candidate.
    ``review_semantics()`` shows it, the user accepts it, and the write-back
    must not overrule that with a regex.
    """
    review = accept_suggestion(
        review_semantics(str(seeded_package)),
        "stream_name",
        "variable",
        rank=1,
    )
    apply_sdp_semantics(str(seeded_package), review, quiet=True)
    row = _dictionary_csv(seeded_package)
    assert (
        row.loc[row["column_name"] == "stream_name", "term_iri"].iloc[0]
        == SPAWNER_IRI
    )


# ---------------------------------------------------------------------------
# The feature's own surface: the checklist, and what prune destroys
# ---------------------------------------------------------------------------


def test_the_review_checklist_hands_over_the_python_calls(seeded_package):
    """The documentation half of the S5 change, as an observable artifact.

    Until the port this file told users to open the CSVs, which is the path the
    whole stream exists to replace.
    """
    text = (seeded_package / "README-review.txt").read_text()
    assert "review = review_semantics(pkg_path)" in text
    assert "apply_sdp_semantics(pkg_path, review)" in text
    assert "review_metadata(pkg_path)" in text
    assert "validate_salmon_datapackage(pkg_path, require_iris=True)" in text
    # The spreadsheet path is still described -- as the fallback, with the
    # reason it is the fallback.
    assert "fallback" in text
    assert "no record of why a term was chosen" in text


def test_the_checklist_says_so_when_there_is_no_shortlist(tmp_path, monkeypatch):
    from metasalmonpy import semantics as sem

    def empty_search(query, role=None, sources=None):
        return pd.DataFrame()

    monkeypatch.setattr(
        sem,
        "suggest_semantics",
        functools.partial(sem.suggest_semantics, search_fn=empty_search),
    )
    package = create_sdp(
        {"spawners": pd.DataFrame({"spawner_count": [1, 2]})},
        path=tmp_path / "bare",
        dataset_id="demo-1",
        seed_semantics=True,
        seed_verbose=False,
        check_updates=False,
    )
    text = (package / "README-review.txt").read_text()
    assert "no shortlist to review" in text
    assert "set_sdp_column(pkg_path" in text


def test_prune_warns_before_it_destroys_recorded_decisions(seeded_package):
    """Pruning wipes the directory, and semantic_suggestions.csv is not among
    the files a rewrite produces. The prune still happens -- a clean rebuild is
    a legitimate thing to want -- but it says what it is about to take."""
    from metasalmonpy import read_salmon_datapackage, write_salmon_datapackage

    review = reject_suggestion(
        review_semantics(str(seeded_package)),
        "spawner_count",
        "variable",
        reason="none fit",
    )
    apply_sdp_semantics(str(seeded_package), review, quiet=True)

    package = read_salmon_datapackage(str(seeded_package))
    with pytest.warns(UserWarning, match="records 1 review decision"):
        write_salmon_datapackage(
            resources=package["resources"],
            dataset_meta=package["dataset"],
            table_meta=package["tables"],
            dict_df=package["dictionary"],
            codes=package["codes"],
            path=seeded_package,
            overwrite=True,
            prune=True,
        )
    assert not (seeded_package / "semantic_suggestions.csv").exists()


def test_prune_is_silent_when_there_is_no_decision_to_lose(seeded_package):
    from metasalmonpy import read_salmon_datapackage, write_salmon_datapackage

    package = read_salmon_datapackage(str(seeded_package))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        write_salmon_datapackage(
            resources=package["resources"],
            dataset_meta=package["dataset"],
            table_meta=package["tables"],
            dict_df=package["dictionary"],
            codes=package["codes"],
            path=seeded_package,
            overwrite=True,
            prune=True,
        )
    assert not [
        entry for entry in caught if "review decision" in str(entry.message)
    ]
