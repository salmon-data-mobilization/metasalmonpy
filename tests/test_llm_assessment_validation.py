"""Twins of metasalmon's tests/testthat/test-llm-assessment-validation.R, defects (2) and (3).

Hub item B-361 fixed two ways the in-package model call's assessment
validator mangled a model's answer, in metasalmon's
``.ms_validate_llm_assessment()``; this module pins the same two rules on
``llm_review._validate_item()``. The path is deprecated and leaves in 0.7.0
(B-330); until then it reads an index as metasalmon does.

  (2) A non-accept decision has its index cleared before the range check, so a
      reject_shortlist carrying a stray out-of-range index stays a rejection
      (and is escalated) instead of becoming review.
  (3) An index that is not a whole number is refused, not truncated: 1.9 used
      to select candidate 1.

Python appends no downgrade note to the rationale on this path, where
metasalmon does, so the twins do not assert one.
"""

from __future__ import annotations

import pandas as pd
import pytest

from metasalmonpy.llm_review import (
    _assess_generic,
    _escalate_reject_shortlist,
    _validate_item,
)


def _candidates() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "iri": ["https://example.org/a", "https://example.org/b"],
            "label": ["A", "B"],
        }
    )


def _item(decision, index, rationale="Looks right.") -> dict:
    return {
        "decision": decision,
        "selected_candidate_index": index,
        "confidence": 0.9,
        "rationale": rationale,
        "missing_context": "",
    }


# (2) --------------------------------------------------------------------------


def test_a_reject_shortlist_carrying_a_stray_out_of_range_index_stays_a_rejection():
    rationale = "The whole shortlist is the wrong concept family."
    result = _validate_item(_item("reject_shortlist", 99, rationale), _candidates(), "property")
    assert result["decision"] == "reject_shortlist"
    assert result["selected_index"] is None
    assert result["rationale"] == rationale


def test_a_review_decision_carrying_a_stray_out_of_range_index_keeps_its_rationale():
    result = _validate_item(_item("review", 0, "Unsure."), _candidates(), "property")
    assert result["decision"] == "review"
    assert result["selected_index"] is None
    assert result["rationale"] == "Unsure."


def test_an_accept_with_an_out_of_range_index_still_downgrades_to_review():
    result = _validate_item(_item("accept", 3), _candidates(), "property")
    assert result["decision"] == "review"
    assert result["selected_index"] is None


def _target() -> pd.Series:
    return pd.Series(
        {
            "dataset_id": "d1",
            "table_id": "t1",
            "column_name": "catch_weight",
            "code_value": None,
            "dictionary_role": "property",
            "target_scope": "column",
            "target_sdp_file": "column_dictionary.csv",
            "target_sdp_field": "property_iri",
            "search_query": "catch weight",
        }
    )


def _config(answer) -> dict:
    return {
        "provider": "openai",
        "model": "gpt-5-mini",
        "request_fn": lambda messages, config: answer,
    }


def test_a_rejection_carrying_a_stray_index_still_escalates_to_request_new_term():
    # The generic path escalates every final reject_shortlist; before the fix
    # the stray index had already turned this one into review, so nothing was
    # left to escalate and no ontology gap was surfaced.
    answer = _item("reject_shortlist", 99, "The whole shortlist is the wrong concept family.")
    row = _assess_generic(_target(), _candidates().assign(source="smn"), pd.DataFrame(), _config(answer), top_n=2)
    assert row["llm_decision"] == "reject_shortlist"
    _escalate_reject_shortlist(row, row.copy())
    assert row["llm_decision"] == "request_new_term"
    assert row["llm_escalated_from"] == "reject_shortlist"


# (3) --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [1.9, "1.9", 0.5, "2.000001"])
def test_a_candidate_index_that_is_not_a_whole_number_is_refused_not_truncated(bad):
    with pytest.raises(ValueError, match="whole number"):
        _validate_item(_item("accept", bad), _candidates(), "property")


@pytest.mark.parametrize("ok", [2, "2", "2.0", 2.0])
def test_a_whole_number_index_written_as_a_decimal_or_a_string_is_still_accepted(ok):
    result = _validate_item(_item("accept", ok), _candidates(), "property")
    assert result["decision"] == "accept"
    assert result["selected_index"] == 2
    assert type(result["selected_index"]) is int


def test_a_non_whole_index_on_a_non_accept_decision_is_ignored():
    result = _validate_item(_item("review", 1.9, "Unsure."), _candidates(), "property")
    assert result["decision"] == "review"
    assert result["selected_index"] is None


def test_an_index_that_is_not_a_number_reads_as_no_selection():
    # metasalmon reads it as NA: an accept with no candidate is downgraded.
    result = _validate_item(_item("accept", "first"), _candidates(), "property")
    assert result["decision"] == "review"
    assert result["selected_index"] is None


def test_a_refused_index_reaches_the_assessments_as_an_error_row_not_as_candidate_1():
    candidates = _candidates().assign(source="smn")
    row = _assess_generic(_target(), candidates, pd.DataFrame(), _config(_item("accept", 1.9)), top_n=2)
    assert "whole number" in str(row["llm_error"])
    assert pd.isna(row.get("llm_selected_candidate_index"))
    assert pd.isna(row.get("llm_selected_iri"))
