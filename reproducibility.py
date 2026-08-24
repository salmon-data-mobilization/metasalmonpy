"""Closed SDP reproducibility manifests.

Mirrors metasalmon's ``R/reproducibility-manifest.R`` at the **v0.1.8** tag.

Reproducibility material can contain scripts, provenance records, reviewed
decisions, and a description of source inputs. These files are deliberately
outside the tabular SDP core. This manifest gives publication adapters a
closed, checksum-bound inventory so they never need to publish everything they
happen to find in a directory.

The writer does not discover files: it binds exactly the artifacts the caller
declares, and then refuses to succeed unless those declarations are *closed*
over the real ``reproducibility/`` tree. That is what stops an editor backup
or a private review note from reaching a public repository.

Ordering is codepoint (C) throughout — R uses ``sort(..., method = "radix")``
here precisely so that ``semantic-release.R`` and ``semantic_suggestions.csv``
cannot swap places between locales, and Python's ``sorted()`` already has that
property (PARITY.md row 3). ``locale.strxfrm`` stays banned.

Byte-parity contract: the manifest JSON is byte-identical to R's apart from
the ``provenance`` block, which honestly names the writer that produced it
(PARITY.md row 11's ruling applied here, and this validator accepts either
implementation's provenance so an R-written package stays readable).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Union

import pandas as pd

from . import provenance as _provenance
from .atomic_io import atomic_write

SDP_REPRODUCIBILITY_PROFILE = "metasalmon-reproducibility-manifest/1.0"
SDP_REPRODUCIBILITY_PATH = "reproducibility/manifest.json"
SDP_REPRODUCIBILITY_ROLES = (
    "reviewed_semantic_selections",
    "workflow",
    "provenance",
    "source",
)
SDP_REVIEWED_SELECTIONS_PATH = "reproducibility/reviewed_semantic_selections.csv"

_ARTIFACT_FIELDS = ("path", "role", "media_type", "sha256", "size_bytes")
_DECLARATION_FIELDS = ("path", "role", "media_type")
_MEDIA_TYPE_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!#$&^_.+-"
)

# The accepted writer set has one owner (``provenance.py``); re-typing the
# pair of literals here is how the same ruling got applied twice out of
# three times on the R side. ``tests/test_provenance.py`` fails if any
# manifest validator re-types a writer literal.
_MANIFEST_WRITER = "write_sdp_reproducibility_manifest"


class ReproducibilityManifestError(ValueError):
    """Raised for every reproducibility-manifest contract violation."""


def _root(path: Union[str, Path]) -> Path:
    """Mirror ``.ms_sdp_reproducibility_root``."""
    if (
        path is None
        or isinstance(path, (list, tuple))
        or not str(path)
        or not Path(str(path)).is_dir()
    ):
        raise ReproducibilityManifestError(
            "path must name one existing Salmon Data Package directory."
        )
    if Path(os.path.expanduser(str(path))).is_symlink():
        raise ReproducibilityManifestError(
            "path must not be a symlink; refusing an unsafe SDP root."
        )
    return Path(os.path.realpath(str(path)))


def _is_safe_path(path: object) -> bool:
    """Mirror ``.ms_sdp_reproducibility_safe_path``."""
    if path is None or isinstance(path, (list, tuple)) or not str(path):
        return False
    text = str(path)
    if "\\" in text or text.startswith("/"):
        return False
    parts = text.split("/")
    return (
        len(parts) >= 2
        and parts[0] == "reproducibility"
        and not any(part in ("", ".", "..") for part in parts)
        and text != SDP_REPRODUCIBILITY_PATH
    )


def _expected_role(path: str) -> Optional[str]:
    """Mirror ``.ms_sdp_reproducibility_expected_role``."""
    if path == SDP_REVIEWED_SELECTIONS_PATH:
        return "reviewed_semantic_selections"
    for role in ("workflow", "provenance", "source"):
        if path.startswith(f"reproducibility/{role}/"):
            return role
    return None


def _assert_not_symlinked(root: Path, path: Path) -> None:
    """Mirror ``.ms_sdp_reproducibility_assert_not_symlinked``.

    Walks every package-relative ancestor of ``path``, so a symlinked
    ``reproducibility/workflow`` is refused as loudly as a symlinked script.
    """
    relative = str(path)[len(str(root)) + 1 :]
    parts = relative.split("/")
    links = []
    for index in range(len(parts)):
        candidate = root.joinpath(*parts[: index + 1])
        if candidate.is_symlink():
            links.append(str(candidate))
    if links:
        raise ReproducibilityManifestError(
            "Reproducibility artifacts cannot be reached through a symlink: "
            + ", ".join(links)
            + "."
        )


def _resolve(root: Path, relative: object) -> Path:
    """Mirror ``.ms_sdp_reproducibility_resolve``."""
    if not _is_safe_path(relative):
        raise ReproducibilityManifestError(
            f"Reproducibility path {relative!r} is not a safe package-relative path."
        )
    candidate = root / str(relative)
    if not candidate.exists() or candidate.is_dir():
        raise ReproducibilityManifestError(
            f"Reproducibility artifact {candidate} is missing or is not a "
            "regular file."
        )
    _assert_not_symlinked(root, candidate)
    resolved = Path(os.path.realpath(str(candidate)))
    if root not in resolved.parents:
        raise ReproducibilityManifestError(
            f"Reproducibility artifact {candidate} resolves outside the SDP "
            "and is unsafe."
        )
    return resolved


def _normalize_declarations(root: Path, artifacts: object) -> List[Dict[str, str]]:
    """Mirror ``.ms_sdp_reproducibility_normalize_declarations``."""
    if isinstance(artifacts, Mapping):
        artifacts = pd.DataFrame(artifacts)
    if not isinstance(artifacts, pd.DataFrame) or len(artifacts) == 0:
        raise ReproducibilityManifestError(
            "artifacts must be a non-empty data frame of explicit declarations."
        )
    if list(artifacts.columns) != list(_DECLARATION_FIELDS):
        raise ReproducibilityManifestError(
            "artifacts must have exactly the columns "
            + ", ".join(_DECLARATION_FIELDS)
            + " in that order."
        )

    declarations: List[Dict[str, str]] = []
    for position in range(len(artifacts)):
        entry: Dict[str, str] = {}
        for field in _DECLARATION_FIELDS:
            value = artifacts[field].iloc[position]
            text = "" if value is None or value is pd.NA else str(value)
            if isinstance(value, float) and value != value:
                text = ""
            if not text.strip():
                raise ReproducibilityManifestError(
                    f"Reproducibility declaration {field} must be non-empty."
                )
            entry[field] = text.strip()
        declarations.append(entry)

    paths = [entry["path"] for entry in declarations]
    if len(set(paths)) != len(paths):
        raise ReproducibilityManifestError(
            "Reproducibility declarations contain duplicate path values."
        )
    for entry in declarations:
        if entry["role"] not in SDP_REPRODUCIBILITY_ROLES:
            raise ReproducibilityManifestError(
                "Reproducibility role must be one of "
                + ", ".join(SDP_REPRODUCIBILITY_ROLES)
                + "."
            )
        if not _is_media_type(entry["media_type"]):
            raise ReproducibilityManifestError(
                "Every reproducibility media_type must be a valid media type."
            )
    for entry in declarations:
        if _expected_role(entry["path"]) != entry["role"]:
            raise ReproducibilityManifestError(
                "Each reproducibility role must match its canonical path location."
            )
    for entry in declarations:
        entry["resolved_path"] = str(_resolve(root, entry["path"]))
    return sorted(declarations, key=lambda entry: entry["path"])


def _is_media_type(value: str) -> bool:
    """``^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$`` without a regex engine."""
    parts = value.split("/")
    if len(parts) != 2:
        return False
    return all(
        part and all(character in _MEDIA_TYPE_CHARS for character in part)
        for part in parts
    )


def _file_entry(declaration: Mapping[str, str]) -> Dict[str, object]:
    """Mirror ``.ms_sdp_reproducibility_file_entry``."""
    data = Path(declaration["resolved_path"]).read_bytes()
    return {
        "path": declaration["path"],
        "role": declaration["role"],
        "media_type": declaration["media_type"],
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
    }


def _package_version() -> str:
    try:
        from importlib.metadata import version

        return version("metasalmonpy")
    except Exception:
        try:
            from . import __version__

            return str(__version__)
        except Exception:
            return "development"


def _manifest_bytes(entries: Sequence[Mapping[str, object]]) -> bytes:
    """Mirror ``.ms_sdp_reproducibility_manifest_bytes``."""
    manifest = {
        "profile": SDP_REPRODUCIBILITY_PROFILE,
        "artifacts": [dict(entry) for entry in entries],
        "provenance": {
            "generated_by": "metasalmonpy.write_sdp_reproducibility_manifest",
            "metasalmonpy_version": _package_version(),
        },
    }
    return (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _read_manifest_text(path: Path) -> str:
    """Mirror ``.ms_sdp_reproducibility_read_bytes``."""
    if not path.exists() or path.is_dir():
        raise ReproducibilityManifestError(
            f"Missing reproducibility manifest at {path}."
        )
    if path.is_symlink():
        raise ReproducibilityManifestError(
            "Refusing to read a reproducibility-manifest symlink."
        )
    data = path.read_bytes()
    if not data or data[-1:] != b"\n" or b"\r" in data:
        raise ReproducibilityManifestError(
            "Reproducibility manifest must use UTF-8, LF endings, and a final newline."
        )
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ReproducibilityManifestError(
            "Reproducibility manifest must contain valid UTF-8."
        ) from None


def _all_files(root: Path) -> List[str]:
    """Mirror ``.ms_sdp_reproducibility_all_files``: the real tree, sorted C."""
    directory = root / "reproducibility"
    if not directory.is_dir() or directory.is_symlink():
        raise ReproducibilityManifestError(
            "The SDP must contain a real reproducibility directory."
        )
    links: List[str] = []
    files: List[str] = []
    for parent, directory_names, file_names in os.walk(directory):
        for name in list(directory_names) + list(file_names):
            candidate = Path(parent) / name
            if candidate.is_symlink():
                links.append(str(candidate))
            elif candidate.is_file():
                files.append(str(candidate.relative_to(root)).replace(os.sep, "/"))
    if links:
        raise ReproducibilityManifestError(
            "Reproducibility trees cannot contain symlinks: " + ", ".join(links) + "."
        )
    return sorted(name for name in files if name != SDP_REPRODUCIBILITY_PATH)


def _validate_manifest(root: Path, manifest: object) -> None:
    """Mirror ``.ms_sdp_reproducibility_validate_manifest``."""
    if (
        not isinstance(manifest, dict)
        or list(manifest.keys()) != ["profile", "artifacts", "provenance"]
        or manifest["profile"] != SDP_REPRODUCIBILITY_PROFILE
        or not isinstance(manifest["artifacts"], list)
        or not manifest["artifacts"]
    ):
        raise ReproducibilityManifestError(
            "Reproducibility manifest has an unsupported profile or incomplete fields."
        )
    provenance = manifest["provenance"]
    version_key = _provenance.version_field(provenance, _MANIFEST_WRITER)
    if version_key is None or not _provenance.version_ok(
        provenance.get(version_key)
    ):
        # A version that is whitespace-only, or not a string at all, is
        # rejected -- which is what the decomposition validator and both R
        # readers already did. Before this the value was coerced with
        # ``str()``, so a JSON number, boolean or array passed.
        raise ReproducibilityManifestError(
            "Reproducibility manifest writer provenance is incomplete."
        )

    paths: List[str] = []
    for index, entry in enumerate(manifest["artifacts"], start=1):
        if not isinstance(entry, dict) or list(entry.keys()) != list(_ARTIFACT_FIELDS):
            raise ReproducibilityManifestError(
                f"Reproducibility manifest artifact entry {index} is incomplete."
            )
        declaration = pd.DataFrame(
            {
                "path": [str(entry["path"])],
                "role": [str(entry["role"])],
                "media_type": [str(entry["media_type"])],
            }
        )
        normalized = _normalize_declarations(root, declaration)
        data = Path(normalized[0]["resolved_path"]).read_bytes()
        actual_hash = hashlib.sha256(data).hexdigest()
        checksum = entry["sha256"]
        if (
            not isinstance(checksum, str)
            or len(checksum) != 64
            or any(character not in "0123456789abcdef" for character in checksum)
            or checksum != actual_hash
        ):
            raise ReproducibilityManifestError(
                f"Reproducibility artifact {entry['path']} does not match its "
                "manifest SHA-256."
            )
        size = entry["size_bytes"]
        if (
            isinstance(size, bool)
            or not isinstance(size, (int, float))
            or size != size
            or size < 0
            or float(size) != float(len(data))
        ):
            raise ReproducibilityManifestError(
                f"Reproducibility artifact {entry['path']} does not match its "
                "manifest size."
            )
        paths.append(str(entry["path"]))

    if len(set(paths)) != len(paths) or paths != sorted(paths):
        raise ReproducibilityManifestError(
            "Reproducibility manifest paths must be unique and sorted."
        )
    discovered = _all_files(root)
    if paths != discovered:
        raise ReproducibilityManifestError(
            "Reproducibility manifest must be closed over the exact directory "
            "contents. No undeclared or missing reproducibility artifacts are "
            "allowed."
        )


# --- public API ----------------------------------------------------------------------


def write_sdp_reproducibility_manifest(
    path: Union[str, Path], artifacts: object, overwrite: bool = False
) -> str:
    """Write a closed reproducibility manifest into a Salmon Data Package.

    Binds an explicit inventory of reviewed semantic selections, workflow
    records, provenance, and source records to exact paths, media types, byte
    sizes, and SHA-256 hashes in ``reproducibility/manifest.json``. The writer
    does not discover files. Validation requires the declarations to be closed
    over the actual reproducibility tree, which prevents accidental
    publication of local notes or editor backups.

    Parameters
    ----------
    path:
        Existing Salmon Data Package directory.
    artifacts:
        Non-empty DataFrame (or dict of columns) with exactly ``path``,
        ``role``, and ``media_type``, in that order. Paths are
        package-relative. Roles are ``reviewed_semantic_selections``,
        ``workflow``, ``provenance``, or ``source`` and must agree with the
        canonical directory layout.
    overwrite:
        Replace an existing managed manifest when ``True``.

    Returns
    -------
    str
        The manifest path.
    """
    root = _root(path)
    if not isinstance(overwrite, bool):
        raise ReproducibilityManifestError("overwrite must be True or False.")
    declarations = _normalize_declarations(root, artifacts)
    entries = [_file_entry(declaration) for declaration in declarations]
    manifest_path = root / SDP_REPRODUCIBILITY_PATH
    if manifest_path.exists() and not overwrite:
        raise FileExistsError(
            "Reproducibility manifest already exists and overwrite is False. "
            f"Existing: {manifest_path}."
        )
    if manifest_path.is_symlink():
        raise ReproducibilityManifestError(
            "Refusing to overwrite a reproducibility-manifest symlink."
        )
    directory = manifest_path.parent
    directory.mkdir(parents=True, exist_ok=True)
    _assert_not_symlinked(root, directory)

    manifest_bytes = _manifest_bytes(entries)
    candidate = json.loads(manifest_bytes.decode("utf-8"))
    # Validate the complete candidate against the current tree before replacing
    # a valid recovery record. In particular, an incomplete overwrite must not
    # leave the package with a newly invalid manifest.
    _validate_manifest(root, candidate)
    atomic_write(manifest_bytes, manifest_path)
    validate_sdp_reproducibility_manifest(root)
    return str(manifest_path)


def read_sdp_reproducibility_manifest(
    path: Union[str, Path], validate: bool = True
) -> Dict[str, object]:
    """Read an SDP reproducibility manifest.

    Parameters
    ----------
    path:
        Existing Salmon Data Package directory.
    validate:
        Validate paths, roles, checksums, sizes, symlinks, provenance,
        deterministic ordering, and exact directory closure.

    Returns
    -------
    dict
        The parsed manifest.
    """
    root = _root(path)
    if not isinstance(validate, bool):
        raise ReproducibilityManifestError("validate must be True or False.")
    text = _read_manifest_text(root / SDP_REPRODUCIBILITY_PATH)
    try:
        manifest = json.loads(text)
    except ValueError as error:
        raise ReproducibilityManifestError(
            f"Reproducibility manifest is not valid JSON: {error}"
        ) from None
    if validate:
        _validate_manifest(root, manifest)
    return manifest


def validate_sdp_reproducibility_manifest(path: Union[str, Path]) -> bool:
    """Validate an SDP reproducibility manifest.

    Returns
    -------
    bool
        ``True`` when validation succeeds; otherwise an exception is raised.
    """
    read_sdp_reproducibility_manifest(path, validate=True)
    return True


__all__ = [
    "ReproducibilityManifestError",
    "SDP_REPRODUCIBILITY_PATH",
    "SDP_REPRODUCIBILITY_PROFILE",
    "SDP_REPRODUCIBILITY_ROLES",
    "SDP_REVIEWED_SELECTIONS_PATH",
    "read_sdp_reproducibility_manifest",
    "validate_sdp_reproducibility_manifest",
    "write_sdp_reproducibility_manifest",
]
