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
import sys
from typing import Dict, List, Mapping, Optional

import requests

from .atomic_io import atomic_write

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
    fallback_urls: Optional[List[str]] = None,
    timeout_seconds: float = 30,
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
        Directory to store cached ontology and headers. Defaults to a
        persistent per-user cache, in the locations R's ``tools::R_user_dir()``
        uses for metasalmon's: ``$XDG_CACHE_HOME/metasalmonpy/ontology`` when
        ``XDG_CACHE_HOME`` is set, and otherwise
        ``~/Library/Caches/metasalmonpy/ontology`` on macOS,
        ``%LOCALAPPDATA%\\metasalmonpy\\ontology`` on Windows and
        ``~/.cache/metasalmonpy/ontology`` elsewhere.
    fallback_urls : List[str], optional
        URLs tried in order if the primary URL fails. ``None`` (the default)
        tries ``["https://w3id.org/smn"]`` when ``url`` is the default and none
        otherwise: that fallback serves smn, so a call for any other ontology
        must not be answered by it. ``[]`` tries none.
    timeout_seconds : float, default=30
        Timeout in seconds for each HTTP request, as metasalmon's
        ``timeout_seconds``. It bounds both the connection and each wait for
        the server's bytes.

    Returns
    -------
    str
        Path to the cached copy that the answering URL returned, holding exactly
        the bytes that URL sent: nothing is decoded or re-encoded.

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
        cache_dir = _default_cache_dir()

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

        # One timeout for the connection and one for the read, both
        # ``timeout_seconds``, as metasalmon gives curl its connect timeout and
        # its transfer timeout. This used to be a fixed 15 s. requests has no
        # bound on a whole transfer, so its read timeout bounds each wait for
        # bytes where curl's bounds the transfer: a server that keeps sending
        # slowly is cut off by metasalmon and not here.
        try:
            response = requests.get(
                attempt_url,
                headers=headers,
                timeout=(timeout_seconds, timeout_seconds),
            )
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


def _default_cache_dir(
    platform: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
    home: Optional[str] = None,
) -> str:
    """The persistent per-user cache ``fetch_salmon_ontology()`` uses by default.

    The locations R's ``tools::R_user_dir(which = "cache")`` uses for
    metasalmon's cache, in its order, with this package's own subdirectory:
    ``XDG_CACHE_HOME`` when it is set and not empty, on every platform as in R;
    otherwise ``LOCALAPPDATA`` on Windows (``~/AppData/Local`` if that is unset),
    ``~/Library/Caches`` on macOS and ``~/.cache`` on Linux and every other
    POSIX system. Standard library only, because a dependency for this would
    breach the core-dependency rule. The default used to be
    ``metasalmonpy-ontology-cache`` under the system temporary directory, where
    the copies an ETag lets a later session reuse do not survive a reboot.
    ``R_USER_CACHE_DIR``, R's own override, has no counterpart here.
    """
    platform = sys.platform if platform is None else platform
    environ = os.environ if environ is None else environ
    home = os.path.expanduser("~") if home is None else home
    base = environ.get("XDG_CACHE_HOME", "")
    if not base:
        if platform.startswith("win"):
            base = environ.get("LOCALAPPDATA", "") or os.path.join(home, "AppData", "Local")
        elif platform == "darwin":
            base = os.path.join(home, "Library", "Caches")
        else:
            base = os.path.join(home, ".cache")
    return os.path.join(base, "metasalmonpy", "ontology")


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
    ``None`` when the file is absent or that line is empty. The bytes are read
    as ISO-8859-1, the encoding requests gives a header value, so a value is
    sent back as the bytes that arrived."""
    if not os.path.exists(path):
        return None
    with open(path, "rb") as handle:
        value = handle.readline().decode("latin-1").strip(" \t\r\n")
    return value or None


def _store_validator(value: Optional[str], path: str) -> None:
    """Writes a validator as metasalmon does: the header's bytes and a newline,
    in binary, so the file is the same on every platform and from either
    package. requests decodes a header value as ISO-8859-1, so encoding it that
    way gives back the bytes that arrived; a value that cannot be encoded so,
    which the wire cannot deliver, is not stored, and the next request for the
    copy is then unconditional."""
    if not value:
        return
    try:
        data = value.encode("latin-1")
    except UnicodeEncodeError:
        return
    with open(path, "wb") as handle:
        handle.write(data + b"\n")


def _store(entry: Dict[str, str], response: requests.Response) -> str:
    """Stores a 200 answer as ``entry``'s copy, with the validators that came
    with it and no others, and returns the copy's path.

    The copy is the body's bytes exactly as the server sent them
    (``response.content``), written through ``atomic_io.atomic_write()``: a
    same-directory temporary and a rename, so an aborted call leaves the
    previous copy whole, as metasalmon's ``writeBin()`` + ``file.rename()``
    does. It used to be ``response.text`` written as UTF-8 by opening the copy
    for writing, which emptied the copy before the new bytes were in hand and
    decoded a text type sent with no charset as ISO-8859-1.
    """
    body = response.content

    # The old validators describe the old body, so they go first: a failure
    # part-way through leaves a copy with no validators, which is fetched in
    # full next time, rather than a new body paired with an old body's
    # validators.
    for path in (entry["etag"], entry["last_modified"]):
        if os.path.exists(path):
            os.remove(path)

    atomic_write(body, entry["body"])

    _store_validator(response.headers.get("ETag"), entry["etag"])
    _store_validator(response.headers.get("Last-Modified"), entry["last_modified"])

    return entry["body"]


__all__ = ['fetch_salmon_ontology']
