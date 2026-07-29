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
        skipped = render_ontology_term_request(gaps, scope="skip", ask=False)
        uncertain = render_ontology_term_request(
            gaps,
            scope="uncertain",
            ask=False,
        )
        self.assertEqual(skipped["request_scope"].iloc[0], "skip")
        self.assertEqual(uncertain["request_scope"].iloc[0], "skip")

    def test_llm_request_new_term_remains_a_gap_even_with_smn_candidate(self):
        suggestions = pd.DataFrame(
            {
                "dataset_id": ["d1"],
                "table_id": ["t1"],
                "column_name": ["catch_weight"],
                "code_value": [pd.NA],
                "dictionary_role": ["variable"],
                "target_scope": ["column"],
                "target_sdp_file": ["column_dictionary.csv"],
                "target_sdp_field": ["term_iri"],
                "target_row_key": ["d1/t1/catch_weight"],
                "target_label": ["Catch weight"],
                "target_description": ["Whole-variable catch weight."],
                "search_query": ["catch weight"],
                "column_label": ["Catch weight"],
                "column_description": ["Whole-variable catch weight."],
                "source": ["smn"],
                "label": ["Fish weight"],
                "iri": ["https://w3id.org/smn/FishWeight"],
                "ontology": ["smn"],
                "match_type": ["label"],
                "definition": ["Weight of a fish."],
                "score": [0.95],
                "llm_decision": ["request_new_term"],
                "llm_confidence": [0.88],
                "llm_rationale": [
                    "FishWeight is a property, not the whole variable."
                ],
                "llm_new_term_label": ["Catch weight"],
                "llm_new_term_definition": [
                    "Weight associated with an observed catch."
                ],
                "llm_new_term_namespace": ["gcdfo"],
                "llm_escalated_from": ["reject_shortlist"],
            }
        )

        gaps = detect_semantic_term_gaps(suggestions=suggestions)

        self.assertEqual(len(gaps), 1)
        self.assertEqual(
            gaps["gap_detection_basis"].iloc[0],
            "llm_request_new_term",
        )
        self.assertEqual(gaps["llm_new_term_namespace"].iloc[0], "gcdfo")
        self.assertEqual(
            gaps["llm_escalated_from"].iloc[0],
            "reject_shortlist",
        )

    def test_detect_reads_assessments_attribute_when_suggestions_omitted(self):
        dictionary = pd.DataFrame(
            {
                "dataset_id": ["d1"],
                "table_id": ["t1"],
                "column_name": ["ocean_phase"],
                "column_label": ["Ocean phase"],
                "column_description": ["Assessment life-cycle phase."],
            }
        )
        dictionary.attrs["semantic_suggestions"] = pd.DataFrame()
        dictionary.attrs["semantic_llm_assessments"] = pd.DataFrame(
            {
                "dataset_id": ["d1"],
                "table_id": ["t1"],
                "column_name": ["ocean_phase"],
                "code_value": [pd.NA],
                "dictionary_role": ["constraint"],
                "target_scope": ["column"],
                "target_sdp_file": ["column_dictionary.csv"],
                "target_sdp_field": ["constraint_iri"],
                "search_query": ["ocean phase"],
                "llm_decision": ["request_new_term"],
                "llm_confidence": [0.8],
                "llm_rationale": ["No precise phase constraint exists."],
                "llm_new_term_label": ["Ocean phase"],
                "llm_new_term_definition": ["Marine life-cycle phase."],
            }
        )

        gaps = detect_semantic_term_gaps(dict_df=dictionary)

        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps["column_label"].iloc[0], "Ocean phase")
        self.assertEqual(
            gaps["gap_detection_basis"].iloc[0],
            "llm_request_new_term",
        )

    def test_gcdfo_namespace_routes_to_gcdfo_template_and_repo(self):
        suggestions = pd.DataFrame(
            {
                "dataset_id": ["d1"],
                "table_id": ["t1"],
                "column_name": ["ocean_phase"],
                "code_value": [pd.NA],
                "dictionary_role": ["constraint"],
                "target_scope": ["column"],
                "target_sdp_file": ["column_dictionary.csv"],
                "target_sdp_field": ["constraint_iri"],
                "target_row_key": ["d1/t1/ocean_phase"],
                "target_label": ["Ocean phase"],
                "target_description": ["Assessment life-cycle phase."],
                "search_query": ["ocean phase"],
                "column_label": ["Ocean phase"],
                "column_description": ["Assessment life-cycle phase."],
                "source": ["ols"],
                "label": ["Marine phase"],
                "iri": ["https://example.org/marine-phase"],
                "ontology": ["example"],
                "match_type": ["label"],
                "definition": ["Marine phase."],
                "score": [0.8],
                "llm_decision": ["request_new_term"],
                "llm_new_term_label": ["Ocean phase"],
                "llm_new_term_definition": ["Marine life-cycle phase."],
                "llm_new_term_namespace": [
                    "https://w3id.org/gcdfo/salmon#"
                ],
            }
        )
        gaps = detect_semantic_term_gaps(suggestions=suggestions)

        requests = render_ontology_term_request(
            gaps,
            scope="auto",
            ask=False,
        )

        self.assertEqual(requests["request_scope"].iloc[0], "gcdfo")
        self.assertEqual(
            requests["ontology_repo"].iloc[0],
            "dfo-pacific-science/dfo-salmon-ontology",
        )
        self.assertIn("GCDFO", requests["request_title"].iloc[0])
        self.assertIn(
            "dfo-salmon-ontology",
            requests["request_body"].iloc[0],
        )

    def test_live_submission_requires_explicit_confirmation(self):
        requests = pd.DataFrame(
            {
                "request_title": ["Request new shared SMN term: Test"],
                "request_body": ["Body"],
                "request_scope": ["smn"],
                "ontology_repo": [
                    "salmon-data-mobilization/salmon-domain-ontology"
                ],
            }
        )
        with self.assertRaisesRegex(ValueError, "confirm=True"):
            submit_term_request_issues(
                requests,
                token="unused",
                dry_run=False,
                confirm=False,
            )

    def test_conflicting_new_term_proposals_abort(self):
        dictionary = pd.DataFrame(
            {
                "dataset_id": ["d1"],
                "table_id": ["t1"],
                "column_name": ["catch_weight"],
            }
        )
        dictionary.attrs["semantic_suggestions"] = pd.DataFrame()
        dictionary.attrs["semantic_llm_assessments"] = pd.DataFrame(
            {
                "dataset_id": ["d1", "d1"],
                "table_id": ["t1", "t1"],
                "column_name": ["catch_weight", "catch_weight"],
                "code_value": [pd.NA, pd.NA],
                "dictionary_role": ["variable", "variable"],
                "target_scope": ["column", "column"],
                "target_sdp_file": [
                    "column_dictionary.csv",
                    "column_dictionary.csv",
                ],
                "target_sdp_field": ["term_iri", "term_iri"],
                "search_query": ["catch weight", "catch weight"],
                "llm_decision": [
                    "request_new_term",
                    "request_new_term",
                ],
                "llm_new_term_label": [
                    "Catch weight",
                    "Observed catch weight",
                ],
            }
        )

        with self.assertRaisesRegex(
            ValueError,
            "Conflicting llm_new_term_label",
        ):
            detect_semantic_term_gaps(dict_df=dictionary)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
