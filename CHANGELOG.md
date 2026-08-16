# Changelog

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
