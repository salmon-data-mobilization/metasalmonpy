"""Public catalogue capture contract: bounded evidence, never approval or overwrite."""

import hashlib
import io
import json
import socket
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
import urllib.request

import pytest

from metasalmonpy import catalogue_discovery as discovery


STAMP = "2026-09-30T16:00:00+00:00"
QUERY = 'text:Fraser AND text:sockeye AND title:"stock recruit"'


@pytest.fixture(autouse=True)
def no_live_socket_in_catalogue_fixtures(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("offline catalogue fixture attempted a live connection")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


def page_bytes(found, start, ids):
    return json.dumps({"response": {"numFound": found, "start": start,
                                   "docs": [{"id": pid, "title": "Fraser sockeye"}
                                            for pid in ids]}}).encode()


def capture(tmp_path, fetch, **kwargs):
    return discovery.capture_catalogue_query(
        QUERY, tmp_path / "capture", fetch=fetch, captured_at=STAMP, **kwargs)


@pytest.mark.parametrize("catalogue,endpoint", [
    ("knb", "https://knb.ecoinformatics.org/knb/d1/mn/v2/query/solr/"),
    ("dataone", "https://cn.dataone.org/cn/v2/query/solr/"),
])
def test_catalogue_endpoint_query_and_pending_receipt(tmp_path, catalogue, endpoint):
    calls = []

    def fetch(url, timeout, max_bytes):
        calls.append((url, timeout, max_bytes))
        return page_bytes(1, 0, ["doi:10.5063/TM78J7"])

    result = capture(tmp_path, fetch, catalogue=catalogue)
    url, timeout, max_bytes = calls[0]
    parts = urlsplit(url)
    assert url.split("?")[0] == endpoint
    assert parts.scheme == "https" and parts.username is None and parts.password is None
    params = parse_qs(parts.query)
    assert params["q"] == [QUERY]
    assert params["fq"] == ["formatType:METADATA"]
    assert params["sort"] == ["id asc"]
    assert params["start"] == ["0"] and params["rows"] == ["50"]
    assert params["wt"] == ["json"]
    assert "obsoletes" in params["fl"][0] and "obsoletedBy" in params["fl"][0]
    assert timeout == 30 and max_bytes == 2_000_000
    assert result["catalogue"] == catalogue and result["endpoint"] == endpoint
    assert result["captured_at"] == STAMP
    assert result["annotation_status"] == "pending"
    assert result["independent_dataset_count"] is None
    assert result["transactional_snapshot"] is False
    saved = json.loads((tmp_path / "capture" / "capture.json").read_text())
    assert saved == result
    raw = (tmp_path / "capture" / result["pages"][0]["file"]).read_bytes()
    assert result["pages"][0]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["pages"][0]["bytes"] == len(raw)


@pytest.mark.parametrize("max_records,expected,complete", [(3, 3, False), (7, 5, True)])
def test_pagination_respects_record_bound_and_reports_truncation(tmp_path, max_records,
                                                              expected, complete):
    calls = []
    ids = ["a", "b", "c", "d", "e"]

    def fetch(url, timeout, max_bytes):
        params = parse_qs(urlsplit(url).query)
        start, rows = int(params["start"][0]), int(params["rows"][0])
        calls.append((start, rows))
        return page_bytes(5, start, ids[start:start + rows])

    result = capture(tmp_path, fetch, max_records=max_records, page_size=2)
    assert result["captured_metadata_records"] == expected
    assert result["reported_metadata_matches"] == 5
    assert result["complete_for_reported_count"] is complete
    assert [x["id"] for x in result["records"]] == ids[:expected]
    assert calls == ([(0, 2), (2, 1)] if max_records == 3 else [(0, 2), (2, 2), (4, 2)])


def test_zero_matches_is_a_query_receipt_with_no_independence_claim(tmp_path):
    result = capture(tmp_path, lambda *args: page_bytes(0, 0, []))
    assert result["reported_metadata_matches"] == result["captured_metadata_records"] == 0
    assert result["records"] == []
    assert result["complete_for_reported_count"] is True
    assert len(result["pages"]) == 1
    assert result["independent_dataset_count"] is None


@pytest.mark.parametrize("second,error", [
    (page_bytes(4, 1, ["b"]), "changed"),
    (page_bytes(3, 1, ["a"]), "repeated"),
    (page_bytes(3, 1, []), "Empty page"),
    (page_bytes(3, 0, ["b"]), "paging"),
    (page_bytes(3, 1, ["b", "c"]), "paging"),
])
def test_unstable_or_malformed_second_page_preserves_raw_failure(tmp_path, second, error):
    replies = iter([page_bytes(3, 0, ["a"]), second])
    with pytest.raises(ValueError, match=error):
        capture(tmp_path, lambda *args: next(replies), page_size=1)
    incomplete = tmp_path / "capture.incomplete"
    assert not (tmp_path / "capture").exists()
    assert (incomplete / "page-0000.json").read_bytes() == page_bytes(3, 0, ["a"])
    assert (incomplete / "page-0001.json").read_bytes() == second
    assert not (incomplete / "capture.json").exists()
    failure = json.loads((incomplete / "failure.json").read_text())
    assert failure["status"] == "incomplete" and failure["semantic_approval"] == "pending"


def test_transport_failure_preserves_first_page_and_original_exception(tmp_path):
    calls = []

    def fetch(*args):
        calls.append(args)
        if len(calls) > 1:
            raise RuntimeError("fixture transport interrupted")
        return page_bytes(2, 0, ["a"])

    with pytest.raises(RuntimeError, match="fixture transport interrupted"):
        capture(tmp_path, fetch, page_size=1)
    incomplete = tmp_path / "capture.incomplete"
    assert (incomplete / "page-0000.json").is_file()
    assert not (incomplete / "capture.json").exists()


@pytest.mark.parametrize("name", ["capture", "capture.incomplete"])
def test_existing_evidence_refused_before_transport(tmp_path, name):
    existing = tmp_path / name
    existing.mkdir()
    marker = existing / "evidence.txt"
    marker.write_bytes(b"previous reviewed evidence")

    def forbidden(*args):
        pytest.fail("transport must not run for an occupied destination")

    with pytest.raises(ValueError, match="never overwritten"):
        capture(tmp_path, forbidden)
    assert marker.read_bytes() == b"previous reviewed evidence"


@pytest.mark.parametrize("name", ["capture", "capture.incomplete"])
def test_dangling_destination_symlinks_refused_before_transport(tmp_path, name):
    link = tmp_path / name
    link.symlink_to(tmp_path / "nonexistent", target_is_directory=True)

    def forbidden(*args):
        pytest.fail("transport must not run for a dangling symlink")

    with pytest.raises(ValueError, match="never overwritten"):
        capture(tmp_path, forbidden)
    assert link.is_symlink()


def test_racing_destination_cannot_be_replaced(tmp_path, monkeypatch):
    """Race after preflight, immediately before exclusive destination creation."""
    out = tmp_path / "capture"
    original_mkdir = Path.mkdir
    marker = out / "competitor.txt"

    def racing_mkdir(path, *args, **kwargs):
        if path == out and not out.exists():
            original_mkdir(out)
            marker.write_bytes(b"another capture owns this directory")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", racing_mkdir)
    with pytest.raises(FileExistsError):
        capture(tmp_path, lambda *args: pytest.fail("network after failed reservation"))
    assert marker.read_bytes() == b"another capture owns this directory"
    assert list(out.iterdir()) == [marker]


def test_competing_incomplete_directory_is_never_overwritten(tmp_path):
    incomplete = tmp_path / "capture.incomplete"
    marker = incomplete / "other-capture.txt"

    def fetch(*args):
        incomplete.mkdir()
        marker.write_bytes(b"other failure receipt")
        raise RuntimeError("fixture failure")

    with pytest.raises(RuntimeError, match="fixture failure"):
        capture(tmp_path, fetch)
    assert marker.read_bytes() == b"other failure receipt"
    # Collision leaves the owned failed reservation visible for explicit recovery.
    assert (tmp_path / "capture" / "failure.json").is_file()


def test_failure_directory_reservation_race_never_replaces_competitor(tmp_path, monkeypatch):
    incomplete = tmp_path / "capture.incomplete"
    original_mkdir = Path.mkdir
    marker = incomplete / "competitor.txt"

    def racing_mkdir(path, *args, **kwargs):
        if path == incomplete and not incomplete.exists():
            original_mkdir(incomplete)
            marker.write_bytes(b"another failed capture owns this directory")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", racing_mkdir)

    def fail(*args):
        raise RuntimeError("capture transport failed")

    with pytest.raises(RuntimeError, match="capture transport failed"):
        capture(tmp_path, fail)
    assert marker.read_bytes() == b"another failed capture owns this directory"
    assert list(incomplete.iterdir()) == [marker]
    assert (tmp_path / "capture" / "failure.json").is_file()


def test_http_failure_is_not_converted_into_empty_success(tmp_path, monkeypatch):
    def unavailable(request, timeout):
        raise HTTPError(request.full_url, 503, "fixture service unavailable", {}, None)

    monkeypatch.setattr(discovery, "build_opener", lambda *handlers: SimpleNamespace(open=unavailable))
    with pytest.raises(HTTPError) as raised:
        discovery.capture_catalogue_query(QUERY, tmp_path / "capture", captured_at=STAMP)
    assert raised.value.code == 503
    incomplete = tmp_path / "capture.incomplete"
    failure = json.loads((incomplete / "failure.json").read_text())
    assert failure["status"] == "incomplete" and failure["pages"] == []
    assert not (incomplete / "capture.json").exists()


@pytest.mark.parametrize("fetch", [False, 0])
def test_invalid_falsey_transport_refused_without_default_network(tmp_path, monkeypatch, fetch):
    def forbidden(*args):
        pytest.fail("invalid explicit fetch activated default network")

    monkeypatch.setattr(discovery, "_public_get", forbidden)
    with pytest.raises(ValueError, match="fetch"):
        discovery.capture_catalogue_query(QUERY, tmp_path / "capture", fetch=fetch,
                                           captured_at=STAMP)
    assert list(tmp_path.iterdir()) == []


def test_injected_transport_never_opens_socket_or_calls_default_transport(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("injected capture reached a network/default transport")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(discovery, "_public_get", forbidden)
    result = capture(tmp_path, lambda *args: page_bytes(1, 0, ["a"]))
    assert result["captured_metadata_records"] == 1


def test_default_transport_has_no_credentials_and_bounds_response(monkeypatch):
    calls = []

    def open_request(request, timeout):
        calls.append((request, timeout))
        return io.BytesIO(b"abcd")

    monkeypatch.setattr(discovery, "build_opener", lambda *handlers: SimpleNamespace(open=open_request))
    with pytest.raises(ValueError, match="max_bytes"):
        discovery._public_get("https://knb.ecoinformatics.org/query", 5, 3)
    request, timeout = calls[0]
    assert request.get_method() == "GET" and timeout == 5
    headers = {k.lower(): v for k, v in request.header_items()}
    assert set(headers) == {"user-agent", "accept"}


def test_default_transport_ignores_installed_auth_opener_and_proxy_credentials(monkeypatch):
    requests = []
    factories = []
    actual_build_opener = urllib.request.build_opener

    class FixtureResponse(io.BytesIO):
        code = 200
        msg = "OK"

        def info(self):
            return {}

    class FixtureHTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, request):
            requests.append(request)
            return FixtureResponse(b"{}")

    def fresh_factory(*handlers):
        factories.append(handlers)
        return actual_build_opener(*handlers, FixtureHTTPSHandler())

    def global_authenticated_opener(*args, **kwargs):
        pytest.fail("default public capture reached installed authenticated opener")

    monkeypatch.setattr(urllib.request, "_opener", SimpleNamespace(open=global_authenticated_opener))
    for name in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://fixture-user:fixture-password@127.0.0.1:9")
    monkeypatch.setattr(discovery, "build_opener", fresh_factory)
    assert discovery._public_get("https://knb.ecoinformatics.org/query", 5, 10) == b"{}"
    assert len(factories) == 1 and len(factories[0]) == 1
    proxy = factories[0][0]
    assert isinstance(proxy, urllib.request.ProxyHandler) and proxy.proxies == {}
    assert len(requests) == 1 and requests[0].host == "knb.ecoinformatics.org"
    assert not requests[0].has_proxy()
    headers = {name.lower() for name, value in requests[0].header_items()}
    assert not headers & {"authorization", "cookie", "proxy-authorization"}


@pytest.mark.parametrize("kwargs", [
    {"max_records": 0}, {"max_records": 1001}, {"max_records": True},
    {"page_size": 0}, {"page_size": 101}, {"page_size": "10"},
    {"max_bytes": 0}, {"max_bytes": 10_000_001}, {"max_bytes": False},
    {"timeout": 0}, {"timeout": 121}, {"timeout": float("inf")},
    {"timeout": float("nan")}, {"timeout": True}, {"catalogue": "private"},
])
def test_invalid_capture_parameters_do_not_create_evidence_or_fetch(tmp_path, kwargs):
    with pytest.raises(ValueError):
        capture(tmp_path, lambda *args: pytest.fail("invalid input made network call"), **kwargs)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("query", ["", " ", "x" * 4097, None, {}])
def test_invalid_query_refused_before_any_write(tmp_path, query):
    with pytest.raises(ValueError, match="query"):
        discovery.capture_catalogue_query(query, tmp_path / "capture", captured_at=STAMP)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("stamp", ["", "2026-09-30T16:00:00", "not a timestamp", 123,
                                  "2026-09-30T16:00:00+02:60",
                                  "2026-09-30T16:00:00-02:60",
                                  "2026-09-30T16:00:00+24:00",
                                  "2026-02-30T16:00:00Z",
                                  "2026-02-29T16:00:00Z",
                                  "2026-09-30T24:00:00Z",
                                  "2026-09-30T16:60:00Z",
                                  "2026-09-30T16:00:60Z"])
def test_invalid_capture_timestamp_refused_before_any_write(tmp_path, stamp):
    with pytest.raises(ValueError):
        discovery.capture_catalogue_query(QUERY, tmp_path / "capture", captured_at=stamp)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("stamp", ["2024-02-29T23:59:59Z",
                                  "2026-09-30T16:00:00+23:59",
                                  "2026-09-30T16:00:00-23:59",
                                  "2026-09-30T16:00:00.123456+00:00"])
def test_valid_capture_timestamp_is_preserved_exactly(tmp_path, stamp):
    receipt = discovery.capture_catalogue_query(
        QUERY, tmp_path / "capture", fetch=lambda *args: page_bytes(0, 0, []),
        captured_at=stamp)
    assert receipt["captured_at"] == stamp


@pytest.mark.parametrize("body", [b"not json", b"{}", page_bytes(0, 0, ["a"]),
                                  page_bytes(1, 0, [""]), page_bytes(True, 0, ["a"])])
def test_bad_first_page_is_preserved_without_success_marker(tmp_path, body):
    with pytest.raises((ValueError, KeyError, TypeError)):
        capture(tmp_path, lambda *args: body)
    incomplete = tmp_path / "capture.incomplete"
    assert (incomplete / "page-0000.json").read_bytes() == body
    assert (incomplete / "failure.json").is_file()
    assert not (incomplete / "capture.json").exists()


@pytest.mark.parametrize("body", ["not bytes", b"x" * 31])
def test_bad_or_oversized_transport_result_never_reports_success(tmp_path, body):
    with pytest.raises(ValueError, match="bounded bytes"):
        capture(tmp_path, lambda *args: body, max_bytes=30)
    assert not (tmp_path / "capture.incomplete" / "capture.json").exists()

@pytest.mark.parametrize("found,start", [(1.0,0.0),(1,0)])
def test_json_integral_numbers_are_protocol_integers(tmp_path,found,start):
    raw=json.dumps({"response":{"numFound":found,"start":start,"docs":[{"id":"fixture"}]}}).encode()
    result=capture(tmp_path,lambda *args:raw)
    assert result["reported_metadata_matches"]==1

@pytest.mark.parametrize("found", [True,1.5,9007199254740992])
def test_json_counts_must_be_exact_nonnegative_integers(tmp_path,found):
    raw=json.dumps({"response":{"numFound":found,"start":0,"docs":[{"id":"fixture"}]}}).encode()
    with pytest.raises(ValueError,match="Malformed"):capture(tmp_path,lambda *args:raw)
