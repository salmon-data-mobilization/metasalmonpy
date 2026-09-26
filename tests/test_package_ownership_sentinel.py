"""The package-ownership sentinel is ONE file shared by both implementations.

Hub item B-127, the mirror half of metasalmon's B-113. Brett ruled Q14 on
2026-08-24: "I want one share sentinel name. Nobody uses this yet so dont worry
about breaking changes." So metasalmonpy and metasalmon write and recognise the
same file name and the same content line, the break for a directory carrying
only a per-language sentinel is accepted, and neither writer removes the
other's file.

The name and content line are recorded in ``PARITY.md`` row 51 and in the hub's
``knowledge/parity-deviations.md`` row 51. metasalmon chose them in B-113 and
this package takes both from that row, so the two literals below are a
cross-repository contract: change them only together with row 51 in both
registers and metasalmon's ``tests/testthat/test-package-ownership-sentinel.R``,
which pins the same two. They are spelled out rather than read back from
``PACKAGE_SENTINEL`` and ``_package_ownership_bytes()``, because a pin that
reads the value it pins passes whatever the value is.

The four tests are the twins of that file's four, in the same order.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from metasalmonpy import read_salmon_datapackage, write_salmon_datapackage
from metasalmonpy.package_io import (
    PACKAGE_SENTINEL,
    _is_owned_package_dir,
    _package_managed_paths,
    _package_ownership_bytes,
)

SHARED_SENTINEL_NAME = ".sdp-package"
SHARED_SENTINEL_BYTES = b"sdp-owned\n"
PER_LANGUAGE_SENTINELS = {
    ".metasalmon-package": "metasalmon-owned",
    ".metasalmonpy-package": "metasalmonpy-owned",
}

# A package metasalmon wrote, read back as the artifacts every write below uses.
R_PACKAGE = Path(__file__).resolve().parent / "data" / "resource_types" / "r-package"


def _write(target: Path, **kwargs) -> Path:
    package = read_salmon_datapackage(str(R_PACKAGE))
    return write_salmon_datapackage(
        resources=package["resources"],
        dataset_meta=package["dataset"],
        table_meta=package["tables"],
        dict_df=package["dictionary"],
        codes=package["codes"],
        path=str(target),
        **kwargs,
    )


def _plant_per_language_sentinel(target: Path, name: str) -> None:
    # One line ending in LF, as each implementation wrote its own.
    (target / name).write_bytes(f"{PER_LANGUAGE_SENTINELS[name]}\n".encode("ascii"))


def test_the_ownership_sentinel_is_the_one_shared_file_name_and_content_line(tmp_path):
    # Row 51 describes the content line as ten ASCII bytes ending in a single LF.
    assert len(SHARED_SENTINEL_BYTES) == 10
    assert SHARED_SENTINEL_BYTES.isascii()
    assert SHARED_SENTINEL_BYTES.count(b"\n") == 1 and SHARED_SENTINEL_BYTES.endswith(b"\n")

    assert PACKAGE_SENTINEL == SHARED_SENTINEL_NAME
    assert _package_ownership_bytes() == SHARED_SENTINEL_BYTES

    target = _write(tmp_path / "pkg")

    # The bytes a real write puts on disk, not only what the helper returns.
    sentinel = target / SHARED_SENTINEL_NAME
    assert sentinel.is_file()
    assert sentinel.read_bytes() == SHARED_SENTINEL_BYTES

    # No per-language sentinel is written. Listing every dot-file at the package
    # root catches one under any name, not only under the two old ones.
    assert sorted(p.name for p in target.iterdir() if p.name.startswith(".")) == [
        SHARED_SENTINEL_NAME
    ]


def test_a_directory_carrying_only_the_shared_sentinel_is_recognised_as_owned(tmp_path):
    target = tmp_path / "sentinel-only"
    target.mkdir()
    # Written by hand rather than by this package, as a package metasalmon wrote
    # would arrive: nothing here but the shared file.
    (target / SHARED_SENTINEL_NAME).write_bytes(SHARED_SENTINEL_BYTES)

    assert _is_owned_package_dir(target)

    # Recognised means `overwrite=True` may replace it: without the sentinel
    # this directory holds no SDP CSVs, and the write would be refused.
    _write(target, overwrite=True)
    assert read_salmon_datapackage(str(target))["dataset"]["dataset_id"].iloc[0] == "diff-1"
    assert (target / SHARED_SENTINEL_NAME).read_bytes() == SHARED_SENTINEL_BYTES


@pytest.mark.parametrize("legacy", sorted(PER_LANGUAGE_SENTINELS))
def test_a_directory_carrying_only_a_per_language_sentinel_is_not_recognised_as_owned(
    tmp_path, legacy
):
    # The compatibility break Q14 accepted. A package that still has its SDP
    # CSVs is recognised by them, so this refuses only a directory whose sole
    # claim to being a package is an old sentinel.
    target = tmp_path / "legacy-only"
    target.mkdir()
    _plant_per_language_sentinel(target, legacy)

    assert not _is_owned_package_dir(target)
    with pytest.raises(ValueError, match="Refusing to overwrite non-metasalmonpy directory"):
        _write(target, overwrite=True)
    assert [p.name for p in target.iterdir()] == [legacy]


def test_the_managed_path_inventory_names_only_the_shared_sentinel_so_a_rewrite_keeps_an_old_one(
    tmp_path,
):
    target = _write(tmp_path / "pkg")
    for legacy in PER_LANGUAGE_SENTINELS:
        _plant_per_language_sentinel(target, legacy)

    managed = _package_managed_paths(target, [])
    assert target / SHARED_SENTINEL_NAME in managed
    assert not {p.name for p in managed} & set(PER_LANGUAGE_SENTINELS)

    # An unmanaged file survives a rewrite, so both old sentinels stay exactly as
    # they were. Q14 rules out either writer removing the other's file, and
    # removing or renaming one would be a migration nobody ruled.
    _write(target, overwrite=True)
    for legacy, content in PER_LANGUAGE_SENTINELS.items():
        assert (target / legacy).read_bytes() == f"{content}\n".encode("ascii"), legacy
    assert (target / SHARED_SENTINEL_NAME).read_bytes() == SHARED_SENTINEL_BYTES
