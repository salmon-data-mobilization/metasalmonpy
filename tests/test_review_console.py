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
from metasalmonpy.review_console import (
    SemanticReview,
    _accept_call,
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
    ``vocabulary_iri``, and discovery still gives it a code-level target, with
    the three roles a measurement parent gives its codes. Its ``code_value`` is
    as empty as the column's own slot's, so only the file tells the two apart.
    Mirrors R's ``vocabulary_code_review()``.
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
                    code_value=pd.NA,
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
    return review_semantics(dictionary)


def test_a_blank_code_value_never_selects_a_code_slot_whose_codes_row_has_no_code_value():
    review = _vocabulary_code_review()
    rows = review.rows
    column_slots = set(rows.loc[rows["target_file"] != "codes.csv", "slot_id"])

    # The column's own slots print calls that run and decide them. The code
    # slot's own calls may still refuse, because no argument tells a code slot
    # with no code value apart from the column's own slot; that is a separate
    # defect, recorded in metasalmon's .hub/workpads/B-151.md. What no call may
    # do is decide the other slot.
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


def _assert_column_call_writes_the_dictionary(path: Path, review: SemanticReview):
    """Paste the column's own entity call, apply it, and check it wrote the
    column's ``entity_iri`` and left ``codes.csv`` alone. Mirrors R's
    ``expect_column_call_writes_the_dictionary()``."""

    def read(file_name: str) -> pd.DataFrame:
        return pd.read_csv(
            path / "metadata" / file_name, dtype=str, keep_default_na=False
        )

    rows = review.rows
    codes_before = read("codes.csv")
    call = _accept_call(rows, MEASUREMENT_COLUMN_SLOT, 1)
    assert 'code_value=""' in call
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


def test_a_measurement_column_whose_codes_row_names_a_vocabulary_round_trips_from_create_sdp_to_disk(
    tmp_path, monkeypatch
):
    # The vocabulary-backed row through the real pipeline: the column's only
    # codes.csv row supplies vocabulary_iri and no code value.
    codes = pd.DataFrame(
        {
            "dataset_id": ["demo-1"],
            "table_id": ["spawners"],
            "column_name": ["spawner_count"],
            "code_value": [pd.NA],
            "code_label": ["Count categories"],
            "code_description": [
                "Counts are recorded against a published category vocabulary."
            ],
            "vocabulary_iri": ["https://example.org/vocab/count-categories"],
        }
    )
    path = _measurement_code_package(tmp_path, monkeypatch, codes, "vocabulary-codes")
    review = review_semantics(str(path))
    rows = review.rows

    # Counted, not named: the slot is keyed on the code value's text, and R and
    # Python spell a missing value differently there ("NA" against "nan").
    code_slots = set(rows.loc[rows["target_file"] == "codes.csv", "slot_id"])
    assert len(code_slots) == 1
    assert {MEASUREMENT_COLUMN_SLOT} | code_slots <= set(
        rows.loc[rows["role"] == "entity", "slot_id"]
    )

    column_slots = set(rows.loc[rows["target_file"] != "codes.csv", "slot_id"])
    _assert_printed_calls_decide_their_own_slots(review, must_run=column_slots)
    _assert_column_call_writes_the_dictionary(path, review)


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
    "a dotless i": "REVıEW:",
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
