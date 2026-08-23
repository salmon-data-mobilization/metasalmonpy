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
#
# The ``[a-z0-9]+[_-]token`` branch is *structural* (metasalmon 0.2.5): any
# qualified name whose final segment is ``token`` — ``dataone_token``,
# ``dataone_test_token``, ``knb_staging_token`` — so a credential introduced
# later is covered without another patch. Naming only ``dataone[_-]?token``
# meant the staging credential leaked while the production one was redacted,
# the worst possible split, and it leaked *at rest*: captured provider errors
# are stored on returned frames and written to CSV. Two constraints keep the
# branch from eating diagnostics. A prefix segment is required, so
# ``token = 42`` in prose is left alone. And ``token`` must be the final name
# segment — without the lookahead, ``max_token_count = 4096`` and
# ``total_tokens = 1500`` matched and the rule then consumed the rest of the
# line, destroying exactly the numbers a user needs to fix a rejected LLM
# request. The trade is deliberate: a credential whose name continues past
# ``token`` (``dataone_token_v2``) is not matched by this branch and would
# need naming explicitly — missing an exotic name is recoverable; shredding a
# diagnostic is not.
_CREDENTIAL_NAME = re.compile(
    r"\b((?:[A-Za-z0-9]+[_-])*"
    r"(?:authorization|proxy-authorization|set-cookie|cookie|"
    r"api[_-]?key|access[_-]?token|refresh[_-]?token|secret[_-]?key|"
    r"[a-z0-9]+[_-]token(?![A-Za-z0-9_]))"
    r"[A-Za-z0-9_]*)"
    # An optional closing quote before the separator: a serialized error body
    # writes ``"api_key":"secret"``, where the quote sits between the name and
    # the colon and an unquoted pattern never matches. The whitespace class
    # around the separator is R's PCRE ``[[:space:]]`` re-enumerated, so a
    # name split from its value by a newline redacts identically on both
    # sides.
    r"[\"']?[ \t\r\n\f\v]*[=:][ \t\r\n\f\v]*[^\r\n]*",
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

    **This is the package's only redactor** (S10 chunk F, mirroring
    metasalmon 0.2.5). Two implementations of one security contract is how
    the qualified-token gap arose on the R side — only one copy was extended
    when the pattern last changed — so the second, narrower KNB redactor
    (``knb_publication._redact``, mirror of the ``.ms_knb_redact()`` R 0.2.5
    deleted) is gone and every KNB boundary message routes through here,
    which is strictly stronger: this also catches ``x-api-key``, provider API
    keys, and serialized JSON credential forms the deleted version missed.
    ``tests/test_text_safety.py`` guards the exactly-one-redactor property.
    """
    text = str(value)
    text = _CREDENTIAL_NAME.sub(lambda match: match.group(1) + "=[REDACTED]", text)
    text = _BARE_SCHEME.sub(lambda match: match.group(1) + " [REDACTED]", text)
    text = _JWT.sub("[REDACTED JWT]", text)
    text = _OPENAI_KEY.sub("[REDACTED KEY]", text)
    text = _GOOGLE_KEY.sub("[REDACTED KEY]", text)
    return text


__all__ = ["redact_secrets"]
