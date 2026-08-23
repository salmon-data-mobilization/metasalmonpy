"""Redaction of text this package did not author.

Mirrors metasalmon's ``.ms_redact_secrets()`` (``R/cli-safety.R``, 0.2.0). The
rule that matters is *where* it is applied: at capture, not at display. A
provider error stored on the exported ``semantic_llm_assessments`` attribute is
written to CSV, so a display-time redactor is already too late.
"""

from __future__ import annotations

import os
import re

from metasalmonpy.text_safety import redact_secrets


def test_a_credential_header_is_redacted_to_end_of_line():
    # A cookie jar contains spaces and semicolons, so stopping at the first
    # whitespace would leave the secret in place.
    assert (
        redact_secrets("Set-Cookie: session=abc; Path=/; HttpOnly")
        == "Set-Cookie=[REDACTED]"
    )
    assert redact_secrets("Authorization: Basic dXNlcjpwYXNz") == "Authorization=[REDACTED]"


def test_a_vendor_prefixed_variable_name_is_matched():
    # ``_`` is a word character, so ``\bAPI_KEY`` never matches inside
    # ``OPENAI_API_KEY`` and the secret survived untouched.
    assert redact_secrets("OPENAI_API_KEY=sk-abc") == "OPENAI_API_KEY=[REDACTED]"
    assert redact_secrets('{"api_key":"secret"}') == '{"api_key=[REDACTED]'


def test_bare_schemes_and_key_shapes_outside_a_header():
    assert redact_secrets("token Bearer abc.def-ghi") == "token Bearer [REDACTED]"
    assert redact_secrets("eyJhbGciOi.eyJzdWIi.c2ln") == "[REDACTED JWT]"
    assert redact_secrets("sk-" + "a" * 24) == "[REDACTED KEY]"
    assert redact_secrets("AIza" + "b" * 35) == "[REDACTED KEY]"


def test_ordinary_validation_text_is_left_alone():
    """Deliberately conservative: an over-eager pattern hides what to fix."""
    message = "Table 'obs' column 'count' declares value_type 'number'."
    assert redact_secrets(message) == message


def test_any_qualified_token_name_is_redacted():
    # The structural rule (metasalmon 0.2.5): naming only ``dataone_token``
    # meant the staging credential leaked while the production one was
    # redacted — the worst possible split, and it leaked at rest, because
    # captured provider errors are written to CSV. Every expectation here is
    # byte-identical to `.ms_redact_secrets()` on metasalmon main @ 794647a
    # (chunk F differential).
    assert (
        redact_secrets("dataone_token=secretvalue trailing")
        == "dataone_token=[REDACTED]"
    )
    assert (
        redact_secrets("dataone_test_token=stagingsecret")
        == "dataone_test_token=[REDACTED]"
    )
    assert (
        redact_secrets("knb_staging_token: stagingsecret2")
        == "knb_staging_token=[REDACTED]"
    )
    assert redact_secrets("DATAONE_TOKEN=UPPERSECRET") == "DATAONE_TOKEN=[REDACTED]"


def test_token_count_diagnostics_survive():
    # ``token`` must be the final name segment: without the lookahead these
    # matched and the rule consumed the rest of the line, destroying exactly
    # the numbers a user needs to correct a rejected LLM request.
    assert redact_secrets("max_token_count = 4096") == "max_token_count = 4096"
    assert redact_secrets("total_tokens = 1500") == "total_tokens = 1500"
    assert redact_secrets("prompt_tokens: 917") == "prompt_tokens: 917"


def test_unqualified_and_continuing_token_names_are_left_alone():
    # A prefix segment is required, so prose survives; and a name continuing
    # past ``token`` is deliberately unmatched — missing an exotic name is
    # recoverable, shredding a diagnostic is not. Both verdicts match R.
    assert redact_secrets("token = 42") == "token = 42"
    assert redact_secrets("dataone_token_v2=exotic") == "dataone_token_v2=exotic"


def test_exactly_one_redactor_exists():
    # metasalmon 0.2.5 deleted its second redaction implementation
    # (`.ms_knb_redact()`) because two implementations of one security
    # contract is how only one gets extended when the pattern changes. This
    # package's mirror of that second implementation, formerly
    # ``knb_publication._redact``, retired at S10 chunk F (PARITY.md row 37's
    # retirement condition). Retires never: this is the standing assertion
    # that the contract has one implementation.
    import inspect

    from metasalmonpy import knb_publication

    assert not hasattr(knb_publication, "_redact")
    # No module but text_safety may define a redaction implementation; call
    # sites must import it. A definition is a ``def`` whose name mentions
    # redact — importing or calling ``redact_secrets`` is what SHOULD appear.
    import metasalmonpy

    package_dir = os.path.dirname(metasalmonpy.__file__)
    offenders = []
    for name in sorted(os.listdir(package_dir)):
        if not name.endswith(".py") or name == "text_safety.py":
            continue
        with open(os.path.join(package_dir, name), encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                if re.match(r"\s*def\s+_?\w*redact", line):
                    offenders.append(f"{name}:{lineno}: {line.strip()}")
    assert offenders == [], (
        "a second redaction implementation appeared; route it through "
        f"text_safety.redact_secrets instead: {offenders}"
    )
    # And the KNB boundary actually uses the shared one.
    assert (
        inspect.getsource(knb_publication._abort_safe).find("redact_secrets")
        != -1
    )


def test_the_llm_error_column_is_redacted_where_it_is_captured():
    from metasalmonpy import llm_review

    import pandas as pd

    row = llm_review._error_assessment(
        {"dataset_id": "d", "table_id": "t", "column_name": "c"},
        RuntimeError("provider said: Authorization: Bearer supersecrettoken"),
        {"provider": "openai", "model": "gpt"},
        pd.DataFrame(columns=["source"]),
    )
    assert "supersecrettoken" not in row["llm_error"]
    assert "[REDACTED]" in row["llm_error"]
