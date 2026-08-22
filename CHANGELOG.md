# Changelog

## Unreleased

Not a version bump: the parity claim stays at **metasalmon 0.2.1**. The S10
chunk-A work below is the sdp-0.3.0 breaking change, and it lands deliberately
unversioned: *which* number the finished port may carry is the execplan's open
decision 2 (hub Q7) — the chunks deliver behaviour verified against
metasalmon's post-0.3.0 `main`, which no R release contains, and nobody has
ruled what a number may claim about that. The bump comes once, at the end of
the S10 chunks, after that ruling. Bump on parity, not on calendar.

### S10 chunk C — the missing-value contract

metasalmon 0.2.4's canonical missing-value contract, completed. Most of it
was forward-ported early (PARITY.md row 21, landed at 0.1.7–0.2.0): every
reader already preserved a literal `"NA"` — a real fisheries gear code — and
only the empty field was missing. This chunk delivers the two pieces that
were not yet true, verified by **running both implementations over the same
inputs** against metasalmon `main` at `39818ce` (2026-08-22):

- **The token has one authority.** `metadata.csv_na_token()` mirrors
  `.ms_csv_na_token()` and returns `""`; every canonical CSV writer
  (`package_io`'s metadata, resource and suggestions writers, the
  `sdp_methods` extension-CSV renderer, the measurement-decomposition
  renderer) now passes it explicitly instead of relying on matching literals
  or on pandas' default `na_rep`, and `scripts/validate_sdp.py` reads through
  the shared `read_sdp_csv` instead of a bare `pd.read_csv()` whose default
  NA vocabulary destroyed the very tokens it was validating.
  `tests/test_missing_value_contract.py` guards the call sites (an implicit
  agreement that holds only incidentally is the thing that decays), pins the
  round trip **on the written bytes** — byte-identical to R `main`'s output
  for the same adversarial frame ("NA", "N/A", "null", "nan", "None",
  embedded commas/quotes/newlines, non-ASCII, a true missing and a true empty
  string) — and pins write→read→write as a fixed point.

- **The EML raw-token audit joins the contract, closing PARITY.md row 22's
  live divergence.** `_ERA_NA_TOKENS` (`("", "NA")`) is deleted; audit
  missingness is now "raw token trims to `csv_na_token()`", which is exactly
  R's `is.na(parsed_values)` under R's one-token read. Measured verdict
  changes, each matched against R `main`: a literal `NA` cell with no
  declared missing-value code is **accepted** (it was rejected here while R
  accepted it — the active interop hazard row 22 warned about), a padded
  `" NA "` cell is **accepted** (data after trimming), a whitespace-only cell
  still **aborts** as an undeclared non-empty missing token, a declared code
  the bytes never contain still **aborts**, and a declared `"NA"` code over
  literal-`NA` data now **aborts** ("declares missing-value code where the
  parsed value is not missing") exactly as current R does.

- **The era fixture corpora moved with the contract**, because their old
  bytes were unregenerable: both fixture families declared
  `missingValueCode = "NA"` over era-written bytes, which current R rejects
  (declared-but-present) and the converged audit here rejects identically.
  `tests/data/eml/` was regenerated against metasalmon `main` @ `39818ce`
  (the SDPs are now sdp-0.3.0-shaped as R's own test helper writes them; the
  four XML documents are canonically equal between R `main` and this package,
  with identical identifiers, which is this chunk's EML differential).
  `tests/data/knb/`'s era pair (`sdp-public`, `sdp-private`) kept its
  **v0.1.8 parity claim**: the derived artifacts were regenerated under the
  same era metasalmon v0.1.8 extraction and toolchain with ONLY the
  missing-value contract moved — the generator was proven first by
  reproducing all 22 committed era artifacts byte-for-byte from the unmoved
  inputs. (`sdp-full` had already converged at chunk A;
  `knb-manifest-v2.json` is a frozen legacy-schema fingerprint fixture and
  deliberately keeps its era values.) Migrating the era pair to current-main
  shape instead would have dragged unported KNB behaviours (chunk E scope)
  into this chunk, which is contractually undiluted.

One divergence found in passing by the byte differential — R fills more
metadata placeholders than this package and with different prose — is
registered as PARITY.md row 48 for chunk D rather than fixed here (48
because the concurrent chunk B had already committed a row 47 on its branch
— the numbering collision the registers' precedent exists for, caught by
running the hub's `check-parity-registers.py` before pushing).

### S10 chunk A — the sdp-0.3.0 method placement model (breaking)

Mirrors metasalmon 0.3.0's breaking change plus its post-0.3.0 fixes.
Everything here was verified by **running both implementations over the same
inputs** against metasalmon `main` at `e02111a` (the v0.3.0 release tree plus
the post-0.3.0 fixes, including the 2026-08-21/22 recon-defect wave), never by
reading R source alone; the four R-derived fixture families in `tests/data/`
were regenerated at that commit and each carries its provenance.

- **`column_dictionary.csv` loses `method_iri` and gains
  `statistical_modifier_iri`.** A statistical modifier is part of variable
  identity — a *mean* weight and a *maximum* weight are different variables —
  so it belongs in the dictionary. A method never was: a procedure shared by a
  whole table now lives in `tables.csv` `method_iri`, protocols are cited
  through `protocol_iri` / `protocol_citation` on `tables.csv` and
  `dataset.csv`, and a row-varying method lives in the data as a code column
  resolving through `codes.csv` `term_iri`. The rename reaches all nine
  affected modules (`metadata`, `dictionary`, `validation`, `package_io`,
  `sdp_methods`, `observation_structures`, `eml`, `measurement_decompositions`,
  `term_requests`); the semantic-pipeline retarget (`semantics`, `llm_review`)
  is chunk B, so until it lands measurements review five column slots and the
  bundle's method slot arrives `already_filled_or_not_requested`.

- **The vendored schema bundle and `SDP_SPEC_TAG` move to `sdp-0.3.0`
  together** (PARITY.md rows 27 and 38: the pin and the bundle must never name
  different spec eras). The bundle is a verbatim copy of the upstream
  `sdp-0.3.0` git tag — byte-identical to what metasalmon vendors — which has
  no `methods.schema.json`: the registry left the specification, so
  `SDP_METHODS_COLUMNS` is now a frozen legacy contract rather than a read of
  the bundle.

- **`migrate_sdp_methods()` migrates sdp-0.2.0 packages**, ported from
  metasalmon `main` so **every stop fires in the dry run as well as the real
  run** (the release tree checked the placement destination only after the
  dry-run return, so a clean preview could promise a migration the real run
  refused). It relocates what can be relocated mechanically and stops and
  reports on anything needing a judgement call: carriers disagreeing about one
  column, columns of one table bound to different methods, a method bound to
  only some measurement columns, bindings naming undeclared tables or no
  table/column at all, an existing `tables.csv` claim that disagrees, and a
  non-bool `dry_run`. `REVIEW:` bindings are dropped and reported; registry
  labels are reported toward the shared vocabulary. The rewrite is atomic —
  registry renamed aside first, restored on failure — and the legacy
  `iAdopt:methodIri` descriptor key is read by the migration (and only by the
  migration). The final R fixture suite landed as pytest before the code, per
  the execplan's logged decision, and a nine-case differential (three
  migrating shapes, five stops, one no-op) matched R stop-for-stop with
  byte-identical rewrites.

- **The registry is removed from every current-package surface with errors
  that point at the migration** — `validate_salmon_datapackage()`, EML export,
  and KNB publication all refuse a lingering `metadata/methods.csv`. The
  **reader and validator survive** (`read_sdp_methods()` /
  `validate_sdp_methods()`) as deliberate legacy read support: this package
  receives R-0.2.x-written packages that carry a registry (PARITY.md row 9).
  `write_sdp_methods()` stays a raising stub, its message now saying the
  registry **is removed** at SDP 0.3.0 and pointing at the migration.

- **Method and protocol placements get the absolute-IRI check the dictionary
  columns already had**: `tables.csv` `method_iri`/`protocol_iri` and
  `dataset.csv` `protocol_iri` are checked unconditionally (reported in the
  returned semantic issues), and strict validation (`require_iris=True`)
  blocks them exactly as it blocks a `REVIEW:` marker. Observation-structure
  validation now checks table-level and enumerated `sosa:usedProcedure` IRIs
  for shared-vocabulary shape instead of registry membership.

- **EML method steps come from the placements.** Table-level method/protocol
  fields and dataset-level protocol fields each emit a method step, and
  row-varying procedures actually used by the data are listed from their code
  resolutions. `write_eml_from_sdp()`'s `methods` return is now the placements
  frame and `used_methods` the used-procedure IRIs. Every vocabulary IRI the
  method path emits stays inside the reviewed closure — a table-level
  `method_iri` needs an accepted semantic-review ledger row and a
  vocabulary-snapshot entry; protocol IRIs are citations and are not gated.
  Measurement decompositions drop the `method` component role for
  `statistical_modifier`, for the same reason the dictionary did.

### Fixes folded into chunk A

- **A failed rollback inside the shared atomic writer no longer deletes the
  backup holding the original bytes**, and the warning now names that file
  (mirrors metasalmon 0.3.0's fix; `observation_structures` inherits it
  through the shared writer). The rollback also survives a destination that
  something replaced with a directory, instead of crashing past the restore.

- **`sdp_methods` now builds its IRI patterns from `metadata.R_SPACE_CLASS`**
  exactly as `eml` and `sssom` already did (hub backlog #86, PARITY.md
  row 33 discharged). Python's `\s` disagreed with R's TRE `[[:space:]]` on 8
  codepoints and rejected every one where R accepts, so this validator was
  the stricter side and could refuse SDP-extension IRIs metasalmon accepts.
  Verdicts for all 12 probe codepoints now match R exactly, pinned by a
  membership test like the ones `test_eml.py` and `test_sssom.py` carry.

- **The bundled `column_dictionary.csv` demo was corrupt as shipped**: two
  descriptions with unquoted commas shifted every later field, so RUN_TYPE
  parsed with `column_role` `" Fall)"` and ESTIMATE_STAGE with `value_type`
  `" near-final)"` — schema-invalid values in a file users are pointed at as
  a starting point (the same defect metasalmon fixed at 0.3.0; found
  independently by the hub's 2026-08-21 recon). The demo metadata and sample
  data are now byte-copies of metasalmon `main`'s (`e02111a`), which also
  picks up the recon-wave example fixes (resolving IRIs, ISO dates, the
  crosswalked codes) and closes a previously unregistered drift in the demo
  `dataset.csv` source citation.

- **`read_salmon_datapackage()`'s descriptor branch now coalesces the
  per-field `custom` keys** (`sdp:columnRole`, `sdp:unitLabel`, `sdp:unitIri`,
  `sdp:termIri`, `sdp:termType`, `iAdopt:propertyIri`, `iAdopt:entityIri`,
  `iAdopt:constraintIri`, `iAdopt:statisticalModifierIri`) before the bare
  field properties, as R always has. Found while porting the reader half of
  the flip: this reader had silently dropped semantic bindings and column
  roles from descriptor-first legacy packages that carry them only under
  `custom`. The legacy `iAdopt:methodIri` key is deliberately **not** read —
  the migration reads old descriptors directly, so a descriptor-only
  sdp-0.2.0 package keeps its method binding until migration relocates it.

### Fixes

- **A `datetime` observation dimension is accepted.** `_as_r_character()`
  rendered a typed instant as `"2024-01-31 10:00:00"` — a space, no `T`, no
  zone — which `_DATETIME_RE` can never match, so every datetime-typed
  observation dimension was rejected. That was **deliberate**: metasalmon had
  the same defect, from taking `as.character()` of the POSIXct its typed
  reader produces, and this package mirrored the rejection rather than
  diverging silently, reporting it to the hub instead. The hub adjudicated it
  as a metasalmon defect and fixed it there
  (`.ms_sdp_observation_typed_character()`); the mirror and its documentation
  are gone, and the helper is renamed `_typed_character()` because it no
  longer mirrors `as.character()`. A tz-aware instant now folds into UTC.

  The adjudication also found the reverse: **metasalmon, not this package, was
  wrong about parsing ISO-8601 instants.** `as.POSIXct()` has no ISO-8601
  entry in its default format list and silently truncated
  `"2024-01-31T10:00:00Z"` to midnight, collapsing two distinct instants on
  one date into a single grain key. `_parse_datetime()`'s
  `datetime.fromisoformat()` was correct throughout; metasalmon was brought
  into line with it. Nothing changed here for that half.

### Adjudication of the 0.2.0 descriptor divergences

The seven `write_salmon_datapackage()` differences 0.2.0 fixed by conforming
to metasalmon were re-decided on their merits under Brett's 2026-08-17 ruling
("if the Python implementation got it right, then update metasalmon").
**All seven fixes stand**, and three of them are not merely house style —
they are load-bearing for publication. `smn-data-pkg`'s strict publication
validator (`scripts/validate_package.py`) compares `schema.fields` to the
`column_dictionary.csv`-derived list with `==`, so an extra, missing or
differing key is an error: suppressing `title` when it equals `name`, and
emitting `constraints: {"required": false}`, each fail it, and a one-element
`primaryKey` array is rejected by name — `primaryKey must be 'pop_id'; found
['pop_id']`. Measured, not reasoned: a package written by metasalmon passes
that validator, and each of this package's pre-0.2.0 behaviours reintroduced
individually makes it fail. No change was warranted on either side.

### CI runs both dependency configurations

No behaviour change; a coverage change and a documentation correction.

`parity.yml`'s `python` job installed `.[test]` — `build` plus `pytest`, and
neither `[eml]` nor `[context]`. The single full-suite CI run was therefore
core-deps-shaped **by accident**, and the 94 extras-gated tests (EML, KNB,
context readers) ran nowhere: 499 passed / 97 skipped was the only result CI
had ever produced. The job is now a two-leg matrix — *core dependencies only*
(`.[test]`) and *with `[eml]` and `[context]` extras* (`.[test,eml,context]`)
— because each configuration is the only thing that can test the other's
claim: `KnbCoreDependencyTests` means nothing in a run that has the extras,
and the extras-gated tests mean nothing in a run that lacks them.

Each leg asserts its own configuration before running anything — the core leg
fails if `yaml`, `lxml`, `openpyxl`, `pypdf` or `xlrd` is importable, the
extras leg fails if any is not — so a typo in an extras list cannot turn the
extras leg into a second core-deps run that skips the tests it was added for
and still reports green.

`AGENTS.md` and `PARITY.md` row 30 both said "the core-deps CI job runs the
whole suite with neither extra installed", which read as a deliberate job
sitting alongside a normal one. There was only the one job, so the sentence
described an accident as design and implied broad coverage that existed
nowhere. Both are corrected, and `KnbCoreDependencyTests`' docstring with
them. Hub backlog #92.

## 0.2.1

**This release is a parity claim against metasalmon 0.2.1.** Built against the
commit that made that version current on `main` (`f675d91`), extracted
read-only with `git archive` and loaded with `pkgload::load_all()`, and
verified by **running both implementations over the same inputs**. Paired with
0.2.0 in one pull request because 0.2.1's per-resource schema URLs are read
through the loader 0.2.0 introduced, and building an interim shape only to
rewrite it would have been the more error-prone route. PARITY.md rows 39 and 40 are new.

### Fixes

- **Canonical calendar strings no longer depend on the C library that built
  the interpreter.** `strftime` hands `%Y` to libc, and glibc does not
  zero-pad a year below 1000 where the macOS/BSD implementation does, so a
  date declared `0001-01-01` keyed as `0001-01-01` on macOS and `1-01-01` on
  Linux. Canonical value keys are what decide whether a data column matches
  its own `codes.csv`, and `render_resource_frame` writes the same strings
  into package bytes, so the determinism this rung claims did not actually
  hold across machines.

  Every calendar rendering now builds its text by explicit padding
  (`resource_types._iso_date`, `._iso_seconds`) or by `date.isoformat()`,
  both pure Python. Three call sites outside the new typed reader carried the
  same defect and are fixed with it: the datetime branch of
  `observation_structures._normalize_typed_values` — whose *date* branch was
  already safe, which is what makes it an oversight rather than a choice —
  `_as_r_character`, and the EML calendar-value round-trip check, which would
  have rejected a valid pre-1000 date on Linux only.

  `tests/test_platform_determinism_guard.py` now fails on any `strftime` call
  in a package module, with an allowlist that must name what retires each
  entry. The guard exists because this failure is **invisible to a macOS
  developer** — the suite was green locally on every run while
  `test_canonical_keys_match_era_r[date]` failed in CI — which is the same
  class of decay `KnbCoreDependencyTests` was written for. PARITY.md row 40
  registers the open question of whether metasalmon's own date key is
  portable; the hub owns that side.

- **Per-resource schema URLs in `datapackage.json` are derived from the loaded
  SDP bundle** rather than composed from a hardcoded constant. 0.2.0 did this
  for the four core metadata resources; this completes it for the SDP
  extension resources (`sdp_methods`, `sdp_observation_structures`,
  `sdp_observation_components`), so **every** URI in a written descriptor —
  profile, rules, and per-resource schemas — now comes from one validated
  bundle. The constant remains as the fallback for a bundle that predates the
  v0.2 extension resources, which is not dead code: such a bundle has no
  `sdp_methods` entry at all.

  Composing the URL gives the *same answer* as reading it out of the vendored
  bundle, so a test against that bundle cannot tell the two apart. The
  regression test moves the bundle's own URLs and asserts both writers follow
  them.

- **Semantic ranking is reproducible across input orders.** metasalmon 0.2.1
  gave nine ordering sites the full tie-break key set
  `(-score, source, ontology, label, iri)`, because score alone is not a total
  order and — with `seed_semantics=True` — the top-1 pick becomes a written IRI
  in `column_dictionary.csv`. Seven of those sites have a counterpart here and
  already carried the key set; this release pins the property with a test that
  permutes the input and asserts one fixed order.

  The *locale* half of metasalmon's fix is inapplicable here: Python's
  `sorted`/`sort_values` are codepoint-ordinal (PARITY.md row 3). The two
  remaining sites — `.apply_embedding_rerank()` and
  `.ms_merge_semantic_target_candidates()` — have no counterpart because this
  package has neither an embedding rerank stage nor a retry retrieval pass;
  recorded as PARITY.md row 39 rather than left as an implied "delivered".

### Parity evidence

Driving `.score_and_rank_terms()` over the same six tie-heavy candidates under
four input permutations returns **one fixed order on both sides**. The orders
are not the same order — R ranks `gcdfo` above `smn` where this package does
the reverse — which confirms PARITY.md row 32's pre-existing ranking-profile
gap **live rather than by reading**, and is why the test asserts the property
0.2.1 added and not an order this package is registered as not sharing.

## 0.2.0

**This release is a parity claim against metasalmon 0.2.0.** Under the mirror
contract a version number here asserts that this package delivers the
behaviour of the metasalmon release with the same number — not a calendar date
and not a partial port. metasalmon's 0.2.x releases are deliberately untagged
history, so everything below was built against the commit that made **0.2.0
current on `main`** (`3fd1618`), extracted read-only with `git archive` and
loaded with `pkgload::load_all()`. Every behavioural claim was checked by
**running both implementations over the same inputs** rather than by reading
the R source. **metasalmon adopted C collation at 0.2.0**, so the byte
equalities claimed here need no locale caveat — unlike the 0.1.7 and 0.1.8
claims, which were measured against era R and do. Deliberate differences are
registered in [PARITY.md](PARITY.md); rows 35–37 are new here, and rows 21, 25
and 30 carry corrections this milestone produced.

### Breaking changes

- **`read_salmon_datapackage()` types data resources from the column
  dictionary.** The dictionary is the sole type authority: a column it declares
  is converted, a column it does not declare stays text rather than being
  guessed, and that is what makes the write → read → write round trip lossless.
  A value that does not satisfy its declared `value_type` **keeps its exact raw
  token** rather than being silently accepted, rounded, clamped or made
  missing, and the mismatch is reported as a structured validation issue.
  Unparseable values, a fractional `integer`, an `integer` or `number` whose
  precision or magnitude a double cannot hold, and a `datetime` finer than the
  representation can carry are all detected by an actual round trip — token
  versus the shortest rendering of the value it produced — not by digit or
  exponent thresholds, which misclassify in both directions at the boundaries.
  New module `resource_types.py`.

  **Logged decision: `integer` reads as `float64`, not nullable `Int64`.**
  metasalmon reads both `integer` and `number` with `readr::col_double()`
  because `col_integer()` silently `NA`s past 2^31. `Int64` would be *exact*
  past 2^53 where a double is not — so an `Int64` column would have to accept
  `9007199254740993`, a token metasalmon reports as beyond exact numeric
  precision. The float keeps every mismatch verdict identical across the two
  implementations, and the raw token is preserved either way. PARITY.md row 35.

- **`write_salmon_datapackage(overwrite=True)` no longer empties the package
  directory.** It replaces only the files it owns — the `metadata/` SDP CSVs,
  the `data/` resources declared in `tables.csv` (including any a previous
  write declared and this one does not), `datapackage.json`, and the ownership
  sentinel — and preserves everything else: reviewed SSSOM mapping sets,
  ordered measurement decompositions, EML and EDH XML, `eml-mapping.yml`,
  review notes, the reproducibility manifest, and `publication/` artifacts. The
  read → edit → write loop silently deleted all of them. The new `prune=True`
  restores the previous behaviour and requires `overwrite=True`; `create_sdp()`
  gained the same argument.

- **`infer_value_type()` answers from the class, not from the values.** A
  `datetime64` column whose values happened to all be midnight used to infer
  `"date"`. This is public API here (PARITY.md row 5), so it is a behaviour
  change for callers. metasalmon 0.2.0 fixed the mirror image from the other
  side — its `Date`-before-`POSIXt` test meant `"datetime"` was never inferred
  at all and timestamps round-tripped as dates. A single midnight timestamp is
  a real instant, and a heuristic that erases its time component silently
  rewrites data on the round trip.

- **Newly written descriptors derive every URI from the loaded SDP bundle.**
  `profile`, `sdp.profile`, `sdp.rules`, `sdp.specVersion` and the per-resource
  metadata schema URLs now all come from one validated document rather than
  from constants in Python source, so an upstream identifier change is
  followable rather than fatal. The metadata resources in `datapackage.json`
  previously carried no `schema` and no `description` at all.

### New

- **A remote SDP schema loader**, `sdp_schema.load_sdp_schema()`, remote-first
  with the vendored bundle as fallback and a per-process cache, mirroring
  `.ms_load_sdp_schema()`. `source="remote"` aborts rather than silently using
  a stale bundle; `"auto"` warns once and falls back; `"vendored"` never
  reaches the network. Identity is **derived** from the loaded bundle and only
  checked for internal self-consistency — asserting it against a constant is
  what made an upstream `$id` migration unfollowable in metasalmon 0.1.x.

  **It is born pinned to the `sdp-0.2.0` tag**, not to `main`. metasalmon's own
  default names `main`, which was `sdp-0.2.0`-shaped when 0.2.1 shipped and is
  `sdp-0.3.0`-shaped now, so following it literally would fetch a bundle from a
  spec era this package does not implement. Overridable through
  `METASALMONPY_SDP_SCHEMA_BASE_URL` / `set_sdp_schema_base_url()`, and both the
  source and the base URL are read at **call** time. PARITY.md row 38.

- **`validate_salmon_datapackage()` returns real issues.** The `issues` frame
  was an unconditionally empty `DataFrame(columns=["message"])`; it now carries
  R's issue columns (`issue_type`, `table_id`, `column_name`, `value`,
  `message`) and reports every declared type the data did not satisfy.

- **`text_safety.redact_secrets()`**, mirroring `.ms_redact_secrets()`, applied
  where external text is **captured** rather than where it is displayed: a
  provider failure stored on the exported `semantic_llm_assessments` attribute
  is written to CSV, so a display-time redactor is already too late. Applied at
  the LLM assessment capture, the update check, and the schema loader's remote
  failure. `knb_publication._redact` stays separate for now — its output is
  pinned against R fixtures, and metasalmon collapses the two redactors at
  0.2.5.

### Fixes

- **Reviewed sidecars survive a rewrite** (the read → edit → write loop above).

- **`write_salmon_datapackage()` refuses to write through a symbolic link.**
  `Path.exists()` follows links, so a `data/` or `metadata/` replaced by one
  would make every managed child resolve outside the package and be deleted
  there. The package root, every managed path component, and the legacy
  root-level metadata shadows are all checked — and checked **before**
  `tables.csv` is parsed, because the managed-path inventory reads it. A root
  spelled with a trailing `..` is refused outright: `readlink(2)` resolves
  every component but the last, so `link/..` inspects `..` inside the target
  and the root then denotes an unrelated directory. `create_sdp()` replaces its
  own outputs rather than writing through them, because a hard-linked
  `README-review.txt` or `semantic_suggestions.csv` would otherwise truncate a
  shared inode outside the package.

- **Typed resource columns are written back canonically.** A float 100000.0
  would otherwise reach disk as `100000.0` and a logical as `True`, so a
  package read and written straight back would not reproduce its own bytes.

- Four descriptor divergences from metasalmon, **all found by driving both
  writers over the same package** and none of them deliberate: the field
  `title` was suppressed when it equalled the column name (R emits it whenever
  `column_label` is non-blank); `constraints` was emitted as
  `{"required": false}` for every column (R emits the block only for a required
  one); a single-column `primaryKey` was written as a one-element array (R
  writes the scalar); and the dataset **contact** contributor was missing
  entirely. A blank `temporal_start` also produced `"temporal": {"start": "",
  "end": ""}`, because `pd.notna("")` is true.

- **`column_dictionary.csv` renders logicals as `TRUE`/`FALSE`**, not Python's
  `True`/`False`, and `datapackage.json` ends with a newline as
  `jsonlite::write_json` does. With those two, **every file in a package this
  release writes is byte-identical to metasalmon's** for the same inputs — see
  below.

- A data resource a previous write declared and this one does not is removed,
  with a warning naming it. Retaining it would leave undeclared data that
  validation never looks at but a hand-made ZIP would carry.

### Parity evidence

An SDP written by metasalmon at `3fd1618`, read here and written straight back,
reproduces **every file byte-for-byte**: `data/obs.csv` (including `100000`,
`0.1`, `1234567890123456`, `TRUE`/`FALSE`, ISO dates and
`2024-01-31T10:00:00Z` datetimes), all four `metadata/` CSVs, and
`datapackage.json`. That package is committed as
`tests/data/resource_types/r-package/`. R then reads and validates the
Python-written copy with no issues, and the type mapping is exactly parallel —
`character/numeric/numeric/logical/Date/POSIXct` against
`str/float64/float64/boolean/date-object/datetime64`.

Token-level fidelity was measured the same way: **524 observations** across 166
tokens and six declared types (conversion verdict, canonical key, lossiness,
significant digits, decimal exponent) were compared against the R
implementation. All agree except two families, both pinned as tests rather than
papered over:

- the canonical *display* key for a token outside the double range (`1e309`,
  `1e-400`, `5e-324`, …), because `readr::parse_double()` clamps a runaway
  exponent to ±307 where `float()` saturates. Both sides still report the token
  as beyond exact numeric precision, so no validation verdict differs;
- the canonical key for a sub-microsecond instant **before** the epoch, where
  R's POSIXct carries −9.999930625781417e-07 for what Python represents exactly
  as −1e-06, so R appends its disambiguating `@` suffix and this package does
  not. Both sides accept the token.

The consequence of PARITY.md row 21 is now visible in a new place and is
recorded there: a literal `NA` in a **declared numeric** column is a value-type
mismatch here and was missing under era R, whose reader still took
`na = c("", "NA")`. This package agrees with metasalmon 0.2.4 onward.

### Internal

- `metadata.SDP_PROFILE_VERSION` resolves through the loader at access time
  rather than being a module constant evaluated at import — an import-time read
  would have turned `import metasalmonpy` into a network call.
- `tests/conftest.py` pins the suite to the vendored bundle, as metasalmon's
  suite pins `sdp_schema_source = "vendored"`. The gap that pin left in
  metasalmon — nothing ever exercised a *successful* remote fetch — is closed
  here with an injected fetcher.
- `observation_structures` renders typed values through R's `as.character()`
  semantics before comparing them against raw `codes.csv` text; `str(2019.0)`
  is `"2019.0"` and matches nothing.

## 0.1.8

**This release is a parity claim against metasalmon 0.1.8.** Under the mirror
contract a version number here asserts that this package delivers the
behaviour of the metasalmon release with the same number — not a calendar date
and not a partial port. Everything below was built against metasalmon at the
**v0.1.8 tag**, extracted read-only and loaded with `pkgload::load_all()`, and
every behavioural claim was checked by **running both implementations over the
same inputs** rather than by reading the R source. The committed fixtures under
`tests/data/sdp-extensions/` and `tests/data/knb/r/` are unmodified R v0.1.8
output, generated under `LC_COLLATE=C`. Deliberate differences are registered
in [PARITY.md](PARITY.md); rows 29–32 are new here, and rows 17, 18, 20, 22,
23, 25, 26 and 27 carry corrections found while re-verifying the register
against metasalmon 0.3.0.

### What the differential runs showed

Measured, not asserted. For the same inputs:

- `metadata/methods.csv`, `observation_structures.csv`,
  `observation_components.csv` and the updated `datapackage.json` written here
  are **byte-identical** to R's, including canonical ordering — Python rewrote
  R's own files from reversed row order and reproduced the bytes exactly.
- `reproducibility/manifest.json` is **byte-identical apart from the two
  provenance values** that name the writer (PARITY.md row 29).
- `extract_sdp_observations()` returns the same structures, in the same order,
  with the same columns, rows and dictionary-derived types as R.
- An **expanded** KNB dry run over an SDP carrying a reproducibility manifest,
  a methods registry and observation structures plans the same 20 objects in
  the same order, with **every PID and every object checksum identical to R's**
  except the resource map, whose bytes differ only in XML formatting and which
  is `ET.canonicalize`-equal (PARITY.md rows 4 and 18). The EML document is
  `ET.canonicalize`-equal to R's, including which registry methods it asserts
  and which it omits.
- `apply_semantic_suggestions()` returns R's exact value for all three
  strategies, including the semicolon-joined multiple constraints.

### Added

- **SDP procedure registry, read and validate.** `read_sdp_methods()` and
  `validate_sdp_methods()` read the optional `metadata/methods.csv` with its
  exact closed schema, canonical `(dataset_id, method_iri)` ordering, absolute
  IRIs, per-dataset uniqueness, static `column_dictionary.method_iri` coverage
  and `datapackage.json` inventory. **`write_sdp_methods()` is deliberately
  not implemented** and raises with the reason: SDP 0.3.0 removes the registry
  from the specification, so a writer would exist only to be deleted in the
  same replay (PARITY.md row 9).
- **Measure-specific observation structures.**
  `read_sdp_observation_structures()`, `write_sdp_observation_structures()` and
  `validate_sdp_observation_structures()` handle the paired
  `metadata/structure/observation_*.csv` resources. Validation enforces
  complete one-structure-per-measure coverage, required dimension grain, typed
  repeated-value invariance, static and row-varying procedure resolution
  (including enumerated codes that no current row uses), and the canonical
  descriptor inventory — and is unchanged when the extension is absent. The two
  CSVs and the descriptor are staged and installed as **one rollback-capable
  transaction**, then re-validated from the bytes on disk.
- **`extract_sdp_observations()`** produces one deterministic normalized table
  per declared measure-specific structure, cast through the dictionary's
  `value_type`, without claiming RDF Data Cube conformance.
- **Reproducibility manifests.** `read_sdp_reproducibility_manifest()`,
  `write_sdp_reproducibility_manifest()` and
  `validate_sdp_reproducibility_manifest()` bind an explicit inventory of
  reviewed selections, workflow, provenance and source records to exact paths,
  media types, sizes and SHA-256 digests in `reproducibility/manifest.json`.
  The writer never discovers files, and validation is **closed over the exact
  directory contents**, so an editor backup or a private note cannot reach a
  public repository.
- **`apply_semantic_suggestions(strategy="reviewed")`** applies explicit
  accepted review decisions. Reviewed and LLM-reviewed selections now preserve
  **multiple constraints for one measurement** as a deduplicated,
  first-occurrence-ordered, semicolon-separated `constraint_iri`; lexical
  `"top"` and all non-constraint roles stay single-winner.
- **Expanded KNB publication.** `publish_sdp_to_knb(representation="expanded")`
  deposits the closed SDP inventory as individually named, EML-documented
  DataONE objects with package-relative PROV-O `atLocation` statements, instead
  of a ZIP. It includes validated SSSOM, decomposition, methods,
  observation-structure and reproducibility artifacts, and can reconstruct the
  exact SDP hierarchy without publishing unrelated files. `"archive"` remains
  the default.
- **EML method steps.** `write_eml_from_sdp()` documents the procedures
  actually used by observed measurements — with method and protocol IRIs,
  versions, descriptions and citations — and returns both the complete registry
  (`methods`) and the asserted subset (`used_methods`). Unused registry
  alternatives are **not** asserted as performed, and a method annotated on a
  non-measurement column is not a measurement procedure.
- **The vendored SDP schema bundle**, taken verbatim from the upstream
  `sdp-0.2.0` **tag** (not `main`, which is 0.3.0-shaped and no longer carries
  `methods.schema.json`). `metadata.SDP_PROFILE_VERSION` is now read from it,
  discharging the retirement condition the 0.1.7 constant recorded.

### Changed

- The reviewed semantic-selection ledger defaults to the extended
  `reproducibility/` layout, with the legacy root-level path retained as a
  compatibility route for already-reviewed packages.
- Supplementary EML objects may be non-ZIP artifacts named by a safe relative
  path; only `application/zip` objects declare `compressionMethod`, and
  `entity_type` distinguishes an expanded artifact from an archive.
- Generated SDP descriptors and the vendored bundle use the canonical
  `salmon-data-mobilization.github.io/smn-data-pkg` publication URLs. These are
  values stamped into output; nothing fetches them.
- The reviewed QUDT-to-EML unit crosswalk covers both HTTP and HTTPS forms of
  QUDT `Individual` (`INDIV`) and `Count`.

### Fixed

- **KNB publication artifacts were published as `0600`.**
  `_atomic_write_raw()` inlined `tempfile.mkstemp` + `os.replace`, both of
  which preserve mkstemp's owner-only mode, so the recovery manifest, the
  resource map and the SDP archive were unreadable to collaborators and to a
  web server. It now routes through `atomic_io`, the module written to prevent
  exactly this (PARITY.md row 24). Every publication write goes through that
  one function; a regression test asserts the resulting mode matches a plain
  write under two umasks, and fails on a reverted build.
- **A missing `match_type` no longer discards every candidate.** The optional
  provider field now contributes to ranking and an absent one scores as
  unclassified, matching `.match_type_score_profiled()` on every probed value.
  Several providers never populate it.
- **The bundled demo dictionary no longer asserts a nonexistent IRI.** Organism
  counts use QUDT `Individual` as their unit and the released Salmon Domain
  Ontology `smn:Abundance` as their property. The former `property_iri`, QUDT
  `NumberOfOrganisms`, does not exist — and a counting unit is not a substitute
  for the ecological property being measured. This demo is copied by users and
  fed to LLMs as context, so a wrong IRI here propagates.

### Dependency boundary

- **The deterministic SDP archive builds on core dependencies again.** The
  reviewed-ledger *binding* assertions moved from the artifact-inventory helper
  into the publication preflight (PARITY.md row 34). Keeping them in the helper
  — where R keeps them, because `yaml` is a hard Import for metasalmon — made
  `_write_sdp_archive`, a pure pandas + `zipfile` path, require the `[eml]`
  extra through a three-call chain with no import statement recording it.
  Behaviour is unchanged for every reachable caller and the assertions now fire
  earlier, before any archive is written. `KnbCoreDependencyTests` blocks
  `yaml` from `sys.meta_path` so the property is enforced in developer
  environments too, not only in the core-deps CI job.

### Register corrections

Re-verifying PARITY.md rows 16–28 against metasalmon 0.3.0 found seven claims
that were wrong or stale. They are corrected in place, and one is a live
interop hazard rather than a documentation nit:

- **Row 22 was reassuring about a divergence that is now real.** The era-NA
  split does change EML audit verdicts against metasalmon ≥ 0.2.4: R accepts a
  literal `NA` cell that this package's audit rejects. Now stated as live, with
  the milestone that closes it.
- Row 17: R allowlists two reviewed `zip` versions, it does not pin 3.0.1. The
  same stale claim was repeated in `knb_archive.py` and a test comment.
- Row 18: the ORE and SystemMetadata documents are `ET.canonicalize`-equal, not
  byte-equal; the manifest and fingerprint payload genuinely are bytes.
- Row 20: metasalmon adopted C collation at **0.2.0**, not 0.3.0.
- Row 23: the `\t"z"\t` trim boundary was asserted but unverified. Measured and
  pinned by a regression test.
- Row 25: reclassified from Idiom to **Gap** — the text read converged at R
  0.2.0, the typing did not.
- Row 26: 21 probe values, not twenty; and percent-encoding is a measured
  divergence rather than an agreeing case, now pinned.
- Row 27: the two implementations stamp different spec versions (R 0.3.0
  declares `sdp-0.3.0`).

## 0.1.7

**This release is a parity claim against metasalmon 0.1.7.** Under the mirror
contract a version number here asserts that this package delivers the
behaviour of the metasalmon release with the same number — not a calendar
date and not a partial port. Everything below was built against metasalmon at
the **v0.1.7 tag**, extracted read-only and loaded with `pkgload::load_all()`,
and every behavioural claim was checked by running both implementations over
the same inputs rather than by reading the R source. Deliberate differences
are registered in [PARITY.md](PARITY.md) rows 10-28.

The milestone landed as five chunks: SSSOM 1.1, measurement decompositions,
reviewed EML 2.2.0 export, KNB/DataONE publication with the deterministic SDP
archive, and this final chunk of era inference corrections. Their notes follow
below.

### Final chunk — era SDP-inference corrections

Mirrors metasalmon 0.1.7's "Corrected SDP inference and semantic matching
defects found while exercising the package on the PSC Fraser Sockeye detailed
release" entry, item by item.

- **Terminal ID qualifiers no longer misclassify quality fields.** A name whose
  last `id`/`key` token is followed by a qualifier token (`quality`,
  `confidence`, `accuracy`, `grade`, `score`) describes the quality of an
  identification, so `id_quality` is an attribute — or a categorical, when the
  column is a factor. `infer_column_role()` is now a node-for-node port of the
  0.1.7 function, including the name tokenizer, the identifier-ish
  (`station_number`) and sample-size heuristics, the method/protocol lane, the
  unit-bearing-header hint and the year-like value check that Python never had.
  Over thirty name/value pairs Python answered thirteen differently from R
  before this change and none after.
- **Nullable identifiers are not made required.** An identifier column carrying
  a missing or blank-after-trim value is left undecided rather than declared
  required — declaring it required makes the package fail its own validation.
  The old name-based fallback that marked anything ID-shaped required is gone,
  as it is in R.
- **A primary key must be able to be one.** `infer_table_metadata_from_resources()`
  now names the first ID-shaped column that is complete *and* unique, instead
  of the first ID-shaped column outright.
- **Profile versions follow the vendored rules.** A blank `spec_version` is
  filled from `metadata.SDP_PROFILE_VERSION` rather than the frozen
  `sdp-0.1.0` literal, and `datapackage.json`'s `sdp.specVersion` reads the
  same constant (PARITY.md row 27).
- **Custom HTTP(S) rights URLs remain URL licence descriptors.** The licence
  field now produces R's descriptor — the named OGL/CC-BY/MIT entries, a
  `{path: …}` descriptor for a canonical HTTP(S) URL, and an error for
  anything else — instead of wrapping any string as `{name: …}` (PARITY.md
  row 26).
- **Biology-bearing query tokens are retained.** `smolt`, `fry` and `juvenile`
  join the organism vocabulary, the count-like test matches R's four-way rule,
  and the whole-variable query keeps the life stage: `recruit abundance`,
  `smolt abundance`, `fry abundance`, and `effective female spawner abundance`
  for the effective-female and eggs-not-spawned shapes. Over eighty role
  queries driven from era R, Python now matches on every count-like case.
- **OWL-class metadata is preserved.** `apply_semantic_suggestions()` writes
  `term_type` alongside `term_iri`, taking the candidate's native ontology type
  (`owl_class`, `owl_object_property`, `skos_concept`) instead of stamping
  every whole-variable term `skos_concept`. Nothing wrote `term_type` here
  before, which made a Python-produced dictionary unexportable by the EML
  writer's `term_type` check. `unit_label` is filled from the accepted unit
  candidate in the same pass, as in R.
- **QUDT serves the property role**, searching `qudt:QuantityKind` rather than
  `qudt:Unit` and reporting `match_type = "quantity_kind"`;
  `sources_for_role("property")` gains `qudt` in R's exact position.
- **An explicit source list is an allowlist on the way out as well as in.**
  Results are filtered to the declared sources, so an injected `search_fn`
  cannot widen a deliberately bounded source set.

### Two reader/validator defects closed

- **SSSOM reference validation uses TRE's character classes, not Python's
  `\s`.** `sssom.R` writes `[[:space:]]` in five validators and calls `grepl()`
  without `perl = TRUE`, so TRE resolves them: U+00A0, U+0085, U+2007, U+202F
  and U+001C are *not* whitespace to R, and Python's `\S` was rejecting
  `author_id` values R accepts. The enumerated memberships now live once in
  `metadata` and are shared with `eml`, which had already been fixed
  (PARITY.md row 28). `measurement_decompositions` is deliberately untouched:
  R passes `perl = TRUE` there, and PCRE's `[[:space:]]` really is ASCII-only.
- **Data resources go through the shared SDP reader.** `read_salmon_datapackage()`
  and the bundled Darwin Core field table were the last bare `pd.read_csv()`
  calls, applying pandas' whole default NA vocabulary (`null`, `N/A`, `nan`,
  `<NA>`, `None`, …) and skipping readr's `trim_ws`. A gear code of `null` was
  destroyed on read, and a whitespace-padded header survived into the parsed
  frame — where it then passed the EML raw-token audit that R aborts on.
  Resources are now text-typed, which is where R goes at 0.2.0 (PARITY.md
  rows 21 and 25).

### Deterministic SDP archive

Contract parity with `R/knb-sdp-archive.R` at v0.1.7, re-verified for this
release: identical closed inventory and radix member order, identical
`file_name`/`dataset_id`/`format_id`/`media_type`, identical fail-closed
behaviour (reserved publication paths, absolute paths and dot segments
refused; symlinked members refused; a missing required artifact refused;
output confined to `publication/` with a `.zip` extension; an existing archive
with different bytes needs `overwrite`), and a stable idempotent rewrite.
Bytes are **not** comparable to R's — miniz and zlib emit different deflate
streams — so this module defines its own determinism reference
(`metasalmonpy-zipfile-1`) whose sha256 is pinned in `tests/data/knb/expected.json`
(PARITY.md rows 4, 17, 18).

### Earlier 0.1.7 chunks

- Fixed a batch of parity defects found by adversarial review of the 0.1.7
  chunks, each reproduced against metasalmon at the v0.1.7 tag.
  - **One CSV reader for the whole package.** `metadata.read_sdp_csv()` now
    backs every metadata, dictionary, vocabulary and reviewed-sidecar read.
    It mirrors readr's `trim_ws = TRUE` (headers and fields, inside quotes as
    well as outside, applied *before* the missing token is matched), which
    pandas does not do at all — a dictionary written as
    `demo-salmon-2026, counts, count, ...` validated in R and failed here. It
    also preserves a literal `"NA"` everywhere, where the EML and
    decomposition readers used to destroy it while `package_io` kept it; that
    asymmetry let a dictionary `constraint_iri` of `NA` demand a
    `semantic_vocabulary.csv` row that the vocabulary reader made impossible
    to write. Preserving `"NA"` follows metasalmon 0.2.4 ("NA" is a real
    fisheries gear code) rather than the 0.1.7-era `na = c("", "NA")`.
  - **Undeclared EML missing tokens are decided on the parsed value.** R
    derives missingness from the frame readr parsed with `trim_ws = TRUE`, so
    a cell of three spaces, or of `" NA "`, is an undeclared non-empty missing
    token. Matching the raw untrimmed token accepted data R refuses.
  - **SSSOM multi-valued references drop one trailing empty piece**, matching
    `strsplit(value, "|", fixed = TRUE)`. `author_id = "psc:PSC-CV-000900|"`
    read fine in R and its written SDP then failed validation here. Leading
    and interior empty pieces are still refused, as in R.
  - **Written artifacts use the umask default** (0644 typically) instead of
    the 0600 `tempfile.mkstemp()` hard-codes and `os.replace` preserves. This
    affected every SSSOM mapping set and manifest, the measurement-
    decomposition CSV and manifest, and reviewed EML — all published as
    private-to-owner files. Shared in the new `atomic_io` module.
  - **`overwrite` is a strict boolean**, mirroring R's `isTRUE()`;
    `overwrite="no"` replaced a differing EML document.
  - **`_as_numeric` screens C `strtod`'s grammar** before delegating to
    Python. `float()` accepted `"1_000"` as 1000 (PEP 515) and non-ASCII
    digits, both of which R rejects, and rejected `"0x1A"`, which R reads as
    26. The underscore direction turned a thousands-separated typo into a
    validated observation.
  - **The EML mapping sidecar parses like `yaml::read_yaml`.** Duplicate map
    keys are refused (PyYAML silently kept the last), and timestamps stay
    verbatim strings, so an unquoted `publication_date: 2026-01-01` — which R
    accepts — no longer fails the sidecar's JSON-Schema string check. Merge
    keys still resolve.
  - **`[[:cntrl:]]` and `[[:space:]]` mirror TRE, not ASCII.** metasalmon
    calls `grepl()` without `perl = TRUE`, so both classes are Unicode-aware:
    U+0085 in an entity name and U+3000 in a PID are rejected by R and were
    accepted here, while U+00A0 is whitespace to neither. The exact
    memberships were enumerated by running `grepl()` over every codepoint.
  - New coverage for the supplementary-object (`otherEntity`) path, which had
    none, using the previously unused R-generated `eml-supplementary.xml` and
    `expected.json["supplementary"]` fixtures.
- Ported metasalmon 0.1.7's KNB/DataONE publication (S10 milestone 0.1.7,
  chunk 4): `publish_sdp_to_knb()` in `knb_publication.py` plus the
  deterministic SDP archive in `knb_archive.py`, mirroring
  `R/knb-publication.R` and `R/knb-sdp-archive.R` at the v0.1.7 tag. That is
  the era shape only: one named SDP archive (`representation = "archive"`)
  with the legacy `sdp_artifact` role still read, aggregated and audited;
  the expanded representations, archive overwrite semantics and upload retry
  hardening added in later releases are deliberately not imported. The
  credential-free, network-free dry run plans the closed object set, builds
  and validates the OAI-ORE resource map, and writes the schema-version-3
  recovery manifest; live publication additionally requires an explicit
  `confirm=True` over a pre-existing exact reviewed manifest, confirmed
  redistribution rights for public deposits, a server-verified DataONE
  subject matching the EML metadata-provider ORCID, per-object byte,
  SystemMetadata and independent-checksum readback, anonymous-denial proof
  for private deposits, and a fresh catalog graph check before any status
  reaches `complete`. The adapter boundary is exactly the fourteen v0.1.7
  methods, injectable via `set_knb_adapter()`; the default implementation
  speaks the DataONE v2 REST API with `requests` rather than
  `dataone`/`datapack` (PARITY.md entry 16). Identifiers, the plan
  fingerprint payload and its SHA-256, and the manifest JSON bytes are
  byte-exact with R, and the ORE and SystemMetadata documents are
  `ET.canonicalize`-equal to R's; the SDP ZIP and EML documents are
  contract-level only, with the consequences disclosed in PARITY.md entries
  17-18. Fixtures in `tests/data/knb/` were generated by running metasalmon
  at v0.1.7; no test touches the network. Live publication needs the new
  `metasalmonpy[knb]` extra.
- Ported metasalmon 0.1.7's reviewed EML 2.2.0 export (S10 milestone 0.1.7,
  chunk 3): `write_eml_from_sdp()` in `eml.py`, mirroring `R/eml-export.R` at
  the v0.1.7 tag — the mapping-sidecar contract and its required-field
  errors, deterministic document construction, UUIDv5 package/series/object
  identifiers, and XSD validation. The builder is stdlib ElementTree; lxml
  (libxml2, the engine behind `emld::eml_validate`) and PyYAML live in the
  optional `metasalmonpy[eml]` extra behind an up-front gate mirroring R's
  `requireNamespace("emld")`. The EML 2.2.0 schema set is vendored from emld
  0.5.3 with its LICENSE. Parity is structural (`ET.canonicalize`) because
  libxml2 and ElementTree format differently, but every identifier matches R
  exactly; fixtures in `tests/data/eml/` were generated by running metasalmon
  at v0.1.7. See PARITY.md rows 14-15.
- Ported metasalmon 0.1.7's ordered measurement-decomposition artifacts (S10
  milestone 0.1.7, chunk 2): `read_sdp_measurement_decompositions()`,
  `write_sdp_measurement_decompositions()`, and
  `validate_sdp_measurement_decompositions()` in
  `measurement_decompositions.py`, mirroring `R/measurement-decompositions.R`
  at the era shape (identical at the v0.1.7 and v0.1.8 tags). The closed
  16-column schema, era role vocabulary — including the transitional
  `method` role that 0.3.0 later replaces with `statistical_modifier` —
  matched/gap state rules, contiguous per-measurement component order,
  semantic-component uniqueness, `value_of_dimension` relations, dictionary
  slot closure (semicolon-separated constraints checked separately), strict
  byte contract (UTF-8, no BOM, LF-only, trailing LF), symlink refusal, and
  the SHA-256-bound `measurement-decompositions.json` manifest all match R
  0.1.7. The canonical CSV serializer is byte-identical to R's
  (`tests/data/decompositions/` fixtures are generated by the R
  implementation at the v0.1.8 tag and asserted by sha256); manifest
  provenance honestly names this writer and validation accepts SDPs written
  by either mirror (PARITY.md entry 12).
- Ported metasalmon 0.1.7's strict SSSOM 1.1 support (S10 milestone 0.1.7,
  chunk 1): `read_sssom_mapping_set()`, `write_sdp_sssom()`, and
  `validate_sdp_sssom()` in `sssom.py`, mirroring `R/sssom.R`. Reviewed
  mapping sets are read from embedded-TSV with a strict byte contract
  (UTF-8, no BOM, LF-only, trailing LF), complete CURIE declarations, the
  alignment-only profile (no decomposition columns, no literal
  assignments), and version-scoped `sssom:NoTermFound` gap records; writes
  are canonical, deterministic, atomic, overwrite-safe, and bound to
  `metadata/semantic/mapping-sets.json` by SHA-256. The canonical
  serializer is byte-identical to R's (`tests/data/sssom/` fixtures are
  generated by the R implementation and asserted by sha256); manifest
  provenance honestly names this writer and validation accepts SDPs
  written by either mirror (PARITY.md entries 10-11).
- No version bump yet: the branch accumulates the 0.1.7 milestone; the
  parity claim moves to 0.1.7 only when the milestone completes.
- Replaced the stub `smn` and `gcdfo` term indexes with real implementations
  ported from metasalmon: Turtle parsing of the eleven SMN modules
  (`term_search_smn.py`), RDF/XML parsing of the gcdfo ontology, I-ADOPT role
  flags and pipe-joined `role_hints` (including the `statistical_modifier`
  hint, carried forward from metasalmon 0.3.0 so the flag never has to be
  retrofitted), and the shared 16-column index contract. `find_terms()` now
  returns real candidates for the `smn` and `gcdfo` sources instead of
  silently finding nothing.
- A failed or empty ontology fetch now raises instead of returning a silently
  empty index — a failed lookup is not an empty lookup.
- No version bump: this restores the existing 0.1.6 parity claim rather than
  making a new one (roadmap stream S10, PR 0).

## 0.1.6
- Aligned core user-facing behavior with metasalmon 0.1.6.
- Added the canonical `create_sdp()` workflow, safe package ownership and
  overwrite handling, `metadata/` plus `data/` layout, strict package
  validation, explicit update checks, and reviewed-package EDH rebuilds.
- Added opt-in bundle-aware semantic review with a stable 30-column assessment
  schema, role-aware defaults, strict explicit source allowlists, one bounded
  retry round, exact duplicate retry suppression, context-file parsing, and
  non-destructive provider fallback.
- Added deterministic method, constraint, role/type, dimensional, and curated
  redundancy validators. Only variable, property, entity, and unit decisions
  can be auto-prefilled, and all inferred IRIs retain the `REVIEW:` marker.
- Added structured term-gap detection from deterministic and LLM evidence,
  GCDFO request routing and template rendering, explicit submission
  confirmation, and resumable measurement-column `chat_decomposition()`.
- Updated CI to run offline without provider credentials and refreshed the
  canonical R/Python package round-trip test.
- Added a Quarto and quartodoc documentation site with workflow guides,
  grouped API reference, offline pull-request builds, and GitHub Pages
  deployment from `main`.

## 0.1.3
- Updated compatibility to align local helpers with metasalmon 0.0.13.
- Added canonical SDP CSV read/write, from-data artifact inference, semantic suggestion application, NuSEDS method crosswalks, EDH XML export, DwC-DP descriptor alias, and ontology term-request helpers.
- Fixed editable package metadata so `pip install -e .` exposes `metasalmonpy` and CLI helper modules.

## 0.1.2
- Renamed the GitHub CSV helpers to generic names: `github_raw_url()` and `read_github_csv()` (`repo` is now required unless a full URL is provided).
- Updated compatibility to align with metasalmon 0.0.5.

## 0.1.0
- Initial alignment with metasalmon 0.0.3: dictionary inference/validation, term search (OLS/NVS/BioPortal), semantics suggestions, SDP package IO.
- Added round-trip tests against metasalmon, SDP validator CLI, new term helper script, and optional term search caching (`SALMONPY_CACHE=1`).
