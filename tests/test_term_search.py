import unittest
from unittest import mock

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None

if pd is None:
    raise unittest.SkipTest("pandas not installed")

import metasalmonpy.term_search as ts
from metasalmonpy import find_terms


class TermSearchTests(unittest.TestCase):
    def test_find_terms_prefers_scored_results(self):
        fake_ols = pd.DataFrame(
            {
                "label": ["Var A"],
                "iri": ["iri:ols"],
                "source": ["ols"],
                "ontology": ["o1"],
                "role": ["variable"],
                "match_type": [""],
                "definition": [""],
            }
        )
        fake_nvs = pd.DataFrame(
            {
                "label": ["Var B"],
                "iri": ["iri:nvs"],
                "source": ["nvs"],
                "ontology": ["o2"],
                "role": ["variable"],
                "match_type": [""],
                "definition": [""],
            }
        )

        with mock.patch.object(ts, "_search_ols", return_value=fake_ols), mock.patch.object(ts, "_search_nvs", return_value=fake_nvs), mock.patch.object(ts, "_search_bioportal", return_value=ts._empty_terms("variable")):
            res = find_terms("count", role="variable", sources=("ols", "nvs"))
        # nvs should rank higher for variable via role boost, so first row should be Var B
        self.assertEqual(res.iloc[0]["label"], "Var B")

    def test_score_and_rank_terms_prioritizes_gcdfo_entities(self):
        df = pd.DataFrame(
            {
                "label": ["GCDO entity", "Other entity"],
                "iri": ["https://w3id.org/gcdfo/salmon#Stock", "http://example.org/entity"],
                "source": ["ols", "ols"],
                "ontology": ["gcdfo", "other"],
                "role": ["entity", "entity"],
                "match_type": ["", ""],
                "definition": ["", ""],
            }
        )
        ranked = ts._score_and_rank_terms(df, "entity", pd.DataFrame())
        self.assertEqual(ranked.iloc[0]["iri"], "https://w3id.org/gcdfo/salmon#Stock")

    def test_sources_for_role_returns_expected_sources(self):
        self.assertEqual(ts.sources_for_role("unit"), ["qudt", "nvs", "ols"])
        self.assertEqual(ts.sources_for_role("entity"), ["smn", "gcdfo", "gbif", "worms", "bioportal", "ols"])
        self.assertEqual(ts.sources_for_role("property"), ["smn", "gcdfo", "nvs", "ols", "zooma"])
        self.assertEqual(ts.sources_for_role("method"), ["smn", "gcdfo", "bioportal", "ols", "zooma"])
        self.assertEqual(ts.sources_for_role("variable"), ["smn", "gcdfo", "nvs", "ols", "zooma"])
        self.assertEqual(ts.sources_for_role("constraint"), ["smn", "gcdfo", "ols"])
        self.assertEqual(ts.sources_for_role(None), ["smn", "gcdfo", "ols", "nvs"])

    def test_alignment_only_wikidata_penalized(self):
        df = pd.DataFrame(
            {
                "label": ["Salmon (Wikidata)", "Salmon (NCBI)"],
                "iri": [
                    "http://www.wikidata.org/entity/Q34134",
                    "http://purl.obolibrary.org/obo/NCBITaxon_8030",
                ],
                "source": ["ols", "ols"],
                "ontology": ["wikidata", "ncbitaxon"],
                "role": ["entity", "entity"],
                "match_type": ["", ""],
                "definition": ["", ""],
            }
        )
        ranked = ts._score_and_rank_terms(df, "entity", pd.DataFrame())
        wikidata_rows = ranked[ranked["iri"].str.contains("wikidata")]
        self.assertTrue(wikidata_rows.iloc[0]["alignment_only"])
        self.assertFalse("wikidata.org" in ranked.iloc[0]["iri"])

    def test_find_terms_adds_rows_hint(self):
        fake_response = {
            "response": {
                "docs": [
                    {"label": "Spawner count", "iri": "http://example.org/a", "ontology_name": "o1", "type": "class", "description": [""]},
                ]
            }
        }
        with mock.patch.object(ts, "_safe_json", side_effect=lambda url, headers=None, timeout=30: fake_response) as mock_json:
            res = find_terms("spawner count", sources=("ols",))
        mock_json.assert_called()
        called_url = mock_json.call_args[0][0]
        self.assertIn("rows=50", called_url)
        self.assertEqual(res.iloc[0]["label"], "Spawner count")
        self.assertIn("score", res.columns)
        self.assertIn("agreement_sources", res.columns)

    def test_score_and_rank_terms_uses_label_overlap(self):
        df = pd.DataFrame(
            {
                "label": ["Spawner count", "Natural killer cell"],
                "iri": ["a", "b"],
                "source": ["ols", "ols"],
                "ontology": ["o1", "o1"],
                "role": [None, None],
                "match_type": ["", ""],
                "definition": ["", ""],
            }
        )
        ranked = ts._score_and_rank_terms(df, None, pd.DataFrame(), "spawner count")
        self.assertEqual(ranked.iloc[0]["label"], "Spawner count")

    def test_search_nvs_uses_sparql(self):
        fake_response = {
            "results": {
                "bindings": [
                    {
                        "uri": {"type": "uri", "value": "http://vocab.nerc.ac.uk/collection/P06/current/XXXX/"},
                        "label": {"type": "literal", "value": "fish"},
                        "definition": {"type": "literal", "value": "A unit-like placeholder"},
                    }
                ]
            }
        }

        with mock.patch.object(ts, "_safe_json", side_effect=lambda url, headers=None, timeout=30: fake_response) as mock_json:
            res = ts._search_nvs("fish", role="unit")

        called_url = mock_json.call_args[0][0]
        called_headers = mock_json.call_args[1]["headers"]
        self.assertIn("vocab.nerc.ac.uk/sparql/", called_url)
        self.assertIn("P01", called_url)
        self.assertIn("P06", called_url)
        self.assertEqual(called_headers["Accept"], "application/sparql-results+json")
        self.assertEqual(res.iloc[0]["source"], "nvs")
        self.assertEqual(res.iloc[0]["ontology"], "P06")

    def test_search_zooma_resolves_ols_terms(self):
        zooma_url = "https://www.ebi.ac.uk/spot/zooma/v2/api/services/annotate?propertyValue=spawner%20count"
        ols_url = "https://www.ebi.ac.uk/ols4/api/terms?iri=http%3A%2F%2Fexample.org%2Fterm"

        def fake_json(url, headers=None, timeout=30):
            if url == zooma_url:
                return [
                    {
                        "confidence": "MEDIUM",
                        "semanticTags": ["http://example.org/term"],
                        "_links": {"olslinks": [{"href": ols_url, "semanticTag": "http://example.org/term"}]},
                    }
                ]
            if url == ols_url:
                return {
                    "_embedded": {
                        "terms": [
                            {
                                "iri": "http://example.org/term",
                                "label": "Spawner count",
                                "ontology_name": "demo",
                                "description": ["A demo definition"],
                            }
                        ]
                    }
                }
            return None

        with mock.patch.object(ts, "_safe_json", side_effect=fake_json):
            res = find_terms("spawner count", sources=("zooma",))

        self.assertEqual(res.iloc[0]["source"], "zooma")
        self.assertEqual(res.iloc[0]["iri"], "http://example.org/term")
        self.assertEqual(res.iloc[0]["match_type"], "zooma_medium")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
