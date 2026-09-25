"""The retry-query classifier, pinned against metasalmon's verdicts (hub B-362).

A retry query written into the shared review record must get the same verdict
in both packages, so the classifier here is a port of metasalmon's
``.ms_llm_normalize_query_text()``, ``.ms_llm_query_looks_like_identifier()``
and ``.ms_llm_classify_retry_query()`` (``R/llm-semantic-helpers.R``).

Every verdict in ``tests/data/llm_review/r-retry-query-verdicts.json`` comes
from **running** R, not from reading it: ``r-retry-query-verdicts.R`` drove the
three functions over ``retry-query-corpus.json`` under R 4.5.2 in a UTF-8
locale, with metasalmon loaded from a read-only export of ``main`` at
``98cb9e6`` (2026-09-25). Two of R's verdicts are surprising and are pinned on
purpose, because the record needs one answer in both packages:

* R's identifier pattern ends in ``[^\\s]+`` under TRE, whose bracket
  expressions have no escapes, so it reads "neither a backslash nor the letter
  s". ``abc:d e`` is therefore identifier-like and ``smn:species`` is not.
  The port reproduces that, and says what retires it, in ``llm_review.py``.
* ``trimws()`` strips only space, tab, CR and LF, so a lone vertical tab
  survives the emptiness check and collapses to an *empty usable* query.

One verdict is deliberately **not** taken from current R. R's duplicate check
compares ``tolower()`` of both queries, and ``tolower()`` folds non-ASCII
letters by locale, so the same pair can be a duplicate in one locale and not in
another. The ruled target (S16 execplan, decisions 11 and 12) is case folding
over ASCII letters only, the same in every locale; metasalmon moves to it under
hub **B-361** point (5), and this module pins the target directly with a pair
that differs only in a non-ASCII letter's case. The corpus carries no such
pair, so the fixture reads the same before and after B-361 lands.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from metasalmonpy import infer_dictionary, suggest_semantics
from metasalmonpy.llm_review import _classify_retry_query

DATA = Path(__file__).resolve().parent / "data" / "llm_review"
R_VERDICTS = json.loads(
    (DATA / "r-retry-query-verdicts.json").read_text(encoding="utf-8")
)


def _cases():
    return [pytest.param(case, id=case["id"]) for case in R_VERDICTS["cases"]]


@pytest.mark.parametrize("case", _cases())
def test_classifier_gives_metasalmons_verdict(case):
    verdict = _classify_retry_query(case["retry"], case["original"])
    assert verdict == {
        "query": case["query"],
        "original_query": case["original_query"],
        "disposition": case["disposition"],
        "rejection_reason": case["rejection_reason"],
    }


@pytest.mark.parametrize("case", _cases())
def test_normalizer_and_identifier_check_give_metasalmons_verdict(case):
    from metasalmonpy.llm_review import (
        _normalize_query_text,
        _query_looks_like_identifier,
    )

    assert _normalize_query_text(case["retry"]) == case["normalized_retry"]
    assert _query_looks_like_identifier(case["retry"]) is bool(
        case["looks_like_identifier"]
    )


def test_fixture_provenance_is_recorded():
    provenance = R_VERDICTS["provenance"]
    assert provenance["metasalmon_commit"] == "98cb9e6"
    assert provenance["r_version"].startswith("R version 4.5.2")


@pytest.mark.parametrize(
    "retry, original",
    [
        ("ÉTUDE", "étude"),
        ("Étude", "étude"),
        ("STRAßE", "strasse"),
    ],
)
def test_a_pair_differing_only_in_a_non_ascii_letters_case_is_not_a_duplicate(
    retry, original
):
    # The ruled rule, not current R's: case folds over ASCII letters only, the
    # same in every locale (B-361 point (5) is the metasalmon half).
    verdict = _classify_retry_query(retry, original)
    assert verdict["disposition"] == "use_query"
    assert verdict["rejection_reason"] is None


def test_ascii_letters_still_fold():
    verdict = _classify_retry_query("MEAN weight", "mean WEIGHT")
    assert verdict["disposition"] == "duplicate_original_query"
    assert verdict["rejection_reason"] == "duplicate_original_query"


# --- the classifier's consumers -------------------------------------------


def _measurement_dictionary():
    data = pd.DataFrame({"fork_length": [55.0, 61.0]})
    dictionary = infer_dictionary(data, dataset_id="demo", table_id="fish")
    dictionary.loc[0, "column_role"] = "measurement"
    dictionary.loc[0, "column_description"] = (
        "Fork length in millimetres measured using callipers for "
        "ocean-phase fish."
    )
    dictionary.loc[0, "unit_label"] = "millimetre"
    return data, dictionary


def _search_stub(calls):
    def search(query, role=None, sources=None):
        calls.append((query, role, tuple(sources or ())))
        query_slug = "-".join(str(query).lower().split())
        return pd.DataFrame(
            {
                "label": [f"{role} candidate"],
                "iri": [f"https://example.org/{role}/{query_slug}"],
                "source": [(sources or ["stub"])[0]],
                "ontology": ["test"],
                "role": [role],
                "role_hints": [role],
                "match_type": ["label"],
                "definition": [f"A {role} candidate."],
                "score": [1.0],
            }
        )

    return search


def _unit_retry_request(retry_query, alternate=None):
    state = {"count": 0}

    def request(messages, config):
        state["count"] += 1
        if "alternate_queries" in messages[0]["content"]:
            return {"alternate_queries": [alternate]}
        payload = json.loads(messages[-1]["content"])
        slots = []
        for slot in payload["slots"]:
            first_unit = slot["role"] == "unit" and state["count"] == 1
            slots.append(
                {
                    "role": slot["role"],
                    "decision": "retry_search" if first_unit else "review",
                    "confidence": 0.5,
                    "retry_query": retry_query if first_unit else None,
                    "rationale": "Review the slot.",
                }
            )
        return {"bundle_summary": "Review bundle.", "slots": slots}

    return request, state


def test_the_retry_is_searched_and_recorded_with_whitespace_collapsed():
    data, dictionary = _measurement_dictionary()
    search_calls = []
    request, state = _unit_retry_request("  precise   millimetre\tunit ")

    result = suggest_semantics(
        data,
        dictionary,
        search_fn=_search_stub(search_calls),
        llm_assess=True,
        llm_request_fn=request,
    )

    assessments = result.attrs["semantic_llm_assessments"]
    unit = assessments.loc[assessments["dictionary_role"] == "unit"].iloc[0]
    unit_queries = [query for query, role, _ in search_calls if role == "unit"]
    assert "precise millimetre unit" in unit_queries
    assert all(query == query.strip() and "  " not in query for query in unit_queries)
    assert unit["llm_exploration_used"]
    assert unit["llm_exploration_queries"] == "precise millimetre unit"
    assert state["count"] == 2


def test_a_curie_retry_query_takes_the_plain_language_fallback():
    data, dictionary = _measurement_dictionary()
    search_calls = []
    request, state = _unit_retry_request(
        "gcdfo_v2:X", alternate="fork length measurement unit"
    )

    result = suggest_semantics(
        data,
        dictionary,
        search_fn=_search_stub(search_calls),
        llm_assess=True,
        llm_request_fn=request,
    )

    assessments = result.attrs["semantic_llm_assessments"]
    unit = assessments.loc[assessments["dictionary_role"] == "unit"].iloc[0]
    unit_queries = [query for query, role, _ in search_calls if role == "unit"]
    assert "gcdfo_v2:X" not in unit_queries
    assert "fork length measurement unit" in unit_queries
    assert state["count"] == 3
    assert unit["llm_exploration_queries"] == "fork length measurement unit"
    assert pd.isna(unit["llm_retry_query_rejection_reason"])
