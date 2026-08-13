import unittest

from metasalmonpy import nuseds_enumeration_method_crosswalk, nuseds_estimate_method_crosswalk


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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
