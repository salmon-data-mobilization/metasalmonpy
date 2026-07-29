# Changelog

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
- Fixed editable package metadata so `pip install -e .` exposes `salmonpy` and CLI helper modules.

## 0.1.2
- Renamed the GitHub CSV helpers to generic names: `github_raw_url()` and `read_github_csv()` (`repo` is now required unless a full URL is provided).
- Updated compatibility to align with metasalmon 0.0.5.

## 0.1.0
- Initial alignment with metasalmon 0.0.3: dictionary inference/validation, term search (OLS/NVS/BioPortal), semantics suggestions, SDP package IO.
- Added round-trip tests against metasalmon, SDP validator CLI, new term helper script, and optional term search caching (`SALMONPY_CACHE=1`).
