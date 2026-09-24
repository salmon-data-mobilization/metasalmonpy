"""A year-shaped measurement column keeps its measurement role.

metasalmon backlog **#53** (hub queue **B-53**; this port is hub queue
**B-240**): a measurement column whose every value happens to be a four-digit
number from 1800 to 2500 -- a small stock's spawner counts, a sample of 1,900
fish -- was typed ``temporal`` on its value shape alone, and
``suggest_semantics()`` skips temporal columns, so the column left the whole
semantic pipeline with no warning. The same column holding numbers outside that
range was typed ``measurement``.

Ported from metasalmon pull request #152. The R counterpart is
``tests/testthat/test-year-shaped-measurement-role.R``, and the cases below are
its cases.

Every year-shaped fixture is first checked against ``_values_look_yearish()``.
That is the control: if the year-shape rule ever narrows so that a fixture
stops being year-shaped, the test fails on the control instead of passing
without exercising the case it exists for.

**The fixtures use this package's year-shaped storage types, not R's.** Most of
R's fixtures are doubles, and here a ``float64`` column is never year-shaped:
``_character_values()`` renders through ``str()``, and ``str(1850.0)`` is
``"1850.0"``. So R's doubles are ``int64`` here, which is what pandas reads a
whole-number column with no missing cell as. R's integer is the nullable
``Int64``, and R's character is text at pandas' default dtype. A ``float64``
fixture would fail its own control, which is the control doing its job. That
the two predicates disagree about a float column is a separate divergence, not
this file's subject.
"""

from __future__ import annotations

import pandas as pd
import pytest

from metasalmonpy.dictionary import (
    _values_look_yearish,
    infer_column_role,
    infer_dictionary,
)
from metasalmonpy.semantics import suggest_semantics

# (name, values, dtype). A dtype of ``None`` is pandas' own choice for text:
# ``str`` from pandas 3, ``object`` before it.
#
# One fixture per kind of whole-word measurement evidence: a measurement word,
# a sample size, a word followed by a unit, and a word joined to the rest of
# the name by punctuation that ``_name_tokens()`` does not split at
# (``Water depth(mm)``, ``adult/count``).
YEAR_SHAPED_MEASUREMENTS = [
    ("NATURAL_ADULT_SPAWNERS", [1850, 2003, 1999], "int64"),
    ("spawner_count", ["1850", "2003", "1999"], None),
    ("escapement", [1812, 2240, 1907], "Int64"),
    ("total_return", [2011, 2400, 1890], "int64"),
    ("mr_1st_sample_size", [1900, 2000], "int64"),
    ("Water depth (mm)", [1850, 1920], "int64"),
    ("avg_weight", [1900, 2100], "int64"),
    ("Water depth(mm)", [1850, 1920], "int64"),
    ("adult/count", ["1850", "2003", "1999"], None),
]

# The invariant is stated over more than the measurement branch. An explicit
# categorical stays categorical and a method-named column stays metadata. The
# last five pass the year-shape check through a punctuation-joined word but are
# not read as measurements by the checks below it, for any values: three
# measurement words the substring pattern does not contain, and two method
# names, which the function's rule keeps out of the measurement role. The year
# shape does not decide them either; what they are is the tokenizer's question,
# not this file's.
INVARIANT_CASES = YEAR_SHAPED_MEASUREMENTS + [
    ("spawner_count", ["1850", "2003", "1850"], "category"),
    ("count_method", ["1850", "2003", "1850"], None),
    ("adult/spawners", [1850, 2003], "int64"),
    ("fish/weight", [1900, 2100], "Int64"),
    ("sample/size", ["1900", "2000"], None),
    ("method/spawners", [1850, 2003], "int64"),
    ("enumeration/abundance", [1850, 2003], "int64"),
]

# The heuristic exists for columns whose name says nothing, such as a brood or
# return year abbreviated to ``BY``. That job is unchanged.
NO_MEASUREMENT_WORD = [
    ("BY", [2001, 2002, 2003], "int64"),
    ("RETURN", ["1999", "2000"], None),
    ("season", ["2001", "2002"], "category"),
]

# A name that says year is temporal before any measurement word is read
# (``count_year``, ``ANALYSIS_YR``). A year word hidden from that token check by
# punctuation still keeps the year shape deciding (``Escapement (yr)``,
# ``count/year``): splitting names at punctuation only to find measurement
# words would have typed those two ``measurement`` while ``count_year`` stays
# temporal, so the split finds time words too. And the plurals the token check
# leaves out count as time words here (``escapement_years``, ``count_days``).
TIME_WORD_IN_NAME = [
    ("count_year", [2001, 2002], "int64"),
    ("ANALYSIS_YR", ["2023", "2024", "2023"], None),
    ("Escapement (yr)", [2001, 2002], "int64"),
    ("count/year", ["2001", "2002"], None),
    ("escapement_years", [2001, 2002], "int64"),
    ("count_days", ["2001", "2002"], None),
]

# The names a rule built on ``_name_has_measurement_hint()`` would get wrong,
# one per pattern in it: ``temp`` inside ``temporal``, which the SDP's own
# ``dataset.csv`` fields carry, and the unit pattern's bare ``g``, which accepts
# any parenthetical holding one.
SUBSTRING_OR_UNIT_ONLY = [
    ("temporal_start", ["2001", "2024"], None),
    ("temporal_end", [2001, 2024], "int64"),
    ("Cohort (Aug)", [2001, 2002], "int64"),
]


def _series(values, dtype):
    return pd.Series(values, dtype=dtype)


def _off_the_year_range(series):
    """The same values moved out of the year range, in the same storage type."""
    if isinstance(series.dtype, pd.CategoricalDtype):
        return pd.Series([f"{value}0" for value in series], dtype="category")
    if pd.api.types.is_numeric_dtype(series.dtype):
        return series * 10
    return pd.Series([f"{value}0" for value in series], dtype=series.dtype)


def _ids(cases):
    return [f"{name}-{dtype or 'text'}" for name, _, dtype in cases]


@pytest.mark.parametrize(
    ("name", "values", "dtype"),
    YEAR_SHAPED_MEASUREMENTS,
    ids=_ids(YEAR_SHAPED_MEASUREMENTS),
)
def test_a_year_shaped_measurement_column_is_typed_measurement_not_temporal(
    name, values, dtype
):
    series = _series(values, dtype)
    assert _values_look_yearish(series)
    assert infer_column_role(name, series) == "measurement"


@pytest.mark.parametrize(
    ("name", "values", "dtype"), INVARIANT_CASES, ids=_ids(INVARIANT_CASES)
)
def test_year_shaped_values_do_not_change_the_role_a_measurement_name_gets(
    name, values, dtype
):
    series = _series(values, dtype)
    shifted = _off_the_year_range(series)
    assert _values_look_yearish(series)
    assert not _values_look_yearish(shifted)
    assert infer_column_role(name, series) == infer_column_role(name, shifted)


def test_an_explicit_categorical_and_a_method_name_keep_their_roles():
    assert (
        infer_column_role(
            "spawner_count", pd.Series(["1850", "2003", "1850"], dtype="category")
        )
        == "categorical"
    )
    assert (
        infer_column_role("count_method", pd.Series(["1850", "2003", "1850"]))
        == "categorical"
    )
    for name in ("method/spawners", "enumeration/abundance"):
        for values in ([1850, 2003], [12, 15]):
            assert infer_column_role(name, pd.Series(values)) != "measurement", (
                name,
                values,
            )


@pytest.mark.parametrize(
    ("name", "values", "dtype"),
    NO_MEASUREMENT_WORD + TIME_WORD_IN_NAME,
    ids=_ids(NO_MEASUREMENT_WORD + TIME_WORD_IN_NAME),
)
def test_the_year_shape_still_decides_without_a_measurement_word_or_with_a_time_word(
    name, values, dtype
):
    series = _series(values, dtype)
    assert _values_look_yearish(series)
    assert infer_column_role(name, series) == "temporal"


def test_a_names_words_are_split_at_punctuation_and_never_at_a_non_ascii_letter():
    from metasalmonpy.dictionary import _name_tokens, _name_words

    def words(name):
        return _name_words(_name_tokens(name))

    assert words("Water depth(mm)") == ["water", "depth", "mm"]
    assert words("adult/count") == ["adult", "count"]
    assert words("Escapement (yr)") == ["escapement", "yr"]
    assert words("NATURAL_ADULT_SPAWNERS") == ["natural", "adult", "spawners"]
    assert _name_words(["température"]) == ["température"]
    assert _name_words([]) == []

    # The boundary set is exactly the four ASCII ranges R's `.ms_name_words()`
    # spells out, checked over every printable ASCII character.
    r_ranges = ((0x21, 0x2F), (0x3A, 0x40), (0x5B, 0x60), (0x7B, 0x7E))
    for code in range(0x21, 0x7F):
        character = chr(code)
        boundary = any(low <= code <= high for low, high in r_ranges)
        expected = ["a", "b"] if boundary else [f"a{character}b"]
        assert _name_words([f"a{character}b"]) == expected, repr(character)


def test_unit_and_rate_headers_keep_their_roles_off_the_year_range():
    # The reason the checks below the year-shape check keep the coarse tokens.
    # Splitting these headers into words for every check -- which is what
    # making the measurement checks read words would require, to keep the
    # checks that outrank them consistent -- types the first three temporal and
    # the last an identifier. The year shape is not involved: these are
    # ordinary values.
    values = pd.Series([12.5, 30.1, 44.2])
    assert infer_column_role("Discharge (m3/day)", values) == "measurement"
    assert infer_column_role("Escapement (fish/yr)", values) == "measurement"
    assert infer_column_role("Rate (per day)", values) == "measurement"
    assert infer_column_role("Fish (no./site)", values) != "identifier"


@pytest.mark.parametrize(
    ("name", "values", "dtype"),
    SUBSTRING_OR_UNIT_ONLY,
    ids=_ids(SUBSTRING_OR_UNIT_ONLY),
)
def test_a_substring_or_a_parenthesised_unit_does_not_override_the_year_shape(
    name, values, dtype
):
    series = _series(values, dtype)
    assert _values_look_yearish(series)
    assert infer_column_role(name, series) == "temporal"


def test_a_year_shaped_measurement_column_reaches_the_semantic_pipeline():
    def empty_search(query, role=None, sources=None):
        return pd.DataFrame()

    def dictionary_for(spawners):
        df = pd.DataFrame(
            {
                "ANALYSIS_YR": ["2023", "2024", "2023"],
                "NATURAL_ADULT_SPAWNERS": spawners,
            }
        )
        dictionary = infer_dictionary(
            df, dataset_id="fraser-coho", table_id="escapement", seed_semantics=False
        )
        return df, dictionary

    def role_of(dictionary):
        rows = dictionary.loc[dictionary["column_name"] == "NATURAL_ADULT_SPAWNERS"]
        return rows["column_role"].iloc[0]

    def measurement_targets(df, dictionary):
        out = suggest_semantics(
            df, dictionary, sources=["smn"], max_per_role=1, search_fn=empty_search
        )
        targets = out.attrs["semantic_targets"]
        rows = targets.loc[
            targets["column_name"] == "NATURAL_ADULT_SPAWNERS",
            ["dictionary_role", "target_sdp_field", "search_query"],
        ]
        return rows.reset_index(drop=True)

    year_shaped = pd.Series([1850, 2003, 1999], dtype="int64")
    assert _values_look_yearish(year_shaped)
    df, dictionary = dictionary_for(year_shaped)
    assert role_of(dictionary) == "measurement"

    # Before the fix this column had no targets at all. It now gets the full
    # measurement set -- the variable, property, entity and unit slots among
    # them -- and exactly the targets it gets with values off the year range.
    seen = measurement_targets(df, dictionary)
    assert {"term_iri", "property_iri", "entity_iri", "unit_iri"} <= set(
        seen["target_sdp_field"]
    )
    off_range = measurement_targets(
        *dictionary_for(pd.Series([12, 15, 18], dtype="int64"))
    )
    pd.testing.assert_frame_equal(seen, off_range)
