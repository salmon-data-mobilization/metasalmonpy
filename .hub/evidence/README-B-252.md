# B-252 selected-schema writer evidence

Measured 2026-09-30 PDT. These scripts write temporary packages only, with
remote schema loading stubbed through the real selected-source option. They
assert the omitted schema fields and preserve a caller-extra positive control.

Run from this Python checkout:

```sh
Rscript .hub/evidence/B-252-selected-schema.R /path/to/metasalmon
uv run --no-project --with pandas --with requests --with pyyaml python .hub/evidence/B-252-selected-schema.py
```

| Case | R at `3364b975` | Python before, `e81cacd` | Python after, `157e31a` |
| --- | --- | --- | --- |
| Create: update_frequency / constraint_iri | Both present | Both present | Both present |
| Create: method_iri | Absent | Present | Absent |
| Direct writer: update_frequency / method_iri / vocabulary_iri | All absent | All present | All absent |
| Direct writer: constraint_iri | Present | Present | Present |
| Explicit caller_extra | Preserved | Preserved | Preserved |

`B-252-R-output.txt`, `B-252-Python-before-output.txt` and
`B-252-Python-after-output.txt` retain complete headers, runtime versions and
source commits. The after output also carries production source-file digests.
The before output was captured from clean production sources at `e81cacd`;
the scripts themselves were untracked diagnostics at that time.

The dictionary's newly supplied `constraint_iri` comes after caller extras in
R and before them in Python under this selected schema. B-252 does not change
the validator or claim byte identity between languages for that ordering; it
pins identical **default Python output bytes before/after** and the measured
field-presence contract. `tests/data/selected_schema_writer/` holds that
pre-fix default SHA-256 baseline, captured before production edits.

The original queue fixture was an inference fixture, so neither missing field
was absent from writer input. It could not demonstrate the proposed port.
The direct-writer test removes optional input fields explicitly, as a caller
can, and tests the actual boundary. The inferred-field test instead proves
those already supplied columns are preserved.
