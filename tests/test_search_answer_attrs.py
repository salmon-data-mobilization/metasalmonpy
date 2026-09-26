"""Search answers carry a DataFrame in ``attrs``, and seeding must survive it.

Hub queue **B-370**. Every ``find_terms()`` answer carries its per-source
diagnostics in ``attrs["diagnostics"]``, a DataFrame. The retrieval loop in
``suggest_semantics()`` copied each answer with its ``attrs``, so every
candidate frame reached ``pd.concat()`` carrying one. When every input to a
concat has ``attrs``, pandas compares them, and a DataFrame there has no single
truth value, so the call raised as soon as two targets got candidates.
``create_sdp()`` with its defaults seeds semantics through that loop with
``find_terms()`` as the search, so the documented one-shot path raised on any
table a search could answer for two targets.

The candidate frames now reach ``pd.concat()`` without the ``attrs`` of the
search that returned them. The answer itself keeps them, so what a caller reads
from ``find_terms()``'s own ``attrs`` is unchanged.

No metasalmon twin: R has no counterpart of pandas' ``attrs`` comparison.
"""

from __future__ import annotations

import re
import socket
import warnings

import pandas as pd
import pytest

from metasalmonpy import create_sdp, find_terms, term_search
from metasalmonpy.semantics import suggest_semantics

# Every source ``find_terms()`` dispatches to. The ``create_sdp()`` test
# patches each of them and checks that no role reaches a source outside them.
SOURCES = ("smn", "gcdfo", "ols", "nvs", "zooma", "bioportal", "qudt", "gbif", "worms")
ROLES = ("variable", "property", "entity", "unit", "constraint", "statistical_modifier", "method")


def _dictionary() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "dataset_id": "d1",
                "table_id": "t1",
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
            for name, label, description in (
                ("spawners", "Spawner abundance", "Spawner abundance estimate"),
                ("fork_length", "Fork length", "Fork length of sampled fish"),
            )
        ]
    )


def _search(with_diagnostics: bool):
    """A search answering two candidates per call, and logging each answer.

    With ``with_diagnostics``, each answer carries a diagnostics frame in
    ``attrs`` as a ``find_terms()`` answer does: one row per source asked,
    naming the query, so no two targets' frames are equal.
    """
    answers = []

    def search_fn(query, role=None, sources=None):
        slug = re.sub(r"[^a-z0-9]+", "-", str(query).lower())
        res = pd.DataFrame(
            {
                "label": [f"{query} A", f"{query} B"],
                "iri": [f"https://example.org/{slug}/{role}/{suffix}" for suffix in "ab"],
                "source": "ols",
                "ontology": "demo",
                "role": role,
                "match_type": "label",
                "definition": "",
                "score": [2.0, 1.0],
            }
        )
        if with_diagnostics:
            res.attrs["diagnostics"] = pd.DataFrame(
                {
                    "source": list(sources or ["ols"]),
                    "query": query,
                    "status": "success",
                    "count": len(res),
                }
            )
        answers.append(res)
        return res

    return search_fn, answers


def _suggest(search_fn) -> pd.DataFrame:
    return suggest_semantics(
        None,
        _dictionary(),
        sources=["ols"],
        max_per_role=2,
        search_fn=search_fn,
    )


def test_suggest_semantics_keeps_the_candidates_of_answers_carrying_diagnostics():
    search_fn, answers = _search(with_diagnostics=True)
    res = _suggest(search_fn)
    suggestions = res.attrs["semantic_suggestions"]

    # The case that raised: two or more targets got candidates, and every
    # answer they came from carries a DataFrame in attrs.
    assert suggestions.groupby(["column_name", "dictionary_role"]).ngroups >= 2
    assert set(suggestions["column_name"]) == {"spawners", "fork_length"}

    # The candidates, and everything else returned, are what the same answers
    # give without attrs.
    plain_fn, _ = _search(with_diagnostics=False)
    reference = _suggest(plain_fn)
    pd.testing.assert_frame_equal(res, reference)
    assert sorted(res.attrs) == sorted(reference.attrs)
    for name in reference.attrs:
        pd.testing.assert_frame_equal(res.attrs[name], reference.attrs[name])

    # One search's diagnostics describe that search, and none of them is passed
    # off as the diagnostics of the candidates as a whole.
    assert "diagnostics" not in suggestions.attrs

    # Dropped from the loop's own copy, never from the answer: every answer the
    # search returned still carries the diagnostics it was given.
    assert answers
    assert all(isinstance(answer.attrs.get("diagnostics"), pd.DataFrame) for answer in answers)


@pytest.fixture
def no_network(monkeypatch):
    """Any connection attempt fails loudly rather than reaching a vocabulary."""

    def refuse(*args, **kwargs):
        raise OSError("network refused by tests/test_search_answer_attrs.py")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


def _patch_every_source(monkeypatch) -> list:
    calls = []

    def answering(source):
        def fake(query, role):
            calls.append(source)
            slug = re.sub(r"[^a-z0-9]+", "-", str(query).lower())
            return pd.DataFrame(
                {
                    "label": [f"{query} {source}"],
                    "iri": [f"https://example.org/{source}/{role}/{slug}"],
                    "source": [source],
                    "ontology": ["demo"],
                    "role": [role],
                    "match_type": ["label"],
                    "definition": [""],
                }
            )

        return fake

    for source in SOURCES:
        monkeypatch.setattr(term_search, f"_search_{source}", answering(source))
    return calls


def test_create_sdp_with_its_defaults_keeps_the_candidates(tmp_path, monkeypatch, no_network):
    calls = _patch_every_source(monkeypatch)

    # Every source a role can reach is patched, so no search is left to the
    # network, which refuses anyway.
    reachable = {source for role in ROLES + (None,) for source in term_search.sources_for_role(role)}
    assert reachable <= set(SOURCES)

    # The premise, measured rather than assumed: a find_terms() answer from
    # these sources carries a DataFrame in attrs, and every source answered.
    probe = find_terms("spawner count", role="variable")
    assert isinstance(probe.attrs["diagnostics"], pd.DataFrame)
    assert set(probe.attrs["diagnostics"]["status"]) == {"success"}
    calls.clear()

    frame = pd.DataFrame({"spawner_count": [120, 340], "fork_length_mm": [512.0, 498.5]})
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        package = create_sdp(frame, path=tmp_path / "sdp")

    # The default search ran, over the patched sources, and none of its lookups
    # was reported incomplete.
    assert calls
    assert not [w for w in caught if "Vocabulary lookup was incomplete" in str(w.message)]

    # Both columns kept their candidates, beside the table's own, and every
    # candidate is one the patched sources gave.
    written = pd.read_csv(package / "semantic_suggestions.csv")
    columns = written[written["target_scope"] == "column"]
    assert set(columns["column_name"]) == {"spawner_count", "fork_length_mm"}
    assert columns.groupby(["column_name", "dictionary_role"]).ngroups >= 2
    assert written["iri"].str.startswith("https://example.org/").all()
