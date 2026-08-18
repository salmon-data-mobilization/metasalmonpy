"""What ``overwrite=True`` is allowed to delete.

metasalmon 0.2.0 turned ``overwrite`` from "empty the directory" into "replace
only the files this writer owns", because the read → edit → write loop was
silently deleting reviewed SSSOM mapping sets, ordered measurement
decompositions, EML and EDH XML, ``eml-mapping.yml``, review notes and
``publication/`` artifacts. ``prune=True`` restores the old behaviour for a
caller who genuinely wants it.

The symlink guards in the same release are here too: ``Path.exists()`` follows
links, so a ``data/`` or ``metadata/`` replaced by one would make every managed
child resolve outside the package and be deleted there.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pandas as pd
import pytest

from metasalmonpy import read_salmon_datapackage, write_salmon_datapackage

R_PACKAGE = Path(__file__).resolve().parent / "data" / "resource_types" / "r-package"

SIDECARS = (
    "metadata/semantic/mapping-sets.json",
    "metadata/semantic/measurement-decompositions.json",
    "metadata/metadata-edh-hnap.xml",
    "eml-mapping.yml",
    "README-review.txt",
    "publication/recovery-manifest.json",
    "reproducibility/manifest.json",
)


def _package(tmp_path, name="pkg"):
    target = tmp_path / name
    shutil.copytree(R_PACKAGE, target)
    (target / ".metasalmonpy-package").write_text("metasalmonpy-owned\n", encoding="utf-8")
    for relative in SIDECARS:
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"reviewed {relative}\n", encoding="utf-8")
    return target


def _rewrite(target, **kwargs):
    package = read_salmon_datapackage(str(target))
    return write_salmon_datapackage(
        resources=package["resources"],
        dataset_meta=package["dataset"],
        table_meta=package["tables"],
        dict_df=package["dictionary"],
        codes=package["codes"],
        path=str(target),
        overwrite=True,
        **kwargs,
    )


def test_reviewed_sidecars_survive_a_rewrite(tmp_path):
    target = _package(tmp_path)
    _rewrite(target)
    for relative in SIDECARS:
        assert (target / relative).exists(), relative
        assert (target / relative).read_text(encoding="utf-8") == f"reviewed {relative}\n"


def test_prune_restores_the_delete_everything_behaviour(tmp_path):
    target = _package(tmp_path)
    _rewrite(target, prune=True)
    for relative in SIDECARS:
        assert not (target / relative).exists(), relative
    # The package itself is still written.
    assert (target / "data" / "obs.csv").exists()
    assert (target / "metadata" / "column_dictionary.csv").exists()


def test_prune_requires_overwrite(tmp_path):
    package = read_salmon_datapackage(str(R_PACKAGE))
    with pytest.raises(ValueError, match="prune=True requires overwrite=True"):
        write_salmon_datapackage(
            resources=package["resources"],
            dataset_meta=package["dataset"],
            table_meta=package["tables"],
            dict_df=package["dictionary"],
            codes=package["codes"],
            path=str(tmp_path / "new"),
            prune=True,
        )


def test_a_data_resource_a_previous_write_declared_is_removed(tmp_path):
    """An orphan would leave undeclared data a hand-made ZIP would carry."""
    target = _package(tmp_path)
    # The package this call will write, read before the orphan is planted, so
    # the writer genuinely does not declare it.
    package = read_salmon_datapackage(str(target))
    orphan = target / "data" / "old.csv"
    orphan.write_text("a\n1\n", encoding="utf-8")
    tables = target / "metadata" / "tables.csv"
    previous = pd.read_csv(tables, dtype=str).fillna("")
    extra = previous.iloc[[0]].copy()
    extra["table_id"] = "old"
    extra["file_name"] = "data/old.csv"
    pd.concat([previous, extra]).to_csv(tables, index=False)

    with pytest.warns(UserWarning, match="no longer declared"):
        write_salmon_datapackage(
            resources=package["resources"],
            dataset_meta=package["dataset"],
            table_meta=package["tables"],
            dict_df=package["dictionary"],
            codes=package["codes"],
            path=str(target),
            overwrite=True,
        )
    assert not orphan.exists()
    # And a file the writer never declared is untouched.
    assert (target / "README-review.txt").exists()


def test_an_undeclared_file_under_data_is_not_deleted(tmp_path):
    target = _package(tmp_path)
    stray = target / "data" / "notes.txt"
    stray.write_text("kept\n", encoding="utf-8")
    _rewrite(target)
    assert stray.read_text(encoding="utf-8") == "kept\n"


def test_a_symlinked_managed_directory_is_refused(tmp_path):
    target = _package(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "obs.csv").write_text("stolen\n", encoding="utf-8")
    package = read_salmon_datapackage(str(target))
    shutil.rmtree(target / "data")
    (target / "data").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symbolic-link path component"):
        write_salmon_datapackage(
            resources=package["resources"],
            dataset_meta=package["dataset"],
            table_meta=package["tables"],
            dict_df=package["dictionary"],
            codes=package["codes"],
            path=str(target),
            overwrite=True,
        )
    assert (outside / "obs.csv").read_text(encoding="utf-8") == "stolen\n"


def test_a_symlinked_package_root_is_refused(tmp_path):
    real = _package(tmp_path, name="real")
    package = read_salmon_datapackage(str(real))
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    for spelling in (str(link), str(link) + "/", str(link) + "/."):
        with pytest.raises(ValueError, match="package root is a symbolic link"):
            write_salmon_datapackage(
                resources=package["resources"],
                dataset_meta=package["dataset"],
                table_meta=package["tables"],
                dict_df=package["dictionary"],
                codes=package["codes"],
                path=spelling,
                overwrite=True,
                prune=True,
            )
    assert (real / "README-review.txt").exists()


def test_a_root_spelled_with_a_trailing_parent_reference_is_refused(tmp_path):
    """The one spelling no lexical check can make safe.

    ``readlink(2)`` resolves every component but the last, so ``link/..``
    inspects ``..`` *inside* the target and the root then denotes the target's
    parent — which can be an unrelated package.
    """
    real = _package(tmp_path, name="real")
    package = read_salmon_datapackage(str(real))
    with pytest.raises(ValueError, match=r"ends in '\.\.'"):
        write_salmon_datapackage(
            resources=package["resources"],
            dataset_meta=package["dataset"],
            table_meta=package["tables"],
            dict_df=package["dictionary"],
            codes=package["codes"],
            path=str(real / "data" / ".."),
            overwrite=True,
        )


def test_a_symlinked_metadata_file_is_refused_before_it_is_read(tmp_path):
    """The containment check runs before ``tables.csv`` is parsed.

    ``_package_managed_paths()`` reads the previous ``tables.csv``, so a
    ``metadata/tables.csv`` pointing at a FIFO or an enormous external file
    would be read before any guard ran.
    """
    target = _package(tmp_path)
    package = read_salmon_datapackage(str(target))
    outside = tmp_path / "elsewhere.csv"
    outside.write_text("dataset_id,table_id,file_name\nx,y,data/z.csv\n", encoding="utf-8")
    tables = target / "metadata" / "tables.csv"
    tables.unlink()
    tables.symlink_to(outside)

    with pytest.raises(ValueError, match="symbolic-link path component"):
        write_salmon_datapackage(
            resources=package["resources"],
            dataset_meta=package["dataset"],
            table_meta=package["tables"],
            dict_df=package["dictionary"],
            codes=package["codes"],
            path=str(target),
            overwrite=True,
        )
    assert outside.exists()


def test_overwrite_still_refuses_a_directory_this_package_does_not_own(tmp_path):
    target = tmp_path / "someone-elses"
    target.mkdir()
    (target / "important.txt").write_text("keep\n", encoding="utf-8")
    package = read_salmon_datapackage(str(R_PACKAGE))
    with pytest.raises(ValueError, match="Refusing to overwrite"):
        write_salmon_datapackage(
            resources=package["resources"],
            dataset_meta=package["dataset"],
            table_meta=package["tables"],
            dict_df=package["dictionary"],
            codes=package["codes"],
            path=str(target),
            overwrite=True,
        )
    assert (target / "important.txt").exists()
