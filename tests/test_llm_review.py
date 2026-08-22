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
        # chunk B retargeted the pipeline, the sixth dictionary slot is
        # statistical_modifier — reviewed only when the column text names an
        # aggregation — and `method` survives as a bundle role with no
        # dictionary field (codes-scope searches only), always arriving
        # already_filled_or_not_requested for column bundles.
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
    # Five reviewed column slots: the sixth dictionary slot is
    # statistical_modifier (chunk B), but a modifier target is emitted only
    # when the column text names an aggregation, which "fork length" does not
    # — so its slot arrives already_filled_or_not_requested and is never
    # assessed. See test_aggregation_measurement_reviews_the_modifier_slot for
    # the six-slot case.
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


def test_partially_filled_measurement_still_uses_one_seven_slot_bundle():
    data, dictionary = _measurement_dictionary()
    dictionary.loc[0, "unit_iri"] = "http://qudt.org/vocab/unit/MilliM"
    request_count = 0
    payloads = []

    def request(messages, config):
        nonlocal request_count
        request_count += 1
        # Captured, NOT asserted here: an exception raised inside the injected
        # request_fn is swallowed by the bundle path's provider-failure
        # handling, so an in-request assert can pass vacuously when the
        # payload drifts. (The pre-chunk-B six-slot assert did exactly that.)
        payloads.append(json.loads(messages[-1]["content"]))
        return {
            "bundle_summary": "Retain the existing unit.",
            "slots": [
                {
                    "role": slot["role"],
                    "decision": "review",
                    "confidence": 0.5,
                    "rationale": "Review this unfilled slot.",
                }
                for slot in payloads[-1]["slots"]
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
    payload = payloads[0]
    # The bundle names every role: the six dictionary slots plus `method`,
    # which has no dictionary field left (sdp-0.3.0) and survives for
    # codes-scope searches only — mirroring .ms_semantic_bundle_roles().
    assert [slot["role"] for slot in payload["slots"]] == [
        "variable",
        "property",
        "entity",
        "unit",
        "constraint",
        "statistical_modifier",
        "method",
    ]
    unit = next(slot for slot in payload["slots"] if slot["role"] == "unit")
    assert unit["status"] == "already_filled_or_not_requested"
    assert unit["current_value"] == "http://qudt.org/vocab/unit/MilliM"
    assert payload["current_slots"]["unit"] == unit["current_value"]
    # No aggregation in the column text, so the modifier slot is unrequested.
    modifier = next(
        slot
        for slot in payload["slots"]
        if slot["role"] == "statistical_modifier"
    )
    assert modifier["status"] == "already_filled_or_not_requested"
    assert modifier["target_sdp_field"] == "statistical_modifier_iri"
    method = next(
        slot for slot in payload["slots"] if slot["role"] == "method"
    )
    assert method["status"] == "already_filled_or_not_requested"
    assert method["target_sdp_field"] is None
    assert "method" not in payload["current_slots"]
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


def test_aggregation_measurement_reviews_the_modifier_slot():
    # Mirror of metasalmon's "bundle validators downgrade unsupported
    # acceptances only" (test-semantic-bundle-validators.R at main): the
    # column text names an aggregation ("Total"), so the bundle carries the
    # five base roles plus statistical_modifier. The constraint acceptance
    # lacks qualifier evidence and is downgraded; every other acceptance —
    # the modifier's included — is retained.
    data = pd.DataFrame({"spawner_count": [10, 12]})
    dictionary = infer_dictionary(data, dataset_id="demo", table_id="fish")
    dictionary.loc[0, "column_role"] = "measurement"
    dictionary.loc[0, "column_description"] = (
        "Total fish observed in a trawl catch."
    )
    dictionary.loc[0, "unit_label"] = "count"

    labels = {
        "variable": "Catch abundance",
        "property": "Count",
        "entity": "Fish",
        "unit": "Count",
        "constraint": "Catch context",
        "statistical_modifier": "Total value",
    }

    def search(query, role=None, sources=None):
        return pd.DataFrame(
            {
                "label": [labels[role]],
                "iri": [f"https://example.org/{role}"],
                "source": ["smn"],
                "ontology": ["demo"],
                "role": [role],
                "role_hints": [role],
                "match_type": ["label"],
                "definition": [f"A {role} candidate"],
                "score": [0.9],
            }
        )

    result = suggest_semantics(
        data,
        dictionary,
        sources=["smn"],
        max_per_role=1,
        search_fn=search,
        llm_assess=True,
        llm_request_fn=_accept_bundle,
    )

    assessments = result.attrs["semantic_llm_assessments"]
    assert set(assessments["dictionary_role"]) == {
        "variable",
        "property",
        "entity",
        "unit",
        "constraint",
        "statistical_modifier",
    }
    modifier = assessments.loc[
        assessments["dictionary_role"] == "statistical_modifier"
    ].iloc[0]
    assert modifier["llm_decision"] == "accept"
    assert modifier["llm_selected_iri"] == (
        "https://example.org/statistical_modifier"
    )
    assert modifier["target_sdp_field"] == "statistical_modifier_iri"
    assert modifier["search_query"] == "total"
    constraint = assessments.loc[
        assessments["dictionary_role"] == "constraint"
    ].iloc[0]
    assert constraint["llm_decision"] == "review"
    assert "SEM_CONSTRAINT_EVIDENCE_REQUIRED" in constraint["llm_rationale"]


def test_modifier_evidence_predicate_splits_underscores():
    # Mirror of .ms_semantic_validator_has_modifier_evidence: underscores are
    # not \b word boundaries, so `mean_weight` needs splitting before the
    # aggregation words can match.
    from metasalmonpy.llm_review import _has_modifier_evidence

    assert _has_modifier_evidence("Mean water temperature by site")
    assert _has_modifier_evidence("mean_weight of sampled fish")
    assert not _has_modifier_evidence("Water temperature in degrees C")


def test_modifier_accept_without_aggregation_evidence_downgrades():
    # SEM_MODIFIER_EVIDENCE_REQUIRED sits BESIDE the surviving method-evidence
    # validator: an accepted statistical_modifier whose column text names no
    # aggregation silently changes what the variable means, so the accept is
    # downgraded to review. Driven at the validator level, as metasalmon's
    # test-semantic-bundle-validators.R drives
    # .ms_validate_semantic_modifier_evidence, because the discovery path
    # never emits an evidence-free modifier target on its own.
    from metasalmonpy.llm_review import _apply_validators

    target = {
        "dataset_id": "demo",
        "table_id": "fish",
        "column_name": "water_temp",
        "code_value": pd.NA,
        "dictionary_role": "statistical_modifier",
        "target_scope": "column",
        "target_sdp_file": "column_dictionary.csv",
        "target_sdp_field": "statistical_modifier_iri",
        "search_query": "statistical modifier",
        "target_query_context": "Water temperature in degrees C",
        "column_label": "Water temperature",
        "column_description": "Water temperature in degrees C",
    }
    targets = pd.DataFrame([target])
    suggestions = pd.DataFrame(
        [
            {
                **{
                    key: target[key]
                    for key in (
                        "dataset_id",
                        "table_id",
                        "column_name",
                        "code_value",
                        "dictionary_role",
                        "target_scope",
                        "target_sdp_file",
                        "target_sdp_field",
                        "search_query",
                    )
                },
                "label": "Mean",
                "iri": "https://w3id.org/smn/MeanStatisticalModifier",
                "source": "smn",
                "ontology": "smn",
                "role": "statistical_modifier",
                "role_hints": "statistical_modifier",
                "match_type": "label",
                "definition": "The arithmetic mean of the observed values.",
                "score": 0.9,
            }
        ]
    )
    row = {
        **{key: target.get(key) for key in target},
        "llm_decision": "accept",
        "llm_selected_candidate_index": 1,
        "llm_selected_iri": "https://w3id.org/smn/MeanStatisticalModifier",
        "llm_selected_label": "Mean",
        "llm_rationale": "Accepted.",
    }

    empty_dictionary = pd.DataFrame(
        columns=["dataset_id", "table_id", "column_name"]
    )
    rows, findings = _apply_validators(
        [row],
        targets,
        empty_dictionary,
        suggestions,
        pd.DataFrame(columns=["source", "chunk_id", "text"]),
    )

    assert rows[0]["llm_decision"] == "review"
    assert "SEM_MODIFIER_EVIDENCE_REQUIRED" in rows[0]["llm_rationale"]
    assert [finding["code"] for finding in findings] == [
        "SEM_MODIFIER_EVIDENCE_REQUIRED"
    ]
    # Other roles are untouched by the modifier validator: the same evidence
    # under the constraint role trips only the constraint validator.
    assert not any(
        finding["code"] == "SEM_MODIFIER_EVIDENCE_REQUIRED"
        for finding in _apply_validators(
            [
                {
                    **row,
                    "dictionary_role": "constraint",
                    "llm_decision": "accept",
                }
            ],
            pd.DataFrame([{**target, "dictionary_role": "constraint"}]),
            empty_dictionary,
            suggestions.assign(
                dictionary_role="constraint",
                role_hints="constraint",
            ),
            pd.DataFrame(columns=["source", "chunk_id", "text"]),
        )[1]
    )


def test_smn_modifier_candidate_is_not_vetoed_by_role_hints():
    # Regression mirror (metasalmon main): modifier concepts live in smn's
    # controlled-vocabularies module, so they used to reach review carrying
    # only a "constraint" hint and the role-type validator downgraded every
    # correct accept — the exact silent-hint-layer failure the role contract
    # names.
    from metasalmonpy.llm_review import _role_type_message
    from metasalmonpy.term_search_smn import _smn_role_flags

    flags = _smn_role_flags(
        label="Mean",
        definition="The arithmetic mean of the observed values.",
        resource_kind="Concept",
        module_name="07-controlled-vocabularies",
        in_scheme="https://w3id.org/smn/StatisticalModifierScheme",
        parent_iris="",
        type_iris="http://w3id.org/iadopt/ont/StatisticalModifier",
        iri="https://w3id.org/smn/MeanStatisticalModifier",
    )
    assert flags["is_statistical_modifier"] is True

    message = _role_type_message(
        "statistical_modifier",
        {
            "iri": "https://w3id.org/smn/MeanStatisticalModifier",
            "label": "Mean",
            "role_hints": "constraint|statistical_modifier",
        },
    )
    assert message is None


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
