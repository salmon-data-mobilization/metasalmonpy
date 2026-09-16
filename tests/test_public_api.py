import re
from importlib.metadata import version
from pathlib import Path

import pytest

import metasalmonpy
from metasalmonpy.version_check import check_for_updates

REPO_ROOT = Path(__file__).parent.parent

# The nine functions and two accessors metasalmon 0.5.0 added, whose whole point
# is a review that never opens a CSV in a spreadsheet. AGENTS.md's mirror
# contract makes the version number a claim that this surface is delivered, and
# "delivered" includes being documented as the workflow -- which is what the
# 0.5.0 NEWS entry leads with.
REVIEW_SURFACE = (
    "review_semantics",
    "accept_suggestion",
    "reject_suggestion",
    "apply_sdp_semantics",
    "review_metadata",
    "set_sdp_dataset",
    "set_sdp_table",
    "set_sdp_column",
    "set_sdp_code",
    "semantic_suggestions",
    "semantic_llm_assessments",
)


def _repo_file(relative: str) -> Path:
    """A repository file this test needs, skipped when only a wheel is present.

    These guards read files that are not packaged into the wheel. Every
    supported way of running the suite -- a source checkout, and the editable
    install both CI legs build -- has them, so a skip here means the suite is
    being run against something else rather than that the check is optional.

    Retires when: the suite stops being runnable from anything but the source
    tree, at which point the absent case cannot arise and this helper can
    assert instead of skipping.
    """
    path = REPO_ROOT / relative
    if not path.is_file():
        pytest.skip(f"{relative} is not present; not a source checkout")
    return path


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
    pyproject = _repo_file("pyproject.toml").read_text(encoding="utf-8")
    declared = re.search(r'(?m)^version = "([^"]+)"', pyproject)
    assert declared is not None, "pyproject.toml has no static version"
    assert declared.group(1) == metasalmonpy.__version__


def test_the_lockfile_declares_the_same_version():
    """``uv.lock``'s own ``metasalmonpy`` entry must not lag the bump.

    AGENTS.md enumerates the places the version lives precisely because a
    missed one drifts silently, and this is the place that actually did: the
    0.4.0 release shipped with a lockfile still claiming 0.2.1, so the next
    contributor's ``uv pip install -e .`` rewrote the line inside an unrelated
    pull request. ``uv lock --check`` catches it too, but only where ``uv`` is
    installed; this reads the bytes.

    Retires when: ``uv.lock`` stops recording this project's own version, or
    the lockfile stops being committed.
    """
    lock = _repo_file("uv.lock").read_text(encoding="utf-8")
    entry = re.search(r'(?m)^name = "metasalmonpy"\nversion = "([^"]+)"', lock)
    assert entry is not None, "uv.lock has no metasalmonpy version entry"
    assert entry.group(1) == metasalmonpy.__version__


def test_the_documentation_site_declares_the_same_version():
    """``_quarto.yml``'s ``quartodoc.version`` is the number the site shows.

    The fifth version place, and the one AGENTS.md's enumeration left out until
    2026-09-16 -- found still reading ``0.4.0`` while the other four were being
    moved to 0.5.0, which is the drift the enumeration exists to prevent
    happening inside the enumeration's own gap. Parsed with a line scan rather
    than PyYAML, because this test also runs on the core-dependencies leg where
    ``yaml`` is deliberately not importable.

    Retires when: ``_quarto.yml`` reads the version from package metadata
    instead of restating it.
    """
    lines = _repo_file("_quarto.yml").read_text(encoding="utf-8").splitlines()
    declared = [
        match.group(1)
        for match in (re.match(r'^  version: "([^"]+)"$', line) for line in lines)
        if match is not None
    ]
    assert declared, "_quarto.yml declares no quartodoc version"
    assert declared == [metasalmonpy.__version__]


def test_the_semantic_review_guide_documents_the_review_surface():
    """The guide has to name the calls, not only the reference index.

    metasalmon 0.5.0's headline claim is that a package reaches strict
    validation "without opening a single file in a spreadsheet", and this
    package's version number is a parity claim that it delivers that. The nine
    functions landed here on 2026-09-16 (pull request #28) with the guide naming
    none of them except one passing mention of ``apply_sdp_semantics()`` inside
    the closure section -- so a reader following the guide had no review path at
    all, while ``_quarto.yml`` already listed all eleven under "Review and edit
    (in Python)". A reference entry per function is not a workflow.

    Retires when: the guide is generated from the same source as the reference
    index, so the two cannot disagree about which calls exist.
    """
    guide = _repo_file("guides/semantic-review.qmd").read_text(encoding="utf-8")
    missing = [name for name in REVIEW_SURFACE if name not in guide]
    assert not missing, "guides/semantic-review.qmd does not name: " + ", ".join(
        missing
    )
    unexported = [name for name in REVIEW_SURFACE if name not in metasalmonpy.__all__]
    assert not unexported, "the guide names what this package does not export: " + ", ".join(
        unexported
    )


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
