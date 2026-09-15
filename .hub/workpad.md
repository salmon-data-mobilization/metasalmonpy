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

---

# Follow-up: the three Codex P2 findings on PR #28 (2026-09-15)

Same claim, same branch (`agent/B-126/a-88b7c77b74107ea6`), follow-up on the
hand-back. Fresh worktree; no new claim taken, and `hub beat` reports the tip is
this agent's own `handoff`, so there is no lease to extend. **No reply to and no
resolution of the review threads** — the writes register denies an agent any
review or comment write, and Brett handles those.

## What changed, and where

- **`sdp_field_setters.py`** — finding 1. `_is_unresolved_iri()` is a new,
  SECOND predicate for the `REVIEW:` IRI marker, used from the gap scan's
  per-field loop for every schema-declared `*_iri` field of the three files
  whose markers block (`_REVIEW_IRI_FILES`). `is_review_placeholder()` is
  untouched. `_IRI_FIELD_HINTS` / `_iri_hint()` give the marker branch and the
  blank branches one spelling of each field's prompt. Finding 2: `_SCHEMA_SOURCE
  = "vendored"` and all five schema reads in the module pass it.
- **`sdp_schema.py`** — finding 2. `source` argument on `sdp_schema_fields()`,
  `sdp_schema_field_names()`, `sdp_schema_required_field_names()` and
  `sdp_schema_field_description()`; `"vendored"` reads
  `_vendored_schema_document()` directly. That function existed and had **no
  caller at all** before this change, which is the shape of the defect: the
  offline reader was written and never wired up.
- **`metadata_write.py`** — finding 3. `_with_hand_picked_accept()` records an
  accepted IRI no candidate row carries, inserted at the head of its slot;
  `_SLOT_ADDRESS_COLUMNS` and `_HAND_PICKED_SOURCE` name what it copies and what
  it says about itself. The decision loop recomputes `_review_slot_id()` per
  decision because an insert invalidates every earlier mask.
- **`tests/test_sdp_field_setters.py`**, **`tests/test_review_console.py`** — six
  new tests; `test_a_review_marked_iri_belongs_to_review_semantics_not_here` was
  replaced by `test_a_review_marked_iri_is_reported_by_both_reviews`, because it
  pinned the defect as intended behaviour.
- **`CHANGELOG.md`** — one Fixed entry per finding, plus the dated R measurement
  showing all three are owed in metasalmon.

## Failing before, passing after

Three separate reproductions, run on `05be2a17f7` before any fix:

```
FAILED tests/test_sdp_field_setters.py::test_a_review_marked_iri_is_reported_by_both_reviews
  E  assert 0 == 1  (the REVIEW:-marked property_iri produced no gap row)
FAILED tests/test_sdp_field_setters.py::test_an_empty_review_says_nothing_is_outstanding
  E  ValueError: Validation cannot pass while REVIEW-prefixed IRI values remain.
     term_iri: spawner_count (rows 2); property_iri: ...   <- while the review was empty
FAILED tests/test_sdp_field_setters.py::test_review_metadata_makes_no_http_request_on_the_default_schema_source
  E  _NetworkReached: review_metadata() made an HTTP request
FAILED tests/test_review_console.py::test_an_accept_outside_the_shortlist_reaches_the_decision_record
  E  assert 0 == 1  (no row marked "accepted")
FAILED tests/test_review_console.py::test_a_hand_picked_accept_replays_on_rebuild
  E  assert 0 == 1  (nothing replayed on rebuild)
FAILED tests/test_review_console.py::test_a_hand_picked_accept_is_visible_inside_max_candidates
FAILED tests/test_review_console.py::test_applying_a_hand_picked_accept_twice_produces_identical_bytes
7 failed, 89 deselected
```

After: `8 passed, 88 deselected` for the same selection.

Both dependency legs CI runs, measured on this machine against the branch head
before and after:

| leg | before | after |
|---|---|---|
| `.[test,eml,context]` (R available) | 898 passed / 1 skipped | 904 / 1 |
| `.[test]`, core deps only | 785 / 114 | 791 / 114 |

Each leg asserts its own configuration: the core leg confirms `yaml`, `lxml`,
`openpyxl`, `pypdf` and `xlrd` are all unimportable; the extras leg confirms all
five import. Also green: `python -m pytest tests/test_roundtrip.py` (2 passed,
the R↔Python round trip, `/tmp/metasalmon-lib` present), `python tests/smoke.py`,
`uv lock --check`, `python -m build`, `git diff --check`.

**The PR body's "896/3 and 783/116" does not reproduce here.** Measured on the
unmodified branch head this machine gives 898/1 and 785/114. The 3-vs-1 skip
difference is R availability (`/tmp/metasalmon-lib` exists here), and 785-vs-783
is a real two-test difference I did not chase because it is outside these three
findings. CI's own numbers are the ones to read on the PR.

## Does R have the same defect?

All three, yes, measured 2026-09-15 against installed metasalmon 0.5.0 under
R 4.3.3 (probe scripts were run outside the repository and are not committed):

1. `.ms_is_unfilled_metadata()` (`R/sdp-field-setters.R`) answers `FALSE` for
   `REVIEW:https://w3id.org/smn/SpawnerAbundance` and `TRUE` for a prose
   placeholder and for `""`. End to end, R's `review_metadata()` printed
   `No outstanding metadata.` for a package `validate_salmon_datapackage(pkg,
   require_iris = TRUE)` then refused with `Validation cannot pass while
   REVIEW-prefixed IRI values remain`.
2. With `metasalmon.sdp_schema_source` at its default and `.ms_schema_env`
   cleared, a mocked `httr2::req_perform` sentinel **fired** inside
   `review_metadata(pkg)`, whose own roxygen says "It never contacts a network or
   an LLM". Path: `.ms_metadata_schema_fields()` → `.ms_load_sdp_schema(quiet =
   TRUE)` → `.ms_fetch_remote_sdp_schema()` → `httr2::req_perform()`.
3. R's `accept_suggestion(review, "spawner_count", "variable", iri =
   "https://example.org/Handpicked")` + `apply_sdp_semantics()` wrote the IRI
   into `column_dictionary.csv` and left `semantic_suggestions.csv` with one
   `not_selected` row, no `accepted` row, no row carrying the hand-picked IRI,
   and zero decisions replayed by `review_semantics(pkg, include_filled = TRUE)`.

So all three are **ports**, not deviations: no `PARITY.md` row, and nothing was
written to metasalmon. Each needs its own queue item; they are described in the
section below.

## Guards and decisions added, with their retirement conditions

1. **`_is_unresolved_iri()` as a second predicate rather than a wider
   `is_review_placeholder()`** (`sdp_field_setters.py`). *Retires when:* nothing.
   The two answer different questions about different kinds of field. Widening
   the prose predicate would have moved the marker into three channels built to
   exclude it — the `license` gate (`package_io.py`), the placeholder sweep in
   `validate_salmon_datapackage()`, and `_gap_row()`'s "is the value's own text a
   usable hint" test. `test_a_prose_placeholder_is_not_reclassified_as_an_iri_gap`
   pins that it was not widened.
2. **`_REVIEW_IRI_FILES`** (`sdp_field_setters.py`). *Retires when:*
   `_collect_review_issues()` changes which files it sweeps; the two lists move
   together. It excludes `dataset.csv` on purpose: a marker there is not swept by
   strict validation, so reporting one would be this scan claiming a block that
   does not exist — the same class of error as missing one, pointing the other
   way. See candidate item 3 below.
3. **`_SCHEMA_SOURCE = "vendored"`** (`sdp_field_setters.py`) and the `source`
   argument (`sdp_schema.py`). *Retires when:* `load_sdp_schema()` stops fetching
   on its default source, at which point the default is already offline and both
   have nothing left to say.
4. **`_NetworkReached(BaseException)`** (`tests/test_sdp_field_setters.py`).
   *Retires when:* `load_sdp_schema()` stops catching bare `Exception` around the
   fetch, at which point an ordinary exception is a sufficient sentinel. Until
   then a sentinel that derives from `Exception` is swallowed by the fallback and
   the test passes whether or not the path is offline.
5. **The recorded hand-picked row is inserted at the head of its slot, not
   appended** (`metadata_write.py`). *Retires when:* `review_semantics()` stops
   deriving `rank` from file position, or stops applying `max_candidates` to a
   row carrying a decision. Appended after a full shortlist the record would rank
   6 and be filtered out, losing the same decision one layer on;
   `test_a_hand_picked_accept_is_visible_inside_max_candidates` is what fails if
   the placement regresses.

## Found, not mine — candidate new items

1. **Candidate (metasalmon, R): `review_metadata()` reports a clean package that
   strict validation refuses.** The R half of finding 1. Evidence above. The
   Python fix in this PR is the shape to mirror, including keeping
   `.ms_is_review_placeholder()` narrow.
2. **Candidate (metasalmon, R): `review_metadata()` contacts the network on the
   default schema source.** The R half of finding 2. R needs the same treatment
   plus its own sentinel; note that `.ms_load_sdp_schema()` wraps the fetch in
   `tryCatch(error = ...)`, so an R sentinel must signal a **non-error**
   condition or it is swallowed exactly as a Python `Exception` would be.
3. **Candidate (both): a `REVIEW:` marker on `dataset.csv`'s `*_iri` fields is
   not swept by strict validation.** `_collect_review_issues()` sweeps
   `tables.csv`, `column_dictionary.csv` and `codes.csv` only, and
   `_collect_placement_iri_issues()` explicitly *skips* `REVIEW:` values. No
   producer writes a marker there today (`_mark_review_iri()` is reached only for
   the dictionary's six role fields and `tables.csv$observation_unit_iri`), so
   this is latent rather than live — which is why `_REVIEW_IRI_FILES` excludes
   `dataset.csv` rather than over-reporting. R's `R/edh-xml-export.R` *does*
   sweep `dataset.csv` while its `validate_salmon_datapackage()` does not, so
   there may be a second question here about which of the two is right.
4. **Candidate (metasalmon, R): `apply_sdp_semantics()` loses a hand-picked
   accept.** The R half of finding 3. Evidence above.
5. **Unrelated to these findings: the PR body's test counts.** 896/3 and 783/116
   do not reproduce on this machine (898/1 and 785/114 on the unmodified head).
   Worth one look at whether the PR body was measured on a different tree.
