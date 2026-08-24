"""Closed SDP reproducibility manifests (metasalmon 0.1.8 parity).

``tests/data/sdp-extensions/repro-sdp`` is genuine metasalmon **v0.1.8**
output. The manifest this package writes for the same declarations is
byte-identical to it apart from the two provenance values, which honestly name
the writer (PARITY.md row 29) -- ``test_manifest_bytes_match_r_apart_from_
provenance`` asserts exactly that, so a drift in ordering, hashing, sizing or
JSON encoding fails while the deliberate difference does not.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

pd = pytest.importorskip("pandas")

from metasalmonpy import (
    read_sdp_reproducibility_manifest,
    validate_sdp_reproducibility_manifest,
    write_sdp_reproducibility_manifest,
)
from metasalmonpy.reproducibility import ReproducibilityManifestError

DATA = Path(__file__).parent / "data" / "sdp-extensions"
CHECKSUMS = json.loads((DATA / "checksums.json").read_text(encoding="utf-8"))

ARTIFACTS = [
    ("reproducibility/workflow/transform.R", "workflow", "text/x-r-source"),
    ("reproducibility/workflow/semantic-release.R", "workflow", "text/x-r-source"),
    ("reproducibility/workflow/semantic_suggestions.csv", "workflow", "text/csv"),
    (
        "reproducibility/reviewed_semantic_selections.csv",
        "reviewed_semantic_selections",
        "text/csv",
    ),
    ("reproducibility/source/source-manifest.csv", "source", "text/csv"),
    ("reproducibility/provenance/activities.csv", "provenance", "text/csv"),
]


def _declarations(rows=None) -> pd.DataFrame:
    return pd.DataFrame(
        list(ARTIFACTS if rows is None else rows),
        columns=["path", "role", "media_type"],
    )


def _sdp(tmp_path: Path) -> Path:
    target = tmp_path / "repro-sdp"
    shutil.copytree(DATA / "repro-sdp", target)
    return target


def test_the_committed_fixture_is_unmodified_r_output():
    name = "repro-sdp/reproducibility/manifest.json"
    digest = hashlib.sha256((DATA / name).read_bytes()).hexdigest()
    assert digest == CHECKSUMS["sha256"][name]


def test_reads_and_validates_an_r_written_manifest(tmp_path):
    root = _sdp(tmp_path)
    manifest = read_sdp_reproducibility_manifest(root)

    assert list(manifest) == ["profile", "artifacts", "provenance"]
    assert manifest["profile"] == "metasalmon-reproducibility-manifest/1.0"
    assert (
        manifest["provenance"]["generated_by"]
        == "metasalmon::write_sdp_reproducibility_manifest"
    )
    assert validate_sdp_reproducibility_manifest(root) is True


def test_manifest_bytes_match_r_apart_from_provenance(tmp_path):
    root = _sdp(tmp_path)
    r_bytes = (root / "reproducibility" / "manifest.json").read_bytes()

    # Deliberately shuffled input: ordering is the writer's job, not the
    # caller's, and R's committed bytes are the canonical order.
    shuffled = _declarations(ARTIFACTS[::-1])
    write_sdp_reproducibility_manifest(root, shuffled, overwrite=True)
    py_bytes = (root / "reproducibility" / "manifest.json").read_bytes()

    assert py_bytes != r_bytes  # the provenance block differs, by design
    r_manifest = json.loads(r_bytes)
    py_manifest = json.loads(py_bytes)
    assert py_manifest["artifacts"] == r_manifest["artifacts"]
    assert py_manifest["profile"] == r_manifest["profile"]
    assert (
        py_manifest["provenance"]["generated_by"]
        == "metasalmonpy.write_sdp_reproducibility_manifest"
    )
    # Everything outside the provenance object is byte-identical: swapping the
    # two provenance values back reproduces R's file exactly.
    py_manifest["provenance"] = r_manifest["provenance"]
    assert (
        json.dumps(py_manifest, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8") == r_bytes


def test_paths_are_ordered_by_codepoint(tmp_path):
    root = _sdp(tmp_path)
    manifest = read_sdp_reproducibility_manifest(root)
    paths = [artifact["path"] for artifact in manifest["artifacts"]]
    assert paths == sorted(paths)
    # The pair that a locale-sensitive sort reorders: '-' before '_' in C,
    # the other way round in many collations.
    assert paths.index("reproducibility/workflow/semantic-release.R") < paths.index(
        "reproducibility/workflow/semantic_suggestions.csv"
    )


def test_a_changed_artifact_fails_the_checksum(tmp_path):
    root = _sdp(tmp_path)
    (root / "reproducibility" / "workflow" / "transform.R").write_text(
        "changed\n", encoding="utf-8"
    )
    with pytest.raises(ReproducibilityManifestError, match="SHA-256|size"):
        validate_sdp_reproducibility_manifest(root)


def test_an_undeclared_file_breaks_closure(tmp_path):
    # This is the whole point of the manifest: a publication adapter must never
    # need to guess which of the files it finds are meant to be published.
    root = _sdp(tmp_path)
    (root / "reproducibility" / "private.txt").write_text(
        "private note\n", encoding="utf-8"
    )
    with pytest.raises(ReproducibilityManifestError, match="undeclared|closed"):
        validate_sdp_reproducibility_manifest(root)


def test_a_missing_declared_file_is_refused(tmp_path):
    root = _sdp(tmp_path)
    (root / "reproducibility" / "source" / "source-manifest.csv").unlink()
    with pytest.raises(ReproducibilityManifestError, match="missing|closed"):
        validate_sdp_reproducibility_manifest(root)


def test_a_role_must_match_its_canonical_location(tmp_path):
    root = _sdp(tmp_path)
    rows = list(ARTIFACTS)
    rows[0] = (rows[0][0], "source", rows[0][2])
    with pytest.raises(ReproducibilityManifestError, match="role|location"):
        write_sdp_reproducibility_manifest(root, _declarations(rows), overwrite=True)


def test_an_unknown_role_is_refused(tmp_path):
    root = _sdp(tmp_path)
    rows = list(ARTIFACTS)
    rows[0] = (rows[0][0], "notes", rows[0][2])
    with pytest.raises(ReproducibilityManifestError, match="role must be one of"):
        write_sdp_reproducibility_manifest(root, _declarations(rows), overwrite=True)


def test_an_escaping_path_is_refused(tmp_path):
    root = _sdp(tmp_path)
    rows = list(ARTIFACTS)
    # Role-consistent so the role/location check (which R runs first) passes
    # and the safe-path check is the one under test. Both messages verified
    # against R v0.1.8 rather than assumed.
    rows[0] = (
        "reproducibility/workflow/../../metadata/dataset.csv",
        "workflow",
        "text/csv",
    )
    with pytest.raises(ReproducibilityManifestError, match="not a safe"):
        write_sdp_reproducibility_manifest(root, _declarations(rows), overwrite=True)

    # A path that escapes the role directory fails the earlier check instead,
    # exactly as it does in R.
    rows[0] = ("reproducibility/../metadata/dataset.csv", "workflow", "text/csv")
    with pytest.raises(ReproducibilityManifestError, match="canonical path location"):
        write_sdp_reproducibility_manifest(root, _declarations(rows), overwrite=True)


def test_a_malformed_media_type_is_refused(tmp_path):
    root = _sdp(tmp_path)
    rows = list(ARTIFACTS)
    rows[0] = (rows[0][0], rows[0][1], "not a media type")
    with pytest.raises(ReproducibilityManifestError, match="media type"):
        write_sdp_reproducibility_manifest(root, _declarations(rows), overwrite=True)


def test_the_manifest_itself_cannot_be_declared(tmp_path):
    root = _sdp(tmp_path)
    rows = list(ARTIFACTS) + [
        ("reproducibility/manifest.json", "workflow", "application/json")
    ]
    # The manifest has no canonical role directory, so it fails the
    # role/location check first -- R gives the same message.
    with pytest.raises(ReproducibilityManifestError, match="canonical path location"):
        write_sdp_reproducibility_manifest(root, _declarations(rows), overwrite=True)


def test_a_symlinked_artifact_is_refused(tmp_path):
    root = _sdp(tmp_path)
    outside = tmp_path / "outside.R"
    outside.write_text("message('outside')\n", encoding="utf-8")
    target = root / "reproducibility" / "workflow" / "transform.R"
    target.unlink()
    try:
        target.symlink_to(outside)
    except OSError:  # pragma: no cover - filesystem without symlinks
        pytest.skip("Filesystem does not permit symlink creation")
    with pytest.raises(ReproducibilityManifestError, match="symlink"):
        write_sdp_reproducibility_manifest(root, _declarations(), overwrite=True)


def test_a_failed_overwrite_preserves_the_prior_manifest(tmp_path):
    # The manifest is a recovery record. An incomplete rewrite must not leave
    # the package holding a newly invalid one.
    root = _sdp(tmp_path)
    manifest_path = root / "reproducibility" / "manifest.json"
    before = manifest_path.read_bytes()

    incomplete = _declarations(ARTIFACTS[1:])
    with pytest.raises(ReproducibilityManifestError, match="undeclared|closed"):
        write_sdp_reproducibility_manifest(root, incomplete, overwrite=True)

    assert manifest_path.read_bytes() == before
    assert validate_sdp_reproducibility_manifest(root) is True


def test_writing_over_an_existing_manifest_needs_overwrite(tmp_path):
    root = _sdp(tmp_path)
    with pytest.raises(FileExistsError, match="overwrite"):
        write_sdp_reproducibility_manifest(root, _declarations())


def test_only_a_symlinked_package_root_is_refused(tmp_path):
    root = _sdp(tmp_path)
    linked = tmp_path / "linked-sdp"
    try:
        linked.symlink_to(root, target_is_directory=True)
    except OSError:  # pragma: no cover - filesystem without symlinks
        pytest.skip("Filesystem does not permit directory symlink creation")
    for candidate in (str(linked), str(linked) + os.sep):
        with pytest.raises(ReproducibilityManifestError, match="symlink"):
            write_sdp_reproducibility_manifest(
                candidate, _declarations(), overwrite=True
            )
    assert validate_sdp_reproducibility_manifest(root) is True


def test_the_manifest_must_end_with_a_final_newline(tmp_path):
    root = _sdp(tmp_path)
    manifest_path = root / "reproducibility" / "manifest.json"
    manifest_path.write_bytes(manifest_path.read_bytes().rstrip(b"\n"))
    with pytest.raises(ReproducibilityManifestError, match="final newline"):
        read_sdp_reproducibility_manifest(root)


def test_an_empty_declaration_set_is_refused(tmp_path):
    root = _sdp(tmp_path)
    with pytest.raises(ReproducibilityManifestError, match="non-empty"):
        write_sdp_reproducibility_manifest(
            root,
            pd.DataFrame(columns=["path", "role", "media_type"]),
            overwrite=True,
        )


def _rewrite_provenance(root, **changes):
    """Rewrite the manifest's provenance block in place and return its path."""
    manifest_path = root / "reproducibility" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for key, value in changes.items():
        if value is _DROP:
            manifest["provenance"].pop(key, None)
        else:
            manifest["provenance"][key] = value
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return manifest_path


_DROP = object()


def test_a_version_that_is_not_a_string_is_refused(tmp_path):
    # metasalmon 0.4.0: a ``metasalmon_version`` that is whitespace-only, or
    # not a string at all, is rejected rather than accepted -- which is what
    # the decomposition validator already did on both sides. This validator
    # coerced the value with ``str()``, so a JSON number, boolean or array
    # sailed through and the manifest claimed a version it did not have.
    for value in (1.8, True, ["0.4.0"], {"v": "0.4.0"}, 0, "", "   ", "\t\n"):
        root = _sdp(tmp_path / f"case-{abs(hash(repr(value)))}")
        _rewrite_provenance(root, metasalmon_version=value)
        with pytest.raises(ReproducibilityManifestError, match="provenance"):
            validate_sdp_reproducibility_manifest(root)


def test_a_python_written_manifest_validates(tmp_path):
    # The reciprocal of metasalmon 0.4.0's fix: each implementation accepts the
    # other's honestly-named provenance block.
    root = _sdp(tmp_path)
    _rewrite_provenance(
        root,
        generated_by="metasalmonpy.write_sdp_reproducibility_manifest",
        metasalmon_version=_DROP,
        metasalmonpy_version="0.4.0",
    )
    assert validate_sdp_reproducibility_manifest(root) is True


def test_an_unknown_writer_is_still_refused(tmp_path):
    root = _sdp(tmp_path)
    _rewrite_provenance(
        root, generated_by="someoneelse.write_sdp_reproducibility_manifest"
    )
    with pytest.raises(ReproducibilityManifestError, match="provenance"):
        validate_sdp_reproducibility_manifest(root)
