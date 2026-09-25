#!/usr/bin/env python3
"""Tests for `scripts/check-changelog-window.py` (hub item B-201).

THE RED DEMONSTRATIONS ARE OF TWO KINDS.

Most tests build a throwaway git repository in a temporary directory -- a bump
commit setting `version` in pyproject.toml, then the change under test -- and
run the script against it as a subprocess, so what is asserted is the exit
code a continuous integration job would see. They are the hub's tests
(metasalmon's `scripts/tests/test_check_changelog_window.py`, hub item B-200)
in this repository's changelog shape, plus the tests of what only this copy
has: the ruled exemption.

`TestThisRepositorysHistory` replays the real thing. At `1e9245c` the check
reports both of the instances this changelog has held: B-144's entry under
`## 0.5.0`, and the calendar fix under `## 0.2.1`. It needs a full clone with
the tags, which CI's suite jobs do not have, so it skips there and runs in the
changelog-window workflow instead.

Every RED has its GREEN. A RED-only test passes when the checker rejects
everything, which is a checker nobody can use. So the bullet that is a finding
under a released heading is shown passing under `## Unreleased`, each
correction that passes with the exemption is shown failing without it, and the
ruled commit is shown red until a correction names it.

`TestTheHubCopy` is what keeps this copy and the hub's one check. It compares
every definition the two share, by syntax tree, with the hub's copy in the
metasalmon checkout that `METASALMON_PATH` names. `DIFFERS` below declares each
deliberate difference, with its reason.

STRICT. With `CHANGELOG_WINDOW_STRICT=1` in the environment, a test that
would skip for want of history, tags or a metasalmon checkout fails instead.
The changelog-window workflow sets it, because a guard that skips where it is
meant to run has not run.

The fixtures run git with GIT_CONFIG_GLOBAL and GIT_CONFIG_SYSTEM pointed at
the null device, as the hub's tests do, so that a developer's commit signing,
hooks or blame settings cannot change what a fixture commits or what blame
reports. Nothing here writes into this repository or makes a network call.

Run with the suite, or on its own:

    python3 tests/test_check_changelog_window.py

RETIRES WHEN: `scripts/check-changelog-window.py` is deleted. A behaviour
removed from the script has its test removed here in the same change, or this
file starts asserting behaviour that no longer exists.
"""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check-changelog-window.py"

EXIT_OK, EXIT_FINDINGS, EXIT_CANNOT_RUN = 0, 1, 2

STRICT = os.environ.get("CHANGELOG_WINDOW_STRICT") == "1"

QUIET_GIT = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def need(condition: object, reason: str) -> None:
    """Skip when `condition` is false, or fail in a strict run."""
    if condition:
        return
    if STRICT:
        raise AssertionError(f"CHANGELOG_WINDOW_STRICT=1, and this cannot run: {reason}")
    raise unittest.SkipTest(reason)


def load_script():
    """The script as a module. Its file name is not an identifier, so by path."""
    spec = importlib.util.spec_from_file_location("check_changelog_window_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


def heading(label: str) -> str:
    """An ATX heading, the form this repository's CHANGELOG.md uses."""
    return f"## {label}\n\n"


TITLE = "# Changelog\n\n"
DEV = heading("Unreleased")
V010 = heading("0.1.0")
SHIPPED = "* A shipped entry, described\n  over two lines.\n"
PYPROJECT = '[project]\nname = "fixture"\nversion = "{}"\n'


class Fixture:
    """A throwaway repository, driven with git and a config nobody else set."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.env = {
            **os.environ,
            **QUIET_GIT,
            "GIT_AUTHOR_NAME": "fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        }
        root.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str) -> str:
        done = subprocess.run(
            ["git", "-C", str(self.root), *args],
            env=self.env, capture_output=True, text=True,
        )
        if done.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} failed:\n{done.stderr}")
        return done.stdout.strip()

    def commit(self, message: str, files: dict[str, str]) -> str:
        for name, text in files.items():
            (self.root / name).write_text(text, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def check(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--repo", str(self.root), *args],
            env=self.env, capture_output=True, text=True,
        )


class WindowTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def released(self) -> tuple[Fixture, str]:
        """Development work, then the bump commit that makes it 0.1.0."""
        repo = Fixture(self.tmp / "repo")
        repo.commit("development", {
            "pyproject.toml": PYPROJECT.format("0.0.1.dev0"),
            "CHANGELOG.md": TITLE + DEV + SHIPPED,
        })
        bump = repo.commit("Bump the version to 0.1.0", {
            "pyproject.toml": PYPROJECT.format("0.1.0"),
            "CHANGELOG.md": TITLE + V010 + SHIPPED,
        })
        return repo, bump

    def assertExit(self, done: subprocess.CompletedProcess, code: int) -> None:
        self.assertEqual(
            done.returncode, code,
            f"expected exit {code}, got {done.returncode}\n"
            f"--- stdout\n{done.stdout}--- stderr\n{done.stderr}",
        )
        if code == EXIT_CANNOT_RUN:
            # Python exits 2 by itself when it cannot open a script, so the
            # code alone would pass these tests with no script at all.
            self.assertIn("cannot run", done.stderr)


class TestTheWindow(WindowTestCase):
    """The shape the rule exists for: a line filed under a version it missed."""

    def test_bullet_added_under_released_heading_after_the_bump_is_red(self):
        repo, _ = self.released()
        repo.commit("A late fix, filed under the release", {
            "CHANGELOG.md": TITLE + DEV + V010 + SHIPPED + "* A late fix.\n",
        })
        done = repo.check()
        self.assertExit(done, EXIT_FINDINGS)
        self.assertIn("* A late fix.", done.stderr)
        self.assertIn('under "## 0.1.0"', done.stderr)

    def test_same_bullet_under_unreleased_is_green(self):
        repo, _ = self.released()
        repo.commit("A late fix, filed under Unreleased", {
            "CHANGELOG.md": TITLE + DEV + "* A late fix.\n\n" + V010 + SHIPPED,
        })
        self.assertExit(repo.check(), EXIT_OK)

    def test_branch_forked_before_the_bump_and_merged_after_it_is_red(self):
        # B-144's shape here: its branch filed the entry under `## Unreleased`,
        # correctly for the tree it forked from, and the merge `b939fd9`, 21
        # seconds after the bump merge `67fb486`, carried it under the renamed
        # heading without a conflict to say so.
        repo = Fixture(self.tmp / "repo")
        repo.commit("development", {
            "pyproject.toml": PYPROJECT.format("0.0.1.dev0"),
            "CHANGELOG.md": TITLE + DEV + SHIPPED,
        })
        repo.git("switch", "-q", "-c", "late")
        repo.commit("A late fix, under Unreleased", {
            "CHANGELOG.md": TITLE + DEV + SHIPPED + "* A late fix.\n",
        })
        repo.git("switch", "-q", "main")
        repo.commit("Bump the version to 0.1.0", {
            "pyproject.toml": PYPROJECT.format("0.1.0"),
            "CHANGELOG.md": TITLE + V010 + SHIPPED,
        })
        repo.git("merge", "-q", "--no-ff", "-m", "Merge the late fix", "late")
        text = (repo.root / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertEqual(text, TITLE + V010 + SHIPPED + "* A late fix.\n")
        done = repo.check()
        self.assertExit(done, EXIT_FINDINGS)
        self.assertIn("* A late fix.", done.stderr)

    def test_a_branch_merged_before_the_bump_shipped_and_is_green(self):
        repo = Fixture(self.tmp / "repo")
        repo.commit("development", {
            "pyproject.toml": PYPROJECT.format("0.0.1.dev0"),
            "CHANGELOG.md": TITLE + DEV + SHIPPED,
        })
        repo.git("switch", "-q", "-c", "early")
        repo.commit("An early fix, under Unreleased", {
            "CHANGELOG.md": TITLE + DEV + SHIPPED + "* An early fix.\n",
        })
        repo.git("switch", "-q", "main")
        repo.git("merge", "-q", "--no-ff", "-m", "Merge the early fix", "early")
        repo.commit("Bump the version to 0.1.0", {
            "pyproject.toml": PYPROJECT.format("0.1.0"),
            "CHANGELOG.md": TITLE + V010 + SHIPPED + "* An early fix.\n",
        })
        self.assertExit(repo.check(), EXIT_OK)


class TestTheBumpCommit(WindowTestCase):
    """The tag when it exists, else the first commit whose version reads it."""

    def test_the_tag_names_the_bump_when_it_exists(self):
        repo, _ = self.released()
        late = repo.commit("A late fix, filed under the release", {
            "CHANGELOG.md": TITLE + V010 + SHIPPED + "* A late fix.\n",
        })
        # Without a tag the first commit reading 0.1.0 is the bump, so the
        # line missed it; a tag on the later commit says it shipped.
        self.assertExit(repo.check(), EXIT_FINDINGS)
        repo.git("tag", "-a", "v0.1.0", "-m", "0.1.0", late)
        self.assertExit(repo.check(), EXIT_OK)

    def test_a_tag_on_the_bump_keeps_the_later_line_red(self):
        repo, bump = self.released()
        repo.git("tag", "-a", "v0.1.0", "-m", "0.1.0", bump)
        repo.commit("A late fix, filed under the release", {
            "CHANGELOG.md": TITLE + V010 + SHIPPED + "* A late fix.\n",
        })
        done = repo.check()
        self.assertExit(done, EXIT_FINDINGS)
        self.assertIn("tag v0.1.0", done.stderr)

    def test_a_tag_on_a_branch_commit_is_the_bump_even_after_the_merge(self):
        # `v0.2.1`'s shape: the tag names the release commit on a pull
        # request's branch, a fix is committed on that branch after it, and
        # the merge that made the version current on `main` carries both. The
        # merge is the first commit on `main` reading the version, but the tag
        # decides, so the fix is red.
        repo = Fixture(self.tmp / "repo")
        repo.commit("development", {
            "pyproject.toml": PYPROJECT.format("0.0.1.dev0"),
            "CHANGELOG.md": TITLE + DEV + SHIPPED,
        })
        repo.git("switch", "-q", "-c", "release")
        tagged = repo.commit("release: 0.1.0", {
            "pyproject.toml": PYPROJECT.format("0.1.0"),
            "CHANGELOG.md": TITLE + V010 + SHIPPED,
        })
        repo.git("tag", "-a", "v0.1.0", "-m", "0.1.0", tagged)
        repo.commit("fix: after the release commit", {
            "CHANGELOG.md": TITLE + V010 + "* A fix after the tag.\n\n" + SHIPPED,
        })
        repo.git("switch", "-q", "main")
        repo.commit("unrelated work on main", {"NOTES": "main moved\n"})
        repo.git("merge", "-q", "--no-ff", "-m", "Merge the release", "release")
        done = repo.check()
        self.assertExit(done, EXIT_FINDINGS)
        self.assertIn("* A fix after the tag.", done.stderr)
        self.assertIn("tag v0.1.0", done.stderr)

    def test_a_heading_renamed_after_the_bump_keeps_what_shipped_green(self):
        # Why the check asks blame as well as the diff: when the heading is
        # renamed after the version moved, the section is absent at the bump
        # commit and every line under it reads as added. The lines that were
        # in the bump's tree, under `## Unreleased`, are ancestors of it and
        # pass; a line written after it is still red.
        repo = Fixture(self.tmp / "repo")
        repo.commit("development", {
            "pyproject.toml": PYPROJECT.format("0.0.1.dev0"),
            "CHANGELOG.md": TITLE + DEV + SHIPPED,
        })
        repo.commit("Bump the version to 0.1.0", {"pyproject.toml": PYPROJECT.format("0.1.0")})
        repo.commit("Rename the heading", {"CHANGELOG.md": TITLE + V010 + SHIPPED})
        self.assertExit(repo.check(), EXIT_OK)
        repo.commit("A late fix, filed under the release", {
            "CHANGELOG.md": TITLE + V010 + SHIPPED + "* A late fix.\n",
        })
        self.assertExit(repo.check(), EXIT_FINDINGS)

    def test_an_untagged_version_superseded_is_measured_where_it_stood(self):
        # While a version is current its window is open and the first commit
        # rules, so a line added under it is red at the time. Once a later
        # version supersedes it untagged, it is measured as it stood at the
        # last commit that read it: what it gained while it stood passes, and
        # a line added after that is red.
        repo, _ = self.released()
        stood = V010 + SHIPPED + "* While it stood.\n"
        repo.commit("An entry while 0.1.0 stands", {"CHANGELOG.md": TITLE + stood})
        self.assertExit(repo.check(), EXIT_FINDINGS)
        v020 = heading("0.2.0") + "* Next.\n\n"
        repo.commit("Bump the version to 0.2.0", {
            "pyproject.toml": PYPROJECT.format("0.2.0"),
            "CHANGELOG.md": TITLE + v020 + stood,
        })
        self.assertExit(repo.check(), EXIT_OK)
        repo.commit("A line under 0.1.0 after it was superseded", {
            "CHANGELOG.md": TITLE + v020 + stood + "* After it.\n",
        })
        done = repo.check()
        self.assertExit(done, EXIT_FINDINGS)
        self.assertIn("* After it.", done.stderr)
        self.assertNotIn("* While it stood.", done.stderr)

    def test_a_heading_with_no_bump_commit_is_listed_not_passed_silently(self):
        repo, _ = self.released()
        old = heading("0.0.1") + "* Older than this history.\n"
        repo.commit("Record a release older than the history", {
            "CHANGELOG.md": TITLE + V010 + SHIPPED + "\n" + old,
        })
        done = repo.check()
        self.assertExit(done, EXIT_OK)
        self.assertIn("0.0.1", done.stdout)
        self.assertIn("not checked", done.stdout)


class TestTheExemption(WindowTestCase):
    """A marked, dated correction passes; an unmarked addition does not."""

    def test_unmarked_paragraph_under_a_released_heading_is_red(self):
        repo, _ = self.released()
        repo.commit("Append to a shipped entry, unmarked", {
            "CHANGELOG.md": TITLE + V010 + SHIPPED
            + "  This entry now also covers the late fix.\n",
        })
        done = repo.check()
        self.assertExit(done, EXIT_FINDINGS)
        self.assertIn("now also covers", done.stderr)

    def test_marked_correction_paragraph_is_green_and_red_without_the_exemption(self):
        repo, _ = self.released()
        repo.commit("Correct a shipped entry", {
            "CHANGELOG.md": TITLE + V010 + SHIPPED
            + "\n  *(Correction, 2026-09-25: this entry shipped saying two\n"
            + "  lines; it is one line (see the fixture).)*\n",
        })
        self.assertExit(repo.check(), EXIT_OK)
        self.assertExit(repo.check("--no-exemption"), EXIT_FINDINGS)

    def test_corrected_bracket_inserted_into_a_line_is_green_and_red_without_it(self):
        # A bracket inserted into a shipped line splits it in two, so one line
        # more than shipped is there now.
        repo, _ = self.released()
        repo.commit("Correct a shipped line in place", {
            "CHANGELOG.md": TITLE + V010
            + "* A shipped entry [corrected 2026-09-25: it was a\n"
            + "  different entry], described\n  over two lines.\n",
        })
        self.assertExit(repo.check(), EXIT_OK)
        self.assertExit(repo.check("--no-exemption"), EXIT_FINDINGS)

    def test_a_marker_that_never_closes_exempts_nothing(self):
        repo, _ = self.released()
        repo.commit("Correct a shipped entry, unclosed", {
            "CHANGELOG.md": TITLE + V010 + SHIPPED
            + "\n  *(Correction, 2026-09-25: this entry shipped saying two\n"
            + "  lines, and the marker is never closed.\n",
        })
        self.assertExit(repo.check(), EXIT_FINDINGS)

    def test_an_undated_marker_exempts_nothing(self):
        repo, _ = self.released()
        repo.commit("Correct a shipped entry, undated", {
            "CHANGELOG.md": TITLE + V010 + SHIPPED
            + "\n  *(Correction: this entry shipped saying two lines.)*\n",
        })
        self.assertExit(repo.check(), EXIT_FINDINGS)


class TestTheRuling(WindowTestCase):
    """A ruled commit is exempt only under its heading and only while cited.

    These call the script's `run()` in-process, because a ruling names a
    commit by its SHA, which only exists once the fixture has made it. The
    fixture's own git settings are in the environment for the call.
    """

    def ruled(self) -> tuple[Fixture, str]:
        """0.1.0 tagged on its bump, then a fix filed under it: `10d0616`'s shape."""
        repo, bump = self.released()
        repo.git("tag", "-a", "v0.1.0", "-m", "0.1.0", bump)
        fix = repo.commit("fix: after the tag", {
            "CHANGELOG.md": TITLE + DEV + V010 + SHIPPED
            + "\n* A fix the tag does not contain,\n  and not in the tag.\n",
        })
        return repo, fix

    def run_check(self, repo: Fixture, rulings: tuple, exemption: bool = True):
        module = load_script()
        rulings = tuple(module.Ruling(*r) for r in rulings)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, repo.env, clear=True), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = module.run(repo.root, "HEAD", module.PROFILE, exemption, rulings)
        return code, out.getvalue(), err.getvalue()

    def correct(self, repo: Fixture, naming: str) -> None:
        text = (repo.root / "CHANGELOG.md").read_text(encoding="utf-8")
        repo.commit("Say that the fix missed the tag", {
            "CHANGELOG.md": text
            + f"\n  *(Correction, 2026-09-25: the fix above is `{naming}`, which\n"
            + "  is not in the `v0.1.0` tag.)*\n",
        })

    def test_a_ruled_commit_is_red_until_a_correction_names_it(self):
        repo, fix = self.ruled()
        ruling = (("0.1.0", fix, "a fixture ruling"),)
        code, _, err = self.run_check(repo, ruling)
        self.assertEqual(code, EXIT_FINDINGS, err)
        self.assertIn("A fix the tag does not contain,", err)
        self.assertIn(f"naming {fix[:7]}", err)
        self.assertIn("a fixture ruling", err)
        self.correct(repo, fix[:7])
        code, out, err = self.run_check(repo, ruling)
        self.assertEqual(code, EXIT_OK, err)
        self.assertIn(f"2 added lines exempt by ruling ({fix[:7]})", out)

    def test_a_correction_naming_another_commit_does_not_cite_it(self):
        repo, fix = self.ruled()
        other = "f" * 7 if not fix.startswith("f") else "e" * 7
        self.correct(repo, other)
        code, _, err = self.run_check(repo, (("0.1.0", fix, "a fixture ruling"),))
        self.assertEqual(code, EXIT_FINDINGS, err)
        self.assertIn("A fix the tag does not contain,", err)

    def test_the_ruling_exempts_only_its_own_commit(self):
        repo, fix = self.ruled()
        self.correct(repo, fix[:7])
        text = (repo.root / "CHANGELOG.md").read_text(encoding="utf-8")
        repo.commit("Another late line under the release", {
            "CHANGELOG.md": text + "\n* Another late line.\n",
        })
        code, _, err = self.run_check(repo, (("0.1.0", fix, "a fixture ruling"),))
        self.assertEqual(code, EXIT_FINDINGS, err)
        self.assertIn("* Another late line.", err)
        self.assertNotIn("A fix the tag does not contain,", err)

    def test_the_ruling_exempts_its_commit_only_under_its_own_heading(self):
        repo, fix = self.ruled()
        self.correct(repo, fix[:7])
        code, _, err = self.run_check(repo, (("0.2.0", fix, "a ruling for another heading"),))
        self.assertEqual(code, EXIT_FINDINGS, err)
        self.assertIn("A fix the tag does not contain,", err)

    def test_no_exemption_switches_the_ruling_off(self):
        repo, fix = self.ruled()
        self.correct(repo, fix[:7])
        ruling = (("0.1.0", fix, "a fixture ruling"),)
        self.assertEqual(self.run_check(repo, ruling)[0], EXIT_OK)
        code, _, err = self.run_check(repo, ruling, exemption=False)
        self.assertEqual(code, EXIT_FINDINGS, err)
        self.assertIn("A fix the tag does not contain,", err)

    def test_there_is_one_ruling_and_it_is_brett_s_of_2026_09_25(self):
        # A ruling is Brett's to make, so a second one is a change this test
        # has to be told about rather than one that arrives unannounced.
        module = load_script()
        self.assertEqual(
            [(r.version, r.commit) for r in module.RULINGS],
            [("0.2.1", "10d06168a77877f09e39258ae41115fcc564ebaf")],
        )
        self.assertIn("Brett, 2026-09-25", module.RULINGS[0].reason)


class TestWhatItDoesNotCover(WindowTestCase):
    """The docstring's limits, pinned so that they stay true."""

    def test_a_line_changed_in_place_is_not_a_finding(self):
        repo, _ = self.released()
        repo.commit("Fix a typo in a shipped entry", {
            "CHANGELOG.md": TITLE + V010 + "* A shipped entry, now described\n  over two lines.\n",
        })
        self.assertExit(repo.check("--no-exemption"), EXIT_OK)

    def test_reordering_shipped_bullets_is_not_a_finding(self):
        # A moved line is not absent from the section at the bump, though the
        # diff removes it in one place and inserts it in another and blame
        # credits whoever moved it.
        repo = Fixture(self.tmp / "repo")
        bullets = ["* First shipped bullet.\n", "* Second shipped bullet.\n",
                   "* Third shipped bullet.\n"]
        repo.commit("Bump the version to 0.1.0", {
            "pyproject.toml": PYPROJECT.format("0.1.0"),
            "CHANGELOG.md": TITLE + V010 + "".join(bullets),
        })
        repo.commit("Put the third bullet first", {
            "CHANGELOG.md": TITLE + V010 + bullets[2] + bullets[0] + bullets[1],
        })
        self.assertExit(repo.check("--no-exemption"), EXIT_OK)

    def test_a_line_moved_in_from_another_heading_is_red(self):
        # Moving is forgiven within a section only. A line moved under a
        # released heading from anywhere else changes that release's record.
        repo, _ = self.released()
        repo.commit("A late fix, under Unreleased", {
            "CHANGELOG.md": TITLE + DEV + "* A late fix.\n\n" + V010 + SHIPPED,
        })
        self.assertExit(repo.check(), EXIT_OK)
        repo.commit("Move it under the release", {
            "CHANGELOG.md": TITLE + DEV + V010 + SHIPPED + "* A late fix.\n",
        })
        done = repo.check()
        self.assertExit(done, EXIT_FINDINGS)
        self.assertIn("* A late fix.", done.stderr)

    def test_a_new_line_beside_a_line_changed_in_place_is_still_red(self):
        # One replaced run: the shipped line is edited and a new line is
        # written beside it. The edit stands for the line it replaced; the new
        # one is added, and it is the new one that is reported.
        repo, _ = self.released()
        repo.commit("Edit a shipped line and add one beside it", {
            "CHANGELOG.md": TITLE + V010 + "* A shipped entry, described\n"
            + "  Something new that did not ship.\n  over two lines, edited.\n",
        })
        done = repo.check()
        self.assertExit(done, EXIT_FINDINGS)
        self.assertIn("Something new that did not ship.", done.stderr)
        self.assertNotIn("over two lines, edited.", done.stderr)

    def test_the_edited_line_stands_for_its_original_even_beside_a_short_one(self):
        # The strings are from metasalmon's NEWS.md, its 0.4.0 correction at
        # line 1664, where the hub measured this: the shipped line was edited
        # and grew into a correction, whose short closing line shares a few of
        # its words. Pairing by difflib's ratio chose the closing line and
        # reported the edited one as added; pairing by characters kept does not.
        repo = Fixture(self.tmp / "repo")
        repo.commit("development", {
            "pyproject.toml": PYPROJECT.format("0.0.1.dev0"),
            "CHANGELOG.md": TITLE + DEV + "* An entry.\n  both Python readers already did.\n",
        })
        repo.commit("Bump the version to 0.1.0", {
            "pyproject.toml": PYPROJECT.format("0.1.0"),
            "CHANGELOG.md": TITLE + V010 + "* An entry.\n  both Python readers already did.\n",
        })
        repo.commit("Correct it", {
            "CHANGELOG.md": TITLE + V010 + "* An entry.\n"
            + "  one Python reader already did. *(Correction, 2026-08-24: this entry shipped\n"
            + "  how widely it already held.)*\n",
        })
        self.assertExit(repo.check(), EXIT_OK)
        done = repo.check("--no-exemption")
        self.assertExit(done, EXIT_FINDINGS)
        self.assertIn("how widely it already held.)*", done.stderr)
        self.assertNotIn("one Python reader already did.", done.stderr)


class TestItCannotBeFooled(WindowTestCase):
    """A history it cannot see must never look like a history that is clean."""

    def test_a_shallow_clone_cannot_run(self):
        repo, _ = self.released()
        repo.commit("A late fix, filed under the release", {
            "CHANGELOG.md": TITLE + V010 + SHIPPED + "* A late fix.\n",
        })
        shallow = self.tmp / "shallow"
        subprocess.run(
            ["git", "clone", "-q", "--depth", "1", f"file://{repo.root}", str(shallow)],
            env=repo.env, check=True, capture_output=True,
        )
        done = subprocess.run(
            [sys.executable, str(SCRIPT), "--repo", str(shallow)],
            env=repo.env, capture_output=True, text=True,
        )
        self.assertExit(done, EXIT_CANNOT_RUN)
        self.assertIn("shallow", done.stderr)

    def test_a_superseded_release_the_policy_tags_needs_its_tag(self):
        # AGENTS.md tags every release from 0.3.0 forward, which here means
        # 0.4.0 and 0.5.0. In a clone without the tags, 0.4.0 would read as
        # untagged history and be measured where it last stood, which forgives
        # anything added while it was current. So it cannot run instead.
        repo = Fixture(self.tmp / "repo")
        v040 = heading("0.4.0") + SHIPPED
        bump = repo.commit("Bump the version to 0.4.0", {
            "pyproject.toml": PYPROJECT.format("0.4.0"),
            "CHANGELOG.md": TITLE + v040,
        })
        repo.commit("Bump the version to 0.5.0", {
            "pyproject.toml": PYPROJECT.format("0.5.0"),
            "CHANGELOG.md": TITLE + heading("0.5.0") + "* Next.\n\n" + v040,
        })
        done = repo.check()
        self.assertExit(done, EXIT_CANNOT_RUN)
        self.assertIn("v0.4.0", done.stderr)
        repo.git("tag", "-a", "v0.4.0", "-m", "0.4.0", bump)
        self.assertExit(repo.check(), EXIT_OK)

    def test_below_the_floor_an_untagged_release_is_untagged_history(self):
        # 0.2.1 is below the floor, so without its tag it is measured as it
        # stood rather than refused: the policy does not promise that tag.
        repo = Fixture(self.tmp / "repo")
        v021 = heading("0.2.1") + SHIPPED
        repo.commit("Bump the version to 0.2.1", {
            "pyproject.toml": PYPROJECT.format("0.2.1"),
            "CHANGELOG.md": TITLE + v021,
        })
        repo.commit("Bump the version to 0.4.0", {
            "pyproject.toml": PYPROJECT.format("0.4.0"),
            "CHANGELOG.md": TITLE + heading("0.4.0") + "* Next.\n\n" + v021,
        })
        done = repo.check()
        self.assertExit(done, EXIT_OK)
        self.assertIn("never tagged, and superseded", done.stdout)

    def test_a_missing_changelog_cannot_run(self):
        repo = Fixture(self.tmp / "repo")
        repo.commit("no changelog", {"pyproject.toml": PYPROJECT.format("0.1.0")})
        self.assertExit(repo.check(), EXIT_CANNOT_RUN)

    def test_released_headings_none_of_which_it_can_check_cannot_run(self):
        # The wrong repository, or a version file it cannot read: every
        # released heading unchecked is a run that verified nothing.
        repo = Fixture(self.tmp / "repo")
        repo.commit("a changelog no version file ever named", {
            "pyproject.toml": '[project]\nname = "fixture"\n',
            "CHANGELOG.md": TITLE + V010 + SHIPPED,
        })
        self.assertExit(repo.check(), EXIT_CANNOT_RUN)


# The finding groups the script writes to stderr, one per run of lines.
GROUP = re.compile(r'^  CHANGELOG\.md:(\d+)(?:-(\d+))? under "([^"]+)", added by ([0-9a-f]{7}),', re.M)


def findings_by_commit(stderr: str) -> dict[tuple[str, str], int]:
    """(heading, commit) -> how many lines the check reported for it."""
    counts: dict[tuple[str, str], int] = {}
    for m in GROUP.finditer(stderr):
        first = int(m.group(1))
        last = int(m.group(2) or first)
        key = (m.group(3), m.group(4))
        counts[key] = counts.get(key, 0) + last - first + 1
    return counts


class TestThisRepositorysHistory(unittest.TestCase):
    """The RED demonstration from this repository's own history.

    Run against this checkout at fixed commits, so what `main` does next
    cannot change what these assert. They need the whole history and the
    tags; CI's suite jobs check out one commit, and skip them.
    """

    COMMITS = ("1e9245c", "3f8349a", "10d0616", "f1d9b0e", "3fdd323")
    TAGS = ("v0.2.1", "v0.4.0", "v0.5.0")

    def setUp(self) -> None:
        self.env = {**os.environ, **QUIET_GIT}
        shallow = self.git("rev-parse", "--is-shallow-repository")
        need(shallow is not None and shallow.returncode == 0,
             f"{ROOT} is not a git checkout, or git is not on PATH")
        need(shallow.stdout.strip() == "false", f"{ROOT} is a shallow clone")
        for commit in self.COMMITS:
            need(self.git("cat-file", "-e", f"{commit}^{{commit}}").returncode == 0,
                 f"{commit} is not in this clone")
        for tag in self.TAGS:
            need(self.git("rev-parse", "-q", "--verify", f"refs/tags/{tag}").returncode == 0,
                 f"{tag} is not in this clone")

    def git(self, *args: str):
        try:
            return subprocess.run(["git", "-C", str(ROOT), *args],
                                  env=self.env, capture_output=True, text=True)
        except OSError:
            return None

    def check(self, *args: str) -> subprocess.CompletedProcess:
        # No --repo: the default is this checkout, which is what AGENTS.md runs.
        return subprocess.run([sys.executable, str(SCRIPT), *args],
                              env=self.env, capture_output=True, text=True)

    def test_at_1e9245c_it_reports_b144_and_the_calendar_fix(self):
        # B-144's entry: 37 lines under `## 0.5.0`, blamed on its branch
        # commit `773f5d4`, which `b939fd9` (#32) merged 21 seconds after the
        # bump merge `67fb486`. And the calendar fix: 24 lines under the
        # tagged `## 0.2.1`, from `10d0616`, which the tag does not contain
        # and which no correction names yet, so its ruling does not hold.
        done = self.check("--rev", "1e9245c")
        self.assertEqual(done.returncode, EXIT_FINDINGS, done.stdout + done.stderr)
        self.assertEqual(findings_by_commit(done.stderr), {
            ("## 0.5.0", "773f5d4"): 37,
            ("## 0.2.1", "10d0616"): 24,
        })
        self.assertIn("61 lines under a released heading missed that release", done.stderr)
        self.assertIn("naming 10d0616", done.stderr)
        self.assertIn("tag v0.5.0", done.stdout)
        self.assertIn("tag v0.2.1", done.stdout)

    def test_from_3f8349a_only_the_calendar_fix_is_left(self):
        # Pull request #35 moved B-144's entry to `## Unreleased`, verbatim,
        # and that cleared it: a move out of a released section is a deletion
        # there. The calendar fix stayed, so the check was not green from
        # `3f8349a`, which is what B-201's corrected condition records.
        done = self.check("--rev", "3f8349a")
        self.assertEqual(done.returncode, EXIT_FINDINGS, done.stdout + done.stderr)
        self.assertEqual(findings_by_commit(done.stderr), {("## 0.2.1", "10d0616"): 24})

    def test_the_ruling_rests_on_the_tag_where_it_is(self):
        # The ruling's premise, so that it cannot outlive it: `v0.2.1` names
        # `f1d9b0e`, `10d0616` is not in it, and the merge that made 0.2.1
        # current on `main`, and the first tag after, both contain it. If the
        # tag is ever moved onto a commit that contains the fix, this fails,
        # and the ruling is to be deleted rather than this test weakened.
        tagged = self.git("rev-parse", "v0.2.1^{commit}").stdout.strip()
        self.assertTrue(tagged.startswith("f1d9b0e"), tagged)
        ancestor = lambda a, b: self.git("merge-base", "--is-ancestor", a, b).returncode == 0
        self.assertFalse(ancestor("10d0616", "v0.2.1"))
        self.assertTrue(ancestor("f1d9b0e", "10d0616"))
        self.assertTrue(ancestor("10d0616", "3fdd323"))
        self.assertTrue(ancestor("10d0616", "v0.4.0"))


# Every definition this copy does not share with the hub's, with the reason.
# Anything not named here has to match the hub's copy exactly, by syntax tree.
DIFFERS = {
    "PROFILES": "the hub's only: its two profiles, one of which reads this "
                "repository as a sibling checkout. This copy has PROFILE.",
    "PROFILE": "this copy's only: the hub's metasalmonpy profile, read from this "
               "checkout, with the tagging floor this AGENTS.md sets.",
    "Ruling": "this copy's only: the hub's copy has nowhere to hold a ruled exemption.",
    "RULINGS": "this copy's only: Brett's ruling of 2026-09-25 for 10d0616.",
    "corrections": "this copy's only: reads the corrections that cite a ruling.",
    "run": "applies RULINGS and reports what they exempt.",
    "main": "checks this checkout with PROFILE, where the hub's picks a profile "
            "and, for this repository, a sibling checkout.",
}


def definitions(path: Path) -> dict[str, str]:
    """Each top-level definition's name -> its syntax tree, without positions."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found[node.name] = ast.dump(node)
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            found[node.targets[0].id] = ast.dump(node)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            found[node.target.id] = ast.dump(node)
    return found


class TestTheHubCopy(unittest.TestCase):
    """This copy and the hub's are one check, apart from what DIFFERS declares."""

    def test_every_shared_definition_is_the_hub_s(self):
        path = os.environ.get("METASALMON_PATH")
        need(path, "METASALMON_PATH names no metasalmon checkout")
        hub = Path(path) / "scripts" / "check-changelog-window.py"
        need(hub.is_file(), f"there is no {hub}")
        ours, theirs = definitions(SCRIPT), definitions(hub)
        problems = []
        for name in sorted(set(theirs) - set(ours) - set(DIFFERS)):
            problems.append(f"{name}: the hub's copy defines it and this one does not. "
                            "Port it, or declare in DIFFERS why not.")
        for name in sorted(set(ours) - set(theirs) - set(DIFFERS)):
            problems.append(f"{name}: this copy defines it and the hub's does not. "
                            "Declare it in DIFFERS with its reason.")
        for name in sorted((set(ours) & set(theirs)) - set(DIFFERS)):
            if ours[name] != theirs[name]:
                problems.append(f"{name}: differs from the hub's copy. The two are one "
                                "check, so change both, or declare it in DIFFERS.")
        for name in sorted(DIFFERS):
            if name not in ours and name not in theirs:
                problems.append(f"{name}: declared in DIFFERS, and neither copy has it.")
            elif name in ours and name in theirs and ours[name] == theirs[name]:
                problems.append(f"{name}: declared in DIFFERS, and the two copies match. "
                                "Remove it from DIFFERS.")
        self.assertEqual(problems, [], f"against {hub}:\n" + "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
