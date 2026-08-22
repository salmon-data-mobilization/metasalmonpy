import json

import pandas as pd

from metasalmonpy import infer_dictionary, suggest_semantics
from metasalmonpy.llm_review import (
    LLM_ASSESSMENT_COLUMNS,
    load_context_chunks,
    normalize_assessment_rows,
)


def _measurement_dictionary(description=None):
    data = pd.DataFrame({"fork_length": [55.0, 61.0]})
    dictionary = infer_dictionary(
        data,
        dataset_id="demo",
        table_id="fish",
    )
    dictionary.loc[0, "column_role"] = "measurement"
    dictionary.loc[0, "column_description"] = (
        description
        or "Fork length in millimetres measured using callipers for "
        "ocean-phase fish."
    )
    dictionary.loc[0, "unit_label"] = "millimetre"
    return data, dictionary


def _search_stub(calls=None):
    def search(query, role=None, sources=None):
        if calls is not None:
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


def _accept_bundle(messages, config):
    payload = json.loads(messages[-1]["content"])
    slots = []
    for slot in payload["slots"]:
        # Only reviewed slots carry candidates and expect an answer. Since
        # chunk A removed the dictionary method_iri field, the bundle's method
        # slot arrives as already_filled_or_not_requested with no candidates
        # (chunk B retargets that slot to statistical_modifier).
        if slot.get("status") != "review" or not slot.get("candidates"):
            continue
        candidate = slot["candidates"][0]
        slots.append(
            {
                "role": slot["role"],
                "decision": "accept",
                "confidence": 0.91,
                "selected_candidate_id": candidate["candidate_id"],
                "rationale": f"Accepted {slot['role']}.",
            }
        )
    return {"bundle_summary": "Coherent measurement bundle.", "slots": slots}


def test_bundle_review_returns_stable_thirty_column_assessments():
    data, dictionary = _measurement_dictionary()

    result = suggest_semantics(
        data,
        dictionary,
        search_fn=_search_stub(),
        llm_assess=True,
        llm_request_fn=_accept_bundle,
    )

    assessments = result.attrs["semantic_llm_assessments"]
    suggestions = result.attrs["semantic_suggestions"]
    assert list(assessments.columns) == LLM_ASSESSMENT_COLUMNS
    # Five column slots since chunk A removed the dictionary method_iri field;
    # the sixth becomes statistical_modifier when chunk B retargets the
    # semantic pipeline (S10 execplan, chunk B).
    assert len(assessments) == 5

    # metasalmon v0.1.7 treats this fixture as a count-like measurement: the
    # description carries the organism token "fish" and the column is numeric,
    # so `is_count_like_measurement()` fires and both the variable and property
    # queries become "count". Driving era R's suggest_semantics() with the same
    # dictionary and a stub search_fn returns exactly that pair of queries. The
    # deterministic dimension validator then does its job -- a "count" property
    # against a "millimetre" unit is a mismatch -- and downgrades those two
    # slots to review. Both facts are parity, not a defect here.
    decisions = dict(zip(assessments["dictionary_role"], assessments["llm_decision"]))
    assert decisions == {
        "variable": "accept",
        "property": "review",
        "entity": "accept",
        "unit": "review",
        "constraint": "accept",
    }
    assert suggestions["llm_selected"].sum() == 3


def test_no_review_targets_returns_empty_schema_without_provider_call():
    data = pd.DataFrame({"sample_id": ["a", "b"]})
    dictionary = infer_dictionary(
        data,
        dataset_id="demo",
        table_id="samples",
    )
    calls = []

    def forbidden_request(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("No provider call is expected.")

    result = suggest_semantics(
        data,
        dictionary,
        search_fn=_search_stub(),
        llm_assess=True,
        llm_request_fn=forbidden_request,
    )

    assessments = result.attrs["semantic_llm_assessments"]
    assert calls == []
    assert assessments.empty
    assert list(assessments.columns) == LLM_ASSESSMENT_COLUMNS


def test_partially_filled_measurement_still_uses_one_six_slot_bundle():
    data, dictionary = _measurement_dictionary()
    dictionary.loc[0, "unit_iri"] = "http://qudt.org/vocab/unit/MilliM"
    request_count = 0

    def request(messages, config):
        nonlocal request_count
        request_count += 1
        payload = json.loads(messages[-1]["content"])
        assert len(payload["slots"]) == 6
        unit = next(
            slot for slot in payload["slots"] if slot["role"] == "unit"
        )
        assert unit["status"] == "already_filled_or_not_requested"
        assert unit["current_value"] == "http://qudt.org/vocab/unit/MilliM"
        assert payload["current_slots"]["unit"] == unit["current_value"]
        return {
            "bundle_summary": "Retain the existing unit.",
            "slots": [
                {
                    "role": slot["role"],
                    "decision": "review",
                    "confidence": 0.5,
                    "rationale": "Review this unfilled slot.",
                }
                for slot in payload["slots"]
                if slot["status"] == "review"
            ],
        }

    result = suggest_semantics(
        data,
        dictionary,
        search_fn=_search_stub(),
        llm_assess=True,
        llm_request_fn=request,
    )

    assessments = result.attrs["semantic_llm_assessments"]
    assert request_count == 1
    assert len(assessments) == 4
    assert "unit" not in set(assessments["dictionary_role"])


def test_reject_shortlist_escalates_to_a_structured_term_request():
    data, dictionary = _measurement_dictionary()

    def request(messages, config):
        payload = json.loads(messages[-1]["content"])
        return {
            "bundle_summary": "Variable shortlist is inadequate.",
            "slots": [
                {
                    "role": slot["role"],
                    "decision": (
                        "reject_shortlist"
                        if slot["role"] == "variable"
                        else "review"
                    ),
                    "confidence": 0.7,
                    "rationale": "No precise whole-variable candidate.",
                }
                for slot in payload["slots"]
                if slot["status"] == "review"
            ],
        }

    result = suggest_semantics(
        data,
        dictionary,
        search_fn=_search_stub(),
        llm_assess=True,
        llm_request_fn=request,
    )

    variable = result.attrs["semantic_llm_assessments"].loc[
        lambda frame: frame["dictionary_role"] == "variable"
    ].iloc[0]
    assert variable["llm_decision"] == "request_new_term"
    assert variable["llm_escalated_from"] == "reject_shortlist"
    assert "No precise whole-variable candidate" in variable["llm_rationale"]


def test_malformed_bundle_response_falls_back_per_target():
    data, dictionary = _measurement_dictionary()
    request_count = 0

    def request(messages, config):
        nonlocal request_count
        request_count += 1
        if request_count == 1:
            return {"unexpected": True}
        return {
            "decision": "review",
            "confidence": 0.5,
            "rationale": "Per-target fallback.",
        }

    result = suggest_semantics(
        data,
        dictionary,
        search_fn=_search_stub(),
        llm_assess=True,
        llm_request_fn=request,
    )

    assessments = result.attrs["semantic_llm_assessments"]
    assert request_count == 6
    assert set(assessments["llm_decision"]) == {"review"}
    assert assessments["llm_error"].isna().all()


def test_explicit_sources_remain_a_strict_allowlist_during_retry():
    data, dictionary = _measurement_dictionary()
    search_calls = []
    request_count = 0

    def request(messages, config):
        nonlocal request_count
        request_count += 1
        payload = json.loads(messages[-1]["content"])
        slots = []
        for slot in payload["slots"]:
            if slot["role"] == "unit" and request_count == 1:
                slots.append(
                    {
                        "role": "unit",
                        "decision": "retry_search",
                        "confidence": 0.4,
                        "retry_query": "millimetre fish length unit",
                        "rationale": "Try a more precise unit query.",
                    }
                )
            else:
                slots.append(
                    {
                        "role": slot["role"],
                        "decision": "review",
                        "confidence": 0.5,
                        "rationale": "Retain for review.",
                    }
                )
        return {"bundle_summary": "Review bundle.", "slots": slots}

    suggest_semantics(
        data,
        dictionary,
        sources=["smn"],
        search_fn=_search_stub(search_calls),
        llm_assess=True,
        llm_request_fn=request,
    )

    assert request_count == 2
    assert search_calls
    assert all(sources == ("smn",) for _, _, sources in search_calls)


def test_duplicate_retry_query_is_recorded_without_second_request_or_search():
    data, dictionary = _measurement_dictionary()
    search_calls = []
    request_count = 0

    def request(messages, config):
        nonlocal request_count
        request_count += 1
        payload = json.loads(messages[-1]["content"])
        slots = []
        for slot in payload["slots"]:
            if slot["role"] == "unit":
                slots.append(
                    {
                        "role": "unit",
                        "decision": "retry_search",
                        "confidence": 0.3,
                        "retry_query": f"  {slot['search_query'].upper()}  ",
                        "rationale": "Retry the same query.",
                    }
                )
            else:
                slots.append(
                    {
                        "role": slot["role"],
                        "decision": "review",
                        "confidence": 0.5,
                        "rationale": "Review.",
                    }
                )
        return {"bundle_summary": "Review bundle.", "slots": slots}

    result = suggest_semantics(
        data,
        dictionary,
        search_fn=_search_stub(search_calls),
        llm_assess=True,
        llm_request_fn=request,
    )

    assessments = result.attrs["semantic_llm_assessments"]
    unit = assessments.loc[assessments["dictionary_role"] == "unit"].iloc[0]
    assert request_count == 1
    assert len(search_calls) == 5
    assert unit["llm_decision"] == "retry_search"
    assert (
        unit["llm_retry_query_rejection_reason"]
        == "duplicate_original_query"
    )


def test_generic_duplicate_retry_is_suppressed_too():
    data = pd.DataFrame({"life_stage": ["adult", "juvenile"]})
    dictionary = infer_dictionary(
        data,
        dataset_id="demo",
        table_id="fish",
    )
    dictionary.loc[0, "column_role"] = "categorical"
    dictionary.loc[0, "column_description"] = "Salmon life stage."
    request_count = 0
    search_calls = []

    def request(messages, config):
        nonlocal request_count
        request_count += 1
        payload = json.loads(messages[-1]["content"])
        return {
            "decision": "retry_search",
            "confidence": 0.4,
            "retry_query": f"  {payload['target']['search_query'].upper()}  ",
            "rationale": "Retry.",
        }

    result = suggest_semantics(
        data,
        dictionary,
        search_fn=_search_stub(search_calls),
        llm_assess=True,
        llm_request_fn=request,
    )

    assessment = result.attrs["semantic_llm_assessments"].iloc[0]
    assert request_count == 1
    assert len(search_calls) == 1
    assert (
        assessment["llm_retry_query_rejection_reason"]
        == "duplicate_original_query"
    )


def test_identifier_like_retry_uses_plain_language_query_fallback():
    data, dictionary = _measurement_dictionary()
    search_calls = []
    request_count = 0

    def request(messages, config):
        nonlocal request_count
        request_count += 1
        if "alternate_queries" in messages[0]["content"]:
            return {"alternate_queries": ["fork length measurement unit"]}
        payload = json.loads(messages[-1]["content"])
        slots = []
        for slot in payload["slots"]:
            slots.append(
                {
                    "role": slot["role"],
                    "decision": (
                        "retry_search"
                        if slot["role"] == "unit" and request_count == 1
                        else "review"
                    ),
                    "confidence": 0.5,
                    "retry_query": (
                        "http://qudt.org/vocab/unit/MilliM"
                        if slot["role"] == "unit" and request_count == 1
                        else None
                    ),
                    "rationale": "Review the slot.",
                }
            )
        return {"bundle_summary": "Review bundle.", "slots": slots}

    result = suggest_semantics(
        data,
        dictionary,
        search_fn=_search_stub(search_calls),
        llm_assess=True,
        llm_request_fn=request,
    )

    assessments = result.attrs["semantic_llm_assessments"]
    unit = assessments.loc[assessments["dictionary_role"] == "unit"].iloc[0]
    assert request_count == 3
    assert any(
        query == "fork length measurement unit"
        for query, role, sources in search_calls
        if role == "unit"
    )
    assert unit["llm_exploration_used"]
    assert unit["llm_exploration_queries"] == "fork length measurement unit"
    assert pd.isna(unit["llm_retry_query_rejection_reason"])


def test_retry_reassesses_when_new_evidence_replaces_a_capped_shortlist():
    data, dictionary = _measurement_dictionary()
    request_count = 0

    def search(query, role=None, sources=None):
        precise = "precise" in str(query)
        return pd.DataFrame(
            {
                "label": [f"{'precise' if precise else 'broad'} {role}"],
                "iri": [f"https://example.org/{role}/{'new' if precise else 'old'}"],
                "source": ["smn"],
                "ontology": ["test"],
                "role": [role],
                "role_hints": [role],
                "match_type": ["label"],
                "definition": [f"A {role} candidate."],
                "score": [2.0 if precise else 1.0],
            }
        )

    def request(messages, config):
        nonlocal request_count
        request_count += 1
        payload = json.loads(messages[-1]["content"])
        slots = []
        for slot in payload["slots"]:
            slots.append(
                {
                    "role": slot["role"],
                    "decision": (
                        "retry_search"
                        if slot["role"] == "unit" and request_count == 1
                        else "review"
                    ),
                    "confidence": 0.5,
                    "retry_query": (
                        "precise millimetre unit"
                        if slot["role"] == "unit" and request_count == 1
                        else None
                    ),
                    "rationale": "Review.",
                }
            )
        return {"bundle_summary": "Review bundle.", "slots": slots}

    result = suggest_semantics(
        data,
        dictionary,
        search_fn=search,
        max_per_role=1,
        llm_top_n=1,
        llm_assess=True,
        llm_request_fn=request,
    )

    unit = result.attrs["semantic_llm_assessments"].loc[
        lambda frame: frame["dictionary_role"] == "unit"
    ].iloc[0]
    assert request_count == 2
    assert unit["llm_exploration_candidate_gain"] == 1


def test_provider_failure_preserves_deterministic_suggestions():
    data, dictionary = _measurement_dictionary()

    def failing_request(messages, config):
        raise RuntimeError("provider unavailable")

    result = suggest_semantics(
        data,
        dictionary,
        search_fn=_search_stub(),
        llm_assess=True,
        llm_request_fn=failing_request,
    )

    suggestions = result.attrs["semantic_suggestions"]
    assessments = result.attrs["semantic_llm_assessments"]
    assert len(suggestions) == 5
    assert assessments["llm_error"].str.contains(
        "provider unavailable",
        na=False,
    ).all()


def test_generated_method_words_do_not_count_as_procedure_evidence():
    # sdp-0.3.0 removed the dictionary method_iri slot, so no column-level
    # method target exists any more — but the code-level method role survives
    # (codes still resolve to shared sosa:Procedure concepts) and this
    # validator with it. Covered at unit level exactly as metasalmon main's
    # test-semantic-bundle-validators.R covers .ms_validate_semantic_method_evidence.
    from metasalmonpy.llm_review import _has_method_evidence

    assert not _has_method_evidence(
        "Catch count does not identify a measurement procedure."
    )
    # Role-shaped generated text ("method slot", "procedure") is not evidence.
    assert not _has_method_evidence(
        "Catch enumeration procedure. A method slot generated for this "
        "column. Catch count; no procedure supplied."
    )


def test_field_anchored_context_can_supply_method_evidence():
    # The positive half of the surviving validator: explicit field, gear, or
    # instrument language is procedure evidence. (The column-level route that
    # used to exercise this through llm_context_text died with the dictionary
    # method_iri slot; the code-level method role keeps the validator alive.)
    from metasalmonpy.llm_review import _has_method_evidence

    assert _has_method_evidence(
        "A technician measured fork length in the field with a measuring "
        "board from the snout tip to the caudal-fin fork."
    )
    assert _has_method_evidence(
        "fork_length was measured in the field using a measuring board."
    )


def test_native_role_hint_mismatch_downgrades_accept():
    data, dictionary = _measurement_dictionary()

    def mismatched_search(query, role=None, sources=None):
        result = _search_stub()(query, role=role, sources=sources)
        if role == "variable":
            result["role_hints"] = "property"
        return result

    result = suggest_semantics(
        data,
        dictionary,
        search_fn=mismatched_search,
        llm_assess=True,
        llm_request_fn=_accept_bundle,
    )

    assessments = result.attrs["semantic_llm_assessments"]
    variable = assessments.loc[
        assessments["dictionary_role"] == "variable"
    ].iloc[0]
    assert variable["llm_decision"] == "review"
    assert "SEM_ROLE_TYPE_MISMATCH" in variable["llm_rationale"]


def test_legacy_assessments_normalize_additively_to_thirty_columns():
    legacy = pd.DataFrame(
        [
            {
                column: pd.NA
                for column in LLM_ASSESSMENT_COLUMNS[:-2]
            }
        ]
    )

    normalized = normalize_assessment_rows(legacy)

    assert list(normalized.columns) == LLM_ASSESSMENT_COLUMNS
    assert pd.isna(normalized.loc[0, "llm_escalated_from"])
    assert pd.isna(
        normalized.loc[0, "llm_retry_query_rejection_reason"]
    )


def test_context_decodes_cp1252_and_disambiguates_duplicate_basenames(tmp_path):
    first = tmp_path / "first" / "dictionary.csv"
    second = tmp_path / "second" / "dictionary.csv"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes("field,description\ncount,caf\u00e9\n".encode("cp1252"))
    second.write_text(
        "field,description\nlength,measuring board\n",
        encoding="utf-8",
    )

    chunks = load_context_chunks([first, second])

    assert "caf\u00e9" in " ".join(chunks["text"])
    assert chunks["source"].nunique() == 2
    assert set(chunks["source"]) == {
        "dictionary.csv",
        "dictionary.csv [2]",
    }
