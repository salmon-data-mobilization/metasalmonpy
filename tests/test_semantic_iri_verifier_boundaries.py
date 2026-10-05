"""B-130 review regressions: optional transport and persisted URL credentials.

Core import/injected controls run without HTTPX; only the real redirect control
needs the verifier extra. No test makes an external request.
"""

import ast
import importlib.util
import json
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pandas as pd
import pytest

import metasalmonpy as ms

ROOT = Path(__file__).resolve().parents[1]
REPORT = "reproducibility/provenance/semantic-iri-dereference.csv"


def metadata(root, iri):
    target = root / "metadata"
    target.mkdir(parents=True)
    pd.DataFrame({"dataset_id": ["boundary"]}).to_csv(target / "dataset.csv", index=False)
    pd.DataFrame({"table_id": ["observations"]}).to_csv(target / "tables.csv", index=False)
    pd.DataFrame({"term_iri": [iri]}).to_csv(target / "column_dictionary.csv", index=False)


def test_backend_is_an_explicit_extra_and_core_remains_pandas_requests():
    # These declared arrays are literal strings on one line in pyproject.toml;
    # literal_eval keeps this contract test runnable on minimum Python 3.9.
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    required = ast.literal_eval(re.search(r"^dependencies = (\[.*\])$", project, re.M)[1])
    assert sorted(re.split(r"[<>=!~]", value, maxsplit=1)[0] for value in required) == [
        "pandas", "requests"]
    extra = re.search(r"^verify = (\[.*\])$", project, re.M)
    assert extra is not None, "The default backend must have an explicit installable extra"
    assert ast.literal_eval(extra[1]) == ["httpx>=0.28.1"]


@pytest.mark.parametrize("entry", ["public", "direct"])
def test_absent_backend_import_and_injected_path_work_but_default_fails_clearly(tmp_path, entry):
    # A fresh process prevents an already-imported backend from disguising the
    # absence. The real HTTPX-free core CI leg executes the same control.
    script = r'''
import importlib.abc, importlib.util, json, pathlib, sys
class NoHTTPX(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "httpx" or fullname.startswith("httpx."):
            raise ModuleNotFoundError("HTTPX deliberately unavailable", name="httpx")
sys.meta_path.insert(0, NoHTTPX())
root, package, entry = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
spec = importlib.util.spec_from_file_location("metasalmonpy", package / "__init__.py",
                                           submodule_search_locations=[str(package)])
ms = importlib.util.module_from_spec(spec)
sys.modules["metasalmonpy"] = ms
spec.loader.exec_module(ms)
import pandas as pd
from metasalmonpy import semantic_iri_verification as module
metadata = root / "metadata"
metadata.mkdir()
iri = "https://selected.invalid/term;exact#one"
for name, values in [("dataset", {"dataset_id":["core"]}),
                     ("tables", {"table_id":["observations"]}),
                     ("column_dictionary", {"term_iri":[iri]})]:
    pd.DataFrame(values).to_csv(metadata / (name + ".csv"), index=False)
rows = ms.verify_sdp_semantic_iris(root, requester=lambda exact:
                                 {"status":200,"final_url":exact})
assert rows.iri.tolist() == [iri]
report = root / "reproducibility/provenance/semantic-iri-dereference.csv"
before = report.read_bytes()
def forbidden_worker(*args, **kwargs):
    raise AssertionError("Missing optional backend must not start a request worker")
module.subprocess.Popen = forbidden_worker
try:
    if entry == "public":
        ms.verify_sdp_semantic_iris(root)
    else:
        module._request(iri)
except ImportError as error:
    assert "metasalmonpy[verify]" in str(error)
else:
    raise AssertionError("The optional backend must fail clearly only on use")
assert report.read_bytes() == before
assert "httpx" not in sys.modules
print(json.dumps({"core_import":True,"injected":True,"clear_missing_backend":True,
                  "prior_report_preserved":True}))
'''
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path), str(ROOT), entry],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert all(json.loads(result.stdout).values())


@pytest.mark.parametrize("final_url, expected", [
    ("https://user:password@example.org/path;exact?q=one#fragment",
     "https://example.org/path;exact?q=one#fragment"),
    ("HTTPS://user@example.org:8443/path", "HTTPS://example.org:8443/path"),
    ("http://:password@example.org/", "http://example.org/"),
    ("https://user:p%40ss%3Aword@example.org/exact", "https://example.org/exact"),
    ("https://user:password@[::1]:8443/exact", "https://[::1]:8443/exact"),
    ("https://first@user:password@example.org/exact", "https://example.org/exact"),
    ("https://example.org/path@ordinary;exact?q=a@b#frag@one",
     "https://example.org/path@ordinary;exact?q=a@b#frag@one"),
    ("HTTPS://Example.ORG:443/%2Fpath;exact?x=1#F", "HTTPS://Example.ORG:443/%2Fpath;exact?x=1#F"),
])
def test_userinfo_is_removed_before_returned_rows_and_atomic_csv(tmp_path, final_url, expected):
    iri = "https://selected.invalid/term;exact#one"
    metadata(tmp_path, iri)
    inputs = {file: file.read_bytes() for file in (tmp_path / "metadata").iterdir()}
    request = lambda exact: {"status": 200, "final_url": final_url}
    rows = ms.verify_sdp_semantic_iris(tmp_path, requester=request)
    assert rows.final_url.tolist() == [expected]
    assert rows.iri.tolist() == [iri]
    assert list(rows) == ["iri", "status", "final_url", "error", "attempts"]
    assert rows.attempts.tolist() == [1]
    report = (tmp_path / REPORT).read_bytes()
    assert pd.read_csv(tmp_path / REPORT).final_url.tolist() == [expected]
    assert all(file.read_bytes() == raw for file, raw in inputs.items())
    ms.verify_sdp_semantic_iris(tmp_path, requester=request)
    assert (tmp_path / REPORT).read_bytes() == report


@pytest.mark.skipif(importlib.util.find_spec("httpx") is None,
                    reason="Real redirect control needs metasalmonpy[verify]")
def test_default_public_redirect_does_not_persist_userinfo(tmp_path, monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy",
                 "https_proxy", "all_proxy", "no_proxy", "REQUESTS_CA_BUNDLE",
                 "CURL_CA_BUNDLE", "SSL_CERT_FILE", "SSL_CERT_DIR"):
        monkeypatch.delenv(name, raising=False)
    netrc = tmp_path / "empty.netrc"
    netrc.write_text("machine unrelated.invalid login demo password demo\n", encoding="utf-8")
    monkeypatch.setenv("NETRC", str(netrc))
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            if self.path == "/start":
                self.send_response(302)
                self.send_header("Location", f"http://review-user:review-password@127.0.0.1:{self.server.server_port}/final")
            else:
                self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    try:
        metadata(tmp_path, origin + "/start")
        rows = ms.verify_sdp_semantic_iris(tmp_path)
        assert calls == ["/start", "/final"]
        assert rows.final_url.tolist() == [origin + "/final"]
        raw = (tmp_path / REPORT).read_bytes()
        assert b"review-user" not in raw and b"review-password" not in raw
        assert rows.iri.tolist() == [origin + "/start"]
        assert rows.attempts.tolist() == [1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
