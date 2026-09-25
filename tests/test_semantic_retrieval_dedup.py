"""``suggest_semantics()`` searches each distinct (query, role, sources) tuple once.

metasalmon backlog **#56** (hub queue **B-56**; this port is hub queue
**B-243**): the retrieval loop called ``search_fn()`` once per target, so a
column repeated across tables, or a unit query two columns fall back to, was
searched again for every target that carried it.

Ported from metasalmon pull request #164. The R counterpart is
``tests/testthat/test-semantic-retrieval-dedup.R``, and the cases below are its
cases, plus two: an answer every source gave is kept even when it is empty,
and nothing is kept from one call to the next.

The fixture is duplicate-heavy on purpose: four tables carry the same two
columns, so every tuple is shared by at least four targets (eight for the
``count`` unit query both columns fall back to). Only the table differs, and
the table is not part of what is searched.

**The per-target reference is ``suggest_semantics()`` with the dedup taken
out**: ``_search_once_per_call`` is replaced by a function that hands back the
search it was given, so the loop searches once per target. Its output is what
every target must still get, and its call log is how many targets share each
tuple. The first test checks that log before relying on it, so a reference
that stopped searching once per target fails there rather than comparing the
dedup with itself.
"""

from __future__ import annotations

import collections
import re

import pandas as pd

from metasalmonpy import semantics
from metasalmonpy.semantics import suggest_semantics

TABLES = ("t1", "t2", "t3", "t4")
COLUMNS = (
    ("spawners", "Spawner abundance", "Spawner abundance estimate"),
    ("fork_length", "Fork length", "Fork length of sampled fish"),
)
CANDIDATE_COLUMNS = (
    "label", "iri", "source", "ontology", "role", "match_type", "definition", "score"
)


def _fixture_dict() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "dataset_id": "d1",
                "table_id": table_id,
                "column_name": name,
                "column_label": label,
                "column_description": description,
                "column_role": "measurement",
                "value_type": "number",
                "unit_label": pd.NA,
                "unit_iri": pd.NA,
                "term_iri": pd.NA,
                "property_iri": pd.NA,
                "entity_iri": pd.NA,
                "constraint_iri": pd.NA,
                "statistical_modifier_iri": pd.NA,
            }
            for table_id in TABLES
            for name, label, description in COLUMNS
        ]
    )


def _slug(query: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", query.lower())


def _diagnostics(status: str) -> pd.DataFrame:
    return pd.DataFrame({"source": ["ols"], "status": [status]})


def _counting_search(answer=lambda n: "full"):
    """A search that logs every call it receives and answers from the tuple.

    A target served another tuple's answer is visible in its IRIs.
    ``answer(n)`` decides how the n-th call for one tuple is answered:

    * ``"full"``: three candidates, and no diagnostics, as in the R test;
    * ``"partial"``: one candidate, from a lookup whose diagnostics say a
      source did not answer;
    * ``"outage"``: no candidates and the same diagnostics, which is what
      ``find_terms()`` returns when its only source fails;
    * ``"answered_empty"``: no candidates, from a source that did answer.
    """
    keys = []

    def search_fn(query, role=None, sources=None):
        key = (query, role, tuple(sources))
        keys.append(key)
        base = f"https://example.org/{_slug(query)}/{role}/"
        kind = answer(keys.count(key))
        if kind == "full":
            return pd.DataFrame(
                {
                    "label": [f"{query} {suffix}" for suffix in "ABC"],
                    "iri": [base + suffix for suffix in "abc"],
                    "source": "ols",
                    "ontology": "demo",
                    "role": role,
                    "match_type": "label",
                    "definition": "",
                    "score": [3.0, 2.0, 1.0],
                }
            )
        if kind == "partial":
            res = pd.DataFrame(
                {
                    "label": [f"{query} partial"],
                    "iri": [base + "partial"],
                    "source": ["ols"],
                    "ontology": ["demo"],
                    "role": [role],
                    "match_type": ["label"],
                    "definition": [""],
                    "score": [1.0],
                }
            )
            res.attrs["diagnostics"] = _diagnostics("error")
            return res
        res = pd.DataFrame(columns=list(CANDIDATE_COLUMNS))
        res.attrs["diagnostics"] = _diagnostics(
            "error" if kind == "outage" else "success"
        )
        return res

    return search_fn, keys


def _suggest(search_fn) -> pd.DataFrame:
    return suggest_semantics(
        None,
        _fixture_dict(),
        sources=["ols"],
        max_per_role=2,
        search_fn=search_fn,
    )


def _per_target_reference(monkeypatch, answer=lambda n: "full"):
    """``suggest_semantics()`` searching once per target, as a reference."""
    search_fn, keys = _counting_search(answer)
    with monkeypatch.context() as patch:
        patch.setattr(
            semantics,
            "_search_once_per_call",
            lambda search: search,
            raising=False,
        )
        res = _suggest(search_fn)
    return res, keys


def _assert_same_result(res: pd.DataFrame, reference: pd.DataFrame) -> None:
    pd.testing.assert_frame_equal(res, reference)
    assert sorted(res.attrs) == sorted(reference.attrs)
    for name in reference.attrs:
        pd.testing.assert_frame_equal(res.attrs[name], reference.attrs[name])


def test_each_distinct_query_role_and_sources_tuple_is_searched_once(monkeypatch):
    search_fn, keys = _counting_search()
    res = _suggest(search_fn)
    reference, reference_keys = _per_target_reference(monkeypatch)

    # The premise: target by target, every tuple is searched at least four times.
    assert keys
    assert min(collections.Counter(reference_keys).values()) >= 4

    # The fix: each distinct tuple is searched once, no tuple is missed, and the
    # searches run in the order the targets first ask for them.
    assert len(keys) == len(set(keys))
    assert keys == list(dict.fromkeys(reference_keys))

    # Every target still gets exactly what a search of its own tuple returns,
    # in the same order, and nothing else in the result moves either.
    _assert_same_result(res, reference)

    # And each table's rows carry that table's own target columns.
    suggestions = res.attrs["semantic_suggestions"]
    assert set(suggestions["table_id"]) == set(TABLES)
    expected_prefix = (
        "https://example.org/"
        + suggestions["retrieval_query"].map(_slug)
        + "/"
        + suggestions["dictionary_role"]
        + "/"
    )
    assert all(
        iri.startswith(prefix)
        for iri, prefix in zip(suggestions["iri"], expected_prefix)
    )


def test_a_degraded_answer_is_never_served_to_another_target(monkeypatch):
    # First search of each tuple degraded, every later one answered in full.
    search_fn, keys = _counting_search(lambda n: "partial" if n == 1 else "full")
    res = _suggest(search_fn)
    suggestions = res.attrs["semantic_suggestions"]

    # The degraded answer was not kept, so the tuple's next target searched
    # again; the full answer was kept, so no target after that searched.
    assert set(collections.Counter(keys).values()) == {2}

    # The target that got the degraded answer keeps it, as before, and every
    # other target with that tuple gets the full answer. One row per target, in
    # the order the targets were searched.
    identity = ["table_id", "column_name", "dictionary_role", "retrieval_query"]
    targets = suggestions[identity].drop_duplicates()
    first_of_tuple = ~targets.duplicated(subset=["retrieval_query", "dictionary_role"])
    got_partial = [
        suggestions.loc[
            (suggestions[identity] == tuple(target)).all(axis=1), "iri"
        ].str.endswith("/partial").any()
        for target in targets.itertuples(index=False)
    ]
    assert len(got_partial) == len(res.attrs["semantic_targets"])
    assert got_partial == first_of_tuple.tolist()

    # An outage that lasts: every target searches for itself and nothing is
    # shared. Its answers carry no candidates, where R's carry one. A
    # one-candidate degraded answer for every target stops suggest_semantics()
    # at pd.concat before any of this is reached, because pandas compares the
    # candidate frames' attrs there and a DataFrame in attrs has no truth value.
    # That is a defect of its own, outside this file. *Retires when:* the
    # candidate frames reach pd.concat without the attrs of the search that
    # returned them; the outage can then answer with "partial", as R's does.
    outage_fn, outage_keys = _counting_search(lambda n: "outage")
    outage = _suggest(outage_fn)
    reference, reference_keys = _per_target_reference(monkeypatch, lambda n: "outage")
    assert len(outage_keys) == len(outage.attrs["semantic_targets"])
    assert collections.Counter(outage_keys) == collections.Counter(reference_keys)
    _assert_same_result(outage, reference)
    assert outage.attrs["semantic_suggestions"].empty


def test_an_answer_every_source_gave_is_kept_even_when_empty(monkeypatch):
    # Whether an answer is kept turns on whether its diagnostics name a source
    # that did not answer, as _search_failed_sources() reads them, not on
    # whether it carries diagnostics at all: every find_terms() answer does. So
    # a gap every source reported is searched once, like any other answer.
    search_fn, keys = _counting_search(lambda n: "answered_empty")
    res = _suggest(search_fn)
    reference, reference_keys = _per_target_reference(
        monkeypatch, lambda n: "answered_empty"
    )
    assert len(keys) == len(set(keys)) < len(reference_keys)
    assert keys == list(dict.fromkeys(reference_keys))
    _assert_same_result(res, reference)


def test_the_saving_lasts_for_one_call():
    # A second call searches every tuple again: nothing one call learned is
    # carried into the next, so this is not a second cache beside find_terms()'s.
    search_fn, keys = _counting_search()
    _suggest(search_fn)
    first = list(keys)
    _suggest(search_fn)
    second = keys[len(first):]
    assert first
    assert collections.Counter(second) == collections.Counter(first)
