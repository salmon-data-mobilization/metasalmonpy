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
3. **Current honest state:** parity is at **metasalmon 0.4.0**. metasalmon is
   at 0.4.0 (tagged `v0.4.0`, `4e2bbb6`), so the catch-up window
   0.2.2 → 0.4.0 — roadmap stream **S10** plus the S3 KNB-environment
   feature 0.4.0 added after every S10 chunk was written — is closed. This
   package's version stays at the last delivered milestone until the next
   one lands — do **not** bump the number ahead of the functionality
   (Brett's decision, 2026-08-13: bump on parity, not on calendar).

   **This number is stated in three places and all three must agree:** here,
   metasalmon's own `AGENTS.md`, and the release index in the hub's
   `knowledge/roadmap.md`. When they disagree, one of them is wrong about
   the single fact the mirror contract turns on and nothing in either file
   reveals which — this line read `0.2.1` / window `0.2.2 → 0.3.0` for two
   days after metasalmon tagged 0.4.0 and said so in its own `AGENTS.md`,
   corrected here 2026-08-24. Whenever either version moves, read the other
   file in the same change.

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

**The version number lives in four places and they drift.** A bump moves all
four in the same change:

1. `pyproject.toml` — `version`
2. `__init__.py` — `__version__`
3. **`uv.lock`** — the `metasalmonpy` entry's own `version`. Refresh it with
   `uv lock` and commit the result. Miss it and the next contributor's
   `uv pip install -e .` silently rewrites the line, producing a spurious
   `M uv.lock` in an unrelated PR. That happened after 0.4.0, was reverted as
   out of scope, and is the reason this list is enumerated rather than
   described. `uv lock --check` verifies it without writing.
4. `AGENTS.md` — the parity-claim number in the mirror contract above.

The version is a **parity claim**, so it moves only when the mirrored behaviour
actually lands; the mirror contract above governs what makes the claim true.
Verify with `uv lock --check` and both dependency legs before tagging.

## Build / test

```sh
uv run --with pytest --with pandas --with requests -- python -m pytest tests/ -q
```

or `pip install -e ".[test]" && pytest -q`. The suite must stay green in **both**
dependency configurations, and CI runs both (see *Dependency boundaries*): 795
passed / 3 skipped with the extras installed, 682 / 116 with core dependencies
only (0.4.0 parity port, 2026-08-24). The 113-test gap is the extras-gated EML,
KNB and context-reader tests; the 3 that skip either way are filesystem-symlink
and R-availability guards.

**Run it from a directory named `metasalmonpy`.** The root `__init__.py` and
`tests/__init__.py` make pytest infer the package name from the checkout
directory, so a git worktree parked at `.../my-fix` collects the suite as
package `my-fix` and *every* test errors on a relative import — a wall of
failures that looks like a broken branch and is only a broken path. Put an
auxiliary worktree at a path whose last component is `metasalmonpy`
(`git worktree add ../.worktrees/<topic>/metasalmonpy <branch>`). Retire this
note if the package ever moves into its own `src/metasalmonpy/` directory,
which is what would make the checkout name irrelevant.

**And check which tree you are actually testing.** `package-dir` makes an
editable install register a `sys.meta_path` finder pinned to the *absolute
path it was installed from*, and `sys.meta_path` is consulted before
`sys.path`. A virtualenv built in the primary checkout therefore keeps
importing the primary checkout's modules while you run pytest inside a
worktree — a green suite that says nothing about the branch you are on, and
nothing in the output hints at it. Install the package from the worktree, and
confirm it took:

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

**CI runs the suite twice**, as the two legs of the `python` matrix in
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
