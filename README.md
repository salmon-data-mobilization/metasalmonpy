# salmonpy

Python mirror of the metasalmon R package for working with Salmon Data Packages (SDPs). Provides helpers to infer and validate dictionaries, search ontology terms, suggest semantics, suggest DwC-DP table/field mappings, and build/read Frictionless-style SDP bundles.

## Quickstart

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

```python
import pandas as pd
from salmonpy import (
    create_salmon_datapackage,
    infer_dictionary,
    suggest_semantics,
    validate_dictionary,
)

df = pd.DataFrame({"species": ["Coho", "Chinook"], "count": [100, 200]})
dict_df = infer_dictionary(df, dataset_id="demo", table_id="observations")
dict_df.loc[dict_df["column_name"] == "count", "column_role"] = "measurement"

# Non-strict validation now warns (not aborts) if semantic IRIs are missing on measurement rows.
validate_dictionary(dict_df)

# If you want an explicit hard check for complete semantic coverage, use strict mode:
# validate_dictionary(dict_df, require_iris=True)

# Optional workflow:
# dict_df = suggest_semantics(df, dict_df)  # attach candidate term/property/entity/unit IRIs
# dict_df.loc[dict_df["column_name"] == "count", "term_iri"] = "https://w3id.org/gcdfo/salmon#..."
# validate_dictionary(dict_df, require_iris=True)
#
# See for guidance:
# https://dfo-pacific-science.github.io/metasalmon/articles/reusing-standards-salmon-data-terms.html
```

## Access private CSVs from GitHub

```python
from salmonpy import github_raw_url, read_github_csv

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
. .venv/bin/activate
python -m unittest discover tests
```

## Compatibility
- salmonpy 0.1.3 aligns with metasalmon 0.0.13 for the local package helpers, semantic suggestion workflow, NuSEDS method crosswalks, DwC-DP descriptor export, EDH XML bootstrap export, and term-request payload helpers.

## Extras
- Validate an SDP: `python -m salmonpy.scripts.validate_sdp --dataset dataset.csv --tables tables.csv --dictionary column_dictionary.csv [--codes codes.csv] [--require-semantics]`
- Draft a new term request: `python -m salmonpy.scripts.draft_new_term --label "<label>" --definition "<definition>" --term-type skos_concept --parent-iri <iri>`
- Enable term search cache: set `SALMONPY_CACHE=1`

## Publishing (PyPI)
1) Bump the version in `pyproject.toml`.
2) Install build tooling: `pip install build twine`.
3) Build artifacts: `python -m build`.
4) Upload: `twine upload dist/*` (requires PyPI credentials).
5) Tag the release in git to match the version.
