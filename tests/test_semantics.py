import unittest

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None

if pd is None:
    raise unittest.SkipTest("pandas not installed")

from salmonpy import apply_semantic_suggestions, infer_dictionary, suggest_semantics
from salmonpy.dwc_dp import suggest_dwc_mappings


class SemanticsTests(unittest.TestCase):
    def test_suggest_semantics_uses_search_fn(self):
        df = pd.DataFrame({"count": [1, 2]})
        dict_df = infer_dictionary(df, dataset_id="demo", table_id="observations")
        dict_df.loc[dict_df["column_name"] == "count", "column_role"] = "measurement"

        def stub_search(query, role=None, sources=None):
            return pd.DataFrame({"label": ["Count"], "iri": ["iri:1"], "source": ["stub"], "ontology": ["o"], "role": [role], "match_type": [""], "definition": [""]})

        enriched = suggest_semantics(df, dict_df, search_fn=stub_search)
        suggestions = enriched.attrs.get("semantic_suggestions")
        self.assertIsNotNone(suggestions)
        self.assertEqual(suggestions.iloc[0]["dictionary_role"], "variable")
        self.assertEqual(suggestions.iloc[0]["target_sdp_file"], "column_dictionary.csv")

    def test_suggest_semantics_uses_role_specific_query(self):
        df = pd.DataFrame({"count": [1, 2]})
        dict_df = infer_dictionary(df, dataset_id="demo", table_id="observations")
        dict_df.loc[dict_df["column_name"] == "count", ["column_role", "unit_label"]] = ["measurement", "fish"]

        queries = []

        def stub_search(query, role=None, sources=None):
            queries.append((query, role))
            return pd.DataFrame({"label": ["Count"], "iri": ["iri:1"], "source": ["stub"], "ontology": ["o"], "role": [role], "match_type": [""], "definition": [""]})

        suggest_semantics(df, dict_df, search_fn=stub_search)
        self.assertIn(("fish", "unit"), queries)

    def test_suggest_semantics_can_include_dwc(self):
        df = pd.DataFrame({"count": [1, 2]})
        dict_df = infer_dictionary(df, dataset_id="demo", table_id="observations")
        dict_df.loc[dict_df["column_name"] == "count", "column_role"] = "measurement"

        def stub_search(query, role=None, sources=None):
            return pd.DataFrame({"label": ["Count"], "iri": ["iri:1"], "source": ["stub"], "ontology": ["o"], "role": [role], "match_type": [""], "definition": [""]})

        enriched = suggest_semantics(df, dict_df, search_fn=stub_search, include_dwc=True)
        dwc_map = enriched.attrs.get("dwc_mappings")
        self.assertIsNotNone(dwc_map)
        self.assertIsInstance(dwc_map, pd.DataFrame)

    def test_suggest_semantics_supports_code_table_dataset_targets(self):
        df = pd.DataFrame({"species_code": ["CO"], "count": [1]})
        dict_df = infer_dictionary(df, dataset_id="demo", table_id="observations")
        dict_df.loc[dict_df["column_name"] == "species_code", "column_role"] = "measurement"
        codes = pd.DataFrame(
            {
                "dataset_id": ["demo"],
                "table_id": ["observations"],
                "column_name": ["species_code"],
                "code_value": ["CO"],
                "code_label": ["Coho"],
                "code_description": ["Coho salmon code"],
            }
        )
        table_meta = pd.DataFrame(
            {
                "dataset_id": ["demo"],
                "table_id": ["observations"],
                "file_name": ["observations.csv"],
                "table_label": ["Observations"],
                "description": ["Fish observations"],
                "observation_unit": ["salmon population"],
                "observation_unit_iri": [pd.NA],
            }
        )
        dataset_meta = pd.DataFrame({"dataset_id": ["demo"], "title": ["Demo"], "description": ["Salmon monitoring"], "keywords": [pd.NA]})

        def stub_search(query, role=None, sources=None):
            return pd.DataFrame(
                {
                    "label": [f"{query} {role}"],
                    "iri": [f"https://example.org/{role}"],
                    "source": ["stub"],
                    "ontology": ["demo"],
                    "role": [role],
                    "match_type": ["label"],
                    "definition": [""],
                    "score": [1.0],
                    "role_hints": [role],
                }
            )

        enriched = suggest_semantics(
            df,
            dict_df,
            search_fn=stub_search,
            max_per_role=1,
            codes=codes,
            table_meta=table_meta,
            dataset_meta=dataset_meta,
        )
        scopes = set(enriched.attrs["semantic_suggestions"]["target_scope"])
        self.assertTrue({"column", "code", "table", "dataset"}.issubset(scopes))

    def test_apply_semantic_suggestions_fills_selected_fields(self):
        dict_df = pd.DataFrame(
            {
                "dataset_id": ["demo"],
                "table_id": ["observations"],
                "column_name": ["count"],
                "column_label": ["Count"],
                "column_description": ["Spawner count"],
                "column_role": ["measurement"],
                "value_type": ["integer"],
                "required": [False],
            }
        )
        suggestions = pd.DataFrame(
            {
                "dataset_id": ["demo", "demo"],
                "table_id": ["observations", "observations"],
                "column_name": ["count", "count"],
                "dictionary_role": ["variable", "property"],
                "iri": ["https://example.org/count", "https://example.org/property"],
                "score": [0.8, 0.9],
            }
        )
        out = apply_semantic_suggestions(dict_df, suggestions=suggestions, verbose=False)
        self.assertEqual(out["term_iri"].iloc[0], "https://example.org/count")
        self.assertEqual(out["property_iri"].iloc[0], "https://example.org/property")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
