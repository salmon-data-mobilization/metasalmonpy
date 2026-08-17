import unittest

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None

if pd is None:
    raise unittest.SkipTest("pandas not installed")

from metasalmonpy import apply_semantic_suggestions, infer_dictionary, suggest_semantics
from metasalmonpy.dwc_dp import suggest_dwc_mappings
from metasalmonpy.semantics import _table_suggestion_is_compatible


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


class EraSemanticMatchingTests(unittest.TestCase):
    """metasalmon 0.1.7's semantic-matching corrections, verified against v0.1.7."""

    # Every (column, role) -> query below is what era R's suggest_semantics()
    # asked a stub search_fn for, driven from the same dictionary rows. 0.1.7
    # widened the organism vocabulary (smolt/fry/juvenile) and added the
    # recruit/smolt/fry and effective-female-spawner whole-variable queries.
    QUERY_CASES = (
        ("total_smolts", "Total smolts enumerated at the fence.", "integer",
         {"variable": "smolt abundance", "property": "count"}),
        ("fry_total", "Total fry counted.", "integer",
         {"variable": "fry abundance", "property": "count"}),
        ("recruits", "Recruits returning to the river.", "integer",
         {"variable": "recruit abundance", "property": "count"}),
        ("juvenile_total", "Total juveniles captured.", "integer",
         {"variable": "count", "property": "count"}),
        ("smolt_abundance", "Smolt abundance estimate.", "number",
         {"variable": "smolt abundance", "property": "abundance"}),
        ("spawner_count", "Spawner count for the stream.", "integer",
         {"variable": "spawner abundance", "property": "spawner abundance"}),
        ("adult_spawners", "Adult spawner abundance.", "integer",
         {"variable": "adult spawner count", "property": "spawner abundance"}),
        ("effective_female_spawners",
         "Effective female spawners after egg retention.", "number",
         {"variable": "effective female spawner abundance",
          "property": "spawner abundance"}),
        # Not count-like in era R: "eggs"/"females" are not organism tokens
        # and "spawned" is not "spawner", so the base query survives intact.
        ("eggs_not_spawned", "Eggs not spawned by females.", "number",
         {"variable": "Eggs not spawned by females",
          "property": "Eggs not spawned by females"}),
        ("gear_code", "Gear code used for the survey.", "string",
         {"variable": "Gear code used for the survey",
          "property": "Gear code used for the survey"}),
        # R runs the count-like *test* over name + label + base query but
        # shapes from the base query alone, so a life stage that appears only
        # in the column name does not reach the query.
        ("smolt_count", "Total fish enumerated at the fence.", "integer",
         {"variable": "count", "property": "count"}),
        ("recruit_index", "Total adults returning.", "integer",
         {"variable": "count", "property": "count"}),
        ("spawner_metric", "Effective female counts recorded.", "number",
         {"variable": "count", "property": "count"}),
        ("abundance_x", "Total salmon observed.", "integer",
         {"variable": "count", "property": "count"}),
    )

    def test_measurement_queries_match_era_r(self):
        from metasalmonpy.semantics import _measurement_query

        for name, description, value_type, expected in self.QUERY_CASES:
            row = {
                "column_name": name,
                "column_label": name,
                "column_description": description,
                "value_type": value_type,
                "unit_label": pd.NA,
            }
            base_query = " ".join(description.rstrip(".").split())
            for role, query in expected.items():
                with self.subTest(column=name, role=role):
                    self.assertEqual(
                        _measurement_query(row, role, base_query)[0], query
                    )

    def test_whole_variable_terms_keep_their_native_ontology_type(self):
        # 0.1.7 stopped stamping every accepted variable term "skos_concept".
        dictionary = infer_dictionary(
            pd.DataFrame({"count": [1, 2]}), dataset_id="d", table_id="t"
        )
        dictionary.loc[0, "column_role"] = "measurement"

        def suggestion(**overrides):
            base = {
                "dataset_id": "d",
                "table_id": "t",
                "column_name": "count",
                "dictionary_role": "variable",
                "iri": "https://example.org/term",
            }
            base.update(overrides)
            return pd.DataFrame([base])

        cases = (
            ({"type_iris": "http://www.w3.org/2002/07/owl#Class"}, "owl_class"),
            ({"resource_kind": "Class"}, "owl_class"),
            (
                {"type_iris": "http://www.w3.org/2002/07/owl#ObjectProperty"},
                "owl_object_property",
            ),
            ({"resource_kind": "Concept"}, "skos_concept"),
            ({}, "skos_concept"),
            ({"term_type": "owl_class"}, "owl_class"),
        )
        for overrides, expected in cases:
            with self.subTest(**overrides):
                applied = apply_semantic_suggestions(
                    dictionary, suggestion(**overrides), verbose=False
                )
                self.assertEqual(applied.loc[0, "term_iri"], "https://example.org/term")
                self.assertEqual(applied.loc[0, "term_type"], expected)

    def test_an_explicit_source_list_filters_results_as_well_as_requests(self):
        # 0.1.7 made an explicit source list a strict allowlist on the way out:
        # a search that answers from an undeclared source contributes nothing.
        dictionary = infer_dictionary(
            pd.DataFrame({"count": [1, 2]}), dataset_id="d", table_id="t"
        )
        dictionary.loc[0, "column_role"] = "measurement"

        def rogue_search(query, role=None, sources=None):
            return pd.DataFrame(
                {
                    "label": ["rogue"],
                    "iri": ["https://example.org/rogue"],
                    "source": ["wikidata"],
                    "ontology": ["wikidata"],
                    "role": [role],
                    "match_type": ["label"],
                    "definition": ["A term from a source nobody asked for."],
                    "score": [9.0],
                }
            )

        bounded = suggest_semantics(
            pd.DataFrame({"count": [1, 2]}),
            dictionary,
            sources=["smn"],
            search_fn=rogue_search,
        )
        self.assertTrue(bounded.attrs["semantic_suggestions"].empty)

        # With no explicit list the role defaults apply and nothing is filtered.
        unbounded = suggest_semantics(
            pd.DataFrame({"count": [1, 2]}),
            dictionary,
            search_fn=rogue_search,
        )
        self.assertFalse(unbounded.attrs["semantic_suggestions"].empty)


class ReviewedStrategyTests(unittest.TestCase):
    """``strategy="reviewed"`` and multiple accepted constraints (0.1.8).

    Expectations in ``tests/data/sdp-extensions/expected-apply-strategies.json``
    are metasalmon **v0.1.8**'s ``apply_semantic_suggestions()`` output for the
    identical dictionary and suggestion frame, for all three strategies.
    """

    SPAWNER_STAGE = "https://w3id.org/smn/SpawnerStageContext"
    FEMALE_SEX = "https://example.org/constraint/FemaleSex"

    def setUp(self):
        import json
        import os

        data = os.path.join(
            os.path.dirname(__file__),
            "data",
            "sdp-extensions",
            "expected-apply-strategies.json",
        )
        with open(data, encoding="utf-8") as handle:
            self.expected = json.load(handle)

        self.dictionary = pd.DataFrame(
            [
                {
                    "dataset_id": "d1",
                    "table_id": "t1",
                    "column_name": "effective_female_spawners",
                    "column_label": "Effective female spawners",
                    "column_description": (
                        "Spawner abundance qualified by spawner stage and "
                        "female sex"
                    ),
                    "column_role": "measurement",
                    "value_type": "number",
                    "required": False,
                }
            ]
        )
        self.suggestions = pd.DataFrame(
            {
                "dataset_id": "d1",
                "table_id": "t1",
                "column_name": "effective_female_spawners",
                "dictionary_role": "constraint",
                "target_scope": "column",
                "target_sdp_file": "column_dictionary.csv",
                "target_sdp_field": "constraint_iri",
                "search_query": [
                    "spawner stage",
                    "female sex",
                    "spawner stage",
                    "brood year",
                ],
                "iri": [
                    self.SPAWNER_STAGE,
                    self.FEMALE_SEX,
                    self.SPAWNER_STAGE,
                    "https://example.org/constraint/BroodYear",
                ],
                "decision": ["accepted", "accepted", "accepted", "rejected"],
                "llm_selected": [True, True, True, False],
                "llm_decision": ["accept", "accept", "accept", "reject"],
                "llm_confidence": [0.98, 0.97, 0.98, 0.94],
            }
        )

    def _apply(self, strategy):
        applied = apply_semantic_suggestions(
            self.dictionary,
            suggestions=self.suggestions,
            strategy=strategy,
            verbose=False,
        )
        return applied["constraint_iri"].iloc[0]

    def test_all_three_strategies_match_r(self):
        for strategy in ("top", "reviewed", "llm"):
            with self.subTest(strategy=strategy):
                self.assertEqual(self._apply(strategy), self.expected[strategy])

    def test_reviewed_keeps_every_accepted_constraint(self):
        # An effective-female-spawner count is qualified by BOTH constraints;
        # dropping either silently changes what the column means.
        self.assertEqual(
            self._apply("reviewed"),
            "; ".join([self.SPAWNER_STAGE, self.FEMALE_SEX]),
        )

    def test_reviewed_deduplicates_in_first_occurrence_order(self):
        # The spawner-stage IRI appears twice among the accepted rows.
        self.assertEqual(self._apply("reviewed").count(self.SPAWNER_STAGE), 1)

    def test_reviewed_ignores_rows_that_were_not_accepted(self):
        self.assertNotIn("BroodYear", self._apply("reviewed"))

    def test_top_stays_single_winner(self):
        self.assertEqual(self._apply("top"), self.SPAWNER_STAGE)

    def test_accept_is_accepted_case_insensitively(self):
        self.suggestions["decision"] = ["  ACCEPT ", "Accepted", "accept", "no"]
        self.assertEqual(
            self._apply("reviewed"),
            "; ".join([self.SPAWNER_STAGE, self.FEMALE_SEX]),
        )

    def test_reviewed_requires_a_decision_column(self):
        suggestions = self.suggestions.drop(columns=["decision"])
        with self.assertRaises(ValueError) as caught:
            apply_semantic_suggestions(
                self.dictionary,
                suggestions=suggestions,
                strategy="reviewed",
                verbose=False,
            )
        self.assertIn("explicit review decisions", str(caught.exception))

    def test_a_non_constraint_role_stays_single_valued(self):
        suggestions = self.suggestions.copy()
        suggestions["dictionary_role"] = "property"
        suggestions["target_sdp_field"] = "property_iri"
        applied = apply_semantic_suggestions(
            self.dictionary,
            suggestions=suggestions,
            strategy="reviewed",
            verbose=False,
        )
        self.assertEqual(applied["property_iri"].iloc[0], self.SPAWNER_STAGE)

    def test_an_unknown_strategy_is_refused(self):
        with self.assertRaises(ValueError):
            apply_semantic_suggestions(
                self.dictionary,
                suggestions=self.suggestions,
                strategy="whatever",
                verbose=False,
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
