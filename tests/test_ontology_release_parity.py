"""The pinned-release fixtures are metasalmon's (tern ECOSYSTEM M-12).

``tests/data/ontology_release/`` is metasalmon's
``tests/testthat/fixtures/ontology-release/`` vendored unchanged, and both
packages' release tests read it, so a fixture changed on one side only would
let the two pass against different inputs. This compares the copies byte for
byte. It runs in the ``parity`` job of ``.github/workflows/parity.yml``, which
clones metasalmon ``main`` to ``/tmp/metasalmon``, and wherever
``METASALMON_PATH`` names a checkout. It skips where neither holds the
fixtures, including a clone of a metasalmon that predates them.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "data" / "ontology_release"


def _hub_fixtures():
    for candidate in (os.environ.get("METASALMON_PATH"), "/tmp/metasalmon"):
        if candidate:
            path = Path(candidate) / "tests" / "testthat" / "fixtures" / "ontology-release"
            if path.is_dir():
                return path
    return None


@pytest.mark.skipif(
    _hub_fixtures() is None,
    reason="no metasalmon checkout with the release fixtures (METASALMON_PATH or the parity job's /tmp/metasalmon)",
)
def test_the_vendored_fixtures_are_metasalmons_byte_for_byte():
    theirs = _hub_fixtures()
    ours = sorted(str(path.relative_to(FIXTURES)) for path in FIXTURES.rglob("*") if path.is_file())
    assert ours, "the vendored fixtures are missing"
    assert ours == sorted(str(path.relative_to(theirs)) for path in theirs.rglob("*") if path.is_file())
    for name in ours:
        assert (FIXTURES / name).read_bytes() == (theirs / name).read_bytes(), name
