"""Selected SDP semantic-IRI dereferencing (R B-130 behavioral companion).

HTTP resolution is evidence about transport, never term suitability or RDF
fragment presence. This module makes no model call and never searches for or
substitutes an identifier. Every requested IRI is already selected metadata.

Integration obligation: when the B-58 condition hierarchy lands, classify this
module's authored errors in its validation family and extend that source guard.
The pending R verifier has the same obligation; no conditional shim is added.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Mapping, Optional, Union

import pandas as pd
import requests

from .atomic_io import atomic_write
from .package_io import _metadata_path, _read_metadata_csv, read_salmon_datapackage
from .sdp_methods import (
    _assert_safe_directory,
    _assert_safe_file,
    _csv_bytes,
    _extension_root,
)
from .sssom import read_sssom_mapping_set, validate_sdp_sssom
from .text_safety import redact_secrets

_COLUMNS = ("iri", "status", "final_url", "error", "attempts")
_DEFAULT_REPORT = "reproducibility/provenance/semantic-iri-dereference.csv"
_DELAYS = (0.1, 0.25)
_REQUEST_TIMEOUT = 30
# Fixed source and JSON stdin keep the selected IRI out of shell/code syntax.
# A separate process gives DNS, headers, redirects and socket waits one real
# deadline without leaking a blocked thread or requiring notebook main guards.
# Retires when a shared verified transport provides the same finite deadline.
_REQUEST_WORKER = """
import json
import sys
import requests
from urllib.parse import urljoin, urlparse

iri = json.load(sys.stdin)
try:
    with requests.Session() as session:
        prepared = session.prepare_request(requests.Request("GET", iri, headers={"Accept": "*/*"}))
        fragment = urlparse(prepared.url).fragment
        for redirects in range(session.max_redirects + 1):
            settings = session.merge_environment_settings(prepared.url, {}, True, None, None)
            # HTTPAdapter.send is the public transport seam. Session.send would
            # consume a redirect's body even with stream=True/redirects disabled.
            with session.get_adapter(prepared.url).send(prepared, timeout=30, **settings) as response:
                if not response.is_redirect:
                    result = {"status": response.status_code, "final_url": response.url}
                    break
                if redirects == session.max_redirects:
                    raise requests.exceptions.TooManyRedirects("Exceeded 30 redirects.")
                target = urljoin(response.url, requests.utils.requote_uri(session.get_redirect_target(response)))
                parsed = urlparse(target)
                if not parsed.fragment and fragment:
                    target = parsed._replace(fragment=fragment).geturl()
                else:
                    fragment = parsed.fragment
                requests.cookies.extract_cookies_to_jar(session.cookies, prepared, response.raw)
                following = session.prepare_request(requests.Request("GET", target, headers={"Accept": "*/*"}))
                authorization = prepared.headers.get("Authorization")
                if authorization and not session.should_strip_auth(prepared.url, following.url):
                    following.headers["Authorization"] = authorization
                session.rebuild_auth(following, response)
                prepared = following
except Exception as error:
    result = {"error": str(error) or type(error).__name__,
              "transport_failure": isinstance(error, (
                  requests.exceptions.ConnectionError, requests.exceptions.Timeout))}
json.dump(result, sys.stdout)
"""
# Retires when a shared verified transport keeps this bound and failure evidence.
_TRANSIENT_MESSAGE = re.compile(
    r"TLS|SSL|connect|connection reset|timed?[ -]?out|timeout|"
    r"could not resolve|network is unreachable|empty reply|recv failure",
    re.IGNORECASE,
)


def _values(values, separator=";"):
    for value in values:
        if pd.isna(value):
            continue
        parts = [str(value)] if separator is None else str(value).split(separator)
        for part in parts:
            part = part.strip()
            if re.match(r"^https?://", part, re.IGNORECASE):
                yield part


def _from_rows(rows, fields=None, separator=";"):
    if rows is None or rows.empty:
        return []
    if fields is None:
        fields = [name for name in rows.columns if name.endswith("_iri")]
    return [iri for name in fields if name in rows.columns
            for iri in _values(rows[name], separator)]


def _from_csv(path, fields=None, accepted_only=False):
    if not path.exists():
        return []
    rows = _read_metadata_csv(path)
    if accepted_only:
        if not {"decision", "iri"} <= set(rows.columns):
            raise ValueError(f"Reviewed semantic selections need decision and iri in {path}.")
        rows = rows[rows.decision == "accepted"]
        fields = ["iri"]
    # Scalar IRIs may contain a literal semicolon; only *_iri multi-value slots split.
    return _from_rows(rows, fields, separator=None if fields == ["iri"] else ";")


def _selected_iris(root):
    paths = [_metadata_path(root, name) for name in
             ("dataset.csv", "tables.csv", "column_dictionary.csv", "codes.csv")]
    if all(path.exists() for path in paths[:3]):
        iris = [iri for path in paths for iri in _from_csv(path)]
    elif (root / "datapackage.json").exists():
        package = read_salmon_datapackage(str(root))
        iris = [iri for name in ("dataset", "tables", "dictionary", "codes")
                for iri in _from_rows(package.get(name))]
    else:
        raise ValueError("An SDP needs complete canonical metadata CSVs or datapackage.json "
                         "for semantic IRI verification.")

    # Retires when the SDP profile exposes one authoritative selected-slot inventory.
    for relative in ("metadata/methods.csv",
                     "metadata/semantic/measurement-decompositions.csv",
                     "metadata/structure/observation_components.csv"):
        iris.extend(_from_csv(root / relative))
    iris.extend(_from_csv(root / "metadata/semantic_vocabulary.csv", fields=["iri"]))
    for relative in ("reviewed_semantic_selections.csv",
                     "reproducibility/reviewed_semantic_selections.csv"):
        iris.extend(_from_csv(root / relative, accepted_only=True))

    manifest_path = root / "metadata/semantic/mapping-sets.json"
    if manifest_path.exists():
        validate_sdp_sssom(root)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        # Retires when SSSOM exposes semantic-reference roles directly. Author,
        # license and issue URLs are provenance rather than term selections.
        fields = ("subject_id", "subject_category", "predicate_id", "object_id",
                  "object_category", "mapping_justification", "subject_type",
                  "predicate_type", "object_type")
        for entry in manifest["mapping_sets"]:
            mappings = read_sssom_mapping_set(root / entry["path"]).mappings
            iris.extend(_from_rows(mappings, fields, separator="|"))
    # Explicit UTF-8 byte ordering is R's C/radix ordering, independent of locale.
    return sorted(set(iris), key=lambda iri: iri.encode("utf-8"))


def _request(iri):
    process = subprocess.Popen(
        [sys.executable, "-c", _REQUEST_WORKER], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        output, _ = process.communicate(json.dumps(iri), timeout=_REQUEST_TIMEOUT)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()  # Reap the worker before retrying or returning.
        raise requests.exceptions.Timeout(
            f"Semantic IRI request timed out after {_REQUEST_TIMEOUT:g} seconds."
        ) from None
    except BaseException:
        process.kill()
        process.communicate()
        raise
    if process.returncode:
        # stderr may contain external request text; never expose it unredacted.
        raise RuntimeError(f"Semantic IRI request worker failed (exit {process.returncode}).")
    try:
        response = json.loads(output)
    except (TypeError, ValueError):
        raise RuntimeError("Semantic IRI request worker returned invalid JSON.") from None
    if "error" in response:
        failure = (requests.exceptions.ConnectionError if response.get("transport_failure")
                   else RuntimeError)
        raise failure(response["error"])
    return response


def _attempt(iri, requester):
    try:
        response = requester(iri)
    except Exception as error:
        message = str(error)
        # requests' connection/timeout families match curl/httr2 transport
        # failures. InvalidURL and arbitrary implementation errors stay permanent.
        transient = isinstance(error, (requests.exceptions.ConnectionError,
                                       requests.exceptions.Timeout)) or bool(
                                           _TRANSIENT_MESSAGE.search(message))
        return None, None, redact_secrets(message), transient

    if not isinstance(response, Mapping):
        response = {"error": "Requester did not return a response mapping."}
    raw_status = response.get("status")
    # bool is an int in Python, but cannot be an HTTP status. R accepts integer,
    # exact integral numeric and three-digit character statuses, so do the same.
    status_valid = raw_status is None or (
        not isinstance(raw_status, bool) and
        isinstance(raw_status, (int, float, str)) and
        re.fullmatch(r"[1-5][0-9][0-9]", str(raw_status)) is not None)
    if isinstance(raw_status, float) and raw_status.is_integer():
        status_valid = 100 <= raw_status <= 599
    status = int(raw_status) if status_valid and raw_status is not None else None
    raw_url = response.get("final_url")
    final_url = raw_url if isinstance(raw_url, str) and raw_url else None
    raw_error = response.get("error")
    error = raw_error if isinstance(raw_error, str) else None
    malformed = []
    if raw_status is not None and not status_valid:
        malformed.append("invalid status")
    if status is not None and final_url is None:
        malformed.append("missing final URL")
    if raw_error is not None and error is None:
        malformed.append("invalid error field")
    if malformed:
        error = "Malformed requester response: " + ", ".join(malformed) + "."
    elif status is None and error is None:
        error = "Requester returned neither an HTTP status nor an error."
    transient = (status in (408, 429) or 500 <= status <= 599) if status is not None else (
        not malformed and error is not None and bool(_TRANSIENT_MESSAGE.search(error)))
    return (status, redact_secrets(final_url) if final_url is not None else None,
            redact_secrets(error) if error is not None else None, transient)


def verify_sdp_semantic_iris(
    path: Union[str, Path],
    report_path: Optional[Union[str, Path]] = None,
    *,
    requester: Callable = _request,
    sleep_fn: Callable = time.sleep,
) -> pd.DataFrame:
    """Dereference exact selected HTTP semantic IRIs and persist every result.

    GET follows redirects and reads response headers without downloading the
    final or redirect body. The default transport has a 30-second worker deadline, including
    startup, DNS, connection and redirects; an expired worker is killed and
    reaped. Injected requesters keep their own execution behavior.
    Only HTTP 408, 429, 5xx,
    or classified transient transport failures retry, at most three times,
    after delays of 0.1 and 0.25 seconds. Candidate suggestions, arbitrary data
    URLs and non-HTTP identifiers are excluded; manifest-bound SSSOM semantic
    references are included after validation. No model provider is called.

    Parameters
    ----------
    path : str or pathlib.Path
        Existing Salmon Data Package directory.
    report_path : str or pathlib.Path, optional
        Defaults to ``reproducibility/provenance/semantic-iri-dereference.csv``
        inside the package. The default refuses symlinked output paths.
    requester : callable, optional
        Offline test hook receiving one exact IRI and returning a mapping with
        ``status`` and ``final_url``; a status-less error may use ``error``.
    sleep_fn : callable, optional
        Offline test hook receiving the retry delay in seconds.

    Returns
    -------
    pandas.DataFrame
        UTF-8/C-sorted rows with ``iri``, ``status``, ``final_url``, ``error``
        and ``attempts``. Stable CSV bytes contain no timestamps.

    Raises
    ------
    ValueError
        Invalid metadata, no selected HTTP IRI, or failed dereferences. For
        dereference failures, the complete report is atomically written before
        an aggregate exception names every failed IRI and final status.

    Notes
    -----
    HTTP success proves resolution only. A fragment's HTTP 200 does not prove
    RDF term presence or semantic suitability. An aborted atomic write keeps
    the prior report; atomic replacement does not promise crash durability.
    The Requests transport currently treats HTTP 103 Early Hints as the final
    response rather than continuing to the eventual 200. Such a server fails
    this check with a retained status 103; it is a client compatibility limit,
    not proof of a missing identifier. This remains unresolved parity debt.
    """
    root = _extension_root(path)
    default_path = Path(path) / _DEFAULT_REPORT
    if report_path is not None and (
        not isinstance(report_path, (str, Path)) or not str(report_path)
    ):
        raise ValueError("report_path must be one non-empty file path.")
    target = default_path if report_path is None else Path(report_path)
    if not callable(requester) or not callable(sleep_fn):
        raise ValueError("requester and sleep_fn must both be functions.")
    iris = _selected_iris(root)
    if not iris:
        raise ValueError("At least one HTTP semantic IRI is required for dereference checking.")
    rows = []
    for iri in iris:
        for attempt in range(1, len(_DELAYS) + 2):
            status, final_url, error, transient = _attempt(iri, requester)
            succeeded = status is not None and 200 <= status < 300 and bool(final_url)
            if succeeded or not transient or attempt > len(_DELAYS):
                break
            sleep_fn(_DELAYS[attempt - 1])
        rows.append((iri, status, final_url, error, attempt))
    results = pd.DataFrame(rows, columns=_COLUMNS)
    # Keep status as integer-or-missing, rather than inferred floating 200.0.
    results["status"] = pd.array(results.status, dtype="Int64")
    if target == default_path:
        _assert_safe_directory(root, "reproducibility/provenance", create=True)
        target = _assert_safe_file(root, _DEFAULT_REPORT, must_exist=False)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise ValueError(f"Refusing to atomically replace SDP metadata symlink {target}.")
    atomic_write(_csv_bytes(_COLUMNS, results), target)
    failed = [(iri, status) for iri, status, final_url, _, _ in rows
              if status is None or not 200 <= status < 300 or not final_url]
    if failed:
        details = ", ".join(f"{redact_secrets(iri)} (status={status if status is not None else 'request-error'})"
                            for iri, status in failed)
        raise ValueError(f"Semantic IRI(s) did not dereference successfully: {details}.")
    return results


__all__ = ["verify_sdp_semantic_iris"]
