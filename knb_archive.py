"""Deterministic SDP archive (mirrors metasalmon's ``R/knb-sdp-archive.R`` at v0.1.7).

KNB presents every aggregated DataONE object as an individual catalog item.
Publishing the canonical SDP's internal metadata files one at a time is
therefore both noisy and easy to misinterpret. This module builds one
reproducible ZIP representation of the SDP from a deliberately closed
inventory. It reuses the KNB publication allowlists instead of scanning the
package directory, so EML, publication receipts, editor backups, and other
local material cannot be swept into the archive by accident.

Determinism reference: R guards the compressor with
``.ms_knb_reviewed_zip_versions`` -- an allowlist of reviewed ``zip`` package
versions (``"3.0.1"``, ``"3.0.2"``) plus a runtime abort -- because that
package's exact bytes are baked into DataONE PIDs and resumable manifests. It
is an allowlist, not a single-version pin: a new version is byte-compared
against a reviewed one and then added. Python has no third-party ZIP writer to
guard, so this module *defines* its own reference —
stdlib :mod:`zipfile` with fixed member order (radix), fixed timestamps
(2000-01-01T00:00:00), fixed permissions (0644), Unix ``create_system``, no
directory entries, and deflate level 9. The bytes are reproducible here and
are **not** comparable to R's; the contract that is mirrored is the closed
inventory, the member ordering, and the fail-closed checks (PARITY.md
entries 4, 17, 18).

This module and ``knb_publication`` mirror one R namespace, so they are
mutually dependent the way R's two topic files are: shared path and hash
helpers live in ``knb_publication`` and are imported here at module level,
while ``knb_publication`` imports this module inside the two functions that
need it.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Union

from .knb_publication import (
    _atomic_write_raw,
    _declared_data_paths,
    _inside_path,
    _lexical_absolute_path,
    _locate_metadata_file,
    _object_bytes,
    _read_metadata_csv,
    _reject_dot_segments,
    _relative_path,
    _sdp_artifact_paths,
    _sha256_raw,
)

#: The Python determinism reference this module implements. Bump it only as a
#: reviewed package change: the archive checksum is an immutable DataONE PID
#: input, which is the same reason R keeps a reviewed-``zip``-version
#: allowlist rather than accepting whatever compressor is installed.
ARCHIVE_DETERMINISM_REFERENCE = "metasalmonpy-zipfile-1"

_ARCHIVE_DATE_TIME = (2000, 1, 1, 0, 0, 0)
_ARCHIVE_FILE_MODE = 0o644
_ARCHIVE_UNIX_SYSTEM = 3
_ARCHIVE_COMPRESS_LEVEL = 9


def _safe_path_slug(value: object) -> str:
    """Mirror ``.ms_safe_path_slug``."""
    slug = "" if value is None else str(value)
    if not slug.strip():
        slug = "dataset"
    slug = slug.strip().lower()
    slug = re.sub(r"[^a-z0-9._-]+", "-", slug)
    slug = re.sub(r"^[-._]+|[-._]+$", "", slug)
    return slug or "dataset"


def _sdp_archive_filename(dataset_id: object) -> str:
    """Mirror ``.ms_knb_sdp_archive_filename``."""
    if dataset_id is None or not str(dataset_id).strip():
        raise ValueError(
            "dataset_id must be one non-empty value for the SDP archive "
            "filename."
        )
    return _safe_path_slug(dataset_id) + "-salmon-data-package.zip"


def _sdp_archive_dataset_id(path: Union[str, Path]) -> str:
    """Mirror ``.ms_knb_sdp_archive_dataset_id``."""
    dataset_path = _locate_metadata_file(path, "dataset.csv")
    if dataset_path is None:
        raise FileNotFoundError(
            "SDP archiving requires canonical metadata/dataset.csv."
        )
    dataset = _read_metadata_csv(dataset_path)
    if len(dataset) != 1 or "dataset_id" not in dataset.columns:
        raise ValueError(
            "SDP archiving requires one dataset.csv$dataset_id value."
        )
    dataset_id = str(dataset["dataset_id"].iloc[0])
    if not dataset_id.strip():
        raise ValueError(
            "SDP archiving requires one non-empty dataset.csv$dataset_id "
            "value."
        )
    return dataset_id


def _sdp_archive_relative_labels(
    paths: Dict[str, str], prefix: str
) -> List[str]:
    """Mirror ``.ms_knb_sdp_archive_relative_labels``."""
    expected_prefix = prefix + ":"
    labels = list(paths.keys())
    if any(not label.startswith(expected_prefix) for label in labels):
        raise ValueError("Internal SDP archive inventory labels are invalid.")
    return [label[len(expected_prefix):] for label in labels]


def _sdp_archive_path(root: str, relative: str) -> str:
    """Mirror ``.ms_knb_sdp_archive_path``."""
    return os.path.join(root, *relative.split("/"))


def _sdp_archive_assert_no_symlink(
    root: str, relative: str, require_file: bool = True
) -> bool:
    """Mirror ``.ms_knb_sdp_archive_assert_no_symlink``."""
    current = root
    for part in relative.split("/"):
        current = os.path.join(current, part)
        if os.path.islink(current):
            raise ValueError(
                f"SDP archive member {relative} must not contain a "
                "symbolic-link path component."
            )
        if not os.path.exists(current):
            break

    if require_file and (
        not os.path.exists(current) or not os.path.isfile(current)
    ):
        raise ValueError(
            f"SDP archive member {relative} must be a regular file."
        )
    return True


def _sdp_archive_validate_relative(relative: object) -> str:
    """Mirror ``.ms_knb_sdp_archive_validate_relative``."""
    text = str(relative).replace("\\", "/")
    if (
        not text
        or text.startswith("/")
        or re.match(r"^[A-Za-z]:/", text)
        or text.endswith("/")
        or "//" in text
    ):
        raise ValueError(
            f"SDP archive member path {relative} is not a canonical relative "
            "file path."
        )
    _reject_dot_segments(text, "SDP archive inventory")

    # EML describes the archive and publication/ contains the archive itself
    # plus mutable upload receipts. Neither can be an archive member without
    # making the bundle self-referential or dependent on publication state.
    if text == "metadata/eml.xml" or text.startswith("publication/"):
        raise ValueError(
            "SDP archive inventory cannot include reserved publication path "
            f"{text}."
        )
    return text


def _sdp_archive_inventory(path: Union[str, Path]) -> "Dict[str, str]":
    """Mirror ``.ms_knb_sdp_archive_inventory``: the closed member allowlist."""
    lexical_root = _lexical_absolute_path(path)
    if not os.path.isdir(lexical_root):
        raise FileNotFoundError(f"SDP directory {path} does not exist.")
    if os.path.islink(lexical_root):
        raise ValueError("The SDP directory itself must not be a symbolic link.")
    root = os.path.realpath(lexical_root)

    # These two helpers are the single source of truth for the KNB package
    # inventory. In particular, the artifact helper validates any declared
    # SSSOM and ordered measurement-decomposition manifests before returning
    # their closed member lists.
    data_paths = _declared_data_paths(root)
    artifact_paths = _sdp_artifact_paths(root)
    paths = list(data_paths.values()) + list(artifact_paths.values())
    relative = [
        _sdp_archive_validate_relative(member)
        for member in _sdp_archive_relative_labels(data_paths, "data")
        + _sdp_archive_relative_labels(artifact_paths, "sdp_artifact")
    ]

    duplicated = sorted(
        {member for member in relative if relative.count(member) > 1}
    )
    if duplicated:
        raise ValueError(
            "SDP archive inventory contains duplicate member path(s): "
            + ", ".join(duplicated)
            + "."
        )

    for index, member in enumerate(relative):
        _sdp_archive_assert_no_symlink(root, member)
        resolved = os.path.realpath(_sdp_archive_path(root, member))
        if resolved != paths[index] or _relative_path(root, resolved) != member:
            raise ValueError(
                f"SDP archive member {member} does not resolve to its "
                "canonical package path."
            )

    order = sorted(range(len(relative)), key=lambda index: relative[index])
    return {relative[index]: paths[index] for index in order}


def _sdp_archive_stage(inventory: Dict[str, str], staging: str) -> str:
    """Mirror ``.ms_knb_sdp_archive_stage``."""
    os.makedirs(staging, exist_ok=True)
    if not os.path.isdir(staging):
        raise ValueError(
            "Could not create the temporary SDP archive staging directory."
        )

    for member, source in inventory.items():
        destination = _sdp_archive_path(staging, member)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        if os.path.exists(destination):
            raise ValueError(
                f"Could not stage exact bytes for SDP archive member {member}."
            )
        shutil.copyfile(source, destination)
        if _sha256_raw(_object_bytes(source)) != _sha256_raw(
            _object_bytes(destination)
        ):
            raise ValueError(
                f"Could not stage exact bytes for SDP archive member {member}."
            )

    # Validate the copied bytes too. This closes the small gap between
    # checking a source manifest and asking the ZIP implementation to read its
    # artifacts.
    if os.path.exists(
        os.path.join(staging, "metadata", "semantic", "mapping-sets.json")
    ):
        from .sssom import validate_sdp_sssom

        validate_sdp_sssom(staging)
    if os.path.exists(
        os.path.join(
            staging, "metadata", "semantic", "measurement-decompositions.json"
        )
    ):
        from .measurement_decompositions import (
            validate_sdp_measurement_decompositions,
        )

        validate_sdp_measurement_decompositions(staging)
    return staging


def _archive_bytes(inventory: Dict[str, str], staging: str) -> bytes:
    """Serialize the staged tree under this module's determinism reference.

    ZIP records each member's timestamp and Unix permission bits; both are
    written from constants so source mtimes, umasks, owners, and temporary
    roots cannot affect the resulting archive bytes. ZIP's DOS timestamp range
    starts in 1980; 2000 is deliberately unambiguous and portable.
    """
    # A scratch file in the system temp directory: :mod:`zipfile` needs a seekable
    # target, and the bytes are read back and the file deleted before returning.
    # It is never installed anywhere, so unlike every *published* artifact it
    # does not need ``atomic_io``'s umask-default mode -- 0600 is right for a
    # throwaway. The published write is ``_atomic_write_raw`` at the end of
    # ``_write_sdp_archive``, and that one does go through the shared writer.
    handle, temporary = tempfile.mkstemp(
        prefix=".metasalmon-sdp-archive-", suffix=".zip"
    )
    os.close(handle)
    try:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=_ARCHIVE_COMPRESS_LEVEL,
        ) as archive:
            for member in inventory:
                info = zipfile.ZipInfo(member, date_time=_ARCHIVE_DATE_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = _ARCHIVE_UNIX_SYSTEM
                info.external_attr = _ARCHIVE_FILE_MODE << 16
                archive.writestr(
                    info,
                    _object_bytes(_sdp_archive_path(staging, member)),
                    compresslevel=_ARCHIVE_COMPRESS_LEVEL,
                )
        with zipfile.ZipFile(temporary) as archive:
            archived_members = archive.namelist()
        if archived_members != list(inventory):
            raise ValueError(
                "Generated SDP archive inventory does not exactly match its "
                "closed source allowlist."
            )
        return _object_bytes(temporary)
    finally:
        if os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _sdp_archive_descriptor(
    path: str,
    dataset_id: str,
    members: Sequence[str],
    payload: Optional[bytes] = None,
) -> Dict[str, object]:
    """Mirror ``.ms_knb_sdp_archive_descriptor``."""
    if payload is None:
        payload = _object_bytes(path)
    return {
        "path": os.path.realpath(path),
        "file_name": os.path.basename(path),
        "dataset_id": dataset_id,
        "format_id": "application/zip",
        "media_type": "application/zip",
        "size": len(payload),
        "sha256": _sha256_raw(payload),
        "members": list(members),
    }


def _write_sdp_archive(
    path: Union[str, Path],
    output_path: Optional[Union[str, Path]] = None,
    overwrite: bool = False,
) -> Dict[str, object]:
    """Mirror ``.ms_knb_write_sdp_archive``."""
    if not isinstance(overwrite, bool):
        raise ValueError("overwrite must be True or False.")

    inventory = _sdp_archive_inventory(path)
    root = os.path.realpath(str(path))
    dataset_id = _sdp_archive_dataset_id(root)
    if output_path is None:
        output_path = os.path.join(
            root, "publication", _sdp_archive_filename(dataset_id)
        )
    output_path = str(output_path)
    if not output_path.strip():
        raise ValueError("output_path must be one non-empty path.")
    if os.path.splitext(output_path)[1].lower() != ".zip":
        raise ValueError("output_path must use a .zip extension.")

    lexical_output = _lexical_absolute_path(output_path)
    root_prefix = root + os.sep
    if lexical_output.startswith(root_prefix):
        _sdp_archive_assert_no_symlink(
            root,
            lexical_output[len(root_prefix):].replace("\\", "/"),
            require_file=False,
        )
    output_path = _inside_path(
        root, lexical_output, must_work=os.path.exists(lexical_output)
    )
    output_relative = _relative_path(
        root, output_path, must_work=os.path.exists(output_path)
    )
    if not output_relative.startswith("publication/"):
        raise ValueError(
            "output_path must remain under the SDP's publication/ directory."
        )
    _sdp_archive_assert_no_symlink(root, output_relative, require_file=False)

    directory = os.path.dirname(output_path)
    if not os.path.isdir(directory):
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError:
            pass
    if not os.path.isdir(directory):
        raise ValueError(
            f"Could not create SDP archive output directory {directory}."
        )
    _sdp_archive_assert_no_symlink(
        root, os.path.dirname(output_relative), require_file=False
    )

    staging = tempfile.mkdtemp(prefix=".metasalmon-sdp-archive-")
    try:
        _sdp_archive_stage(inventory, staging)
        archive_bytes = _archive_bytes(inventory, staging)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    if os.path.exists(output_path):
        if os.path.islink(output_path):
            raise ValueError(
                "Refusing to replace symbolic-link SDP archive output."
            )
        existing_bytes = _object_bytes(output_path)
        if existing_bytes == archive_bytes:
            return _sdp_archive_descriptor(
                output_path, dataset_id, list(inventory), payload=existing_bytes
            )
        if not overwrite:
            # Without the remedy the only way forward was to work out that a
            # manual delete was required, which made every re-plan after a
            # corrected input a dead end (metasalmon 0.2.3).
            raise ValueError(
                "SDP archive output already exists with different bytes and "
                "overwrite is False. Review the existing publication artifact "
                "before replacing it. To rebuild it from the current inputs, "
                f"pass overwrite=True. Existing: {output_path}."
            )

    _atomic_write_raw(archive_bytes, output_path)
    return _sdp_archive_descriptor(
        output_path, dataset_id, list(inventory), payload=archive_bytes
    )


__all__ = ["ARCHIVE_DETERMINISM_REFERENCE"]
