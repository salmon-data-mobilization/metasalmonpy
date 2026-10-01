"""Canonical JSON for the semantic review packet (hub item B-327).

The mirror of metasalmon's ``R/semantic-review-json.R`` (hub item B-326, S16
step 1). The review packet is a byte contract shared by the two packages: the
same inputs must give the same bytes in both languages, on every platform and
locale, because ``packet_id`` is the SHA-256 of those bytes and the ingester
recomputes it. R's emitter was written to render exactly what
``json.dumps(obj, indent=2, ensure_ascii=False)`` renders, with one stated
difference for numbers, so this side is the standard library's string escaping
plus the shared number-token formatter:

* UTF-8, no byte-order mark, two-space indent, one member per line, ``": "``
  between a key and its value, ``","`` at the end of every member but the last,
  and a single trailing LF after the closing bracket;
* empty containers inline: ``[]`` and ``{}``;
* only ``"``, ``\\`` and U+0000-U+001F escaped (``\\b``, ``\\f``, ``\\n``,
  ``\\r``, ``\\t``, and ``\\u00xx`` in lower-case hex for the rest), everything
  else written as itself -- which is ``json.dumps(..., ensure_ascii=False)``'s
  own string rule, so it is borrowed rather than rewritten;
* ``true``, ``false`` and ``null``; a missing value of any type is ``null``;
* **numbers are rendered by value, not by type**: a finite whole number below
  2**53 is an integer literal (``2``, never ``2.0``), and every other finite
  number goes through :func:`~metasalmonpy.resource_types.format_number_token`,
  the shortest decimal that round-trips and never scientific notation (PARITY.md
  rows 36 and 59 record it as agreeing with ``.ms_format_number_token()``).
  ``NaN`` and infinities are ``null``. This is the one place the rendering
  departs from ``json.dumps``, which would write ``2.0`` and ``1e+20``;
* key order is the order the builder gave. The emitter never sorts, so the
  builder owns every ordering.

One value, one rendering: a value reaches the bytes through exactly one branch
below, and the identity hash is computed over the bytes that are written, never
over a second rendering.

The Python representation the emitter reads: a mapping is an object, a list or
tuple is an array, a string, bool or number is a scalar, and ``None`` (or any
pandas missing value) is ``null``. Anything else is refused rather than guessed.
"""

from __future__ import annotations

import hashlib
import json
import math
import numbers
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Union

from .resource_types import format_number_token

__all__ = [
    "read_semantic_review_json",
    "semantic_review_canonical_bytes",
    "semantic_review_packet_id",
    "semantic_review_sha256",
]

# Below this, ``int(value)`` writes every digit of a whole double exactly, as
# R's ``sprintf("%.0f")`` does; at or above it R hands the value to the
# number-token formatter, and so does this.
_WHOLE_NUMBER_LIMIT = 2 ** 53


def _is_missing_scalar(value) -> bool:
    """``pd.NA``, ``NaT`` and friends, without importing pandas eagerly."""
    if value is None:
        return True
    try:
        import pandas as pd

        return value is pd.NA or value is pd.NaT
    except ImportError:  # pragma: no cover - pandas is a core dependency
        return False


def _render_number(value) -> str:
    """One number, by value: the rule ``.ms_json_render_number()`` applies."""
    if isinstance(value, numbers.Integral):
        whole = int(value)
        if abs(whole) < _WHOLE_NUMBER_LIMIT:
            return str(whole)
        value = float(whole)
    number = float(value)
    if not math.isfinite(number):
        return "null"
    if number == math.trunc(number) and abs(number) < _WHOLE_NUMBER_LIMIT:
        # A negative zero is written as ``0``, as R writes it.
        return "0" if number == 0 else str(int(number))
    return format_number_token(number)


def _render_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _render(value: Any, indent: int) -> str:
    pad = "  " * indent
    inner = "  " * (indent + 1)
    if _is_missing_scalar(value):
        return "null"
    # bool before Integral: ``True`` is an ``int`` in Python.
    if isinstance(value, bool) or type(value).__name__ == "bool_":
        return "true" if bool(value) else "false"
    if isinstance(value, str):
        return _render_string(value)
    if isinstance(value, numbers.Number):
        if isinstance(value, float) and math.isnan(value):
            return "null"
        return _render_number(value)
    if isinstance(value, Mapping):
        if not value:
            return "{}"
        members = []
        for key, member in value.items():
            if not isinstance(key, str) or not key:
                raise TypeError("Every member of a JSON object needs a non-empty string name.")
            members.append(f"{inner}{_render_string(key)}: {_render(member, indent + 1)}")
        return "{\n" + ",\n".join(members) + "\n" + pad + "}"
    if isinstance(value, (list, tuple)):
        if not value:
            return "[]"
        members = [f"{inner}{_render(member, indent + 1)}" for member in value]
        return "[\n" + ",\n".join(members) + "\n" + pad + "]"
    raise TypeError(f"Cannot render a value of type {type(value).__name__} as JSON.")


def semantic_review_canonical_bytes(value: Any) -> bytes:
    """The canonical bytes of a packet, or of any value built under the rules above.

    The rendering plus one trailing LF, as UTF-8. The mirror of
    ``.ms_semantic_review_canonical_bytes()``.
    """
    return (_render(value, 0) + "\n").encode("utf-8")


def semantic_review_sha256(data: bytes) -> str:
    """Lower-case hex SHA-256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


def semantic_review_packet_id(packet: Mapping) -> str:
    """``packet_id``: the SHA-256 of the canonical bytes minus ``packet_id`` and ``producer``.

    So the same packet built by metasalmon and metasalmonpy has the same id,
    and the id is an integrity check at ingest: the ingester recomputes it from
    the packet it read. Every other member, in its order, is hashed.
    """
    stripped = {key: value for key, value in packet.items() if key not in ("packet_id", "producer")}
    return semantic_review_sha256(semantic_review_canonical_bytes(stripped))


def read_semantic_review_json(path: Union[str, Path]) -> Any:
    """Read a packet from disk: objects as dicts in file order, arrays as lists.

    The bytes are decoded the way ``.ms_read_text_utf8()`` decodes them (a
    byte-order mark dropped; UTF-8, then Windows-1252, then latin-1), the one
    decoding the context reader already shares with R.
    """
    from .llm_review import _decode_context_bytes

    return json.loads(_decode_context_bytes(Path(path).read_bytes()))
