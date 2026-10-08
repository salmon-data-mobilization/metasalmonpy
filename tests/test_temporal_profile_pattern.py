"""A written package's temporal fields, checked against the pattern the SDP
profile itself declares for them. Hub item B-199, the twin of metasalmon's
``tests/testthat/test-temporal-profile-pattern.R`` (hub item B-198).

WHY THIS FILE EXISTS. Every other check on these two fields compares one writer
with another -- ``datapackage.json`` against ``metadata/dataset.csv``
(``tests/test_platform_determinism_guard.py``, hub item B-145) -- or a writer
against a spelling typed into the test. Nothing compared either file with the
profile's own ``constraints.pattern``. So when the descriptor began writing a
typed instant, the two files agreed with each other while both broke the
profile, and the break went unseen through two implementations and four
reviews (the Q-51 passage in the hub's ``knowledge/backlog.md``). Two writers
agreeing does not make either of them conform; these tests check conformance.

The pattern is READ from the vendored bundle through the package's own vendored
loader, never typed here. A regex copied into this file would pass against
itself whatever the bundle said, and that is exactly the comparison that was
missing.

FOUR-DIGIT YEARS ONLY, ON PURPOSE. Which bytes the ecosystem writes for a year
below 1000 is hub item B-161's question, and this package's emission half is
B-207. This package pads that year by construction, while metasalmon writes
whatever ``readr::write_csv()`` writes, which on Linux is unpadded and fails the
pattern. A four-digit year keeps these tests off that platform-dependent case
instead of pinning a rendering nobody has ruled on. *The exclusion retires
when:* B-161 is ruled and B-207 emits the ruled pre-1000 spelling. At that point
a pre-1000 instant belongs in the first test below, and the pattern it is
checked against may have changed.

*Retires when:* nothing. When ``validate_salmon_datapackage()`` enforces
``constraints.pattern`` (hub item B-205), that is a second and more general
check of the same property. These tests stay, because they pin the writer and
the vendored bundle to each other without depending on the validator being
right.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path

import pandas as pd

from metasalmonpy import sdp_schema, write_salmon_datapackage


def _matches_profile_pattern(pattern: str, value: str) -> bool:
    """Full-match semantics, which is how the specification applies a pattern.

    smn-data-pkg's own validator calls ``re.fullmatch`` (``scripts/
    validate_package.py`` at ``sdp-0.3.2``). ``re.search`` or ``re.match``
    would not do: the pattern ends in ``$``, which also matches just before a
    trailing newline, so either would accept ``"...Z\\n"``.
    """
    return re.fullmatch(pattern, value) is not None


def _vendored_dataset_field(field_name: str) -> dict:
    bundle = sdp_schema._load_vendored_sdp_schema()
    fields = [
        field
        for field in bundle["metadata_schemas"]["dataset"]["fields"]
        if field.get("name") == field_name
    ]
    assert len(fields) == 1, field_name
    return fields[0]


def _vendored_dataset_field_pattern(field_name: str) -> str:
    pattern = (_vendored_dataset_field(field_name).get("constraints") or {}).get(
        "pattern"
    )
    # A bundle that dropped the pattern must fail here. It must not make every
    # value pass by default.
    assert isinstance(pattern, str) and pattern, field_name
    return pattern


def test_the_temporal_instants_a_written_package_carries_satisfy_the_vendored_profile_pattern(
    tmp_path,
):
    # The condition B-199 retires on. It pins what the WRITER emits against the
    # vendored pattern. Making the VALIDATOR read ``constraints.pattern`` at
    # all is B-205's job, not this test's.
    start_pattern = _vendored_dataset_field_pattern("temporal_start")
    end_pattern = _vendored_dataset_field_pattern("temporal_end")

    target = Path(
        write_salmon_datapackage(
            {"obs": pd.DataFrame({"site_id": ["s1", "s2"]})},
            pd.DataFrame(
                [
                    {
                        "dataset_id": "d1",
                        "title": "T",
                        "description": "D",
                        "creator": "metasalmonpy tests",
                        # The two instants the ruled profile gives as its own
                        # ``sdp:examples`` (smn-data-pkg ``sdp-0.3.2``). The end
                        # is supplied at UTC-8, Vancouver's offset in December,
                        # on purpose, because the pattern admits only the ``Z``
                        # zone marker. A writer that kept the caller's offset
                        # would fail here.
                        "temporal_start": _dt.datetime(
                            1996, 1, 1, 0, 0, 0, tzinfo=_dt.timezone.utc
                        ),
                        "temporal_end": _dt.datetime(
                            2024,
                            12,
                            31,
                            15,
                            59,
                            59,
                            tzinfo=_dt.timezone(_dt.timedelta(hours=-8)),
                        ),
                    }
                ]
            ),
            pd.DataFrame(
                [
                    {
                        "dataset_id": "d1",
                        "table_id": "obs",
                        "file_name": "data/obs.csv",
                        "table_label": "Observations",
                        "description": "One site column",
                    }
                ]
            ),
            pd.DataFrame(
                [
                    {
                        "dataset_id": "d1",
                        "table_id": "obs",
                        "column_name": "site_id",
                        "column_label": "Site",
                        "column_description": "Site identifier",
                        "column_role": "identifier",
                        "value_type": "string",
                        "required": False,
                    }
                ]
            ),
            path=str(tmp_path / "pkg"),
            overwrite=True,
        )
    )

    descriptor = json.loads((target / "datapackage.json").read_text(encoding="utf-8"))
    dataset_csv = pd.read_csv(
        target / "metadata" / "dataset.csv", dtype=str, keep_default_na=False
    ).iloc[0]

    # The values under test are typed instants, not dates that slipped
    # through. The date branch of the pattern would accept a date, so without
    # these two lines a writer that dropped the time would pass unnoticed.
    assert descriptor["temporal"]["start"] == "1996-01-01T00:00:00Z"
    assert descriptor["temporal"]["end"] == "2024-12-31T23:59:59Z"

    # The descriptor instant satisfies the pattern the vendored profile
    # declares.
    assert _matches_profile_pattern(start_pattern, descriptor["temporal"]["start"])
    assert _matches_profile_pattern(end_pattern, descriptor["temporal"]["end"])

    # So does the CSV, which is the file the pattern is declared on.
    assert _matches_profile_pattern(start_pattern, dataset_csv["temporal_start"])
    assert _matches_profile_pattern(end_pattern, dataset_csv["temporal_end"])


def test_the_profile_pattern_check_can_fail_it_rejects_the_spellings_the_ruling_excludes():
    # Without this, the test above could pass vacuously. A matcher that
    # accepted everything, or a pattern read from the wrong field, would pass
    # there without anyone noticing. These are smn-data-pkg's own rejected
    # fixtures (``tests/test_validate_package.py`` at ``sdp-0.3.2``), minus the
    # unpadded year, plus a partial date and a trailing newline. The unpadded
    # year is B-161's question and is deliberately not pinned here.
    pattern = _vendored_dataset_field_pattern("temporal_end")

    rejected = {
        "no zone marker, the descriptor's spelling before B-145": "2024-12-31T00:00:00",
        "space separator, what to_csv wrote before B-145": "2024-12-31 00:00:00",
        "offset zone marker, what isoformat() gives an aware value": (
            "2024-12-31T00:00:00+00:00"
        ),
        "fractional second": "2024-12-31T00:00:00.5Z",
        "two-digit year": "24-12-31",
        "partial date": "1996-01",
        "trailing newline, which a bare $ would accept": "2024-12-31T00:00:00Z\n",
    }
    for label, value in rejected.items():
        assert not _matches_profile_pattern(pattern, value), label

    # The control, so the rejections above are the pattern's answer and not a
    # matcher that rejects everything: the profile's own examples satisfy its
    # own pattern, and since the Q-51 ruling they include a typed instant.
    examples = _vendored_dataset_field("temporal_end").get("sdp:examples")
    assert "2024-12-31T23:59:59Z" in examples
    for example in examples:
        assert _matches_profile_pattern(pattern, example), example
