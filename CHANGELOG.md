# Changelog

## Unreleased

**Work that landed after the `0.5.0` number moved, and the reason it is not
under that heading.** `## 0.5.0` below is the section hub queue item **B-153**
closed when it set `__version__` to `0.5.0`, and the commit that made that
version current is B-153's merge, `67fb486` (#33). On 2026-09-16 `v0.5.0` was
not tagged and no GitHub Release existed for it, so that section described a
number that had been *claimed* rather than a release that had shipped. Measured
2026-09-24, it has shipped: `v0.5.0` was tagged on `67fb486` that day and its
GitHub Release published. Anything merged after `67fb486` belongs here, because
filing it under `## 0.5.0` would make this file say a version contains a change
that the commit making the version current does not. metasalmon's `NEWS.md`
keeps the same shape with its *(development version)* heading, which is what
this heading mirrors. **The number does not move here**: it is a parity claim,
and moving it is a separate outward act.

### Fixed

* **`datapackage.json` and `metadata/dataset.csv` spell a typed instant the
  same way, in the form Brett ruled.** A `datetime`/`Timestamp` in
  `temporal_start` or `temporal_end` reached both files through two different
  renderers and they disagreed about everything. Measured before the fix,
  2026-09-16 on pandas 3.0.5 / Python 3.11.15, for
  `datetime(999, 6, 5, 13, 45, 30)` and `datetime(2024, 12, 31, 0, 0, 0)`:

  ```
  descriptor start : 0999-06-05T13:45:30    csv start : 999-06-05 13:45:30
  descriptor end   : 2024-12-31T00:00:00    csv end   : 2024-12-31
  ```

  All four cells differed — separator, zone marker, year padding, **and**
  whether an all-midnight column keeps its time at all. Both files now read
  `0999-06-05T13:45:30Z` and `2024-12-31T00:00:00Z`.

  The spelling is **ruled, not chosen**: Brett ruled it on **2026-09-14**, once
  for both implementations so that neither side's implementer picks one —
  readr's ISO instant form, the `T` separator and the `Z` zone marker.
  metasalmon adopted it the same day (hub **B-115**, metasalmon pull request
  #118); this is the mirror half, hub queue item **B-145**. It opens no new
  `PARITY.md` row — row **56** already owns this divergence and named this very
  ruling as its retirement condition, so it is amended in place.

  **Two thirds of the defect were pandas', not this package's, and that is why
  the CSV side had to move too.** A column of `datetime.datetime` becomes
  `datetime64[us]`, and `to_csv` renders it **column-wise**: the year is
  unpadded below 1000, the separator is a space, and the time is dropped
  entirely when every value in the column is midnight. One cell's bytes
  depending on the other rows in its column is exactly the shape `AGENTS.md`'s
  *one value, one rendering* contract names. `str()` of the same value and an
  object-dtype column pad and keep the time, so the module converged on the
  padded form everywhere it rendered text itself and nowhere pandas rendered
  for it.

  **`resource_types.iso_instant_text()` is now the single renderer**, and that
  is the substance rather than a tidy-up. The string `_iso_seconds(x) + "Z"`
  stood at **four** call sites across two modules — `render_resource_frame()`
  twice, `observation_structures._typed_character()` and
  `._normalize_typed_values()` — and **two of them wrote a different string
  from the other two for the same tz-aware instant**. Measured on the pre-fix
  tree for `datetime(2024, 12, 31, tzinfo=UTC-08:00)`: `render_resource_frame()`
  gave `2024-12-31T00:00:00Z`, a local wall clock wearing a `Z`, where
  `_typed_character()` gave the correct `2024-12-31T08:00:00Z` — which is also
  what `readr::write_csv()` writes for the same value (measured, R 4.3.3 /
  readr 2.2.0). Every suite stayed green throughout, because each test built
  its expectation with the same call it was testing. All four now call one
  function, which folds to UTC and pads the year by construction, so the
  tz-aware case **converges onto metasalmon** rather than merely becoming
  self-consistent. `tests/test_platform_determinism_guard.py` gains a call-site
  guard that fails on a fifth, in the shape of the `strftime` guard beside it.

  **A second, smaller defect was found by wiring the renderer up and is fixed
  here rather than deferred.** `pd.NaT` **subclasses** `datetime.datetime`, so
  `render_resource_frame()`'s object-column branch accepted a missing value and
  raised `ValueError: cannot convert float NaN to integer` — while its own
  `datetime64` branch two lines above guarded with `pd.isna`. One function, two
  branches, two answers. Measured on clean `main`: an object column holding one
  instant and one `NaT` raised; the `datetime64` column rendered
  `['2024-12-31T00:00:00Z', None]`. `readr::write_csv()` with metasalmon's own
  NA token writes a missing `POSIXct` as the **empty field** rather than
  aborting, so raising was a divergence from metasalmon too. `is_instant()` is
  now the shared test — `value is not pd.NaT` rather than `pd.isna`, because an
  object column may hold a list or an array whose `pd.isna` returns an array. It
  is fixed rather than filed because the crashing branch is a line this change
  already rewrites, and leaving one caller of the new single renderer crashing
  on a missing value while the other did not would re-create in miniature the
  inconsistency this entry is about.

  **One residual is deliberately left open and measured rather than decided.**
  Each implementation now agrees with itself; below year 1000 they do not agree
  with each other. Measured 2026-09-16 on one Linux container (R 4.3.3 / readr
  2.2.0, pandas 3.0.5), driving each writer over the same fixture: metasalmon
  writes `999-06-05T13:45:30Z`, this package writes `0999-06-05T13:45:30Z`, and
  for `2024-12-31T00:00:00Z` the two agree exactly. On macOS R 4.5.2 metasalmon
  wrote the padded form and would agree (cited from B-115, not re-measured
  here). Which year is correct is hub item **B-161** and is Brett's, because
  padding R's CSV side means reopening backlog #93 item 1 deliberately. Reached
  only from a caller-supplied typed instant, which neither implementation
  produces on its own.

* **`validate_salmon_datapackage()` now checks three things it had been
  claiming and not doing.** Ported from metasalmon pull request #111 (backlog
  **#49**), hub queue item **B-124**. This is R-shipped-first lag being closed,
  not a deliberate difference, so it opens no `PARITY.md` row.

  1. **A column the dictionary declares `required` must not ship missing
     values.** The flag was inferred, written to `column_dictionary.csv`,
     parsed back to boolean, exported as Frictionless `constraints.required`
     and read by nothing that compared it to the data — so a package could
     state a column is required and ship blanks in it. Only columns present in
     the data are checked; an absent one was already reported.
  2. **A schema-required metadata field must not be blank.** The Frictionless
     schemas have carried `constraints.required` since the schema bundle
     landed, `review_metadata()` reports a blank one as blocking strict
     validation, and strict validation let it through — the placeholder scan
     only sees a field that *says* it is missing, not one that is. A blank
     **key** field is structural in every mode, because a row without its key
     cannot be addressed; a blank **non-key** required field takes the
     placeholder channel, warning by default and erroring under
     `require_iris=True`, so a freshly created package stays valid until the
     user asks for the strict answer. A column the file does not have counts as
     blank in every row, in all four metadata files.
  3. **A corrupt SSSOM mapping set or measurement decomposition is refused.**
     Both artifacts have had their own validator since they shipped and only
     the KNB publication and archive paths called them, so end-to-end
     validation reported success over a manifest whose SHA-256 no longer
     matched its bytes. Presence is detected by the managed file names and
     never by scanning `metadata/semantic/`, so an editor backup or an
     unapproved draft there stays local and unread.

  Each class has a failing-before test in `tests/test_validation_hardening.py`,
  and every expected message was measured by running metasalmon 0.5.0 over the
  same package directory on disk: for the two issue classes R and Python emit
  byte-identical messages, pluralisation included. The differential fixture
  `pk-missing-values` gains a second expected row, because the example's
  `POP_ID` is declared required — R reports the same pair.

* **`migrate_sdp_methods()`'s nothing-to-migrate report carries the same three
  columns as every other exit.** Its early return built
  `pd.DataFrame(columns=["table_id", "method_iri"])`, so
  `report["tables"]["columns"]` raised `KeyError` in exactly the case where the
  package was already clean — the branch least likely to be exercised — while
  the populated build and the no-placement return both named all three.
  Brett ruled the three-column shape on **2026-09-14, for both
  implementations**; hub queue item **B-144**, the mirror half of metasalmon
  backlog **#112** (hub **B-112**, metasalmon pull request #117).

  **This runs backwards, and the direction is the point.** This package carried
  the internally consistent three-column frame *first* and gave it up at S10
  chunk A (pull request 14, 2026-08-22) to mirror R's two-column early return —
  but R's other two exits had three columns all along, so what chunk A mirrored
  was an inconsistency rather than a shape. Under the amended mirror contract
  (Brett, 2026-08-17) which side is right is a ruling and not an implementer's
  call, so **R was the side that moved** and this restores what was here before
  chunk A. It is catch-up to a ruling rather than a chosen difference, so it
  opens no `PARITY.md` row; row **9** is amended in place instead, which is
  where that row's `1:1` claim lived.

  The comment above the frame moved with it. It read *"Two columns, not three:
  R's nothing-to-migrate report frame has no `columns` column … and the
  differential run showed it"* — an accurate report of what the differential
  saw and a wrong conclusion about what the shape should be, and a fix that left
  it standing would leave the next reader an explanation for a behaviour that no
  longer exists.

  Reproduced before the fix and pinned after it by
  `test_every_migration_exit_reports_the_same_three_table_columns`, which pins
  **all three** exits — the no-op early return, the populated build and the
  no-placement empty frame — rather than only the one that was wrong, because
  pinning one leaves the others free to drift away from it and the failure would
  look identical. Its R counterpart pins the same three. Measured by running
  both implementations rather than by reading either: on metasalmon `main`
  (`9eec204`) all three R exits return `table_id`, `method_iri`, `columns`, all
  `character`, with `report$tables$columns` empty rather than `NULL` at the
  no-op exit; here all three now return the same three names at `object` dtype,
  the type the populated build renders because it joins the bound column names
  into one string.

* **The test suite runs from a checkout at any path.** The repository root is
  the package, and pytest named it after the directory the checkout sits in, so
  the suite only worked when that directory was called `metasalmonpy`. Hub
  queue item **B-191**. Measured 2026-09-23 on `3f8349a`, with the package
  installed editable as CI installs it. From a directory named like a hub
  worktree (`salmon-data-mobilization-metasalmonpy-B-191`), pytest 8.4.2 and
  9.1.1 imported the root as a bare `__init__` module and **all 935 tests
  errored at setup**. pytest 7.4.4 was unaffected. From a directory named
  `checkout`, the suite went **green against a different tree**: pytest
  imported the root a second time under that name, and `import metasalmonpy`
  found whichever copy was installed. With nothing installed, only a directory
  named `metasalmonpy` loaded the suite at all. CI never saw any of it, because
  `actions/checkout` names the directory after the repository.

  `tests/conftest.py` now binds `metasalmonpy` to the checkout it lives in by
  file location. On pytest 8 and later it also collects the checkout root as a
  plain directory, so pytest never imports it under a name of its own.
  `tests/__init__.py` is gone, because it made pytest name every test module
  after the checkout directory. That turns the one relative sibling import, in
  `tests/test_validation_hardening.py`, into a top-level one.
  `tests/test_import_route_guard.py` runs a copy of the checkout under both
  names. It fails on the unfixed tree even from a directory named
  `metasalmonpy`, which is the view CI now has, and it fails when any one of
  the three measures above is removed. The wheel's file list is unchanged.
  This changes how the suite finds the package and nothing the package does,
  so it opens no `PARITY.md` row.

* **The documented `pip install -e ".[test]" && pytest -q` runs the whole
  suite.** Hub queue item **B-227**. `tests/test_validation.py` and
  `tests/test_term_deduplication.py` imported `validation` and
  `term_deduplication` by top-level name, which resolves only when the checkout
  root is on `sys.path`. `python -m pytest` from the root puts it there, which
  is why CI, which runs that form, never saw the problem. Bare `pytest` does
  not, and neither form does from inside `tests/`. Measured 2026-09-25 on
  `25dc7f3`, bare `pytest` from the root and either form from inside `tests/`
  stopped at two collection errors, *No module named 'validation'* and *No
  module named 'term_deduplication'*, and ran nothing. Both modules now
  import from `metasalmonpy.validation` and
  `metasalmonpy.term_deduplication`. Each form, from the root and from inside
  `tests/`, gives every test the outcome it had under `python -m pytest` from
  the root. A third job in `.github/workflows/parity.yml`, `bare-pytest`, runs
  the documented command on every pull request. It failed on the two imports
  before they changed, so the next top-level import fails a pull request
  rather than a contributor's first run. The two existing jobs are unchanged.
  This changes how two test modules import the package and nothing the
  package does, so it opens no `PARITY.md` row.

* **A measurement column whose values all look like years is no longer typed
  `temporal`.** Ported from metasalmon pull request #152 (backlog **#53**),
  hub queue item **B-240**, the mirror half of **B-53**. This is
  R-shipped-first lag being closed, not a deliberate difference, so it opens
  no `PARITY.md` row.

  `infer_column_role()` typed a column `temporal` whenever every value was a
  four-digit number from 1800 to 2500, reading the values alone and ahead of
  any measurement word in its name. So a small stock's `NATURAL_ADULT_SPAWNERS`
  of 1850, 2003 and 1999 became a `temporal` column. `suggest_semantics()`
  skips temporal columns, so the column left the whole semantic pipeline with
  no variable, property, entity or unit target and no warning, while the same
  column holding numbers outside that range was typed `measurement`.

  The year shape now decides unless the name's words include a measurement
  word (one the measurement check already reads: `count`, `total`, `spawners`,
  `escapement`, `weight`, `depth` and the rest) or a sample or partition size,
  and no date or time word. Then the year shape is not consulted, and the
  column is typed by the checks that follow exactly as it would be with values
  outside the year range. Words are split at whitespace, punctuation and case
  changes, so `Water depth(mm)` and `adult/count` are measurement names, while
  the year word in `Escapement (yr)` and `count/year`, or a plural one as in
  `escapement_years`, keeps them `temporal`, as `count_year` always was. Whole
  words rather than the broader measurement hint, because that hint's
  substring and unit patterns match names that are not measurements: `temp`
  inside `temporal_start`, and any parenthetical containing a `g`, such as
  `Cohort (Aug)`.

  **Not covered, as in R:** a name whose only measurement evidence is a
  substring (`ADULTCOUNT`) or a unit in parentheses (`Mass (kg)`) is still
  `temporal` when its values look like years. The words decide only whether the
  year shape may decide, and the role checks after it read the name as before,
  so a year-shaped `adult/spawners` or `fish/weight` is no longer `temporal` but
  not `measurement` either: it gets the role it gets with any other values.
  **Nor does it touch a `float64` column,** which never looks year-shaped here:
  the values are rendered through `str()`, and `str(1850.0)` is `"1850.0"`. R
  reads a double column of the same numbers as year-shaped. So a year column
  that pandas reads as `float64`, as it reads any integer column with a missing
  cell, is typed from its name alone here: `pd.read_csv()` on
  `BY` / `2001` / (blank) / `2003` gives an `attribute`, where
  `readr::read_csv()` on the same text gives R a `temporal` column. That was
  true before this change and still is.

  Pinned by `tests/test_year_shaped_measurement_role.py`, the port of
  `tests/testthat/test-year-shaped-measurement-role.R`. It checks each fixture
  against `_values_look_yearish()` before asserting its role, and follows one
  column through `infer_dictionary()` and `suggest_semantics()` to its semantic
  targets. Its fixtures are `int64`, nullable `Int64`, text and categorical,
  the storage types this package reads as year-shaped. Measured 2026-09-24 by
  running metasalmon `main` (`16976b1`) over the same 64 name and value pairs,
  R and this package now agree on every year-shape verdict and every role,
  where 18 of the 64 differed before. Over the 1,271 columns in the CSVs of
  metasalmon, metasalmonpy, smn-data-pkg and salmon-domain-ontology, read with
  `pd.read_csv()` defaults, no column's role changes.

* **The call `review_semantics()` prints for a measurement column's own slot
  now runs when the column has a code list.** Hub queue item **B-242**, the
  mirror half of metasalmon's **B-151** (metasalmon pull request #153). A
  measurement column's `entity_iri` and `constraint_iri` targets share their
  roles with the `codes.csv` targets of its codes, and an omitted `code_value`
  matches every code. So the column's own slot printed
  `accept_suggestion(review, "spawner_count", "entity", rank=1, table="spawners")`,
  which matched that slot and every code's slot and raised *"That column and
  role match more than one review slot"*; its `reject_suggestion()` line did
  the same. The refusal's list of arguments to add was no way out either,
  because the option it offered for the column's slot was the `table=` the call
  already carried. Measured through `create_sdp(semantic_code_scope="all")`
  with two codes on a count column, on `e595752`: 6 of the 33 printed calls
  raised.

  `review_semantics()` now prints `code_value=""` whenever `table` alone would
  not select the column's own slot, and the refusal offers it too. A blank
  `code_value` (`""`, `pd.NA` or `NaN`) selects the slots that belong to no
  code, while `None`, the default, still leaves it unconstrained, as R's
  `NULL` does. The matcher already read a blank that way, where metasalmon's
  raised, but it also matched a code slot whose `codes.csv` row leaves
  `code_value` empty because it supplies `vocabulary_iri`, which the codes
  schema allows.
  Whether a slot is a code's is now read from its file, so a blank never
  selects a code's slot. An omitted `code_value` still matches every code, and
  no earlier version printed a blank one, so every call an earlier version
  printed resolves as it did.

  One case is not fixed, in either implementation. A code slot whose
  `codes.csv` row has no code value has no call of its own that tells it apart
  from another slot of the same column, role and table, such as the column's
  own slot. Its printed call still refuses as ambiguous, as it did before, and
  never decides the other slot.

  Run through each package's `create_sdp()` on the same inputs, metasalmon
  `main` (`71a9199`) and this package now print the same arguments for every
  slot, resolve each printed call to the same slot, and offer the same options
  when they refuse. This removes
  `test_a_column_level_slot_sharing_a_role_with_its_codes_is_still_ambiguous`,
  the pin pull request #28 added with this defect as its retirement condition,
  and replaces it with mirrors of metasalmon's tests. It is R-shipped-first lag
  being closed, not a deliberate difference, so it opens no `PARITY.md` row.

* **`apply_salmon_dictionary()` names each code value it turns into a missing
  value.** Hub queue item **B-241**, the mirror half of metasalmon backlog
  **#55** (hub **B-55**, metasalmon pull request #154). A value that is present
  in a column and absent from the column's code list has no category, so it
  becomes missing, and until now it did that without a word. Under pandas 3 the
  only signal was pandas' own `Pandas4Warning` that building a Categorical from
  such a value "will raise in a future version". Under pandas 2.2 there was no
  signal at all. Each such value is now named in one `RuntimeWarning` per
  column, under either value of `strict`, because `strict` governs type
  coercion. Missing and blank values are not named. Past twenty values the list
  is shortened the way cli shortens R's (the first eighteen, an ellipsis, and
  the last two), and the count is always given in full.

  The value is blanked explicitly, before the Categorical is built, so the codes
  step no longer relies on the construction pandas deprecates. That includes a
  column that is already a Categorical, whose unused categories are dropped
  first. One consequence goes beyond the report. A code list pandas cannot
  build a Categorical from, because a `code_value` repeats or a hand-built one
  is missing, used to send the column to the fallback that keeps it as text,
  with every value kept, unlisted ones included. The unlisted values are now
  blanked on that path too, so the report is true there as well. For a repeated
  `code_value` that is what R does.

  **A code list now applies only to a text or Categorical column, as in R**,
  whose codes step runs only on a character or factor column. A column typed
  `integer`, `number`, `boolean` or `date` keeps its values. Matching those
  values against the text of `codes.csv` used to blank every one of them
  without a report, where R leaves the column as it is.

  **The other half of #55 owed a test and no fix.** metasalmon's
  `strict = TRUE` let through a value that its coercion only warns about.
  `_coerce_series()` has always raised on such a value, through
  `errors="raise"`, so R moved to where this package already was. Nothing pinned
  that behaviour, and `ApplyDictionaryFailureReportTests` in
  `tests/test_dictionary.py` now does, the twin of metasalmon's
  `tests/testthat/test-edge-cases.R`. This closes R-shipped-first lag and is not
  a deliberate difference, so it opens no `PARITY.md` row.

* **`accept_suggestion()` refuses an `iri` that is only the `REVIEW:`
  marker.** Hub queue item **B-220**, the mirror half of metasalmon's
  **B-219** (metasalmon pull request #165). It checked that `iri` was not empty
  before stripping the marker, so
  `accept_suggestion(review, column, role, iri="REVIEW:")` passed the check.
  So did every other spelling `_strip_review_iri()` removes: in any case, after
  leading spaces, tabs or newlines, and with whitespace after the colon. The
  accept recorded an IRI that named no term. Measured on `85ebbb0`,
  `apply_sdp_semantics()` then cleared the slot's `term_iri`, leaving its
  `term_type` in place, marked the retrieved candidate `not_selected`, and
  wrote an `accepted` row with an empty `iri` to `semantic_suggestions.csv`.
  The next `review_semantics()` does not queue that row, so the slot came back
  undecided. The check now reads the value after the strip, which is the value
  the decision records, and the refusal says that the marker was removed and
  nothing followed it. An `iri` with a term after the marker is accepted as
  before and recorded without the marker. `apply_sdp_semantics()` is
  unchanged.

  The two packages' strips still remove different spellings, so they still
  refuse different ones. metasalmon's also removes a space or a tab before the
  colon, as in `"REVIEW :"`, which this package records as the IRI verbatim,
  and this package's also removes some whitespace after the colon that
  metasalmon's leaves, such as a no-break space. Which spellings both should
  recognise is hub question **Q-63**, and this
  change settles none of them. This closes R-shipped-first lag and is not a
  deliberate difference, so it opens no `PARITY.md` row.

* **`detect_semantic_term_gaps()` no longer reports an ontology gap for a slot
  the reviewer has just filled by hand.** Ported from metasalmon pull request
  #146 (hub queue item **B-176**), as hub queue item **B-216**.
  `apply_sdp_semantics()` records a hand-picked accept, an
  `accept_suggestion(iri=...)` whose IRI no retrieved candidate carries, as a
  row of its own in `semantic_suggestions.csv`, with `source` `user`. The
  detector counted that row as retrieval evidence. Its blank `search_query`
  made it a target of its own whose only candidate was not `smn`. So when the
  post-review file was passed as `suggestions`, the output gained one more
  gap row than the pre-review file gave, carrying the hand-picked IRI as
  `top_non_smn_iri`. `render_ontology_term_request()` drafts a term request
  from a gap row and `submit_term_request_issues()` files it, so a false gap
  could have been sent to an ontology's maintainers.
  The detector now drops recorded rows before it derives anything from the
  table, embedded LLM assessments included, so the post-review file yields
  exactly the gap rows the pre-review file did, and a real non-`smn` gap on the
  same slot is still reported. A hand-picked IRI under `w3id.org/smn/` never
  showed the defect, because the detector counts that namespace as `smn`.
  `HandPickedAcceptGapTests` in `tests/test_term_requests.py` failed on the
  detector as it stood. R shipped this behaviour first and the difference was
  not deliberate, so the port opens no `PARITY.md` row.

* **`review_metadata()`, the four `set_sdp_*()` setters and the blank-required
  check in `validate_salmon_datapackage()` read the schema the settings
  select, and the metadata files are written in that schema's field order.**
  Hub queue item **B-215**, the port of metasalmon's **B-175**
  (metasalmon pull request #145). They read the bundled schema under every
  setting. So a schema selected with `set_sdp_schema_source()` or
  `set_sdp_schema_base_url()`, or with `METASALMONPY_SDP_SCHEMA_SOURCE` or
  `METASALMONPY_SDP_SCHEMA_BASE_URL`, reached the package writers and none of
  these. Measured on `fc5d16f` with a selected schema that adds one required
  `dataset.csv` field: the writers' field list included it, `review_metadata()`
  did not report it, `set_sdp_dataset()` refused it as "no such field", and the
  blank-required check did not name it. All four settings were silently
  ignored. Now they read the schema the settings select, as the writers do:
  from this process's schema cache once a writer has loaded it, and otherwise
  by loading it once.

  Under a selected schema, the metadata files are now also written the way
  metasalmon writes them. The setters, `write_salmon_datapackage()` and
  `apply_sdp_semantics()` all align each file to the fields that schema
  declares, in its order, with any other column after them. So a field the
  schema declares mid-list is written in place rather than last, and a setter
  call, a fresh write or an apply adds any declared column the file lacks, as
  an empty column. A rebuild or an apply therefore keeps the bytes a setter
  wrote.

  Under the shipped settings, no written byte changes, because the declared
  order is the order these files always had. `review_metadata()` and the
  setters still read the bundled copy and contact no network, as
  `review_metadata()` documents.

  One consequence follows the writers' loader. With
  `set_sdp_schema_source("remote")` and no network, `review_metadata()`, the
  setters, `validate_salmon_datapackage()` and `apply_sdp_semantics()` now
  raise `SdpSchemaError` where they used to read the bundled copy. This closes
  R-shipped-first lag and is not a deliberate difference, so it opens no
  `PARITY.md` row.

* **Naming a shortlisted candidate's IRI in `accept_suggestion(iri=...)` now
  writes that candidate's `term_type`.** Hub queue item **B-222**, the port of
  metasalmon's **B-221** (metasalmon pull request #166). The accept was
  recorded on the slot's first row. `apply_sdp_semantics()` takes `term_type`
  from the row a decision sits on only when that row carries the accepted IRI,
  and writes `skos_concept` otherwise. So hand-picking the IRI of a candidate
  below rank 1 wrote `skos_concept`, whatever that candidate was. The review
  rebuilt from the package replays the same decision on the candidate's own
  row, and re-applying it wrote the candidate's type, so one decision changed
  `column_dictionary.csv` and `datapackage.json` between two applies. Measured
  on `ed5e22e` with an `owl_class` candidate at rank 2: the first apply wrote
  `skos_concept` and the re-apply `owl_class`. An `iri` that a candidate in the
  review's shortlist carries, compared trimmed and without the `REVIEW:`
  marker, is now recorded on that candidate's row. It is the same decision as
  `rank=<its rank>`, and applying, rebuilding and re-applying it writes the
  same bytes.

  A candidate stored with the `REVIEW:` marker on its IRI now writes its own
  `term_type` too, by `iri=` and by `rank=` alike. The writer compared that
  stored IRI, marker and all, with the unmarked IRI the decision records, so it
  never recognised the candidate and wrote `skos_concept`. The selection and
  the writer now read a candidate's IRI through `_strip_review_iri()`, the
  rendering the decision record in `semantic_suggestions.csv` already matched
  rows by. metasalmon's fix also changed how its record reads that IRI, to
  trim it; the record here already did, so that part needs no port. An IRI
  that no candidate in the shortlist carries still writes `skos_concept`, as
  before. That includes hub item **B-176**'s case, ported here as **B-216**: a
  term the reviewer typed, whose type nothing records.

  The twins of metasalmon's tests in `tests/test_review_console.py` failed on
  the code as it stood: by `iri=` for the unmarked candidate, and by both
  `iri=` and `rank=` for the marked one. This closes R-shipped-first lag and is
  not a deliberate difference, so it opens no `PARITY.md` row.

* **`review_metadata()` reports a placeholder in an IRI field once, and the
  call it prints for that row runs.** Hub queue item **B-212**. A
  `MISSING METADATA:`, `MISSING DESCRIPTION:` or `REVIEW REQUIRED:`
  placeholder in `tables.csv`'s `observation_unit_iri`, or in a measurement
  column's `term_iri`, `property_iri`, `entity_iri` or `unit_iri`, came back
  as two rows. The scan's field loop reported it with reason `placeholder`, as
  it does a placeholder in any field. The check for a blank one of those
  fields then reported it again with reason `iri`, because its test counts a
  placeholder as unfilled. The `set_sdp_table()` or `set_sdp_column()` call
  printed for the row named the field twice, and Python refuses to compile a
  call that repeats a keyword argument. Measured on `70fa8fd`, with one
  placeholder planted in each of those two files: two rows for each field, and
  both printed calls failed with *keyword argument repeated*. Each field now
  comes back once, as a placeholder, and the console's count of fields still
  blocking strict validation counts it once. A blank IRI field is still
  reported once with reason `iri`, and so is one still carrying a `REVIEW:`
  marker.

  The scan now keeps one row per field of each metadata row, and the first
  check to report a field keeps it. So the same holds whichever two checks find
  one field. Measured on `70fa8fd` under a selected schema that calls
  `unit_iri` required, a blank one came back as `required` and as `iri`, and
  its call failed the same way; it now comes back once, as `required`. No
  shipped schema calls an IRI field required, and nothing in this package
  writes a placeholder into an IRI field, so only a hand-edited package reached
  either. `tests/test_sdp_field_setters.py` runs the printed calls for the
  placeholder, blank and `REVIEW:` states, and the placeholder state failed on
  the scan as it stood. metasalmon's scan has the same defect, which is hub
  item **B-211**. A defect the two packages share is not a deliberate
  difference, so this opens no `PARITY.md` row.

### Changed

* **The vendored SDP rules file carries the reworded SOSA Procedure rules.**
  Hub queue item **B-166**, the twin of the copy metasalmon made in its pull
  request #120. The change is Brett's ruling of 2026-09-14, landed upstream by
  hub item **B-106** as smn-data-pkg pull request 8. `data/schema/sdp.rules.yaml`
  is a byte-for-byte copy of smn-data-pkg `main`'s `schema/sdp.rules.yaml`, and
  so of metasalmon's `inst/extdata/schema/sdp.rules.yaml`. Measured 2026-09-25,
  all three are git blob `489d46a0` (md5 `f94d6c8f...`, 8506 bytes); this copy
  was md5 `3c702a37...`, the `sdp-0.3.0` tag's bytes. Nothing was hand-edited.

  `methods_are_sosa_procedures` and `row_varying_procedures_use_codes` now state
  **reachability**. A method or protocol IRI, and every `codes.csv` `term_iri`
  on a component bound with `sosa:usedProcedure`, is declared by a shared
  vocabulary and reaches a resource typed `sosa:Procedure` by a `skos:broader`
  path of zero or more steps, so a directly typed IRI passes. An asserted SKOS
  semantic relation whose other side is an `owl:Class` is refused by name, and
  estimate-type and data-quality vocabularies are never method vocabularies.
  The reasoning moved upstream to `docs/adr/0002-sosa-procedure-reachability.md`.
  The file's comments name that path, which resolves in smn-data-pkg and not in
  this package.

  **Nothing this package accepts, rejects or reports changes.** `sdp_schema`
  reads only the file's top-level `version:` and `profile:` scalars, which are
  unchanged, and no rule `id` or `severity` moved. The remote loader still pins
  the `sdp-0.3.0` tag, which serves the older wording, so the vendored rules
  file is now the bundle's one departure from that tag; `sdp_schema`'s module
  docstring records it and what retires it. This ports metasalmon's change
  rather than choosing a difference, so it opens no `PARITY.md` row.

* **`create_sdp()` no longer seeds a code list for a date column.** Hub queue
  item **B-188**. `pandas.read_csv` reads a column of ISO dates as text, and the
  code-row seeder, `metadata.code_list_values()`, accepted any `object` or
  string column. So on the bundled 30-row sample `START_DTT` and `END_DTT` each
  carried fourteen `codes.csv` rows while the dictionary typed them `temporal`,
  and the specification's validator (`scripts/validate_package.py` in
  smn-data-pkg) reports every such row as targeting "a non-categorical or
  unknown column". metasalmon cannot write those rows: `readr::read_csv()` reads
  the same column as a `Date`, and its seeder selects only character and factor
  columns.

  The seeder now applies R's guard the way `apply_salmon_dictionary()` does
  (hub B-241): a Categorical, a string column, or an `object` column of text. An
  `object` column of `datetime.date`, `datetime`, number or logical values
  seeds nothing, as a `Date`, `POSIXct`, numeric or logical column seeds
  nothing in R. Text that readr would read as a date or a date-time seeds
  nothing either. That means every present value has the date shape readr
  guesses (`2001-11-06`, `2001/11/06`), or every present value is a date-time
  that `readr::parse_datetime()` accepts. The boundary was measured against
  readr 2.2.0 and is pinned token by token in
  `tests/test_codes_target_categorical.py`. A Categorical still seeds whatever
  its values are, as a factor does in R. Text that readr reads as a time of day
  still seeds a code list. One case now differs from R. metasalmon's seeder,
  handed the same dates as a character vector rather than through readr, still
  lists them, because its guard reads the class alone. This package cannot tell
  that case apart, since `pandas.read_csv` gives it text either way.

  The role heuristic reads the same predicate (hub B-125). So a column of date
  text whose name has no time word is now typed `attribute`, where it was typed
  `categorical` and given a code list. On the bundled sample `create_sdp()` now
  seeds the same 129 rows over the same twelve columns as metasalmon, and
  `tests/test_codes_target_categorical.py` asserts R's plain condition, that no
  `codes.csv` row targets a non-categorical column, on every bundled example.
  The test that pinned the two date columns as a known residual is deleted.
  Apart from the one case above, this ports R's behaviour. That case is
  `PARITY.md` row **62**, and Brett ruled on 2026-09-25 that R moves to match
  (hub B-310).

## 0.5.0

**The `0.4.0 → 0.5.0` catch-up window is closed** (roadmap S5; hub queue
**B-126** for the behaviour, **B-153** for the documentation and this number).
It opened on 2026-08-25 when metasalmon released `v0.5.0`, and closing it took
two halves, because the number is a parity claim and metasalmon 0.5.0's own NEWS
entry leads with a documentation claim: that a package reaches
`validate_salmon_datapackage(require_iris=True)` **without opening a single file
in a spreadsheet**. The behavioural half landed first and deliberately left the
number at 0.4.0. Only now is that claim true of this package, so only now may
the number say so.

**The documentation half, in full, because the heading here described the gap
wrongly.** It said `guides/semantic-review.qmd` "still presents the spreadsheet
as the workflow rather than as the fallback". Measured 2026-09-16: the word
*spreadsheet* appeared nowhere in that file, or in any `.qmd` in this repository
— there was no spreadsheet workflow to demote. What the guide did was name
**none** of the nine functions, except one passing mention of
`apply_sdp_semantics()` inside the closure section, and neither accessor as a
call, while `_quarto.yml` had listed all eleven under *Review and edit (in
Python)* since the port landed. The guide is now built around
`create_sdp()` → `review_semantics()` → `accept_suggestion()` /
`reject_suggestion()` → `apply_sdp_semantics()` → `review_metadata()` →
`set_sdp_*()` → strict validation, showing the real printed output of each step,
with the spreadsheet named as the fallback and told why it is one. `index.qmd`,
`README.md` and `guides/parity.qmd` follow.

**`_quarto.yml`'s `quartodoc.version` was a fifth version place nothing
enumerated**, found still reading `0.4.0` while the other four moved. It moves
here, `AGENTS.md`'s list of places grows to five, and three new tests in
`tests/test_public_api.py` pin `uv.lock`, `_quarto.yml` and the guide's coverage
of the review surface against `__version__`, so the next bump cannot miss one
quietly. Two stale prose copies — `index.qmd` and `README.md`, both still
claiming parity with metasalmon **0.1.6** — were deleted rather than updated,
which is the better fix for a copy no test reads.

Tagging `v0.5.0` and publishing the GitHub Release are separate outward acts and
are not part of this change.

### Added

* **`write_sdp_semantic_closure()` produces the reviewed semantic closure**, the
  two files `write_eml_from_sdp()` and `publish_sdp_to_knb()` both require and
  neither wrote. Ports metasalmon backlog **#116** / hub **B-116** (hub queue
  **B-165**); the R half is metasalmon pull request #121. Until now a Python user
  reaching the publication gate had to hand-author
  `metadata/semantic_vocabulary.csv` and `reviewed_semantic_selections.csv`,
  **including a SHA-256 per row and two more in the sidecar**, with the digest
  helper private in `eml.py` and reachable only by importing past the API
  boundary. Neither file was mentioned in any user-facing doc.

  ```python
  closure = write_sdp_semantic_closure(pkg_path, evidence=judgements)
  closure["gaps"]        # term-gap shape, feeds render_ontology_term_request()
  closure["incomplete"]  # found, but short of a required evidence field
  ```

  - **Both canonical sets are derived, neither is reasoned from the other.** The
    measurement set includes a code-resolved `sosa:usedProcedure` and excludes a
    table's `observation_unit_iri`; the review-target set is the reverse. In the
    bundled fixture they differ by exactly one row.
  - **Evidence is resolved through this package's own `find_terms()`**, not a new
    retrieval path, and **no LLM is reachable from it** — pinned by a raising
    binding on the provider call.
  - **Gap, not abort**, the shape Brett ruled on 2026-09-12 for both
    implementations: an IRI every searched source answered about and none has
    becomes a `detect_semantic_term_gaps()`-shaped row and both files are still
    written. **And a gap is a claim, so only that one outcome makes one.** A
    lookup that *did not answer* — a search that raised, or an empty result whose
    `attrs["diagnostics"]` names a failed source — raises `RuntimeError` and
    writes nothing; a term *found* with a blank required field is reported in
    `incomplete` with a warning saying in as many words that it is not an
    ontology gap. Routing either through the gap table would ask an ontology to
    mint a term nobody established was missing.
  - **The degraded-source test is one copy.** `find_terms()`'s own warning and
    this producer both read `term_search._search_failed_sources()`, hoisted out
    of `find_terms()` here. A second copy would let one caller keep manufacturing
    gaps after the other stopped, and nothing in either copy would say which was
    current. `term_requests._namespace_scope()` was hoisted out of
    `render_ontology_term_request()` for the same reason.
  - **The two CSVs and the sidecar install as one link-refusing set**, through
    `sdp_methods._atomic_write_set()`, with both sidecar digests computed over
    the bytes *about to be installed* rather than by hashing files already on
    disk — which is what lets all three join one write set instead of leaving a
    replaced CSV beside its previous `sha256`. The sidecar is edited line-wise
    rather than round-tripped through a YAML dump, because a dump deletes the
    template's own instructions. Root, every intermediate component and each
    final entry are refused when symlinked; **hard links are closed structurally
    by the staged rename rather than detected**, because no portable check can
    see them, and the docstring says so rather than implying coverage.
  - **Verified against R, not asserted.** Both producers driven over the same
    package with the same injected search and the same evidence:
    `semantic_vocabulary.csv`, `reviewed_semantic_selections.csv` and
    `eml-mapping.yml` came out **byte-identical** in the ordinary case, the gap
    case and the incomplete case, and the degraded case aborts in both with the
    same IRI count and the same named source.

  `guides/semantic-review.qmd` gains an *Assemble the reviewed closure* section.
  **That is this item's documentation, not S5's** — the note above about the
  version staying at 0.4.0 still holds, because the guide still presents the
  spreadsheet as the workflow for the review itself (hub **B-153**).

* **The semantic review and the metadata editing no longer have to leave
  Python.** Nine new public functions plus two accessors, porting roadmap
  stream **S5** from metasalmon 0.5.0 (hub queue **B-126**). A salmon data
  package can now be taken from `create_sdp()` to
  `validate_salmon_datapackage(require_iris=True)` **without opening a single
  file in a spreadsheet**, and that whole sequence is asserted end to end in
  `tests/test_sdp_field_setters.py`.

  ```python
  review = review_semantics(pkg_path)
  review                                   # prints each slot and its shortlist
  review = accept_suggestion(review, "spawner_count", "variable", rank=1)
  apply_sdp_semantics(pkg_path, review)
  review_metadata(pkg_path)                # prints the set_sdp_*() call per gap
  set_sdp_dataset(pkg_path, creator="Fisheries and Oceans Canada")
  ```

  - `review_semantics()` builds a re-runnable queue from suggestions that
    already exist, in `review_console.py`. **It never contacts a network or an
    LLM**; a `find_terms` binding that raises is the sentinel that pins this.
  - `accept_suggestion()` / `reject_suggestion()` record a decision and return a
    **new** review, so a whole review is an ordinary re-runnable script.
    `reject_suggestion(reason=...)` records *why* no candidate fitted.
  - `apply_sdp_semantics()` is surgical and re-runnable, in `metadata_write.py`:
    it strips `REVIEW:` from decided fields, clears rejected ones, leaves
    undecided slots untouched, and **does not touch the data CSV bytes**.
    Applying the same review twice produces identical bytes. The metadata CSVs,
    `semantic_suggestions.csv` and the `datapackage.json` field entries are
    installed as **one** transactional set, because per-file atomicity is not
    enough when the rule that would catch a CSV/descriptor drift
    (`datapackage_consistent_with_csv_metadata`) is one of the dead rules in
    `sdp.rules.yaml`.
  - `review_metadata()` and the `set_sdp_dataset()` / `set_sdp_table()` /
    `set_sdp_column()` / `set_sdp_code()` setters, in `sdp_field_setters.py`,
    close what the semantic review structurally cannot see: **a slot with no
    candidates at all**, and free-text `MISSING …:` placeholders. It does not
    read a suggestion list — it reads the package against the rules that decide
    strict validation.
  - `semantic_suggestions()` / `semantic_llm_assessments()` are the supported
    way to read what was previously only reachable through `df.attrs[...]`, and
    `semantic_suggestions(path)` reads `semantic_suggestions.csv` back out of a
    written package, which nothing here did before.

  **The printed call is the contract, not decoration.** The console prints the
  exact call and the user pastes it; there is no prompt loop and no TUI, because
  an interactive prompt would make the decision as unreproducible as the
  spreadsheet it replaces. So the argument set is computed by *resolving* it —
  `table=` and `code_value=` appear only when they are needed to address one
  slot — and the tests `exec()` every printed line and assert it produces the
  decision it claims. A printed call that names a column the package does not
  have passes every substring assertion ever written.

  **The printed call is Python, not R.** R prints
  `review <- accept_suggestion(review, "x", "variable", rank = 1)`; this prints
  `review = accept_suggestion(review, "x", "variable", rank=1)`. Likewise R's
  `NA`-clears-a-field / `NULL`-means-not-passed pair is spelled `pd.NA` and
  `None` here. These are the mirror contract's "simple language differences that
  do not materially change behaviour" and are **not** registered as deviations:
  a Python package that printed R syntax would print a line its own user cannot
  paste, which is the one property the feature exists to have.

* **The SDP schema's `constraints.required` has a consumer.**
  `sdp_schema.sdp_schema_required_field_names()`,
  `sdp_schema_fields()` and `sdp_schema_field_description()`. The Frictionless
  metadata schemas have carried the constraint since the schema bundle landed
  and **nothing in this package read it**, so a field the spec calls required
  and one it calls optional were indistinguishable here and the only
  requirement checks were the hand-enumerated ones in `package_io`.
  `review_metadata()` is built on it.

* **The three descriptor builders are extracted and shared.**
  `package_io._descriptor_field_entry()`,
  `_descriptor_apply_resource_meta()` and `_descriptor_apply_dataset_meta()`
  were inline in `write_salmon_datapackage()`; the setters and the review
  write-back now call the same three, so "the surgical patch produces the shape
  a rebuild would" is true by construction rather than by a test that has to
  imagine every field. Measured anyway, over seven field/setter combinations,
  by `test_a_setter_patch_produces_the_descriptor_a_rebuild_would`. No
  descriptor bytes changed: the existing byte-level descriptor tests are
  unchanged and green.

* **The existing-empty-directory write is finally pinned.** This package has
  always written into an existing directory that contains nothing without
  `overwrite=True`, and **had no test for it** — which is half of why PARITY.md
  row 54 survived from before the 0.1.6 parity claim through four minor
  versions of green suites. `tests/test_current_workflow.py` now pins the write
  itself, the `create_sdp()` path, and a parametrized trio of near misses: a
  dot-file, a stale ownership sentinel, and an empty `data/` subdirectory each
  make the directory **non-empty** and still require `overwrite=True`.
  Emptiness is never recursive.

  Brett ruled on 2026-08-24 ("Go with the python implementation"), so **this
  package's behaviour is unchanged and metasalmon moved** to match it. Row 54
  is retired as converged in both registers. `write_salmon_datapackage()`'s
  docstring and `guides/faq.qmd` now state the rule instead of leaving it
  implicit.

### Fixed

* **An enumerable string column is typed `categorical`, not `attribute`.**
  Ported from metasalmon pull request #112 (backlog **#95**), ruled **Q29** on
  2026-09-05; hub queue item **B-125**. R-shipped-first lag, so it opens no
  `PARITY.md` row.

  A column that has a code list is categorical by the specification's own
  definition. `infer_codes_from_resources()` seeds one `codes.csv` row per
  distinct value of an enumerable string column, and the specification's
  `codes_required_for_categorical_columns` rule binds a code list to
  `column_role = "categorical"` — so `create_sdp()` was writing a dictionary
  row and code rows that contradicted each other in a single call, and
  `scripts/validate_package.py` in `smn-data-pkg` rejects every such row as
  "targets a non-categorical or unknown column". Measured on the bundled 30-row
  example: **twelve** columns were typed `attribute` while carrying seeded code
  rows — `AREA`, `POPULATION`, `SPECIES`, `RUN_TYPE`, `WATERBODY`,
  `WATERSHED_CDE`, `RELIABILITY`, `FULL_CU_IN`, `ENUMERATION_METHODS`,
  `ESTIMATE_METHOD`, `ESTIMATE_CLASSIFICATION`, `ESTIMATE_STAGE` — and now none
  is.

  The correction is in role inference with the seeder downstream of it, per the
  ruling, and the two now read **one** predicate:
  `metadata.values_form_code_list()`, built on `metadata.code_list_values()`,
  which *is* the seeder's criterion (a string or categorical column with 1 to
  `CODE_LIST_LIMIT` distinct non-missing values). Three branches of
  `infer_column_role()` answered `attribute` and now consult it — the
  identifier-qualifier branch, the method-token branch and the final default.
  The identifier, temporal and measurement checks deliberately still run first,
  so a key, a date, or a unit-bearing or percent-like text column keeps its role
  even when its values happen to repeat.

  A method-named column whose values enumerate (`ESTIMATE_METHOD`,
  `ENUMERATION_METHODS`) is a code list too, and its procedures resolve through
  `codes.csv$term_iri`; a free-text method note stays an attribute.

  Eleven rows of the `EraColumnRoleTests` differential fixture move with this.
  **Each moved in R first**: R and Python were run over the same thirty-one
  name/value pairs and agree on every row, so no expectation was edited to fit
  the Python change.

* **A reviewed semantic decision is no longer overruled by the unattended
  auto-apply heuristic.** `apply_semantic_suggestions(strategy="reviewed")` ran
  every accepted row through `_filter_auto_apply_suggestions()` at
  `semantics.py:1294` — the lexical compatibility gate that decides whether a
  *seeded* top-1 hit is safe to write into a dictionary nobody has looked at. On
  the reviewed path a human has read the definition and said yes, so the effect
  was that a regex silently overruled the decision and the caller was told only
  that some rows "did not meet the requested filters". The unattended `"top"`
  and `"llm"` paths keep the gate, which is the whole reason the gate exists.
  metasalmon backlog **#118**, which this package carried in the same shape.

  Reproduced before the fix and pinned after it by
  `ReviewedExemptFromAutoApplyGateTests`, whose fixture is the shape the defect
  actually takes: an `attribute` column whose accepted term label shares no
  token with the column name. Before: `term_iri` stayed blank. After: the
  accepted IRI is written, and `"top"` still drops it.

* **A review decision now survives being put down and picked up again.**
  `semantic_suggestions.csv` gains a **`decision_reason`** column, and
  `review_semantics()` replays the recorded `decision` on queue rebuild. Until
  now nothing read the `decision` column back: a reject *clears* the field, a
  cleared field reads as undecided, and so a reviewer who worked through
  sixteen slots, rejected four and came back the next day was asked the same
  four questions with no sign they had ever answered them. Decided slots are now
  kept out of the default queue and shown under `include_filled=True`.
  `review_metadata()` reads the reason back onto the gap the rejection left —
  otherwise that gap comes back looking exactly like one nobody ever considered.
  Applying a review also **preserves** decisions it does not itself carry, so
  Monday's four rejections are not erased by Tuesday's acceptance.

* **`review_metadata()` no longer reports a clean package that strict
  validation refuses.** An unresolved `REVIEW:` IRI is non-blank and is not one
  of the three `MISSING …` / `REVIEW REQUIRED:` prose spellings, so the gap scan
  passed straight over it while `_collect_review_iri_issues()` refused the
  package for it. The visible failure was `review_metadata()` printing "No
  outstanding metadata." for a package
  `validate_salmon_datapackage(path, require_iris=True)` then rejected — which
  breaks the one handoff the scan exists to provide, and is the state a user
  reaches by leaving any part of the semantic queue undecided. Every
  schema-declared `*_iri` field of `tables.csv`, `column_dictionary.csv` and
  `codes.csv` still carrying a marker is now reported, with the draft value shown
  and the console footer pointing at `review_semantics()`, which has the
  candidates. Such a slot now appears in **both** reviews; a duplicate report is
  the right answer where reporting it in neither was the defect.

  **Which files, exactly, is a measurement and not a reading** — the three gates
  that sweep the marker do not sweep the same files, so no single call site shows
  the answer. Measured 2026-09-16, one marked field per file:
  `validate_salmon_datapackage(require_iris=True)` refuses `tables.csv` and
  `column_dictionary.csv` and **passes** `codes.csv` and `dataset.csv`; the EDH
  XML gate refuses all three of the first but not `dataset.csv`. `codes.csv` is
  therefore reported although that validator does not refuse it, because
  `create_sdp()` tells the user every `REVIEW:` entry must be confirmed, the EDH
  gate refuses one and `read_salmon_datapackage()` warns about one — a scan that
  stayed silent would be the only voice in the package saying the marker is
  fine. `dataset.csv` is excluded because no gate refuses one there at all.
  `test_which_files_a_review_marker_actually_blocks` asserts all twelve
  file/gate answers, so a gate that changes which files it sweeps fails a test
  instead of drifting. That the validator passes a `codes.csv` marker at all is a
  defect in the **validator** and the same in metasalmon — `AGENTS.md` says
  strict validation fails if any `REVIEW:` marker remains — reported rather than
  fixed under this item.

  **Known gap, pinned rather than closed.** The scan reaches only the
  *schema-declared* fields, because every row it reports must print a runnable
  `set_sdp_*()` call and `_set_sdp_metadata()` refuses an undeclared field. An
  `*_iri` column hand-added to `tables.csv` is swept by
  `_collect_review_iri_issues()` and not by the scan, so that one case still
  reaches "No outstanding metadata." plus a refusing validator.
  `test_an_undeclared_iri_column_is_still_missed` asserts it, with its retirement
  condition in the docstring; no code path in this package writes such a column,
  so it is reachable only by hand-editing.

  `is_review_placeholder()` was **not** widened. It mirrors R's
  `.ms_is_review_placeholder()` and its narrowness is load-bearing for five other
  callers — the `license` gate reads it precisely *because* a bare `REVIEW:` IRI
  is not prose, and the placeholder sweep in `validate_salmon_datapackage()`
  excludes the marker because it has its own reporting path. The IRI-field test is
  a second predicate, `_is_unresolved_iri()`, and a test pins that the prose one
  still answers `False` for a `REVIEW:` IRI.

* **`review_metadata()` keeps its documented no-network promise.** The scan is
  built on the schema's `constraints.required`, and it read the schema through
  `load_sdp_schema()`, whose default `"auto"` source fetches six documents over
  HTTP before falling back to the copy bundled with the package. So on a fresh
  process a function documented as never contacting a network contacted one, and
  waited out a timeout per document when there was none. `sdp_schema_fields()`,
  `sdp_schema_field_names()`, `sdp_schema_required_field_names()` and
  `sdp_schema_field_description()` gain a `source` argument, and
  `sdp_field_setters` passes `source="vendored"` throughout — the setters too,
  because a call `review_metadata()` prints must be one `_set_sdp_metadata()`
  accepts, and it would not be if one read the remote schema and the other the
  bundled one. The vendored read goes straight to the bundled document rather
  than through `load_sdp_schema(source="vendored")`, whose cache is a single slot
  keyed by source: alternating sources in one process would evict the bundle each
  time and make the *next* `"auto"` read re-fetch.

  Proved with a sentinel rather than by reading, the way the R side proves LLM
  opt-in: an injected `requests.get` that raises. It derives from `BaseException`
  on purpose, because `load_sdp_schema()` catches `Exception` and falls back, so
  an ordinary raising stub would be swallowed and the test would pass either way.
  The test also un-pins `tests/conftest.py`'s suite-wide
  `sdp_schema_source="vendored"` and clears the caches — that pin is why nothing
  caught this, and it is a fact about the test environment rather than evidence
  about the default.

  A second guard, `test_the_offline_path_opens_no_socket_at_all`, blocks
  `socket.connect` / `connect_ex` / `create_connection` instead of `requests.get`
  and covers the console renderer and the four setters as well as the scan.
  Injecting `requests.get` guards the call the fetch makes *today*: a move to
  `requests.Session`, `urllib`, `httpx` or a subprocess would leave that sentinel
  green while every call reached the network. Both guards were demonstrated
  failing against the pre-fix tree, which is the only thing that distinguishes a
  guard from a comment.

* **A hand-picked accept now reaches the decision record.**
  `accept_suggestion(..., iri="...")` is the supported escape hatch for a term
  retrieval never surfaced, and a shortlist match was the only way an `accepted`
  row was ever written to `semantic_suggestions.csv`. So a hand-picked IRI left
  the mask empty: every candidate was marked `not_selected`, nothing was marked
  `accepted`, and the acceptance survived only in the user's script while the
  metadata CSV had it — the one decision the documented evidence trail lost, and
  the one `review_semantics(include_filled=True)` could not replay. The accepted
  IRI now gets its own row, carrying the slot's addressing columns and
  `source="user"`, rather than an existing candidate being relabelled: the
  shortlist rows are accurate as they stand — none of them was selected — and the
  thing with no row is the term the user supplied.

  The row is inserted at the **head** of its slot, not appended to the file.
  `review_semantics()` derives `rank` from file position and then drops everything
  past `max_candidates` (5 by default), so an appended record would rank 6 behind
  a full shortlist and be filtered straight back out — the same decision lost one
  layer further on. At the head it ranks 1, which is also what makes a replayed
  `accept_suggestion(..., rank=1)` re-accept the term that was actually chosen.
  Re-applying is still byte-identical: the second pass finds the row the first
  recorded and matches it.

  **All three of the fixes above are owed in metasalmon, and none of them is a
  `PARITY.md` row.** Each was measured against metasalmon 0.5.0 and found in the
  same shape, so they are ports rather than deliberate differences and the
  register would be the wrong place for them (hub queue **B-126** says so
  explicitly). Measured 2026-09-15 against metasalmon 0.5.0 under R 4.3.3:
  `.ms_is_unfilled_metadata()` (`R/sdp-field-setters.R`) answers `FALSE` for
  `REVIEW:https://…`, and R's `review_metadata()` prints "No outstanding
  metadata." for a package `validate_salmon_datapackage(require_iris = TRUE)`
  refuses; R's `review_metadata()` reaches `httr2::req_perform` on the default
  `metasalmon.sdp_schema_source` through `.ms_metadata_schema_fields()` →
  `.ms_load_sdp_schema(quiet = TRUE)`; and R's `apply_sdp_semantics()` writes the
  hand-picked IRI into `column_dictionary.csv` while leaving
  `semantic_suggestions.csv` with a lone `not_selected` row and replaying zero
  decisions on rebuild (`R/metadata-write.R`, the `accepted <- in_slot & …` mask).
  Until the R half lands, these three are temporary divergences that go the
  direction the mirror contract's "the mirror is not automatically the follower"
  clause allows: Python got it right here, and R changes.

* **An empty queue under a bad `columns` filter no longer reads as success.**
  `review_semantics(pkg, columns=["TYPO"])` would have reported an empty queue,
  telling a user who mistyped a column name that their package was finished. A
  `columns` value matching no column is an error that names the columns that do
  exist.

* **`prune=True` warns before it destroys recorded review decisions.** Pruning
  wipes the package directory, and `semantic_suggestions.csv` is not among the
  files a rewrite produces, so a user reaching for `prune` mid-review silently
  lost the audit trail. The prune still happens — a clean rebuild is a
  legitimate thing to want — but it now says what it is about to take, and only
  when there is a recorded decision to lose. *Retires when* the write path
  preserves `semantic_suggestions.csv` across a prune.

### Changed

* **The review checklist stops sending users to a spreadsheet.**
  `README-review.txt`, which `create_sdp()` writes into every package, told
  users to review the CSVs; it now hands over the Python calls in order, each of
  which prints the call for the next step. The spreadsheet path is still
  supported and still described — as the fallback, with the reason it is the
  fallback: a spreadsheet edit leaves no record of *why* a value was chosen.

* **`PARITY.md` row 31's final clause is amended in place**, not appended to. It
  closed with "verified identical to R's output for all three strategies", which
  was true when written and went false at metasalmon 0.5.0. It now records that
  the identity was broken at 0.5.0 and holds again as of this port,
  **re-measured** against metasalmon `main` @ `4cd085c` (`DESCRIPTION` 0.5.0):
  the three strategies over the `expected-apply-strategies.json` frame plus the
  #118 case under `reviewed` and `top` — five values, byte-identical in both
  implementations. A new row saying the two differ, sitting under an older row
  saying they were verified identical, would leave a reader to guess which
  sentence is current.

* **The `_quarto.yml` API reference gains a "Review and edit (in Python)"
  group** for the nine functions and the two accessors.

* **`create_sdp()`'s deterministic prefill now applies every role the evidence
  allows, and marks all of it.** `_auto_apply_package_suggestions()` passed
  `roles=["variable", "property", "entity", "unit"]` on both of its paths; it
  now passes `roles=None` on the deterministic (`"top"`) path, so
  `constraint_iri` and `statistical_modifier_iri` fill when — and only when —
  the column's own name, label or description carries the evidence for the
  qualifier or the aggregation. The gates that decide this
  (`semantics._measurement_suggestion_is_compatible()`) already existed and
  were already faithful mirrors of metasalmon's; the role filter sat in front
  of them and meant they never ran for these two slots.

  **Both new slots are marked `REVIEW:` like the four core ones**, and that is
  not a detail of the port. Review-visibility is the property Brett's ruling
  turned on — an unmarked constraint IRI is an unreviewed assertion in a slot
  the user never asked about — so a port that filled the same slots without
  marking them would have taken the behaviour and dropped its justification.

  Adopts metasalmon's behaviour on Brett's ruling of 2026-08-24 ("Yeah lets go
  the R way"), retiring PARITY.md row 57. **Two things the port found that the
  row did not describe.** First, metasalmon's "no role restriction" is true of
  its **deterministic path only**: its LLM path restricts to the same four core
  roles, so porting `roles=None` unconditionally would have widened this
  package *past* metasalmon and re-opened the divergence in the other
  direction. The split is now mirrored and pinned by
  `test_llm_auto_apply_still_refuses_the_two_qualifier_roles`. Second,
  **neither side pinned the positive case** — both suites asserted the slots
  stay empty when the gate rejects and neither asserted they ever fill, which
  is precisely how the divergence survived two green suites.

  `guides/faq.qmd`, `guides/semantic-review.qmd` and `guides/parity.qmd` all
  carried the "never auto-filled" claim, which was true of this package and
  false of metasalmon. All three are corrected in the same change, and each
  says so rather than quietly reading correctly.

### Registered

* **PARITY.md row 60** — `create_sdp()` refuses a doomed write *after* running
  inference here and *before* it in metasalmon. Measured, not read: a
  `create_sdp()` into an existing non-empty directory with `overwrite=False`
  runs `infer_salmon_datapackage_artifacts()` once here and zero times there.
  The outcome is identical on both sides, so no assertion about the result
  could ever have seen it; what differs is wasted work, and with
  `llm_assess=True` that work is billable. Found while implementing the row 54
  ruling and deliberately **not** fixed — direction is a ruling, not a
  drive-by. Raised as hub Q17.

* **PARITY.md row 61** — the `REVIEW:` marker is written without a trailing
  space here and with one in metasalmon. Every detector on both sides matches
  the no-space prefix, so each recognises the other and strict validation
  refuses both; the difference is inert to behaviour and visible only in bytes.
  Found by the row 57 differential, and it is invisible to every test either
  side has, because both build the expected string from their own prefix.
  Registered rather than converged: Brett was asked which slots a prefill may
  fill, not what the marker's bytes should be. Raised as hub Q18.

## 0.4.0

**The bump the S10 chunks were saving.** This release closes the
`0.2.2 → 0.4.0` catch-up window against metasalmon **v0.4.0**
(`4e2bbb6c2a7cc578cb05f9f350c834c89796142c`, verified against a pristine
`git archive` of that tag, never a working checkout).

The version number is a parity claim, so it is worth saying exactly what it now
claims and on what evidence. Every entry in metasalmon 0.4.0's `NEWS.md` was
audited against this tree before anything was written: 25 entries, each
resolved to present / absent / registered / not-applicable with the file and
line that proves it. The S10 chunks (A–H, merged 2026-08-22) had made this
package behaviourally complete through metasalmon **0.3.0** — but 0.4.0 is
0.3.0 *plus* the post-0.3.0 fix stream, and its headline feature landed in R
**after** every chunk was written. Bumping without porting it would have made
the claim false, which is the exact condition the release was cut to end.

Four gaps were found and ported; five differences were registered instead,
each with the condition that would retire it.

### New

* **`publish_sdp_to_knb()` and `write_eml_from_sdp()` take a
  `knb_environment`, so a deposit can finally be rehearsed** (roadmap S3, the
  mirror of metasalmon 0.4.0's headline). The member node, coordinating node,
  resolver, and Solr endpoints were module-level constants in
  `knb_publication.py` pinned to production — this is the first state those
  modules have ever had to vary. Two environments, matched exactly, with no
  partial matching, no custom endpoints, and no fallback between them:

  | Environment | DataONE network | Member node | Credential |
  |---|---|---|---|
  | `"test"` | `STAGING` | `urn:node:mnTestKNB` at `dev.nceas.ucsb.edu` | `set_dataone_test_token()` |
  | `"production"` | `PROD` | `urn:node:KNB` at `knb.ecoinformatics.org` | `set_dataone_token()` |

  Every value comes from the DataONE node documents themselves, read on
  2026-08-22 for the R implementation and mirrored here unchanged.

  **A dry run defaults to `"test"`; a live call has no default.** That is
  Brett's 2026-08-22 ruling — develop against the test node first, post to
  production once the package looks good there — and an unstated environment on
  a live call raises rather than silently targeting production. Live
  publication still demands `confirm=True` in both environments; naming an
  environment is not a substitute for approving the plan, and a rehearsal does
  not relax the gate.

  Three properties are structural rather than advisory, and each is
  revert-verified — the port was mutated four ways and each mutation was
  confirmed to fail the test that covers it:

  - **An environment switches whole.** Every derived URL is built in
    `knb_environments.py` from that environment's own member-node and
    coordinating-node base URLs, so no assignment in the package can pair a
    test node with a production Solr endpoint. Reads re-derive the whole record
    from the plan's fingerprinted `node_id` and refuse any plan whose network,
    node, and environment name disagree — a hand-edited manifest cannot smuggle
    a mixed pair through, and neither can the default REST adapter, which now
    refuses a mismatched network/node pair and any unregistered node.
  - **A test deposit cannot mint production identity.** The node identifier in
    every SystemMetadata and OAI-ORE artifact is the selected environment's
    own, and identifiers are scoped through `pid_preimage()` so a test PID can
    never collide with or be mistaken for a production one. The SDP archive is
    the sharpest case: its bytes are environment-independent, so without the
    scope the same package would mint the same archive identifier in both.
  - **The reviewed production EML is never replaced.** A test document has
    different bytes, so test artifacts go to `publication/test/` while
    production keeps `metadata/eml.xml` and `publication/knb-manifest.json`. A
    rehearsal is publication-writer output that the base package writer
    preserves, which is asserted by hashing the rehearsal before and after an
    ordinary `write_salmon_datapackage()` rewrite.

  Test deposits also request zero replicas, and a revision may not cross
  environments. **Production behaviour is unchanged**, and that is pinned
  rather than asserted: production identifier preimages are byte-identical to
  those minted before this change, verified against R's own v0.1.8 manifest
  fixture — every metadata and data PID, the package id and the series id.
  `publish_sdp_to_knb()` and `write_eml_from_sdp()` now also return the
  resolved `knb_environment`.

  A test plan's manifest carries `knb_environment` immediately after `node_id`,
  where R puts it. It is deliberately **not** fingerprinted, so every manifest
  written before this field still validates; and a plan that has no
  `knb_environment` omits the key rather than writing a null, mirroring R's
  list semantics exactly, so era manifests stay byte-identical on both sides.

  No live deposit has been performed in either environment; a test-node token
  is still outstanding.

### Fixed

* **`statistical_modifier` reached ranking with no source preference at all.**
  The seventh surface of the role contract, missing on *both* sides: the role
  has had `ontology-preferences.csv` rows since sdp-0.3.0 and
  `sources_for_role()` serves it from `smn` and `ols`, but `role_boost` had no
  entry, so it was scored on base weight alone — a 0.1-0.2 spread across
  sources, which is no source preference at all. It now carries
  `smn = 1.5, ols = 0.4`. Measured: the smn-over-ols margin on two otherwise
  identical candidates moves from **1.9 to 3.0** (`constraint`, the closest
  comparable role, is 3.8).

  **The guard that would have caught it is now able to.** `role_boost` and
  `base_source_weight` were dict literals inside `_score_and_rank_terms`, with
  nothing enumerable to assert against, and
  `tests/test_role_contract_guard.py` said so — it covered six of seven
  surfaces and named the seventh as out of reach. They are module constants
  now (`term_search.ROLE_BOOST`, `.BASE_SOURCE_WEIGHT`), and that guard checks
  what R's `test-smn-outranks-gcdfo.R` checks: every role with ranking
  preferences has a boost entry, and `smn` leads `gcdfo` wherever both are
  ranked. A second test pins the constants to the scorer that reads them, so
  hoisting them cannot drift into a second copy. This does not deliver a
  ranking-*profile* system (hub backlog #87); that tripwire stays.

* **The accepted manifest-writer set has one owner.** `provenance.py` now owns
  the pair of writer strings every manifest validator accepts, and
  `sssom.py`, `measurement_decompositions.py` and `reproducibility.py` resolve
  through it instead of each re-typing the pair. Re-typing is how the
  honest-provenance ruling got applied twice out of three times on the R side,
  leaving `validate_sdp_reproducibility_manifest()` rejecting Python-written
  manifests — and blocking KNB publication of any Python-written SDP, since
  publication validates the manifest while planning. A structural guard
  (`tests/test_provenance.py`) fails if any validator re-types a writer
  literal, reading the function body with the docstring stripped so a
  validator that merely *documents* its accepted writers still passes.

* **A reproducibility-manifest version that is not a string is refused.**
  `_validate_manifest` coerced the value with `str()`, so a JSON number,
  boolean or array passed as a version and the manifest claimed a version it
  did not have. metasalmon 0.4.0's NEWS says "both Python readers already"
  rejected those; that was true of `measurement_decompositions.py` and not of
  this one. Both now demand one non-blank string, through the shared
  `provenance.version_ok()`. The SSSOM validator deliberately keeps its weaker
  presence-only check, because metasalmon's asks the same weaker question and
  the two readers of one artifact must accept the same manifests.

* **A whitespace-only metadata scalar is absent from the descriptor, as it is
  in R.** Every scalar presence test in the descriptor builder now goes through
  `package_io._meta_scalar_present()`, which renders the value to text and
  **trims** it before deciding — the mirror of `.ms_meta_scalar_present()`.
  `_has_value` did neither, so a whitespace-only `primary_key` passed the
  presence test, split to nothing, and wrote `"primaryKey": []` into
  `datapackage.json` — a key R cannot produce and the SDP publication
  validator has no reading for. A whitespace-only scalar now produces a
  descriptor byte-identical to an empty one.

### Documentation

* **Docs that were wrong, not merely absent, are corrected.**
  `guides/semantic-review.qmd`, `guides/glossary.qmd`, `README.md` and
  `guides/parity.qmd` all still described a dictionary **method** slot that
  sdp-0.3.0 removed; the glossary named four I-ADOPT components instead of
  five. `guides/parity.qmd` was the worst of it — it claimed 0.1.6 parity
  against a 0.2.1 package and listed EML export, KNB publication, SSSOM,
  measurement decompositions and observation structures under "Not yet in
  Python", every one of which ships. Its "not yet" list now names what is
  actually missing, each item pointing at its `PARITY.md` row. The checked-in
  generated `reference/*.qmd` remain stale (regenerating them needs quartodoc
  and a `quarto` binary); the source docstrings they were generated from were
  already correct.

### Registered rather than ported

Five differences are recorded in [PARITY.md](PARITY.md) with retirement
conditions instead of being changed here, and rows 11, 12, 16, 28, 29 and 32
were updated where 0.4.0 moved what they describe:

* **Row 54** — `write_salmon_datapackage()` accepts an existing *empty*
  directory without `overwrite=True`; metasalmon refuses it. Pre-0.1.6 debt,
  carried forward silently since `e33526d`, pinned by neither side's tests. No
  0.4.0 entry calls for a change and which order is right is a ruling.
* **Row 55** — the datetime-observation-dimension defect this package fixed on
  2026-08-17, seven days before metasalmon released the same fix, having
  deliberately mirrored the rejection before that. The window in which the two
  sides knowingly differed had no register row at all.
* **Row 56** — a `POSIXct`/`Timestamp` metadata scalar renders differently in
  the descriptor and in the CSV, on both sides, in opposite directions. A
  unilateral change would match neither R's output nor this package's own CSV;
  the canonical lexical form for a metadata instant is a profile decision
  (hub backlog #93).
* **Row 57** — `create_sdp()`'s deterministic prefill is narrower here
  (`variable`, `property`, `entity`, `unit`); metasalmon 0.4.0 also applies a
  constraint or statistical modifier when the column text carries the
  evidence. Each side's prose matched its own code while the code diverged.
* **Row 58** — no counterparts to metasalmon 0.4.0's two new vignettes. A
  prose gap, not a capability gap.

Rows 54-58 are **one-sided** until the hub lands its twins; the register says
so in as many words, and `check-parity-registers.py` reports them.

### Verified, not assumed

* Both dependency legs, import-probe confirmed against the tree under test:
  **795 passed / 3 skipped** with `[test,eml,context]`, **682 / 116** with core
  dependencies only, with the core leg asserted to have no extra importable.
* Every fix above is revert-verified: the fix was undone and the covering test
  confirmed to fail, then restored.
* Production KNB output is pinned against R's own v0.1.8 manifest fixture, so
  "production is unchanged" is a cross-implementation measurement rather than
  an assertion about this package alone.

---

The S10 chunk work below shipped under `## Unreleased` and is part of this
release. It was left unversioned deliberately: the chunks delivered behaviour
verified against metasalmon's post-0.3.0 `main`, which no R release contained,
and nobody had ruled what a number could claim about that. metasalmon v0.4.0 is
that ruling.

### S10 chunk H — the abort-safe write path

`write_salmon_datapackage()` is now **transactional over the files it owns**.
The mirror of metasalmon PR #77 (hub backlog #96's ordering half), and the
defect it fixes was measured present here before the fix, not inherited on
faith.

- **An abort no longer destroys the caller's package.** The old path called
  `_prepare_package_dir()` first — unlinking every managed path, or
  `shutil.rmtree`-wiping the directory under `prune` — and only afterwards
  rendered resources, loaded the SDP schema, built the descriptor and wrote
  the metadata CSVs. Every one of those is an abort point, and an exception
  in that window left the package with its data CSVs and **nothing else**:
  `datapackage.json`, all of `metadata/`, and the ownership sentinel deleted
  with no replacement. Measured against the pre-fix code, an injected abort
  during the descriptor build reduced a valid package to a single
  `data/obs.csv`.
- **Render first, install atomically, clean up last.** The complete write set
  — data resources, metadata CSVs, `datapackage.json`, sentinel — is rendered
  to bytes before anything on disk is touched, then installed by
  `_commit_package_write()`, the single destructive step. Installation goes
  through `sdp_methods._atomic_write_set()`, the existing rollback-capable
  writer this package already mirrors from R, rather than a second one:
  same-directory staged siblings, originals renamed aside, rollback on any
  mid-install failure, umask-default modes. Stale managed paths this call did
  not rewrite are unlinked only **after** the install succeeds.
  `_prepare_package_dir()` is replaced by `_check_package_write_dir()`, which
  performs no deletion at all.
- **`prune=True` is honestly narrower rather than silently equal.** The wipe
  removes files this writer does not own, so those sidecars are not in the
  write set and nothing can restore them. The wipe now runs as late as
  possible — after every input-dependent computation and the full byte
  rendering have succeeded, so an input-triggered abort leaves the package and
  its sidecars intact — but a *pure filesystem* failure between the wipe and
  the install remains unrecoverable. Stated in the code and in the function's
  own docstring instead of being papered over. The symlink containment guard
  now also covers the prune wipe, which previously relied on the earlier
  metadata-subset check alone.
- **The bytes did not change.** The reorder is byte-neutral by construction —
  each file is rendered through its exact former writer call, deliberately not
  through a shared JSON or CSV helper that differs in separators or
  terminators. Verified rather than asserted: five packages (the shipped
  30-row example, an R-written fixture round-tripped, an in-place rewrite, a
  `prune=True` rewrite, and a no-codes/no-descriptor package) hash identically
  file-for-file before and after, and the example package remains
  byte-identical to metasalmon `main` @ `3b620ae` on all six shared files.
  Two guard tests pin the in-memory renders against direct `to_csv` and
  `json.dump` writes so a future pandas or stdlib change cannot drift them.
- **A structural guard, with a stated retirement condition.**
  `tests/test_write_datapackage_abort_safety.py` fails if the writer body
  regains a direct filesystem call, which is what would reopen the window
  while every trigger-specific test stayed green. It says what it covers and
  what would retire it; its scan surface differs from R's twin
  (PARITY.md row 52).
- `create_sdp()` writes the package through the same helper, so it inherits
  the fix; a regression test pins that. Its three **create-owned sidecars**
  (`README-review.txt`, `semantic_suggestions.csv`,
  `metadata/metadata-edh-hnap.xml`) are still unlink-then-rewrite — hub
  backlog #111's shape, measured present here and, for the EDH XML, on a
  *wider* window than R's. Registered as PARITY.md row 53, not fixed here.

### S10 chunk F — redaction

metasalmon 0.2.5's redaction contract, verified by driving both redactors
over a 31-string adversarial battery (credential headers, cookie jars,
serialized JSON credentials, qualified tokens, token-count diagnostics, JWTs,
provider keys, URLs with query-string keys, multi-line splits): **byte-identical
to `.ms_redact_secrets()` on metasalmon `main` @ `794647a`** after this chunk,
against three divergences before it.

- **Credential redaction covers qualified token names.** `_CREDENTIAL_NAME`
  enumerated `dataone[_-]?token`, so the production credential was redacted
  and `dataone_test_token` / `knb_staging_token` were not — the worst
  possible split, since staging is the credential a first-time user is most
  likely to paste into a script, and it leaked *at rest*: captured provider
  errors are stored on returned frames and written to CSV. The rule is now
  structural — any qualified name whose final segment is `token` — so a
  credential introduced later is covered without another patch. `token` must
  be the final segment, so `max_token_count` and `total_tokens` survive in
  provider diagnostics, and a name continuing past `token`
  (`dataone_token_v2`) is deliberately unmatched, matching R's recorded
  trade — that last verdict *changed* here, from redacted to left alone.
  The separator's whitespace class was also aligned to R's PCRE
  `[[:space:]]`, so a name split from its value by a newline redacts
  identically on both sides.
- **The second redaction implementation is deleted.**
  `knb_publication._redact` (mirror of the `.ms_knb_redact()` R 0.2.5
  deleted) and `text_safety.redact_secrets()` were two implementations of
  one security contract, and only one gets extended when a pattern changes —
  exactly how the gap above arose in R. KNB boundary messages (`_abort_safe`,
  the live-adapter warning wrapper, the DataONE REST error) now redact
  through the shared function, which is strictly stronger: it also catches
  `x-api-key`, provider API keys, and serialized JSON credential forms the
  deleted version missed. One observable output changed with the
  consolidation, exactly as it did in R at 0.2.5: a `Authorization: Bearer …`
  line is now redacted as a credential header (whole line), not just its
  Bearer payload. `tests/test_text_safety.py::test_exactly_one_redactor_exists`
  is the standing guard, and PARITY.md row 37's redaction half retires as
  converged. The era `expected.json` fixture's three `redact_*` helper
  values retire with the function they pinned.
- The capture-time URL-redaction sites chunk E placed in `_safe_json`
  inherit the structural rule automatically — the one-redactor property is
  what makes that sentence true, which is why 0.2.3's URL rule and this
  consolidation were scoped together.

### S10 chunk E — cache, environment and network robustness

metasalmon 0.2.2's and 0.2.3's cache/environment/network behaviours, verified
by **running both implementations over the same inputs** against metasalmon
`main` at `794647a` (2026-08-22; the R tree carrying every post-0.3.0 fix —
same convention as chunks A–C, execplan open decision 2 / hub Q7 pending):

- **The smn/gcdfo term indexes are resolved once per session** (0.2.2).
  `_smn_term_index()`/`_gcdfo_term_index()` had no cache at all, so every
  `find_terms()` call re-fetched and re-parsed both ontologies — the same
  cost R paid when its stamp check sat after the fetch. `refresh=True`
  bypasses and replaces the cache; a failed resolve caches nothing. The
  trade mirrors R's: a module updated upstream mid-session is not picked up
  until `refresh=True`, the stronger guarantee for seeding.
- **Environment switches are read at call time, under the package's own
  name.** `SALMONPY_CACHE` was read at import — the exact bug class R 0.2.2
  fixed for `METASALMON_CACHE`, where an installed package captured the
  importing process's environment once and the cache could never be enabled
  afterwards. The switch is now the call-time `METASALMONPY_CACHE`
  (truthiness verified value-for-value against R), and the stale `SALMONPY_`
  prefix is renamed to `METASALMONPY_` across the package
  (`METASALMONPY_DEBUG_FETCH`; test-only Qualark gates renamed cleanly).
  **Deprecation window, decided here:** the old `SALMONPY_*` spellings keep
  working with a once-per-process `DeprecationWarning` and are removed in
  the **first tagged release after the S10 parity release** (PARITY.md row
  50 carries the decision and its retirement condition).
- **A failed vocabulary lookup is no longer indistinguishable from a
  successful empty one** (0.2.2). `_safe_json` returned `None` for both, so
  a degraded OLS looked exactly like "no such term exists" — the input that
  drives `request_new_term` escalation, letting an outage manufacture
  ontology gaps. Failures are now signalled per source and recorded in the
  diagnostics frame as `status="http_error"` (columns now match R's,
  including `elapsed_secs`; a source returning rows past a failed side
  request is a partial answer, not a clean success), surfaced as a warning,
  and **a degraded lookup is never written to the result cache**. The curl
  fallback gained `--fail` so an HTTP error page served as valid JSON cannot
  masquerade as a successful lookup.
- **`publish_sdp_to_knb()` gains `overwrite`** (0.2.3). A dry run could not
  be re-planned after correcting an input: the SDP archive writer, the
  plan-mismatch check, and the resource-map ownership check each treated an
  existing artifact as a published one, and none of the messages said the
  way forward. `overwrite=True` rebuilds derived artifacts and replaces a
  manifest and resource map left by an *unpublished dry run*; eligibility is
  decided before the plan builder mutates anything, a manifest whose status
  is not `dry_run` still requires a reviewed revision, and live publication
  is still gated by `confirm`. Differential: the full corrected-input
  sequence over the same package matches R gate-for-gate, with byte-exact
  `package_id`/`series_id` across implementations.
- **The default LLM providers now retry** (0.2.3). There was no retry at all
  — a 429 or a 503 failed the whole review on the first try, after the user
  had already paid for every preceding request. All three provider call
  sites (generic, bundle, exploration; chat-decomposition stays direct,
  matching R) route through `_request_json_with_retries`: 3 total attempts
  (4 for openrouter `:free` and chapi `gpt-oss*`, matching
  `.ms_llm_retry_limit()`), `Retry-After` honoured in both delta-seconds and
  IMF-fixdate forms and capped at 60s, locale-independent date parsing,
  jittered exponential backoff otherwise, and R's retryable set — expressed
  over this library's error shapes (an attached `response.status_code` and
  the requests timeout/connection exception types, because requests spells
  "429 Client Error" where httr2 says "HTTP 429") plus R's message-pattern
  list verbatim for injected `request_fn` errors. A non-retryable error
  raises from the first attempt, so a sentinel `request_fn` still proves
  exactly one call.
- **The BioPortal API key travels in an `Authorization` header** instead of
  the query string, where it was written into request logs at both ends and
  would be quoted by any warning recording the URL (0.2.3). **Request URLs
  are additionally redacted at capture** in `_safe_json` — before they reach
  failure records, timeout warnings, or the diagnostics frame — through the
  shared `text_safety.redact_secrets()`. Placement note for the execplan's
  open question: the URL-redaction *call sites* land here in E because E
  rewrites `_safe_json`; the redaction *pattern* they call is strengthened
  to R 0.2.5's structural `*_token` rule by chunk F, and these sites inherit
  that automatically.

### S10 chunk D — validation hardening

`validate_salmon_datapackage()` becomes metasalmon's validator: the typed,
accumulate-then-report issue system, the 0.2.6 tidy checks, and the
placeholder fill converged on current R. Everything here was verified by
**running both implementations over the same fixture packages** against a
pristine `git archive` of metasalmon `main` at `9d8f125` (2026-08-22):
seventeen single-defect corruptions of the shipped example plus one stacked
five-issue package matched **field-for-field across all five issue columns,
message bytes included**, and two whole written packages (the shipped example
and a blank-metadata fill probe) came out **byte-identical file-for-file**,
`datapackage.json` included.

- **The typed issue collector (hub backlog #91 / PARITY.md row 41, closed).**
  Structural findings accumulate into R's five-column frame (`issue_type`,
  `table_id`, `column_name`, `value`, `message`) across all **eight** typed
  categories — `dataset`, `tables`, `dictionary`, `codes`, `resource`,
  `columns`, `primary_key`, `composite_intent` — and the validator aborts
  once with the full list (ten-message preview, R's wording), never at the
  first problem with an untyped string. The raised `ValueError` carries the
  frame as `.issues`. The `codes` and `composite_intent` categories are new
  here in full: code values are canonicalized through their declared type on
  both sides of the comparison, and the WSP composite-intent check (route
  hints in metadata or the descriptor vs populated `cu_timeseries` signal
  columns) is ported whole. **This lifts the standing prohibition on
  cross-implementation issue-count verification** recorded in the execplan
  and in row 41: both sides now report the same issue set for the same
  broken package, so milestone checks may compare counts and categories.
- **`value_type` declarations are enforced, not just reported** (the #98
  exposure): a value that fails its declared type — including `date`, where
  R rejected DD-MON-YY bytes and this package's validator returned normally
  with a side-channel frame — is now a structural `columns` issue and the
  call aborts. Previously `validate_salmon_datapackage()` could not fail for
  any value-type mismatch at all.
- **metasalmon 0.2.6's tidy checks.** A declared primary key must identify a
  row: missing values in key columns and duplicated key tuples are errors
  (`tables` issues), and a key naming absent columns is a `primary_key`
  issue. Column names that look like data values — bare year-like names, or
  a shared numeric-suffix stem, across three or more columns — warn without
  ever erroring, in both validation modes; the message points at
  `pandas.melt()` where R points at `tidyr::pivot_longer()` (PARITY.md
  row 49, the only deliberate difference in the check). Unresolved
  `MISSING METADATA:` placeholders are surfaced as a default-mode warning
  naming each `file$column`; strict mode keeps reporting them as errors.
- **The placeholder fill converges on R, at the byte level (PARITY.md
  row 48, closed).** Blank `creator`, `contact_name`, `contact_email` and
  `license` gain R's `MISSING METADATA:` guidance; blank `title` and
  `table_label` are titleized from their identifiers via a verbatim
  `tools::toTitleCase` port; dataset, table and column-description prose is
  R's exact wording. `infer_dataset_metadata_from_resources()` and
  `infer_table_metadata_from_resources()` return placeholder-filled frames
  as R's do (titleized labels, `data/`-prefixed file names).
- **Two descriptor defects found by the byte differential, fixed.** A blank
  dictionary `required` wrote `constraints: {"required": true}` — the
  inverted claim, on every unlabeled-required column including the shipped
  example's RUN_TYPE and ESTIMATE_STAGE rows — because `iterrows()` hands a
  boolean-dtype NA back as a truthy float `nan`. And a blank `column_label`
  omitted the `title` key where R emits an explicit `null`.
- **Strict-mode review reporting uses R's messages and composition.**
  Placeholder, blank observation-unit-IRI, REVIEW-prefixed-IRI and
  malformed-placement findings are collected with R's message texts and
  row contexts; `validate_dictionary()` gains R's REVIEW-marker handling
  (default-mode warning, strict abort with R's wording) and R's per-field
  strict message (`Measurement columns require term_iri; missing in rows
  8.`). Reading a package whose declared resource file is missing now warns
  (`Resource file '…' not found, skipping`), as R has always done, and the
  default mode warns when `validate_semantics()` reports issues, naming the
  first three.
- **The missing example round-trip test** (the #100 exposure):
  `tests/test_example_round_trip.py` builds an SDP from the shipped 30-row
  example and validates it in **both** modes, pinning strict validation to
  **zero** issues and lenient validation to silence, plus a well-formedness
  gate over every shipped metadata CSV. metasalmon's counterpart pins its
  fuller 173-row example to exactly one known strict failure; that example
  is not shipped here (PARITY.md row 46, open), so the tiny example's
  zero-issue pin is the whole gate.

### S10 chunk B — the semantic pipeline retarget

Retargets the semantic pipeline onto the dictionary shape chunk A introduced:
`statistical_modifier` becomes the sixth reviewed dictionary slot, and the
code-level `method` role survives for `codes.csv` searches only. Everything
here was verified by **running both implementations over the same inputs**
against metasalmon `main` at `9d8f125`; the differential matched on every
probe — sources per role, all three crosswalks value-for-value, the codes
prefills, target discovery, bundle payload shape, the prompt's judged-slot
sentence, and gap detection.

- **Measurement columns discover a `statistical_modifier` target** —
  deliberately conservative, only when the column text (name, label, or
  description, underscores split) names an aggregation, so plain measurements
  gain no slot; the emitted query is the canonical aggregation word
  (`total` > `mean` > `maximum` > `minimum` > `peak`, mirroring R's ladder).
  `apply_semantic_suggestions()` maps the role to `statistical_modifier_iri`
  and no longer accepts `method`; auto-apply stays limited to variable,
  property, entity, and unit, and a modifier auto-fill additionally requires
  the column itself to name an aggregation.
- **The ranking-preferences data gains the three `statistical_modifier`
  rows** (smn's StatisticalModifierScheme first, then I-ADOPT, then STATO) —
  `data/ontology-preferences.csv` is again byte-identical to metasalmon's —
  and `sources_for_role("statistical_modifier")` serves the role from
  `smn` + `ols`, exactly R's list.
- **The bundle review names every role**: the payload carries the six
  dictionary slots plus `method` (no dictionary field; always
  `already_filled_or_not_requested` for column bundles), and the system
  prompt's opening instruction judges exactly the dictionary slots — naming
  `statistical_modifier`, never `method`, which is described as
  usedProcedure-style context instead. **`SEM_MODIFIER_EVIDENCE_REQUIRED`**
  lands *beside* the surviving `SEM_METHOD_EVIDENCE_REQUIRED`, downgrading a
  modifier accept whose column text names no aggregation.
- **A role-contract guard that states its own limited scope**
  (`tests/test_role_contract_guard.py`): it pins six of metasalmon's seven
  role surfaces — the prompt, the slot/role maps, the hint emitters, the
  retrieval filters, the deterministic validators, and the ranking
  preferences — and says plainly that the seventh (`role_boost`) is
  unreachable here because this package has no ranking-profile system (hub
  backlog #87, PARITY.md row 32). A tripwire test fails the moment a profile
  system appears, so the guard is extended rather than silently narrower.
- **Gap detection sees zero-candidate targets** (hub backlog #97, the same
  defect metasalmon fixed in PR #75): `suggest_semantics()` now attaches its
  discovered targets as a `semantic_targets` attribute, and
  `detect_semantic_term_gaps()` reports any target with no retrieval evidence
  at all — no suggestion row before `min_score`, no assessment of any
  decision — as `gap_detection_basis = "no_candidates"`, distinguishing
  "nothing found" from "found and rejected". The distinction is per target,
  not per column, and the scope/role filters apply to no-candidate targets
  exactly as to suggestion-backed ones. The explicit-`suggestions` path keeps
  its historical row-in/row-out behaviour.
- **`nuseds_estimate_classification_crosswalk()`** maps the NuSEDS
  `ESTIMATE_CLASSIFICATION` strings onto the released gcdfo Hyatt (1997)
  types (`gcdfo:Type1`–`Type6`), records `NO SURVEY THIS YEAR` and `UNKNOWN`
  as deliberate non-mappings, and links the multi-year relative
  classifications at scheme level (hub backlog #101).
- **All three NuSEDS crosswalks are wired into the package path** — the
  shared prefill engine fills `codes.csv` `term_iri` during `create_sdp()` /
  `infer_salmon_datapackage_artifacts()` for estimate-method,
  estimate-classification, and enumeration-method code values (matching on
  the word `enumeration` alone, because `ENUMERATION_METHODS` is plural and a
  `\bmethod\b` test can never match it), never overwriting an explicit
  caller-supplied IRI. Until now **no** crosswalk was wired here at all,
  although metasalmon has wired the estimate one since its initial commit —
  a previously undocumented divergence, registered as **PARITY row 47** in
  this change (hub backlog #102).

#### Fixed in passing

- **Measurement constraint and entity queries are role-shaped, as R's have
  been since era 0.1.7**: constraint text naming `natural`/`hatchery` becomes
  an origin query, and a spawner measurement's entity query becomes
  `population` (or `stock`/`population` when a sibling column names one). The
  era port pinned only the variable/property shaping, so the raw description
  leaked into these two searches — an undocumented divergence the chunk-B
  differential caught, which also cascaded into gap detection (a candidate
  matching the description text turned a genuinely-empty target into a
  `candidate_gap`).
- The bundle payload-shape test no longer asserts inside the injected
  `request_fn`, where the bundle path's provider-failure handling swallows
  `AssertionError` and lets a drifted payload pass vacuously — observed in
  this chunk: the old six-slot assertion kept passing, green, against the
  new seven-role payload until the assertions moved outside the request.

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
