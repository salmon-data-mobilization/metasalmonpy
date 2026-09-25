"""A declared date-time whose offset carries it past year 1 or year 9999 (hub B-388).

``parse_datetime_token()`` builds the wall clock a token spells and then applies
the token's UTC offset. A ``datetime`` holds years 1 to 9999, so a token such as
``0001-01-01T00:00:00+01``, an hour before year 1 once its offset is applied,
raised ``OverflowError`` there. ``convert_declared_tokens()`` raised with it, and
so did ``read_salmon_datapackage()`` and ``validate_salmon_datapackage()`` on a
package whose declared ``datetime`` column held one.

**readr reads every one of these tokens, and this file pins the instant it
reads.** Measured 2026-09-25 under R 4.3.3 and readr 2.2.0 with ``TZ`` set to
``Etc/UTC``: ``readr::parse_datetime()`` and ``readr::read_csv()`` with a
``col_datetime()`` column return the same ``POSIXct``, with no parse problem,
for each token:

=========================  ============================  ===============
token                      how R prints it               epoch second
=========================  ============================  ===============
0001-01-01T00:00:00+01     ``"0-12-31 23:00:00 UTC"``     -62135600400
0001-01-01T00:30:00+01     ``"0-12-31 23:30:00 UTC"``     -62135598600
9999-12-31T23:00:00-02     ``"10000-01-01 01:00:00 UTC"`` 253402304400
=========================  ============================  ===============

R's year 0 is the proleptic Gregorian year before year 1. metasalmon, loaded
from metasalmon ``main`` at ``9aeb0ec``, reads and validates the package below
with each token, and keys each token as :data:`READR_READS` says. Its
validator names that key when the value is missing from ``codes.csv``. The
same session measured :data:`BOUNDARIES`, one second either side of each end
of the years a ``datetime`` holds.

No ``datetime`` holds these instants, so this package returns a
``numpy.datetime64`` at microsecond resolution for them. That type exists under
every pandas this package supports, including 1.5, where ``pd.Timestamp``
cannot hold them. The tests pin the instant rather than the type, because the
column a pandas version builds from these values differs. pandas 3 builds a
``datetime64[us]`` column of ``Timestamp`` values, and older pandas keep an
object column, the fallback ``typed_series()`` already used for year 1.
"""

from __future__ import annotations

import datetime as _dt
import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from metasalmonpy import read_salmon_datapackage, validate_salmon_datapackage
from metasalmonpy import resource_types as rt

R_PACKAGE = Path(__file__).resolve().parent / "data" / "resource_types" / "r-package"

# token -> (the epoch second readr reads, metasalmon's canonical key for it)
READR_READS = {
    "0001-01-01T00:00:00+01": (-62135600400, "0000-12-31T23:00:00.000000Z"),
    "0001-01-01T00:30:00+01": (-62135598600, "0000-12-31T23:30:00.000000Z"),
    "9999-12-31T23:00:00-02": (253402304400, "10000-01-01T01:00:00.000000Z"),
}
TOKENS = sorted(READR_READS)

# The same pair, one second either side of each end of the years a datetime
# holds.
BOUNDARIES = {
    "0001-01-01T00:59:59+01": (-62135596801, "0000-12-31T23:59:59.000000Z"),
    "0001-01-01T01:00:00+01": (-62135596800, "0001-01-01T00:00:00.000000Z"),
    "9999-12-31T21:59:59-02": (253402300799, "9999-12-31T23:59:59.000000Z"),
    "9999-12-31T22:00:00-02": (253402300800, "10000-01-01T00:00:00.000000Z"),
}

# The r-package's other three survey_time values, 2024-02-29T00:00:00Z,
# 2023-12-31T23:59:59Z and 2024-06-01T12:34:56Z, which must read as before.
UNCHANGED_SECONDS = [1709164800, 1704067199, 1717245296]


def _epoch_microseconds(value) -> int:
    """The instant a value holds, in microseconds since 1970-01-01T00:00:00Z."""
    if isinstance(value, pd.Timestamp):
        value = value.to_datetime64()
    if isinstance(value, np.datetime64):
        return int(value.astype("datetime64[us]").astype("int64"))
    assert isinstance(value, _dt.datetime) and value.tzinfo is None, repr(value)
    return (value - _dt.datetime(1970, 1, 1)) // _dt.timedelta(microseconds=1)


def _package_with_survey_time(tmp_path, token, coded=False):
    """The r-package, copied, with its first ``survey_time`` set to ``token``.

    ``coded=True`` also lists every ``survey_time`` value in ``codes.csv``, so
    the validator compares the data column with its codes.
    """
    target = tmp_path / "pkg"
    shutil.copytree(R_PACKAGE, target)
    obs = target / "data" / "obs.csv"
    lines = obs.read_text(encoding="utf-8").split("\n")
    column = lines[0].split(",").index("survey_time")
    cells = lines[1].split(",")
    cells[column] = token
    lines[1] = ",".join(cells)
    obs.write_text("\n".join(lines), encoding="utf-8")
    if coded:
        values = [line.split(",")[column] for line in lines[1:] if line]
        codes = target / "metadata" / "codes.csv"
        with codes.open("a", encoding="utf-8") as handle:
            for value in values:
                handle.write(f"diff-1,obs,survey_time,{value},{value},,,,\n")
    return target


@pytest.mark.parametrize("token", TOKENS)
def test_convert_declared_tokens_reads_the_instant_readr_reads(token):
    outcome = rt.convert_declared_tokens([token], "datetime")
    assert outcome.reason is None
    assert outcome.offenders == []
    assert _epoch_microseconds(outcome.values[0]) == READR_READS[token][0] * 10**6


@pytest.mark.parametrize("token", TOKENS)
def test_the_canonical_key_is_metasalmon_s_from_text_and_from_the_value(token):
    key = READR_READS[token][1]
    assert rt.canonical_value_tokens([token], "datetime") == [key]
    values = rt.convert_declared_tokens([token], "datetime").values
    assert rt.canonical_value_tokens(values, "datetime") == [key]
    column = rt.typed_series(values, "datetime")
    assert rt.canonical_value_tokens(list(column), "datetime") == [key]


@pytest.mark.parametrize("token", sorted(BOUNDARIES))
def test_either_side_of_the_datetime_range_reads_and_keys_like_metasalmon(token):
    seconds, key = BOUNDARIES[token]
    value = rt.parse_datetime_token(token)
    assert _epoch_microseconds(value) == seconds * 10**6
    assert rt.canonical_value_tokens([token], "datetime") == [key]


def test_an_instant_a_datetime_holds_is_still_a_datetime():
    """Only an instant before year 1 or after year 9999 changes representation.

    readr reads these two as ``"1-01-01 UTC"`` and ``"9999-12-31 23:59:59
    UTC"``, the first and last seconds a ``datetime`` holds.
    """
    assert rt.parse_datetime_token("0001-01-01T01:00:00+01") == _dt.datetime(1, 1, 1)
    assert rt.parse_datetime_token("9999-12-31T21:59:59-02") == _dt.datetime(
        9999, 12, 31, 23, 59, 59
    )


@pytest.mark.parametrize("token", TOKENS)
def test_read_salmon_datapackage_reads_the_instant_readr_reads(tmp_path, token):
    package = read_salmon_datapackage(str(_package_with_survey_time(tmp_path, token)))
    frame = package["resources"]["obs"]
    assert "ms_value_type_mismatches" not in frame.attrs
    assert [_epoch_microseconds(value) for value in frame["survey_time"]] == [
        seconds * 10**6 for seconds in [READR_READS[token][0]] + UNCHANGED_SECONDS
    ]


@pytest.mark.parametrize("token", TOKENS)
def test_validate_salmon_datapackage_returns(tmp_path, token):
    result = validate_salmon_datapackage(str(_package_with_survey_time(tmp_path, token)))
    assert len(result["issues"]) == 0
    first = result["package"]["resources"]["obs"]["survey_time"].iloc[0]
    assert _epoch_microseconds(first) == READR_READS[token][0] * 10**6


@pytest.mark.parametrize("token", TOKENS)
def test_a_coded_column_matches_its_codes_and_names_metasalmon_s_key(tmp_path, token):
    target = _package_with_survey_time(tmp_path, token, coded=True)
    result = validate_salmon_datapackage(str(target))
    assert len(result["issues"]) == 0

    codes = target / "metadata" / "codes.csv"
    kept = [
        line
        for line in codes.read_text(encoding="utf-8").split("\n")
        if f",{token}," not in line
    ]
    codes.write_text("\n".join(kept), encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        validate_salmon_datapackage(str(target))
    messages = list(excinfo.value.issues["message"])
    assert messages == [
        "Table 'obs' column 'survey_time' has data values not listed in "
        f"codes.csv: {READR_READS[token][1]}."
    ]
