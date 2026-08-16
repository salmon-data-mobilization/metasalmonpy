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
3. **Current honest state:** parity is at **metasalmon 0.1.6**. metasalmon is
   at 0.2.6. The catch-up (0.1.7 → 0.2.6) is roadmap stream **S10** in the
   hub; this package's version stays 0.1.6 until parity milestones land —
   do **not** bump the number ahead of the functionality (Brett's decision,
   2026-08-13: bump on parity, not on calendar).
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

or `pip install -e ".[test]" && pytest -q`. The suite must stay green
(93 passed / 3 skipped as of the 2026-08-13 rename).

## Layout notes

- Modules live at the repo root and are packaged as `metasalmonpy` via
  `pyproject.toml`'s `package-dir` mapping (`"metasalmonpy" = "."`).
- Keep module names aligned with metasalmon's `R/` topic files where a
  counterpart exists (e.g. `dictionary.py` ↔ `dictionary-helpers.R`).
- This file and `CLAUDE.md` are git-tracked on purpose; do not add them to
  `.gitignore`.
