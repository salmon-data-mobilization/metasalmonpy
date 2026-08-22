import unittest

import pandas as pd

from metasalmonpy import (
    nuseds_enumeration_method_crosswalk,
    nuseds_estimate_classification_crosswalk,
    nuseds_estimate_method_crosswalk,
)


class NusedsCrosswalkTests(unittest.TestCase):
    def test_enumeration_crosswalk_structure(self):
        cross = nuseds_enumeration_method_crosswalk()
        self.assertTrue({"nuseds_value", "method_family", "ontology_term", "notes"}.issubset(cross.columns))
        self.assertIn("Bank Walk", set(cross["nuseds_value"]))
        self.assertIn("unknown", set(cross["method_family"]))
        self.assertIn("FS", set(cross["method_family"]))

    def test_estimate_crosswalk_structure(self):
        cross = nuseds_estimate_method_crosswalk()
        self.assertTrue({"nuseds_value", "method_family", "guidance_interpretation", "ontology_term", "notes"}.issubset(cross.columns))
        self.assertIn("Sonar-ARIS", set(cross["nuseds_value"]))
        self.assertIn("depends", set(cross["method_family"]))
        self.assertIn("M", set(cross["method_family"]))

    def test_classification_crosswalk_maps_the_hyatt_types(self):
        # Hub backlog #101: ESTIMATE_CLASSIFICATION appeared nowhere in the
        # package although the released gcdfo 0.0.9 ships Type1-Type6 as
        # skos:Concepts under gcdfo:EstimateType, labelled to match the NuSEDS
        # strings (Hyatt 1997). A wiring gap, not an ontology gap — it must
        # not be filed as a term request. Mirrors metasalmon main's
        # test-nuseds-method-crosswalk.R.
        cross = nuseds_estimate_classification_crosswalk()

        self.assertEqual(
            list(cross.columns),
            ["nuseds_value", "estimate_type", "ontology_term", "notes"],
        )

        lookup = dict(zip(cross["nuseds_value"], cross["ontology_term"]))
        self.assertEqual(lookup["TRUE ABUNDANCE (TYPE-1)"], "gcdfo:Type1")
        self.assertEqual(lookup["TRUE ABUNDANCE (TYPE-2)"], "gcdfo:Type2")
        self.assertEqual(lookup["RELATIVE ABUNDANCE (TYPE-3)"], "gcdfo:Type3")
        self.assertEqual(lookup["RELATIVE ABUNDANCE (TYPE-4)"], "gcdfo:Type4")
        self.assertEqual(lookup["RELATIVE ABUNDANCE (TYPE-5)"], "gcdfo:Type5")
        self.assertEqual(lookup["PRESENCE-ABSENCE (TYPE-6)"], "gcdfo:Type6")

        # NO SURVEY THIS YEAR is an absence-of-observation marker, not an
        # estimate type: mapping it to any Type concept would be wrong, and
        # the crosswalk records that disposition instead of forcing a term.
        self.assertTrue(pd.isna(lookup["NO SURVEY THIS YEAR"]))
        no_survey_note = cross.loc[
            cross["nuseds_value"] == "NO SURVEY THIS YEAR", "notes"
        ].iloc[0]
        self.assertIn("absence", no_survey_note.lower())
        self.assertTrue(pd.isna(lookup["UNKNOWN"]))

        # The multi-year relative classifications have no released concept of
        # their own; they link at scheme level, the same convention the
        # estimate-method crosswalk uses for Cumulative CPUE.
        self.assertEqual(
            lookup["RELATIVE: CONSTANT MULTI-YEAR METHODS"],
            "gcdfo:EstimateType",
        )
        self.assertEqual(
            lookup["RELATIVE: VARYING MULTI-YEAR METHODS"],
            "gcdfo:EstimateType",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
