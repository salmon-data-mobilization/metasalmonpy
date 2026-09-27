"""``fetch_salmon_ontology()``, with ``requests.get`` stubbed so no request leaves
the process.

Each rule here was demonstrated failing on the function as it stood before the
change that introduced it, except the last, which this package already kept and
which is pinned here because metasalmon now keeps it too. metasalmon's
``tests/testthat/test-ontology-fetch.R`` pins the same rules on its side:

* hub **B-423** (Q71 clause 1, ruled by Brett on 2026-09-26: smn is the default
  starting point): the default url is smn's, with smn's fallback, as in
  metasalmon. The old default answered 404.
* hub **B-334** (metasalmon's twin is **B-333**): the default fallback belongs to
  the default url, so a call for another ontology is never answered by it.
* hub **B-336** (metasalmon's twin is **B-335**): each cached copy and its
  validators are keyed by the URL that returned them and the accept they were
  fetched under, and both packages name the files the same way.
* Q71 clause 2 (metasalmon's **B-422**): when every URL fails the call raises,
  even with a copy cached.
* The follow-ups that converged the two fetchers' remaining differences on
  2026-09-26: ``timeout_seconds`` with metasalmon's default of 30 bounds the
  connection and the read; a copy holds exactly the bytes the server sent, and
  is written atomically; and the default ``cache_dir`` is a persistent
  per-user cache.
"""

import inspect
import os
from pathlib import Path

import pytest
import requests

from metasalmonpy import ontology_fetch
from metasalmonpy.ontology_fetch import fetch_salmon_ontology

SMN = "https://w3id.org/smn/"
SMN_FALLBACK = "https://w3id.org/smn"
GCDFO = "https://w3id.org/gcdfo/salmon"
OLD_DEFAULT = "https://dfo-pacific-science.github.io/dfo-salmon-ontology/ontology/dfo-salmon.ttl"


def _answer(url, status, body="", etag=None, last_modified=None, content_type="text/turtle; charset=utf-8"):
    """An answer in the shape ``requests.get()`` returns.

    ``body`` is text, sent as its UTF-8 bytes, or ``bytes`` sent as given. The
    encoding is set from the headers the way requests' adapter sets it, so a
    text type with no charset reads as ISO-8859-1, as it does on the wire.
    """
    response = requests.Response()
    response.status_code = status
    response.url = url
    response._content = body if isinstance(body, bytes) else body.encode("utf-8")
    response.headers["Content-Type"] = content_type
    response.encoding = requests.utils.get_encoding_from_headers(response.headers)
    if etag is not None:
        response.headers["ETag"] = etag
    if last_modified is not None:
        response.headers["Last-Modified"] = last_modified
    return response


def _ok(url, body, etag=None, last_modified=None):
    """A route that always answers 200 with ``body``."""
    return lambda sent: _answer(url, 200, body, etag=etag, last_modified=last_modified)


def _unreachable(sent):
    """A route whose request never completes."""
    raise requests.ConnectionError("Could not resolve host (stub)")


def _304_to_any_validator(url, body, etag):
    """A route that answers 304 to any request carrying a validator."""

    def route(sent):
        if "If-None-Match" in sent or "If-Modified-Since" in sent:
            return _answer(url, 304)
        return _answer(url, 200, body, etag=etag)

    return route


def _fetch(monkeypatch, routes, timeouts=None, **kwargs):
    """Calls ``fetch_salmon_ontology(**kwargs)`` with ``requests.get`` answered
    by ``routes``, a mapping from each URL to a function of the headers the
    request carried. Returns what the call returned (or the exception it
    raised), the URLs it requested, and the headers each request carried. A
    ``timeouts`` list, if given, receives each request's ``timeout``.
    """
    urls, sent = [], []

    def get(url, headers=None, timeout=None, **_):
        carried = dict(headers or {})
        urls.append(url)
        sent.append(carried)
        if timeouts is not None:
            timeouts.append(timeout)
        if url not in routes:
            raise AssertionError(f"no route for {url}")
        return routes[url](carried)

    monkeypatch.setattr(ontology_fetch.requests, "get", get)
    try:
        value = fetch_salmon_ontology(**kwargs)
    except Exception as exc:  # noqa: BLE001 - the tests read what was raised
        value = exc
    return value, urls, sent


def _body(path):
    return Path(path).read_text(encoding="utf-8")


# --- B-423: the default --------------------------------------------------------


def test_the_default_url_is_smn_and_the_fallback_is_resolved_by_url():
    parameters = inspect.signature(fetch_salmon_ontology).parameters
    assert parameters["url"].default == SMN
    assert parameters["fallback_urls"].default is None


def test_a_bare_call_starts_at_smn_and_falls_back_to_smn(monkeypatch, tmp_path):
    value, urls, _ = _fetch(
        monkeypatch,
        {SMN: _unreachable, SMN_FALLBACK: _ok(SMN_FALLBACK, "SMN BODY")},
        cache_dir=str(tmp_path),
    )
    assert urls == [SMN, SMN_FALLBACK]
    assert _body(value) == "SMN BODY"
    assert OLD_DEFAULT not in urls


def test_naming_the_default_url_is_the_same_as_omitting_it(monkeypatch, tmp_path):
    value, urls, _ = _fetch(
        monkeypatch,
        {SMN: _unreachable, SMN_FALLBACK: _ok(SMN_FALLBACK, "SMN BODY")},
        url=SMN,
        cache_dir=str(tmp_path),
    )
    assert urls == [SMN, SMN_FALLBACK]
    assert _body(value) == "SMN BODY"


# --- B-334: the default fallback belongs to the default url ---------------------


@pytest.mark.parametrize(
    "url,other_url,other_body",
    [
        # What the function did before B-423 and B-334: asked for smn, whose
        # url failed, it went on to its gcdfo default and returned gcdfo.
        (SMN, GCDFO, "GCDFO BODY"),
        # metasalmon's B-333 case, which the smn default makes the live one.
        (GCDFO, SMN_FALLBACK, "SMN BODY"),
    ],
)
def test_a_failed_url_is_never_answered_by_another_ontology(monkeypatch, tmp_path, url, other_url, other_body):
    routes = {SMN: _unreachable, SMN_FALLBACK: _unreachable, GCDFO: _unreachable}
    routes[other_url] = _ok(other_url, other_body)
    value, urls, _ = _fetch(monkeypatch, routes, url=url, cache_dir=str(tmp_path))
    assert isinstance(value, RuntimeError), value
    assert str(value).startswith(f"Failed to fetch ontology from provided URLs: {url}")
    assert other_url not in urls
    assert os.listdir(tmp_path) == []


def test_named_fallbacks_are_tried_for_any_url_and_an_empty_list_names_none(monkeypatch, tmp_path):
    mirror = "https://example.org/gcdfo.ttl"
    value, _, _ = _fetch(
        monkeypatch,
        {GCDFO: _unreachable, mirror: _ok(mirror, "GCDFO BODY")},
        url=GCDFO,
        cache_dir=str(tmp_path / "a"),
        fallback_urls=[mirror],
    )
    assert _body(value) == "GCDFO BODY"
    value, urls, _ = _fetch(
        monkeypatch,
        {SMN: _unreachable, SMN_FALLBACK: _ok(SMN_FALLBACK, "SMN BODY")},
        cache_dir=str(tmp_path / "b"),
        fallback_urls=[],
    )
    assert isinstance(value, RuntimeError)
    assert urls == [SMN]


# --- B-336: one copy per URL and representation ---------------------------------


def test_two_ontologies_fetched_into_one_cache_dir_keep_a_copy_each(monkeypatch, tmp_path):
    cache_dir = str(tmp_path)
    smn, _, _ = _fetch(monkeypatch, {SMN: _ok(SMN, "SMN BODY", etag='"smn-1"')}, cache_dir=cache_dir)
    gcdfo, _, sent = _fetch(
        monkeypatch,
        {GCDFO: _ok(GCDFO, "GCDFO BODY", etag='"gcdfo-1"')},
        url=GCDFO,
        cache_dir=cache_dir,
        fallback_urls=[],
    )
    assert smn != gcdfo
    assert _body(smn) == "SMN BODY"
    assert _body(gcdfo) == "GCDFO BODY"
    # The gcdfo request carried no validator: smn's ETag is smn's.
    assert "If-None-Match" not in sent[0]

    # And smn's own validator still travels with smn.
    again, _, sent = _fetch(monkeypatch, {SMN: _304_to_any_validator(SMN, "CHANGED", '"smn-2"')}, cache_dir=cache_dir)
    assert sent[0]["If-None-Match"] == '"smn-1"'
    assert again == smn
    assert _body(again) == "SMN BODY"


def test_one_url_fetched_under_two_accepts_keeps_a_copy_each(monkeypatch, tmp_path):
    cache_dir = str(tmp_path)
    turtle, _, _ = _fetch(
        monkeypatch, {SMN: _ok(SMN, "TURTLE BODY", etag='"ttl-1"')}, cache_dir=cache_dir, accept="text/turtle"
    )
    rdfxml, _, sent = _fetch(
        monkeypatch, {SMN: _ok(SMN, "RDFXML BODY", etag='"rdf-1"')}, cache_dir=cache_dir, accept="application/rdf+xml"
    )
    assert turtle != rdfxml
    assert _body(turtle) == "TURTLE BODY"
    assert _body(rdfxml) == "RDFXML BODY"
    assert "If-None-Match" not in sent[0]


def test_a_fallbacks_validators_never_answer_for_the_url(monkeypatch, tmp_path):
    # The url fails and its fallback answers; the fallback's copy and ETag are
    # the fallback's. The next call's url answers 304 to any validator, so if
    # the fallback's ETag were sent to it, the fallback's body would come back
    # as the url's -- which is what used to happen.
    cache_dir = str(tmp_path)
    first, _, _ = _fetch(
        monkeypatch,
        {SMN: _unreachable, SMN_FALLBACK: _ok(SMN_FALLBACK, "FALLBACK BODY", etag='"fb-1"')},
        cache_dir=cache_dir,
    )
    assert _body(first) == "FALLBACK BODY"
    second, _, sent = _fetch(monkeypatch, {SMN: _304_to_any_validator(SMN, "URL BODY", '"url-1"')}, cache_dir=cache_dir)
    assert "If-None-Match" not in sent[0]
    assert _body(second) == "URL BODY"
    assert second != first
    assert _body(first) == "FALLBACK BODY"


def test_a_refreshed_copy_replaces_its_validators_rather_than_keeping_stale_ones(monkeypatch, tmp_path):
    cache_dir = str(tmp_path)
    _fetch(
        monkeypatch,
        {SMN: _ok(SMN, "V1", etag='"v1"', last_modified="Mon, 01 Jan 2024 00:00:00 GMT")},
        cache_dir=cache_dir,
    )
    # A 200 with no validators: the old ones described the old body.
    _fetch(monkeypatch, {SMN: _ok(SMN, "V2")}, cache_dir=cache_dir)
    third, _, sent = _fetch(monkeypatch, {SMN: _ok(SMN, "V3")}, cache_dir=cache_dir)
    assert "If-None-Match" not in sent[0]
    assert "If-Modified-Since" not in sent[0]
    assert _body(third) == "V3"


def test_a_304_with_no_cached_copy_is_that_urls_failure_not_an_empty_copy(monkeypatch, tmp_path):
    # This used to write the 304's empty body as the copy and return it.
    always_304 = lambda sent: _answer(SMN, 304)  # noqa: E731
    value, _, _ = _fetch(monkeypatch, {SMN: always_304}, cache_dir=str(tmp_path / "a"), fallback_urls=[])
    assert isinstance(value, RuntimeError), value
    assert str(value).endswith("last error: HTTP 304")
    # ...so the next url is tried.
    value, _, _ = _fetch(
        monkeypatch,
        {SMN: always_304, SMN_FALLBACK: _ok(SMN_FALLBACK, "SMN BODY")},
        cache_dir=str(tmp_path / "b"),
    )
    assert _body(value) == "SMN BODY"


def test_the_cache_file_names_are_the_ones_metasalmon_writes(monkeypatch, tmp_path):
    # The first 16 hexadecimal digits of the SHA-256 of the UTF-8 bytes of the
    # url, a newline and the accept. metasalmon's test pins the same four.
    def key(url, accept):
        return Path(ontology_fetch._cache_entry(str(tmp_path), url, accept)["body"]).stem

    assert key(SMN, "text/turtle, application/rdf+xml;q=0.8") == "5891e28fd43e0292"
    assert key(SMN_FALLBACK, "text/turtle, application/rdf+xml;q=0.8") == "5188e73de1bcc279"
    assert key(SMN, "application/rdf+xml") == "8fa14febd5b318d1"
    assert key("https://example.org/ontologie/unit\u00e9", "text/turtle") == "6bf05a094db6a6b1"

    entry = ontology_fetch._cache_entry("cache", SMN, "text/turtle, application/rdf+xml;q=0.8")
    assert [os.path.basename(entry[part]) for part in ("body", "etag", "last_modified")] == [
        "5891e28fd43e0292.ttl",
        "5891e28fd43e0292.etag",
        "5891e28fd43e0292.last_modified",
    ]

    value, _, _ = _fetch(
        monkeypatch,
        {SMN: _ok(SMN, "SMN BODY", etag='"e"', last_modified="Mon, 01 Jan 2024 00:00:00 GMT")},
        cache_dir=str(tmp_path),
    )
    assert os.path.basename(value) == "5891e28fd43e0292.ttl"
    assert sorted(os.listdir(tmp_path)) == [
        "5891e28fd43e0292.etag",
        "5891e28fd43e0292.last_modified",
        "5891e28fd43e0292.ttl",
    ]


# --- Q71 clause 2: every URL failing raises -------------------------------------


def test_every_url_failing_raises_even_when_a_copy_is_cached(monkeypatch, tmp_path):
    cache_dir = str(tmp_path)
    cached, _, _ = _fetch(monkeypatch, {SMN: _ok(SMN, "SMN BODY", etag='"smn-1"')}, cache_dir=cache_dir)
    value, _, _ = _fetch(monkeypatch, {SMN: _unreachable, SMN_FALLBACK: _unreachable}, cache_dir=cache_dir)
    assert isinstance(value, RuntimeError)
    assert str(value) == (
        "Failed to fetch ontology from provided URLs: https://w3id.org/smn/, https://w3id.org/smn; "
        "last error: Could not resolve host (stub)"
    )
    # The copy stays where it was; it is only not returned.
    assert _body(cached) == "SMN BODY"

    # An HTTP status is named as metasalmon names it.
    value, _, _ = _fetch(
        monkeypatch,
        {SMN: lambda sent: _answer(SMN, 404), SMN_FALLBACK: lambda sent: _answer(SMN_FALLBACK, 503)},
        cache_dir=cache_dir,
    )
    assert str(value) == (
        "Failed to fetch ontology from provided URLs: https://w3id.org/smn/, https://w3id.org/smn; "
        "last error: HTTP 503"
    )


# --- The 2026-09-26 follow-ups ---------------------------------------------------


def test_timeout_seconds_bounds_the_connection_and_the_read(monkeypatch, tmp_path):
    # metasalmon's `timeout_seconds` defaults to 30 and bounds both the
    # connection and the transfer; this package used a fixed 15 s.
    assert inspect.signature(fetch_salmon_ontology).parameters["timeout_seconds"].default == 30
    timeouts = []
    _fetch(monkeypatch, {SMN: _ok(SMN, "SMN BODY")}, timeouts=timeouts, cache_dir=str(tmp_path / "a"))
    assert timeouts == [(30, 30)]

    timeouts = []
    _fetch(
        monkeypatch,
        {SMN: _unreachable, SMN_FALLBACK: _ok(SMN_FALLBACK, "SMN BODY")},
        timeouts=timeouts,
        cache_dir=str(tmp_path / "b"),
        timeout_seconds=5,
    )
    assert timeouts == [(5, 5), (5, 5)]


@pytest.mark.parametrize(
    "name,body",
    [
        # The four bodies metasalmon's twin test sends, each with no charset.
        ("no_final_newline", "@prefix smn: <https://w3id.org/smn/> .\nsmn:Unit\u00e9 a smn:Thing .".encode("utf-8")),
        ("latin1", b"caf\xe9\n"),
        ("crlf", b"a\r\nb\r\n"),
        ("empty", b""),
    ],
)
def test_a_copy_holds_exactly_the_bytes_the_server_sent(monkeypatch, tmp_path, name, body):
    # The copy used to be `response.text` written as UTF-8, so a text type with
    # no charset was decoded as ISO-8859-1 and re-encoded: a UTF-8 e-acute became
    # the four bytes of its ISO-8859-1 misreading.
    value, _, _ = _fetch(
        monkeypatch,
        {SMN: lambda sent: _answer(SMN, 200, body, content_type="text/turtle")},
        cache_dir=str(tmp_path),
    )
    assert Path(value).read_bytes() == body


def test_a_validator_is_stored_as_the_headers_bytes_and_a_newline(monkeypatch, tmp_path):
    # The file metasalmon writes, so a cache_dir either package writes holds the
    # same files.
    _fetch(
        monkeypatch,
        {SMN: _ok(SMN, "SMN BODY", etag='"e"', last_modified="Mon, 01 Jan 2024 00:00:00 GMT")},
        cache_dir=str(tmp_path),
    )
    entry = ontology_fetch._cache_entry(str(tmp_path), SMN, "text/turtle, application/rdf+xml;q=0.8")
    assert Path(entry["etag"]).read_bytes() == b'"e"\n'
    assert Path(entry["last_modified"]).read_bytes() == b"Mon, 01 Jan 2024 00:00:00 GMT\n"


class _BodyThatBreaks(requests.Response):
    """A 200 whose body cannot be read, as when a connection drops mid-body."""

    @property
    def content(self):
        raise requests.exceptions.ChunkedEncodingError("connection broken while reading the body (stub)")


def test_a_failed_store_leaves_the_previous_copy_whole(monkeypatch, tmp_path):
    # The copy used to be written by opening it for writing, which empties it
    # before the new bytes are in hand; it now goes through
    # atomic_io.atomic_write(), a same-directory temporary and a rename, as
    # metasalmon's writeBin() + file.rename() does.
    cache_dir = str(tmp_path)
    cached, _, _ = _fetch(monkeypatch, {SMN: _ok(SMN, "OLD BODY", etag='"old"')}, cache_dir=cache_dir)

    def broken(sent):
        response = _BodyThatBreaks()
        response.status_code = 200
        response.url = SMN
        return response

    value, _, _ = _fetch(monkeypatch, {SMN: broken}, cache_dir=cache_dir)
    assert isinstance(value, requests.exceptions.ChunkedEncodingError), value
    assert Path(cached).read_bytes() == b"OLD BODY"

    # A rename that fails leaves the old copy and no temporary behind.
    def refuse(source, destination):
        raise OSError("rename refused (stub)")

    monkeypatch.setattr(os, "replace", refuse)
    value, _, _ = _fetch(monkeypatch, {SMN: _ok(SMN, "NEW BODY")}, cache_dir=cache_dir)
    assert isinstance(value, OSError), value
    assert Path(cached).read_bytes() == b"OLD BODY"
    assert [name for name in os.listdir(cache_dir) if name.startswith(".")] == []


def test_the_default_cache_is_a_persistent_per_user_cache(monkeypatch, tmp_path):
    # The per-user cache locations R's tools::R_user_dir() uses for metasalmon,
    # under metasalmonpy/ontology, standard library only. XDG_CACHE_HOME wins on
    # every platform when it is set, as it does in R.
    home = str(tmp_path / "home")
    default = ontology_fetch._default_cache_dir
    tail = os.path.join("metasalmonpy", "ontology")
    assert default(platform="linux", environ={}, home=home) == os.path.join(home, ".cache", tail)
    assert default(platform="freebsd14", environ={}, home=home) == os.path.join(home, ".cache", tail)
    assert default(platform="darwin", environ={}, home=home) == os.path.join(home, "Library", "Caches", tail)
    assert default(platform="win32", environ={"LOCALAPPDATA": "C:/Users/me/AppData/Local"}, home=home) == os.path.join(
        "C:/Users/me/AppData/Local", tail
    )
    assert default(platform="win32", environ={}, home=home) == os.path.join(home, "AppData", "Local", tail)
    for platform in ("linux", "darwin", "win32"):
        assert default(platform=platform, environ={"XDG_CACHE_HOME": "/x/cache", "LOCALAPPDATA": "C:/L"}, home=home) == (
            os.path.join("/x/cache", tail)
        )
    # An empty XDG_CACHE_HOME counts as unset, as R's nzchar() test has it.
    assert default(platform="linux", environ={"XDG_CACHE_HOME": ""}, home=home) == os.path.join(home, ".cache", tail)

    # A bare call caches there, not under the temporary directory.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    value, _, _ = _fetch(monkeypatch, {SMN: _ok(SMN, "SMN BODY")})
    assert value == str(tmp_path / "xdg" / "metasalmonpy" / "ontology" / "5891e28fd43e0292.ttl")
