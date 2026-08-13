import unittest
from unittest import mock

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None

if pd is None:
    raise unittest.SkipTest("pandas not installed")

from metasalmonpy import apply_salmon_dictionary, infer_dictionary, validate_dictionary


class DictionaryTests(unittest.TestCase):
    def test_infer_and_validate_dictionary(self):
        df = pd.DataFrame(
            {
                "id": [1, 2],
                "when": pd.to_datetime(["2024-01-01", "2024-01-02"]),
                "count": [10, 20],
            }
        )
        dict_df = infer_dictionary(df, dataset_id="demo", table_id="observations")
        roles = dict_df.set_index("column_name")["column_role"].to_dict()
        types = dict_df.set_index("column_name")["value_type"].to_dict()

        self.assertEqual(roles["id"], "identifier")
        self.assertEqual(roles["when"], "temporal")
        self.assertEqual(roles["count"], "measurement")
        self.assertEqual(types["count"], "integer")
        validated = validate_dictionary(dict_df)
        self.assertIn("unit_iri", validated.columns)

    def test_apply_salmon_dictionary_with_codes(self):
        df = pd.DataFrame({"code": ["A", "B"], "value": [1, 2]})
        dict_df = pd.DataFrame(
            {
                "dataset_id": ["demo", "demo"],
                "table_id": ["tbl", "tbl"],
                "column_name": ["code", "value"],
                "column_label": ["code_label", "value_label"],
                "column_description": ["c", "v"],
                "column_role": ["categorical", "measurement"],
                "value_type": ["string", "integer"],
                "required": [True, False],
            }
        )
        codes = pd.DataFrame(
            {
                "table_id": ["tbl", "tbl"],
                "column_name": ["code", "code"],
                "code_value": ["A", "B"],
                "code_label": ["Alpha", "Beta"],
            }
        )
        result = apply_salmon_dictionary(df, dict_df, codes=codes, strict=True)
        self.assertIn("code_label", result.columns)
        self.assertIn("value_label", result.columns)
        self.assertTrue(isinstance(result["code_label"].dtype, pd.CategoricalDtype))
        self.assertEqual(list(result["code_label"].cat.categories), ["A", "B"])

    def test_validation_warnings_for_missing_semantic_fields_non_strict(self):
        bad = pd.DataFrame(
            {
                "dataset_id": ["d1", "d1"],
                "table_id": ["tbl", "tbl"],
                "column_name": ["id", "count"],
                "column_label": ["ID", "Count"],
                "column_description": ["row id", "spawners"],
                "column_role": ["identifier", "measurement"],
                "value_type": ["integer", "integer"],
                "required": [True, True],
            }
        )
        with self.assertWarnsRegex(
            UserWarning,
            "Hey, you definitely should fill those out before publishing",
        ):
            validate_dictionary(bad)

    def test_validation_requires_missing_semantic_fields_in_strict_mode(self):
        bad = pd.DataFrame(
            {
                "dataset_id": ["d1", "d1"],
                "table_id": ["tbl", "tbl"],
                "column_name": ["id", "count"],
                "column_label": ["ID", "Count"],
                "column_description": ["row id", "spawners"],
                "column_role": ["identifier", "measurement"],
                "value_type": ["integer", "integer"],
                "required": [True, True],
            }
        )
        with self.assertRaises(ValueError):
            validate_dictionary(bad, require_iris=True)

    def test_validation_catches_missing_required_columns(self):
        bad = pd.DataFrame({"column_name": ["x"]})
        with self.assertRaises(ValueError):
            validate_dictionary(bad)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
