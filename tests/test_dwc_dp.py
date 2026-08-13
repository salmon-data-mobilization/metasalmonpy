import unittest

import pandas as pd

from metasalmonpy.dwc_dp import suggest_dwc_mappings


class TestDwcDpMappings(unittest.TestCase):
    def test_suggest_dwc_mappings_returns_suggestions(self):
        dict_df = pd.DataFrame(
            {
                "column_name": ["event_date", "decimal_latitude", "scientific_name"],
                "column_label": ["Event Date", "Decimal Latitude", "Scientific Name"],
                "column_description": [
                    "Date the event occurred",
                    "Latitude in decimal degrees",
                    "Scientific name of the organism",
                ],
            }
        )

        res = suggest_dwc_mappings(dict_df, max_per_column=2)
        suggestions = res.attrs.get("dwc_mappings")

        self.assertIsNotNone(suggestions)
        self.assertFalse(suggestions.empty)
        self.assertIn("field_name", suggestions.columns)
        self.assertTrue(any(suggestions["field_name"] == "eventDate"))
        self.assertTrue(any(suggestions["field_name"] == "decimalLatitude"))
        self.assertTrue(any(suggestions["field_name"] == "scientificName"))

        # New tables: material and material-assertion
        dict_df2 = pd.DataFrame(
            {
                "column_name": ["material_entity_id", "assertion_value_numeric"],
                "column_label": ["Material Entity ID", "Assertion Value Numeric"],
                "column_description": [
                    "Identifier for the material entity",
                    "Numeric assertion value",
                ],
            }
        )
        res2 = suggest_dwc_mappings(dict_df2, max_per_column=2)
        suggestions2 = res2.attrs.get("dwc_mappings")
        self.assertIsNotNone(suggestions2)
        self.assertFalse(suggestions2.empty)
        self.assertTrue(any(suggestions2["field_name"] == "materialEntityID"))
        self.assertTrue(any(suggestions2["field_name"] == "assertionValueNumeric"))


if __name__ == "__main__":
    unittest.main()
