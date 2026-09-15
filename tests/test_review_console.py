"""The Python-native semantic review (stream S5 port, metasalmon 0.5.0).

The load-bearing test in this file is
``test_every_printed_accept_call_produces_the_decision_it_claims``: the printed
call is the feature, so the tests ``exec()`` it. A printed call that names a
column the package does not have passes every substring assertion ever written
and fails the moment anyone pastes it.
"""

from __future__ import annotations

import functools
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
from metasalmonpy.review_console import SemanticReview

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


def test_a_column_level_slot_sharing_a_role_with_its_codes_is_still_ambiguous():
    """A known limitation, shared with metasalmon 0.5.0 and pinned here.

    A **measurement** column with a code list gets a column-level
    ``entity_iri`` target (role ``entity``, no ``code_value``) AND a code-level
    ``codes.csv`` ``term_iri`` target per code (role ``entity``, with a
    ``code_value``) -- see ``semantics.py``'s
    ``["constraint", "entity", "method"]`` role set for a measurement parent.
    ``_review_call_args()`` adds ``code_value=`` only when the row it is
    printing *has* one, so the column-level slot prints
    ``accept_suggestion(review, "col", "entity", rank=1, table="t")``, which
    then resolves to two slots and raises.

    R's ``.ms_review_call_args()`` / ``.ms_review_match_slot_rows()`` behave
    identically, so this is inherited rather than introduced, and it is left
    matching R rather than fixed here: a deliberate divergence would need a
    ``PARITY.md`` row, and the S5 port is a port. The raised message does name
    ``code_value=`` as the argument to add, so a user can recover.

    *Retires when:* the shared defect is fixed in both implementations -- the
    column-level slot needs to constrain ``code_value`` to *absent* rather than
    leaving it unconstrained. Reported as a hub queue candidate by the B-126
    port; delete this test when that item lands.
    """
    dictionary = _dictionary_with(
        [
            _suggestion_row(
                dictionary_role="entity", target_sdp_field="entity_iri"
            ),
            _suggestion_row(
                code_value="wild",
                dictionary_role="entity",
                target_sdp_file="codes.csv",
                target_sdp_field="term_iri",
                target_row_key="demo-1/spawners/spawner_count/wild",
            ),
        ]
    )
    review = review_semantics(dictionary)
    printed = [
        line.strip()[len("review = "):]
        for line in review.render_lines(object_name="review")
        if line.strip().startswith("review = accept_suggestion(")
    ]
    column_level = printed[0]
    assert "code_value=" not in column_level
    with pytest.raises(ValueError, match="more than one review slot"):
        eval(  # noqa: S307
            column_level,
            {"accept_suggestion": accept_suggestion, "review": review},
        )
    # The code-level sibling's own printed call is unambiguous and runs.
    assert 'code_value="wild"' in printed[1]
    eval(  # noqa: S307
        printed[1], {"accept_suggestion": accept_suggestion, "review": review}
    )


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
