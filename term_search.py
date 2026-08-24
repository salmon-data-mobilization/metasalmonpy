from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
import xml.etree.ElementTree as ET
from typing import Dict, Iterable, List, Optional, Sequence

try:
    import pandas as pd
except ImportError as exc:  # pragma: no cover - import guard
    raise ImportError("metasalmonpy requires pandas; install via `pip install pandas`.") from exc

try:
    import requests
except ImportError as exc:  # pragma: no cover - import guard
    raise ImportError("metasalmonpy requires requests; install via `pip install requests`.") from exc

from .term_search_smn import (
    SMN_INDEX_COLUMNS,
    _smn_module_urls,
    _smn_role_hints,
    parse_smn_ttl_modules,
)
from .text_safety import redact_secrets

try:
    from importlib import resources
except ImportError as exc:  # pragma: no cover
    raise

try:  # pragma: no cover - best effort metadata lookup
    from importlib.metadata import version as _pkg_version
except ImportError:  # pragma: no cover
    _pkg_version = None


_warned_bioportal_missing = False
_term_cache: Dict[tuple, pd.DataFrame] = {}

# Legacy SALMONPY_* spellings that already warned once this process, so the
# deprecation nudge fires once per variable rather than once per call.
_legacy_env_warned: set = set()


def _env_flag(name: str) -> bool:
    """Read the ``METASALMONPY_<name>`` on/off switch **at call time**.

    A module-level binding is evaluated when the module is imported, so an
    installed package captured the importing process's environment once and a
    user who set the variable afterwards was never heard — the exact bug class
    metasalmon 0.2.2 fixed for ``METASALMON_CACHE`` (a top-level R binding
    evaluated when the namespace was built). Mirrors
    ``.metasalmon_cache_enabled()``: unset and empty are both "off".

    The pre-rename ``SALMONPY_<name>`` spelling still works with a
    ``DeprecationWarning`` while the S10 deprecation window is open; it is
    consulted only when the current spelling is unset or empty, and is removed
    in the first tagged release after the S10 parity release (see CHANGELOG).
    """
    current = os.getenv(f"METASALMONPY_{name}", "")
    if current != "":
        return current.lower() in {"1", "true", "yes"}
    legacy = os.getenv(f"SALMONPY_{name}", "")
    if legacy != "":
        if name not in _legacy_env_warned:
            warnings.warn(
                f"SALMONPY_{name} is deprecated; set METASALMONPY_{name} "
                "instead. The old spelling is removed in the first release "
                "after the S10 parity release.",
                DeprecationWarning,
                stacklevel=3,
            )
            _legacy_env_warned.add(name)
        return legacy.lower() in {"1", "true", "yes"}
    return False


def _cache_enabled() -> bool:
    return _env_flag("CACHE")


def _debug_fetch_enabled() -> bool:
    return _env_flag("DEBUG_FETCH")

_USER_AGENT = "metasalmonpy/unknown"
if _pkg_version:
    try:
        _USER_AGENT = f"metasalmonpy/{_pkg_version('metasalmonpy')}"
    except Exception:  # pragma: no cover - fallback
        _USER_AGENT = "metasalmonpy/unknown"


def _empty_terms(role=None) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "label": pd.Series(dtype=object),
            "iri": pd.Series(dtype=object),
            "source": pd.Series(dtype=object),
            "ontology": pd.Series(dtype=object),
            "role": pd.Series(dtype=object),
            "match_type": pd.Series(dtype=object),
            "definition": pd.Series(dtype=object),
            "alignment_only": pd.Series(dtype=bool),
            "score": pd.Series(dtype=float),
            "agreement_sources": pd.Series(dtype="Int64"),
        }
    )


# Per-call sinks installed by find_terms() around each source function, so a
# failed vocabulary lookup can be *recorded* without discarding the rows that
# did resolve. Mirrors metasalmon's `.ms_signal_search_failure()` +
# withCallingHandlers pair: R signals a classed condition that is silent when
# nobody handles it, so outside find_terms() a failure stays quiet here too.
_search_failure_sinks: List[List[str]] = []


def _signal_search_failure(url: str, detail: str) -> None:
    """Record a vocabulary lookup that did not answer.

    The point is that a failed lookup must never be indistinguishable from a
    successful empty one. It was: ``_safe_json`` returned ``None`` for both,
    every caller collapsed ``None`` into ``_empty_terms()``, and the
    diagnostic recorded ``status="success", count=0``. A degraded OLS
    therefore looked exactly like "no such term exists", which is the input
    that drives ``request_new_term`` escalation — so an outage manufactured
    ontology gaps (metasalmon 0.2.2).
    """
    if _search_failure_sinks:
        _search_failure_sinks[-1].append(
            f"Vocabulary API request failed: {detail}"
        )


_TIMEOUT_ERROR_PATTERN = re.compile(
    r"timeout|timed out|operation timed out|timeout exceeded|timedout"
)


def _is_timeout_error(message: object) -> bool:
    """Mirror ``.ms_is_timeout_error``."""
    if message is None:
        return False
    return bool(_TIMEOUT_ERROR_PATTERN.search(str(message).lower().strip()))


def _warn_request_timeout(safe_url: str, detail: str) -> None:
    warnings.warn(
        "Vocabulary API request timed out while querying "
        f"{safe_url}. {detail}",
        RuntimeWarning,
        stacklevel=3,
    )


def _safe_json(url: str, headers: Optional[Dict[str, str]] = None, timeout: int = 30) -> Optional[dict]:
    merged_headers = headers.copy() if headers else {}
    merged_headers.setdefault("User-Agent", _USER_AGENT)

    _debug = _debug_fetch_enabled()
    # Redacted at capture, not at display: the redacted form is what reaches
    # warnings, failure records and the diagnostics frame, all of which
    # outlive this call. metasalmon no longer puts a key in a URL either, but
    # a user-supplied endpoint or a future source could, and this is the one
    # place every source's URL is recorded (metasalmon 0.2.3).
    safe_url = redact_secrets(url)

    try:
        req = urllib.request.Request(url, headers=merged_headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status >= 300:
                if _debug:
                    print(f"[_safe_json] HTTP error {resp.status}", file=sys.stderr)
                if resp.status == 408:
                    _warn_request_timeout(safe_url, "HTTP 408 (Request Timeout)")
                _signal_search_failure(safe_url, f"HTTP {resp.status}")
                return None
            body = resp.read().decode("utf-8")
            return json.loads(body)
    except urllib.error.HTTPError as http_err:
        # A real server answer. The curl fallback below exists for broken
        # local HTTPS stacks; re-asking a server that already said 404/503
        # would only repeat the same answer, so this signals immediately —
        # matching R, whose httr GET never falls back at all.
        if _debug:
            print(f"[_safe_json] HTTP error {http_err.code}", file=sys.stderr)
        if http_err.code == 408:
            _warn_request_timeout(safe_url, "HTTP 408 (Request Timeout)")
        _signal_search_failure(safe_url, f"HTTP {http_err.code}")
        return None
    except Exception as _urllib_err:
        # Some environments ship Python builds that fail to establish HTTPS
        # connections (e.g., Errno 8/9 "nodename nor servname provided" or
        # "Bad file descriptor"). Fall back to curl if available.
        if _debug:
            print(f"[_safe_json] urllib failed: {type(_urllib_err).__name__}", file=sys.stderr)
        urllib_detail = redact_secrets(str(_urllib_err))
        try:
            if shutil.which("curl") is None:
                if _debug:
                    print("[_safe_json] curl not found", file=sys.stderr)
                if _is_timeout_error(urllib_detail):
                    _warn_request_timeout(safe_url, urllib_detail)
                _signal_search_failure(safe_url, urllib_detail)
                return None
            # --fail keeps the fallback honest: without it an HTTP error page
            # served as valid JSON would masquerade as a successful lookup —
            # the exact failure mode the signalling above exists to prevent.
            cmd = ["curl", "-s", "--fail", "-L", url]
            for key, value in merged_headers.items():
                cmd.extend(["-H", f"{key}: {value}"])
            if _debug:
                print(f"[_safe_json] running curl (timeout={timeout})...", file=sys.stderr)
            body = subprocess.check_output(cmd, timeout=timeout).decode("utf-8")
            if _debug:
                print(f"[_safe_json] curl success: {len(body)} bytes", file=sys.stderr)
            return json.loads(body) if body else None
        except Exception as _curl_err:
            if _debug:
                print(f"[_safe_json] curl failed: {type(_curl_err).__name__}: {_curl_err}", file=sys.stderr)
            curl_detail = redact_secrets(str(_curl_err))
            if _is_timeout_error(urllib_detail) or _is_timeout_error(curl_detail):
                _warn_request_timeout(safe_url, urllib_detail)
            _signal_search_failure(safe_url, f"{urllib_detail}; {curl_detail}")
            return None


def _load_iadopt_vocab() -> pd.DataFrame:
    try:
        vocab_path = resources.files("metasalmonpy").joinpath("data/iadopt-terminologies.csv")
        with resources.as_file(vocab_path) as path:
            df = pd.read_csv(path)
    except Exception:
        return pd.DataFrame()

    df["host"] = df["ttl_url"].apply(lambda u: urllib.parse.urlparse(u).hostname or "")
    df["slug"] = df["ttl_url"].apply(lambda u: os.path.splitext(os.path.basename(u))[0])
    df["label_tokens"] = df["label"].apply(lambda x: re.sub(r"[^a-z0-9]+", " ", str(x).lower()))
    return df


def _load_role_preferences() -> pd.DataFrame:
    try:
        pref_path = resources.files("metasalmonpy").joinpath("data/ontology-preferences.csv")
        with resources.as_file(pref_path) as path:
            df = pd.read_csv(path)
    except Exception:
        return pd.DataFrame(
            columns=[
                "role",
                "ontology",
                "priority",
                "source_hint",
                "iri_pattern",
                "alignment_only",
                "notes",
            ]
        )

    if "alignment_only" in df.columns:
        df["alignment_only"] = df["alignment_only"].astype(str).str.lower().isin({"true", "1", "yes"})
    return df


def _search_ols(query: str, role) -> pd.DataFrame:
    encoded = urllib.parse.quote(query, safe="")
    url = f"https://www.ebi.ac.uk/ols4/api/search?q={encoded}&rows=50"
    data = _safe_json(url)
    docs = data.get("response", {}).get("docs", []) if data else []
    if not docs:
        return _empty_terms(role)

    docs_df = pd.DataFrame(docs)
    desc_series = docs_df.get("description", pd.Series([], dtype=object)).apply(
        lambda x: x[0] if isinstance(x, list) and x else ""
    )
    return pd.DataFrame(
        {
            "label": docs_df.get("label", pd.Series([], dtype=object)).fillna(""),
            "iri": docs_df.get("iri", pd.Series([], dtype=object)).fillna(""),
            "source": "ols",
            "ontology": docs_df.get("ontology_name", pd.Series([], dtype=object)).fillna(""),
            "role": role,
            "match_type": docs_df.get("type", pd.Series([], dtype=object)).fillna(""),
            "definition": desc_series,
        }
    )


def _search_nvs(query: str, role) -> pd.DataFrame:
    tokens = list({tok for tok in re.sub(r"[^a-z0-9]+", " ", str(query).lower()).split() if tok})
    if not tokens:
        return _empty_terms(role)

    # NVS search_nvs endpoints are not reliable; use the SPARQL endpoint instead.
    # Restrict to P01 (observables) and P06 (units).
    # Use simple CONTAINS on prefLabel for speed (REGEX + OPTIONAL is too slow on P01).
    pattern = ".*".join(tokens)
    sparql = (
        "PREFIX skos: <http://www.w3.org/2004/02/skos/core#>\n"
        "SELECT DISTINCT ?uri ?label ?definition WHERE {\n"
        "  ?uri skos:prefLabel ?label .\n"
        "  OPTIONAL { ?uri skos:definition ?definition . }\n"
        "  FILTER(\n"
        "    STRSTARTS(STR(?uri), \"http://vocab.nerc.ac.uk/collection/P01/\") ||\n"
        "    STRSTARTS(STR(?uri), \"http://vocab.nerc.ac.uk/collection/P06/\")\n"
        "  )\n"
        f"  FILTER(REGEX(LCASE(STR(?label)), \"{pattern}\"))\n"
        "}\n"
        "LIMIT 50\n"
    )

    url = "https://vocab.nerc.ac.uk/sparql/?" + urllib.parse.urlencode({"query": sparql})
    data = _safe_json(url, headers={"Accept": "application/sparql-results+json"}, timeout=60)
    bindings = data.get("results", {}).get("bindings", []) if data else []
    if not bindings:
        return _empty_terms(role)

    rows = []
    for b in bindings:
        iri = b.get("uri", {}).get("value", "")
        label = b.get("label", {}).get("value", "")
        definition = b.get("definition", {}).get("value", "") if isinstance(b.get("definition"), dict) else ""
        match = re.match(r"^http://vocab\.nerc\.ac\.uk/collection/([^/]+)/", iri)
        ontology = match.group(1) if match else ""
        rows.append(
            {
                "label": label,
                "iri": iri,
                "source": "nvs",
                "ontology": ontology,
                "role": role,
                "match_type": "concept",
                "definition": definition,
            }
        )

    df = pd.DataFrame(rows)
    if df.empty:
        return _empty_terms(role)
    return df.drop_duplicates(subset=["iri"]).reset_index(drop=True)


def _search_zooma(query: str, role) -> pd.DataFrame:
    encoded = urllib.parse.quote(query, safe="")
    url = f"https://www.ebi.ac.uk/spot/zooma/v2/api/services/annotate?propertyValue={encoded}"
    data = _safe_json(url, headers={"Accept": "application/json"}, timeout=60)
    if not isinstance(data, list) or not data:
        return _empty_terms(role)

    hrefs: List[str] = []
    confidence_by_iri: Dict[str, str] = {}
    for ann in data:
        if not isinstance(ann, dict):
            continue
        conf = str(ann.get("confidence") or "")
        for tag in ann.get("semanticTags", []) or []:
            if isinstance(tag, str) and tag and tag not in confidence_by_iri:
                confidence_by_iri[tag] = conf
        links = (ann.get("_links") or {}).get("olslinks", []) or []
        for link in links:
            if not isinstance(link, dict):
                continue
            href = link.get("href")
            if isinstance(href, str) and href and href not in hrefs:
                hrefs.append(href)

    hrefs = hrefs[:25]
    rows = []
    for href in hrefs:
        term_data = _safe_json(href)
        terms = (term_data.get("_embedded") or {}).get("terms", []) if isinstance(term_data, dict) else []
        if not terms:
            continue
        term = terms[0] if isinstance(terms[0], dict) else {}
        iri = str(term.get("iri") or "")
        label = str(term.get("label") or "")
        ontology = str(term.get("ontology_name") or "")
        desc = term.get("description") or []
        definition = desc[0] if isinstance(desc, list) and desc else ""
        confidence = confidence_by_iri.get(iri) or "unknown"
        match_type = f"zooma_{confidence.lower()}" if confidence else "zooma_unknown"
        rows.append(
            {
                "label": label,
                "iri": iri,
                "source": "zooma",
                "ontology": ontology,
                "role": role,
                "match_type": match_type,
                "definition": definition,
            }
        )

    df = pd.DataFrame(rows)
    if df.empty:
        return _empty_terms(role)
    return df.drop_duplicates(subset=["iri"]).reset_index(drop=True)


def _search_bioportal(query: str, role) -> pd.DataFrame:
    apikey = os.getenv("BIOPORTAL_APIKEY", "")
    if not apikey:
        global _warned_bioportal_missing
        if not _warned_bioportal_missing:
            warnings.warn(
                "BioPortal API key missing; set env BIOPORTAL_APIKEY and restart your session. "
                "Example (bash/zsh): export BIOPORTAL_APIKEY=your_key_here "
                "Persist it in ~/.Renviron or ~/.zshrc with a line: BIOPORTAL_APIKEY=your_key_here "
                "Get a key at https://bioportal.bioontology.org/register. "
                "Do not paste keys into chat; keep them in your environment.",
                RuntimeWarning,
            )
            _warned_bioportal_missing = True
        return _empty_terms(role)
    encoded = urllib.parse.quote(query, safe="")
    # The key travels in a header, not the query string. In the URL it was
    # written into request logs and proxy logs at both ends, and would be
    # quoted verbatim by any warning or diagnostic that records the URL
    # (metasalmon 0.2.3).
    url = f"https://data.bioontology.org/search?q={encoded}"
    data = _safe_json(url, headers={"Authorization": f"apikey token={apikey}"})
    coll = data.get("collection", []) if data else []
    if not coll:
        return _empty_terms(role)

    coll_df = pd.DataFrame(coll)
    ontology_series = coll_df.get("links", pd.Series([], dtype=object)).apply(
        lambda x: x.get("ontology") if isinstance(x, dict) else ""
    )

    return pd.DataFrame(
        {
            "label": coll_df.get("prefLabel", pd.Series([], dtype=object)).fillna(""),
            "iri": coll_df.get("@id", pd.Series([], dtype=object)).fillna(""),
            "source": "bioportal",
            "ontology": ontology_series.fillna(""),
            "role": role,
            "match_type": coll_df.get("matchType", pd.Series([], dtype=object)).fillna(""),
            "definition": [
                (desc[0] if isinstance(desc, list) and desc else "")
                for desc in coll_df.get("definition", pd.Series([], dtype=object))
            ],
        }
    )


def _search_qudt(query: str, role) -> pd.DataFrame:
    tokens = list({tok for tok in re.sub(r"[^a-z0-9]+", " ", str(query).lower()).split() if tok})
    if not tokens:
        return _empty_terms(role)

    pattern = ".*".join(tokens)
    # metasalmon v0.1.7: QUDT is a preferred source for the *property* role
    # too, where the matching resource class is QuantityKind rather than Unit.
    resource_class = "QuantityKind" if str(role or "").lower() == "property" else "Unit"
    match_type = "quantity_kind" if resource_class == "QuantityKind" else "unit"
    sparql = (
        "PREFIX qudt: <http://qudt.org/schema/qudt/>\n"
        "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n"
        "PREFIX skos: <http://www.w3.org/2004/02/skos/core#>\n"
        "SELECT DISTINCT ?uri ?label ?definition WHERE {\n"
        f"  ?uri a qudt:{resource_class} .\n"
        "  ?uri rdfs:label ?label .\n"
        "  OPTIONAL { ?uri skos:definition ?definition . }\n"
        "  OPTIONAL { ?uri qudt:description ?definition . }\n"
        f"  FILTER(REGEX(LCASE(STR(?label)), \"{pattern}\", \"i\"))\n"
        "}\n"
        "LIMIT 50\n"
    )
    url = "https://www.qudt.org/fuseki/qudt/sparql?" + urllib.parse.urlencode({"query": sparql})
    data = _safe_json(url, headers={"Accept": "application/sparql-results+json"}, timeout=60)
    bindings = data.get("results", {}).get("bindings", []) if data else []
    if not bindings:
        return _empty_terms(role)

    rows = []
    for b in bindings:
        iri = b.get("uri", {}).get("value", "")
        label = b.get("label", {}).get("value", "")
        definition = b.get("definition", {}).get("value", "") if isinstance(b.get("definition"), dict) else ""
        rows.append(
            {
                "label": label,
                "iri": iri,
                "source": "qudt",
                "ontology": "qudt",
                "role": role,
                "match_type": match_type,
                "definition": definition,
            }
        )
    df = pd.DataFrame(rows)
    if df.empty:
        return _empty_terms(role)
    return df.drop_duplicates(subset=["iri"]).reset_index(drop=True)


def _search_gbif(query: str, role) -> pd.DataFrame:
    encoded = urllib.parse.quote(query, safe="")
    url = f"https://api.gbif.org/v1/species/match?name={encoded}&verbose=true"
    data = _safe_json(url, timeout=30)
    if not data or not data.get("usageKey"):
        url = f"https://api.gbif.org/v1/species/search?q={encoded}&limit=20"
        data = _safe_json(url, timeout=30)
        results = data.get("results", []) if data else []
        if not results:
            return _empty_terms(role)
        rows = []
        for item in results:
            label = item.get("scientificName") or item.get("canonicalName") or ""
            key = item.get("key")
            if not key:
                continue
            rows.append(
                {
                    "label": label,
                    "iri": f"https://www.gbif.org/species/{key}",
                    "source": "gbif",
                    "ontology": "gbif_backbone",
                    "role": role,
                    "match_type": str(item.get("rank") or "taxon").lower(),
                    "definition": "; ".join(
                        part
                        for part in [
                            f"Kingdom: {item.get('kingdom')}" if item.get("kingdom") else None,
                            f"Phylum: {item.get('phylum')}" if item.get("phylum") else None,
                            f"Class: {item.get('class')}" if item.get("class") else None,
                            f"Order: {item.get('order')}" if item.get("order") else None,
                            f"Family: {item.get('family')}" if item.get("family") else None,
                        ]
                        if part
                    ),
                }
            )
        df = pd.DataFrame(rows)
        if df.empty:
            return _empty_terms(role)
        return df.drop_duplicates(subset=["iri"]).reset_index(drop=True)

    label = data.get("scientificName") or data.get("canonicalName") or ""
    key = data.get("usageKey")
    if not key:
        return _empty_terms(role)
    return pd.DataFrame(
        {
            "label": [label],
            "iri": [f"https://www.gbif.org/species/{key}"],
            "source": ["gbif"],
            "ontology": ["gbif_backbone"],
            "role": [role],
            "match_type": [str(data.get("rank") or "taxon").lower()],
            "definition": [
                "; ".join(
                    part
                    for part in [
                        f"Kingdom: {data.get('kingdom')}" if data.get("kingdom") else None,
                        f"Phylum: {data.get('phylum')}" if data.get("phylum") else None,
                        f"Class: {data.get('class')}" if data.get("class") else None,
                        f"Order: {data.get('order')}" if data.get("order") else None,
                        f"Family: {data.get('family')}" if data.get("family") else None,
                    ]
                    if part
                )
            ],
        }
    )


def _search_worms(query: str, role) -> pd.DataFrame:
    encoded = urllib.parse.quote(query, safe="")
    url = (
        "https://www.marinespecies.org/rest/AphiaRecordsByName/"
        f"{encoded}?like=true&marine_only=false&offset=1"
    )
    data = _safe_json(url, timeout=30)
    if not isinstance(data, list) or not data:
        url = (
            "https://www.marinespecies.org/rest/AphiaRecordsByMatchNames"
            f"?scientificnames%5B%5D={encoded}"
        )
        data = _safe_json(url, timeout=30)
        if not data or not isinstance(data, list) or not data[0]:
            return _empty_terms(role)
        data = data[0]

    rows = []
    for item in data:
        if not isinstance(item, dict):
            continue
        aphia_id = item.get("AphiaID")
        if not aphia_id:
            continue
        rows.append(
            {
                "label": item.get("scientificname") or "",
                "iri": f"urn:lsid:marinespecies.org:taxname:{aphia_id}",
                "source": "worms",
                "ontology": "worms",
                "role": role,
                "match_type": str(item.get("rank") or "taxon").lower(),
                "definition": "; ".join(
                    part
                    for part in [
                        f"Kingdom: {item.get('kingdom')}" if item.get("kingdom") else None,
                        f"Phylum: {item.get('phylum')}" if item.get("phylum") else None,
                        f"Class: {item.get('class')}" if item.get("class") else None,
                        f"Order: {item.get('order')}" if item.get("order") else None,
                        f"Family: {item.get('family')}" if item.get("family") else None,
                    ]
                    if part
                ),
            }
        )
    df = pd.DataFrame(rows)
    if df.empty:
        return _empty_terms(role)
    return df.drop_duplicates(subset=["iri"]).reset_index(drop=True)


def _ontology_index_empty() -> pd.DataFrame:
    return pd.DataFrame(columns=list(SMN_INDEX_COLUMNS))


# Ontology sources for the two local term indexes. The W3ID IRIs are the
# canonical entrypoints; they currently redirect to the
# salmon-data-mobilization GitHub organization. No ref is pinned because the
# R implementation follows the unpinned W3ID redirects.
_SMN_TTL_ACCEPT = "text/turtle, text/plain;q=0.9"
_RDFXML_ACCEPT = "application/rdf+xml"
_SMN_ROOT_URL = "https://w3id.org/smn/"
_SMN_ROOT_FALLBACK_URLS = ("https://w3id.org/smn",)
_GCDFO_URL = "https://w3id.org/gcdfo/salmon"
_GCDFO_FALLBACK_URLS = ("https://w3id.org/gcdfo/salmon/",)
_SMN_IRI_PATTERN = r"^https?://w3id\.org/smn(#|/|$)"
_GCDFO_IRI_PATTERN = r"^https?://w3id\.org/gcdfo/salmon(#|$)"

_RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
_RDFS_NS = "http://www.w3.org/2000/01/rdf-schema#"
_OWL_NS = "http://www.w3.org/2002/07/owl#"
_SKOS_NS = "http://www.w3.org/2004/02/skos/core#"
_DCTERMS_NS = "http://purl.org/dc/terms/"


def _fetch_ontology_text(
    url: str,
    accept: str,
    fallback_urls: Sequence[str] = (),
    timeout: int = 30,
) -> str:
    """
    Fetch one ontology document and return its text.

    A pure function of the source URL(s): no caching happens here, so a
    session cache can wrap it later (metasalmon 0.2.2 parity) without a
    behaviour change. Raises ``RuntimeError`` when every URL fails or the
    body is empty — a failed lookup must never masquerade as an empty index.
    """
    urls = [url, *fallback_urls]
    last_error: Optional[str] = None
    for attempt_url in urls:
        try:
            response = requests.get(
                attempt_url,
                headers={"Accept": accept, "User-Agent": _USER_AGENT},
                timeout=timeout,
            )
        except requests.RequestException as exc:
            last_error = str(exc)
            continue
        if response.status_code == 200:
            if response.text.strip():
                return response.text
            last_error = "empty response body"
        else:
            last_error = f"HTTP {response.status_code}"
    raise RuntimeError(
        f"Failed to fetch ontology from {', '.join(urls)}; last error: {last_error}"
    )


def _iri_local_name(value: Optional[str]) -> str:
    if not value:
        return ""
    return re.sub(r"^.*[#/]", "", value)


def _camel_words(value: str) -> str:
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    return re.sub(r"[_-]+", " ", value).strip()


def _xml_local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_text_values(node: "ET.Element", namespace: str, local: str) -> List[str]:
    values = []
    qualified = f"{{{namespace}}}{local}"
    for child in node:
        if child.tag == qualified:
            value = (child.text or "").strip()
            if value:
                values.append(value)
    return values


def _xml_text_values_local(node: "ET.Element", local: str) -> List[str]:
    values = []
    for child in node:
        if _xml_local_tag(child.tag) == local:
            value = (child.text or "").strip()
            if value:
                values.append(value)
    return values


def _xml_resource_values(node: "ET.Element", namespace: str, local: str) -> List[str]:
    resource_attr = f"{{{_RDF_NS}}}resource"
    qualified = f"{{{namespace}}}{local}"
    values = []
    for child in node:
        if child.tag == qualified:
            value = child.attrib.get(resource_attr)
            if value:
                values.append(value)
    return values


def _xml_resource_values_local(node: "ET.Element", local: str) -> List[str]:
    resource_attr = f"{{{_RDF_NS}}}resource"
    values = []
    for child in node:
        if _xml_local_tag(child.tag) == local:
            value = child.attrib.get(resource_attr)
            if value:
                values.append(value)
    return values


def _first_non_empty(values: Iterable[str]) -> str:
    for value in values:
        if value and value.strip():
            return value.strip()
    return ""


def _parse_salmon_rdfxml(xml_text: str, iri_pattern: str) -> pd.DataFrame:
    """
    Parse an RDF/XML salmon ontology document into the shared term-index frame.

    Mirrors R's `.parse_salmon_rdfxml`: direct children of the root that carry
    ``rdf:about`` (excluding ``owl:Ontology``) become rows; I-ADOPT
    decomposition targets drive the role flags.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise RuntimeError(f"Failed to parse ontology RDF/XML: {exc}") from exc

    about_attr = f"{{{_RDF_NS}}}about"
    ontology_tag = f"{{{_OWL_NS}}}Ontology"

    records: List[dict] = []
    for node in list(root):
        iri = node.attrib.get(about_attr)
        if not iri or node.tag == ontology_tag:
            continue

        label = _first_non_empty(
            _xml_text_values(node, _SKOS_NS, "prefLabel")
            + _xml_text_values(node, _RDFS_NS, "label")
        )
        definition = _first_non_empty(
            _xml_text_values(node, _SKOS_NS, "definition")
            + _xml_text_values_local(node, "IAO_0000115")
            + _xml_text_values(node, _RDFS_NS, "comment")
            + _xml_text_values(node, _DCTERMS_NS, "description")
        )
        alt_labels = _xml_text_values(node, _SKOS_NS, "altLabel")
        in_scheme = _xml_resource_values(node, _SKOS_NS, "inScheme")
        rdf_types = _xml_resource_values(node, _RDF_NS, "type")
        parents = list(
            dict.fromkeys(
                _xml_resource_values(node, _SKOS_NS, "broader")
                + _xml_resource_values(node, _RDFS_NS, "subClassOf")
            )
        )
        iadopt_property = _xml_resource_values_local(node, "iadoptProperty")
        iadopt_entity = _xml_resource_values_local(node, "iadoptEntity")
        iadopt_constraint = _xml_resource_values_local(node, "iadoptConstraint")
        used_procedure = _xml_resource_values_local(node, "usedProcedure")

        iri_local = _iri_local_name(iri)
        label_fallback = label if label else _camel_words(iri_local)
        search_text = " ".join(
            [label_fallback]
            + alt_labels
            + [_camel_words(iri_local)]
            + [_camel_words(_iri_local_name(value)) for value in in_scheme]
            + [_camel_words(_iri_local_name(value)) for value in rdf_types]
            + [definition]
        ).lower()

        records.append(
            {
                "iri": iri,
                "label": label_fallback,
                "alt_labels": " | ".join(alt_labels),
                "definition": definition,
                "resource_kind": _xml_local_tag(node.tag),
                "in_scheme": " | ".join(in_scheme),
                "parent_iris": " | ".join(parents),
                "type_iris": " | ".join(rdf_types),
                "search_text": search_text,
                "is_variable": len(iadopt_property + iadopt_entity + iadopt_constraint) > 0,
                "_iadopt_property_targets": iadopt_property,
                "_iadopt_entity_targets": iadopt_entity,
                "_iadopt_constraint_targets": iadopt_constraint,
                "_used_procedure_targets": used_procedure,
            }
        )

    records = [record for record in records if re.search(iri_pattern, record["iri"])]
    if not records:
        return _ontology_index_empty()

    property_targets = {t for record in records for t in record["_iadopt_property_targets"]}
    entity_targets = {t for record in records for t in record["_iadopt_entity_targets"]}
    constraint_targets = {t for record in records for t in record["_iadopt_constraint_targets"]}
    method_targets = {t for record in records for t in record["_used_procedure_targets"]}

    rows: List[dict] = []
    for record in records:
        flags = {
            "is_variable": bool(record["is_variable"]),
            "is_property": record["iri"] in property_targets,
            "is_entity": record["iri"] in entity_targets,
            "is_constraint": record["iri"] in constraint_targets,
            "is_method": record["iri"] in method_targets
            or bool(re.search(r"method|procedure|enumeration", record["in_scheme"].lower())),
            "is_statistical_modifier": bool(
                re.search(
                    r"statistical modifier|statisticalmodifier",
                    f"{record['in_scheme']} {record['type_iris']} {record['label']}".lower(),
                )
            ),
        }
        rows.append(
            {
                "iri": record["iri"],
                "label": record["label"],
                "alt_labels": record["alt_labels"],
                "definition": record["definition"],
                "resource_kind": record["resource_kind"],
                "in_scheme": record["in_scheme"],
                "parent_iris": record["parent_iris"],
                "type_iris": record["type_iris"],
                "search_text": record["search_text"],
                **flags,
                "role_hints": _smn_role_hints(flags),
            }
        )

    return pd.DataFrame(rows, columns=list(SMN_INDEX_COLUMNS))


# An index that has already been resolved in this session is returned without
# touching the network or the parser (metasalmon 0.2.2).
#
# The trade is deliberate: an index is resolved once per session, so a module
# updated upstream mid-session is not picked up until ``refresh=True``. That
# matches the decision already taken for the schema bundle — once a session
# resolves an identity, everything it writes carries that same identity — and
# it is the stronger guarantee for seeding, where two columns in one package
# must not be seeded against two different ontology versions.
_smn_index_cache: Dict[str, pd.DataFrame] = {}
_gcdfo_index_cache: Dict[str, pd.DataFrame] = {}


def _cached_term_index(cache: Dict[str, pd.DataFrame], refresh: bool, resolve) -> pd.DataFrame:
    """Mirror ``.ms_cached_term_index``: resolve once per session.

    A failed ``resolve()`` raises and caches nothing, so the next call tries
    again rather than freezing an outage for the rest of the session.
    """
    if not refresh and "index" in cache:
        return cache["index"]
    index = resolve()
    cache["index"] = index
    return index


def _smn_term_index(refresh: bool = False) -> pd.DataFrame:
    """
    Build the Salmon Domain Ontology (smn) term index.

    Resolved once per session; ``refresh=True`` bypasses and replaces the
    session cache (metasalmon 0.2.2 parity).
    """

    def resolve() -> pd.DataFrame:
        try:
            texts = {
                url: _fetch_ontology_text(url, _SMN_TTL_ACCEPT) for url in _smn_module_urls()
            }
            index: Optional[pd.DataFrame] = parse_smn_ttl_modules(texts)
        except Exception:
            index = None
        if index is not None and not index.empty:
            return index

        # The modules are Turtle-first on W3ID; the root is the RDF/XML
        # fallback for when they are unavailable or parse to nothing.
        xml_text = _fetch_ontology_text(
            _SMN_ROOT_URL, _RDFXML_ACCEPT, fallback_urls=_SMN_ROOT_FALLBACK_URLS
        )
        index = _parse_salmon_rdfxml(xml_text, iri_pattern=_SMN_IRI_PATTERN)
        if index.empty:
            raise RuntimeError(
                "Salmon Domain Ontology (smn) fetch succeeded but parsed to an empty "
                "term index; refusing to return a silently empty index."
            )
        return index

    return _cached_term_index(_smn_index_cache, refresh, resolve)


def _gcdfo_term_index(refresh: bool = False) -> pd.DataFrame:
    """
    Build the DFO Salmon Ontology (gcdfo) term index.

    Resolved once per session; ``refresh=True`` bypasses and replaces the
    session cache (metasalmon 0.2.2 parity).
    """

    def resolve() -> pd.DataFrame:
        xml_text = _fetch_ontology_text(
            _GCDFO_URL, _RDFXML_ACCEPT, fallback_urls=_GCDFO_FALLBACK_URLS
        )
        index = _parse_salmon_rdfxml(xml_text, iri_pattern=_GCDFO_IRI_PATTERN)
        if index.empty:
            raise RuntimeError(
                "DFO Salmon Ontology (gcdfo) fetch succeeded but parsed to an empty "
                "term index; refusing to return a silently empty index."
            )
        return index

    return _cached_term_index(_gcdfo_index_cache, refresh, resolve)


def _filter_local_index(index: pd.DataFrame, query: str, role, source: str, ontology: str) -> pd.DataFrame:
    if index.empty:
        return _empty_terms(role)
    q_tokens = {tok for tok in re.sub(r"[^a-z0-9]+", " ", str(query).lower()).split() if tok}
    if not q_tokens:
        return _empty_terms(role)
    role_col = (
        f"is_{role}"
        if role in {"variable", "property", "entity", "constraint", "method", "statistical_modifier"}
        else None
    )
    df = index.copy()
    if role_col and role_col in df.columns:
        df = df[df[role_col].fillna(False).astype(bool)]
    text = df.get("search_text", df.get("label", pd.Series("", index=df.index))).fillna("").astype(str).str.lower()
    keep = text.apply(lambda value: all(token in value for token in q_tokens))
    df = df[keep].copy()
    if df.empty:
        return _empty_terms(role)
    return pd.DataFrame(
        {
            "label": df["label"].fillna(""),
            "iri": df["iri"].fillna(""),
            "source": source,
            "ontology": ontology,
            "role": role,
            "match_type": "label",
            "definition": df.get("definition", pd.Series("", index=df.index)).fillna(""),
            "role_hints": df.get("role_hints", pd.Series(pd.NA, index=df.index)),
        }
    ).drop_duplicates(subset=["iri"], keep="first").reset_index(drop=True)


def _search_smn(query: str, role) -> pd.DataFrame:
    return _filter_local_index(_smn_term_index(), query, role, "smn", "smn")


def _search_gcdfo(query: str, role) -> pd.DataFrame:
    return _filter_local_index(_gcdfo_term_index(), query, role, "gcdfo", "gcdfo")


def _expand_query(query: str, role) -> List[str]:
    query = str(query)
    if role is None or pd.isna(role) or str(role) == "":
        return [query]
    role_key = str(role).lower()
    out = [query]
    q_lower = query.lower().strip()
    if role_key == "unit":
        unit_map = {"kg": "kilogram", "g": "gram", "m": "meter", "cm": "centimeter", "mm": "millimeter"}
        if q_lower in unit_map:
            out.append(unit_map[q_lower])
        if "unit" not in q_lower:
            out.append(f"{query} unit")
    elif role_key == "method" and "method" not in q_lower:
        out.append(f"{query} method")
    elif role_key == "entity":
        parts = query.split()
        if len(parts) >= 2 and re.match(r"^[A-Z][a-z]+$", parts[0]):
            out.append(parts[0])
    seen = set()
    result = []
    for item in out:
        if item and item.lower() not in seen:
            result.append(item)
            seen.add(item.lower())
    return result


# metasalmon's default ``match_type_weights`` (``.ranking_profile_defaults()``).
# Every value here was read back out of the R v0.1.8 tag rather than copied from
# source, including the ladder's fall-through.
MATCH_TYPE_WEIGHTS = {
    "label_exact": 1.0,
    "label": 0.45,
    "label_partial": 0.45,
    "zooma_high": 0.3,
    "zooma": 0.3,
    "definition": 0.15,
    "concept": 0.15,
    "other": 0.05,
}


def _match_type_score(match_type: object, weights: Optional[dict] = None) -> float:
    """Mirror ``.match_type_score_profiled``: how well the provider matched.

    ``match_type`` is an **optional** field: several providers (QUDT units,
    GBIF/WoRMS taxa, the local term indexes) never populate it, and a caller
    can hand-build a candidate frame without it. A missing value therefore
    means *unclassified*, and scores as such — it is not an error, and it must
    never abort the search.

    metasalmon 0.1.8 fixed exactly that: before the fix a ``None``/``NA``
    ``match_type`` reached a scalar ``if`` and aborted ``suggest_semantics()``
    for the whole dictionary, throwing away every legitimate candidate
    alongside it.
    """
    weights = MATCH_TYPE_WEIGHTS if weights is None else weights
    other = weights.get("other", 0.05)
    # These two guards are intent-stating, not behaviour-changing today:
    # ``str(None)``, ``str(pd.NA)``, ``str(nan)`` and ``str([...])`` all fall
    # through the ladder to ``other`` anyway. They stay because that is a
    # coincidence of ``str()`` and the ladder's current shape -- add one
    # substring branch and ``"nan"`` or ``"<NA>"`` could start matching it.
    # R needs the same two checks for a harder reason: without them a missing
    # value reaches a scalar ``if`` and aborts the whole search (0.1.8's fix).
    if match_type is None or isinstance(match_type, (list, tuple, dict, set)):
        return other
    if match_type is pd.NA or (
        isinstance(match_type, float) and match_type != match_type
    ):
        return other
    text = str(match_type).strip().lower()
    if not text:
        return other
    if text == "label_exact":
        return weights.get("label_exact", 1.0)
    if text.startswith("label"):
        return weights.get("label", 0.45)
    if text.startswith("zooma"):
        return weights.get("zooma", 0.3)
    if text in ("definition", "concept"):
        return weights.get("definition", 0.15)
    return other


def _apply_cross_source_agreement(df: pd.DataFrame, iri_boost: float = 0.5, label_boost: float = 0.2) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    if "score" not in out.columns:
        out["score"] = 0.0
    out["agreement_sources"] = 1
    iri_counts = (
        out[~out["iri"].isna() & (out["iri"].astype(str) != "")]
        .groupby("iri")["source"]
        .nunique()
        .to_dict()
    )
    label_counts = out.groupby(out["label"].fillna("").astype(str).str.lower())["source"].nunique().to_dict()
    for idx, row in out.iterrows():
        iri_count = iri_counts.get(row.get("iri"), 1)
        label_count = label_counts.get(str(row.get("label", "")).lower(), 1)
        agreement = max(int(iri_count), int(label_count), 1)
        out.at[idx, "agreement_sources"] = agreement
        if iri_count > 1:
            out.at[idx, "score"] = out.at[idx, "score"] + ((iri_count - 1) * iri_boost)
        elif label_count > 1:
            out.at[idx, "score"] = out.at[idx, "score"] + ((label_count - 1) * label_boost)
    out["agreement_sources"] = out["agreement_sources"].astype(int)
    return out


#: The ranking source weights, hoisted to module scope so the role-contract
#: guard can enumerate them. R keeps the same two tables inside
#: ``.ranking_profile_defaults()``; this package has no ranking-profile
#: override system (hub backlog #87, PARITY.md row 32), so these are constants
#: rather than a merged profile.
BASE_SOURCE_WEIGHT = {
    "smn": 1.2,
    "gcdfo": 1.0,
    "ols": 0.3,
    "nvs": 0.6,
    "zooma": 0.5,
    "bioportal": 0.2,
    "qudt": 0.7,
    "gbif": 0.6,
    "worms": 0.6,
}

#: ``gcdfo`` is the DFO fallback behind the shared ``smn`` namespace, which is
#: what ontology-preferences.csv declares (smn priority 1, gcdfo 2 where it is
#: listed at all). Keep gcdfo at a flat 1.0: a per-role boost of 1.3 puts it
#: within 0.5 of smn, and routine per-candidate bonuses -- label overlap plus
#: cross-source agreement reach 0.6 on their own -- then overturn the source
#: preference entirely. metasalmon 0.4.0 adopted this package's margin after
#: Brett's 2026-08-17 ruling.
#:
#: **Every role the retrieval layer ranks needs an entry here.** A role absent
#: from this table is scored on base weight alone -- a 0.1-0.2 spread across
#: sources, which is effectively no source preference at all. That is the
#: seventh surface of the role contract, and it is what
#: ``tests/test_role_contract_guard.py`` now checks.
ROLE_BOOST = {
    "unit": {"qudt": 1.5, "nvs": 1.2, "ols": 0.3},
    "property": {"smn": 1.4, "gcdfo": 1.0, "nvs": 1.0, "ols": 0.4},
    "variable": {
        "smn": 1.5,
        "gcdfo": 1.0,
        "nvs": 0.6,
        "ols": 0.2,
        "bioportal": 0.4,
    },
    "entity": {
        "smn": 1.5,
        "gcdfo": 1.0,
        "gbif": 1.3,
        "worms": 1.3,
        "bioportal": 0.4,
        "ols": 0.4,
    },
    "constraint": {"smn": 1.3, "gcdfo": 1.0, "ols": 0.4, "bioportal": 0.4},
    "method": {"smn": 1.3, "gcdfo": 1.0, "bioportal": 0.4, "ols": 0.4},
    # sources_for_role() serves this role from smn and ols only, so there is
    # no gcdfo entry to give it. Without the row the role reached ranking on
    # base weight alone, which is the defect metasalmon 0.4.0 fixed on the R
    # side and this entry fixes here.
    "statistical_modifier": {"smn": 1.5, "ols": 0.4},
}


def _score_and_rank_terms(df: pd.DataFrame, role, vocab_tbl: pd.DataFrame, query: Optional[str] = None) -> pd.DataFrame:
    if df.empty:
        return df

    df = df.copy()
    role_prefs = _load_role_preferences()
    base_source_weight = BASE_SOURCE_WEIGHT
    role_boost = ROLE_BOOST

    df["score"] = df["source"].map(base_source_weight).fillna(0)

    query_tokens: List[str] = []
    if query:
        query_tokens = list({tok for tok in re.sub(r"[^a-z0-9]+", " ", query.lower()).split() if tok})

    role_key = role if role is not None else None
    if role_key and role_key in role_boost:
        df["score"] += df["source"].map(role_boost.get(role_key, {})).fillna(0)

    if not role_prefs.empty and role_key:
        role_specific = role_prefs[(role_prefs["role"] == role_key) | (role_prefs["role"] == "wikidata")]
        alignment_flags: List[bool] = [False] * len(df)
        boosts: List[float] = [0.0] * len(df)
        for idx, iri in enumerate(df["iri"].fillna("").astype(str)):
            for _, pref in role_specific.iterrows():
                pattern = str(pref.get("iri_pattern") or "")
                if pattern and re.search(pattern, iri, re.IGNORECASE):
                    if bool(pref.get("alignment_only")):
                        boosts[idx] = -0.5
                        alignment_flags[idx] = True
                    else:
                        try:
                            priority = float(pref.get("priority"))
                        except (TypeError, ValueError):
                            priority = None
                        boost = max(0, 2.5 - (priority * 0.5)) if priority is not None else 0
                        boosts[idx] = boost
                    break
        df["score"] += pd.Series(boosts, index=df.index)
        df["alignment_only"] = alignment_flags

    role_vocabs = vocab_tbl[vocab_tbl["role"] == role_key] if (role_key and not vocab_tbl.empty) else pd.DataFrame()
    if not role_vocabs.empty:
        host_pattern = "|".join(role_vocabs["host"].dropna().unique())
        slug_pattern = "|".join(role_vocabs["slug"].dropna().unique())
        label_pattern = "|".join(role_vocabs["label_tokens"].dropna().unique())

        if host_pattern:
            df.loc[df["iri"].str.contains(host_pattern, case=False, na=False), "score"] += 1
        if slug_pattern:
            df.loc[
                df["iri"].str.contains(slug_pattern, case=False, na=False)
                | df["ontology"].str.contains(slug_pattern, case=False, na=False),
                "score",
            ] += 1
        if label_pattern:
            df.loc[df["ontology"].str.contains(label_pattern, case=False, na=False), "score"] += 0.5

    # How well the provider said it matched. metasalmon adds this term
    # unconditionally (``match_type_enabled`` defaults to TRUE); a candidate
    # with no ``match_type`` scores as unclassified rather than aborting.
    if "match_type" in df.columns:
        df["score"] += df["match_type"].map(_match_type_score)
    else:
        df["score"] += MATCH_TYPE_WEIGHTS["other"]

    if query_tokens:
        def _label_overlap(lbl: str) -> float:
            lbl_tokens = {tok for tok in re.sub(r"[^a-z0-9]+", " ", str(lbl).lower()).split() if tok}
            return len(lbl_tokens.intersection(query_tokens)) * 0.2

        df["score"] += df["label"].apply(_label_overlap)

    if "alignment_only" not in df.columns:
        df["alignment_only"] = df["iri"].str.contains("wikidata.org", case=False, na=False)
    else:
        df["alignment_only"] = df["alignment_only"] | df["iri"].str.contains("wikidata.org", case=False, na=False)

    df = _apply_cross_source_agreement(df)
    return df.sort_values(by=["score", "source", "ontology", "label", "iri"], ascending=[False, True, True, True, True])


def sources_for_role(role: Optional[str]) -> List[str]:
    """
    Return the ordered default retrieval sources for a semantic role.

    Parameters
    ----------
    role
        One of variable, property, entity, unit, constraint,
        statistical_modifier, or method. Unknown and empty roles receive the
        generic default.

    Returns
    -------
    list of str
        Ordered source identifiers. These defaults apply only when callers omit
        sources; an explicit source list remains a strict allowlist.
    """
    if role is None or role == "":
        return ["smn", "gcdfo", "ols", "nvs"]

    role_key = str(role).lower()
    if role_key == "unit":
        return ["qudt", "nvs", "ols"]
    if role_key == "property":
        # v0.1.7 added qudt: its QuantityKind vocabulary is the canonical
        # source for the property half of an I-ADOPT decomposition.
        return ["smn", "gcdfo", "qudt", "nvs", "ols", "zooma"]
    if role_key == "entity":
        return ["smn", "gcdfo", "gbif", "worms", "bioportal", "ols"]
    if role_key == "method":
        return ["smn", "gcdfo", "bioportal", "ols", "zooma"]
    if role_key == "statistical_modifier":
        # The reviewed smn StatisticalModifierScheme first; OLS reaches the
        # I-ADOPT vocabulary and STATO (see data/ontology-preferences.csv).
        return ["smn", "ols"]
    if role_key == "variable":
        return ["smn", "gcdfo", "nvs", "ols", "zooma"]
    if role_key == "constraint":
        return ["smn", "gcdfo", "ols"]
    return ["smn", "gcdfo", "ols", "nvs"]


def _normalize_explicit_sources(sources: Sequence[str]) -> tuple[str, ...]:
    values = (sources,) if isinstance(sources, str) else sources
    return tuple(
        dict.fromkeys(
            str(source).strip().lower()
            for source in values
            if str(source).strip()
        )
    )


def find_terms(
    query: str,
    role: Optional[str] = None,
    sources: Optional[Sequence[str]] = None,
    expand_query: bool = True,
) -> pd.DataFrame:
    """
    Find ontology terms across OLS, NVS, and other vocab sources.
    """
    resolved_sources = (
        tuple(sources_for_role(role))
        if sources is None
        else _normalize_explicit_sources(sources)
    )
    if not resolved_sources or query is None or query == "":
        return _empty_terms(role)

    cache_key = (query, role, tuple(sorted(resolved_sources)), expand_query)
    if _cache_enabled() and cache_key in _term_cache:
        return _term_cache[cache_key].copy()

    queries = _expand_query(query, role) if expand_query else [query]
    results = []
    diagnostics = []
    for query_variant in queries:
        for src in resolved_sources:
            start_time = time.time()
            # Failures signalled by ``_safe_json`` are recorded here rather
            # than raised, so an optional enrichment request that fails does
            # not discard the rows that did resolve — but the source can no
            # longer report ``status="success"`` when it never heard back
            # (metasalmon 0.2.2). The sink is per source call, mirroring R's
            # call-local accumulator.
            failures: List[str] = []
            _search_failure_sinks.append(failures)
            try:
                if src == "smn":
                    res = _search_smn(query_variant, role)
                elif src == "gcdfo":
                    res = _search_gcdfo(query_variant, role)
                elif src == "ols":
                    res = _search_ols(query_variant, role)
                elif src == "nvs":
                    res = _search_nvs(query_variant, role)
                elif src == "zooma":
                    res = _search_zooma(query_variant, role)
                elif src == "bioportal":
                    res = _search_bioportal(query_variant, role)
                elif src == "qudt":
                    res = _search_qudt(query_variant, role)
                elif src == "gbif":
                    res = _search_gbif(query_variant, role)
                elif src == "worms":
                    res = _search_worms(query_variant, role)
                else:
                    res = _empty_terms(role)
                diagnostics.append(
                    {
                        "source": src,
                        "query": query_variant,
                        # A source that returned rows despite a failed side
                        # request is still a partial answer, not a clean
                        # success.
                        "status": "success" if not failures else "http_error",
                        "count": len(res),
                        "elapsed_secs": round(time.time() - start_time, 2),
                        "error": None if not failures else "; ".join(failures),
                    }
                )
                results.append(res)
            except Exception as exc:
                err_msg = redact_secrets(str(exc))
                if _is_timeout_error(err_msg):
                    warnings.warn(
                        f"Vocabulary API lookup timed out for source {src!r} "
                        f"while searching {query_variant!r}. {err_msg}",
                        RuntimeWarning,
                        stacklevel=2,
                    )
                diagnostics.append(
                    {
                        "source": src,
                        "query": query_variant,
                        "status": "error",
                        "count": 0,
                        "elapsed_secs": round(time.time() - start_time, 2),
                        "error": err_msg,
                    }
                )
                results.append(_empty_terms(role))
            finally:
                _search_failure_sinks.pop()

    combined = pd.concat(results, ignore_index=True) if results else _empty_terms(role)
    vocab_tbl = _load_iadopt_vocab()
    ranked = _score_and_rank_terms(combined, role, vocab_tbl, query)
    if "alignment_only" not in ranked.columns:
        ranked["alignment_only"] = False
    if "score" not in ranked.columns:
        ranked["score"] = pd.Series(dtype=float)
    if "agreement_sources" not in ranked.columns:
        ranked["agreement_sources"] = 1
    ranked = ranked[
        [
            "label",
            "iri",
            "source",
            "ontology",
            "role",
            "match_type",
            "definition",
            "score",
            "alignment_only",
            "agreement_sources",
        ]
        + [col for col in ["role_hints", "zooma_confidence", "zooma_annotator"] if col in ranked.columns]
    ]
    diag_df = pd.DataFrame(diagnostics)
    ranked.attrs["diagnostics"] = diag_df

    degraded = [
        entry for entry in diagnostics if entry["status"] in ("error", "http_error")
    ]
    if degraded:
        failed_sources = sorted({entry["source"] for entry in degraded})
        warnings.warn(
            "Vocabulary lookup was incomplete: "
            f"{', '.join(repr(source) for source in failed_sources)} did not "
            "answer. Treat an empty or short result as unknown rather than as "
            'an ontology gap. See result.attrs["diagnostics"] for per-source '
            "detail.",
            RuntimeWarning,
            stacklevel=2,
        )

    # A degraded lookup is never cached. Caching it would freeze an outage's
    # empty result for the rest of the session, so every later column would
    # inherit the same manufactured gap (metasalmon 0.2.2).
    if _cache_enabled() and not degraded:
        _term_cache[cache_key] = ranked.copy()
    return ranked


def benchmark_term_ranking_fixtures(
    fixture_path: Optional[str] = None,
    profiles: Optional[dict] = None,
    top_k: int = 3,
    include_details: bool = True,
    fixture_path_override: Optional[list] = None,
) -> dict:
    """
    Evaluate deterministic term ranking against versioned fixtures.

    Parameters
    ----------
    fixture_path
        JSON fixture path. Uses the packaged fixture path when omitted.
    profiles
        Named profile mapping included in the benchmark result.
    top_k
        Rank threshold used for top-k accuracy.
    include_details
        Include one result row per case when ``True``.
    fixture_path_override
        In-memory fixture list for tests.

    Returns
    -------
    dict
        Summary metrics, optional per-case results, and the supplied profiles.
    """
    if fixture_path_override is not None:
        fixtures = fixture_path_override
    else:
        if fixture_path is None:
            fixture_path = str(resources.files("metasalmonpy").joinpath("tests/fixtures/semantic-ranking-fixtures.json"))
        with open(fixture_path, "r", encoding="utf-8") as fp:
            fixtures = json.load(fp)
    profiles = profiles or {"baseline": None}
    vocab = _load_iadopt_vocab()
    summary_rows = []
    per_case_rows = []
    for profile_name, _profile in profiles.items():
        top1_ok = 0
        topk_ok = 0
        mrr_total = 0.0
        for case in fixtures:
            candidates = pd.DataFrame(case.get("candidates", []))
            ranked = _score_and_rank_terms(candidates, case.get("role"), vocab, case.get("query"))
            expected = case.get("expected", {})
            expected_top = expected.get("top", {})
            expected_id = expected_top.get("candidate_id")
            ids = ranked.get("candidate_id", pd.Series(range(len(ranked)))).astype(str).tolist()
            position = (ids.index(str(expected_id)) + 1) if expected_id is not None and str(expected_id) in ids else None
            case_top1 = position == 1
            case_topk = position is not None and position <= top_k
            top1_ok += int(case_top1)
            topk_ok += int(case_topk)
            mrr_total += (1.0 / position) if position else 0.0
            if include_details:
                per_case_rows.append(
                    {
                        "profile": profile_name,
                        "case_id": case.get("case_id"),
                        "top1_ok": case_top1,
                        "top_k_ok": case_topk,
                        "mrr": (1.0 / position) if position else 0.0,
                        "top1_position": position,
                    }
                )
        n = max(len(fixtures), 1)
        summary_rows.append(
            {
                "profile": profile_name,
                "top1_accuracy": top1_ok / n,
                "top_k_accuracy": topk_ok / n,
                "mrr": mrr_total / n,
                "case_count": len(fixtures),
            }
        )
    return {
        "summary": pd.DataFrame(summary_rows),
        "per_case": pd.DataFrame(per_case_rows),
        "profiles": profiles,
    }


__all__ = ["benchmark_term_ranking_fixtures", "find_terms", "sources_for_role"]
