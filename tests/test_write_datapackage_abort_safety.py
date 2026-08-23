"""An abort anywhere in ``write_salmon_datapackage()`` must leave the caller's
previously valid package intact.

Mirrors metasalmon's ``test-write-datapackage-abort-safety.R`` (PR #77, hub
backlog #96's ordering half). The Python defect was measured present on
2026-08-22 and is the same shape R fixed: ``_prepare_package_dir()`` unlinked
every managed path — or ``shutil.rmtree``-wiped the directory under ``prune`` —
and the descriptor build, the schema load, ``json.dump`` and the metadata CSV
writes all ran *afterwards*, so ANY exception in that window deleted
``metadata/`` and ``datapackage.json`` and left nothing in their place.

The ordering is the defect class; these tests pin the ordering, not one
trigger. The injection points are therefore *mocked* rather than provoked by
crafted inputs, so they stay valid when the individual input bugs are fixed —
and each one is deliberately chosen to sit **after** the old destructive point.
That last part is not automatic: R's first two candidate injections passed
against unfixed code because the helpers they targeted also ran during input
normalization, i.e. *before* the unlink, so the test proved nothing. Both
helpers mocked here (``render_resource_frame`` and
``_metadata_resource_entries``) are called exactly once in
``write_salmon_datapackage()``, downstream of ``_prepare_package_dir()``, and
were verified RED against the pre-fix ordering.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path

import pandas as pd
import pytest

from metasalmonpy import package_io
from metasalmonpy import read_salmon_datapackage, write_salmon_datapackage


def _fixture() -> dict:
    return {
        "resources": {"obs": pd.DataFrame({"site_id": ["s1", "s2"]})},
        "dataset_meta": pd.DataFrame(
            [
                {
                    "dataset_id": "d1",
                    "title": "Abort safety",
                    "description": "Regression for hub backlog #96, ordering half",
                    "creator": "metasalmonpy tests",
                    "license": "CC-BY-4.0",
                    "temporal_start": "2001-01-01",
                    "temporal_end": "2002-06-30",
                }
            ]
        ),
        "table_meta": pd.DataFrame(
            [
                {
                    "dataset_id": "d1",
                    "table_id": "obs",
                    "file_name": "data/obs.csv",
                    "table_label": "Observations",
                    "description": "One site column",
                }
            ]
        ),
        "dict_df": pd.DataFrame(
            [
                {
                    "dataset_id": "d1",
                    "table_id": "obs",
                    "column_name": "site_id",
                    "column_label": "Site",
                    "column_description": "Site identifier",
                    "column_role": "identifier",
                    "value_type": "string",
                    "required": False,
                }
            ]
        ),
    }


def _write(path: Path, fixture: dict, **kwargs) -> Path:
    return write_salmon_datapackage(
        resources=fixture["resources"],
        dataset_meta=fixture["dataset_meta"],
        table_meta=fixture["table_meta"],
        dict_df=fixture["dict_df"],
        path=str(path),
        overwrite=True,
        **kwargs,
    )


def _package_file_hashes(path: Path) -> dict:
    """Every file in the package, hashed.

    "Intact" therefore means byte-identical, not merely "a file still exists at
    that path" — the weaker check passes against a truncated or half-written
    replacement.
    """
    hashes = {}
    for candidate in sorted(Path(path).rglob("*")):
        if candidate.is_file() and not candidate.is_symlink():
            digest = hashlib.md5(candidate.read_bytes()).hexdigest()
            hashes[str(candidate.relative_to(path))] = digest
    return hashes


class _Injected(RuntimeError):
    """Distinct from every exception the writer raises on its own."""


def test_abort_after_the_old_unlink_point_leaves_the_package_byte_intact(
    tmp_path, monkeypatch
):
    target = tmp_path / "pkg"
    fixture = _fixture()
    _write(target, fixture)
    before = _package_file_hashes(target)
    assert "datapackage.json" in before

    # ``_metadata_resource_entries()`` runs when the descriptor is assembled —
    # after the destructive point of the pre-fix write path (managed paths
    # unlinked, only the data CSVs rewritten) and nowhere earlier in the call,
    # so an abort here destroyed metadata/ and datapackage.json.
    def _boom(*args, **kwargs):
        raise _Injected("injected post-unlink abort")

    monkeypatch.setattr(package_io, "_metadata_resource_entries", _boom)
    with pytest.raises(_Injected, match="injected post-unlink abort"):
        _write(target, fixture)
    monkeypatch.undo()

    assert _package_file_hashes(target) == before
    # Intact must also mean readable, not just present.
    read_salmon_datapackage(str(target))


def test_abort_while_rendering_a_data_resource_leaves_the_package_byte_intact(
    tmp_path, monkeypatch
):
    target = tmp_path / "pkg"
    fixture = _fixture()
    _write(target, fixture)
    before = _package_file_hashes(target)

    # ``render_resource_frame()`` runs once per resource inside the write loop
    # and nowhere before it — in the pre-fix ordering, after the unlink and
    # before any metadata write. This is the widest window in the old path:
    # every data CSV was written straight to its final name here, so a
    # multi-resource package could be left half-rewritten.
    def _boom(*args, **kwargs):
        raise _Injected("injected resource-render abort")

    monkeypatch.setattr(package_io, "render_resource_frame", _boom)
    with pytest.raises(_Injected, match="injected resource-render abort"):
        _write(target, fixture)
    monkeypatch.undo()

    assert _package_file_hashes(target) == before
    read_salmon_datapackage(str(target))


def test_prune_abort_leaves_the_package_and_its_sidecars_intact(tmp_path, monkeypatch):
    # ``prune=True`` accepted more deletion, not *earlier* deletion: the caller
    # asked to replace everything with a successfully written package. The wipe
    # must therefore run only after every input-dependent computation and the
    # full byte rendering have succeeded. (The residual prune window — pure
    # filesystem failure between the wipe and the install — is documented at
    # ``_commit_package_write()`` and in the writer's own docstring.)
    target = tmp_path / "pkg"
    fixture = _fixture()
    _write(target, fixture)
    sidecar = target / "README-review.txt"
    sidecar.write_text("reviewed content\n", encoding="utf-8")
    before = _package_file_hashes(target)

    def _boom(*args, **kwargs):
        raise _Injected("injected post-unlink abort")

    monkeypatch.setattr(package_io, "_metadata_resource_entries", _boom)
    with pytest.raises(_Injected, match="injected post-unlink abort"):
        _write(target, fixture, prune=True)
    monkeypatch.undo()

    assert _package_file_hashes(target) == before
    assert sidecar.exists()
    read_salmon_datapackage(str(target))


def test_a_successful_rewrite_leaves_no_staging_or_backup_scratch_behind(tmp_path):
    # The transactional install stages dot-prefixed siblings next to each
    # target and renames the originals aside; success must clean up every one.
    target = tmp_path / "pkg"
    fixture = _fixture()
    _write(target, fixture)
    _write(target, fixture)

    scratch = [
        str(candidate.relative_to(target))
        for candidate in target.rglob("*")
        if "-stage-" in candidate.name or "-backup-" in candidate.name
    ]
    assert scratch == []


def test_create_sdp_inherits_the_transactional_package_write(tmp_path, monkeypatch):
    """``create_sdp()`` reaches the package write through the same helper.

    It calls ``write_salmon_datapackage()`` for the package itself, so the
    transactional install covers the create path with no separate fix. Pinned
    here because "the helper is shared" is an implementation fact that a later
    refactor could quietly stop being true, and the create path is the one most
    users are actually on.

    Note the deliberate boundary: this covers the *package*. The three files
    ``create_sdp()`` writes on its own afterwards — ``README-review.txt``,
    ``semantic_suggestions.csv`` and ``metadata/metadata-edh-hnap.xml``, each
    through ``_replace_create_output()`` — are still unlink-then-rewrite and
    are NOT covered here. That is hub backlog #111's shape, measured present in
    this package, and it is a separate item rather than scope creep: single-file
    blast radius, and the files are ``create_sdp()``-owned rather than part of
    the writer's managed-path inventory.
    """
    from metasalmonpy import create_sdp

    resources = {"catches": pd.DataFrame({"fish_id": ["a", "b"], "count": [1, 2]})}
    target = tmp_path / "pkg"

    def _create():
        return create_sdp(
            resources,
            path=str(target),
            dataset_id="demo",
            seed_semantics=False,
            overwrite=True,
        )

    _create()
    before = _package_file_hashes(target)
    assert "datapackage.json" in before

    def _boom(*args, **kwargs):
        raise _Injected("injected post-unlink abort")

    monkeypatch.setattr(package_io, "_metadata_resource_entries", _boom)
    with pytest.raises(_Injected, match="injected post-unlink abort"):
        _create()
    monkeypatch.undo()

    assert _package_file_hashes(target) == before
    read_salmon_datapackage(str(target))


def test_metadata_csv_bytes_match_a_direct_to_csv_write(tmp_path):
    """Rendering to bytes must not change what the bytes are.

    The fix reorders *when* files are installed; it must not alter a single
    byte, because two whole packages were proved byte-identical to metasalmon's
    at S10 chunk D and this change is not allowed to break that. The former
    call wrote ``to_csv(path, ...)``; the write set needs ``to_csv(None, ...)``.
    pandas routes both through one encoder, but the line terminator for a path
    write is documented as defaulting to ``os.linesep`` — so this asserts the
    equality rather than trusting it, on whatever platform the suite runs.

    *Retires when:* the writer stops going through ``DataFrame.to_csv`` for
    metadata CSVs, at which point there is no second rendering path to compare
    against.
    """
    frame = pd.DataFrame(
        {
            "plain": ["x", "y", None],
            "comma": ["a,b", 'q"m', "line\nbreak"],
            "number": [1.0, None, 3.5],
            "flag": [True, False, True],
        }
    )
    on_disk_path = tmp_path / "direct.csv"
    prepared = frame.copy()
    prepared["flag"] = ["TRUE" if value else "FALSE" for value in prepared["flag"]]
    prepared.to_csv(on_disk_path, index=False, na_rep=package_io.csv_na_token())

    assert package_io._metadata_csv_bytes(frame) == on_disk_path.read_bytes()


def test_datapackage_json_bytes_match_a_direct_json_dump(tmp_path):
    """Same contract for the descriptor: the reorder must be byte-neutral.

    ``json.dump(obj, fp, indent=2)`` into a UTF-8 handle plus an explicit
    trailing newline is what the writer used to emit, and that newline was
    itself the last byte ever wrong here.

    *Retires when:* the descriptor stops going through ``json.dumps``.
    """
    descriptor = {
        "profile": "https://example.org/p",
        "name": "d1",
        "nested": {"a": [1, 2], "b": None, "c": True},
        "unicode": "Fraser sockeye — Chilko",
    }
    on_disk_path = tmp_path / "direct.json"
    with on_disk_path.open("w", encoding="utf-8") as handle:
        json.dump(descriptor, handle, indent=2)
        handle.write("\n")

    assert package_io._datapackage_json_bytes(descriptor) == on_disk_path.read_bytes()


def test_the_writer_performs_no_direct_filesystem_mutation(tmp_path):
    """Structural guard for the fix's ordering.

    ``write_salmon_datapackage()`` renders the full write set to bytes and
    hands it to ``_commit_package_write()``, the single place allowed to
    delete or replace anything. A direct write or unlink added back into the
    writer body would reopen the destroy-on-abort window this file pins, while
    every trigger-specific test above stayed green — which is exactly the
    failure mode ``AGENTS.md`` describes for a guard that does not say what it
    covers.

    *Retires when:* the write set is enforced by construction — the renderers
    return into a builder that owns the only filesystem handle, making a stray
    direct write in the writer body unrepresentable — or when
    ``write_salmon_datapackage()`` is replaced. Until then, keep the token list
    in sync with Python's filesystem-mutating vocabulary; it is a deliberately
    literal source scan, so a mutation reached through an alias this list does
    not name is not caught.
    """
    body = inspect.getsource(package_io.write_salmon_datapackage)
    mutating_tokens = (
        ".to_csv(",
        ".write_text(",
        ".write_bytes(",
        ".mkdir(",
        ".unlink(",
        ".rmdir(",
        "json.dump(",
        "shutil.rmtree(",
        "shutil.copy",
        "shutil.move(",
        "os.replace(",
        "os.remove(",
        "os.rename(",
        "os.makedirs(",
        ".open(\"w",
        ".open('w",
    )
    found = [token for token in mutating_tokens if token in body]
    assert found == [], (
        "write_salmon_datapackage() body contains direct filesystem calls: "
        + ", ".join(found)
    )
