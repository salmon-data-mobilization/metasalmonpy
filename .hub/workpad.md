# B-126 — Port metasalmon 0.5.0 to metasalmonpy: the nine review functions, decision_reason, and the constraints.required consumer

Queue item: `queue/items/B-126.yaml` (P1, stream S5, repo `metasalmonpy`).
Evidence pointer: `knowledge/parity-deviations.md`, section *"What metasalmon
0.5.0 owes the mirror"*.

This is the mirror contract's open catch-up window. metasalmon released
`v0.5.0` on 2026-08-25 (roadmap S5) and this package was at 0.4.0 with none of
it. The gap is ordinary "R shipped first" lag, so it is owed as a **port**, not
as deviation rows — **no new `PARITY.md` rows were opened**, and the one
register change is an in-place amendment to row 31.

## What changed, and where

### New modules

- **`review_console.py`** — `review_semantics()`, `accept_suggestion()`,
  `reject_suggestion()`, the `semantic_suggestions()` /
  `semantic_llm_assessments()` accessors, and the `SemanticReview` object.
  Mirrors `R/review-console.R`.
- **`metadata_write.py`** — `apply_sdp_semantics()`. Mirrors
  `R/metadata-write.R`. Surgical, transactional, re-runnable; does not touch
  data CSV bytes.
- **`sdp_field_setters.py`** — `review_metadata()` and `set_sdp_dataset()` /
  `set_sdp_table()` / `set_sdp_column()` / `set_sdp_code()`. Mirrors
  `R/sdp-field-setters.R`.

### Changed modules

- **`semantics.py`** — the **#118** fix: `strategy="reviewed"` is exempt from
  `_filter_auto_apply_suggestions()` (was line 1294). `"top"` and `"llm"` keep
  the gate. Docstring records the split and its retirement condition.
- **`sdp_schema.py`** — `sdp_schema_fields()`,
  `sdp_schema_required_field_names()`, `sdp_schema_field_description()`. The
  **first consumer of `constraints.required`** in this package;
  `review_metadata()` is built on it. `sdp_schema_field_names()` now delegates
  to `sdp_schema_fields()` rather than re-loading the bundle.
- **`package_io.py`** — the three descriptor builders
  (`_descriptor_field_entry()`, `_descriptor_apply_resource_meta()`,
  `_descriptor_apply_dataset_meta()`) extracted from
  `write_salmon_datapackage()` and now shared with the setters and the review
  write-back, plus `_descriptor_sync_fields()`. Also:
  `_warn_pruning_recorded_decisions()` (the `prune=True` warning) and
  `_write_review_readme()` rewritten to hand over the Python calls instead of
  sending users to a spreadsheet.
- **`__init__.py`** — the nine functions, the two accessors and the two review
  classes exported and added to `__all__`.
- **`_quarto.yml`** — new "Review and edit (in Python)" API reference group.

### Registers and docs

- **`PARITY.md` row 31 amended in place** (not appended to), with the clause
  drafted under *"What metasalmon 0.5.0 owes the mirror"* adjusted to the facts
  as they now stand: the drafted text said "neither is ported yet", and both
  are. It now records that the identity claim was true against metasalmon
  0.4.0, broke at 0.5.0, and holds again as of this port — **re-measured**, see
  Evidence below.
- **`AGENTS.md`** parity paragraph corrected. It read "both at 0.4.0, no window
  open", which was false from 2026-08-25. It now states the open
  `0.4.0 → 0.5.0` window, what the port landed, why the number stays at 0.4.0,
  and records this as the **third** occurrence of the three-copies fact
  disagreeing. Test counts in *Build / test* refreshed and marked as a dated
  measurement.
- **`CHANGELOG.md`** — full Unreleased entry (Added / Fixed / Changed); the
  duplicate `### Added` heading created by the insert was merged away.

## Commands run, and their results

### The #118 defect: failing before, passing after

Reproduction fixture (`ReviewedExemptFromAutoApplyGateTests` in
`tests/test_semantics.py`) is the shape the defect actually takes — an
`attribute` column whose accepted term label shares no token with the column
name, so `_non_measurement_suggestion_is_compatible()` refuses it.

**Before the fix:**

```
$ .venv/bin/python -m pytest tests/test_semantics.py -q -k ReviewedExemptFromAutoApplyGate
F..
>       self.assertEqual(self._apply("reviewed"), self.WATERCOURSE)
E       AssertionError: '' != 'https://w3id.org/smn/WatercourseDesignation'
1 failed, 2 passed, 28 deselected
```

The two that passed are the premise (`_filter_auto_apply_suggestions()` does
refuse this candidate) and the control (`"top"` drops it, and must keep
dropping it).

**After the fix:**

```
$ .venv/bin/python -m pytest tests/test_semantics.py -q
31 passed, 37 subtests passed
```

End-to-end through the review flow, the same defect is pinned by
`test_a_reviewed_accept_the_lexical_gate_would_drop_still_lands` in
`tests/test_review_console.py`.

### Suite, both dependency legs (the property CI checks)

| leg | before | after |
|---|---|---|
| `.[test,eml,context]` | 803 passed / 3 skipped | **896 passed / 3 skipped** |
| core deps only (`pandas`, `requests`, `pytest`) | 690 passed / 116 skipped | **783 passed / 116 skipped** |

Baseline measured on this branch with `git stash -u`. Core-only leg verified to
be core-only first (`yaml`, `lxml`, `openpyxl`, `pypdf`, `xlrd` all
unimportable). The three new modules import only pandas and the standard
library, so nothing moved across the dependency boundary.

With R plus metasalmon installed the figure is **898 passed / 1 skipped** — the
two extra are the R↔Python round-trip tests, see below.

### R↔Python round trip (the parity job CI runs)

```
$ R CMD INSTALL /home/user/metasalmon -l /tmp/metasalmon-lib     # metasalmon 0.5.0
$ .venv/bin/python -m pytest tests/test_roundtrip.py -q
2 passed
```

Run deliberately, because the descriptor-builder extraction touches exactly the
bytes this test compares. It passed.

### Re-measurement behind the amended row 31

R side driven directly against metasalmon `main` @ `4cd085c`
(`DESCRIPTION` 0.5.0), Python side against this branch, same inputs:

| case | metasalmon `main` | this branch |
|---|---|---|
| `strategy="top"` | `…/SpawnerStageContext` | same |
| `strategy="reviewed"` | `…/SpawnerStageContext; …/FemaleSex` | same |
| `strategy="llm"` | `…/SpawnerStageContext; …/FemaleSex` | same |
| #118 case, `reviewed` | `…/WatercourseDesignation` | same |
| #118 case, `top` | `""` | same |

Five values, byte-identical. The first three also still match the v0.1.8
`expected-apply-strategies.json` fixture, which the existing
`test_all_three_strategies_match_r` asserts.

### Other checks

```
$ .venv/bin/python tests/smoke.py        # metasalmonpy smoke test passed
$ .venv/bin/python -m build              # built sdist + wheel; all three new
                                         # modules present in the wheel
$ uv lock --check                        # Resolved 82 packages (no drift)
$ git diff --check                       # clean
```

## What I did not do, and why

1. **The version stays at 0.4.0.** The S5 *behaviour* has landed, but
   metasalmon 0.5.0's documentation half has not: `guides/semantic-review.qmd`
   still presents the spreadsheet as the workflow rather than as the fallback,
   which is the change 0.5.0's own NEWS entry leads with. The version is a
   parity claim and Brett's 2026-08-13 decision is "bump on parity, not on
   calendar", so bumping it here would put a false claim in the tree — and a
   bump is a release act (tag plus GitHub Release) that an agent may not
   perform. `AGENTS.md` now names exactly what closes it. **Needs Brett.**
2. **`read_salmon_datapackage()` does not gain a `semantic_suggestions` key.**
   `retires_when` asks for "reads `semantic_suggestions.csv` back in
   `read_salmon_datapackage`", but R's own `read_salmon_datapackage()` returns
   only `dataset` / `tables` / `dictionary` / `codes` / `resources`
   (`R/package-helpers.R:1602`) and does **not** carry the suggestions. In R the
   read-back is the accessor: `semantic_suggestions(path)` reads the CSV out of
   a written package. That is what is implemented here, and it is what
   `review_semantics(path)` uses. Adding a Python-only key would be an
   undocumented divergence from the mirror, and the item forbids opening rows
   for this gap. **Flagged as a question for Brett rather than decided.**
3. **The three-copies fact outside this repo.** metasalmon's own `AGENTS.md`
   already states the window correctly, but the release index in
   `knowledge/roadmap.md` needs checking against it. Out of scope for a claim
   scoped to `metasalmonpy`; reported to the orchestrator, not edited.
4. **`guides/semantic-review.qmd` and the other Quarto guide prose.** Only the
   API reference nav was updated. The guide rewrite is the remaining piece of
   0.5.0 and is what unblocks the version bump; left as named follow-up work
   rather than absorbed.
5. **`create_sdp()`'s closing console message.** R's `create_sdp()` ends with a
   cli message that 0.5.0 rewrote to name the R calls; this package's
   `create_sdp()` prints no closing message at all. That is a pre-existing
   difference in console verbosity, older than this window, and not mine to
   close inside a port.

## Guards, suppressions and workarounds added, with their retirement conditions

1. **The `strategy="reviewed"` auto-apply exemption** (`semantics.py`).
   *Retires when:* never — this is the intended split. If a future strategy
   also represents an explicit human decision, add it to the exemption. Stated
   in the code comment, mirroring R's.
2. **`_warn_pruning_recorded_decisions()`** (`package_io.py`). *Retires when:*
   the write path preserves `semantic_suggestions.csv` across a prune, at which
   point there is nothing left to warn about.
3. **`METADATA_KEY_FIELDS` as a not-settable list** (`sdp_field_setters.py`).
   *Retires when:* a setter can address a row without a key — which it cannot,
   so read it as permanent rather than as an unexplained exclusion.
4. **`MEASUREMENT_IRI_FIELDS` enumeration** (`sdp_field_setters.py`). The
   requirement lives in the dictionary validator, not the schema (the schema
   calls these `conditional`). *Retires when:* the schema states the
   measurement IRI requirement itself. Until then the enumeration must be kept
   in step with `validate_dictionary()`, and a test drives the validator so a
   drift fails rather than silently reporting a clean package.
5. **`test_a_column_level_slot_sharing_a_role_with_its_codes_is_still_ambiguous`**
   (`tests/test_review_console.py`) — pins a **known shared defect** rather
   than asserting correct behaviour. *Retires when:* the defect is fixed in both
   implementations; delete the test then. See item 1 below.
6. **`if primary_key:` guard in `_descriptor_apply_resource_meta()`**
   (`package_io.py`). The pre-extraction code indexed `primary_key[0]`
   unconditionally, which raises `IndexError` on a `primary_key` of `","` (it
   passes `_meta_scalar_present()` and splits to nothing). *Retires when:*
   `_meta_scalar_present()` rejects a value that splits to no parts. R writes
   `[]` for this input where this now omits the key; the input is pathological
   and reaches neither implementation from any producer, but it is a difference
   and is named here rather than left in the diff.

## Found, not mine — reported rather than absorbed

1. **Candidate new item (affects BOTH implementations): the printed
   `accept_suggestion()` call is ambiguous for a column-level slot that shares
   a role with its own code-level slots.** `_review_call_args()` (R:
   `.ms_review_call_args()`) adds `code_value=` only when the row it is printing
   *has* one, and `_match_slot_rows()` (R: `.ms_review_match_slot_rows()`)
   leaves `code_value` unconstrained when it is not passed. A **measurement**
   column with a code list gets a column-level `entity_iri` target (role
   `entity`, no `code_value`) *and* a `codes.csv` `term_iri` target per code
   (role `entity`, with a `code_value`) — see the
   `["constraint", "entity", "method"]` role set for a measurement parent in
   `semantics.py`. The column-level slot therefore prints
   `accept_suggestion(review, "col", "entity", rank=1, table="t")`, which
   resolves to two slots and raises. Reachable from the real pipeline, not only
   from a hand-built frame. **This is the exact defect class
   `R/review-console.R`'s own header says the design exists to prevent** ("a
   printed call that does not parse, or that names a column that does not
   exist"), and R 0.5.0's tests did not catch it. The fix is one line on each
   side: a column-level slot should constrain `code_value` to *absent* rather
   than leaving it unconstrained. **Not fixed here**, because a deliberate
   divergence needs a `PARITY.md` row and this item forbids opening one for
   this gap; pinned by the test named above so it is visible rather than
   latent. The raised message does name `code_value=` as the argument to add,
   so a user can recover.
2. **`knowledge/roadmap.md`'s release index** is the third copy of the version
   fact and should be read against both `AGENTS.md` files in the same change
   that merges this. Cannot be edited under a claim scoped to `metasalmonpy`.
3. **B-124** (validation port, blocked by B-49) and **B-125** (role inference
   port, blocked by B-95) are named in the same parity-register section as
   separate items and were left alone. `sdp_schema_required_field_names()`,
   which this port adds, is what B-124's blank-required-field check will
   consume — noting it so B-124 does not add a second parser.

## No new parity-register rows

Deliberate, per the item. Two language-level differences are intrinsic to the
port rather than deviations, and are recorded in `CHANGELOG.md` instead of as
rows: the printed call uses Python syntax (`review = …`, `rank=1`) because a
Python package printing R syntax would print a line its own user cannot paste;
and `pd.NA` / `None` spell R's `NA` / `NULL` in the setters. Both fall under the
contract's "simple language differences that do not materially change behaviour
or capability are fine".
