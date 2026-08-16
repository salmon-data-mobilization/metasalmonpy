"""Umask-respecting atomic file writes shared by the artifact writers.

metasalmon writes every artifact with ``tempfile()`` + ``writeBin()`` +
``file.rename()``. R's ``writeBin`` opens the file through the C library, so
the result carries the process umask — 0644 on a normal system.

``tempfile.mkstemp()`` hard-codes ``0600`` for security (it is designed for
secrets), and ``os.replace`` preserves the source file's mode, so the direct
Python transliteration silently published private-to-owner SDP artifacts:
mapping sets, decomposition CSVs and manifests, and reviewed EML. That is not
a security improvement in this context, it is a parity break that makes a
published package unreadable to collaborators and to a web server.

Every writer therefore restores the umask-default mode before the rename.
The helper lives here rather than being repeated per module because the bug
is invisible from the call site: nothing in ``mkstemp(...)`` says ``0600``.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path
from typing import Optional, Union

# What a plain write yields when the probe below cannot run at all (a
# read-only or missing temp directory). 0644 is the conventional 022 umask
# result, which is what R's writeBin produces on every machine this package
# has been exercised on. This constant retires the day a supported platform
# offers a read-only umask query the probe can use instead; POSIX defines
# ``umask`` as get-and-set, so as of Python 3.13 there is none in the stdlib.
_FALLBACK_FILE_MODE = 0o644


def _probe_default_file_mode() -> Optional[int]:
    """Measure the umask default by creating one throwaway file.

    ``os.open`` asks the kernel for 0666 and the kernel subtracts the umask
    itself, so the mode ``fstat`` reports back IS ``0666 & ~umask`` -- the
    same number, obtained without ever mutating process-wide state.

    The mutating alternative (``os.umask(0)`` then restore) is wrong here
    because the umask is per-*process*, not per-thread: two concurrent
    artifact writes can interleave the set/restore pair so that one restores
    the temporary zero after the other restored the real value, leaving the
    process at 000 permanently and publishing world-writable files. A lock
    would only serialize *this* module's calls, not the rest of the process,
    so the non-mutating probe is preferred over serializing the dance.
    """
    name = f".metasalmon-umask-probe-{os.getpid()}-{uuid.uuid4().hex}"
    probe = os.path.join(tempfile.gettempdir(), name)
    try:
        handle = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
    except OSError:  # pragma: no cover - unwritable temp directory
        return None
    try:
        # Mask to 0o777: a setgid temp directory can add 0o2000 to the new
        # file, and that bit is not part of "what a plain write produces".
        return os.fstat(handle).st_mode & 0o777
    finally:
        os.close(handle)
        try:
            os.unlink(probe)
        except OSError:  # pragma: no cover - probe already gone
            pass


def default_file_mode() -> int:
    """The permission bits a normal (umask-respecting) file write produces."""
    mode = _probe_default_file_mode()
    return _FALLBACK_FILE_MODE if mode is None else mode


def apply_default_file_mode(path: Union[str, Path]) -> None:
    """Give ``path`` the mode a plain write would have produced."""
    try:
        os.chmod(path, default_file_mode())
    except OSError:  # pragma: no cover - filesystem without chmod support
        pass


def atomic_write(data: bytes, path: Union[str, Path]) -> None:
    """Write ``data`` to ``path`` via a same-directory temporary and rename."""
    path = Path(path)
    handle, temporary = tempfile.mkstemp(
        prefix=f".{path.name}-", dir=str(path.parent)
    )
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        apply_default_file_mode(temporary)
        os.replace(temporary, str(path))
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


__all__ = ["apply_default_file_mode", "atomic_write", "default_file_mode"]
