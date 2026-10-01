"""The direct LLM path must retain enough retrieved candidates for llm_top_n.

Every dictionary wrapper already widens its first retrieval pass. This pins
the same rule at the public ``suggest_semantics()`` entry point (hub B-302).
"""

import json

import pandas as pd

from metasalmonpy import infer_dictionary, suggest_semantics


def _measurement_dictionary():
    data = pd.DataFrame({"spawner_count": [120, 340]})
    dictionary = infer_dictionary(data, dataset_id="d1", table_id="t1")
    dictionary.loc[0, "column_role"] = "measurement"
    return data, dictionary


def _eight_candidate_search(query, role=None, sources=None):
    return pd.DataFrame(
        {
            "label": [f"{role} candidate {index}" for index in range(1, 9)],
            "iri": [f"https://example.org/{role}/c{index}" for index in range(1, 9)],
            "source": ["smn"] * 8,
            "ontology": ["demo"] * 8,
            "role": [role] * 8,
            "role_hints": [role] * 8,
            "match_type": ["label_partial"] * 8,
            "definition": [f"Candidate {index} for {role}" for index in range(1, 9)],
            "score": [index / 10 for index in range(9, 1, -1)],
        }
    )


def _variable_shortlist_recorder():
    seen = []

    def request(messages, config):
        payload = json.loads(messages[-1]["content"])
        variable = next(slot for slot in payload["slots"] if slot["role"] == "variable")
        seen.append([candidate["iri"] for candidate in variable["candidates"]])
        return {
            "bundle_summary": "The shortlist needs human review.",
            "slots": [
                {
                    "role": slot["role"],
                    "decision": "review",
                    "confidence": 0.2,
                    "rationale": "Shortlist-width stub.",
                }
                for slot in payload["slots"]
                if slot["status"] == "review"
            ],
        }

    return request, seen


def _kept_per_role(result):
    suggestions = result.attrs["semantic_suggestions"]
    return suggestions.groupby("dictionary_role").size().to_dict()


def test_llm_top_n_widens_direct_suggest_semantics_first_pass():
    data, dictionary = _measurement_dictionary()
    request, seen = _variable_shortlist_recorder()

    result = suggest_semantics(
        data,
        dictionary,
        sources="smn",
        search_fn=_eight_candidate_search,
        llm_assess=True,
        llm_request_fn=request,
    )

    # Defaults are max_per_role=3 and llm_top_n=5. The first request must
    # actually see five; a later retry cannot recover a truncated first pass.
    assert set(_kept_per_role(result).values()) == {5}
    assert seen[0] == [f"https://example.org/variable/c{index}" for index in range(1, 6)]


def test_larger_max_per_role_still_controls_retained_suggestions():
    data, dictionary = _measurement_dictionary()
    request, seen = _variable_shortlist_recorder()

    result = suggest_semantics(
        data,
        dictionary,
        sources="smn",
        search_fn=_eight_candidate_search,
        max_per_role=6,
        llm_top_n=2,
        llm_assess=True,
        llm_request_fn=request,
    )

    assert set(_kept_per_role(result).values()) == {6}
    assert seen[0] == ["https://example.org/variable/c1", "https://example.org/variable/c2"]


def test_without_llm_assess_retrieval_stays_at_max_per_role():
    data, dictionary = _measurement_dictionary()

    result = suggest_semantics(
        data,
        dictionary,
        sources="smn",
        search_fn=_eight_candidate_search,
    )

    assert set(_kept_per_role(result).values()) == {3}


def test_non_bundle_target_shows_only_llm_top_n_without_dropping_suggestions():
    table_meta = pd.DataFrame(
        {
            "dataset_id": ["d1"],
            "table_id": ["t1"],
            "file_name": ["counts.csv"],
            "table_label": ["Counts"],
            "description": ["Salmon counts"],
            "observation_unit": ["salmon population"],
            "observation_unit_iri": [pd.NA],
        }
    )
    seen = []

    def request(messages, config):
        payload = json.loads(messages[-1]["content"])
        seen.append([candidate["iri"] for candidate in payload["candidates"]])
        return {"decision": "review", "confidence": 0.2, "rationale": "Review."}

    result = suggest_semantics(
        None,
        pd.DataFrame(),
        table_meta=table_meta,
        sources="smn",
        search_fn=_eight_candidate_search,
        max_per_role=6,
        llm_top_n=2,
        llm_assess=True,
        llm_request_fn=request,
    )

    assert _kept_per_role(result) == {"entity": 6}
    assert seen[0] == ["https://example.org/entity/c1", "https://example.org/entity/c2"]
