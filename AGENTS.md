# metasalmonpy — agent & contributor guidance

`metasalmonpy` (repo `salmon-data-mobilization/metasalmonpy`, formerly
`metaSmnPy`; package renamed from `salmonpy` on 2026-08-13) is the **Python
mirror of the metasalmon R package** for creating, validating, and packaging
Salmon Data Packages (SDP).

## Non-negotiable: the mirror contract

This is the firm rule Brett set on 2026-08-13. It is not aspirational; treat a
violation like a failing test.

1. **metasalmon leads; metasalmonpy mirrors.** Any change requested or made in
   metasalmon is **presumed to require the same change here** — same
   user-facing behaviour, same contracts (opt-in LLM review, `REVIEW:` IRI
   markers, tidy-data enforcement, deterministic/byte-reproducible outputs).
   If a change genuinely cannot or should not be mirrored, that decision must
   be logged in the hub roadmap (see below), never left implicit.
   **Parity is behavioural, not literal** (Brett, 2026-08-15): do not force
   100% API mimicry where it would be unintuitive for Python users, and
   simple language differences that do not materially change behaviour or
   capability are fine — raise exceptions instead of R conditions, accept
   snake_case/keyword idioms, use extras instead of Suggests. Every such
   difference is recorded in [PARITY.md](PARITY.md) here **and** in the hub's
   `knowledge/parity-deviations.md`; an undocumented difference is a contract
   violation even when the difference itself is fine.
2. **Version lockstep.** metasalmonpy's version number is a *parity claim*: it
   matches the metasalmon version whose functionality it actually delivers.
   When a metasalmon release ships, mirror the work and bump this package to
   the same number.
3. **Current honest state:** this package is at **0.5.0** and metasalmon is at
   **0.5.0** (tagged `v0.5.0`, released 2026-08-25), so **no catch-up window is
   open.** The `0.4.0 → 0.5.0` window (roadmap stream S5) closed 2026-09-16, by
   hub queue **B-153**; the earlier `0.2.2 → 0.4.0` one (S10 plus the S3
   KNB-environment feature 0.4.0 added after every S10 chunk was written) closed
   2026-08-24. This package's version stays at the last delivered milestone
   until the next one lands — do **not** bump the number ahead of the
   functionality (Brett's decision, 2026-08-13: bump on parity, not on calendar).
   The tree reads 0.5.0, and measured 2026-09-24 the newest tag here is
   `v0.5.0`: annotated, on the bump merge `67fb486`, made that day by the
   Release workflow together with its GitHub Release.

   **That window was closed in two halves, and recording both is the point,
   because the second half is the one that gets skipped.** The behavioural half
   (hub **B-126**, pull request #28, merged 2026-09-16) ported the S5
   review-and-edit surface: the nine functions (`review_semantics`,
   `accept_suggestion`, `reject_suggestion`, `apply_sdp_semantics`,
   `review_metadata` and the four `set_sdp_*()` setters) plus the
   `semantic_suggestions()` / `semantic_llm_assessments()` accessors, the
   `decision_reason` column and decision replay on queue rebuild, the first
   consumer of the schema's `constraints.required`, the backlog **#118**
   auto-apply exemption, the `prune=True` warning, and the review checklist that
   hands over the Python calls. It deliberately left the number at 0.4.0,
   because a version is a parity claim and metasalmon 0.5.0's own NEWS entry
   leads with a **documentation** claim — that a package reaches
   `validate_salmon_datapackage(require_iris=True)` "without opening a single
   file in a spreadsheet" — which a reference index cannot deliver on its own.
   The documentation half (**B-153**) rewrote `guides/semantic-review.qmd`
   around that path and moved the number.

   **What the documentation half actually found, recorded because the standing
   description of the defect was wrong in both directions.** That description
   said the guide "presents the spreadsheet as the workflow rather than as the
   fallback". Measured against this tree on 2026-09-16: the word *spreadsheet*
   appeared **nowhere** in that file, nor in any `.qmd` in the repository, so
   there was no spreadsheet workflow to demote. And the guide named **none** of
   the nine functions except one passing mention of `apply_sdp_semantics()`
   inside the closure section, and neither accessor as a call — it offered no
   in-Python review path at all, which is worse than an outdated one: an
   outdated path can be followed and then corrected, while a missing one leaves
   the reader to assemble the sequence from the reference index. Distrust a
   defect description that errs toward the defect being *smaller* than it is,
   and re-measure before quoting one.

   **This number is stated in three places and all three must agree:** here,
   metasalmon's own `AGENTS.md`, and the release index in the hub's
   `knowledge/roadmap.md`. When they disagree, one of them is wrong about
   the single fact the mirror contract turns on and nothing in either file
   reveals which. **It has now gone wrong four times — once in each direction,
   once here again, and once in a shape none of the first three had** (the fourth
   is the paragraph after this one), which is the argument for reading any parity
   sentence
   as a dated measurement and checking the other file rather than trusting the
   one in front of you: metasalmon's line read 0.1.8 for three days after this
   package tagged 0.2.1 (corrected there 2026-08-21); this line read `0.2.1` /
   window `0.2.2 → 0.3.0` for two days after metasalmon tagged 0.4.0 and said so
   in its own `AGENTS.md` (corrected here 2026-08-24); and **this line read
   "both at 0.4.0, no window open" for twenty days after metasalmon tagged
   `v0.5.0`** — 2026-08-25 to 2026-09-14 — while metasalmon's own `AGENTS.md`
   said the window was open and said *this file* was the one that was wrong.
   Editing the mirror was out of scope for the release that opened the window,
   which is how the gap lasted twenty days rather than two; corrected here
   2026-09-14 by B-126. Whenever either version moves, read the other file in
   the same change.

   **The fourth time is the one worth reading, because it is a shape the rule
   above does not catch.** The 2026-09-16 closure moved all three copies in one
   change, and read all three against each other first. **All three already
   agreed** — 0.5.0 / 0.4.0, window open. What was wrong was every *description*
   of them: metasalmon's `AGENTS.md` said this file "still reads 0.4.0/0.4.0 with
   no window open", the roadmap release index said the same, and both **B-126**'s
   and **B-153**'s retirement conditions said it too. All four had been true when
   written and stopped being true hours earlier, when #28 merged. So the failure
   was not two files disagreeing about the number — it was four places agreeing
   about *which file was wrong* and all four being wrong about it, which checking
   that the three numbers match cannot catch. **Read the three files, not a
   description of them** — including this paragraph, which is a dated measurement
   like every other one here.

   **The mirror is not automatically the follower** (Brett, 2026-08-17):
   *"Don't just make things match metasalmon. If the Python implementation
   got it right, then update metasalmon."* A parity divergence opens the
   question of which side is right rather than settling it. Applied twice
   already: R adopted this package's `smn`-over-`gcdfo` ranking margin, and
   R adopted its unconditional year padding. Changing R does **not** close a
   `PARITY.md` row by itself — the row records the divergence, the ruling,
   and which side moved.
4. New metasalmon work started after 2026-08-13 must land its Python mirror
   as part of the same stream, so the gap never widens again.

## Coordination hub

The metasalmon repo is the coordinating hub for this family of repos
(metasalmon, metasalmonpy, smn-data-pkg, salmon-domain-ontology,
dfo-salmon-ontology, psc-salmon-vocabularies). Sequencing, execplans, and the
cross-repo release index live in metasalmon's `knowledge/` OKF bundle — start
at its `ROADMAP` card. Do not maintain a competing roadmap here.

## Salmon knowledge goes to the commons, not into a PR body

The hub carries sequencing. Knowledge about **salmon itself** — biology,
ecology, management, what a term means, why a modelling choice went the way it
did — goes to
[`salmon-knowledge-commons`](https://github.com/salmon-data-mobilization/salmon-knowledge-commons).

This matters more here than in most repos, because of the mirror contract. When
mirroring work turns up a domain fact — that a life-history label is a proxy
rather than a trait, that two vocabularies share a word and not a meaning — that
fact is not a parity deviation and does not belong in `PARITY.md`. It belongs in
the commons, where the R side can find it too. Left in a PR body it evaporates,
and both packages re-derive it separately, which is how the two sides drift on
something neither of them recorded.

If you can push there, open a PR. If you cannot, put the finding **in your
report with its sources** so a maintainer can. Source-backed claims only — the
commons rejects a claim with no citation — and **never assert your own
verification**: `generated` says who wrote a card, `verified` says who checked
it, and those are not the same actor.

The commons is also the register for an **ontology gap**: a concept with no term
in `smn`, `gcdfo` or the PSC CV, with a note saying what a term would have to
say and where it should be minted. That register feeds this ecosystem's
term-request pipeline.

## Releases

Mirrored from metasalmon's own contract, because this repository had **no
release procedure written down at all** until 2026-08-25 — which is why the
0.4.0 release shipped with a stale `uv.lock`.

Every release from **0.3.0 forward** is tagged (`vX.Y.Z`, annotated) **and**
published as a GitHub Release with its `CHANGELOG.md` entry as the body. **Tag
the commit that made the version current, not a later docs-only merge** — this
repository has been following that rule without stating it (`v0.4.0` sits at
`3b587e6`, with a docs-only merge after it), so the practice was real and only
the contract was missing.

**To cut one, run the Release workflow** (`.github/workflows/release.yml`) with
the version and the full SHA of the commit that made it current. For a version
bumped in a pull request, that is the merge commit on `main`, not the bump
commit on the branch. It refuses a commit that is not on `main`'s first-parent
history (so a branch commit cannot be tagged, although it is an ancestor of
`main`), a version that `pyproject.toml` at that commit does not carry, a commit
whose parent already carried the version (so a later merge cannot be tagged by
mistake), and a release that already exists. An existing tag is refused if it is
lightweight or names a different commit; an annotated tag on the requested
commit is accepted, so a rerun can finish a release whose tag was pushed before
the release step failed. It publishes the version's `CHANGELOG.md` section, as
it stood at that commit, as the release body. It exists because agent sessions
cannot push tags. Running it is still an outward act, and it is Brett's
decision: an agent dispatches it only on his word.

**A change that merges after the commit that bumped the version and before
that version's tag exists is filed under `## Unreleased`, never under the
version it did not ship in, and the tag stays on the bump commit** (Brett,
2026-09-16: *"Regarding the agents.md change log entry. I will take your
recommendation."*). metasalmon's `AGENTS.md` states the same rule for its
`NEWS.md`. A dated correction to a shipped entry may still be appended in
place, because it makes the entry describe what shipped more accurately rather
than adding to what shipped; a change, a fix or an addition goes under
`## Unreleased`. The instance that produced the rule is this repository's.
B-144's branch filed its entry under `## Unreleased`, and its merge `b939fd9`
(#32), 21 seconds after the bump merge `67fb486` (#33, B-153), carried it under
`## 0.5.0` with no conflict to say so. Pull request #35 moved it back
(`3f8349a`). The window is real on every release, because the tag is a separate
act from the bump.

**The pre-tag step is `python3 scripts/check-changelog-window.py`, run on an
up-to-date `main` in a full clone before the Release workflow is dispatched.**
Until the tag exists it measures the version against its bump commit, the
first commit on `main`'s first-parent history whose `pyproject.toml` reads it,
which is the commit the workflow must be given. It fails on any line under that
heading added by a commit that is not an ancestor of it. A correction passes
only in a marked, dated form: a `*(Correction, YYYY-MM-DD: …)*` paragraph or a
`[corrected YYYY-MM-DD: …]` bracket. A red run before tagging means an entry
moves to `## Unreleased` first. It also runs on every pull request
(`.github/workflows/changelog-window.yml`), and its docstring states what it
does not cover. **One commit is exempt by ruling:** `10d0616`, the calendar fix
under `## 0.2.1`, which pull request #10 merged after the commit `v0.2.1`
names. Brett ruled on 2026-09-25 that the tag does not move and that the entry
says so, and the exemption holds only while the entry's dated correction names
the commit. The script is a port of the hub's copy (hub item B-200; B-201
here). Its test file fails when a definition the two copies share stops
matching, so a change to either has to reach the other.

**The version number lives in six places and they drift.** A bump moves all
six in the same change, and **every one of them is pinned by a test in
`tests/test_public_api.py`**, so a bump that misses one turns the suite red
rather than shipping a stale claim. That pairing is the rule: an entry is added
to this list *and* to that file, or the number is deleted from that place
instead. An enumerated place with no guard is the shape entries 3, 5 and 6 each
drifted in.

1. `pyproject.toml` — `version`
2. `__init__.py` — `__version__`
3. **`uv.lock`** — the `metasalmonpy` entry's own `version`. Refresh it with
   `uv lock` and commit the result. Miss it and the next contributor's
   `uv pip install -e .` silently rewrites the line, producing a spurious
   `M uv.lock` in an unrelated PR. That happened after 0.4.0, was reverted as
   out of scope, and is the reason this list is enumerated rather than
   described. `uv lock --check` verifies it without writing.
4. `AGENTS.md` — the parity-claim number in the mirror contract above.
5. **`_quarto.yml`** — `quartodoc.version`, which is the number the published
   documentation site shows. **This entry was missing from the list until
   2026-09-16**, while the list said in as many words that it was enumerated
   rather than described *because* a missed place drifts silently. It had been
   moved in lockstep once (`0.1.6 → 0.4.0`, in the 0.4.0 parity commit) and was
   found still reading `0.4.0` while B-153 was bumping the other four, so the
   list's own argument was proved by the list's own gap.
6. **`guides/parity.qmd`** — the *"metasalmonpy X aligns its core user-facing
   behavior with metasalmon X"* sentence under *Compatibility target*. The one
   prose copy kept, because naming the current release **is** that page's
   subject. **Added 2026-09-16 on a Codex finding against B-153**, which caught
   the paragraph below claiming this page was "listed here" while the list ran to
   five entries and no test read it — so the next bump could have moved every
   enumerated and guarded copy and left the public parity page stale, by exactly
   the mechanism entry 5 exists to record.

**Two more copies of the number were deleted rather than added to this list**,
and deletion is the preferred fix whenever the number is incidental to what the
text says: `index.qmd` and `README.md` each asserted parity with metasalmon
**0.1.6** — three releases stale, and stale precisely because they were prose
nobody thought of as a version place — and both now point at the parity guide
instead of restating a number. **Prefer deleting a restatement to enumerating
it**, and when a place must be enumerated because the number is the point, give
it a guard in the same change. An unguarded entry is a thing to remember; a
guarded one is a thing the suite remembers; a deleted one cannot go stale at all.

Two further mentions of `0.1.6` are deliberately left alone, because they are
about **tags** rather than about this claim: the install instructions in
`README.md` and `getting-started.qmd` both say the `v0.1.6` tag is what a user
can install. That is wrong — `v0.4.0` exists at `3b587e6` — but fixing it is a
statement about which tag to install, so it waited on the tagging decision
rather than riding along with a version bump. That decision was made on
2026-09-24, when `v0.5.0` was tagged on `67fb486`, so the fix is no longer
blocked and is still owed.

The version is a **parity claim**, so it moves only when the mirrored behaviour
actually lands; the mirror contract above governs what makes the claim true.
Verify with `uv lock --check` and both dependency legs before tagging.

## Build / test

```sh
uv run --with pytest --with pandas --with requests -- python -m pytest tests/ -q
```

or `pip install -e ".[test]" && pytest -q`. The suite must stay green in **both**
dependency configurations, and CI runs both (see *Dependency boundaries*): 1136
passed / 2 skipped with the extras installed, 995 / 143 with core dependencies
only (2026-09-25, measured locally for hub B-201 with `main` `66ad1a3` merged
in, under Python 3.11.15, pytest 9.1.1 and pandas 3.0.6, on a machine where
`Rscript` is on `PATH` and `/tmp/metasalmon-lib` exists, in a full clone with
every tag and with `METASALMON_PATH` unset, the same under `python -m pytest -q`
and bare `pytest -q`; 1102 / 1 and 961 / 142 for B-234 on `f872f51`, branched
from `main` `ba1b54a`, whose tests `main` `66ad1a3` merged unchanged; 1118 / 2
and 977 / 143 for B-201 on `d1182e5`, branched from `main` `ba1b54a`; 1084 / 1
and 943 / 142 on `main` `ba1b54a` before both, measured the same four ways;
1066 / 1 and 925 / 142 for B-189 on `10750ec`, branched from `main` `dcafe28`;
1064 / 1 and 923 / 142 on `main` `dcafe28` before it, as for B-212 on
`351fed6`, branched from `main`
`70fa8fd`; 1061 / 1
and 920 / 142 for B-222 on `e3b8330`, branched from `main` `ed5e22e`; 1056 / 1
and 915 / 142 on `main` `ed5e22e` before it, as for B-215 on `9577f35`, which
has `main` `2405df2` merged in; 1032 / 1
and 891 / 142 for B-216 with `main` `fc5d16f` merged in, 1030 / 1 and 889 / 142
for B-220 on `acf243e` and 1024 / 1 and 883 / 142 for B-216 on `f663c9b`
earlier that day, 1022 / 1 and 881 / 142 for B-241 on the tree of its head
`5b83c27` on 2026-09-24, 1012 / 1 and
871 / 142 for B-242 and 1007 / 1 and 866 / 142 for B-240 earlier that day,
966 / 1 and 825 / 142 for B-191 on 2026-09-23, 951 / 1 and
810 / 142 at 0.5.0 on 2026-09-16, 896 / 3 and 783 / 116 at the S5 parity port
on 2026-09-14, and 803 / 3 and 690 / 116 before that). **CI reads five fewer
passes in each leg for the same tree**: 1113 / 7 and 972 / 148 on B-201's head
`098030f`, read from its check logs on 2026-09-25. Before B-201 it read two
fewer, as B-241's head `5b83c27` read 1020 / 3 and 879 / 144 on 2026-09-24 and
B-242's head `183f887` read 1010 / 3 and 869 / 144. Two of the five are
`tests/test_roundtrip.py`, which runs only where both of those hold. CI's suite
jobs have neither, so it skips there and runs in CI's `parity` job instead. The
other three are `tests/test_check_changelog_window.py`'s replays of this
repository's history, which need a full clone with the tags. CI's suite jobs
check out one commit, so they skip there and run in the `changelog-window`
workflow instead. The gap between the legs is the extras-gated EML, KNB and
context-reader tests. Two tests skip in both legs, locally and on CI: the
Qualark fetch test, which runs only when `METASALMONPY_RUN_QUALARK_TEST=1` is
set, and the comparison of `scripts/check-changelog-window.py` with the hub's
copy, which runs only when `METASALMON_PATH` names a metasalmon checkout. These
counts are a dated measurement, not a target — update them when you add tests
rather than treating a mismatch as a failure.

**The suite runs from a checkout at any path.** It did not until 2026-09-23
(hub **B-191**). The root `__init__.py` and a `tests/__init__.py` made pytest
name the package after the checkout directory, so under pytest 8 and later a
worktree at `.../my-fix` errored on every test. Hub agents took to nesting each
worktree under a directory named `metasalmonpy` to get a suite to run.
**Nesting is no longer needed, and `tests/` must stay a plain directory**,
because an `__init__.py` there brings the dependency back. `tests/conftest.py`
says how the suite now finds the package. `tests/test_import_route_guard.py`
runs a copy of the checkout under two directory names that used to break, so
CI, whose checkout is always named `metasalmonpy`, sees a regression. Retire
this note with the conftest's measures, when the package moves into its own
`src/metasalmonpy/` directory.

**A test imports the package's modules through the package**, as
`from metasalmonpy.validation import validate_semantics`, never as
`from validation import ...`. A top-level name resolves only when the checkout
root is on `sys.path`: `python -m pytest` from the root puts it there, and bare
`pytest` does not. So two test modules passed CI and stopped the documented
`pytest -q` at two collection errors until 2026-09-25 (hub **B-227**).
`parity.yml`'s `bare-pytest` job now runs that command, so the next import by
top-level name fails a pull request.

**A pytest run tests the tree it lives in, and nothing else does that for
you.** `package-dir` makes an editable install register an import finder
pinned to the *absolute path it was installed from*. That finder sits after
`sys.path` on `sys.meta_path` (measured 2026-09-23), so it answers whenever
nothing on `sys.path` provides `metasalmonpy`. Before B-191 that was every
pytest run in a checkout not named `metasalmonpy`: a virtualenv built in the
primary checkout kept testing the primary checkout from inside a worktree, the
run was green, and nothing in the output hinted at it. `tests/conftest.py` now
binds `metasalmonpy` to its own checkout, and refuses to load if the package
was already imported from somewhere else. **Everything outside pytest still
resolves the package through whatever is installed**: `tests/smoke.py`, the
scripts, a notebook, `python -c`. For those, install the package from the tree
you mean, and confirm it took:

```sh
python -c "import metasalmonpy; print(metasalmonpy.__file__)"
```

This is the dependency rule again: the only proof is running it. Retire the
check when editable installs stop pinning an absolute path.

## Dependency boundaries

Core dependencies are **pandas + requests**. lxml and PyYAML live in the
`[eml]` extra; `[knb]` is `[eml]` plus core. A path that a user can reach
without an extra must keep working without it.

**A deferred import is a hard dependency for every caller of the function that
contains it.** `import yaml` inside a function body is the correct pattern for
an optional extra, but it does not make anything that *calls* that function
core-deps-safe — the requirement simply moves to the call site, where no
import statement records it. Reading the import statements therefore cannot
tell you whether a path is core-deps-safe; two people reached the wrong
conclusion that way in one exchange while landing 0.1.8, and a pure
pandas + `zipfile` archive builder had silently begun requiring PyYAML through
a three-call chain.

The only proof is running the path with the extra genuinely absent:

```sh
uv venv /tmp/coreenv && uv pip install --python /tmp/coreenv/bin/python pandas requests pytest
uv pip install --python /tmp/coreenv/bin/python -e .
/tmp/coreenv/bin/python -m pytest tests/ -q      # must be green
```

**CI runs the suite in both configurations**, as the two legs of the
`python` matrix in
`.github/workflows/parity.yml`: *core dependencies only* installs `.[test]` and
is the run this recipe describes; *with `[eml]` and `[context]` extras* installs
`.[test,eml,context]` and is the only run that executes the extras-gated tests
at all. Each leg begins by asserting its own dependency configuration —
the core leg fails if `yaml`, `lxml`, `openpyxl`, `pypdf` or `xlrd` is
importable, the extras leg fails if any of them is not — so neither can quietly
become a copy of the other.

That verification step is the point of the pairing, not decoration. Until
2026-08-21 there was a single job installing `.[test]`, which is `build` plus
`pytest` and names no extra, so CI was core-deps-shaped **by accident**: nothing
declared it, nothing checked it, and the 94 extras-gated tests ran nowhere (hub
backlog #92). A configuration that holds only incidentally is the one that
stops holding without anyone noticing.

Because most local environments have the extras installed,
`tests/test_knb_publication.py::KnbCoreDependencyTests` also blocks `yaml` from
`sys.meta_path`, so the property fails locally too rather than only in CI. When you add a reader to a shared helper, check what
calls it before assuming the helper is where it belongs (PARITY.md rows 30
and 34). The SDP schema bundle is the live example: `write_salmon_datapackage()`
loads it on the core path, so `sdp_schema` reads `sdp.rules.yaml` with a
top-level-scalar scan rather than PyYAML.

## Platform determinism

Canonical keys, identifiers and written bytes must not vary with the machine.
The Python trap is **`strftime`**: it hands `%Y` to the platform C library,
and glibc does not zero-pad a year below 1000 where the macOS/BSD
implementation does, so `date(1, 1, 1)` renders `1-01-01` in CI and
`0001-01-01` on a developer's Mac. That shipped once already — 0.2.0's new
`resource_types.py` keyed dates through `strftime`, the whole suite was green
on every local run, and `test_canonical_keys_match_era_r[date]` failed on
Linux only.

Build calendar text with `resource_types._iso_date` / `._iso_seconds`, or with
`date.isoformat()`; all three are pure Python.
`tests/test_platform_determinism_guard.py` fails on any `strftime` call in a
package module and carries an allowlist for text a human reads that no machine
re-checks — each entry naming what would retire it. This is the dependency
lesson in a second costume: **reading the call site cannot tell you it is
wrong**, and the environment where it is wrong is not the one you develop in.

## Layout notes

- Modules live at the repo root and are packaged as `metasalmonpy` via
  `pyproject.toml`'s `package-dir` mapping (`"metasalmonpy" = "."`).
- Keep module names aligned with metasalmon's `R/` topic files where a
  counterpart exists (e.g. `dictionary.py` ↔ `dictionary-helpers.R`).
- This file and `CLAUDE.md` are git-tracked on purpose; do not add them to
  `.gitignore`.
