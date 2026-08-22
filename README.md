# metasalmonpy

Python implementation of the
[metasalmon](https://github.com/salmon-data-mobilization/metasalmon) workflows
for Salmon Data Packages (SDPs). It provides the same core package-creation,
semantic-review, term-governance, validation, and EDH metadata lifecycle for
Python users.

[Documentation](https://salmon-data-mobilization.github.io/metasalmonpy/) |
[API reference](https://salmon-data-mobilization.github.io/metasalmonpy/reference/) |
[Issue tracker](https://github.com/salmon-data-mobilization/metasalmonpy/issues)

## Installation

Install from the repository (the `v0.1.6` tag predates the rename and still
packages the old `salmonpy` name; the next parity release will be the first
tag installable as `metasalmonpy`):

```bash
python -m pip install \
  "metasalmonpy @ git+https://github.com/salmon-data-mobilization/metasalmonpy@main"
```

For development:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[test]"
```

## Quickstart

```python
import pandas as pd
from metasalmonpy import create_sdp, validate_salmon_datapackage

df = pd.DataFrame({"species": ["Coho", "Chinook"], "count": [100, 200]})
package_path = create_sdp(
    df,
    path="demo-sdp",
    dataset_id="demo",
    table_id="observations",
    seed_semantics=False,
)

# After reviewing metadata and replacing MISSING/REVIEW values:
validate_salmon_datapackage(package_path, require_iris=True)
```

`create_sdp()` writes data under `data/`, metadata under `metadata/`, a
Frictionless `datapackage.json`, and a review checklist. Use
`write_salmon_datapackage()` when the metadata tables are already prepared.
The previous `create_salmon_datapackage*()` names remain as deprecated
compatibility aliases.

## Semantic review

Deterministic retrieval remains the default. LLM review is strictly opt-in:
context files never enable it by themselves, and context inputs must be local
paths rather than parsed pandas/XML objects.

```python
from metasalmonpy import infer_dictionary, suggest_semantics

dictionary = infer_dictionary(
    df,
    dataset_id="demo",
    table_id="observations",
)

reviewed = suggest_semantics(
    df,
    dictionary,
    sources=["smn"],  # Explicit sources are a strict allowlist, including retries.
    llm_assess=True,
    llm_provider="openrouter",
    llm_context_files=["data-dictionary.csv"],
)

suggestions = reviewed.attrs["semantic_suggestions"]
assessments = reviewed.attrs["semantic_llm_assessments"]
```

Measurement candidates are reviewed as six-slot bundles: variable, property,
entity, unit, constraint, and method. The stable 30-column assessment schema
records retries and escalation provenance. Provider failures preserve the
deterministic shortlist, and conservative validators can downgrade unsupported
acceptances without inventing or substituting terms.

Use `detect_semantic_term_gaps()` to combine candidate gaps with final
`request_new_term` decisions. `render_ontology_term_request()` supports SMN,
GCDFO, and local-profile routing; submission always requires explicit curator
confirmation. `chat_decomposition()` provides a resumable interactive review
for a measurement column.

The optional `context` extra adds Excel and PDF context readers:

```bash
pip install -e ".[context,test]"
```

## Access private CSVs from GitHub

```python
from metasalmonpy import github_raw_url, read_github_csv

# Token discovery checks GITHUB_PAT/GH_TOKEN or your git credential store.
# Run metasalmon::ms_setup_github() once in R to create/store a PAT with repo scope.

dim_date = read_github_csv(
    "data/gold/dimension_tables/dim_date.csv",
    repo="dfo-pacific-science/qualark-data",
)
dim_date_pinned = read_github_csv(
    "data/gold/dimension_tables/dim_date.csv",
    ref="v0.3.0",
    repo="dfo-pacific-science/qualark-data",
)

print(
    github_raw_url(
        "data/gold/dimension_tables/dim_date.csv",
        repo="dfo-pacific-science/qualark-data",
    )
)
```

## Running tests

```bash
.venv/bin/python -m pytest -q
.venv/bin/python tests/smoke.py
```

Tests and CI use deterministic injected provider adapters. They blank provider
credentials and never spend OpenAI/OpenRouter credits. Live-provider evaluation
is a separate, explicitly invoked maintainer check.

## Building the documentation

Install [Quarto](https://quarto.org/docs/get-started/) and the documentation
extra, then generate the API pages and render the site:

```bash
python -m pip install -e ".[docs]"
python -m quartodoc build
quarto render
```

The source site is `_quarto.yml`, `index.qmd`, `getting-started.qmd`,
`guides/`, and public Python docstrings. `reference/`, `_sidebar.yml`, and
`_site/` are generated.

## Compatibility

- metasalmonpy 0.1.6 aligns its core user-facing behavior with metasalmon 0.1.6.
- The R package remains the normative SDP/ontology contract. Python-native
  implementation details and test harnesses intentionally differ where the
  public behavior does not.

## Extras
- Validate metadata CSVs: `python -m metasalmonpy.scripts.validate_sdp --dataset metadata/dataset.csv --tables metadata/tables.csv --dictionary metadata/column_dictionary.csv [--codes metadata/codes.csv] [--require-semantics]`
- Draft a new term request: `python -m metasalmonpy.scripts.draft_new_term --label "<label>" --definition "<definition>" --term-type skos_concept --parent-iri <iri>`
- Enable term search cache: set `METASALMONPY_CACHE=1` (read at call time; the
  pre-rename `SALMONPY_CACHE` spelling still works with a `DeprecationWarning`
  until the first release after the S10 parity release)
- Check explicitly for a newer release: `python -c "import metasalmonpy; print(metasalmonpy.check_for_updates())"`

## Releasing

The public distribution currently uses GitHub Releases rather than PyPI.
Release tags match the package version and include both the source distribution
and wheel produced by `python -m build`.
