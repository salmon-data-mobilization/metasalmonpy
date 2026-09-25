"""The bundle validators give metasalmon's verdicts on the same input (hub B-360).

Brett ruled on 2026-09-25 (S16 execplan, decision 9) that the two packages
converge on R's validators, this package moving: they are surface 5 of the
role contract, and the shared review-packet fixtures (B-326 / B-327) fail until
the two sides agree. This is convergence, not a PARITY.md row.

The pin is one set of cases scored by both packages:

* ``tests/data/validator_parity/cases.json`` holds them -- 118 dimension
  inputs, the three evidence predicates' strings, role-hint strings, role-type
  candidates, field-anchored evidence cases and whole bundles run through the
  driver;
* ``tests/data/validator_parity/expected.json`` is what metasalmon's own
  functions in ``R/semantic-bundle-validators.R`` returned for them, written by
  ``expected-from-r.R`` beside it (run with
  ``R_LIBS=/tmp/metasalmon-lib Rscript expected-from-r.R . expected.json``
  against metasalmon ``main`` at ``98cb9e6``, 2026-09-25);
* the offline tests below hold this package to that file, and the last test
  re-runs the R script wherever R and an installed metasalmon are available
  (the ``parity`` job of ``.github/workflows/parity.yml``), so a change on the
  R side turns that job red instead of drifting.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pandas as pd
import pytest

from metasalmonpy.llm_review import (
    VALIDATOR_FINDING_COLUMNS,
    _apply_validators,
    _bundle_validator_evidence,
    _dimension,
    _has_constraint_evidence,
    _has_method_evidence,
    _has_modifier_evidence,
    _role_type_message,
    _split_role_hints,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "data" / "validator_parity"
R_LIB_PATH = "/tmp/metasalmon-lib"
HAVE_R = shutil.which("Rscript") is not None and os.path.isdir(R_LIB_PATH)

CASES = json.loads((FIXTURE_DIR / "cases.json").read_text(encoding="utf-8"))
EXPECTED = json.loads((FIXTURE_DIR / "expected.json").read_text(encoding="utf-8"))

IDENTITY = {
    "code_value": None,
    "target_scope": "column",
    "target_sdp_file": "column_dictionary.csv",
}


def _none_to_na(value):
    return pd.NA if value is None else value


def _na_to_none(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _row(fields: dict) -> pd.Series:
    return pd.Series({key: _none_to_na(value) for key, value in fields.items()})


def _context(chunks: list[str]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "source": ["inline_context"] * len(chunks),
            "chunk_id": [f"inline_context[{i}]#1" for i in range(1, len(chunks) + 1)],
            "text": chunks,
        },
        columns=["source", "chunk_id", "text"],
    )


# --- the pieces, each against R's verdicts ------------------------------------


@pytest.mark.parametrize("index", range(len(CASES["dimension"])))
def test_the_dimension_classifier_matches(index):
    values = CASES["dimension"][index]
    assert _dimension(*values) == EXPECTED["dimension"][index], values


@pytest.mark.parametrize("index", range(len(CASES["method_evidence"])))
def test_method_evidence_matches(index):
    text = CASES["method_evidence"][index]
    assert _has_method_evidence(text) is EXPECTED["method_evidence"][index], text


@pytest.mark.parametrize("index", range(len(CASES["constraint_evidence"])))
def test_constraint_evidence_matches(index):
    text = CASES["constraint_evidence"][index]
    assert _has_constraint_evidence(text) is EXPECTED["constraint_evidence"][index], text


@pytest.mark.parametrize("index", range(len(CASES["modifier_evidence"])))
def test_modifier_evidence_matches(index):
    text = CASES["modifier_evidence"][index]
    assert _has_modifier_evidence(text) is EXPECTED["modifier_evidence"][index], text


@pytest.mark.parametrize("index", range(len(CASES["role_hints"])))
def test_role_hints_split_on_the_bar_only_and_keep_their_order(index):
    value = CASES["role_hints"][index]
    assert _split_role_hints(value) == EXPECTED["role_hints"][index], value


@pytest.mark.parametrize("name", [case["name"] for case in CASES["role_type"]])
def test_the_role_type_verdict_matches(name):
    case = next(case for case in CASES["role_type"] if case["name"] == name)
    assert _role_type_message(case["role"], case["candidate"]) == EXPECTED["role_type"][name]


@pytest.mark.parametrize("name", [case["name"] for case in CASES["evidence"]])
def test_field_anchored_evidence_matches(name):
    case = next(case for case in CASES["evidence"] if case["name"] == name)
    evidence = _bundle_validator_evidence(
        _row(case["target"]),
        {key: _none_to_na(value) for key, value in case["dict_row"].items()},
        _context(case["chunks"]),
    )
    assert evidence == EXPECTED["evidence"][name]


# --- whole bundles through the driver ------------------------------------------


def _bundle_frames(case: dict):
    dictionary_row = {key: _none_to_na(value) for key, value in case["dict_row"].items()}
    identity = {
        "dataset_id": dictionary_row["dataset_id"],
        "table_id": dictionary_row["table_id"],
        "column_name": dictionary_row["column_name"],
        **{key: _none_to_na(value) for key, value in IDENTITY.items()},
    }
    targets = pd.DataFrame(
        [
            {
                **identity,
                **{key: _none_to_na(value) for key, value in target.items()},
            }
            for target in case["targets"]
        ]
    )
    query_for = dict(zip(targets["dictionary_role"], targets["search_query"]))
    field_for = dict(zip(targets["dictionary_role"], targets["target_sdp_field"]))
    suggestions = pd.DataFrame(
        [
            {
                **identity,
                "dictionary_role": role,
                "target_sdp_field": field_for[role],
                "search_query": query_for[role],
                "role": role,
                "match_type": "label",
                **{key: _none_to_na(value) for key, value in candidate.items()},
            }
            for role, candidates in case["candidates"].items()
            for candidate in candidates
        ]
    )
    rows = [
        {
            **identity,
            "target_sdp_field": field_for[row["dictionary_role"]],
            "search_query": query_for[row["dictionary_role"]],
            **{key: _none_to_na(value) for key, value in row.items()},
        }
        for row in case["assessments"]
    ]
    return rows, targets, pd.DataFrame([dictionary_row]), suggestions, _context(case["chunks"])


@pytest.mark.parametrize("name", [case["name"] for case in CASES["bundles"]])
def test_the_bundle_verdicts_match(name):
    case = next(case for case in CASES["bundles"] if case["name"] == name)
    rows, findings = _apply_validators(*_bundle_frames(case))
    expected = EXPECTED["bundles"][name]
    # Codes, severities, roles, decisions and messages, in R's order.
    assert [
        {column: _na_to_none(finding[column]) for column in VALIDATOR_FINDING_COLUMNS}
        for finding in findings
    ] == expected["findings"]
    assert [
        {
            "dictionary_role": row["dictionary_role"],
            "llm_decision": row["llm_decision"],
            "llm_selected_candidate_index": _na_to_none(row["llm_selected_candidate_index"]),
            "llm_selected_iri": _na_to_none(row["llm_selected_iri"]),
            "llm_selected_label": _na_to_none(row["llm_selected_label"]),
            "llm_rationale": _na_to_none(row["llm_rationale"]),
            "llm_confidence": _na_to_none(row["llm_confidence"]),
        }
        for row in rows
    ] == expected["rows"]


def test_the_cases_cover_every_validator_code():
    # A code no case raises would let a validator drift unpinned.
    raised = {
        finding["code"]
        for bundle in EXPECTED["bundles"].values()
        for finding in bundle["findings"]
    }
    assert raised == {
        "SEM_METHOD_EVIDENCE_REQUIRED",
        "SEM_MODIFIER_EVIDENCE_REQUIRED",
        "SEM_CONSTRAINT_EVIDENCE_REQUIRED",
        "SEM_ROLE_TYPE_MISMATCH",
        "SEM_DIMENSION_MISMATCH",
        "SEM_PROPERTY_UNIT_DIMENSION_MISMATCH",
        "SEM_REDUNDANT_CATCH_CONTEXT",
    }
    # And the dimension inputs reach every class the classifier names.
    assert set(EXPECTED["dimension"]) >= {
        "flow", "speed", "temperature", "area", "volume", "mass", "length",
        "count", "dimensionless", "rate", None,
    }


# --- the R side, wherever it can run ------------------------------------------


@pytest.mark.skipif(not HAVE_R, reason="Rscript or metasalmon library not available")
def test_expected_json_is_what_the_installed_metasalmon_computes():
    env = os.environ.copy()
    env["R_LIBS"] = R_LIB_PATH
    completed = subprocess.run(
        ["Rscript", str(FIXTURE_DIR / "expected-from-r.R"), str(FIXTURE_DIR)],
        env=env,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert json.loads(completed.stdout) == EXPECTED
