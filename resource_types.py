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
from typing import Any, Dict, List, Optional, Sequence, Tuple

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


def parse_datetime_token(token: Any) -> Optional[_dt.datetime]:
    """``readr::parse_datetime()`` for one token, or ``None``.

    Returns a naive UTC datetime, matching the POSIXct R produces: an offset
    in the token is applied and then discarded, exactly as R does.
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
        value = value - sign * _dt.timedelta(hours=hours, minutes=minutes)
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


def _epoch_seconds(value: _dt.datetime) -> float:
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


def format_datetime_token(value: Optional[_dt.datetime]) -> Optional[str]:
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
    rendered = (_EPOCH + _dt.timedelta(seconds=whole)).strftime("%Y-%m-%dT%H:%M:%S")
    token = "%s.%06dZ" % (rendered, micros)
    if seconds == round(seconds, 6):
        return token
    return token + "@" + str(format_number_token(seconds))


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
            rendered.append(None if parsed is None else parsed.strftime("%Y-%m-%d"))
        else:
            parsed = (
                value.to_pydatetime()
                if isinstance(value, pd.Timestamp)
                else value
                if isinstance(value, _dt.datetime)
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
                None
                if pd.isna(value)
                else pd.Timestamp(value).to_pydatetime().strftime("%Y-%m-%dT%H:%M:%SZ")
                for value in series
            ])
        elif series.dtype == object:
            values = list(series)
            if any(isinstance(value, _dt.datetime) for value in values):
                assign(column, [
                    value.strftime("%Y-%m-%dT%H:%M:%SZ")
                    if isinstance(value, _dt.datetime)
                    else value
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
