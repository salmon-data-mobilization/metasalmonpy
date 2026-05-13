import unittest

import pandas as pd

from salmonpy import detect_semantic_term_gaps, render_ontology_term_request, submit_term_request_issues


class TermRequestTests(unittest.TestCase):
    def test_detect_semantic_term_gaps_classifies_non_smn_candidates(self):
        suggestions = pd.DataFrame(
            {
                "dataset_id": ["d1", "d1"],
                "table_id": ["t1", "t1"],
                "column_name": ["run_id", "run_id"],
                "code_value": [pd.NA, pd.NA],
                "dictionary_role": ["variable", "variable"],
                "target_scope": ["column", "column"],
                "target_sdp_file": ["column_dictionary.csv", "column_dictionary.csv"],
                "target_sdp_field": ["term_iri", "term_iri"],
                "target_row_key": ["run_id", "run_id"],
                "search_query": ["run id", "run id"],
                "column_label": ["Run ID", "Run ID"],
                "column_description": ["Local run identifier", "Local run identifier"],
                "label": ["run id", "run identifier"],
                "iri": [pd.NA, pd.NA],
                "source": ["gbif", "worms"],
                "ontology": ["gbif", "worms"],
                "match_type": ["label", "label"],
                "definition": [pd.NA, pd.NA],
                "score": [0.9, 0.85],
            }
        )
        gaps = detect_semantic_term_gaps(suggestions=suggestions, include_dictionary_roles=["variable"])
        self.assertGreaterEqual(len(gaps), 1)
        self.assertEqual(gaps["placement_recommendation"].iloc[0], "profile")

    def test_render_and_dry_run_submit(self):
        gaps = pd.DataFrame(
            {
                "dataset_id": ["d1"],
                "table_id": ["t1"],
                "column_name": ["run_id"],
                "code_value": [pd.NA],
                "target_scope": ["column"],
                "target_sdp_file": ["column_dictionary.csv"],
                "target_sdp_field": ["term_iri"],
                "target_row_key": ["run_id"],
                "dictionary_role": ["variable"],
                "search_query": ["run id"],
                "column_label": ["Run ID"],
                "column_description": ["Dataset-specific run identifier"],
                "top_non_smn_source": ["gbif"],
                "top_non_smn_label": ["Run event id"],
                "top_non_smn_iri": [pd.NA],
                "top_non_smn_ontology": [pd.NA],
                "top_non_smn_match_type": ["label"],
                "top_non_smn_score": [0.9],
                "candidate_count": [1],
                "non_smn_sources": ["gbif"],
                "placement_recommendation": ["profile"],
                "placement_confidence": [0.82],
                "placement_rationale": ["contains internal identifier signal"],
            }
        )
        reqs = render_ontology_term_request(gaps, scope="profile", ask=False, profile_name="pacific-monitoring")
        self.assertEqual(reqs["request_scope"].iloc[0], "profile")
        self.assertIn("pacific-monitoring", reqs["request_title"].iloc[0])
        self.assertIn("New term template", reqs["request_body"].iloc[0])
        dry = submit_term_request_issues(reqs, dry_run=True, confirm=False)
        self.assertEqual(dry["status"].iloc[0], "dry_run")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
