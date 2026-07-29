# Contributing to salmonpy

salmonpy mirrors the user-facing Salmon Data Package behavior of the metasalmon
R package. Changes should preserve Python-native implementation quality while
keeping the shared package and ontology contracts aligned.

## Development setup

```bash
git clone https://github.com/salmon-data-mobilization/metaSmnPy.git
cd metaSmnPy
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[context,docs,test]"
```

## Workflow

Use the repository issue, branch, pull request, and Project workflow described
in `AGENTS.md`. Keep a change focused and update the issue and Project status
as it moves through review.

Do not commit credentials. LLM review must remain strictly opt-in, and routine
tests must use injected deterministic provider responses.

## Tests

```bash
python -m pytest -q
python tests/smoke.py
python -m build
```

The GitHub Actions parity job also installs the current metasalmon package and
runs the R-to-Python round-trip test.

## Documentation

Public behavior belongs in the Quarto guides and public function docstrings.
Build the site before opening a pull request:

```bash
python -m quartodoc build
quarto render
```

Canonical documentation sources are:

- `_quarto.yml` for navigation and API grouping;
- `index.qmd`, `getting-started.qmd`, and `guides/` for user workflows;
- public NumPy-style docstrings for function reference; and
- `README.md` for installation and the shortest usable example.

`reference/`, `_sidebar.yml`, and `_site/` are generated and must not be
committed.

## Parity changes

When metasalmon changes an externally visible contract:

1. Add or update a Python behavior test.
2. Implement the corresponding Python workflow.
3. Update the parity guide and changelog.
4. Run the Python suite and the R-to-Python round trip.
5. Record any deliberate Python-native difference in the pull request.
