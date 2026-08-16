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
from pathlib import Path
from typing import Union


def default_file_mode() -> int:
    """The permission bits a normal (umask-respecting) file write produces.

    ``os.umask`` is a get-and-set call, so the current value has to be read by
    setting it and immediately putting it back.
    """
    current = os.umask(0)
    os.umask(current)
    return 0o666 & ~current


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
