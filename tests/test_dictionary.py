import unittest
import warnings
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
        # The labels in code_label, as metasalmon's factor(levels = code_value,
        # labels = code_label) gives them (hub item B-274).
        self.assertEqual(list(result["code_label"].cat.categories), ["Alpha", "Beta"])
        self.assertEqual(result["code_label"].tolist(), ["Alpha", "Beta"])

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
    """Column-role and required inference, node for node with metasalmon.

    Every expectation below is the answer R gives. The thirty pairs were
    produced by calling ``infer_column_role()`` and ``.ms_infer_required_flag()``
    on a ``git archive v0.1.7`` extraction under R 4.5.2; before that port
    thirteen of the thirty differed.

    **Re-measured 2026-09-16 against an installed metasalmon 0.5.0** (R 4.3.3)
    for hub queue **B-125**, the port of metasalmon backlog #95: an enumerable
    string column is ``categorical`` where three branches of
    ``infer_column_role()`` answered ``attribute``. Eleven rows moved, each
    marked ``#95`` below, and **each moved in R first** — R and Python were run
    over the same thirty-one name/value pairs and agree on every row, role and
    required flag alike. No row here was edited to fit the Python change.
    """

    # (column name, values, R's column_role, R's required)
    CASES = (
        # 0.1.7's terminal-ID-qualifier fix: a qualifier token after the last
        # ID/key token means the column describes an identification's quality.
        # #95: all five carry an enumerable code list, so the qualifier branch
        # answers categorical. What the 0.1.7 fix asserts -- that none of them
        # is an *identifier* -- is untouched.
        ("stock_ID_quality", ["high", "low", "high"], "categorical", None),
        ("id_quality", ["a", "b", "c"], "categorical", None),
        ("key_confidence", ["a", "b", "c"], "categorical", None),
        ("sample_id_score", ["a", "b", "c"], "categorical", None),
        ("sampleIdQuality", ["a", "b", "c"], "categorical", None),
        # 0.1.7's nullable-identifier fix: an identifier carrying a missing or
        # blank-after-trim value is undecided, not required.
        ("fish_id", ["a", "b", "c"], "identifier", True),
        ("sample_id", ["a", None, "c"], "identifier", None),
        ("sample_id", ["a", " ", "c"], "identifier", None),
        ("dup_id", ["x", "x", "y"], "identifier", True),
        ("key", ["a", "b", "c"], "identifier", True),
        # The rest of the role heuristic.
        ("station_number", ["1", "2", "3"], "identifier", True),
        ("release_no", ["1", "2", "3"], "identifier", True),
        # #95: a method column whose values enumerate is a code list, and its
        # procedures resolve through codes.csv$term_iri. The point of these two
        # has always been that neither is a *measurement*.
        ("counting_method", ["visual", "weir", "visual"], "categorical", None),
        ("gear", ["net", "trap", "net"], "categorical", None),
        ("sample_size", [10, 20, 30], "measurement", None),
        ("survey_year", ["2001", "2002", "2003"], "temporal", None),
        ("run_year", [2001, 2002, 2003], "temporal", None),
        ("spawner_count", [10, 20, 30], "measurement", None),
        ("escapement", [10, 20, 30], "measurement", None),
        ("total_length_mm", [10.5, 20.5, 30.5], "measurement", None),
        ("water_temp", ["8.1", "9.2", "10.3"], "measurement", None),
        ("discharge (m3/s)", ["1.2", "2.3", "3.4"], "measurement", None),
        # #95: the final default. Three distinct strings are a code list the
        # seeder would write rows for, so the dictionary must say categorical
        # or the specification's validator rejects those rows.
        ("comment", ["a", "b", "c"], "categorical", None),
        ("abundance", ["n/a", "n/a", "n/a"], "categorical", None),
        # Unchanged, and the reason is the ordering: the measurement check runs
        # ahead of the code-list check, so a percent-like text column keeps its
        # role even though its values would enumerate.
        ("count", ["5%", "10%", "15%"], "measurement", None),
        ("survey_date", ["2020-01-01", "2020-02-01", "2020-03-01"], "temporal", None),
        ("region", ["N", "S", "N"], "categorical", None),  # #95
        ("proportion_female", [0.4, 0.5, 0.6], "measurement", None),
        ("mortality", ["low", "high", "low"], "categorical", None),  # #95
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


def _one_column_dictionary(column, value_type, role="attribute"):
    return pd.DataFrame(
        {
            "dataset_id": ["d"],
            "table_id": ["t"],
            "column_name": [column],
            "column_label": [column],
            "column_description": ["c"],
            "column_role": [role],
            "value_type": [value_type],
            "required": [False],
        }
    )


def _code_list(column, values, labels=None):
    return pd.DataFrame(
        {
            "dataset_id": "d",
            "table_id": "t",
            "column_name": column,
            "code_value": values,
            "code_label": values if labels is None else labels,
        }
    )


def _call_recording_warnings(call):
    """Run ``call``; return its result, its reports, and pandas' Categorical deprecations.

    A report is a ``RuntimeWarning``, the category this module reports a data
    problem with; pandas' deprecation warnings are not a subclass of it. The
    two are counted separately rather than as "every warning", because an
    unrelated library deprecation is not what these tests are about: pandas
    1.5 raises numpy's ``find_common_type`` one on the way through
    ``to_numeric``.
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = call()
    reports = [str(w.message) for w in caught if issubclass(w.category, RuntimeWarning)]
    deprecations = [
        str(w.message)
        for w in caught
        if issubclass(w.category, (DeprecationWarning, FutureWarning)) and "Categorical" in str(w.message)
    ]
    return result, reports, deprecations


class ApplyDictionaryFailureReportTests(unittest.TestCase):
    """Both paths of hub item B-241, the mirror half of metasalmon's B-55.

    The R twins are in metasalmon's ``tests/testthat/test-edge-cases.R``. The
    coercion path is a pin and not a fix: ``_coerce_series()`` has always raised
    on a value that does not convert, through ``errors="raise"``, and metasalmon
    moved to where this package was. The codes path is the fix: an unlisted
    value used to become missing with no report except pandas' own deprecation
    warning.
    """

    def test_strict_raises_naming_a_value_that_does_not_convert(self):
        frame = pd.DataFrame({"count": ["100", "not-a-number", "200"]})
        with self.assertRaisesRegex(ValueError, "not-a-number"):
            apply_salmon_dictionary(frame, _one_column_dictionary("count", "integer"), strict=True)

        weights = pd.DataFrame({"weight": ["1.5", "1,5"]})
        with self.assertRaisesRegex(ValueError, "1,5"):
            apply_salmon_dictionary(weights, _one_column_dictionary("weight", "number"), strict=True)

    def test_non_strict_warns_and_keeps_the_column_as_text(self):
        frame = pd.DataFrame({"count": ["100", "not-a-number", "200"]})
        with self.assertWarnsRegex(RuntimeWarning, "keeping as string"):
            result = apply_salmon_dictionary(frame, _one_column_dictionary("count", "integer"), strict=False)
        self.assertEqual(result["count"].tolist(), ["100", "not-a-number", "200"])

    def test_missing_and_blank_values_are_not_coercion_failures(self):
        frame = pd.DataFrame({"count": ["1", None, "", "2"]})
        result, reports, _ = _call_recording_warnings(
            lambda: apply_salmon_dictionary(frame, _one_column_dictionary("count", "integer"), strict=True)
        )
        self.assertEqual(reports, [])
        self.assertEqual(str(result["count"].dtype), "Int64")
        self.assertEqual(result["count"].isna().tolist(), [False, True, True, False])

    def test_strict_raises_on_a_coercion_that_errors(self):
        frame = pd.DataFrame({"day": ["not a date", "2024-01-01"]})
        with warnings.catch_warnings():
            # pandas' "Could not infer format" notice on the way to the error.
            warnings.simplefilter("ignore", UserWarning)
            with self.assertRaisesRegex(ValueError, "Failed to coerce column to date"):
                apply_salmon_dictionary(frame, _one_column_dictionary("day", "date"), strict=True)

    def test_an_unlisted_code_value_is_named_once_under_both_strict_modes(self):
        frame = pd.DataFrame({"species": ["Coho", "Chinook", "Unknown", None, ""]})
        dictionary = _one_column_dictionary("species", "string")
        codes = _code_list("species", ["Coho", "Chinook"], ["Coho Salmon", "Chinook Salmon"])
        for strict in (True, False):
            with self.subTest(strict=strict):
                result, reports, deprecations = _call_recording_warnings(
                    lambda: apply_salmon_dictionary(frame, dictionary, codes=codes, strict=strict)
                )
                self.assertEqual(len(reports), 1, reports)
                message = reports[0]
                # "1 value": the missing and the blank value are not named.
                self.assertIn("has 1 value not in its code list", message)
                self.assertIn("'Unknown'", message)
                self.assertNotIn("''", message)
                self.assertEqual(result["species"].isna().tolist(), [False, False, True, True, True])
                # The deprecation pandas raises for a Categorical built from a
                # value outside its categories was the only signal before.
                self.assertEqual(deprecations, [])

    def test_a_column_whose_present_values_are_all_listed_is_not_reported(self):
        frame = pd.DataFrame({"species": ["Coho", "Chinook", None, ""]})
        _, reports, deprecations = _call_recording_warnings(
            lambda: apply_salmon_dictionary(
                frame, _one_column_dictionary("species", "string"), codes=_code_list("species", ["Coho", "Chinook"])
            )
        )
        self.assertEqual(reports, [])
        self.assertEqual(deprecations, [])

    def test_a_code_list_leaves_a_numeric_logical_or_date_column_alone_as_in_r(self):
        # R's codes step runs only on a character or factor column, so a
        # column typed integer, number, boolean or date keeps its values with
        # no report, even where a value is unlisted. Matching such values
        # against the text of a code list used to blank every one of them.
        cases = {
            "integer": (["1", "2", "3"], ["1", "2"]),
            "number": (["1.5", "2"], ["1.5"]),
            "boolean": (["TRUE", "FALSE"], ["TRUE"]),
            "date": (["2024-01-01", "2024-02-01"], ["2024-01-01"]),
        }
        for value_type, (values, listed) in cases.items():
            with self.subTest(value_type):
                frame = pd.DataFrame({"v": values})
                dictionary = _one_column_dictionary("v", value_type)
                without_codes = apply_salmon_dictionary(frame, dictionary)
                result, reports, deprecations = _call_recording_warnings(
                    lambda: apply_salmon_dictionary(frame, dictionary, codes=_code_list("v", listed))
                )
                self.assertEqual(reports, [])
                self.assertEqual(deprecations, [])
                pd.testing.assert_series_equal(result["v"], without_codes["v"])

    def test_a_named_value_is_blanked_when_pandas_cannot_build_the_categorical(self):
        # A repeated code_value, or a missing one in a code list built by hand,
        # made pd.Categorical raise, which sent the column to the fallback that
        # keeps it as text; the name records that path. Since B-274 the labels
        # are applied as R's factor() applies them, and neither list reaches
        # the fallback. Either way the unlisted value is blanked first, so the
        # report is true.
        frame = pd.DataFrame({"run": ["Early", "Late", "Summer"]})
        code_lists = {
            "a repeated code_value": _code_list("run", ["Early", "Early", "Late"]),
            "a missing code_value": _code_list("run", ["Early", "Late", None]),
        }
        for name, codes in code_lists.items():
            with self.subTest(name):
                result, reports, _ = _call_recording_warnings(
                    lambda: apply_salmon_dictionary(frame, _one_column_dictionary("run", "string"), codes=codes)
                )
                self.assertEqual(len(reports), 1, reports)
                self.assertIn("has 1 value not in its code list", reports[0])
                self.assertIn("'Summer'", reports[0])
                self.assertEqual(result["run"].isna().tolist(), [False, False, True])

    def test_a_categorical_column_is_reported_without_the_pandas_deprecation(self):
        # A Categorical input keeps its categories after the blanking, and
        # pandas deprecates recoding from a category the new one lacks even
        # when no row uses it.
        frame = pd.DataFrame({"run": pd.Categorical(["Early", "Summer"])})
        result, reports, deprecations = _call_recording_warnings(
            lambda: apply_salmon_dictionary(
                frame, _one_column_dictionary("run", None), codes=_code_list("run", ["Early", "Late"])
            )
        )
        self.assertEqual(len(reports), 1, reports)
        self.assertIn("'Summer'", reports[0])
        self.assertEqual(result["run"].isna().tolist(), [False, True])
        self.assertEqual(deprecations, [])

    def test_a_long_report_is_shortened_the_way_cli_shortens_it(self):
        from metasalmonpy.dictionary import _collapse_inline

        values = [f"v{i:02d}" for i in range(1, 22)]
        # cli 3.6.6: twenty values are all named; twenty-one keep the first
        # eighteen and the last two.
        self.assertNotIn("...", _collapse_inline(values[:20], trunc=20))
        self.assertTrue(_collapse_inline(values, trunc=20).endswith("v17, v18, ..., v20, and v21"))

        frame = pd.DataFrame({"run": [f"v{i:02d}" for i in range(1, 26)]})
        _, reports, _ = _call_recording_warnings(
            lambda: apply_salmon_dictionary(
                frame, _one_column_dictionary("run", "string"), codes=_code_list("run", ["Early"])
            )
        )
        self.assertEqual(len(reports), 1, reports)
        message = reports[0]
        self.assertIn("has 25 values not in its code list; they become missing:", message)
        self.assertIn("'v18', ..., 'v24', and 'v25'", message)
        self.assertNotIn("'v19'", message)


class ApplyDictionaryCodeLabelTests(unittest.TestCase):
    """Hub item B-274: a coded column carries the labels in ``code_label``.

    metasalmon applies a code list with ``factor(x, levels = code_value,
    labels = code_label)``, so a coded column's levels are its labels. Here the
    relabel called ``rename_categories`` on a Series rather than on its
    ``.cat`` accessor, and an ``except`` marked defensive swallowed the
    ``AttributeError``, so every coded column became text, or, under the
    ``categorical`` role, a Categorical of the code values. The R behaviour the
    tests name was measured on metasalmon ``11c770c`` under R 4.3.3.
    """

    def _apply(self, frame, codes, role="attribute", value_type="string"):
        column = frame.columns[0]
        return _call_recording_warnings(
            lambda: apply_salmon_dictionary(
                frame, _one_column_dictionary(column, value_type, role), codes=codes
            )
        )

    def test_a_coded_column_carries_its_labels_under_either_role(self):
        frame = pd.DataFrame({"gear": ["N", "W", "N"]})
        codes = _code_list("gear", ["N", "W"], ["Net", "Weir"])
        for role in ("attribute", "categorical"):
            with self.subTest(role=role):
                result, reports, deprecations = self._apply(frame, codes, role)
                self.assertIsInstance(result["gear"].dtype, pd.CategoricalDtype)
                self.assertEqual(list(result["gear"].cat.categories), ["Net", "Weir"])
                self.assertEqual(result["gear"].tolist(), ["Net", "Weir", "Net"])
                self.assertEqual(reports, [])
                self.assertEqual(deprecations, [])

    def test_an_unlisted_value_is_still_named_and_still_becomes_missing(self):
        frame = pd.DataFrame({"gear": ["N", "W", "X", None, ""]})
        codes = _code_list("gear", ["N", "W"], ["Net", "Weir"])
        for role in ("attribute", "categorical"):
            with self.subTest(role=role):
                result, reports, deprecations = self._apply(frame, codes, role)
                # B-241's report, unchanged: it names the value as the data
                # holds it, not a label.
                self.assertEqual(
                    reports, ["Column 'gear' has 1 value not in its code list; it becomes missing: 'X'"]
                )
                self.assertEqual(result["gear"].tolist()[:2], ["Net", "Weir"])
                self.assertEqual(result["gear"].isna().tolist(), [False, False, True, True, True])
                self.assertEqual(deprecations, [])

    def test_a_categorical_of_integers_takes_its_labels_from_integer_code_values(self):
        # The B-241 run's shape. Under the categorical role it came back
        # [nan, nan, nan]: the listed values were blanked with the unlisted one.
        frame = pd.DataFrame({"n": pd.Categorical([1, 2, 3])})
        code_lists = {
            "labelled": (_code_list("n", [1, 2], ["one", "two"]), ["one", "two"]),
            "labelled with its values": (_code_list("n", [1, 2]), [1, 2]),
        }
        for role in ("attribute", "categorical"):
            for name, (codes, labels) in code_lists.items():
                with self.subTest(role=role, codes=name):
                    result, reports, deprecations = self._apply(frame, codes, role, value_type=None)
                    self.assertIsInstance(result["n"].dtype, pd.CategoricalDtype)
                    self.assertEqual(list(result["n"].cat.categories), labels)
                    self.assertEqual(result["n"].tolist()[:2], labels)
                    self.assertTrue(pd.isna(result["n"].iloc[2]))
                    self.assertEqual(reports, ["Column 'n' has 1 value not in its code list; it becomes missing: 3"])
                    self.assertEqual(deprecations, [])

    def test_codes_that_share_a_label_share_a_category_as_in_r(self):
        # R's factor() merges labels that repeat into one level, where
        # rename_categories refuses them.
        frame = pd.DataFrame({"gear": ["N", "W", "T"]})
        codes = _code_list("gear", ["N", "W", "T"], ["Fixed", "Fixed", "Trap"])
        result, reports, _ = self._apply(frame, codes)
        self.assertIsInstance(result["gear"].dtype, pd.CategoricalDtype)
        self.assertEqual(list(result["gear"].cat.categories), ["Fixed", "Trap"])
        self.assertEqual(result["gear"].tolist(), ["Fixed", "Fixed", "Trap"])
        self.assertEqual(reports, [])

    def test_a_repeated_code_value_takes_its_first_label_as_in_r(self):
        # R's match() finds a value's first row, and every label is a level.
        frame = pd.DataFrame({"run": ["E", "L", "S"]})
        codes = _code_list("run", ["E", "E", "L"], ["Early", "Early again", "Late"])
        result, reports, _ = self._apply(frame, codes)
        self.assertIsInstance(result["run"].dtype, pd.CategoricalDtype)
        self.assertEqual(list(result["run"].cat.categories), ["Early", "Early again", "Late"])
        self.assertEqual(result["run"].tolist()[:2], ["Early", "Late"])
        self.assertTrue(pd.isna(result["run"].iloc[2]))
        self.assertEqual(reports, ["Column 'run' has 1 value not in its code list; it becomes missing: 'S'"])

    def test_a_code_with_no_label_becomes_missing_as_in_r(self):
        # R gives a code whose code_label is NA a missing level, so its values
        # print and write as NA, and R's reader reads a blank label as NA.
        # pandas holds no missing category, so here the value itself becomes
        # missing. R does not report it, and neither does this.
        frame = pd.DataFrame({"gear": ["N", "W", "T"]})
        codes = _code_list("gear", ["N", "W", "T"], ["Net", None, ""])
        result, reports, _ = self._apply(frame, codes)
        self.assertIsInstance(result["gear"].dtype, pd.CategoricalDtype)
        self.assertEqual(list(result["gear"].cat.categories), ["Net"])
        self.assertEqual(result["gear"].isna().tolist(), [False, True, True])
        self.assertEqual(reports, [])

    def test_an_ordered_categorical_stays_ordered_as_in_r(self):
        # R's factor() takes ordered from is.ordered(x).
        frame = pd.DataFrame({"size": pd.Categorical(["L", "S"], categories=["S", "L"], ordered=True)})
        codes = _code_list("size", ["S", "L"], ["Small", "Large"])
        result, reports, _ = self._apply(frame, codes, value_type=None)
        self.assertIsInstance(result["size"].dtype, pd.CategoricalDtype)
        self.assertTrue(result["size"].cat.ordered)
        self.assertEqual(list(result["size"].cat.categories), ["Small", "Large"])
        self.assertEqual(result["size"].tolist(), ["Large", "Small"])
        self.assertEqual(reports, [])

    def test_a_code_list_that_cannot_be_applied_is_reported_and_its_values_kept(self):
        # A column name the data repeats is the one input left that the codes
        # step cannot apply a code list to. It used to blank every value of
        # both columns, with no report but pandas 3's own deprecation warning.
        # R refuses such a data frame outright. What this package should do
        # with it is hub item B-397's; this pins only that it is reported and
        # nothing is lost.
        frame = pd.DataFrame([["N", "W"], ["W", "X"]], columns=["gear", "gear"])
        result, reports, _ = self._apply(frame, _code_list("gear", ["N", "W"], ["Net", "Weir"]))
        self.assertEqual(len(reports), 1, reports)
        self.assertIn("Column 'gear'", reports[0])
        self.assertIn("more than one column", reports[0])
        self.assertEqual(result.iloc[:, 0].tolist(), ["N", "W"])
        self.assertEqual(result.iloc[:, 1].tolist(), ["W", "X"])

    def test_a_failure_to_apply_the_labels_is_reported_and_a_named_value_stays_missing(self):
        frame = pd.DataFrame({"gear": ["N", "X"]})
        codes = _code_list("gear", ["N", "W"], ["Net", "Weir"])
        with mock.patch("metasalmonpy.dictionary._apply_code_labels", side_effect=ValueError("boom")):
            result, reports, _ = self._apply(frame, codes)
        self.assertEqual(len(reports), 2, reports)
        self.assertIn("'X'", reports[0])
        self.assertIn("Column 'gear'", reports[1])
        self.assertIn("boom", reports[1])
        self.assertEqual(result["gear"].tolist()[0], "N")
        self.assertEqual(result["gear"].isna().tolist(), [False, True])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
