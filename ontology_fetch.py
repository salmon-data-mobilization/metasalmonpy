"""
Fetch DFO Salmon Ontology with HTTP caching.

This module downloads the DFO Salmon Ontology using HTTP content negotiation
and implements ETag/Last-Modified caching for offline work and bandwidth reduction.
"""

import os
import tempfile
from typing import List, Optional
import requests


def fetch_salmon_ontology(
    url: str = "https://dfo-pacific-science.github.io/dfo-salmon-ontology/ontology/dfo-salmon.ttl",
    accept: str = "text/turtle, application/rdf+xml;q=0.8",
    cache_dir: Optional[str] = None,
    fallback_urls: Optional[List[str]] = None
) -> str:
    """
    Fetch the DFO Salmon Ontology with HTTP caching.

    Downloads the DFO Salmon Ontology using HTTP content negotiation and caches
    the response using ETag/Last-Modified headers when available. Supports fallback
    URLs if the primary URL fails.

    Parameters
    ----------
    url : str, default="https://dfo-pacific-science.github.io/dfo-salmon-ontology/ontology/dfo-salmon.ttl"
        Primary ontology URL
    accept : str, default="text/turtle, application/rdf+xml;q=0.8"
        Accept header for content negotiation
    cache_dir : str, optional
        Directory to store cached ontology and headers.
        Defaults to <temp>/metasalmonpy-ontology-cache
    fallback_urls : List[str], optional
        List of fallback URLs to try if primary fails.
        Defaults to ["https://w3id.org/gcdfo/salmon"]

    Returns
    -------
    str
        Path to the cached ontology file

    Raises
    ------
    RuntimeError
        If all URLs fail to fetch the ontology

    Examples
    --------
    >>> from metasalmonpy import fetch_salmon_ontology
    >>> ttl_path = fetch_salmon_ontology()
    >>> print(f"Ontology cached at: {ttl_path}")
    >>> # Read the ontology with rdflib or similar
    >>> with open(ttl_path, 'r') as f:
    ...     ontology_content = f.read()
    """
    if fallback_urls is None:
        fallback_urls = ["https://w3id.org/gcdfo/salmon"]

    if cache_dir is None:
        cache_dir = os.path.join(tempfile.gettempdir(), "metasalmonpy-ontology-cache")

    # Create cache directory
    os.makedirs(cache_dir, exist_ok=True)

    ttl_file = os.path.join(cache_dir, "dfo-salmon.ttl")
    etag_file = os.path.join(cache_dir, "etag.txt")
    lastmod_file = os.path.join(cache_dir, "last_modified.txt")

    # Build headers with conditional request if cached
    headers = {"Accept": accept}

    if os.path.exists(etag_file):
        with open(etag_file, 'r') as f:
            headers["If-None-Match"] = f.read().strip()

    if os.path.exists(lastmod_file):
        with open(lastmod_file, 'r') as f:
            headers["If-Modified-Since"] = f.read().strip()

    # Try primary URL and fallbacks
    urls = [url] + fallback_urls
    response = None
    last_error = None

    for attempt_url in urls:
        try:
            response = requests.get(attempt_url, headers=headers, timeout=15)

            # Success or Not Modified
            if response.status_code in (200, 304):
                break

            # Store error and try next URL
            last_error = f"HTTP {response.status_code}"
            response = None

        except requests.RequestException as exc:
            last_error = str(exc)
            response = None
            continue

    # All URLs failed
    if response is None:
        raise RuntimeError(
            f"Failed to fetch ontology from provided URLs: {', '.join(urls)}; "
            f"last error: {last_error}"
        )

    # Not Modified - use cached version
    if response.status_code == 304 and os.path.exists(ttl_file):
        return ttl_file

    # Success - save new content
    response.raise_for_status()

    with open(ttl_file, 'w', encoding='utf-8') as f:
        f.write(response.text)

    # Save cache headers
    etag = response.headers.get('ETag')
    if etag:
        with open(etag_file, 'w') as f:
            f.write(etag)

    last_modified = response.headers.get('Last-Modified')
    if last_modified:
        with open(lastmod_file, 'w') as f:
            f.write(last_modified)

    return ttl_file


__all__ = ['fetch_salmon_ontology']
