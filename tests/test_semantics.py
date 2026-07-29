import unittest

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None

if pd is None:
    raise unittest.SkipTest("pandas not installed")

from salmonpy import apply_semantic_suggestions, infer_dictionary, suggest_semantics
from salmonpy.dwc_dp import suggest_dwc_mappings
from salmonpy.semantics import _table_suggestion_is_compatible


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

    def test_explicit_string_source_is_a_single_strict_source(self):
        df = pd.DataFrame({"count": [1, 2]})
        dict_df = infer_dictionary(df, dataset_id="demo", table_id="observations")
        dict_df.loc[
            dict_df["column_name"] == "count",
            "column_role",
        ] = "measurement"
        observed_sources = []

        def stub_search(query, role=None, sources=None):
            observed_sources.append(tuple(sources))
            return pd.DataFrame()

        suggest_semantics(
            df,
            dict_df,
            sources="smn",
            search_fn=stub_search,
        )

        self.assertTrue(observed_sources)
        self.assertEqual(set(observed_sources), {("smn",)})

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

    def test_apply_filters_incompatible_categorical_suggestions(self):
        dict_df = pd.DataFrame(
            {
                "dataset_id": ["demo", "demo"],
                "table_id": ["observations", "observations"],
                "column_name": ["AREA", "WATERBODY"],
                "column_label": ["Area", "Waterbody"],
                "column_description": ["Reporting area", "Waterbody"],
                "column_role": ["categorical", "categorical"],
                "value_type": ["string", "string"],
                "required": [False, False],
            }
        )
        suggestions = pd.DataFrame(
            {
                "dataset_id": ["demo", "demo"],
                "table_id": ["observations", "observations"],
                "column_name": ["AREA", "WATERBODY"],
                "dictionary_role": ["variable", "variable"],
                "target_sdp_field": ["term_iri", "term_iri"],
                "search_query": ["Area", "Waterbody"],
                "column_label": ["Area", "Waterbody"],
                "label": ["In River Mortality Rate", "Waterbody"],
                "iri": [
                    "https://example.org/mortality",
                    "https://example.org/waterbody",
                ],
                "match_type": ["label_exact", "label_exact"],
                "score": [0.95, 0.95],
            }
        )

        out = apply_semantic_suggestions(
            dict_df,
            suggestions=suggestions,
            verbose=False,
        )

        self.assertTrue(pd.isna(out.loc[out["column_name"] == "AREA", "term_iri"].iloc[0]))
        self.assertEqual(
            out.loc[out["column_name"] == "WATERBODY", "term_iri"].iloc[0],
            "https://example.org/waterbody",
        )

    def test_apply_filters_physical_measurement_false_matches_but_keeps_unit(self):
        dict_df = pd.DataFrame(
            {
                "dataset_id": ["demo"],
                "table_id": ["hydro"],
                "column_name": ["water_level"],
                "column_label": ["Water Level"],
                "column_description": ["Water level in meters"],
                "column_role": ["measurement"],
                "value_type": ["number"],
                "required": [False],
                "unit_label": ["meter"],
            }
        )
        suggestions = pd.DataFrame(
            {
                "dataset_id": ["demo", "demo"],
                "table_id": ["hydro", "hydro"],
                "column_name": ["water_level", "water_level"],
                "dictionary_role": ["variable", "unit"],
                "target_sdp_field": ["term_iri", "unit_iri"],
                "search_query": ["water level", "meter"],
                "column_label": ["Water Level", "Water Level"],
                "label": ["Escapement", "Meter"],
                "iri": [
                    "https://w3id.org/smn/Escapement",
                    "http://qudt.org/vocab/unit/M",
                ],
                "source": ["smn", "qudt"],
                "ontology": ["smn", "qudt"],
                "match_type": ["class", "unit"],
                "score": [8.0, 4.4],
            }
        )

        out = apply_semantic_suggestions(
            dict_df,
            suggestions=suggestions,
            verbose=False,
        )

        self.assertTrue(pd.isna(out["term_iri"].iloc[0]))
        self.assertEqual(out["unit_iri"].iloc[0], "http://qudt.org/vocab/unit/M")

    def test_table_suggestion_requires_lexical_support(self):
        table_row = pd.Series(
            {
                "dataset_id": "demo",
                "table_id": "catches",
                "table_label": "Catches",
                "description": "One row per catch.",
                "observation_unit": pd.NA,
            }
        )
        compatible = pd.Series(
            {
                "target_query_basis": "table_label",
                "target_query_context": "Catches catches",
                "label": "Catches observation",
                "match_type": "label_exact",
                "score": 2.0,
            }
        )
        incompatible = compatible.copy()
        incompatible["label"] = "Metadata note"

        self.assertTrue(_table_suggestion_is_compatible(compatible, table_row))
        self.assertFalse(_table_suggestion_is_compatible(incompatible, table_row))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
