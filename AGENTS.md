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
3. **Current honest state:** parity is at **metasalmon 0.2.1**. metasalmon is
   at 0.3.0. The catch-up (0.2.2 → 0.3.0) is roadmap stream **S10** in the
   hub; this package's version stays at the last delivered milestone until
   the next one lands — do **not** bump the number ahead of the functionality
   (Brett's decision, 2026-08-13: bump on parity, not on calendar).
4. New metasalmon work started after 2026-08-13 must land its Python mirror
   as part of the same stream, so the gap never widens again.

## Coordination hub

The metasalmon repo is the coordinating hub for this family of repos
(metasalmon, metasalmonpy, smn-data-pkg, salmon-domain-ontology,
dfo-salmon-ontology, psc-salmon-vocabularies). Sequencing, execplans, and the
cross-repo release index live in metasalmon's `knowledge/` OKF bundle — start
at its `ROADMAP` card. Do not maintain a competing roadmap here.

## Build / test

```sh
uv run --with pytest --with pandas --with requests -- python -m pytest tests/ -q
```

or `pip install -e ".[test]" && pytest -q`. The suite must stay green: 591
passed / 3 skipped with the extras installed, 497 / 97 with core dependencies
only (0.2.1, 2026-08-18).

**Run it from a directory named `metasalmonpy`.** The root `__init__.py` and
`tests/__init__.py` make pytest infer the package name from the checkout
directory, so a git worktree parked at `.../my-fix` collects the suite as
package `my-fix` and *every* test errors on a relative import — a wall of
failures that looks like a broken branch and is only a broken path. Put an
auxiliary worktree at a path whose last component is `metasalmonpy`
(`git worktree add ../.worktrees/<topic>/metasalmonpy <branch>`). Retire this
note if the package ever moves into its own `src/metasalmonpy/` directory,
which is what would make the checkout name irrelevant.

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

The core-deps CI job runs exactly this. Because most local environments have
the extras installed, `tests/test_knb_publication.py::KnbCoreDependencyTests`
also blocks `yaml` from `sys.meta_path`, so the property fails locally too
rather than only in CI. When you add a reader to a shared helper, check what
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
