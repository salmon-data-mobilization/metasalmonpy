"""B-130 behavioral mirror: injected and local transports; no external HTTP claims."""

import io
import json
import subprocess
import sys
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pandas as pd
import pytest
import requests

import metasalmonpy as ms

REPORT = "reproducibility/provenance/semantic-iri-dereference.csv"


def write_csv(root, relative, rows):
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(target, index=False)
    return target


def fixture(root):
    write_csv(root, "metadata/dataset.csv", {"dataset_id": ["iri-test"]})
    write_csv(root, "metadata/tables.csv", {"table_id": ["observations"]})
    write_csv(root, "metadata/column_dictionary.csv", {"term_iri": [None]})
    return root


def verify(*args, **kwargs):
    # getattr allows the initial RED run to demonstrate missing public behavior.
    return getattr(ms, "verify_sdp_semantic_iris")(*args, **kwargs)


def good(iri):
    return {"status": 200, "final_url": iri}


def no_sleep(*args):
    pytest.fail("This response must not be retried")


def test_complete_failures_bounded_retries_and_repeat_bytes(tmp_path):
    fixture(tmp_path)
    write_csv(tmp_path, "metadata/column_dictionary.csv", {
        "term_iri": ["https://example.org/z#exact; https://example.org/a#exact"],
        "property_iri": ["https://example.org/b#exact"],
        "constraint_iri": ["https://example.org/c#exact"],
        "unrelated_url": ["https://example.org/not-selected"],
    })
    calls, delays = Counter(), []

    def request(iri):
        calls[iri] += 1
        if "/a#" in iri and calls[iri] == 1:
            return {"status": 503, "final_url": iri}
        if "/b#" in iri:
            return {"status": 404, "final_url": iri}
        if "/c#" in iri:
            raise RuntimeError("timeout; Authorization: Bearer test-credential")
        return {"status": 200, "final_url": iri.split("#")[0]}

    for repeat in range(2):
        calls.clear()
        delays.clear()
        with pytest.raises(ValueError, match=r"/b#exact.*?/c#exact"):
            verify(tmp_path, requester=request, sleep_fn=delays.append)
        raw = (tmp_path / REPORT).read_bytes()
        rows = pd.read_csv(tmp_path / REPORT)
        assert list(rows) == ["iri", "status", "final_url", "error", "attempts"]
        assert rows.iri.tolist() == [f"https://example.org/{x}#exact" for x in "abcz"]
        assert rows.attempts.tolist() == [2, 1, 3, 1]
        assert rows.status.iloc[:2].tolist() == [200, 404]
        assert pd.isna(rows.status.iloc[2])
        assert delays == [0.1, 0.1, 0.25]
        assert b"test-credential" not in raw
        assert b"[REDACTED]" in raw
        if repeat:
            assert raw == first
        first = raw


def test_selected_surfaces_and_exact_scalars(tmp_path):
    fixture(tmp_path)
    expected = []
    for relative, field in [
        ("metadata/dataset.csv", "protocol_iri"),
        ("metadata/tables.csv", "method_iri"),
        ("metadata/column_dictionary.csv", "term_iri"),
        ("metadata/codes.csv", "vocabulary_iri"),
        ("metadata/methods.csv", "method_iri"),
        ("metadata/semantic/measurement-decompositions.csv", "component_iri"),
        ("metadata/structure/observation_components.csv", "component_relation_iri"),
    ]:
        iri = f"https://example.org/{field}#one"
        expected.append(iri)
        write_csv(tmp_path, relative, {field: [iri]})
    for relative, iri in [
        ("metadata/semantic_vocabulary.csv", "https://example.org/term;variant#one"),
        ("reviewed_semantic_selections.csv", "https://example.org/review;variant#two"),
        ("reproducibility/reviewed_semantic_selections.csv", "https://example.org/repro#three"),
    ]:
        expected.append(iri)
        write_csv(tmp_path, relative, {
            "iri": [iri, "https://example.org/rejected#no"],
            "decision": ["accepted", "rejected"],
            "source_url": ["https://example.org/source", None],
        } if "selections" in relative else {"iri": [iri]})
    write_csv(tmp_path, "semantic_suggestions.csv", {"iri": ["https://example.org/candidate"]})
    seen = []
    def request(iri):
        seen.append(iri)
        return good(iri)
    result = verify(tmp_path, requester=request, sleep_fn=no_sleep)
    assert seen == sorted(set(expected), key=lambda value: value.encode("utf-8"))
    assert result.iri.tolist() == seen


def test_no_http_identifier_cannot_pass(tmp_path):
    fixture(tmp_path)
    write_csv(tmp_path, "metadata/column_dictionary.csv", {
        "term_iri": ["REVIEW:pending", "urn:example:local"]})
    with pytest.raises(ValueError, match="At least one HTTP semantic IRI"):
        verify(tmp_path, requester=lambda iri: pytest.fail("No request expected"))
    assert not (tmp_path / REPORT).exists()


def test_manifest_bound_sssom_literals_and_hash_validation(tmp_path):
    fixture(tmp_path)
    source = tmp_path / "source.sssom.tsv"
    source.write_text("\n".join([
        "# sssom_version: 1.1", "# mapping_set_id: https://example.org/mappings/test",
        "# mapping_set_version: 2026-10-01",
        "# license: https://creativecommons.org/licenses/by/4.0/",
        "# subject_source: https://example.org/subject/", "# subject_source_version: v1",
        "# object_source: https://example.org/object/", "# object_source_version: v1",
        "# curie_map:", "#   skos: http://www.w3.org/2004/02/skos/core#",
        "#   semapv: https://w3id.org/semapv/vocab/",
        "\t".join(["subject_id", "subject_label", "subject_category", "predicate_id",
                    "object_id", "object_label", "mapping_justification"]),
        "\t".join(["https://example.org/subject#one", "Subject",
                    "https://example.org/category#one|https://example.org/category#two",
                    "http://www.w3.org/2004/02/skos/core#exactMatch",
                    "https://example.org/object#one", "Object", "semapv:ManualMappingCuration"]),
    ]) + "\n", encoding="utf-8")
    ms.write_sdp_sssom(tmp_path, source)
    result = verify(tmp_path, requester=good, sleep_fn=no_sleep)
    assert set(result.iri) == {
        "https://example.org/subject#one", "https://example.org/category#one",
        "https://example.org/category#two", "http://www.w3.org/2004/02/skos/core#exactMatch",
        "https://example.org/object#one"}
    manifest = json.loads((tmp_path / "metadata/semantic/mapping-sets.json").read_text())
    bound = tmp_path / manifest["mapping_sets"][0]["path"]
    bound.write_bytes(bound.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="SHA-256"):
        verify(tmp_path, requester=lambda iri: pytest.fail("Invalid mapping must stop before HTTP"))


def test_partial_canonical_uses_descriptor(tmp_path):
    write_csv(tmp_path, "metadata/column_dictionary.csv", {"term_iri": ["https://example.org/stale"]})
    write_csv(tmp_path, "data/observations.csv", {"value": [1]})
    (tmp_path / "datapackage.json").write_text(json.dumps({
        "id": "iri-test", "resources": [{"name": "observations", "path": "data/observations.csv",
        "schema": {"fields": [{"name": "value", "type": "number",
                    "custom": {"sdp:termIri": "https://example.org/descriptor#term"}}]}}]}))
    rows = verify(tmp_path, requester=good, sleep_fn=no_sleep)
    assert rows.iri.tolist() == ["https://example.org/descriptor#term"]


@pytest.mark.parametrize("response", [
    None, {"status": [200], "final_url": ["https://example.org/a"]},
    {"status": True, "final_url": "https://example.org/a"},
    {"status": 200}, {"status": "200junk", "final_url": "https://example.org/a"},
    {"status": 200, "final_url": "https://example.org/a", "error": ["timeout"]},
])
def test_malformed_response_does_not_interrupt_sweep(tmp_path, response):
    fixture(tmp_path)
    write_csv(tmp_path, "metadata/column_dictionary.csv", {
        "term_iri": ["https://example.org/a#bad;https://example.org/b#good"]})
    seen = []
    def request(iri):
        seen.append(iri)
        return response if "/a#" in iri else good(iri)
    # Like R, a valid HTTP 200+URL remains successful even if error text is malformed.
    if isinstance(response, dict) and response.get("status") == 200 and isinstance(response.get("final_url"), str):
        verify(tmp_path, requester=request, sleep_fn=no_sleep)
    else:
        with pytest.raises(ValueError, match="/a#bad"):
            verify(tmp_path, requester=request, sleep_fn=no_sleep)
    assert len(seen) == 2
    rows = pd.read_csv(tmp_path / REPORT)
    assert rows.attempts.tolist() == [1, 1]
    assert rows.status.iloc[1] == 200
    assert isinstance(rows.error.iloc[0], str)


@pytest.mark.parametrize("status, attempts", [(408, 3), (429, 3), (500, 3), (599, 3), (400, 1), (401, 1), (404, 1), (300, 1)])
def test_http_retry_classification(tmp_path, status, attempts):
    fixture(tmp_path)
    write_csv(tmp_path, "metadata/column_dictionary.csv", {"term_iri": ["https://example.org/a"]})
    delays = []
    with pytest.raises(ValueError):
        verify(tmp_path, requester=lambda iri: {"status": status, "final_url": iri}, sleep_fn=delays.append)
    assert pd.read_csv(tmp_path / REPORT).attempts.tolist() == [attempts]
    assert delays == ([0.1, 0.25] if attempts == 3 else [])


@pytest.mark.parametrize("error, attempts", [
    (requests.exceptions.ConnectionError("temporary outage"), 3),
    (requests.exceptions.Timeout("temporary outage"), 3),
    (RuntimeError("TLS handshake failed"), 3),
    (RuntimeError("programming defect"), 1),
    (requests.exceptions.InvalidURL("bad URL"), 1),
])
def test_transport_retry_classification(tmp_path, error, attempts):
    fixture(tmp_path)
    write_csv(tmp_path, "metadata/column_dictionary.csv", {"term_iri": ["https://example.org/a"]})
    def request(iri):
        raise error
    with pytest.raises(ValueError):
        verify(tmp_path, requester=request, sleep_fn=lambda _: None)
    assert pd.read_csv(tmp_path / REPORT).attempts.tolist() == [attempts]


@pytest.mark.parametrize("relative", ["reproducibility", REPORT])
def test_default_report_refuses_symlinks(tmp_path, relative):
    root, outside = tmp_path / "sdp", tmp_path / "outside"
    root.mkdir(); outside.mkdir(); fixture(root)
    write_csv(root, "metadata/column_dictionary.csv", {"term_iri": ["https://example.org/a"]})
    link = root / relative
    link.parent.mkdir(parents=True, exist_ok=True)
    target = outside if relative == "reproducibility" else outside / "old.csv"
    if relative == REPORT:
        target.write_bytes(b"old")
    link.symlink_to(target, target_is_directory=relative == "reproducibility")
    with pytest.raises(ValueError, match="symlink"):
        verify(root, requester=good, sleep_fn=no_sleep)
    assert not (outside / "provenance/semantic-iri-dereference.csv").exists()
    if relative == REPORT:
        assert target.read_bytes() == b"old"


def test_atomic_write_failure_preserves_old_report(tmp_path, monkeypatch):
    fixture(tmp_path)
    write_csv(tmp_path, "metadata/column_dictionary.csv", {"term_iri": ["https://example.org/a"]})
    verify(tmp_path, requester=good, sleep_fn=no_sleep)
    first = (tmp_path / REPORT).read_bytes()
    from metasalmonpy import atomic_io
    def fail(*args):
        raise OSError("interrupted replace")
    monkeypatch.setattr(atomic_io.os, "replace", fail)
    with pytest.raises(OSError, match="interrupted replace"):
        verify(tmp_path, requester=good, sleep_fn=no_sleep)
    assert (tmp_path / REPORT).read_bytes() == first
    assert list((tmp_path / REPORT).parent.iterdir()) == [tmp_path / REPORT]


def test_default_transport_is_get_redirecting_bounded_and_no_model(tmp_path, monkeypatch):
    fixture(tmp_path)
    iri = "https://example.org/term#exact"
    write_csv(tmp_path, "metadata/column_dictionary.csv", {"term_iri": [iri]})
    calls = []
    class Response:
        status_code = 200
        url = "https://example.org/term"
        def __enter__(self):
            return self
        def __exit__(self, *args):
            calls.append("closed")
        @property
        def content(self):
            pytest.fail("The verifier needs only response headers, not the body")
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return Response()
    monkeypatch.setattr(requests, "get", get)
    from metasalmonpy import semantic_iri_verification as module
    # Execute the fixed worker with in-process streams so keyword/close behavior
    # is observable. The real subprocess/deadline is exercised separately below.
    def worker_process(*args, **kwargs):
        class Process:
            returncode = 0
            def communicate(self, input, timeout):
                output = io.StringIO()
                with monkeypatch.context() as patch:
                    patch.setattr(sys, "stdin", io.StringIO(input))
                    patch.setattr(sys, "stdout", output)
                    exec(module._REQUEST_WORKER, {})
                return output.getvalue(), ""
            def poll(self):
                return 0
        return Process()
    monkeypatch.setattr(subprocess, "Popen", worker_process)
    from metasalmonpy import llm_review
    assert callable(llm_review.request_json)  # Positive control for provider reach.
    for name in dir(llm_review):
        if "request" in name and callable(getattr(llm_review, name)):
            monkeypatch.setattr(llm_review, name, lambda *args, **kwargs: pytest.fail("No model call"))
    rows = verify(tmp_path, sleep_fn=no_sleep)
    assert calls == [(iri, {"headers": {"Accept": "*/*"}, "timeout": 30,
                           "allow_redirects": True, "stream": True}), "closed"]
    assert rows.final_url.tolist() == [Response.url]


def test_worker_uses_fixed_command_and_literal_json_input(monkeypatch):
    from metasalmonpy import semantic_iri_verification as module
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: pytest.fail("No parent HTTP"))
    iri = 'https://example.org/term?literal="$(touch never)"#exact'
    seen = []
    class Process:
        returncode = 0
        def communicate(self, input, timeout):
            seen.append((json.loads(input), timeout))
            return json.dumps({"status": 200, "final_url": iri}), ""
        def poll(self):
            return 0
    def launch(command, **kwargs):
        assert command == [sys.executable, "-c", module._REQUEST_WORKER]
        assert kwargs == {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE,
                          "stderr": subprocess.PIPE, "text": True}
        return Process()
    monkeypatch.setattr(subprocess, "Popen", launch)
    assert module._request(iri) == {"status": 200, "final_url": iri}
    assert seen == [(iri, 30)]


def test_blocked_default_worker_is_killed_reaped_and_reported(tmp_path, monkeypatch):
    from metasalmonpy import semantic_iri_verification as module
    fixture(tmp_path)
    write_csv(tmp_path, "metadata/column_dictionary.csv", {
        "term_iri": ["https://example.org/blocked#one"]})
    monkeypatch.setattr(module, "_REQUEST_WORKER", "import time; time.sleep(60)")
    monkeypatch.setattr(module, "_REQUEST_TIMEOUT", 0.1)
    processes = []
    popen = subprocess.Popen
    def launch(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(subprocess, "Popen", launch)
    delays = []
    started = time.monotonic()
    with pytest.raises(ValueError, match="blocked#one.*request-error"):
        verify(tmp_path, sleep_fn=delays.append)
    assert time.monotonic() - started < 5
    assert len(processes) == 3
    assert all(process.poll() is not None for process in processes)
    assert delays == [0.1, 0.25]
    row = pd.read_csv(tmp_path / REPORT).iloc[0]
    assert row.attempts == 3
    assert pd.isna(row.status)
    assert "timed out" in row.error


def test_local_default_transport_ignores_body_and_bounds_headers(monkeypatch):
    from metasalmonpy import semantic_iri_verification as module
    monkeypatch.setattr(module, "_REQUEST_TIMEOUT", 2)
    release = threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/headers":
                release.wait(5)  # No headers arrive before the scaled deadline.
                return
            self.send_response(200)
            self.send_header("Content-Length", "1000000")
            self.end_headers()
            release.wait(5)  # The body never needs to arrive for HTTP resolution.
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = f"http://127.0.0.1:{server.server_port}"
    try:
        assert module._request(root + "/body") == {
            "status": 200, "final_url": root + "/body"}
        started = time.monotonic()
        with pytest.raises(requests.exceptions.Timeout, match="timed out"):
            module._request(root + "/headers")
        assert time.monotonic() - started < 4
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_canonical_order_non_ascii_and_explicit_report_path(tmp_path):
    fixture(tmp_path)
    iris = [f"https://example.org/{part}#one" for part in ["z", "A", "é", "Z", "a"]]
    write_csv(tmp_path, "metadata/column_dictionary.csv", {"term_iri": [";".join(iris)]})
    target = tmp_path / "custom" / "report.csv"
    rows = verify(tmp_path, report_path=target, requester=good, sleep_fn=no_sleep)
    assert rows.iri.tolist() == sorted(iris, key=lambda iri: iri.encode("utf-8"))
    assert target.read_bytes().startswith(b"iri,status,final_url,error,attempts\n")
    assert not (tmp_path / REPORT).exists()


def test_report_bytes_match_r_success_redirect_http_and_transport(tmp_path):
    fixture(tmp_path)
    write_csv(tmp_path, "metadata/column_dictionary.csv", {"term_iri": [
        "https://example.org/z#ok", "https://example.org/A#redirect",
        "https://example.org/é#missing", "https://example.org/a#transport"]})
    def request(iri):
        if "/a#" in iri:
            raise RuntimeError("timeout; Authorization: Bearer test-credential")
        if "/é#" in iri:
            return {"status": 404, "final_url": iri}
        return {"status": 200, "final_url": "https://example.org/A" if "/A#" in iri else iri}
    with pytest.raises(ValueError):
        verify(tmp_path, requester=request, sleep_fn=lambda _: None)
    expected = Path(__file__).parent / "data/semantic_iri_verification/report-from-r.csv"
    assert (tmp_path / REPORT).read_bytes() == expected.read_bytes()
