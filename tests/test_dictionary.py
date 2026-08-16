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


class EraColumnRoleTests(unittest.TestCase):
    """Column-role and required inference, node for node with metasalmon 0.1.7.

    Every expectation below is the answer era R gives. They were produced by
    calling ``infer_column_role()`` and ``.ms_infer_required_flag()`` on a
    v0.1.7 extraction (``git archive v0.1.7``) under R 4.5.2 with the same
    thirty name/value pairs; before this port thirteen of the thirty differed.
    """

    # (column name, values, R's column_role, R's required)
    CASES = (
        # 0.1.7's terminal-ID-qualifier fix: a qualifier token after the last
        # ID/key token means the column describes an identification's quality.
        ("stock_ID_quality", ["high", "low", "high"], "attribute", None),
        ("id_quality", ["a", "b", "c"], "attribute", None),
        ("key_confidence", ["a", "b", "c"], "attribute", None),
        ("sample_id_score", ["a", "b", "c"], "attribute", None),
        ("sampleIdQuality", ["a", "b", "c"], "attribute", None),
        # 0.1.7's nullable-identifier fix: an identifier carrying a missing or
        # blank-after-trim value is undecided, not required.
        ("fish_id", ["a", "b", "c"], "identifier", True),
        ("sample_id", ["a", None, "c"], "identifier", None),
        ("sample_id", ["a", " ", "c"], "identifier", None),
        ("dup_id", ["x", "x", "y"], "identifier", True),
        ("key", ["a", "b", "c"], "identifier", True),
        # The rest of the 0.1.7 role heuristic.
        ("station_number", ["1", "2", "3"], "identifier", True),
        ("release_no", ["1", "2", "3"], "identifier", True),
        ("counting_method", ["visual", "weir", "visual"], "attribute", None),
        ("gear", ["net", "trap", "net"], "attribute", None),
        ("sample_size", [10, 20, 30], "measurement", None),
        ("survey_year", ["2001", "2002", "2003"], "temporal", None),
        ("run_year", [2001, 2002, 2003], "temporal", None),
        ("spawner_count", [10, 20, 30], "measurement", None),
        ("escapement", [10, 20, 30], "measurement", None),
        ("total_length_mm", [10.5, 20.5, 30.5], "measurement", None),
        ("water_temp", ["8.1", "9.2", "10.3"], "measurement", None),
        ("discharge (m3/s)", ["1.2", "2.3", "3.4"], "measurement", None),
        ("comment", ["a", "b", "c"], "attribute", None),
        ("abundance", ["n/a", "n/a", "n/a"], "attribute", None),
        ("count", ["5%", "10%", "15%"], "measurement", None),
        ("survey_date", ["2020-01-01", "2020-02-01", "2020-03-01"], "temporal", None),
        ("region", ["N", "S", "N"], "attribute", None),
        ("proportion_female", [0.4, 0.5, 0.6], "measurement", None),
        ("mortality", ["low", "high", "low"], "attribute", None),
        ("recruit_abundance", [1, 2, 3], "measurement", None),
    )

    def test_roles_and_required_flags_match_era_r(self):
        from metasalmonpy.dictionary import infer_column_role, infer_required_flag

        for name, values, role, required in self.CASES:
            with self.subTest(column=name, values=values):
                series = pd.Series(values)
                got_role = infer_column_role(name, series)
                self.assertEqual(got_role, role)
                self.assertEqual(infer_required_flag(name, series, got_role), required)

    def test_a_categorical_qualifier_keeps_its_factor_intent(self):
        # R returns "categorical" rather than "attribute" when the qualifier
        # column is a factor.
        from metasalmonpy.dictionary import infer_column_role

        series = pd.Series(["high", "low"], dtype="category")
        self.assertEqual(infer_column_role("stock_id_quality", series), "categorical")

    def test_a_nullable_identifier_is_not_declared_required(self):
        frame = pd.DataFrame({"sample_id": ["a", None, "c"], "note": ["x", "y", "z"]})
        dictionary = infer_dictionary(frame, dataset_id="d", table_id="t")
        row = dictionary.loc[dictionary["column_name"] == "sample_id"].iloc[0]
        self.assertEqual(row["column_role"], "identifier")
        self.assertTrue(pd.isna(row["required"]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
