from importlib.metadata import version

import metasalmonpy
from metasalmonpy.version_check import check_for_updates


def test_public_version_matches_package_metadata():
    assert metasalmonpy.__version__ == version("metasalmonpy")


def test_the_two_declared_version_strings_agree():
    """``pyproject.toml`` and ``__init__.py`` must not drift apart.

    The version number is a parity claim under the mirror contract, and these
    two literals have drifted before — which is why bumping both is a
    checklist item in the S10 execplan. This reads the files directly so it
    holds without an install step. Retirement condition: delete this test only
    if the version stops being declared in two places (for example if
    ``pyproject.toml`` learns to read ``__version__`` dynamically).
    """
    import re
    from pathlib import Path

    pyproject = (Path(__file__).parent.parent / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    declared = re.search(r'(?m)^version = "([^"]+)"', pyproject)
    assert declared is not None, "pyproject.toml has no static version"
    assert declared.group(1) == metasalmonpy.__version__


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
