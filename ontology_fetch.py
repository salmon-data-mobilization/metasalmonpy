"""
Fetch the Salmon Domain Ontology with HTTP caching.

This module downloads an ontology -- by default the Salmon Domain Ontology
(smn), as metasalmon's ``fetch_salmon_ontology()`` does -- using HTTP content
negotiation, and caches each response with its ETag/Last-Modified validators so
that an unchanged ontology is not downloaded again. A call that cannot reach any
of its URLs raises rather than returning a copy it could not refresh.
"""

import hashlib
import os
import tempfile
from typing import Dict, List, Optional

import requests

# The default url and the fallback that belongs to it. Both serve smn, the
# ontology both packages search first (hub B-423; Q71 clause 1, ruled by Brett
# on 2026-09-26: smn is the default starting point, so this package moves). The
# default used to be a gcdfo Turtle file at a GitHub Pages path that no longer
# exists -- it answered 404 when measured on 2026-09-25 and again on 2026-09-26
# -- so every default call made one failing request before its gcdfo fallback
# answered.
_DEFAULT_URL = "https://w3id.org/smn/"
_DEFAULT_FALLBACK_URLS = ("https://w3id.org/smn",)


def fetch_salmon_ontology(
    url: str = _DEFAULT_URL,
    accept: str = "text/turtle, application/rdf+xml;q=0.8",
    cache_dir: Optional[str] = None,
    fallback_urls: Optional[List[str]] = None
) -> str:
    """
    Fetch the Salmon Domain Ontology with HTTP caching.

    Downloads an ontology, by default the Salmon Domain Ontology (smn), using
    HTTP content negotiation, and caches each response with its
    ETag/Last-Modified validators when the server sends them. Supports fallback
    URLs if the primary URL fails.

    Each copy is cached under the URL that returned it and the ``accept`` it was
    fetched under: its file name is the first 16 hexadecimal digits of the
    SHA-256 of the URL, a newline and ``accept`` (as UTF-8), with the extension
    ``.ttl`` whatever the representation, and its validators sit beside it with
    the extensions ``.etag`` and ``.last_modified``. Fetches of different URLs
    or representations into one ``cache_dir`` therefore never overwrite each
    other, a conditional request carries only the validators that the URL it is
    sent to returned under the same ``accept``, and a 304 answer returns that
    URL's own copy. metasalmon names its cache files the same way.

    Parameters
    ----------
    url : str, default="https://w3id.org/smn/"
        Primary ontology URL: the canonical SMN namespace root, as in metasalmon.
    accept : str, default="text/turtle, application/rdf+xml;q=0.8"
        Accept header for content negotiation
    cache_dir : str, optional
        Directory to store cached ontology and headers.
        Defaults to <temp>/metasalmonpy-ontology-cache
    fallback_urls : List[str], optional
        URLs tried in order if the primary URL fails. ``None`` (the default)
        tries ``["https://w3id.org/smn"]`` when ``url`` is the default and none
        otherwise: that fallback serves smn, so a call for any other ontology
        must not be answered by it. ``[]`` tries none.

    Returns
    -------
    str
        Path to the cached copy that the answering URL returned

    Raises
    ------
    RuntimeError
        If every URL fails, naming the URLs and the last failure. This holds
        even when a copy fetched by an earlier call is cached: a copy that could
        not be refreshed is not returned, and it is left on disk.

    Examples
    --------
    >>> from metasalmonpy import fetch_salmon_ontology
    >>> ttl_path = fetch_salmon_ontology()
    >>> print(f"Ontology cached at: {ttl_path}")
    >>> # Read the ontology with rdflib or similar
    >>> with open(ttl_path, 'r') as f:
    ...     ontology_content = f.read()
    """
    # The default fallback belongs to the default url alone. It used to follow
    # any url, so a call for smn whose url failed returned gcdfo's body with no
    # warning (hub B-334; metasalmon's twin is B-333, and the two now apply one
    # rule).
    if fallback_urls is None:
        fallback_urls = list(_DEFAULT_FALLBACK_URLS) if url == _DEFAULT_URL else []

    if cache_dir is None:
        cache_dir = os.path.join(tempfile.gettempdir(), "metasalmonpy-ontology-cache")

    # Create cache directory
    os.makedirs(cache_dir, exist_ok=True)

    urls = [url] + list(fallback_urls)
    last_error = None

    for attempt_url in urls:
        entry = _cache_entry(cache_dir, attempt_url, accept)

        # A validator travels only with the copy it describes: the one this URL
        # returned under this accept (hub B-336). One set of validators used to
        # be shared by every URL and representation in ``cache_dir``, so a 304
        # could answer for another URL's or another representation's body.
        headers = {"Accept": accept}
        if os.path.exists(entry["body"]):
            etag = _read_validator(entry["etag"])
            if etag:
                headers["If-None-Match"] = etag
            last_modified = _read_validator(entry["last_modified"])
            if last_modified:
                headers["If-Modified-Since"] = last_modified

        try:
            response = requests.get(attempt_url, headers=headers, timeout=15)
        except requests.RequestException as exc:
            last_error = str(exc)
            continue

        if response.status_code == 200:
            return _store(entry, response)
        # A 304 can only confirm a copy this URL returned. With none cached it
        # is this URL's failure and the next URL is tried; it used to write the
        # 304's empty body as the copy and return it.
        if response.status_code == 304 and os.path.exists(entry["body"]):
            return entry["body"]
        last_error = f"HTTP {response.status_code}"

    # Every URL failed. A copy cached by an earlier call is not returned
    # (Q71 clause 2, ruled by Brett on 2026-09-26, and the rule this package
    # always kept; metasalmon now raises too, as its B-422).
    raise RuntimeError(
        f"Failed to fetch ontology from provided URLs: {', '.join(urls)}; "
        f"last error: {last_error}"
    )


def _cache_entry(cache_dir: str, url: str, accept: str) -> Dict[str, str]:
    """Where one cached copy and its validators live (hub B-336).

    The name is derived from the URL as requested (before any redirect) and
    the ``accept`` it was requested under, so no two URLs or representations
    share a file. The key is the first 16 hexadecimal digits -- 64 bits, which
    keeps a deep cache path well inside Windows' 260-character limit and makes a
    collision among the handful of URLs one directory holds negligible -- of
    the SHA-256 of the UTF-8 bytes of the URL, a newline and the accept.
    metasalmon's ``.ms_ontology_cache_entry()`` (``R/ontology_fetch.R``)
    computes the same name, and ``tests/test_ontology_fetch.py`` pins four
    inputs its twin also pins.
    """
    key = hashlib.sha256(f"{url}\n{accept}".encode("utf-8")).hexdigest()[:16]
    return {
        "body": os.path.join(cache_dir, f"{key}.ttl"),
        "etag": os.path.join(cache_dir, f"{key}.etag"),
        "last_modified": os.path.join(cache_dir, f"{key}.last_modified"),
    }


def _read_validator(path: str) -> Optional[str]:
    """A stored validator, read as metasalmon's ``.ms_read_cached_header()``
    reads one: the first line, trimmed of what R's ``trimws()`` trims, or
    ``None`` when the file is absent or that line is empty."""
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        value = handle.readline().strip(" \t\r\n")
    return value or None


def _store(entry: Dict[str, str], response: requests.Response) -> str:
    """Stores a 200 answer as ``entry``'s copy, with the validators that came
    with it and no others, and returns the copy's path."""
    # The old validators describe the old body, so they go first: a failure
    # part-way through leaves a copy with no validators, which is fetched in
    # full next time, rather than a new body paired with an old body's
    # validators.
    for path in (entry["etag"], entry["last_modified"]):
        if os.path.exists(path):
            os.remove(path)

    with open(entry["body"], 'w', encoding='utf-8') as f:
        f.write(response.text)

    etag = response.headers.get('ETag')
    if etag:
        with open(entry["etag"], 'w', encoding='utf-8') as f:
            f.write(etag)

    last_modified = response.headers.get('Last-Modified')
    if last_modified:
        with open(entry["last_modified"], 'w', encoding='utf-8') as f:
            f.write(last_modified)

    return entry["body"]


__all__ = ['fetch_salmon_ontology']
