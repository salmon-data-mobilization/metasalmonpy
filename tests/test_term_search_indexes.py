"""
Offline tests for the smn/gcdfo term indexes (metasalmon 0.1.6 parity, PR 0).

All ontology fetches are monkeypatched; fixture TTL/OWL snippets live in
tests/data/ and are trimmed copies of real salmon-domain-ontology modules and
the gcdfo OWL document.
"""

import os
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
from metasalmonpy.term_search_smn import SMN_INDEX_COLUMNS, parse_smn_ttl_modules

_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

_INDEX_CONTRACT = [
    "iri",
    "label",
    "alt_labels",
    "definition",
    "resource_kind",
    "in_scheme",
    "parent_iris",
    "type_iris",
    "search_text",
    "is_variable",
    "is_property",
    "is_entity",
    "is_constraint",
    "is_method",
    "is_statistical_modifier",
    "role_hints",
]

_PREFIX_ONLY_TTL = (
    "@prefix smn: <https://w3id.org/smn/> .\n"
    "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
)

_ONTOLOGY_ONLY_RDFXML = (
    '<?xml version="1.0"?>\n'
    '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"\n'
    '     xmlns:owl="http://www.w3.org/2002/07/owl#">\n'
    '    <owl:Ontology rdf:about="https://w3id.org/smn"/>\n'
    "</rdf:RDF>\n"
)


def _fixture(name: str) -> str:
    with open(os.path.join(_DATA_DIR, name), "r", encoding="utf-8") as fp:
        return fp.read()


def _smn_fetch(url: str, accept: str, fallback_urls=(), timeout=30) -> str:
    """Serve fixture TTL for three modules; prefix-only TTL for the rest."""
    fixtures = {
        "https://w3id.org/smn/modules/01-entity-systematics": "smn-module-01-entity-systematics.ttl",
        "https://w3id.org/smn/modules/02-observation-measurement": "smn-module-02-observation-measurement.ttl",
        "https://w3id.org/smn/modules/07-controlled-vocabularies": "smn-module-07-controlled-vocabularies.ttl",
    }
    if url in fixtures:
        return _fixture(fixtures[url])
    return _PREFIX_ONLY_TTL


def _gcdfo_fetch(url: str, accept: str, fallback_urls=(), timeout=30) -> str:
    return _fixture("gcdfo-sample.owl")


def _failing_fetch(url: str, accept: str, fallback_urls=(), timeout=30) -> str:
    raise RuntimeError(f"Failed to fetch ontology from {url}; last error: HTTP 404")


class SmnTermIndexTests(unittest.TestCase):
    def _index(self) -> pd.DataFrame:
        with mock.patch.object(ts, "_fetch_ontology_text", side_effect=_smn_fetch):
            return ts._smn_term_index()

    def _row(self, index: pd.DataFrame, iri: str) -> pd.Series:
        rows = index[index["iri"] == iri]
        self.assertEqual(len(rows), 1, f"expected exactly one row for {iri}")
        return rows.iloc[0]

    def test_index_columns_match_contract(self):
        index = self._index()
        self.assertEqual(list(index.columns), _INDEX_CONTRACT)
        self.assertEqual(list(SMN_INDEX_COLUMNS), _INDEX_CONTRACT)
        self.assertFalse(index.empty)

    def test_statistical_modifier_concept_gets_modifier_hint(self):
        index = self._index()
        row = self._row(index, "https://w3id.org/smn/MeanStatisticalModifier")
        self.assertEqual(row["label"], "Mean")
        self.assertTrue(row["is_statistical_modifier"])
        self.assertIn("statistical_modifier", row["role_hints"].split("|"))
        # Module 07 concepts also carry the broad constraint hint.
        self.assertIn("constraint", row["role_hints"].split("|"))

    def test_controlled_vocabulary_concept_gets_constraint_hint(self):
        index = self._index()
        row = self._row(index, "https://w3id.org/smn/OceanPhase")
        self.assertTrue(row["is_constraint"])
        self.assertIn("constraint", row["role_hints"].split("|"))
        self.assertIn("Marine phase", row["alt_labels"])

    def test_modifier_token_fallback_confined_to_controlled_vocab_module(self):
        index = self._index()
        # "Total length" (module 02) contains the token "total" but must not
        # pick up a statistical-modifier hint outside module 07.
        total_length = self._row(index, "https://w3id.org/smn/totalLength")
        self.assertFalse(total_length["is_statistical_modifier"])
        self.assertNotIn("statistical_modifier", total_length["role_hints"].split("|"))
        # "Abundance" mentions "statistical modifiers" only in its prose
        # definition, which is not evidence about the term itself.
        abundance = self._row(index, "https://w3id.org/smn/Abundance")
        self.assertFalse(abundance["is_statistical_modifier"])
        self.assertTrue(abundance["is_variable"])

    def test_entity_concept_gets_entity_hint(self):
        index = self._index()
        row = self._row(index, "https://w3id.org/smn/Population")
        self.assertTrue(row["is_entity"])
        self.assertIn("entity", row["role_hints"].split("|"))

    def test_known_label_findable_through_find_terms(self):
        with mock.patch.object(ts, "_fetch_ontology_text", side_effect=_smn_fetch):
            res = find_terms("population", role="entity", sources=("smn",))
        self.assertFalse(res.empty)
        self.assertEqual(res.iloc[0]["iri"], "https://w3id.org/smn/Population")
        self.assertEqual(res.iloc[0]["source"], "smn")
        self.assertEqual(res.iloc[0]["ontology"], "smn")
        self.assertIn("entity", str(res.iloc[0]["role_hints"]).split("|"))

    def test_index_raises_when_fetch_fails(self):
        with mock.patch.object(ts, "_fetch_ontology_text", side_effect=_failing_fetch):
            with self.assertRaises(RuntimeError):
                ts._smn_term_index()

    def test_index_raises_when_everything_parses_empty(self):
        def empty_fetch(url, accept, fallback_urls=(), timeout=30):
            if "modules" in url:
                return _PREFIX_ONLY_TTL
            return _ONTOLOGY_ONLY_RDFXML

        with mock.patch.object(ts, "_fetch_ontology_text", side_effect=empty_fetch):
            with self.assertRaises(RuntimeError):
                ts._smn_term_index()

    def test_failed_fetch_surfaces_as_error_diagnostic_not_empty_success(self):
        with mock.patch.object(ts, "_fetch_ontology_text", side_effect=_failing_fetch):
            res = find_terms("population", role="entity", sources=("smn",))
        self.assertTrue(res.empty)
        diagnostics = res.attrs["diagnostics"]
        self.assertEqual(diagnostics.iloc[0]["status"], "error")
        self.assertIn("HTTP 404", diagnostics.iloc[0]["error"])

    def test_parse_is_pure_function_of_module_texts(self):
        texts = {
            "https://w3id.org/smn/modules/07-controlled-vocabularies": _fixture(
                "smn-module-07-controlled-vocabularies.ttl"
            )
        }
        first = parse_smn_ttl_modules(texts)
        second = parse_smn_ttl_modules(texts)
        pd.testing.assert_frame_equal(first, second)
        self.assertEqual(
            list(first["iri"]),
            [
                "https://w3id.org/smn/OceanPhase",
                "https://w3id.org/smn/StatisticalModifierScheme",
                "https://w3id.org/smn/MeanStatisticalModifier",
            ],
        )


class GcdfoTermIndexTests(unittest.TestCase):
    def _index(self) -> pd.DataFrame:
        with mock.patch.object(ts, "_fetch_ontology_text", side_effect=_gcdfo_fetch):
            return ts._gcdfo_term_index()

    def _row(self, index: pd.DataFrame, iri: str) -> pd.Series:
        rows = index[index["iri"] == iri]
        self.assertEqual(len(rows), 1, f"expected exactly one row for {iri}")
        return rows.iloc[0]

    def test_index_columns_match_contract(self):
        index = self._index()
        self.assertEqual(list(index.columns), _INDEX_CONTRACT)
        self.assertFalse(index.empty)

    def test_iadopt_targets_drive_role_flags(self):
        index = self._index()
        metric = self._row(index, "https://w3id.org/gcdfo/salmon#AbundancePercentileMetric")
        self.assertTrue(metric["is_variable"])
        self.assertIn("variable", metric["role_hints"].split("|"))

        entity = self._row(index, "https://w3id.org/gcdfo/salmon#ConservationUnit")
        self.assertTrue(entity["is_entity"])
        self.assertIn("entity", entity["role_hints"].split("|"))
        self.assertEqual(entity["resource_kind"], "Class")

        prop = self._row(index, "https://w3id.org/gcdfo/salmon#SpawnerAbundancePercentile")
        self.assertTrue(prop["is_property"])
        self.assertIn("property", prop["role_hints"].split("|"))

        method = self._row(index, "https://w3id.org/gcdfo/salmon#WSPRapidStatusAssessmentMethod")
        self.assertTrue(method["is_method"])
        self.assertIn("method", method["role_hints"].split("|"))

    def test_constraint_and_label_fallback(self):
        index = self._index()
        constraint = self._row(index, "https://w3id.org/gcdfo/salmon#WildSalmonPolicy")
        self.assertTrue(constraint["is_constraint"])
        self.assertIn("constraint", constraint["role_hints"].split("|"))
        # No rdfs:label / skos:prefLabel: the camel-cased local name fills in.
        self.assertEqual(constraint["label"], "Wild Salmon Policy")

    def test_statistical_modifier_scheme_concept_gets_modifier_hint(self):
        index = self._index()
        row = self._row(index, "https://w3id.org/gcdfo/salmon#GenerationalMean")
        self.assertTrue(row["is_statistical_modifier"])
        self.assertIn("statistical_modifier", row["role_hints"].split("|"))

    def test_iri_pattern_excludes_foreign_namespaces(self):
        index = self._index()
        self.assertNotIn("https://w3id.org/smn/MeasurementContext", set(index["iri"]))
        self.assertTrue(index["iri"].str.startswith("https://w3id.org/gcdfo/salmon#").all())

    def test_known_label_findable_through_find_terms(self):
        with mock.patch.object(ts, "_fetch_ontology_text", side_effect=_gcdfo_fetch):
            res = find_terms("abundance percentile", role="variable", sources=("gcdfo",))
        self.assertFalse(res.empty)
        self.assertEqual(
            res.iloc[0]["iri"], "https://w3id.org/gcdfo/salmon#AbundancePercentileMetric"
        )
        self.assertEqual(res.iloc[0]["source"], "gcdfo")
        self.assertIn("variable", str(res.iloc[0]["role_hints"]).split("|"))

    def test_index_raises_when_fetch_fails(self):
        with mock.patch.object(ts, "_fetch_ontology_text", side_effect=_failing_fetch):
            with self.assertRaises(RuntimeError):
                ts._gcdfo_term_index()

    def test_index_raises_when_parse_is_empty(self):
        with mock.patch.object(
            ts, "_fetch_ontology_text", return_value=_ONTOLOGY_ONLY_RDFXML
        ):
            with self.assertRaises(RuntimeError):
                ts._gcdfo_term_index()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class TermIndexSessionCacheTests(unittest.TestCase):
    """An index already resolved in this session is returned without touching
    the network or the parser (metasalmon 0.2.2).

    The stamp check used to sit *after* the fetch and the parse on the R side,
    so every ``find_terms()`` call paid 11 conditional GETs and a full reparse
    before discovering nothing had changed; here there was no session cache at
    all, which is the same cost. The conftest fixture clears the caches around
    every test, so each test below owns its whole session.
    """

    def _counting(self, inner):
        calls = {"n": 0}

        def fetch(url, accept, fallback_urls=(), timeout=30):
            calls["n"] += 1
            return inner(url, accept, fallback_urls, timeout)

        return fetch, calls

    def test_smn_index_is_resolved_once_per_session(self):
        fetch, calls = self._counting(_smn_fetch)
        with mock.patch.object(ts, "_fetch_ontology_text", side_effect=fetch):
            first = ts._smn_term_index()
            first_cost = calls["n"]
            second = ts._smn_term_index()
        self.assertGreater(first_cost, 0)
        self.assertEqual(calls["n"], first_cost)
        self.assertIs(second, first)

    def test_gcdfo_index_is_resolved_once_per_session(self):
        fetch, calls = self._counting(_gcdfo_fetch)
        with mock.patch.object(ts, "_fetch_ontology_text", side_effect=fetch):
            first = ts._gcdfo_term_index()
            second = ts._gcdfo_term_index()
        self.assertEqual(calls["n"], 1)
        self.assertIs(second, first)

    def test_refresh_bypasses_and_replaces_the_session_cache(self):
        # The trade is deliberate: a module updated upstream mid-session is
        # not picked up until refresh=True — the stronger guarantee for
        # seeding, where two columns in one package must not be seeded
        # against two different ontology versions.
        fetch, calls = self._counting(_gcdfo_fetch)
        with mock.patch.object(ts, "_fetch_ontology_text", side_effect=fetch):
            ts._gcdfo_term_index()
            ts._gcdfo_term_index(refresh=True)
            ts._gcdfo_term_index()
        self.assertEqual(calls["n"], 2)

    def test_a_failed_resolve_is_not_cached(self):
        # An outage must not freeze an error (or an empty index) for the rest
        # of the session; the next call tries again.
        with mock.patch.object(ts, "_fetch_ontology_text", side_effect=_failing_fetch):
            with self.assertRaises(RuntimeError):
                ts._gcdfo_term_index()
        with mock.patch.object(ts, "_fetch_ontology_text", side_effect=_gcdfo_fetch):
            index = ts._gcdfo_term_index()
        self.assertFalse(index.empty)

    def test_find_terms_pays_no_fetch_after_the_first_call(self):
        fetch, calls = self._counting(_smn_fetch)
        with mock.patch.object(ts, "_fetch_ontology_text", side_effect=fetch):
            find_terms("population", role="entity", sources=("smn",))
            first_cost = calls["n"]
            find_terms("spawner", role="entity", sources=("smn",))
        self.assertEqual(calls["n"], first_cost)


class NonTurtleModuleTests(unittest.TestCase):
    """One corrupted module must not silently drop its terms (PR 6 review)."""

    def test_non_turtle_module_raises_instead_of_silently_dropping(self):
        # A module body without a single @prefix is not Turtle (an error page
        # served with 200, or a format change); skipping it would silently
        # omit every term it carries while the aggregate looks healthy.
        good = _fixture("smn-module-01-entity-systematics.ttl")
        with self.assertRaisesRegex(RuntimeError, "non-Turtle content"):
            parse_smn_ttl_modules(
                {
                    "https://w3id.org/smn/modules/01-entity-systematics": good,
                    "https://w3id.org/smn/modules/02-observation-measurement": (
                        "<html>Service unavailable</html>"
                    ),
                }
            )

    def test_zero_row_bridge_module_is_legitimate(self):
        # The RDA profile-bridge modules hold foreign-subject statements only
        # (smn CONVENTIONS 5b), so real Turtle yielding zero smn-subject rows
        # must NOT raise -- R indexes those modules to zero rows too.
        good = _fixture("smn-module-01-entity-systematics.ttl")
        bridge = (
            "@prefix smn: <https://w3id.org/smn/> .\n"
            "@prefix ext: <https://example.org/profile/> .\n\n"
            "ext:ForeignTerm a ext:Bridge ;\n"
            "    ext:maps smn:Stock .\n"
        )
        index = parse_smn_ttl_modules(
            {
                "https://w3id.org/smn/modules/01-entity-systematics": good,
                "https://w3id.org/smn/modules/08-rda-case-study-profile-bridges": bridge,
            }
        )
        self.assertGreater(len(index), 0)

    def test_bad_module_triggers_rdfxml_fallback(self):
        # The index builder recovers through the root RDF/XML serialization,
        # which carries the whole ontology, so search sees no partial index.
        gcdfo_xml = _fixture("gcdfo-sample.owl")

        def fake_fetch(url, accept=None, fallback_urls=None):
            if "modules" in url:
                return "<html>Service unavailable</html>"
            return gcdfo_xml

        with mock.patch.object(ts, "_fetch_ontology_text", side_effect=fake_fetch), \
                mock.patch.object(ts, "_SMN_IRI_PATTERN", ts._GCDFO_IRI_PATTERN):
            index = ts._smn_term_index()
        self.assertFalse(index.empty)
