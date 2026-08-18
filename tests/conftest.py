"""Test-suite defaults.

The schema bundle loader is remote-first (``sdp_schema.load_sdp_schema``), so
without this the suite would make nine HTTP requests on the first write and
behave differently on a machine with no network. metasalmon's own suite pins
``metasalmon.sdp_schema_source = "vendored"`` for exactly this reason, and
0.2.0's NEWS records the gap that pin left: nothing had ever exercised a
successful remote fetch. ``tests/test_sdp_schema.py`` closes it here with an
injected fetcher, so the pin below costs no coverage.
"""

import pytest

from metasalmonpy import sdp_schema


@pytest.fixture(autouse=True)
def _pin_vendored_schema_bundle():
    sdp_schema.set_sdp_schema_source("vendored")
    yield
    sdp_schema.set_sdp_schema_source(None)
