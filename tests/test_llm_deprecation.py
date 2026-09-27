"""The deprecation of the in-package model call (hub item B-327, S16 step 1).

The twin of metasalmon's ``tests/testthat/test-llm-deprecation.R`` (B-326,
execplan section 6). The suite-wide quiet switch -- one warnings filter,
installed by ``tests/conftest.py`` -- keeps every other test as it was; these
tests switch the warning back on inside ``warnings.catch_warnings()`` with
``simplefilter("always")`` and assert: one warning per entry point, none on the
default path, one even with ``seed_semantics=False``, one from
``chat_decomposition()``, and that the opt-in warnings come first.

One difference from R is registered (PARITY.md row 65): R detects a supplied
``llm_*`` argument with ``missing()``, so ``llm_assess = FALSE`` passed
explicitly warns there; Python cannot tell an omitted argument from one passed
with its default value, so an argument warns here when it holds anything other
than its default.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path

import pandas as pd
import pytest

import metasalmonpy
from metasalmonpy import (
    LLMDeprecationWarning,
    chat_decomposition,
    create_sdp,
    infer_dictionary,
    infer_salmon_datapackage_artifacts,
    suggest_semantics,
)
from metasalmonpy.semantic_review_deprecation import LLM_ARGUMENT_NAMES, _llm_arguments_used


def _all_warnings(call):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        call()
    return list(caught)


def _deprecations(caught):
    return [warning for warning in caught if issubclass(warning.category, LLMDeprecationWarning)]


def _dictionary():
    data = pd.DataFrame({"fork_length": [55.0, 61.0]})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        dictionary = infer_dictionary(data, dataset_id="demo", table_id="fish")
    dictionary.loc[0, "column_role"] = "measurement"
    dictionary.loc[0, "column_description"] = "Fork length in millimetres measured using callipers."
    dictionary.loc[0, "unit_label"] = "millimetre"
    return data, dictionary


def _search(query, role=None, sources=None):
    return pd.DataFrame(
        {
            "label": [f"{role} candidate"],
            "iri": [f"https://example.org/{role}/candidate"],
            "source": ["smn"],
            "ontology": ["test"],
            "role": [role],
            "match_type": ["label"],
            "definition": [f"A {role} candidate."],
            "score": [1.0],
        }
    )


def _review_everything(messages, config):
    payload = json.loads(messages[-1]["content"])
    if "slots" in payload:
        return {
            "bundle_summary": "",
            "slots": [
                {"role": slot["role"], "decision": "review", "confidence": 0.4, "rationale": "Unsure."}
                for slot in payload["slots"]
                if slot["status"] == "review"
            ],
        }
    return {"decision": "review", "confidence": 0.4, "rationale": "Unsure."}


def test_the_warning_is_a_future_warning_that_names_the_release_and_the_replacement():
    assert issubclass(LLMDeprecationWarning, FutureWarning)
    assert "LLMDeprecationWarning" in metasalmonpy.__all__
    data, dictionary = _dictionary()
    caught = _all_warnings(
        lambda: suggest_semantics(data, dictionary, search_fn=_search, llm_assess=True, llm_request_fn=_review_everything)
    )
    deprecations = _deprecations(caught)
    assert len(deprecations) == 1
    message = str(deprecations[0].message)
    for fragment in ("suggest_semantics", "0.7.0", "write_semantic_review_packet", "ingest_semantic_assessments"):
        assert fragment in message
    # Attributed to the caller's line, not to the package.
    assert Path(deprecations[0].filename).name == Path(__file__).name


def test_context_supplied_without_llm_assess_still_warns_it_is_ignored_first_and_makes_no_call():
    data, dictionary = _dictionary()

    def forbidden(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("the model must not be called")

    caught = _all_warnings(
        lambda: suggest_semantics(data, dictionary, search_fn=_search, llm_context_text="Some context.", llm_request_fn=forbidden)
    )
    classes = [issubclass(warning.category, LLMDeprecationWarning) for warning in caught]
    assert sum(classes) == 1
    # The opt-in warning comes first; the deprecation warning is last.
    assert not classes[0]
    assert "ignored unless llm_assess=True" in str(caught[0].message)
    assert classes[-1]
    # The opt-in warning is still attributed to the caller, through the scope
    # the deprecation wraps the entry point in.
    assert Path(caught[0].filename).name == Path(__file__).name


def test_the_default_path_emits_no_deprecation_warning_from_any_entry_point(tmp_path):
    data, dictionary = _dictionary()
    assert not _deprecations(_all_warnings(lambda: suggest_semantics(data, dictionary, search_fn=_search)))
    frame = pd.DataFrame({"spawner_count": [1, 2, 3]})
    assert not _deprecations(_all_warnings(lambda: infer_dictionary(frame, seed_semantics=False)))
    assert not _deprecations(
        _all_warnings(lambda: infer_salmon_datapackage_artifacts({"t1": frame}, seed_semantics=False, seed_verbose=False))
    )
    assert not _deprecations(
        _all_warnings(
            lambda: create_sdp({"t1": frame}, path=tmp_path / "no-deprecation", seed_semantics=False, check_updates=False, overwrite=True)
        )
    )


def test_infer_dictionary_warns_once_even_when_seed_semantics_false_leaves_the_options_unused():
    frame = pd.DataFrame({"spawner_count": [1, 2, 3]})
    deprecations = _deprecations(_all_warnings(lambda: infer_dictionary(frame, seed_semantics=False, llm_assess=True)))
    assert len(deprecations) == 1
    assert "infer_dictionary" in str(deprecations[0].message)


def test_a_nested_call_chain_warns_exactly_once_naming_the_outermost_entry_point(tmp_path):
    frame = pd.DataFrame({"spawner_count": [1, 2, 3]})
    deprecations = _deprecations(
        _all_warnings(
            lambda: create_sdp(
                {"t1": frame}, path=tmp_path / "one-warning", seed_semantics=False, llm_top_n=3,
                check_updates=False, overwrite=True,
            )
        )
    )
    assert len(deprecations) == 1
    assert "create_sdp" in str(deprecations[0].message)

    deprecations = _deprecations(
        _all_warnings(
            lambda: infer_salmon_datapackage_artifacts({"t1": frame}, seed_semantics=False, seed_verbose=False, llm_timeout_seconds=30)
        )
    )
    assert len(deprecations) == 1
    assert "infer_salmon_datapackage_artifacts" in str(deprecations[0].message)


def test_a_seeded_nested_call_with_llm_assess_warns_once(tmp_path, monkeypatch):
    # create_sdp -> infer_salmon_datapackage_artifacts -> infer_dictionary and
    # suggest_semantics, every one handed the llm_* arguments. Retrieval is
    # stubbed, as the review console's tests stub it, so nothing leaves the
    # machine.
    import functools

    from metasalmonpy import semantics

    monkeypatch.setattr(semantics, "suggest_semantics", functools.partial(semantics.suggest_semantics, search_fn=_search))
    frame = pd.DataFrame({"fork_length_mm": [55.0, 61.0, 58.0]})
    deprecations = _deprecations(
        _all_warnings(
            lambda: create_sdp(
                {"t1": frame}, path=tmp_path / "seeded", seed_semantics=True, seed_verbose=False,
                llm_assess=True, llm_request_fn=_review_everything, check_updates=False, overwrite=True,
            )
        )
    )
    assert len(deprecations) == 1
    assert "create_sdp" in str(deprecations[0].message)


def test_chat_decomposition_warns_on_every_call(tmp_path):
    dictionary = pd.DataFrame(
        {
            "dataset_id": ["demo"], "table_id": ["fish"], "column_name": ["fork_length"],
            "column_label": ["Fork length"], "column_description": ["Fork length measured with callipers."],
            "column_role": ["measurement"], "value_type": ["number"], "required": [False],
            "unit_label": ["millimetre"], "unit_iri": [pd.NA], "term_iri": [pd.NA], "term_type": [pd.NA],
            "property_iri": [pd.NA], "entity_iri": [pd.NA], "constraint_iri": [pd.NA],
        }
    )
    suggestions = pd.DataFrame(
        {
            "dataset_id": ["demo"], "table_id": ["fish"], "column_name": ["fork_length"], "code_value": [pd.NA],
            "dictionary_role": ["variable"], "target_scope": ["column"], "target_sdp_file": ["column_dictionary.csv"],
            "target_sdp_field": ["term_iri"], "search_query": ["fork length"], "label": ["Fork length"],
            "iri": ["https://example.org/ForkLength"], "source": ["smn"], "ontology": ["smn"], "role": ["variable"],
            "match_type": ["label"], "definition": ["Length from snout to tail fork."],
        }
    )
    for attempt in range(2):
        deprecations = _deprecations(
            _all_warnings(
                lambda: chat_decomposition(
                    dictionary, column_name="fork_length", suggestions=suggestions, session_root=tmp_path / str(attempt),
                    commands=["/choose 1", "/approve"], output_fn=lambda message: None,
                )
            )
        )
        assert len(deprecations) == 1
        assert "chat_decomposition" in str(deprecations[0].message)


def test_the_warnings_filter_silences_the_deprecation():
    frame = pd.DataFrame({"spawner_count": [1, 2, 3]})
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        warnings.filterwarnings("ignore", category=metasalmonpy.LLMDeprecationWarning)
        infer_dictionary(frame, seed_semantics=False, llm_assess=True)
    assert not _deprecations(caught)


def test_the_trigger_reads_every_llm_argument_as_a_non_default_value():
    import inspect

    def entry(llm_assess=False, llm_provider="openai", llm_model=None, llm_api_key=None, llm_base_url=None,
              llm_reasoning_effort=None, llm_top_n=5, llm_context_files=None, llm_context_text=None,
              llm_timeout_seconds=60, llm_request_fn=None, other=None):
        return None

    signature = inspect.signature(entry)
    assert not _llm_arguments_used(signature, (), {})
    assert not _llm_arguments_used(signature, (), {"other": 1})
    # A value equal to the default is indistinguishable from an omitted one:
    # this is the idiom difference PARITY.md row 65 records.
    assert not _llm_arguments_used(signature, (), {"llm_assess": False, "llm_timeout_seconds": 60})
    assert _llm_arguments_used(signature, (), {"llm_assess": True})
    assert _llm_arguments_used(signature, (), {"llm_timeout_seconds": 30})
    assert _llm_arguments_used(signature, (), {"llm_request_fn": print})
    assert _llm_arguments_used(signature, (), {"llm_context_files": pd.DataFrame({"x": [1]})})
    assert _llm_arguments_used(signature, (True,), {})
    assert set(LLM_ARGUMENT_NAMES) == {name for name in signature.parameters if name.startswith("llm_")}
    for entry_point in (suggest_semantics, infer_dictionary, infer_salmon_datapackage_artifacts, create_sdp):
        assert set(LLM_ARGUMENT_NAMES) <= set(inspect.signature(entry_point).parameters), entry_point.__name__
