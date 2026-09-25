#!/usr/bin/env python3
"""Fail when a line under a released changelog heading is not in that release.

This is metasalmonpy's copy of the hub's check: hub item B-201, the pair of
B-200. AGENTS.md's Releases section carries the rule, which Brett accepted on
2026-09-16: a change that merges after the commit that bumped the version and
before that version's tag exists is filed under `## Unreleased` -- never under
the version it did not ship in -- and the tag stays on the bump commit. The
window opens on every release, because the tag is a separate act from the
bump, and here it was missed within a minute of opening: B-144's merge
`b939fd9` landed 21 seconds after the bump merge `67fb486`, descends from it,
and filed its entry under `## 0.5.0`. A sentence did not stop that, so this is
the rule's mechanical form.

A port, not a vendored copy
---------------------------
The hub's copy is metasalmon's `scripts/check-changelog-window.py`, git blob
`f984c4c6`, as its pull request #173 merged it (`f8681c0`). It could not be
vendored byte for byte. It reads this repository only as a sibling of the hub,
it leaves this repository's tagging floor unset, and it has nowhere to hold the
exemption Brett ruled for this changelog. So this copy is that blob with one
profile, this repository's, read from this checkout by default; the floor
this repository's AGENTS.md sets; the ruled exemption; and this docstring.
Every other definition is the hub's, unchanged.

What keeps the two copies one check is `tests/test_check_changelog_window.py`.
It compares every definition the two share, by syntax tree, with the hub's copy
in the metasalmon checkout that `METASALMON_PATH` names. It fails on a
definition the hub has and this copy lacks, on a shared one that differs, and
on a declared difference that has stopped differing. The differences are
declared in that file, each with its reason, and that list is the authority
rather than this paragraph. The changelog-window workflow clones metasalmon and
runs the comparison on every pull request, so a change to either copy turns
this repository red until the other one follows it.

What it checks
--------------
For every released version heading in `CHANGELOG.md` at the revision checked:

1. The BUMP COMMIT is the version's `vX.Y.Z` tag when one exists, and
   otherwise the first commit on the first-parent history of the checked
   revision whose `pyproject.toml` reads that version -- the open window
   between bump and tag. First-parent, because that is the commit that made
   the version current on `main`, which is where AGENTS.md says the tag
   belongs and the only commit the Release workflow will tag. Measured
   2026-09-25, it is the commit that five of this repository's seven `v*`
   tags name. `v0.2.0` and `v0.2.1` name commits on pull request #10's branch.
   Both were tagged before AGENTS.md said which commit a tag belongs on, and a
   tag, where there is one, is the bump.
   One exception, inherited from the hub, where it keeps `main` green: an
   untagged version that a later version has superseded will never have a
   tag, and it is measured as it stood at the LAST commit whose
   `pyproject.toml` read it. Here those versions are 0.1.2 and 0.1.3, and
   measured 2026-09-25 the exception holds nothing: read at their first
   commits instead, neither gains a finding. A line added under a superseded
   version after it was superseded is still a finding. *Retires when:* the
   hub's copy retires it.
2. The section as it stood at the bump commit is diffed against the section
   as it stands now. A line the diff inserts is ADDED, unless the diff
   removed a line of exactly its text from elsewhere in the section: that is
   a move within it, so reordering shipped entries is not a finding. A run
   that replaces shipped lines stands for them one for one -- the lines that
   keep the most of them, in order -- and only the lines by which it outgrows
   them are added.
3. An added line is a FINDING when `git blame` attributes it to a commit that
   is not an ancestor of the bump commit (`git merge-base --is-ancestor`):
   the line was not in the tree the release names. Blame is asked as well as
   the diff because a heading renamed after the bump leaves the section
   absent at the bump commit; the lines that were there, under
   `## Unreleased`, are blamed on ancestors and pass.

THE EXEMPTIONS. There are two, and each needs a marker. Neither is a hole.

A marked, dated correction to a shipped entry is not a finding, because the
Releases section admits exactly that: a `*(Correction, YYYY-MM-DD: ...)*`
paragraph appended to an entry, or a `[corrected YYYY-MM-DD: ...]` bracket
inserted into one. An added line is exempt when such a marker, closed by its
matching bracket inside its own paragraph, covers any part of it. An undated
or unclosed marker exempts nothing, and an unmarked paragraph is a finding.

One commit's lines are exempt by ruling, and `RULINGS` names it, with the
reason beside it. It is `10d0616`, the calendar fix under `## 0.2.1`, which
pull request #10 merged after the commit that `v0.2.1` names. Brett ruled on
2026-09-25 that the tag does not move and that the entry says so. So the
exemption holds only while the 0.2.1 entry carries a marked, dated correction
that names the commit. Without one its 24 lines are findings, as they are at
`1e9245c`.

`--no-exemption` switches both off, to show what they are holding. Measured
2026-09-25 on the change that added the correction, the check passed with them
and failed without them, on 10d0616's lines and on the correction's.

WHAT IT DOES NOT COVER. A guard whose claimed scope exceeds its real scope is
worse than none, so:

* A line changed in place under a released heading is not a finding, so a
  typo fix to a shipped entry stays possible -- and so does any rewrite that
  keeps to the lines it replaced, including one that turns a shipped bullet
  into a description of new work. `10d0616` did that once: it changed
  "PARITY.md row 39 is new" to "rows 39 and 40 are new" in place, and row 40
  is not in the `v0.2.1` tag either. Its correction says so; this check does
  not.
* A paragraph carrying the correction marker passes whatever it says,
  including new work, and so does text sharing a line with a marker.
  Reading catches those; this check does not.
* The ruled exemption is by commit, as ruled. A later edit to one of those
  lines is blamed on its editor and is a finding, unless the edit carries a
  correction marker of its own.
* A line added to an untagged version while it stood, after its bump, passes
  once a later version supersedes it (see 1), including one that was red when
  it merged and was merged anyway. A version that is tagged is not forgiven:
  its tag is its bump for good.
* A shipped line moved within its section passes only with its text
  unchanged; moved and edited, it reads as an addition. A line moved in from
  any other heading is an addition, which is the point: it changes what that
  release records.
* A deleted line is never a finding, and a blank line is never checked.
* A heading with no bump commit is not checked: 0.1.0, which is older than
  the history (`pyproject.toml` reads 0.1.2 at the first commit), or a
  version no `pyproject.toml` has read yet. Those are listed in the output
  rather than skipped silently, and a run that could check no released
  heading at all cannot run rather than passes.
* The tags are trusted, and required where this repository's policy says a
  release is tagged, which AGENTS.md says of every one from 0.3.0 forward.
  There was never a 0.3.0, so today that means 0.4.0 and 0.5.0. Every
  released heading from 0.1.6 to 0.2.1 has a tag too, but the policy does
  not promise one. So a clone holding only some tags reads a missing tag
  there as untagged history, and a clone with no tags at all cannot run,
  because 0.4.0 has none. A tag on the wrong commit makes this measure the
  wrong tree; AGENTS.md says which commit a tag belongs on.
* It reads a committed revision, never the working tree.

It is a check on this repository and not part of the package, so `MANIFEST.in`
keeps it out of the source distribution, and out of the wheel built from it.

Usage
-----
    python3 scripts/check-changelog-window.py
    python3 scripts/check-changelog-window.py --rev 1e9245c --no-exemption

With no arguments it checks `CHANGELOG.md` at `HEAD` in this checkout, and
`--repo` names another. It exits 0 with no findings, 1 on any finding, and 2
when it cannot run: not a git repository, a shallow clone (blame and ancestry
need the whole history), no changelog, or nothing it could check. A history
it cannot see must never look like a history that is clean.

*Retires when:* the changelog stops being written by hand -- generated at
release from what the tag contains -- so that a line cannot be filed under a
version it did not ship in. The ruled exemption goes with it, or sooner if
`v0.2.1` is ever moved onto a commit that contains `10d0616`. Brett ruled
against that, and the test that pins the ruling's premise would fail if it
happened.
"""

from __future__ import annotations

import argparse
import difflib
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_CANNOT_RUN = 2

ROOT = Path(__file__).resolve().parent.parent

# A released heading names a version that is tagged `vX.Y.Z`. Any other label
# -- the development heading, `Unreleased` -- bounds a section and is not
# checked.
RELEASED = re.compile(r"^\d+\.\d+\.\d+$")
SETEXT_UNDERLINE = re.compile(r"^(?:-{3,}|={3,})\s*$")

# The two correction markers, each with the bracket pair that closes it.
MARKERS = (
    (re.compile(r"\*\(Correction, \d{4}-\d{2}-\d{2}:"), "(", ")"),
    (re.compile(r"\[corrected \d{4}-\d{2}-\d{2}:"), "[", "]"),
)

BLAME_HEADER = re.compile(r"^([0-9a-f]{40,64}) \d+ (\d+)(?: \d+)?$")


@dataclass(frozen=True)
class Profile:
    changelog: str
    version_file: str
    version: re.Pattern
    # Group 1 is an ATX prefix, absent for a setext heading; group 2 the label.
    heading: re.Pattern
    sibling_env: str | None = None
    sibling_dir: str | None = None
    # The first version whose release the repository's own policy tags. A
    # superseded version from here on with no tag in the clone cannot run.
    tagged_from: str | None = None


# This repository's changelog: `## 0.5.0` below `## Unreleased`, and the
# version in pyproject.toml. The hub's copy holds it as its `metasalmonpy`
# profile, which reads this repository as a sibling checkout; this copy reads
# the checkout it sits in.
PROFILE = Profile(
    changelog="CHANGELOG.md",
    version_file="pyproject.toml",
    version=re.compile(r"^version\s*=\s*[\"']([^\"']+)[\"']", re.M),
    heading=re.compile(r"^(##\s+)(\S.*?)\s*$"),
    # AGENTS.md, Releases: "Every release from 0.3.0 forward is tagged". There
    # was never a 0.3.0, so the first release this requires a tag for is 0.4.0.
    tagged_from="0.3.0",
)


@dataclass(frozen=True)
class Ruling:
    """One commit whose lines under one released heading are exempt by ruling.

    The exemption holds only while that heading's section carries a marked,
    dated correction naming the commit by its first seven hex digits, so the
    changelog says in its own text why those lines are there.
    """

    version: str
    commit: str  # the full SHA, as blame reports it
    reason: str


RULINGS = (
    # Hub item B-201; ruled by Brett on 2026-09-25, taking the recommendation
    # (decisions page, item 6). `10d0616` is the calendar fix. Pull request #10
    # committed it after the release commit `f1d9b0e`, which `v0.2.1` names,
    # and merged both as `3fdd323`, the merge that made 0.2.1 current on
    # `main`. So its 24 lines under `## 0.2.1` are in `main` from that merge
    # and in no tag before `v0.4.0`. The tag is not moved, because tags are
    # Brett's, and a dated correction appended to the 0.2.1 entry says the fix
    # is not in `v0.2.1`. Retires when `v0.2.1` names a commit that contains
    # `10d0616`, which the ruling says it will not.
    Ruling(
        version="0.2.1",
        commit="10d06168a77877f09e39258ae41115fcc564ebaf",
        reason=(
            "the calendar fix merged in pull request #10 after the commit "
            "v0.2.1 names; the tag stays where it is and the entry's dated "
            "correction says so (Brett, 2026-09-25)"
        ),
    ),
)


class CannotRun(Exception):
    """The check could not see what it needs. Never to be read as a pass."""


@dataclass(frozen=True)
class Finding:
    heading: str
    version: str
    line: int  # 1-based, in the changelog at the checked revision
    text: str
    commit: str
    bump: str
    how: str


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, encoding="utf-8", errors="replace",
    )
    if done.returncode != 0:
        raise CannotRun(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout


def read_blobs(repo: Path, specs: list[str]) -> list[str | None]:
    """The text of each `rev:path`, or None where there is no such file."""
    if not specs:
        return []
    done = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "--batch"],
        input=("\n".join(specs) + "\n").encode(), capture_output=True,
    )
    if done.returncode != 0:
        raise CannotRun(f"git cat-file failed: {done.stderr.decode(errors='replace').strip()}")
    data, pos, out = done.stdout, 0, []
    for _ in specs:
        end = data.index(b"\n", pos)
        header = data[pos:end].split()
        pos = end + 1
        if len(header) != 3:  # "<spec> missing"
            out.append(None)
            continue
        size = int(header[2])
        body = data[pos:pos + size]
        pos += size + 1
        out.append(body.decode("utf-8", "replace") if header[1] == b"blob" else None)
    return out


def split_lines(text: str) -> list[str]:
    """Lines as git numbers them: split on newline only.

    Not `str.splitlines()`, which also splits on a form feed or U+2028 and
    would put every later line out of step with blame's line numbers.
    """
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def sections(profile: Profile, text: str) -> tuple[dict[str, tuple[str, int, list[str]]], list[str]]:
    """Each heading's label -> (its heading line, index of its first body line, its body lines).

    The second value lists labels that head more than one section; only the
    first of them is kept.
    """
    lines = split_lines(text)
    heads = []  # (heading line index, label, first body line index)
    for i, line in enumerate(lines):
        m = profile.heading.match(line)
        if not m:
            continue
        if m.group(1):
            heads.append((i, m.group(2), i + 1))
        elif i + 1 < len(lines) and SETEXT_UNDERLINE.match(lines[i + 1]):
            heads.append((i, m.group(2), i + 2))
    found: dict[str, tuple[str, int, list[str]]] = {}
    repeated = []
    for n, (at, label, body) in enumerate(heads):
        end = heads[n + 1][0] if n + 1 < len(heads) else len(lines)
        if label in found:
            repeated.append(label)
        else:
            found[label] = (lines[at], body, lines[body:end])
    return found, repeated


def bump_commits(repo: Path, rev: str, profile: Profile,
                 versions: list[str]) -> dict[str, tuple[str, str]]:
    """Each version that has one -> (its bump commit, how that was decided).

    The tag when there is one. Otherwise the version file on the first-parent
    history decides: the FIRST commit reading the version while it is still
    the one the checked revision reads -- the open window -- and the LAST
    commit that read it once a later version has superseded it untagged.
    """
    found = {}
    for version in versions:
        done = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "-q", "--verify",
             f"refs/tags/v{version}^{{commit}}"],
            capture_output=True, text=True,
        )
        if done.returncode == 0:
            found[version] = (done.stdout.strip(), f"tag v{version}")
    wanted = set(versions) - set(found)
    if wanted:
        chain = git(repo, "rev-list", "--first-parent", "--reverse", rev).split()
        texts = read_blobs(repo, [f"{c}:{profile.version_file}" for c in chain])
        first: dict[str, str] = {}
        last: dict[str, str] = {}
        current = None
        for commit, text in zip(chain, texts):
            m = profile.version.search(text) if text else None
            current = m.group(1) if m else None
            if current in wanted:
                first.setdefault(current, commit)
                last[current] = commit
        for version in wanted & set(first):
            if version == current:
                found[version] = (
                    first[version],
                    f"no tag yet; the first commit on the first-parent history "
                    f"whose {profile.version_file} reads {version}",
                )
            else:
                found[version] = (
                    last[version],
                    f"never tagged, and superseded; the last commit on the "
                    f"first-parent history whose {profile.version_file} read {version}",
                )
    return found


def blame(repo: Path, rev: str, path: str) -> dict[int, str]:
    """Line number -> the commit that put that line where it is."""
    # `--ignore-revs-file=` with nothing after it empties any list configured
    # by blame.ignoreRevsFile. An ignored revision's lines are attributed to an
    # earlier commit, which would hide the very commits this check looks for:
    # measured 2026-09-25 with git 2.43, a configured list naming 0909953 left
    # none of its nine NEWS.md lines attributed to it, and this option all nine.
    out = git(repo, "blame", "--porcelain", "--ignore-revs-file=", rev, "--", path)
    commits = {}
    for line in out.split("\n"):
        m = BLAME_HEADER.match(line)
        if m:
            commits[int(m.group(2))] = m.group(1)
    return commits


def most_alike(old: list[str], new: list[str]) -> set[int]:
    """The len(old) indices into `new`, in order, whose lines best match `old`.

    Needs len(new) >= len(old). Likeness is how many characters of the old
    line the new one keeps, summed over the pairs and maximised, so that a
    line edited in place is the one standing for the line it replaced and the
    line written beside it is the one added.

    Characters kept, not difflib's ratio: the ratio divides by both lengths,
    so a short line sharing a few words outscores the long line that was
    edited. Measured 2026-09-25 on NEWS.md's 0.4.0 correction at line 1664,
    where the ratio paired the shipped line with the correction's closing line
    (0.554 against 0.541) and reported the edited line as the one added.
    """
    k, n = len(old), len(new)
    worst = float("-inf")
    score = [[0.0] * (n + 1)] + [[worst] * (n + 1) for _ in range(k)]
    paired = [[False] * (n + 1) for _ in range(k + 1)]
    for i in range(1, k + 1):
        for j in range(i, n + 1):
            skip = score[i][j - 1]
            kept = difflib.SequenceMatcher(None, old[i - 1], new[j - 1], autojunk=False)
            pair = score[i - 1][j - 1] + sum(b.size for b in kept.get_matching_blocks())
            score[i][j], paired[i][j] = (pair, True) if pair >= skip else (skip, False)
    kept, i, j = set(), k, n
    while i > 0:
        if paired[i][j]:
            kept.add(j - 1)
            i -= 1
        j -= 1
    return kept


def added_lines(shipped: list[str], now: list[str]) -> list[int]:
    """Indices into `now` of the lines the diff from `shipped` adds.

    A line whose exact text the diff removes from elsewhere in the section is
    a move within it, not an addition. A diff shows a reordering as a line
    deleted in one place and inserted in another, and blame credits whoever
    moved it, so without this a shipped bullet moved to the top reads as new.
    """
    added = []
    removed: Counter[str] = Counter()
    matcher = difflib.SequenceMatcher(None, shipped, now, autojunk=False)
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op in ("delete", "replace"):
            removed.update(shipped[i1:i2])
        if op == "insert":
            added.extend(range(j1, j2))
        elif op == "replace" and j2 - j1 > i2 - i1:
            kept = most_alike(shipped[i1:i2], now[j1:j2])
            added.extend(j1 + j for j in range(j2 - j1) if j not in kept)
    new = []
    for j in added:
        if removed[now[j]]:
            removed[now[j]] -= 1
        else:
            new.append(j)
    return new


def closing(text: str, at: int, opener: str, closer: str) -> int | None:
    """Index of the bracket closing the one at `at`, or None if none does."""
    depth = 0
    for pos in range(at, len(text)):
        if text[pos] == opener:
            depth += 1
        elif text[pos] == closer:
            depth -= 1
            if depth == 0:
                return pos
    return None


def marked_lines(body: list[str]) -> set[int]:
    """Indices into `body` of the lines a closed, dated correction marker touches."""
    marked: set[int] = set()
    start = 0
    while start < len(body):
        if not body[start].strip():
            start += 1
            continue
        end = start
        while end < len(body) and body[end].strip():
            end += 1
        paragraph = body[start:end]
        text = "\n".join(paragraph)
        offsets, pos = [], 0
        for line in paragraph:
            offsets.append(pos)
            pos += len(line) + 1
        for pattern, opener, closer in MARKERS:
            for m in pattern.finditer(text):
                close = closing(text, text.index(opener, m.start()), opener, closer)
                if close is None:
                    continue
                for n, offset in enumerate(offsets):
                    if offset <= close and m.start() < offset + len(paragraph[n]):
                        marked.add(start + n)
        start = end
    return marked


def corrections(body: list[str]) -> list[str]:
    """The text of every closed, dated correction marker in `body`.

    Paragraphs are split as `marked_lines()` splits them, so a marker is read
    here exactly when it exempts lines there.
    """
    found: list[str] = []
    start = 0
    while start < len(body):
        if not body[start].strip():
            start += 1
            continue
        end = start
        while end < len(body) and body[end].strip():
            end += 1
        text = "\n".join(body[start:end])
        for pattern, opener, closer in MARKERS:
            for m in pattern.finditer(text):
                close = closing(text, text.index(opener, m.start()), opener, closer)
                if close is not None:
                    found.append(text[m.start():close + 1])
        start = end
    return found


def run(repo: Path, rev: str, profile: Profile, exemption: bool,
        rulings: tuple[Ruling, ...] = RULINGS) -> int:
    git(repo, "rev-parse", "--git-dir")
    if git(repo, "rev-parse", "--is-shallow-repository").strip() == "true":
        raise CannotRun(
            f"{repo} is a shallow clone, and blame and ancestry need the whole "
            "history. Fetch it (git fetch --unshallow --tags), or check out with "
            "fetch-depth: 0."
        )
    head = git(repo, "rev-parse", "--verify", f"{rev}^{{commit}}").strip()
    (text,) = read_blobs(repo, [f"{head}:{profile.changelog}"])
    if text is None:
        raise CannotRun(f"there is no {profile.changelog} at {rev} in {repo}")
    now, repeated = sections(profile, text)
    released = [label for label in now if RELEASED.match(label)]
    repeated = [label for label in repeated if RELEASED.match(label)]
    if repeated:
        raise CannotRun(f"{profile.changelog} has more than one heading for {', '.join(repeated)}")
    if not released:
        print(f"changelog window: {profile.changelog} at {head[:7]} has no released heading yet")
        return EXIT_OK

    bumps = bump_commits(repo, head, profile, released)
    unchecked = [v for v in released if v not in bumps]
    if profile.tagged_from:
        floor = version_key(profile.tagged_from)
        missing = [v for v, (_, how) in bumps.items()
                   if how.startswith("never tagged") and version_key(v) >= floor]
        if missing:
            raise CannotRun(
                f"no tag for {', '.join('v' + v for v in missing)} in this clone, "
                f"though every release from {profile.tagged_from} forward is tagged. "
                "Fetch the tags (git fetch --tags), or check out with fetch-depth: 0; "
                "read as untagged history, the check would pass what the tag names."
            )
    if not bumps:
        raise CannotRun(
            f"none of the {len(released)} released headings in {profile.changelog} "
            f"has a bump commit on this history: no v* tag, and no "
            f"{profile.version_file} on the first-parent history reads one of "
            "them. Is this the right repository and profile?"
        )
    shas = sorted({sha for sha, _ in bumps.values()})
    at_bump = dict(zip(shas, read_blobs(repo, [f"{s}:{profile.changelog}" for s in shas])))
    blamed = blame(repo, head, profile.changelog)

    findings: list[Finding] = []
    exempt: dict[str, int] = {}
    ruled_exempt: dict[str, dict[str, int]] = {}
    uncited: list[Ruling] = []
    ancestry: dict[tuple[str, str], bool] = {}
    for version in released:
        if version not in bumps:
            continue
        bump, how = bumps[version]
        shipped_text = at_bump[bump]
        shipped = sections(profile, shipped_text)[0] if shipped_text else {}
        title, body_at, body = now[version]
        marked = marked_lines(body) if exemption else set()
        # A ruling holds only while this section's own corrections name its
        # commit, so that the changelog says why the lines are there.
        ruled = {r.commit: r for r in rulings if r.version == version} if exemption else {}
        cited_text = "\n".join(corrections(body)) if ruled else ""
        cited = {c for c in ruled
                 if re.search(rf"(?<![0-9a-f]){c[:7]}", cited_text)}
        for index in added_lines(shipped.get(version, ('', 0, []))[2], body):
            if not body[index].strip():
                continue
            line = body_at + index + 1
            commit = blamed.get(line)
            if commit is None:
                raise CannotRun(f"blame gave no commit for {profile.changelog}:{line}")
            if (commit, bump) not in ancestry:
                done = subprocess.run(
                    ["git", "-C", str(repo), "merge-base", "--is-ancestor", commit, bump],
                    capture_output=True, text=True,
                )
                if done.returncode not in (0, 1):
                    raise CannotRun(f"git merge-base --is-ancestor failed: {done.stderr.strip()}")
                ancestry[(commit, bump)] = done.returncode == 0
            if ancestry[(commit, bump)]:
                continue
            if index in marked:
                exempt[version] = exempt.get(version, 0) + 1
                continue
            if commit in cited:
                counts = ruled_exempt.setdefault(version, {})
                counts[commit] = counts.get(commit, 0) + 1
                continue
            if commit in ruled and ruled[commit] not in uncited:
                uncited.append(ruled[commit])
            findings.append(Finding(title, version, line, body[index], commit, bump, how))

    checked = len(released) - len(unchecked)
    print(f"changelog window: {profile.changelog} at {head[:7]}, "
          f"{checked} released heading{'s' if checked != 1 else ''} checked"
          + ("" if exemption else ", with the exemptions switched off"))
    for version in released:
        if version in bumps:
            bump, how = bumps[version]
            note = f"; {exempt[version]} added lines exempt as marked corrections" if version in exempt else ""
            for commit, count in ruled_exempt.get(version, {}).items():
                note += f"; {count} added lines exempt by ruling ({commit[:7]})"
            print(f"  {version:<8} {bump[:7]}  {how}{note}")
    if unchecked:
        print(f"not checked, no bump commit on this history ({len(unchecked)}): "
              + " ".join(unchecked))
    if not findings:
        print("no findings")
        return EXIT_OK

    report(profile, findings)
    for ruling in uncited:
        sys.stderr.write(
            f"\n{ruling.commit[:7]}'s lines under ## {ruling.version} are exempt by ruling only while\n"
            f"that section carries a marked, dated correction naming {ruling.commit[:7]}, and at\n"
            f"{head[:7]} none does. The ruling:\n  {ruling.reason}.\n"
        )
    return EXIT_FINDINGS


def report(profile: Profile, findings: list[Finding]) -> None:
    groups: list[list[Finding]] = []
    for f in findings:
        last = groups[-1][-1] if groups else None
        if last and (last.version, last.commit, last.line + 1) == (f.version, f.commit, f.line):
            groups[-1].append(f)
        else:
            groups.append([f])
    err = sys.stderr
    err.write(f"\nchangelog window: {len(findings)} line"
              f"{'s' if len(findings) != 1 else ''} under a released heading "
              "missed that release\n")
    for group in groups:
        first, last = group[0], group[-1]
        where = f"{first.line}" if first is last else f"{first.line}-{last.line}"
        err.write(
            f"  {profile.changelog}:{where} under \"{first.heading}\", "
            f"added by {first.commit[:7]}, which is not an ancestor of {first.version}'s "
            f"bump commit {first.bump[:7]} ({first.how}):\n"
        )
        for f in group[:5]:
            err.write(f"      {f.text}\n")
        if len(group) > 5:
            err.write(f"      ... and {len(group) - 5} more lines\n")
    err.write(
        "\nA line under a released heading has to be in the tree that release names:\n"
        "its vX.Y.Z tag, or, before the tag exists, the commit that bumped the version.\n"
        "AGENTS.md, Releases: a change that merged after that commit is filed under the\n"
        "development heading, never under a version it did not ship in, so move it\n"
        "there. A correction to a shipped entry is the one exception, marked and dated\n"
        "so it can be told apart: a *(Correction, YYYY-MM-DD: ...)* paragraph, or a\n"
        "[corrected YYYY-MM-DD: ...] bracket.\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail when a line under a released changelog heading is not in that release.",
    )
    parser.add_argument("--repo", help="the checkout to read (default: this repository)")
    parser.add_argument("--rev", default="HEAD", help="the revision to check (default: HEAD)")
    parser.add_argument("--no-exemption", action="store_true",
                        help="treat marked corrections and ruled commits as findings, "
                        "to see what the exemptions hold")
    args = parser.parse_args(argv)
    repo = Path(args.repo) if args.repo else ROOT
    try:
        return run(repo, args.rev, PROFILE, exemption=not args.no_exemption)
    except CannotRun as err:
        sys.stderr.write(f"changelog window: cannot run: {err}\n")
        return EXIT_CANNOT_RUN


if __name__ == "__main__":
    raise SystemExit(main())
