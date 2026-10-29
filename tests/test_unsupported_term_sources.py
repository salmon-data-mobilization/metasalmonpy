"""Public find_terms diagnostics distinguish unsupported sources from no match."""

import io
import json
import urllib.request
from pathlib import Path

import pytest

from metasalmonpy import find_terms

SNAPSHOT = Path(__file__).parent / "data" / "ontology_release" / "smn-0.0.3"


@pytest.fixture
def ols_response(monkeypatch):
    """Replace only the HTTP boundary; search and ranking run normally."""
    response = {"status": 200, "docs": []}

    def urlopen(request, **kwargs):
        assert request.full_url.startswith("https://www.ebi.ac.uk/ols4/api/search?")
        body = io.BytesIO(json.dumps({"response": {"docs": response["docs"]}}).encode())
        body.status = response["status"]
        return body

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return response


def test_an_unsupported_source_is_not_a_successful_empty_search():
    with pytest.warns(RuntimeWarning, match="Vocabulary lookup was incomplete"):
        result = find_terms("escapement", sources=[" PSC ", "psc"], expand_query=False)

    assert result.empty
    diagnostics = result.attrs["diagnostics"]
    assert list(diagnostics["source"]) == ["psc"]
    assert list(diagnostics["status"]) == ["unsupported"]
    assert list(diagnostics["count"]) == [0]
    assert "Unsupported vocabulary source" in diagnostics.iloc[0]["error"]


def test_a_local_match_keeps_its_evidence_and_reports_unsupported_sources():
    with pytest.warns(RuntimeWarning, match="Vocabulary lookup was incomplete"):
        result = find_terms("escapement", sources=["psc", "smn"], expand_query=False,
                            snapshot_dir={"smn": SNAPSHOT})

    assert "https://w3id.org/smn/Escapement" in list(result["iri"])
    diagnostics = result.attrs["diagnostics"].set_index("source")
    assert diagnostics.loc["smn", "status"] == "success"
    assert diagnostics.loc["psc", "status"] == "unsupported"
    assert list(result.attrs["ontology_release"]["version"]) == ["0.0.3"]


def test_a_supported_source_with_no_matches_is_still_a_success(ols_response):
    result = find_terms("escapement", sources=["ols"], expand_query=False)

    assert result.empty
    diagnostic = result.attrs["diagnostics"].iloc[0]
    assert diagnostic["status"] == "success"
    assert diagnostic["count"] == 0
    assert diagnostic["error"] is None


@pytest.mark.parametrize("status,expected", [(200, "success"), (503, "http_error")])
def test_a_mixed_lookup_keeps_supported_empty_or_failed_status(ols_response, status, expected):
    ols_response["status"] = status
    with pytest.warns(RuntimeWarning, match="Vocabulary lookup was incomplete"):
        result = find_terms("escapement", sources=["psc", "ols"], expand_query=False)

    assert result.empty
    diagnostics = result.attrs["diagnostics"]
    assert list(diagnostics["source"]) == ["psc", "ols"]
    assert list(diagnostics["status"]) == ["unsupported", expected]
    assert list(diagnostics["count"]) == [0, 0]


def test_a_mixed_lookup_with_unsupported_source_does_not_freeze_supported_results(ols_response, monkeypatch):
    monkeypatch.setenv("METASALMONPY_CACHE", "1")
    with pytest.warns(RuntimeWarning, match="Vocabulary lookup was incomplete"):
        first = find_terms("escapement", sources=["psc", "ols"], expand_query=False)
    ols_response["docs"] = [{
        "label": "Escapement", "iri": "https://example.org/Escapement",
        "ontology_name": "example", "type": "class", "description": ["Synthetic search result."],
    }]
    with pytest.warns(RuntimeWarning, match="Vocabulary lookup was incomplete"):
        second = find_terms("escapement", sources=["psc", "ols"], expand_query=False)

    assert first.empty
    assert list(second["iri"]) == ["https://example.org/Escapement"]
    assert list(second.attrs["diagnostics"]["status"]) == ["unsupported", "success"]
