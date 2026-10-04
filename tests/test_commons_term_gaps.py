"""B-279: the commons register remains evidence, including rows held for review.

The JSON fixtures are byte-identical to the B-278 R fixtures. A rendered draft
is never authority to mint a term or submit an ontology issue.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from metasalmonpy import (
    detect_semantic_term_gaps,
    render_ontology_term_request,
    submit_term_request_issues,
)
from metasalmonpy.term_requests import GAP_COLUMNS


FIXTURES = Path(__file__).parent / "fixtures" / "commons-gaps"
COMMONS_FIELDS = [
    "commons_" + field
    for field in (
        "concept", "title", "context", "registry", "status", "mint_target",
        "state", "proposal", "rejected_because", "evidence_needed",
        "blocked_by", "conflicts", "note", "card_status", "verified",
        "hold_reason",
    )
]


def _export(name="register-excerpt.json"):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["gaps"]


def _write_export(document):
    descriptor, name = tempfile.mkstemp(suffix=".json")
    os.close(descriptor)
    path = Path(name)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


class CommonsTermGapTests(unittest.TestCase):
    def test_register_rows_keep_order_evidence_types_and_existing_prefix(self):
        raw = _export()
        gaps = detect_semantic_term_gaps(commons_gaps=FIXTURES / "register-excerpt.json")
        self.assertEqual(list(gaps.columns), GAP_COLUMNS + COMMONS_FIELDS)
        self.assertEqual(len(gaps), len(raw))
        self.assertEqual(gaps["commons_concept"].tolist(), [r["concept"] for r in raw])
        self.assertLess(len(set(gaps["commons_concept"])), len(gaps))
        for field in COMMONS_FIELDS[:-1]:
            original = field.removeprefix("commons_")
            for value, record in zip(gaps[field], raw):
                expected = record[original]
                if expected is None:
                    self.assertTrue(pd.isna(value), field)
                else:
                    self.assertEqual(value, expected, field)
        self.assertEqual(gaps.loc[0, "commons_blocked_by"], [])
        self.assertEqual(json.dumps(gaps.loc[0, "commons_blocked_by"]), "[]")
        self.assertEqual(gaps["target_label"].tolist(), gaps["commons_title"].tolist())
        self.assertEqual(gaps["search_query"].tolist(), gaps["commons_title"].tolist())
        self.assertTrue((gaps["gap_detection_basis"] == "commons_register").all())
        for field in ("dataset_id", "table_id", "column_name", "dictionary_role", "target_row_key", "llm_decision"):
            self.assertTrue(gaps[field].isna().all(), field)
        self.assertEqual(list(detect_semantic_term_gaps(dict_df=pd.DataFrame()).columns), GAP_COLUMNS)

    def test_lifecycle_holds_cannot_be_reopened_by_renderer_controls(self):
        rows = _export() + _export("synthetic-controls.json")
        combined = dict(rows[0], blocked_by=["concepts/example"], conflicts="Two authorities disagree about this concept.")
        rows.append(combined)
        path = _write_export({"gaps": rows})
        self.addCleanup(path.unlink)
        gaps = detect_semantic_term_gaps(commons_gaps=path)
        reasons = gaps["commons_hold_reason"].tolist()
        self.assertIn("", reasons)
        for reason in (
            "proposed", "rejected", "do-not-mint", "blocked", "conflicted",
            "contested", "evidence-needed", "unsupported-target", "deprecated-card",
        ):
            self.assertTrue(any(reason in value for value in reasons), reason)
        self.assertTrue(any("blocked; conflicted" in value for value in reasons))
        requests = render_ontology_term_request(gaps, ask=False)
        expected = ["skip" if reason else target for reason, target in zip(reasons, gaps["commons_mint_target"])]
        self.assertEqual(requests["request_scope"].tolist(), expected)
        for kwargs in ({"scope": "smn"}, {"scope_overrides": "gcdfo"}, {"ask": True}):
            controlled = render_ontology_term_request(gaps, **kwargs)
            self.assertEqual(controlled["request_scope"].tolist(), expected)
        tampered = gaps.copy()
        tampered["commons_hold_reason"] = ""
        tampered["placement_recommendation"] = "smn"
        tampered["gap_detection_basis"] = pd.NA
        self.assertEqual(render_ontology_term_request(tampered, ask=True)["request_scope"].tolist(), expected)
        tampered = gaps.copy()
        tampered.at[0, "commons_blocked_by"] = None
        with self.assertRaisesRegex(ValueError, "commons_gaps"):
            render_ontology_term_request(tampered, scope="smn")

    def test_drafts_cite_commons_without_fake_dataset_and_dry_run_has_no_network(self):
        gaps = detect_semantic_term_gaps(commons_gaps=FIXTURES / "register-excerpt.json")
        labels = [" custom ", "", " custom "]
        requests = render_ontology_term_request(gaps, ask=False, issue_labels=labels)
        self.assertTrue(all(value == labels for value in requests["issue_labels"]))
        for row, (_, request) in zip(_export(), requests.iterrows()):
            body = request["request_body"]
            self.assertIn(row["concept"], body)
            self.assertIn(row["note"], body)
            self.assertIn("Curator definition required.", body)
            self.assertIn("Curator term type required.", body)
            self.assertIn("Verified: false", body)
            self.assertNotIn("Dataset evidence", body)
            self.assertNotIn("unknown / unknown / unknown", body)
        with mock.patch("metasalmonpy.term_requests.requests.post", side_effect=AssertionError("network")), mock.patch(
            "metasalmonpy.github_io._github_token", side_effect=AssertionError("authentication")
        ):
            preview = submit_term_request_issues(requests, dry_run=True)
        eligible = gaps["commons_hold_reason"].eq("")
        self.assertEqual(len(preview), int(eligible.sum()))
        self.assertEqual(preview["request_scope"].tolist(), gaps.loc[eligible, "commons_mint_target"].tolist())
        self.assertTrue((preview["status"] == "dry_run").all())

    def test_reader_rejects_malformed_types_duplicate_keys_and_sdp_filters(self):
        fixture = FIXTURES / "register-excerpt.json"
        for value in ({"gaps": []}, [fixture, fixture], "", '{"gaps":[]}', FIXTURES / "missing.json"):
            with self.assertRaisesRegex((TypeError, ValueError), "commons_gaps"):
                detect_semantic_term_gaps(commons_gaps=value)
        for kwargs in (
            {"dict_df": pd.DataFrame()}, {"suggestions": pd.DataFrame()},
            {"include_target_scopes": ["column"]},
            {"include_dictionary_roles": ["variable"]}, {"min_score": 0.5},
        ):
            with self.assertRaisesRegex(ValueError, "commons_gaps|exclusive|SDP"):
                detect_semantic_term_gaps(commons_gaps=fixture, **kwargs)
        base = _export()[0]
        for change in (
            {"registry": "invalid"}, {"status": "minted"}, {"mint_target": "profile"},
            {"state": "minted"}, {"state": "proposed"}, {"state": "rejected"},
            {"card_status": "unknown"}, {"context": "unknown"}, {"verified": "false"},
            {"concept": 2}, {"note": "short"}, {"blocked_by": None},
            {"blocked_by": "concepts/example"}, {"blocked_by": [2]},
            {"proposal": "relative/path"},
        ):
            path = _write_export({"gaps": [dict(base, **change)]})
            self.addCleanup(path.unlink)
            with self.assertRaisesRegex(ValueError, "commons_gaps"):
                detect_semantic_term_gaps(commons_gaps=path)
        for content in ('{', '[]', '{}', '{"gaps":null}', '{"gaps":{}}', '{"gaps":[null]}',
                        '{"gaps":[{}]}', '{"gaps":[],"gaps":[]}',
                        '{"gaps":[{"state":"open","state":"rejected"}]}'):
            descriptor, name = tempfile.mkstemp(suffix=".json")
            os.close(descriptor)
            path = Path(name)
            self.addCleanup(path.unlink)
            path.write_text(content, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "commons_gaps"):
                detect_semantic_term_gaps(commons_gaps=path)
        path = _write_export({"gaps": []})
        self.addCleanup(path.unlink)
        empty = detect_semantic_term_gaps(commons_gaps=path)
        self.assertEqual(len(empty), 0)
        self.assertEqual(list(empty.columns), GAP_COLUMNS + COMMONS_FIELDS)
        self.assertTrue(pd.api.types.is_string_dtype(empty["placement_recommendation"]))

    def test_parse_error_never_echoes_secret_bearing_source_text(self):
        descriptor, name = tempfile.mkstemp(suffix=".json")
        os.close(descriptor)
        path = Path(name)
        self.addCleanup(path.unlink)
        path.write_text('{"gaps":[API_KEY=synthetic-secret {1+1}]}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "commons_gaps") as caught:
            detect_semantic_term_gaps(commons_gaps=path)
        self.assertNotIn("synthetic-secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
