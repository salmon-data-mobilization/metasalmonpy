"""Pinned ontology releases (tern ECOSYSTEM M-12).

``find_terms()`` and ``fetch_salmon_ontology()`` reading a release snapshot of
smn or gcdfo. No test here reaches the network: every ``requests.get()`` is
answered by a stub, and the ones that expect no request at all stub it with an
error. metasalmon's ``tests/testthat/test-ontology-release.R`` pins the same
behaviour against the same fixtures, ``tests/data/ontology_release/``, which
are metasalmon's ``tests/testthat/fixtures/ontology-release/`` vendored
unchanged; ``tests/test_ontology_release_parity.py`` holds the two copies to
each other.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

import pandas as pd
import pytest
import requests

from metasalmonpy import OntologyReleaseError, fetch_salmon_ontology, find_terms, term_search
from metasalmonpy.term_search_smn import _smn_index_empty, _smn_release_index, parse_smn_ttl_modules

FIXTURES = Path(__file__).resolve().parent / "data" / "ontology_release"
SMN_PAGES = "https://salmon-data-mobilization.github.io/salmon-domain-ontology/releases/"
SMN_MANIFEST_URL = f"{SMN_PAGES}0.0.3/MANIFEST.sha256"


def _sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _snapshot(tmp_path: Path, name: str, manifest=True) -> str:
    """A copy of a fixture snapshot in its own directory.

    ``manifest`` is True to write a MANIFEST.sha256 from the copied bytes, False
    for none, or a list of manifest lines to write as given. Written at test
    time, so a checkout that rewrites line endings cannot make a correct manifest
    look wrong.
    """
    directory = tmp_path / f"{name}-{len(list(tmp_path.iterdir()))}"
    shutil.copytree(FIXTURES / name, directory)
    if manifest is True:
        lines = [f"{_sha256(path)}  {path.name}" for path in sorted(directory.iterdir())]
    elif manifest is False:
        lines = None
    else:
        lines = list(manifest)
    if lines is not None:
        (directory / "MANIFEST.sha256").write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    return str(directory)


def _answer(url, status, body=b""):
    """An answer in the shape ``requests.get()`` returns, its body already read."""
    response = requests.Response()
    response.status_code = status
    response.url = url
    response._content = body if isinstance(body, bytes) else body.encode("utf-8")
    response._content_consumed = True
    return response


def _serve_file(path):
    data = Path(path).read_bytes()
    return lambda url, accept: _answer(url, 200, data)


@pytest.fixture
def no_network(monkeypatch):
    def get(url, *args, **kwargs):
        raise AssertionError(f"unexpected request to {url}")

    monkeypatch.setattr(requests, "get", get)


@pytest.fixture
def routes(monkeypatch):
    """Stubs ``requests.get()`` with a mapping from each URL to a function of the
    URL and the Accept header sent; an unrouted URL answers 404. Returns the
    mapping, to fill in, and the log of every request made."""
    table: dict = {}
    log: list = []

    def get(url, headers=None, **kwargs):
        accept = (headers or {}).get("Accept")
        log.append((url, accept))
        route = table.get(url)
        if route is None:
            return _answer(url, 404)
        return route(url, accept)

    monkeypatch.setattr(requests, "get", get)
    return table, log


@pytest.fixture
def session_cache(monkeypatch, tmp_path):
    cache = tmp_path / "session-cache"
    monkeypatch.setattr(term_search, "_release_session_cache_root", lambda: str(cache))
    return cache


# ---------------------------------------------------------------------------
# The smn release reader
# ---------------------------------------------------------------------------


def _release_index() -> pd.DataFrame:
    return _smn_release_index((FIXTURES / "smn-0.0.3" / "smn.owl").read_bytes())


def test_the_release_reader_gives_smn_terms_the_hints_the_module_reader_gives_them():
    # The release is one merged RDF/XML graph; the latest read is the modules.
    # Both build rows with _smn_index_row(), and the release reader makes up for
    # the two ways a release differs: the serializer's owl:NamedIndividual
    # declarations, and the module names it no longer carries. Remove either
    # adjustment and AgeClassValue1's hints differ here.
    modules = ("01-entity-systematics", "02-observation-measurement", "07-controlled-vocabularies")
    latest = parse_smn_ttl_modules(
        {
            f"https://w3id.org/smn/modules/{module}": (FIXTURES / "smn-modules" / f"{module}.ttl").read_text(
                encoding="utf-8"
            )
            for module in modules
        }
    )
    release = _release_index()

    assert set(release["iri"]) == set(latest["iri"])
    by_iri = release.set_index("iri").loc[list(latest["iri"])]
    for column in ("role_hints", "label", "definition", "resource_kind"):
        assert list(by_iri[column]) == list(latest[column]), column
    assert list(release.columns) == list(latest.columns)

    # The same table is pinned in metasalmon's tests/testthat/test-ontology-release.R.
    hints = dict(zip(release["iri"].str.replace("https://w3id.org/smn/", "", regex=False), release["role_hints"]))
    assert dict(sorted(hints.items())) == {
        "AgeClassValue1": "constraint",
        "BroodYearBasis": "constraint",
        "Escapement": "variable",
        "MeanStatisticalModifier": "constraint|statistical_modifier",
        "StatisticalModifierScheme": "constraint|statistical_modifier",
        "Stock": "entity",
    }


def test_the_release_reader_leaves_out_the_ontology_header_and_foreign_subjects():
    release = _release_index()
    assert not release["iri"].str.contains("sosa", regex=False).any()
    assert "https://w3id.org/smn" not in set(release["iri"])
    assert not release["type_iris"].str.contains("NamedIndividual", regex=False).any()
    # A subject spread over two nodes is one row holding both nodes' values.
    stock = release[release["iri"] == "https://w3id.org/smn/Stock"]
    assert len(stock) == 1
    assert stock["definition"].iloc[0] == "A group of salmon managed as one unit."


def test_the_rdfxml_reader_reads_definitions_whatever_prefix_the_file_binds():
    # The fixture binds the OBO namespace to `ns1`, as the smn 0.0.3 release
    # does. metasalmon's reader looked prefixes up in the file and lost every
    # IAO definition here; this one matches by namespace, and this pins it.
    index = term_search._parse_salmon_rdfxml(
        (FIXTURES / "smn-0.0.3" / "smn.owl").read_bytes(), iri_pattern=term_search._SMN_IRI_PATTERN
    )
    escapement = index[index["iri"] == "https://w3id.org/smn/Escapement"]
    assert escapement["definition"].iloc[0].startswith("The number of mature salmon")


# ---------------------------------------------------------------------------
# find_terms() against a snapshot directory
# ---------------------------------------------------------------------------


def test_find_terms_searches_a_pinned_smn_snapshot_and_records_which_one(no_network, monkeypatch, tmp_path):
    def latest(*args, **kwargs):
        raise AssertionError("the latest smn index was read")

    monkeypatch.setattr(term_search, "_smn_term_index", latest)
    directory = _snapshot(tmp_path, "smn-0.0.3")

    result = find_terms(
        "escapement",
        role="variable",
        sources=["smn"],
        release={"smn": "0.0.3"},
        snapshot_dir={"smn": directory},
    )

    assert result["iri"].iloc[0] == "https://w3id.org/smn/Escapement"
    assert result["role_hints"].iloc[0] == "variable"
    record = result.attrs["ontology_release"]
    assert record.to_dict("records") == [
        {
            "ontology": "smn",
            "version": "0.0.3",
            "version_iri": "https://w3id.org/smn/0.0.3",
            "file": "smn.owl",
            "sha256": _sha256(Path(directory) / "smn.owl"),
            "manifest_verified": True,
            "source": directory,
        }
    ]


def test_a_snapshot_without_a_manifest_is_read_and_recorded_as_unverified(no_network, tmp_path):
    directory = _snapshot(tmp_path, "smn-0.0.3", manifest=False)

    result = find_terms("escapement", sources=["smn"], snapshot_dir={"smn": directory})

    record = result.attrs["ontology_release"]
    assert not record["manifest_verified"].iloc[0]
    # With no version pinned, the version the snapshot declares is recorded.
    assert record["version"].iloc[0] == "0.0.3"


def test_a_pinned_read_refuses_bytes_its_manifest_does_not_vouch_for(no_network, tmp_path):
    wrong = _snapshot(tmp_path, "smn-0.0.3", manifest=["0" * 64 + "  smn.owl"])
    with pytest.raises(OntologyReleaseError, match="does not match"):
        find_terms("escapement", sources=["smn"], snapshot_dir={"smn": wrong})
    # The caller's own snapshot is never touched.
    assert (Path(wrong) / "smn.owl").exists()

    unlisted = _snapshot(tmp_path, "smn-0.0.3", manifest=["0" * 64 + "  smn.ttl"])
    with pytest.raises(OntologyReleaseError, match="does not list"):
        find_terms("escapement", sources=["smn"], snapshot_dir={"smn": unlisted})

    malformed = _snapshot(tmp_path, "smn-0.0.3", manifest=["smn.owl is fine"])
    with pytest.raises(OntologyReleaseError, match="sha256sum"):
        find_terms("escapement", sources=["smn"], snapshot_dir={"smn": malformed})


def test_a_manifest_in_binary_mode_sha256sum_format_verifies_too(no_network, tmp_path):
    digest = _sha256(FIXTURES / "smn-0.0.3" / "smn.owl").upper()
    directory = _snapshot(tmp_path, "smn-0.0.3", manifest=[f"{digest} *./smn.owl"])
    result = find_terms("escapement", sources=["smn"], snapshot_dir={"smn": directory})
    assert bool(result.attrs["ontology_release"]["manifest_verified"].iloc[0])


def test_a_snapshot_that_declares_another_version_is_not_the_release_pinned(no_network, tmp_path):
    directory = _snapshot(tmp_path, "smn-0.0.3")
    with pytest.raises(OntologyReleaseError, match="is not release 0.0.4"):
        find_terms("escapement", sources=["smn"], release={"smn": "0.0.4"}, snapshot_dir={"smn": directory})


def test_smn_and_gcdfo_can_be_pinned_together_and_a_pin_is_read_only_when_searched(no_network, tmp_path):
    smn = _snapshot(tmp_path, "smn-0.0.3")
    gcdfo = _snapshot(tmp_path, "gcdfo-0.0.9")

    result = find_terms("conservation unit", sources=["smn", "gcdfo"], snapshot_dir={"smn": smn, "gcdfo": gcdfo})
    record = result.attrs["ontology_release"]
    assert list(record["ontology"]) == ["smn", "gcdfo"]
    assert list(record["version"]) == ["0.0.3", "0.0.9"]
    assert "https://w3id.org/gcdfo/salmon#ConservationUnit" in set(result["iri"])

    # gcdfo is pinned to a directory that does not exist, but not searched.
    only_smn = find_terms(
        "escapement",
        sources=["smn"],
        snapshot_dir={"smn": smn, "gcdfo": os.path.join(smn, "missing")},
    )
    assert list(only_smn.attrs["ontology_release"]["ontology"]) == ["smn"]


def test_a_snapshot_that_cannot_be_read_stops_the_call_instead_of_answering_nothing(no_network, tmp_path):
    with pytest.raises(OntologyReleaseError, match="does not exist"):
        find_terms("escapement", sources=["smn"], snapshot_dir={"smn": str(tmp_path / "nowhere")})
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(OntologyReleaseError, match="holds none of the files"):
        find_terms("escapement", sources=["smn"], snapshot_dir={"smn": str(empty)})


def test_release_and_snapshot_dir_are_checked_before_anything_is_searched(no_network):
    with pytest.raises(OntologyReleaseError, match="mapping"):
        find_terms("x", release="0.0.3")
    with pytest.raises(OntologyReleaseError, match="named by its ontology"):
        find_terms("x", release={3: "0.0.3"})
    with pytest.raises(OntologyReleaseError, match="only smn and gcdfo"):
        find_terms("x", release={"ols": "1.0.0"})
    with pytest.raises(OntologyReleaseError, match="three numbers"):
        find_terms("x", release={"smn": "v0.0.3"})
    with pytest.raises(OntologyReleaseError, match="more than once"):
        find_terms("x", release={"smn": "0.0.3", "SMN": "0.0.4"})
    # Checked even when the call would search nothing.
    with pytest.raises(OntologyReleaseError, match="three numbers"):
        find_terms("", release={"smn": "latest"})


def test_pinned_and_latest_results_do_not_share_a_cache_entry(no_network, monkeypatch, tmp_path):
    monkeypatch.setenv("METASALMONPY_CACHE", "1")
    monkeypatch.setattr(term_search, "_smn_term_index", lambda *args, **kwargs: _smn_index_empty())
    directory = _snapshot(tmp_path, "smn-0.0.3")

    pinned = find_terms("escapement", sources=["smn"], snapshot_dir={"smn": directory})
    latest = find_terms("escapement", sources=["smn"])

    assert len(pinned) > 0
    assert len(latest) == 0
    assert "ontology_release" not in latest.attrs


# ---------------------------------------------------------------------------
# find_terms() against a release downloaded from its version IRI
# ---------------------------------------------------------------------------


def test_a_pinned_release_is_downloaded_once_from_its_version_iri_and_verified(routes, session_cache):
    table, log = routes
    owl = FIXTURES / "smn-0.0.3" / "smn.owl"
    table["https://w3id.org/smn/0.0.3"] = _serve_file(owl)
    table[SMN_MANIFEST_URL] = lambda url, accept: _answer(url, 200, f"{_sha256(owl)}  smn.owl\n")

    first = find_terms("escapement", sources=["smn"], release={"smn": "0.0.3"})
    second = find_terms("brood year basis", sources=["smn"], release={"smn": "0.0.3"})

    assert [url for url, _ in log] == ["https://w3id.org/smn/0.0.3", SMN_MANIFEST_URL]
    assert log[0][1] == "application/rdf+xml"
    record = first.attrs["ontology_release"]
    assert bool(record["manifest_verified"].iloc[0])
    assert record["source"].iloc[0] == "https://w3id.org/smn/0.0.3"
    assert record["sha256"].iloc[0] == _sha256(owl)
    assert second.attrs["ontology_release"].equals(record)
    assert second["iri"].iloc[0] == "https://w3id.org/smn/BroodYearBasis"


def test_a_release_with_no_manifest_is_recorded_unverified_and_pages_answers_when_w3id_cannot(
    routes, session_cache
):
    table, log = routes

    def unreachable(url, accept):
        raise requests.ConnectionError("Could not resolve host (stub)")

    table["https://w3id.org/smn/0.0.3"] = unreachable
    table[f"{SMN_PAGES}0.0.3/smn.owl"] = _serve_file(FIXTURES / "smn-0.0.3" / "smn.owl")

    result = find_terms("escapement", sources=["smn"], release={"smn": "0.0.3"})

    assert not result.attrs["ontology_release"]["manifest_verified"].iloc[0]
    assert (session_cache / "smn" / "0.0.3" / "MANIFEST.sha256.absent").exists()
    assert len(log) == 3


def test_a_manifest_that_cannot_be_fetched_stops_the_read_and_the_next_call_tries_again(routes, session_cache):
    table, _ = routes
    owl = FIXTURES / "smn-0.0.3" / "smn.owl"
    table["https://w3id.org/smn/0.0.3"] = _serve_file(owl)
    table[SMN_MANIFEST_URL] = lambda url, accept: _answer(url, 503)

    with pytest.raises(OntologyReleaseError, match="Could not establish"):
        find_terms("escapement", sources=["smn"], release={"smn": "0.0.3"})

    table[SMN_MANIFEST_URL] = lambda url, accept: _answer(url, 200, f"{_sha256(owl)}  smn.owl\n")
    result = find_terms("escapement", sources=["smn"], release={"smn": "0.0.3"})
    assert bool(result.attrs["ontology_release"]["manifest_verified"].iloc[0])


def test_a_downloaded_copy_that_fails_its_manifest_is_removed_so_the_next_call_fetches_it_again(
    routes, session_cache
):
    table, _ = routes
    table["https://w3id.org/smn/0.0.3"] = _serve_file(FIXTURES / "smn-0.0.3" / "smn.owl")
    table[SMN_MANIFEST_URL] = lambda url, accept: _answer(url, 200, "a" * 64 + "  smn.owl\n")

    with pytest.raises(OntologyReleaseError, match="does not match"):
        find_terms("escapement", sources=["smn"], release={"smn": "0.0.3"})
    assert not (session_cache / "smn" / "0.0.3" / "smn.owl").exists()


def test_a_release_that_serves_no_file_is_an_error_not_an_empty_source(routes, session_cache):
    with pytest.raises(OntologyReleaseError, match="serves none of the files"):
        find_terms("escapement", sources=["smn"], release={"smn": "9.9.9"})


def test_the_session_download_directory_is_this_process_own():
    first = term_search._release_session_cache_root()
    assert os.path.isdir(first)
    assert term_search._release_session_cache_root() == first
    assert Path(first).name.startswith("metasalmonpy-ontology-releases-")


# ---------------------------------------------------------------------------
# fetch_salmon_ontology()
# ---------------------------------------------------------------------------


def test_fetch_salmon_ontology_returns_the_snapshot_file_accept_prefers(no_network, tmp_path):
    directory = _snapshot(tmp_path, "smn-0.0.3")

    assert fetch_salmon_ontology(snapshot_dir=directory) == os.path.join(directory, "smn.ttl")
    assert fetch_salmon_ontology(
        accept="text/turtle;q=0.5, application/rdf+xml", snapshot_dir=directory
    ) == os.path.join(directory, "smn.owl")
    # A representation the snapshot lacks gives way to the next one asked for.
    assert fetch_salmon_ontology(
        accept="application/ld+json, application/rdf+xml;q=0.9", snapshot_dir=directory
    ) == os.path.join(directory, "smn.owl")
    with pytest.raises(OntologyReleaseError, match="holds none of the files"):
        fetch_salmon_ontology(accept="application/ld+json", snapshot_dir=directory)
    with pytest.raises(OntologyReleaseError, match="no representation"):
        fetch_salmon_ontology(accept="text/html", snapshot_dir=directory)

    tampered = _snapshot(tmp_path, "smn-0.0.3", manifest=["0" * 64 + "  smn.ttl"])
    with pytest.raises(OntologyReleaseError, match="does not match"):
        fetch_salmon_ontology(snapshot_dir=tampered)


def test_fetch_salmon_ontology_downloads_a_release_into_cache_dir_once(routes, tmp_path):
    table, log = routes
    ttl = FIXTURES / "smn-0.0.3" / "smn.ttl"
    table["https://w3id.org/smn/0.0.3"] = _serve_file(ttl)
    table[SMN_MANIFEST_URL] = lambda url, accept: _answer(url, 200, f"{_sha256(ttl)}  smn.ttl\n")
    cache = str(tmp_path / "cache")

    first = fetch_salmon_ontology(release="0.0.3", cache_dir=cache)
    second = fetch_salmon_ontology(release="0.0.3", cache_dir=cache)

    expected = os.path.join(cache, "releases", "smn", "0.0.3", "smn.ttl")
    assert first == expected
    assert second == expected
    assert _sha256(first) == _sha256(ttl)
    assert len(log) == 2
    assert log[0][1] == "text/turtle"


def test_fetch_salmon_ontology_pins_only_smn_and_gcdfo_and_says_what_it_ignores(no_network, tmp_path):
    gcdfo = _snapshot(tmp_path, "gcdfo-0.0.9")
    assert fetch_salmon_ontology(
        url="https://w3id.org/gcdfo/salmon", accept="application/rdf+xml", snapshot_dir=gcdfo
    ) == os.path.join(gcdfo, "gcdfo.owl")
    with pytest.raises(OntologyReleaseError, match="smn or gcdfo"):
        fetch_salmon_ontology(url="https://example.org/onto", release="1.0.0")
    with pytest.raises(OntologyReleaseError, match="three numbers"):
        fetch_salmon_ontology(release="latest")
    smn = _snapshot(tmp_path, "smn-0.0.3")
    # The file is not parsed, so nothing could check the directory is 0.0.3.
    with pytest.raises(OntologyReleaseError, match="not both"):
        fetch_salmon_ontology(release="0.0.3", snapshot_dir=smn)
    with pytest.warns(UserWarning, match="not used"):
        fetch_salmon_ontology(snapshot_dir=smn, fallback_urls=["https://example.org/mirror"])
