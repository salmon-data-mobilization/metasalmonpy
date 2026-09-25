"""Retrieval through one function, and metasalmon's second-pass merge (hub B-363).

Two pins, one per half of the item.

**The extraction changes nothing.** ``suggest_semantics()`` used to retrieve
each target's shortlist in a loop written inline; that loop is now
``semantics._retrieve_semantic_target_candidates()``, the counterpart of
metasalmon's ``.ms_retrieve_semantic_target_candidates()``.
``tests/data/semantics/suggest-semantics-pinned.json`` is the output of
``run_pinned_cases()`` on ``main`` at ``ba1b54a``, **before** the loop moved:
the suggestions, the discovered targets and every ``search_fn`` call, for three
retrieval configurations over one multi-table fixture. The pin test replays
the same cases and requires the same output, row for row and column for
column. To regenerate it deliberately (a ruled change to pass-1 retrieval, not
a drift), write ``run_pinned_cases()``'s result to that file through the
package-binding route ``tests/conftest.py`` documents, and say in the commit
which behaviour was meant to change.

**The merge is R's.** ``semantics._merge_semantic_target_candidates()`` is a
port of ``.ms_merge_semantic_target_candidates()`` (``R/semantics-helpers.R``):
a C-order sort on seven keys (``-score`` or ``-role_hint_bonus``, then
``source``, ``ontology``, ``label``, ``iri``, ``retrieval_pass``,
``retrieval_query``), deduplicated by candidate identity
(``.ms_semantic_candidate_identity()``, ported as
``_semantic_candidate_identity()``), capped at ``max(1, max_per_role)``.
``tests/data/semantics/r-merge-candidates.json`` holds the merged rows,
identities and candidate gain that **running** R gave for the inputs in
``merge-candidates-cases.json`` (R 4.5.2, metasalmon ``main`` @ ``98cb9e6``,
via ``r-merge-candidates.R``, which sits beside the fixture). The Python merge
must give the same on the same inputs.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd
import pytest

from metasalmonpy import suggest_semantics

DATA = Path(__file__).resolve().parent / "data" / "semantics"
PIN_PATH = DATA / "suggest-semantics-pinned.json"
R_MERGE_PATH = DATA / "r-merge-candidates.json"


# --- one multi-table fixture -------------------------------------------------


def _fixture():
    fish = pd.DataFrame(
        {
            "fork_length": [55.0, 61.0],
            "mean_weight": [1.2, 1.4],
            "life_stage": ["adult", "juvenile"],
            "species_code": ["CO", "CK"],
        }
    )
    sites = pd.DataFrame(
        {"site_name": ["Upper", "Lower"], "water_temperature": [8.5, 9.0]}
    )
    dictionary = pd.DataFrame(
        [
            {
                "dataset_id": "demo",
                "table_id": "fish",
                "column_name": "fork_length",
                "column_label": "Fork length",
                "column_description": "Fork length in millimetres measured using callipers.",
                "column_role": "measurement",
                "value_type": "number",
                "unit_label": "millimetre",
                "required": False,
            },
            {
                "dataset_id": "demo",
                "table_id": "fish",
                "column_name": "mean_weight",
                "column_label": "Mean weight",
                "column_description": "Mean weight of sampled fish in grams.",
                "column_role": "measurement",
                "value_type": "number",
                "unit_label": "gram",
                "required": False,
            },
            {
                "dataset_id": "demo",
                "table_id": "fish",
                "column_name": "life_stage",
                "column_label": "Life stage",
                "column_description": "Salmon life stage.",
                "column_role": "categorical",
                "value_type": "string",
                "unit_label": pd.NA,
                "required": False,
            },
            {
                "dataset_id": "demo",
                "table_id": "fish",
                "column_name": "species_code",
                "column_label": "Species code",
                "column_description": "Species code.",
                "column_role": "attribute",
                "value_type": "string",
                "unit_label": pd.NA,
                "required": False,
            },
            {
                "dataset_id": "demo",
                "table_id": "sites",
                "column_name": "site_name",
                "column_label": "Site name",
                "column_description": "Name of the sampling site.",
                "column_role": "attribute",
                "value_type": "string",
                "unit_label": pd.NA,
                "required": False,
            },
            {
                "dataset_id": "demo",
                "table_id": "sites",
                "column_name": "water_temperature",
                "column_label": "Water temperature",
                "column_description": "Water temperature at the site.",
                "column_role": "measurement",
                "value_type": "number",
                "unit_label": "degree Celsius",
                "required": False,
            },
        ]
    )
    codes = pd.DataFrame(
        [
            {
                "dataset_id": "demo",
                "table_id": "fish",
                "column_name": "species_code",
                "code_value": "CO",
                "code_label": "Coho",
                "code_description": "Coho salmon",
            },
            {
                "dataset_id": "demo",
                "table_id": "fish",
                "column_name": "species_code",
                "code_value": "CK",
                "code_label": "Chinook",
                "code_description": "Chinook salmon",
            },
            {
                "dataset_id": "demo",
                "table_id": "fish",
                "column_name": "fork_length",
                "code_value": "NR",
                "code_label": "Not recorded",
                "code_description": "Length not recorded",
            },
        ]
    )
    table_meta = pd.DataFrame(
        [
            {
                "dataset_id": "demo",
                "table_id": "fish",
                "file_name": "fish.csv",
                "table_label": "Fish",
                "description": "Fish observations",
                "observation_unit": "salmon population",
                "observation_unit_iri": pd.NA,
            },
            {
                "dataset_id": "demo",
                "table_id": "sites",
                "file_name": "sites.csv",
                "table_label": "Sites",
                "description": "Sampling sites",
                "observation_unit": "stream reach",
                "observation_unit_iri": pd.NA,
            },
        ]
    )
    dataset_meta = pd.DataFrame(
        {
            "dataset_id": ["demo"],
            "title": ["Demo"],
            "description": ["Salmon monitoring"],
            "keywords": [pd.NA],
        }
    )
    return {"fish": fish, "sites": sites}, dictionary, codes, table_meta, dataset_meta


def _candidates(query, role, with_scores):
    """A deterministic shortlist with the shapes retrieval has to handle.

    Two rows share a ``(source, iri)`` (the second must be dropped), one comes
    from ``qudt`` (dropped under an explicit ``smn``/``gcdfo`` allowlist), one
    has no score, the hints cover a match, a mismatch and an unknown, and the
    ``variable`` and ``property`` shortlists share one label so the
    role-collision block has something to mark.
    """
    slug = "-".join(str(query).lower().split())
    mismatch = {"variable": "property", "property": "variable"}.get(role, "entity")
    shared = "Shared label" if role in ("variable", "property") else f"{role} label"
    rows = [
        {"label": shared, "iri": f"https://w3id.org/smn/{role}/{slug}", "source": "smn",
         "ontology": "smn", "role": role, "match_type": "label",
         "definition": f"An smn {role}.", "score": 1.2, "role_hints": role},
        {"label": f"{shared} (dup)", "iri": f"https://w3id.org/smn/{role}/{slug}", "source": "smn",
         "ontology": "smn", "role": role, "match_type": "synonym",
         "definition": "Duplicate iri.", "score": 1.1, "role_hints": role},
        {"label": f"Broad {role}", "iri": f"https://gcdfo.example/{role}/{slug}", "source": "gcdfo",
         "ontology": "gcdfo", "role": role, "match_type": "label",
         "definition": f"A gcdfo {role}.", "score": 1.2, "role_hints": mismatch},
        {"label": f"{role} unit", "iri": f"http://qudt.org/vocab/{role}/{slug}", "source": "qudt",
         "ontology": "qudt", "role": role, "match_type": "label",
         "definition": "A qudt term.", "score": 2.0, "role_hints": role},
        {"label": f"{role} obo", "iri": f"http://purl.obolibrary.org/obo/X_{len(slug)}", "source": "ols",
         "ontology": "obo", "role": role, "match_type": "label",
         "definition": "An OLS term.", "score": math.nan, "role_hints": pd.NA},
        {"label": f"alpha {role}", "iri": f"https://gcdfo.example/{role}/alpha", "source": "gcdfo",
         "ontology": "gcdfo", "role": role, "match_type": "label",
         "definition": "Tie-break row.", "score": 1.2, "role_hints": mismatch},
    ]
    frame = pd.DataFrame(rows)
    if not with_scores:
        frame = frame.drop(columns=["score"])
    return frame


def _search(calls, with_scores=True):
    def search(query, role=None, sources=None):
        calls.append([str(query), str(role), list(sources or ())])
        return _candidates(query, role, with_scores)

    return search


def _scalar(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if pd.api.types.is_bool(value):
        return bool(value)
    if pd.api.types.is_integer(value):
        return int(value)
    if pd.api.types.is_float(value):
        return float(value)
    return value if isinstance(value, str) else str(value)


def _records(frame: pd.DataFrame) -> dict:
    return {
        "columns": [str(column) for column in frame.columns],
        "rows": [
            {str(column): _scalar(value) for column, value in row.items()}
            for row in frame.to_dict(orient="records")
        ],
    }


def run_pinned_cases() -> dict:
    """The three retrieval configurations the pin covers."""
    cases = {
        "role-defaults-top3": {"sources": None, "max_per_role": 3, "scores": True},
        "explicit-smn-gcdfo-top2": {"sources": ["smn", "gcdfo"], "max_per_role": 2, "scores": True},
        "no-score-column-top3": {"sources": None, "max_per_role": 3, "scores": False},
    }
    out = {}
    for name, spec in cases.items():
        resources, dictionary, codes, table_meta, dataset_meta = _fixture()
        calls = []
        result = suggest_semantics(
            resources,
            dictionary,
            sources=spec["sources"],
            max_per_role=spec["max_per_role"],
            search_fn=_search(calls, spec["scores"]),
            codes=codes,
            table_meta=table_meta,
            dataset_meta=dataset_meta,
        )
        out[name] = {
            "search_calls": calls,
            "targets": _records(result.attrs["semantic_targets"]),
            "suggestions": _records(result.attrs["semantic_suggestions"]),
        }
    return out


# --- the pin: suggest_semantics() gives the output it gave before ------------


def _load(path: Path, default: dict) -> dict:
    # Guarded so the capture route can import this module before the pin
    # exists; test_both_fixtures_are_present() keeps a missing file loud.
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


PINNED = _load(PIN_PATH, {"cases": {}})


def test_both_fixtures_are_present():
    assert PIN_PATH.is_file(), PIN_PATH
    assert R_MERGE_PATH.is_file(), R_MERGE_PATH


@pytest.mark.parametrize("name", sorted(PINNED["cases"]))
def test_suggest_semantics_output_is_unchanged_by_the_extraction(name):
    actual = run_pinned_cases()[name]
    expected = PINNED["cases"][name]
    assert actual["search_calls"] == expected["search_calls"]
    assert actual["targets"]["columns"] == expected["targets"]["columns"]
    assert actual["targets"]["rows"] == expected["targets"]["rows"]
    assert actual["suggestions"]["columns"] == expected["suggestions"]["columns"]
    assert len(actual["suggestions"]["rows"]) == len(expected["suggestions"]["rows"])
    for position, (got, want) in enumerate(
        zip(actual["suggestions"]["rows"], expected["suggestions"]["rows"])
    ):
        assert got == want, f"row {position} differs"


def test_the_pin_covers_every_retrieval_shape():
    suggestions = PINNED["cases"]["role-defaults-top3"]["suggestions"]["rows"]
    assert any(row["role_collision"] for row in suggestions)
    assert {row["role_hint_status"] for row in suggestions} >= {
        "match",
        "mismatch_property",
        "unknown",
    }
    assert {row["target_scope"] for row in suggestions} == {"column", "code", "table", "dataset"}
    explicit = PINNED["cases"]["explicit-smn-gcdfo-top2"]["suggestions"]["rows"]
    assert {row["source"] for row in explicit} == {"smn", "gcdfo"}


def test_suggest_semantics_delegates_to_the_retrieval_function(monkeypatch):
    from metasalmonpy import semantics

    seen = []
    original = semantics._retrieve_semantic_target_candidates

    def spy(target, source_policy, max_per_role, search_fn, query=None, retrieval_pass=1):
        seen.append((dict(target)["dictionary_role"], query, retrieval_pass, max_per_role))
        return original(target, source_policy, max_per_role, search_fn, query, retrieval_pass)

    monkeypatch.setattr(semantics, "_retrieve_semantic_target_candidates", spy)
    resources, dictionary, codes, table_meta, dataset_meta = _fixture()
    calls = []
    suggest_semantics(
        resources,
        dictionary,
        max_per_role=2,
        search_fn=_search(calls),
        codes=codes,
        table_meta=table_meta,
        dataset_meta=dataset_meta,
    )
    assert len(seen) == len(calls)
    assert all(query is None and retrieval_pass == 1 and depth == 2 for _, query, retrieval_pass, depth in seen)


def test_retrieval_function_returns_nothing_without_a_query_or_a_role():
    from metasalmonpy.llm_review import make_source_policy
    from metasalmonpy.semantics import _retrieve_semantic_target_candidates

    def refuse(query, role=None, sources=None):  # pragma: no cover - must not run
        raise AssertionError("search_fn must not be called")

    policy = make_source_policy(None)
    blank_query = {"dictionary_role": "unit", "search_query": "   "}
    assert _retrieve_semantic_target_candidates(blank_query, policy, 3, refuse).empty
    no_role = {"dictionary_role": "", "search_query": "millimetre"}
    assert _retrieve_semantic_target_candidates(no_role, policy, 3, refuse).empty
    # An explicit query overrides the target's, and the pass is recorded.
    calls = []
    rows = _retrieve_semantic_target_candidates(
        {"dictionary_role": "unit", "search_query": "millimetre"},
        policy,
        3,
        _search(calls),
        query="precise millimetre",
        retrieval_pass=2,
    )
    assert calls == [["precise millimetre", "unit", list(calls[0][2])]]
    assert set(rows["retrieval_query"]) == {"precise millimetre"}
    assert set(rows["retrieval_pass"]) == {2}
    assert set(rows["search_query"]) == {"millimetre"}


# --- the merge: R's rule, on shared inputs -----------------------------------


R_MERGE = _load(R_MERGE_PATH, {"cases": [], "provenance": {}})


def _frame(rows, columns):
    if not rows:
        return pd.DataFrame(columns=columns)
    frame = pd.DataFrame(rows)
    return frame[[column for column in columns if column in frame.columns]]


def _merge_cases():
    return [pytest.param(case, id=case["id"]) for case in R_MERGE["cases"]]


@pytest.mark.parametrize("case", _merge_cases())
def test_merge_gives_the_rows_r_gives(case):
    from metasalmonpy.semantics import _merge_semantic_target_candidates

    existing = _frame(case["existing"], case["existing_columns"])
    extra = _frame(case["extra"], case["extra_columns"])
    merged = _merge_semantic_target_candidates(existing, extra, case["max_per_role"])
    got = _records(merged)
    assert got["columns"] == case["merged"]["columns"]
    assert got["rows"] == case["merged"]["rows"]


@pytest.mark.parametrize("case", _merge_cases())
def test_merge_identities_and_gain_match_r(case):
    from metasalmonpy.semantics import (
        _merge_semantic_target_candidates,
        _semantic_candidate_identity,
    )

    existing = _frame(case["existing"], case["existing_columns"])
    extra = _frame(case["extra"], case["extra_columns"])
    merged = _merge_semantic_target_candidates(existing, extra, case["max_per_role"])
    assert list(_semantic_candidate_identity(merged)) == case["merged_identity"]
    assert list(_semantic_candidate_identity(existing)) == case["existing_identity"]
    existing_ids = set(_semantic_candidate_identity(existing))
    gain = sum(1 for identity in _semantic_candidate_identity(merged) if identity not in existing_ids)
    assert gain == case["gain"]


def test_r_fixture_provenance_is_recorded():
    provenance = R_MERGE["provenance"]
    assert provenance["metasalmon_commit"] == "98cb9e6"
    assert provenance["r_version"].startswith("R version 4.5.2")


# --- the second pass through the retriever: R's rule, on shared inputs -------
#
# Pass 1 keeps today's rule on three points the item pins (the (source, iri)
# key, a missing score filled with 0, the cap as given); pass 2 takes R's,
# because the second pass is what B-363 converges. The retriever's docstring
# carries the retirement condition. ``r-retrieve-candidates.json`` holds what
# R gives at pass 2 for the inputs in ``retrieve-candidates-cases.json``.

R_RETRIEVE_PATH = DATA / "r-retrieve-candidates.json"
R_RETRIEVE = _load(R_RETRIEVE_PATH, {"cases": [], "provenance": {}})


def _retrieve_cases():
    return [pytest.param(case, id=case["id"]) for case in R_RETRIEVE["cases"]]


def _same_cell(got, want) -> bool:
    if isinstance(got, float) and isinstance(want, (int, float)) and not isinstance(want, bool):
        return math.isclose(got, float(want), rel_tol=1e-12, abs_tol=0.0)
    return got == want


def _same_row(got: dict, want: dict) -> bool:
    return set(got) == set(want) and all(_same_cell(got[key], want[key]) for key in want)


def _retrieve_at(case, retrieval_pass):
    from metasalmonpy.llm_review import make_source_policy
    from metasalmonpy.semantics import _retrieve_semantic_target_candidates

    results = _frame(case["results"], case["result_columns"])
    calls = []

    def search(query, role=None, sources=None):
        calls.append({"query": query, "role": role})
        return results.copy()

    rows = _retrieve_semantic_target_candidates(
        dict(case["target"]),
        make_source_policy(case["sources"]),
        case["max_per_role"],
        search,
        query=case["query"],
        retrieval_pass=retrieval_pass,
    )
    return rows, calls


@pytest.mark.parametrize("case", _retrieve_cases())
def test_second_pass_retrieval_gives_the_rows_r_gives(case):
    rows, calls = _retrieve_at(case, retrieval_pass=2)
    assert calls == case["search_calls"]
    got = _records(rows)
    assert set(got["columns"]) == set(case["retrieved"]["columns"])
    assert len(got["rows"]) == len(case["retrieved"]["rows"])
    for position, (have, want) in enumerate(zip(got["rows"], case["retrieved"]["rows"])):
        assert _same_row(have, want), f"row {position}: {have} != {want}"


def test_r_retrieve_fixture_provenance_is_recorded():
    assert R_RETRIEVE_PATH.is_file(), R_RETRIEVE_PATH
    provenance = R_RETRIEVE["provenance"]
    assert provenance["metasalmon_commit"] == "98cb9e6"
    assert provenance["r_version"].startswith("R version 4.5.2")


def test_pass_one_keeps_todays_rule_where_pass_two_takes_rs():
    # The same shortlist through both passes. Pass 1 is today's behaviour,
    # pinned by the item; pass 2 is R's. The branch retires with pass-1
    # convergence (see the retriever's docstring).
    case = next(case for case in R_RETRIEVE["cases"] if case["id"] == "iri-less-and-missing-score")
    pass_one, _ = _retrieve_at(case, retrieval_pass=1)
    pass_two, _ = _retrieve_at(case, retrieval_pass=2)
    # (source, iri) collapses every IRI-less zooma row to one; identity keeps
    # the two that differ in match_type and drops the exact repeat.
    assert int(pass_one["iri"].isna().sum()) == 1
    assert int(pass_two["iri"].isna().sum()) == 2
    assert list(pass_two.loc[pass_two["iri"].isna(), "match_type"]) == ["label", "synonym"]
    # A missing score is filled with 0 (then the bonus) on pass 1 and left
    # missing, sorting last, on pass 2.
    assert pass_one["score"].notna().all()
    assert int(pass_two["score"].isna().sum()) == 1
    assert pd.isna(pass_two["score"].iloc[-1])
    assert pass_two["label"].iloc[-1] == "marine phase"
    assert set(pass_one["retrieval_pass"]) == {1}
    assert set(pass_two["retrieval_pass"]) == {2}


def test_a_zero_depth_keeps_nothing_on_pass_one_and_one_row_on_pass_two():
    case = next(case for case in R_RETRIEVE["cases"] if case["id"] == "cap-zero-keeps-one")
    pass_one, _ = _retrieve_at(case, retrieval_pass=1)
    pass_two, _ = _retrieve_at(case, retrieval_pass=2)
    assert len(pass_one) == 0
    assert len(pass_two) == 1


def test_retry_candidates_keep_distinct_iri_less_alternatives():
    """The retry path sees every distinct IRI-less candidate, as it did before B-363."""
    from metasalmonpy.llm_review import _retry_candidates, make_source_policy

    case = next(case for case in R_RETRIEVE["cases"] if case["id"] == "iri-less-and-missing-score")
    results = _frame(case["results"], case["result_columns"])
    target = pd.Series(case["target"])
    rows = _retry_candidates(
        target,
        case["query"],
        lambda query, role=None, sources=None: results.copy(),
        make_source_policy(None),
        case["max_per_role"],
    )
    assert int(rows["iri"].isna().sum()) == 2
    assert set(rows["retrieval_pass"]) == {2}
    assert set(rows["retrieval_query"]) == {case["query"]}
    assert set(rows["search_query"]) == {case["target"]["search_query"]}


def test_retry_merge_counts_gain_the_way_r_does():
    """``_merge_retry_candidates()`` splices R's merge into the suggestions frame."""
    from metasalmonpy.llm_review import _merge_retry_candidates

    case = next(case for case in R_MERGE["cases"] if case["id"] == "basic-rescored-duplicate")
    existing = _frame(case["existing"], case["existing_columns"])
    extra = _frame(case["extra"], case["extra_columns"])
    other = existing.iloc[[0]].copy()
    other["column_name"] = "other_column"
    suggestions = pd.concat([other, existing], ignore_index=True)
    target = existing.iloc[0]
    merged, gain = _merge_retry_candidates(suggestions, extra, target, case["max_per_role"])
    assert gain == case["gain"]
    kept = merged.loc[merged["column_name"] == target["column_name"]]
    assert _records(kept.reset_index(drop=True))["rows"] == case["merged"]["rows"]
    assert (merged["column_name"] == "other_column").sum() == 1
