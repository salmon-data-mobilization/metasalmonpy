"""Regression tests for the umask-default file mode helper.

``atomic_io`` exists because ``tempfile.mkstemp()`` hard-codes 0600 while R's
``writeBin`` + ``file.rename`` leaves the umask default, so the mode has to be
restored explicitly before the rename (PARITY.md row 24). The tests here pin
*how* that mode is obtained: by measuring, never by mutating the process-wide
umask.
"""

import os
import stat

import pytest

from metasalmonpy import atomic_io


def _plain_write_mode(directory) -> int:
    """The mode an ordinary write into ``directory`` actually produces."""
    control = directory / "control.txt"
    control.write_text("x", encoding="utf-8")
    return stat.S_IMODE(control.stat().st_mode) & 0o777


def test_default_file_mode_never_calls_os_umask(monkeypatch, tmp_path):
    # os.umask is get-and-set, so the old implementation set 0 and restored
    # the real value. The umask is per-process, not per-thread: two concurrent
    # artifact writes can interleave those calls and leave the process at 000
    # permanently, publishing world-writable files. A raising sentinel is the
    # deterministic proof that neither entry point touches it any more -- a
    # thread race would only fail intermittently.
    def forbidden(*args, **kwargs):
        raise AssertionError("atomic_io must not mutate the process umask")

    monkeypatch.setattr(os, "umask", forbidden)

    mode = atomic_io.default_file_mode()
    assert mode == mode & 0o777
    assert mode & stat.S_IRUSR

    atomic_io.atomic_write(b"payload\n", tmp_path / "written.txt")
    assert (tmp_path / "written.txt").read_bytes() == b"payload\n"
    assert stat.S_IMODE((tmp_path / "written.txt").stat().st_mode) == mode


@pytest.mark.parametrize("umask_value", [0o022, 0o027, 0o077])
def test_default_file_mode_tracks_the_live_umask(umask_value, tmp_path):
    # The measured mode must equal what a plain write produces under the same
    # umask -- that equality is the whole contract, and it has to hold for a
    # non-default umask too, not just the conventional 022.
    previous = os.umask(umask_value)
    try:
        assert atomic_io.default_file_mode() == 0o666 & ~umask_value
        assert atomic_io.default_file_mode() == _plain_write_mode(tmp_path)
    finally:
        os.umask(previous)


def test_default_file_mode_restores_nothing_and_leaves_the_umask_intact(tmp_path):
    # Belt and braces for the reported failure mode: after any number of
    # calls the process umask is exactly what the caller set.
    previous = os.umask(0o027)
    try:
        for _ in range(5):
            atomic_io.default_file_mode()
            atomic_io.atomic_write(b"x", tmp_path / "artifact.txt")
        observed = os.umask(0o027)
        assert observed == 0o027
    finally:
        os.umask(previous)


def test_probe_leaves_no_file_behind(tmp_path, monkeypatch):
    # The probe creates a real file to read the kernel's answer; it must not
    # accumulate litter in the temp directory.
    monkeypatch.setattr(atomic_io.tempfile, "gettempdir", lambda: str(tmp_path))
    atomic_io.default_file_mode()
    assert list(tmp_path.iterdir()) == []


def test_atomic_write_publishes_at_the_umask_default_mode(tmp_path):
    atomic_io.atomic_write(b"bytes\n", tmp_path / "artifact.txt")
    written = stat.S_IMODE((tmp_path / "artifact.txt").stat().st_mode) & 0o777
    assert written == _plain_write_mode(tmp_path)
