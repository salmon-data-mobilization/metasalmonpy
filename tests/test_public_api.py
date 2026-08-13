from importlib.metadata import version

import metasalmonpy
from metasalmonpy.version_check import check_for_updates


def test_public_version_matches_package_metadata():
    assert metasalmonpy.__version__ == version("metasalmonpy")


def test_current_workflow_exports_are_public():
    expected = {
        "chat_decomposition",
        "check_for_updates",
        "create_sdp",
        "detect_semantic_term_gaps",
        "edh_build_hnap_xml",
        "render_ontology_term_request",
        "validate_salmon_datapackage",
        "write_edh_xml_from_sdp",
        "write_salmon_datapackage",
    }
    assert expected <= set(metasalmonpy.__all__)
    assert all(callable(getattr(metasalmonpy, name)) for name in expected)


def test_update_check_is_explicit_and_uses_canonical_repository():
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "tag_name": "v0.1.7",
                "html_url": "https://github.com/example/release",
                "name": "0.1.7",
            }

    calls = []

    def request(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    result = check_for_updates(
        current="0.1.6",
        quiet=True,
        request_fn=request,
    )

    assert result["status"] == "update_available"
    assert result["latest_version"] == "0.1.7"
    assert calls[0][0].endswith(
        "/repos/salmon-data-mobilization/metasalmonpy/releases/latest"
    )
