"""Redaction for text this package did not author.

Mirrors the half of metasalmon's ``R/cli-safety.R`` that has a Python
counterpart. The other half — ``.ms_cli_escape()``, ``.ms_cli_bullets()`` and
``.ms_abort_external()`` — exists because cli treats every element of a
condition message as a glue template, so a provider error containing
``{Sys.getenv("OPENAI_API_KEY")}`` printed the key and an unbalanced brace
replaced the message with a parse error. Python has no such layer: an
exception message is a finished string, and ``str.format``/f-strings are only
applied to templates this package writes. Escaping external text here would
mangle it, not protect it, so the escape half is deliberately absent
(PARITY.md row 37).

The redaction half applies exactly as it does in R, and for the same reason:
**apply it where external text is CAPTURED, not where it is displayed.** Text
stored on a returned frame or written to a CSV outlives any message, so a
display-time redactor is already too late.
"""

from __future__ import annotations

import re

# Credential headers first, and to end of line: the value may be a scheme plus
# a token ("Authorization: Basic dXNlcjpwYXNz") or a cookie jar containing
# spaces and semicolons, so stopping at the first whitespace leaves the secret
# in place. Running this rule before the bare-scheme rules also avoids double
# substitution mangling the result.
#
# The vendor prefix and trailing qualifier are both part of the name: without
# them a leading ``\b`` never matches the variables this package actually
# reads, because ``_`` is a word character and ``OPENAI_API_KEY`` therefore has
# no boundary before ``API_KEY``.
_CREDENTIAL_NAME = re.compile(
    r"\b((?:[A-Za-z0-9]+[_-])*"
    r"(?:authorization|proxy-authorization|set-cookie|cookie|dataone[_-]?token|"
    r"api[_-]?key|access[_-]?token|refresh[_-]?token|secret[_-]?key)"
    r"[A-Za-z0-9_]*)"
    # An optional closing quote before the separator: a serialized error body
    # writes ``"api_key":"secret"``, where the quote sits between the name and
    # the colon and an unquoted pattern never matches.
    r"[\"']?[ \t]*[=:][ \t]*[^\r\n]*",
    re.IGNORECASE,
)

_BARE_SCHEME = re.compile(
    r"\b(Bearer|Basic|Digest)[ \t\r\n\f\v]+[A-Za-z0-9._~+/=-]+",
    re.IGNORECASE,
)

_JWT = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_OPENAI_KEY = re.compile(r"sk-[A-Za-z0-9_-]{20,}")
_GOOGLE_KEY = re.compile(r"AIza[0-9A-Za-z_-]{35}")


def redact_secrets(value: object) -> str:
    """Best-effort redaction of credentials that services echo back.

    Mirrors ``.ms_redact_secrets()``. Deliberately conservative: over-eager
    patterns applied to validation messages would hide the very value a user
    needs in order to fix their metadata.

    metasalmon 0.2.0 introduced this alongside a second, narrower redactor for
    the KNB boundary (``.ms_knb_redact()``); metasalmon 0.2.5 collapses the two
    into one. ``knb_publication._redact`` is this package's mirror of the
    narrow one and stays separate for the same window — its output is pinned
    against R-generated fixtures. **Retirement condition:** delete
    ``knb_publication._redact`` and route its call sites here at the 0.2.5
    rung, when metasalmon asserts there is exactly one redactor.
    """
    text = str(value)
    text = _CREDENTIAL_NAME.sub(lambda match: match.group(1) + "=[REDACTED]", text)
    text = _BARE_SCHEME.sub(lambda match: match.group(1) + " [REDACTED]", text)
    text = _JWT.sub("[REDACTED JWT]", text)
    text = _OPENAI_KEY.sub("[REDACTED KEY]", text)
    text = _GOOGLE_KEY.sub("[REDACTED KEY]", text)
    return text


__all__ = ["redact_secrets"]
