"""The repository declares no checkout that a clone cannot produce (hub B-337).

Until 2026-09-26 ``.gitmodules`` declared a ``data/ontology`` submodule whose
url, ``../dfo-salmon-ontology``, resolves against this repository's remote to a
repository that does not exist, and three more tracked files assumed that
checkout: a pre-commit hook that could never match a file, the script it ran,
and two ``.quartoignore`` lines. None of it came from this package; the initial
commit carried it over from a website project and nothing here reads the path.
Both tests failed before those files were removed.

They read git's own view of the checkout, so they skip where there is no git
repository to read (an unpacked sdist, say).
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.fixture(scope="module")
def index_entries():
    """``(mode, path)`` for every entry in the git index."""
    if shutil.which("git") is None:
        pytest.skip("git is not on PATH")
    try:
        listing = _git("ls-files", "--stage", "-z")
    except subprocess.CalledProcessError:
        pytest.skip("not a git checkout")
    entries = []
    for record in listing.split("\0"):
        if not record:
            continue
        meta, path = record.split("\t", 1)
        entries.append((meta.split()[0], path))
    return entries


def test_every_declared_submodule_has_a_gitlink(index_entries):
    gitmodules = REPO_ROOT / ".gitmodules"
    if not gitmodules.exists():
        return
    try:
        declared = _git("config", "--file", str(gitmodules), "--get-regexp", r"^submodule\..*\.path$")
    except subprocess.CalledProcessError:
        declared = ""  # a .gitmodules with no path entries
    paths = {line.split(" ", 1)[1] for line in declared.splitlines() if " " in line}
    gitlinks = {path for mode, path in index_entries if mode == "160000"}
    assert paths - gitlinks == set()


def test_nothing_tracked_assumes_a_data_ontology_checkout(index_entries):
    # Prose may still tell the history (the changelog, the parity register, hub
    # workpads), and this file names the path to test for it; configuration and
    # code may not depend on it. `data/ontology-preferences.csv` is a different
    # file and is not matched.
    pattern = re.compile(rb"data/ontology(?![-\w.])")
    offenders = []
    for mode, path in index_entries:
        if mode == "160000" or path.endswith((".md", ".qmd")) or path.startswith((".hub/", "tests/")):
            continue
        if pattern.search((REPO_ROOT / path).read_bytes()):
            offenders.append(path)
    assert offenders == []
