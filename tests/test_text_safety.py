"""Redaction of text this package did not author.

Mirrors metasalmon's ``.ms_redact_secrets()`` (``R/cli-safety.R``, 0.2.0). The
rule that matters is *where* it is applied: at capture, not at display. A
provider error stored on the exported ``semantic_llm_assessments`` attribute is
written to CSV, so a display-time redactor is already too late.
"""

from __future__ import annotations

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
