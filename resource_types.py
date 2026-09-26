"""Dictionary-driven typing for SDP data resources.

Mirrors the block metasalmon 0.2.0 added to ``R/dictionary-helpers.R`` and
``R/package-helpers.R``: the column dictionary is the **sole type authority**
for a data resource, and a value that does not satisfy its declared
``value_type`` keeps its exact raw token rather than being silently accepted,
rounded, clamped, or ``NA``-d.

The shape of the port follows R's exactly, because the reasoning is the same in
both languages:

* One text read, then in-memory conversion — never a typed read plus a re-read.
  That keeps the original token available for every fidelity check.
* Every collector is lossy in some direction (a double collapses past 15
  significant digits, a POSIXct collapses sub-resolution instants), and no
  amount of careful formatting downstream can recover what the collector
  discarded.
* Fidelity is decided by an actual round trip — token versus the shortest
  rendering of the value it produced — not by digit or exponent thresholds,
  which misclassify in both directions at the boundaries.

**Logged decision — ``integer`` reads as a float, not ``Int64``**
(2026-08-17, S10 rung 3). metasalmon reads both ``integer`` and ``number``
with ``readr::col_double()`` and says why: ``col_integer()`` silently ``NA``s
values past 2^31. pandas' nullable ``Int64`` would not have that defect, and
that is exactly the problem — it is *exact* past 2^53, where a double is not.
The fidelity check above asks whether the token survives conversion to the
column's storage type, so an ``Int64`` column would accept
``9007199254740993`` while metasalmon reports it as beyond exact numeric
precision. Choosing ``Int64`` would therefore have bought a better numeric
type at the cost of the two implementations disagreeing about which packages
are valid. ``float64`` keeps every mismatch verdict identical, and the raw
token is preserved either way. Recorded as PARITY.md row 35.

The readr parser acceptance encoded here was **measured**, not read: every
token in ``tests/data/resource_types/`` was run through
``readr::parse_double``/``parse_logical``/``parse_date``/``parse_datetime``
under R 4.5.2 against the metasalmon ``0.2.1`` tree.
"""

from __future__ import annotations

import datetime as _dt
import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple, Union

# numpy is not a dependency of its own here: every pandas this package supports
# requires it. It is imported for ``numpy.datetime64``, the one type that holds
# an instant before year 1 or after year 9999 under all of them (hub B-388).
import numpy as np
import pandas as pd

# The SDP ``value_type`` vocabulary, mirroring the enum in the vendored
# ``column_dictionary.schema.json``.
VALUE_TYPES: Tuple[str, ...] = (
    "string",
    "integer",
    "number",
    "boolean",
    "date",
    "datetime",
)

# readr's ``trim_ws``/``trimws()`` set. Imported rather than redefined would be
# circular (``metadata`` reads the schema bundle), so it is repeated with the
# same retirement condition as ``metadata.READR_TRIM_CHARS``: these two must
# stay equal.
_TRIM = " \t\r\n"

_EPOCH = _dt.datetime(1970, 1, 1)

# What a parsed ``datetime`` token becomes: a naive UTC ``datetime``, or a
# ``numpy.datetime64`` for an instant no ``datetime`` holds. See
# ``parse_datetime_token``.
_Instant = Union[_dt.datetime, np.datetime64]


# ``strftime()`` hands ``%Y`` to the platform C library, and the platforms
# disagree below year 1000: glibc renders ``date(1, 1, 1)`` as ``1-01-01``
# where the macOS/BSD implementation pads it to ``0001-01-01``. Every calendar
# string this module produces is a canonical comparison key or a byte of a
# written package, so none of them may depend on which libc built the
# interpreter -- and the disagreement is invisible to a macOS developer, which
# is how it reached CI. The padded form is what the measured R verdicts record
# and what ISO 8601 requires, so it is the one to converge on -- but note that
# R's own answer may not be portable either: its internal tzcode is a configure
# option (``--with-internal-tzcode``, the default on macOS, where
# ``format(as.Date("0001-01-01"), "%Y-%m-%d")`` was measured as padded), so an R
# built against glibc may well emit the unpadded form. That is a metasalmon
# defect rather than a licence to copy it; PARITY.md row 40 registers the
# possible divergence and the hub owns the fix.
#
# Retire these two helpers only if Python's ``strftime`` stops delegating year
# formatting to libc. ``date.isoformat()`` is already safe (it is pure Python)
# and is used directly where the value is known to be a plain date; these
# exist for the second-resolution date-time forms, which ``isoformat()``
# cannot spell without also committing to a timezone suffix.
def _iso_date(value: _dt.date) -> str:
    """``%Y-%m-%d`` with a zero-padded year on every platform."""
    return "%04d-%02d-%02d" % (value.year, value.month, value.day)


def _iso_seconds(value: _dt.datetime, sep: str = "T") -> str:
    """``%Y-%m-%dT%H:%M:%S`` with a zero-padded year on every platform."""
    return "%s%s%02d:%02d:%02d" % (
        _iso_date(value),
        sep,
        value.hour,
        value.minute,
        value.second,
    )


def iso_instant_text(value: Any) -> str:
    """The **one** rendering of an instant as bytes this package writes.

    ``AGENTS.md``'s "one value, one rendering" contract: a value that becomes
    canonical bytes is coerced to text once, and every consumer of it reads
    that one rendering. Two renderings of one value is the defect, and it is
    the defect hub item **B-145** closes -- ``datapackage.json`` spelled a
    metadata instant with ``_clean()``'s ``isoformat()`` while
    ``metadata/dataset.csv`` got whatever ``to_csv`` chose for the column's
    dtype, and the two disagreed about the separator, the zone marker, the
    year, and whether an all-midnight column keeps its time at all.

    So every instant that reaches package bytes comes through here:

    * ``render_resource_frame`` -- a data resource's ``datetime64`` column and
      its object column of ``datetime`` values;
    * ``package_io._metadata_csv_bytes`` -- every SDP metadata CSV;
    * ``package_io._descriptor_temporal_text`` -- ``datapackage.json``'s
      ``temporal.start`` / ``temporal.end``;
    * ``observation_structures._typed_character`` -- a typed dimension value.

    The spelling is **ruled, not chosen** (Brett, 2026-09-14, once for both
    implementations): readr's ISO instant form, the ``T`` separator and the
    ``Z`` zone marker. metasalmon adopted it in hub item **B-115** by asking
    ``readr::write_csv()`` for the bytes; this package cannot ask readr, so it
    renders them itself -- which is why the renderer is one function rather
    than an agreement between four call sites that nothing rechecks.

    Three details are load bearing and each is silent when wrong:

    * **UTC.** A tz-aware value is folded to UTC before rendering, because
      ``Z`` is a claim about the instant and not decoration. Stamping ``Z`` on
      a local wall clock moves the instant. ``readr::write_csv()`` folds the
      same way, and ``observation_structures._typed_character`` already did.
    * **The year is padded by construction**, via ``_iso_date``, never through
      ``strftime``. ``tests/test_platform_determinism_guard.py`` exists for
      this: glibc renders year 999 as ``999`` where BSD pads it, and the
      difference is invisible to a macOS developer.
    * **The fractional second is truncated**, as every caller already did.

    *Retires when:* nothing. One rendering per value is the end state, not a
    step toward one. The *spelling* changes only on a ruling that moves both
    implementations at once, and the year-padding residual against
    ``readr::write_csv()`` on Linux is hub item **B-161**, recorded in
    ``PARITY.md`` row 56 rather than settled here.
    """
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    elif not isinstance(value, _dt.datetime):
        value = pd.Timestamp(value).to_pydatetime()
    if value.tzinfo is not None:
        value = value.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return _iso_seconds(value) + "Z"


def is_instant(value: Any) -> bool:
    """Is this a value :func:`iso_instant_text` can render?

    ``pd.NaT`` is the trap, and it is not a hypothetical one: ``NaTType``
    **subclasses** ``datetime.datetime``, so a bare ``isinstance(value,
    datetime)`` accepts a missing value and ``iso_instant_text`` then raises
    ``ValueError: cannot convert float NaN to integer``. That is exactly what
    ``render_resource_frame``'s object-column branch did before B-145 --
    measured, an object column holding one instant and one ``NaT`` raised --
    while its own ``datetime64`` branch two lines above guarded with
    ``pd.isna``. One function, two branches, two answers.

    ``value is not pd.NaT`` rather than ``not pd.isna(value)`` on purpose: an
    object column may hold anything, and ``pd.isna`` on a list or an array
    returns an array whose truthiness raises. ``NaT`` is a singleton, so
    identity is both exact and safe here.

    A missing instant is left in place for the CSV writer's ``na_rep`` to
    render, rather than aborting the write, because that is what metasalmon
    does: ``readr::write_csv(na = .ms_csv_na_token())`` writes an ``NA``
    POSIXct as the empty field (measured 2026-09-16, R 4.3.3 / readr 2.2.0,
    with ``.ms_csv_na_token()`` being the empty string). **Stated with its
    configuration on purpose** -- readr's *own* default ``na`` is the two
    characters ``NA``, so "readr writes the empty field" would be a claim
    stronger than the measurement. What is true of readr unconditionally, and
    is the part this guard turns on, is that it renders a missing instant
    rather than raising.

    *Retires when:* nothing, unless ``NaTType`` stops subclassing ``datetime``.
    """
    return isinstance(value, _dt.datetime) and value is not pd.NaT


def _is_blank(token: Any) -> bool:
    """R's ``!present``: NA, or text that is empty after trimming."""
    if token is None:
        return True
    if isinstance(token, float) and math.isnan(token):
        return True
    if token is pd.NA:
        return True
    return not str(token).strip(_TRIM)


# --- readr-equivalent scalar parsers ---------------------------------------

# readr's number grammar, measured token by token against R 4.5.2. Two results
# are worth naming because they look like bugs and are not:
#
# * ``"1e"`` is rejected but ``"1e+"`` parses as 1 — an exponent introducer
#   followed by a sign and no digits is read as exponent zero, while a bare
#   introducer is not. Encoded literally below, and pinned by
#   ``test_resource_types.py::test_readr_number_grammar_boundaries``.
# * ``"Inf"``, ``"NaN"`` and ``"1,000"`` are all rejected. readr's double
#   parser has no non-finite literals and no grouping mark.
_NUMBER_RE = re.compile(
    r"^[+-]?(?=[.0-9])(?P<mantissa>[0-9]*(?:\.[0-9]*)?)"
    r"(?:[eE](?P<exp>[+-][0-9]*|[0-9]+))?$"
)

# Measured against ``readr::parse_logical()``: the four spellings of each word
# plus the single letter in either case. ``"yes"``/``"no"``/``"2"``/``"-1"`` do
# not parse.
_TRUE_TOKENS = frozenset({"T", "t", "TRUE", "true", "True"})
_FALSE_TOKENS = frozenset({"F", "f", "FALSE", "false", "False"})
# readr also accepts the two numeric spellings; ``"2"`` and ``"-1"`` do not
# parse, so this is an exact set rather than "any number".
_TRUE_NUMERIC = frozenset({"1"})
_FALSE_NUMERIC = frozenset({"0"})

# Two-digit month and day are required: ``2024-1-3`` and ``2024-01-3`` are both
# rejected by readr, ``2024/01/31`` is accepted. Measured, not assumed.
_DATE_RE = re.compile(r"^([0-9]{4})[-/]([0-9]{2})[-/]([0-9]{2})$")

# ISO 8601, extended and basic, with an optional time and an optional offset.
# ``readr::parse_datetime()`` accepts a bare date, a date plus hour, hour and
# minute, or a full time, with ``T`` or a space as the separator.
_DATETIME_RE = re.compile(
    r"^(?P<year>[0-9]{4})(?:-?(?P<month>[0-9]{2})(?:-?(?P<day>[0-9]{2})"
    r"(?:[T ](?P<hour>[0-9]{2})(?::?(?P<minute>[0-9]{2})"
    r"(?::?(?P<second>[0-9]{2})(?:\.(?P<fraction>[0-9]+))?)?)?"
    r"(?P<tz>Z|[+-][0-9]{2}:?[0-9]{2}|[+-][0-9]{2})?)?)?)?$"
)


def parse_double_token(token: Any) -> Optional[float]:
    """``readr::parse_double()`` for one token, or ``None``."""
    if _is_blank(token):
        return None
    text = str(token).strip(_TRIM)
    match = _NUMBER_RE.match(text)
    if match is None:
        return None
    mantissa = match.group("mantissa")
    if not any(character.isdigit() for character in mantissa):
        return None
    exponent = match.group("exp")
    if exponent is not None and not any(c.isdigit() for c in exponent):
        # ``"1e+"`` -> exponent zero (measured); ``"1e"`` never reaches here
        # because the pattern requires a sign when there are no digits.
        exponent = "0"
    sign = "-" if text.startswith("-") else ""
    rebuilt = sign + mantissa + ("e" + exponent if exponent is not None else "")
    try:
        return float(rebuilt)
    except (ValueError, OverflowError):
        return None


def parse_logical_token(token: Any) -> Optional[bool]:
    """``readr::parse_logical()`` for one token, or ``None``."""
    if _is_blank(token):
        return None
    text = str(token).strip(_TRIM)
    if text in _TRUE_TOKENS or text in _TRUE_NUMERIC:
        return True
    if text in _FALSE_TOKENS or text in _FALSE_NUMERIC:
        return False
    return None


def parse_date_token(token: Any) -> Optional[_dt.date]:
    """``readr::parse_date()`` for one token, or ``None``."""
    if _is_blank(token):
        return None
    match = _DATE_RE.match(str(token).strip(_TRIM))
    if match is None:
        return None
    try:
        return _dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def parse_datetime_token(token: Any) -> Optional[_Instant]:
    """``readr::parse_datetime()`` for one token, or ``None``.

    Returns a naive UTC datetime, matching the POSIXct R produces: an offset
    in the token is applied and then discarded, exactly as R does.

    **An offset can carry the instant out of the years a** ``datetime``
    **holds**, which run from 1 to 9999. ``0001-01-01T00:00:00+01`` is an hour
    before year 1, and ``9999-12-31T23:00:00-02`` an hour after year 9999.
    readr reads both, as the POSIXct values R prints as ``"0-12-31 23:00:00
    UTC"`` and ``"10000-01-01 01:00:00 UTC"`` (measured under R 4.3.3 and
    readr 2.2.0; ``tests/test_instant_beyond_datetime_range.py`` records
    them). So such a token returns the same instant as a ``numpy.datetime64``
    at microsecond resolution, the resolution of every other value here.
    Until hub B-388 the subtraction below raised ``OverflowError``, and every
    read and validation of a package holding such a value raised with it.
    ``numpy.datetime64`` rather than ``pd.Timestamp`` because pandas 1.5,
    which this package supports, cannot hold these instants in a
    ``Timestamp``.
    """
    if _is_blank(token):
        return None
    match = _DATETIME_RE.match(str(token).strip(_TRIM))
    if match is None:
        return None
    parts = match.groupdict()
    if parts["month"] is None or parts["day"] is None:
        # A bare year or year-month does not parse in R either.
        return None
    fraction = parts["fraction"] or ""
    # Microseconds, truncated rather than rounded: the fractional digits past
    # the sixth are not representable and must not round the sixth up.
    micros = int((fraction + "000000")[:6]) if fraction else 0
    try:
        value = _dt.datetime(
            int(parts["year"]),
            int(parts["month"]),
            int(parts["day"]),
            int(parts["hour"] or 0),
            int(parts["minute"] or 0),
            int(parts["second"] or 0),
            micros,
        )
    except ValueError:
        return None
    offset = parts["tz"]
    if offset and offset != "Z":
        sign = 1 if offset[0] == "+" else -1
        body = offset[1:].replace(":", "")
        hours = int(body[:2])
        minutes = int(body[2:4]) if len(body) > 2 else 0
        shift = sign * _dt.timedelta(hours=hours, minutes=minutes)
        try:
            value = value - shift
        except OverflowError:
            return np.datetime64(value, "us") - np.timedelta64(shift)
    return value


# --- numeric fidelity ------------------------------------------------------


def numeric_token_precision(token: Any) -> int:
    """Significant decimal digits carried by a numeric token.

    Mirrors ``.ms_numeric_token_precision()``: sign, leading zeros, trailing
    zeros and any exponent are all ignored. A double reliably round-trips 15
    significant decimal digits, so a token carrying more may not survive.
    """
    if token is None:
        return 0
    digits = re.sub(r"[eE].*$", "", str(token).strip(_TRIM))
    digits = re.sub(r"[^0-9]", "", digits)
    digits = digits.lstrip("0").rstrip("0")
    return len(digits)


def numeric_token_exponent(token: Any) -> Optional[float]:
    """Base-10 exponent of a numeric token, or ``None`` for zero/non-numeric.

    Mirrors ``.ms_numeric_token_exponent()``.
    """
    if token is None:
        return None
    text = str(token).strip(_TRIM).lstrip("+-")
    explicit = 0.0
    if "e" in text or "E" in text:
        match = re.match(r"^.*[eE]([+-]?[0-9]+)$", text)
        explicit = float(match.group(1)) if match else 0.0
    mantissa = re.sub(r"[eE].*$", "", text)
    integer_part = re.sub(r"[.].*$", "", mantissa)
    fraction_part = re.sub(r"^[^.]*[.]", "", mantissa) if "." in mantissa else ""

    integer_significant = integer_part.lstrip("0")
    fraction_leading_zeros = len(fraction_part) - len(fraction_part.lstrip("0"))

    # A token with no significant digit at all is zero; magnitude does not apply.
    if not re.sub(r"[^1-9]", "", mantissa):
        return None
    if integer_significant:
        base = len(integer_significant) - 1
    else:
        base = -(fraction_leading_zeros + 1)
    return base + explicit


def _decimal_tokens_equal(token: str, rendered: str) -> bool:
    """Exact decimal equality, treating an unparsable side as "not equal".

    ``.ms_normalize_decimal_token()`` expands both sides to plain decimal by
    string surgery because R has no exact decimal type; ``Decimal`` does the
    same comparison directly. ``Decimal("0.10") == Decimal("1e-1")`` is True,
    which is the property that normalizer exists to provide.
    """
    try:
        return Decimal(token) == Decimal(rendered)
    except (InvalidOperation, ValueError):
        return False


def shortest_round_trip(value: float) -> str:
    """The rendering ``.ms_shortest_round_trip()`` produces for this double.

    ``repr()`` is Python's shortest round-tripping form, but R's
    ``format(value, digits = 15:17)`` is **not** shortest for an integral
    double past 2^53: it prints every integer digit, so 9.00719925474099e16
    renders as ``90071992547409904``. That difference is load-bearing — it is
    what makes metasalmon report ``"90071992547409900"`` as beyond exact
    numeric precision, because the double it parses to is really ...904. Using
    ``repr()`` there would have silently accepted a token metasalmon rejects.
    Integral values therefore render as their exact integer, which is what R
    prints, and everything else uses ``repr()``, which agrees with R's
    widening loop.
    """
    if value != value or math.isinf(value):
        return str(value)
    if float(value).is_integer():
        return str(int(value))
    return repr(value)


def numeric_token_lossy(token: Any) -> bool:
    """Whether one token survives conversion to a double.

    Mirrors ``.ms_numeric_tokens_lossy()`` including its fast path, its
    magnitude rules, and its fixed |exponent| <= 290 band. The band is not an
    approximation of the platform's capability — it exists so the same package
    validates the same way everywhere, which is the class of defect metasalmon
    0.2.0 removed.
    """
    if _is_blank(token):
        return False
    digits = numeric_token_precision(token)
    exponent = numeric_token_exponent(token)

    # Fifteen significant digits always round-trip, but only below the exact
    # integer range: ``90071992547409900`` has 15 significant digits once
    # trailing zeros are dropped and still parses to 90071992547409904.
    if digits <= 15 and (exponent is None or (-290 <= exponent <= 15)):
        return False

    parsed = parse_double_token(token)
    if parsed is None or not math.isfinite(parsed):
        return True
    if parsed == 0 and exponent is not None:
        return True
    parsed_exponent = (
        math.floor(math.log10(abs(parsed))) if parsed != 0 else None
    )
    if exponent is not None and parsed_exponent is not None:
        if exponent != parsed_exponent:
            return True
    if exponent is not None and abs(exponent) > 290:
        return True
    return not _decimal_tokens_equal(
        str(token).strip(_TRIM), shortest_round_trip(parsed)
    )


# --- datetime fidelity -----------------------------------------------------


def datetime_token_precision(token: Any) -> int:
    """Significant fractional-second digits, trailing zeros ignored.

    Mirrors ``.ms_datetime_token_precision()``.
    """
    if token is None:
        return 0
    text = str(token)
    match = re.match(r"^[^.]*\.([0-9]+)", text)
    if match is None:
        return 0
    return len(match.group(1).rstrip("0"))


def double_spacing(value: float) -> float:
    """Spacing between adjacent representable doubles at this magnitude.

    Mirrors ``.ms_double_spacing()``. Fractional seconds finer than this
    cannot survive, which is why a fixed digit threshold is not enough: at
    year 2243 the spacing already exceeds one microsecond.
    """
    magnitude = max(abs(value), 2.2250738585072014e-308)
    return 2.0 ** (math.floor(math.log2(magnitude)) - 52)


def _epoch_seconds(value: _Instant) -> float:
    """Seconds since the epoch, the double R's POSIXct stores.

    A ``numpy.datetime64`` is divided from whole microseconds, the same
    correctly rounded division ``timedelta.total_seconds()`` makes, so an
    instant gets one epoch value whichever type holds it. Its NaT is NaN, as
    ``pd.NaT``'s is: NaT's integer is the smallest ``int64``, which would
    otherwise pass for an instant in the year -290308.
    """
    if isinstance(value, np.datetime64):
        if np.isnat(value):
            return math.nan
        return int(value.astype("datetime64[us]").astype("int64")) / 1_000_000
    return (value - _EPOCH).total_seconds()


# --- conversion ------------------------------------------------------------


class ConversionOutcome:
    """Result of converting one declared column's tokens.

    ``values`` is populated only when the conversion is exact; otherwise
    ``reason`` explains why and ``offenders`` carries the tokens at fault.
    Mirrors the list ``.ms_convert_declared_tokens()`` returns.
    """

    __slots__ = ("values", "reason", "offenders")

    def __init__(
        self,
        values: Optional[List[Any]] = None,
        reason: Optional[str] = None,
        offenders: Optional[List[str]] = None,
    ) -> None:
        self.values = values
        self.reason = reason
        self.offenders = offenders or []


def convert_declared_tokens(tokens: Sequence[Any], value_type: Any) -> ConversionOutcome:
    """Convert one declared column's raw tokens, or explain why it cannot be.

    Mirrors ``.ms_convert_declared_tokens()``. The token is the ground truth:
    the column is read as text and converted here, where the original is still
    available to check against.
    """
    tokens = list(tokens)
    declared = str(value_type).strip(_TRIM).lower() if value_type is not None else ""
    parser = {
        "integer": parse_double_token,
        "number": parse_double_token,
        "boolean": parse_logical_token,
        "date": parse_date_token,
        "datetime": parse_datetime_token,
    }.get(declared)
    if parser is None:
        return ConversionOutcome(values=tokens)

    present = [not _is_blank(token) for token in tokens]
    values = [parser(token) for token in tokens]

    unparseable = [
        token
        for token, here, value in zip(tokens, present, values)
        if here and value is None
    ]
    if unparseable:
        return ConversionOutcome(reason="unparseable as that type", offenders=unparseable)

    if declared in ("integer", "number"):
        lossy = [
            token
            for token, here in zip(tokens, present)
            if here and numeric_token_lossy(token)
        ]
        if lossy:
            return ConversionOutcome(
                reason="beyond exact numeric precision", offenders=lossy
            )
        if declared == "integer":
            fractional = [
                token
                for token, here, value in zip(tokens, present, values)
                if here and value is not None and math.isfinite(value)
                and value != math.trunc(value)
            ]
            if fractional:
                return ConversionOutcome(
                    reason="not a whole number", offenders=fractional
                )

    if declared == "datetime":
        # Six fractional digits are not uniformly safe: a POSIXct is a double,
        # so the spacing between representable instants grows with the epoch
        # magnitude and already exceeds a microsecond around year 2243.
        too_fine = []
        for token, here, value in zip(tokens, present, values):
            if not here or value is None:
                continue
            precision = datetime_token_precision(token)
            seconds = _epoch_seconds(value)
            if precision > 6 or (
                precision > 0 and 10.0 ** (-precision) < double_spacing(seconds)
            ):
                too_fine.append(token)
        if too_fine:
            return ConversionOutcome(
                reason="finer than the datetime representation can hold",
                offenders=too_fine,
            )

    return ConversionOutcome(values=values)


def typed_series(values: Sequence[Any], value_type: str) -> pd.Series:
    """The pandas column one converted list becomes.

    ``integer`` and ``number`` both land in ``float64`` — see the logged
    decision in this module's docstring. ``boolean`` uses pandas' nullable
    dtype because a missing logical is a real state in R. ``date`` stays an
    object column of ``datetime.date``, which is what makes
    ``infer_value_type()`` round-trip it back to ``"date"`` rather than
    ``"datetime"``.
    """
    if value_type in ("integer", "number"):
        return pd.Series(
            [float("nan") if value is None else float(value) for value in values],
            dtype="float64",
        )
    if value_type == "boolean":
        return pd.Series(
            [pd.NA if value is None else bool(value) for value in values],
            dtype="boolean",
        )
    if value_type == "date":
        return pd.Series(list(values), dtype="object")
    if value_type == "datetime":
        try:
            return pd.Series(pd.to_datetime(list(values)))
        except (ValueError, OverflowError, pd.errors.OutOfBoundsDatetime):
            # Outside pandas' nanosecond range. R's POSIXct has no such bound,
            # so the column stays an object column of datetimes rather than
            # being reported as a type mismatch metasalmon would not report.
            # An instant before year 1 or after year 9999 is a numpy
            # ``datetime64`` in that column (hub B-388). pandas 3 reaches none
            # of this for these values: it infers microseconds and holds them
            # all in a ``datetime64[us]`` column.
            return pd.Series(list(values), dtype="object")
    return pd.Series(list(values), dtype="object")


# --- canonical comparison keys ---------------------------------------------


def format_number_token(value: Optional[float]) -> Optional[str]:
    """Canonical plain-decimal key for one numeric value.

    Mirrors ``.ms_format_number_token()``: the shortest representation that
    round-trips, never scientific. ``as.character(100000)`` is ``"1e+05"`` in
    R and ``"100000.0"`` in pandas — both are the exact defect this
    canonicalizer exists to prevent.
    """
    if value is None:
        return None
    if isinstance(value, float) and value != value:
        return None
    if isinstance(value, float) and math.isinf(value):
        return "Inf" if value > 0 else "-Inf"
    if value == 0:
        # ``-0`` renders as ``"0"`` in R, and a signed zero key would make two
        # equal values compare unequal.
        return "0"
    return format(Decimal(shortest_round_trip(float(value))).normalize(), "f")


class _UtcFields(NamedTuple):
    """The calendar fields ``_iso_seconds`` reads, for a year of any size."""

    year: int
    month: int
    day: int
    hour: int
    minute: int
    second: int


# The Gregorian calendar repeats exactly every 400 years, which are 146097
# days, so moving an instant by whole cycles changes its year by a multiple of
# 400 and leaves every other calendar field alone.
_GREGORIAN_CYCLE_SECONDS = 146097 * 86400


def _utc_fields(epoch_second: int) -> _UtcFields:
    """The UTC calendar fields of a whole epoch second, in any year.

    ``_EPOCH + timedelta(seconds=...)`` raises ``OverflowError`` outside the
    years 1 to 9999, and R's POSIXct has no such bound: metasalmon keys the
    instant before year 1 as year ``0000`` and the one after year 9999 as
    ``10000`` (hub B-388). So the second is moved into the cycle that starts
    at the epoch, read there, and its year moved back by the same number of
    cycles. Every second takes this one path, so a key never depends on which
    side of a range its instant falls.
    """
    cycles, within = divmod(epoch_second, _GREGORIAN_CYCLE_SECONDS)
    moment = _EPOCH + _dt.timedelta(seconds=within)
    return _UtcFields(
        moment.year + 400 * cycles,
        moment.month,
        moment.day,
        moment.hour,
        moment.minute,
        moment.second,
    )


def format_datetime_token(value: Optional[_Instant]) -> Optional[str]:
    """Canonical microsecond-ISO key for one datetime.

    Mirrors ``.ms_format_datetime_token()``, including its two details that
    look like rounding bugs and are the point: the fractional part is
    **truncated** from the epoch double (R's ``%OS6``), so an instant stored
    as 0.09999990463256836 keys as ``.099999``; and an instant finer than the
    rendered precision gets the exact epoch value appended after ``@`` so two
    distinct instants can never share a key.
    """
    if value is None:
        return None
    seconds = _epoch_seconds(value)
    whole = math.floor(seconds)
    micros = int((seconds - whole) * 1_000_000)
    rendered = _iso_seconds(_utc_fields(whole))
    token = "%s.%06dZ" % (rendered, micros)
    if seconds == round(seconds, 6):
        return token
    return token + "@" + str(format_number_token(seconds))


def _timestamp_instant(value: pd.Timestamp) -> _Instant:
    """The instant a ``Timestamp`` holds, as ``parse_datetime_token`` returns it.

    pandas 2 and later hold an instant before year 1 or after year 9999 in a
    ``Timestamp`` (pandas 3 reads a declared column of them into one), and
    ``to_pydatetime()`` raises ``ValueError`` for it, so it becomes the
    ``numpy.datetime64`` the parser gives for the same token (hub B-388).
    """
    if _dt.MINYEAR <= value.year <= _dt.MAXYEAR:
        return value.to_pydatetime()
    return value.to_datetime64()


def canonical_value_tokens(values: Sequence[Any], value_type: Any) -> List[Optional[str]]:
    """One canonical text key per value, so parsed data and raw CSV compare.

    Mirrors ``.ms_canonical_value_tokens()``. Without it, ``"0.10"`` read back
    as a double stringifies to ``"0.1"`` and ``100000`` to ``"1e+05"``, and a
    package fails validation against its own codes list.
    """
    declared = str(value_type).strip(_TRIM).lower() if value_type is not None else ""
    if declared not in VALUE_TYPES:
        declared = "string"

    original: List[Optional[str]] = []
    for value in values:
        if value is None or value is pd.NA or (isinstance(value, float) and value != value):
            original.append(None)
        else:
            original.append(str(value).strip(_TRIM))

    if declared == "string":
        return original

    rendered: List[Optional[str]] = []
    for value, text in zip(values, original):
        if text is None:
            rendered.append(None)
            continue
        if declared in ("integer", "number"):
            parsed = value if isinstance(value, float) else parse_double_token(text)
            rendered.append(format_number_token(parsed) if parsed is not None else None)
        elif declared == "boolean":
            parsed = value if isinstance(value, bool) else parse_logical_token(text)
            rendered.append(None if parsed is None else ("TRUE" if parsed else "FALSE"))
        elif declared == "date":
            parsed = value if isinstance(value, _dt.date) and not isinstance(
                value, _dt.datetime
            ) else parse_date_token(text)
            rendered.append(None if parsed is None else _iso_date(parsed))
        else:
            parsed = (
                _timestamp_instant(value)
                if isinstance(value, pd.Timestamp)
                else value
                if isinstance(value, _dt.datetime)
                or (isinstance(value, np.datetime64) and not np.isnat(value))
                else parse_datetime_token(text)
            )
            rendered.append(format_datetime_token(parsed))

    # Unparseable input keeps its original text rather than collapsing to
    # missing, so a genuine mismatch still reads as a mismatch.
    return [
        text if key is None and text else key
        for key, text in zip(rendered, original)
    ]


# --- canonical CSV rendering ----------------------------------------------

# **Logged deviation (PARITY.md row 36).** metasalmon writes data resources
# with ``readr::write_csv()``, whose double formatter is vroom's C++
# shortest-representation writer with a ``%.17g`` fallback: 1e15 becomes
# ``1e15``, 1.5e15 becomes ``15e14``, 0.00015 becomes ``1.5e-4``, and the
# fallback's output is platform-dependent (metasalmon's own source records a
# macOS/Linux disagreement around 1e-300). Reproducing those bytes is the same
# class of impossibility as the ZIP and libxml2 formatters. This package
# renders the shortest round-trip decimal in plain notation instead — which is
# ``.ms_format_number_token()``, metasalmon's *own* canonical rendering — so
# the values are identical and the bytes agree wherever readr also chooses
# plain notation.


def render_resource_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Render a typed resource frame as the text a package carries.

    Only the dtypes the typed reader produces are re-rendered; an integer or
    string column the caller supplied is left exactly as it was, so this
    changes nothing for a frame that never went through
    :func:`read_salmon_datapackage`.
    """
    out = frame.copy()

    def assign(column, values):
        # ``object`` dtype so a missing value stays ``None`` rather than being
        # coerced to ``nan``; ``to_csv`` renders both as the empty field, but
        # the frame is also inspected directly by callers and by tests.
        out[column] = pd.Series(values, dtype="object", index=out.index)

    for column in out.columns:
        series = out[column]
        if pd.api.types.is_bool_dtype(series.dtype):
            assign(column, [
                None if value is pd.NA or value is None else ("TRUE" if value else "FALSE")
                for value in series
            ])
        elif pd.api.types.is_float_dtype(series.dtype):
            assign(column, [format_number_token(value) for value in series])
        elif pd.api.types.is_datetime64_any_dtype(series.dtype):
            assign(column, [
                None if pd.isna(value) else iso_instant_text(value)
                for value in series
            ])
        elif series.dtype == object:
            values = list(series)
            if any(is_instant(value) for value in values):
                assign(column, [
                    iso_instant_text(value) if is_instant(value) else value
                    for value in values
                ])
    return out


def value_type_mismatch_record(
    table_id: str, column: str, declared: str, outcome: ConversionOutcome
) -> Dict[str, Any]:
    """One structured mismatch, in the shape the validator reports."""
    unique_offenders: List[str] = []
    for token in outcome.offenders:
        text = str(token)
        if text not in unique_offenders:
            unique_offenders.append(text)
    return {
        "table_id": table_id,
        "column": column,
        "declared": declared,
        "reason": outcome.reason,
        "count": len(outcome.offenders),
        "examples": unique_offenders[:3],
    }


__all__ = [
    "VALUE_TYPES",
    "ConversionOutcome",
    "canonical_value_tokens",
    "convert_declared_tokens",
    "datetime_token_precision",
    "double_spacing",
    "format_datetime_token",
    "format_number_token",
    "is_instant",
    "iso_instant_text",
    "numeric_token_exponent",
    "numeric_token_lossy",
    "numeric_token_precision",
    "parse_date_token",
    "parse_datetime_token",
    "parse_double_token",
    "parse_logical_token",
    "render_resource_frame",
    "shortest_round_trip",
    "typed_series",
    "value_type_mismatch_record",
]
