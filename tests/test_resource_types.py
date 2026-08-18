"""The dictionary-driven typed reader, measured against metasalmon 0.2.1.

Every expectation in this file comes from **running** the R implementation,
not from reading it. ``tests/data/resource_types/r-token-verdicts.json`` is the
output of driving ``.ms_convert_declared_tokens()``,
``.ms_canonical_value_tokens()``, ``.ms_numeric_tokens_lossy()``,
``.ms_numeric_token_precision()``, ``.ms_numeric_token_exponent()`` and
``.ms_datetime_token_precision()`` over ``token-corpus.json`` under R 4.5.2,
with metasalmon loaded from a read-only extraction of the commit that made
0.2.1 current (``f675d91``; the 0.2.x releases are deliberately untagged).

``r-package/`` is a Salmon Data Package written by that same tree. Because R
adopted C collation at **0.2.0**, byte-equality claims measured against it need
no locale caveat — unlike the 0.1.7/0.1.8 rungs, which did.
"""

from __future__ import annotations

import datetime as _dt
import json
import math
from pathlib import Path

import pandas as pd
import pytest

from metasalmonpy import resource_types as rt

DATA = Path(__file__).resolve().parent / "data" / "resource_types"
R_VERDICTS = json.loads((DATA / "r-token-verdicts.json").read_text(encoding="utf-8"))

# Two tokens are outside the double range and are reported as beyond exact
# numeric precision by BOTH implementations; only the canonical *display* key
# differs, because readr clamps a runaway exponent to +-307 where Python's
# ``float()`` saturates to inf/0. A value that reaches a canonical key has
# already passed the fidelity check, so this can never change a verdict.
_CANONICAL_OUT_OF_BAND = {
    "1e309",
    "-1e309",
    "1e-400",
    "1e308",
    "1e-310",
    "1.7976931348623157e308",
    "2.2250738585072014e-308",
    "5e-324",
}


# One measured datetime boundary. ``readr::parse_datetime()`` builds its
# POSIXct from a computation that loses precision on a sub-second instant
# *before* the epoch, so R's epoch double for 1969-12-31T23:59:59.999999Z is
# -9.999930625781417e-07 where Python's ``timedelta.total_seconds()`` is
# exactly -1e-06. R therefore appends its disambiguating ``@`` suffix and this
# package does not. It changes no verdict -- both sides accept the token -- and
# only the canonical comparison key differs, for a sub-microsecond timestamp
# used as a code value.
_CANONICAL_PRE_EPOCH = {"1969-12-31T23:59:59.999999Z"}


def _cases(value_type):
    return [(case["token"], case) for case in R_VERDICTS[value_type]]


@pytest.mark.parametrize(
    "value_type", ["integer", "number", "boolean", "date", "datetime", "string"]
)
def test_conversion_verdicts_match_era_r_token_for_token(value_type):
    """The reason a declared type is not satisfied, for every probed token."""
    for token, expected in _cases(value_type):
        outcome = rt.convert_declared_tokens([token], value_type)
        assert outcome.reason == expected["reason"], (value_type, token)


@pytest.mark.parametrize(
    "value_type", ["integer", "number", "boolean", "date", "datetime", "string"]
)
def test_canonical_keys_match_era_r(value_type):
    for token, expected in _cases(value_type):
        if token in _CANONICAL_OUT_OF_BAND or token in _CANONICAL_PRE_EPOCH:
            continue
        assert rt.canonical_value_tokens([token], value_type)[0] == expected["canonical"], (
            value_type,
            token,
        )


@pytest.mark.parametrize("value_type", ["integer", "number"])
def test_numeric_fidelity_helpers_match_era_r(value_type):
    for token, expected in _cases(value_type):
        assert rt.numeric_token_precision(token) == expected["precision"], token
        r_exponent = expected["exponent"]
        py_exponent = rt.numeric_token_exponent(token)
        if r_exponent in (None, "NA"):
            assert py_exponent is None, token
        else:
            assert py_exponent == float(r_exponent), token
        assert rt.numeric_token_lossy(token) is bool(expected["lossy"]), token


def test_the_pre_epoch_datetime_key_is_a_measured_boundary():
    """R appends its exact-epoch suffix here and this package does not."""
    parsed = rt.parse_datetime_token("1969-12-31T23:59:59.999999Z")
    assert rt.format_datetime_token(parsed) == "1969-12-31T23:59:59.999999Z"
    era_r = R_VERDICTS["datetime"]
    recorded = next(
        case for case in era_r if case["token"] == "1969-12-31T23:59:59.999999Z"
    )
    assert recorded["canonical"].startswith("1969-12-31T23:59:59.999999Z@")
    assert recorded["reason"] is None


def test_the_out_of_band_canonical_keys_are_a_measured_boundary():
    """The two implementations disagree only where no faithful value exists.

    ``readr::parse_double()`` clamps a runaway exponent to +-307 while
    ``float()`` saturates, so the canonical *key* for a token past the double
    range differs. Pinned rather than papered over: both sides still report the
    token as beyond exact numeric precision, which is the verdict that decides
    whether a package is valid.
    """
    for token in ("1e309", "1e-400"):
        assert (
            rt.convert_declared_tokens([token], "number").reason
            == "beyond exact numeric precision"
        )
    assert rt.canonical_value_tokens(["1e309"], "number")[0] == "Inf"


def test_readr_number_grammar_boundaries():
    """Two acceptances that look like bugs and are readr's actual behaviour."""
    assert rt.parse_double_token("1e") is None
    assert rt.parse_double_token("1e+") == 1.0
    assert rt.parse_double_token("Inf") is None
    assert rt.parse_double_token("1,000") is None
    assert rt.parse_double_token(" 42 ") == 42.0
    assert rt.parse_double_token("+.5") == 0.5


def test_readr_date_grammar_requires_two_digit_month_and_day():
    assert rt.parse_date_token("2024-01-31") == _dt.date(2024, 1, 31)
    assert rt.parse_date_token("2024/01/31") == _dt.date(2024, 1, 31)
    assert rt.parse_date_token("2024-1-3") is None
    assert rt.parse_date_token("2024-01-3") is None
    assert rt.parse_date_token("2024-02-30") is None


def test_a_shortest_round_trip_is_r_format_not_python_repr():
    """The one place ``repr()`` would have silently accepted a bad token.

    ``repr(9.00719925474099e16)`` is the shortest form that round-trips, so a
    ``repr``-based comparison calls ``"90071992547409900"`` faithful. R prints
    every integer digit and therefore reports it as beyond exact numeric
    precision, because the double it parses to is really ...904.
    """
    assert rt.shortest_round_trip(9.00719925474099e16) == "90071992547409904"
    assert rt.numeric_token_lossy("90071992547409900") is True
    assert rt.numeric_token_lossy("1234567890123456") is False


def test_the_integer_storage_decision_keeps_r_s_mismatch_verdicts():
    """``integer`` is a float column — the logged decision, made observable.

    A nullable ``Int64`` column would represent ``9007199254740993`` exactly
    and so would have to accept a token metasalmon rejects. Storing a double
    keeps the two implementations agreeing about which packages are valid.
    """
    assert (
        rt.convert_declared_tokens(["9007199254740993"], "integer").reason
        == "beyond exact numeric precision"
    )
    typed = rt.typed_series([1.0, 2.0, None], "integer")
    assert str(typed.dtype) == "float64"
    assert math.isnan(typed.iloc[2])


def test_a_datetime_finer_than_the_representation_keeps_its_token():
    outcome = rt.convert_declared_tokens(["2024-01-31T10:00:00.1234567Z"], "datetime")
    assert outcome.reason == "finer than the datetime representation can hold"
    assert outcome.offenders == ["2024-01-31T10:00:00.1234567Z"]


def test_the_datetime_key_truncates_from_the_epoch_double_like_r():
    """``.099999``, not ``.100000`` — the point of keying off epoch seconds."""
    parsed = rt.parse_datetime_token("2024-01-31T10:00:00.100000000Z")
    assert rt.format_datetime_token(parsed) == "2024-01-31T10:00:00.099999Z"


def test_the_typed_dtypes_mirror_r_s_classes():
    """character/numeric/logical/Date/POSIXct, in pandas terms."""
    assert str(rt.typed_series([1.0], "number").dtype) == "float64"
    assert str(rt.typed_series([True, None], "boolean").dtype) == "boolean"
    assert rt.typed_series([_dt.date(2024, 1, 31)], "date").dtype == object
    assert pd.api.types.is_datetime64_any_dtype(
        rt.typed_series([_dt.datetime(2024, 1, 31)], "datetime").dtype
    )


def test_render_resource_frame_reproduces_the_tokens_the_reader_consumed():
    frame = pd.DataFrame(
        {
            "n": rt.typed_series([100000.0, 0.1, 1234567890123456.0], "number"),
            "b": rt.typed_series([True, False, None], "boolean"),
            "d": rt.typed_series(
                [_dt.date(2024, 1, 31), _dt.date(2024, 2, 29), _dt.date(2023, 12, 31)],
                "date",
            ),
        }
    )
    rendered = rt.render_resource_frame(frame)
    assert list(rendered["n"]) == ["100000", "0.1", "1234567890123456"]
    assert list(rendered["b"]) == ["TRUE", "FALSE", None]
    assert list(rendered["d"]) == [
        _dt.date(2024, 1, 31),
        _dt.date(2024, 2, 29),
        _dt.date(2023, 12, 31),
    ]
