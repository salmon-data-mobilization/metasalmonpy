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

from metasalmonpy import sdp_schema, term_search


@pytest.fixture(autouse=True)
def _pin_vendored_schema_bundle():
    sdp_schema.set_sdp_schema_source("vendored")
    yield
    sdp_schema.set_sdp_schema_source(None)


@pytest.fixture(autouse=True)
def _reset_term_search_session_caches():
    """The 0.2.2 session caches are per-process state; tests are not sessions.

    Without this, the first test to resolve an smn/gcdfo index (usually
    through a monkeypatched fetch) would feed its fixture index to every
    later test, and a cached ``find_terms`` result would cross test
    boundaries whenever a test enables ``METASALMONPY_CACHE``.
    """
    term_search._smn_index_cache.clear()
    term_search._gcdfo_index_cache.clear()
    term_search._term_cache.clear()
    yield
    term_search._smn_index_cache.clear()
    term_search._gcdfo_index_cache.clear()
    term_search._term_cache.clear()
