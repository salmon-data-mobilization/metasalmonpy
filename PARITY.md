# Parity deviations — metasalmonpy vs metasalmon

The mirror contract (see `AGENTS.md`) requires the same behaviour and
capabilities as metasalmon at the version this package claims — **not**
literal API mimicry. This register lists every deliberate difference; its
twin lives in the hub at `metasalmon/knowledge/parity-deviations.md`, and the
two must agree. An undocumented difference is a contract violation even when
the difference itself is fine (Brett, 2026-08-15).

Three kinds of entries:

- **Idiom** — same behaviour, Pythonic delivery.
- **Ahead** — Python already has semantics R adopted later (or improved on);
  R converges or stays.
- **Inapplicable** — the R behaviour has no Python counterpart, with the why.

| # | Kind | Difference | Why |
|---|---|---|---|
| 1 | Idiom | Errors are Python exceptions with actionable messages, not R cli conditions | Language idiom; same conditions trigger them |
| 2 | Idiom | Optional dependencies are extras (`metasalmonpy[eml]`, `[knb]`), not Suggests | Packaging idiom; same lazy-guard behaviour and install pointers |
| 3 | Idiom | Canonical ordering uses codepoint `sorted()`, not explicit C-collation flags | Python's default sort is already locale-independent; the *contract* (locale-independent deterministic output) is identical, and `locale.strxfrm` is banned |
| 4 | Idiom | Archive/EML/ORE parity is contract-level (structure, manifest, ordering, fail-closed), never byte-level | R's bytes come from zip-3.0.1/libxml2 formatters Python cannot and should not reproduce |
| 5 | Ahead | `infer_value_type` is public API here, internal in R | Was already exported at 0.1.6; removing it would break users for no capability gain |
| 6 | Ahead | A failed or empty ontology fetch raises; 0.1.6-era R returned an empty index | Failed lookup ≠ empty lookup; R adopted the same principle at 0.2.2 |
| 7 | Ahead | `is_statistical_modifier` is a real column in both term-index frames; R's TTL path carries it only inside `role_hints` | Saves the 0.3.0 milestone a retrofit; hint strings match R exactly |
| 8 | Inapplicable | No interactive term-request console, so R 0.2.0's cancel-must-not-submit semantics have no counterpart | Submission here is a single explicit function call with confirmation |
| 9 | Planned (0.1.8) | `write_sdp_methods` will not be implemented; registry read/validate only | The writer would exist only to be deleted at 0.3.0 in the same replay; logged in the S10 execplan |
| 10 | Idiom | The SSSOM embedded metadata header is parsed with a restricted YAML-subset parser (plain/quoted scalars, one-level block mappings, block sequences); R parses it with the `yaml` package | Dependency policy is pandas+requests only; the subset is exactly what the canonical writer emits and the SDP profile uses. Headers outside the subset raise the same "not valid YAML" report R gives malformed YAML. Canonical mapping-set bytes are byte-identical to R's (`.ms_sssom_canonical_bytes` parity is tested against R-generated fixtures) |
| 11 | Idiom | `mapping-sets.json` provenance records `metasalmonpy.write_sdp_sssom` + `metasalmonpy_version`; `validate_sdp_sssom` accepts either implementation's provenance (R's validator accepts only R's) | Honest provenance: a manifest must name the writer that produced it. Dual acceptance keeps R-written SDPs valid here. Everything else in the manifest — entry fields, sha256 binding, radix `mapping_set_id` ordering — matches R exactly; the mapping-set TSV artifacts themselves are byte-identical |
| 12 | Idiom | `measurement-decompositions.json` provenance records `metasalmonpy.write_sdp_measurement_decompositions` + `metasalmonpy_version`; the validator accepts either implementation's provenance (R's validator accepts only R's) | The same honest-provenance ruling as row 11, applied to the 0.1.7 decomposition artifact. The artifact binding (path, sha256, row_count) and the decomposition CSV bytes themselves are byte-identical to R's (asserted against R-generated fixtures in `tests/data/decompositions/`) |
| 13 | Ahead | A dangling symlink at a managed output path is treated as existing (blocked without `overwrite`, then refused as a symlink) in `write_sdp_sssom` and `write_sdp_measurement_decompositions`; R's `file.exists()` misses dangling symlinks and silently writes through/over them | Fail-closed writer safety: a symlink the writer did not create should never be replaced implicitly. Read-side symlink handling matches R exactly (only resolvable symlinks are refused; dangling ones surface as missing files) |

| 14 | Idiom | The EML mapping sidecar is parsed with PyYAML (in the `[eml]` extra), not the restricted YAML-subset parser used for SSSOM headers | The sidecar is genuinely nested (creators, contacts, per-table attribute configs), unlike the flat SSSOM header; R uses `yaml::read_yaml` here too. Core dependencies stay pandas+requests |
| 15 | Idiom | EML documents are built with stdlib ElementTree and validated with lxml; parity with R is structural (`ET.canonicalize`), not byte-level | R serializes through libxml2, so whitespace differs while the documents are identical. lxml IS libxml2 — the engine behind `emld::eml_validate` — so accept/reject matches R by construction, and all UUIDv5 identifiers match exactly |

Maintenance: add a row in the same PR that introduces the difference, and
mirror it to the hub register in the same stream.
