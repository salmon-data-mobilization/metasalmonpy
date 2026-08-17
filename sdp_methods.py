"""SDP SOSA procedure registry, plus the shared SDP metadata-extension helpers.

Mirrors metasalmon's ``R/sdp-methods.R`` at the **v0.1.8** tag.

I-ADOPT describes variable meaning; it does not define a Method component.
This module implements SDP's separate registry of resources interpreted as
SOSA Procedures. A measurement can refer to one fixed procedure through the
compatibility ``column_dictionary.method_iri`` field. Row-varying procedures
are validated with observation structures in ``observation_structures.py``.

Like R's ``sdp-methods.R``, this file also owns the helpers that every SDP
metadata extension shares — safe path resolution, symlink refusal, the
all-or-nothing multi-file writer, and canonical CSV/JSON byte emission.
``observation_structures.py`` imports them from here rather than duplicating
them, exactly as ``observation-structures.R`` calls ``.ms_sdp_extension_*``.

**Why this module touches ``datapackage.json`` when ``sssom.py`` and
``measurement_decompositions.py`` do not:** those two sidecars are bound to the
package by their fixed path plus a checksum manifest, and R does not register
them as Frictionless resources. The methods and observation-structure
resources *are* declared in the SDP profile, so R's readers validate the
descriptor inventory and R's writers keep it in step. Skipping that here would
make a Python-written package fail R's validator.

Byte-parity contract: ``metadata/methods.csv`` written by either
implementation is byte-identical — canonical row order is
``(dataset_id, method_iri)`` under codepoint ordering, which is R's default
``dplyr::arrange()`` on ASCII IRIs. This module only reads and validates that
file (see ``write_sdp_methods`` below for why), so the contract is exercised
against R-generated fixtures in ``tests/data/sdp-extensions/``.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import tempfile
import warnings
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Union

import pandas as pd

from .atomic_io import apply_default_file_mode
from .metadata import read_sdp_csv
from .sdp_schema import sdp_schema_url

SDP_METHODS_PATH = "metadata/methods.csv"
SDP_METHODS_COLUMNS = (
    "dataset_id",
    "method_iri",
    "method_label",
    "method_description",
    "method_version",
    "protocol_iri",
    "citation",
)

# R's ``trimws()`` default character class, shared with ``metadata.py``.
_TRIM_CHARS = " \t\r\n"

# ``.ms_sdp_extension_is_absolute_iri``: a scheme, a colon, no whitespace, and
# never a REVIEW: placeholder. HTTP(S) IRIs additionally need an authority.
_ABSOLUTE_IRI_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:[^\s]+$")
_HTTP_SCHEME_RE = re.compile(r"^https?:", re.IGNORECASE)
_HTTP_AUTHORITY_RE = re.compile(r"^https?://[^/\s]+", re.IGNORECASE)
_REVIEW_RE = re.compile(r"^REVIEW:", re.IGNORECASE)


class SdpExtensionError(ValueError):
    """Raised for every SDP metadata-extension contract violation.

    R signals all of these through ``.ms_sdp_extension_abort()``. A single
    exception type keeps ``except`` clauses in caller code as selective as
    R's ``tryCatch`` on that one condition class, while remaining a
    ``ValueError`` so existing handlers still catch it.
    """


# --- shared extension helpers -------------------------------------------------------


def _is_blank(value: object) -> bool:
    """Mirror ``.ms_sdp_extension_is_blank`` for one cell."""
    if value is None or value is pd.NA:
        return True
    if isinstance(value, float) and value != value:
        return True
    return not str(value).strip(_TRIM_CHARS)


def _is_absolute_iri(value: object) -> bool:
    """Mirror ``.ms_sdp_extension_is_absolute_iri`` for one cell."""
    if _is_blank(value):
        return False
    text = str(value)
    if not _ABSOLUTE_IRI_RE.match(text) or _REVIEW_RE.match(text):
        return False
    if _HTTP_SCHEME_RE.match(text):
        return _HTTP_AUTHORITY_RE.match(text) is not None
    return True


def _is_symlink(path: Union[str, Path]) -> bool:
    """Mirror ``.ms_sdp_extension_is_symlink``.

    R strips trailing separators before ``Sys.readlink`` because
    ``readlink("link/")`` reports nothing on macOS even when ``link`` is a
    symlink. ``Path`` already discards trailing separators, so the lexical
    entry the caller named is what gets inspected — deliberately *not* its
    ancestors, so a package under macOS's ``/var -> /private/var`` alias is
    still usable.
    """
    return Path(os.path.expanduser(str(path))).is_symlink()


def _extension_root(path: Union[str, Path]) -> Path:
    """Mirror ``.ms_sdp_extension_root``: one real, non-symlinked SDP root."""
    if (
        path is None
        or isinstance(path, (list, tuple))
        or not str(path)
        or not Path(str(path)).is_dir()
    ):
        raise SdpExtensionError(
            "path must name one existing Salmon Data Package directory."
        )
    if _is_symlink(path):
        raise SdpExtensionError(
            "path must not be a symlink; refusing an unsafe SDP root."
        )
    return Path(os.path.realpath(str(path)))


def _assert_safe_directory(
    root: Path, relative_directory: str, create: bool = False
) -> Path:
    """Mirror ``.ms_sdp_extension_assert_safe_directory``."""
    current = root
    for part in relative_directory.split("/"):
        current = current / part
        if _is_symlink(current):
            raise SdpExtensionError(
                f"Refusing an SDP metadata path that traverses symlink {current}."
            )
        if current.exists() and not current.is_dir():
            raise SdpExtensionError(
                f"Expected SDP metadata directory but found a file at {current}."
            )
        if not current.is_dir() and create:
            try:
                current.mkdir()
            except OSError:
                raise SdpExtensionError(
                    f"Could not create SDP metadata directory {current}."
                ) from None
        if current.is_dir():
            resolved = Path(os.path.realpath(str(current)))
            if resolved != root and root not in resolved.parents:
                raise SdpExtensionError(
                    "SDP metadata directory resolves outside the package root "
                    "and is unsafe."
                )
    return current


def _assert_safe_file(root: Path, relative_path: str, must_exist: bool = True) -> Path:
    """Mirror ``.ms_sdp_extension_assert_safe_file``."""
    directory = os.path.dirname(relative_path)
    if directory:
        _assert_safe_directory(root, directory, create=False)
    path = root / relative_path
    if _is_symlink(path):
        raise SdpExtensionError(f"Refusing SDP metadata symlink {path}.")
    if must_exist and (not path.exists() or path.is_dir()):
        raise SdpExtensionError(f"Missing SDP metadata file {relative_path}.")
    if path.exists():
        resolved = Path(os.path.realpath(str(path)))
        if root not in resolved.parents:
            raise SdpExtensionError(
                "SDP metadata file resolves outside the package root and is unsafe."
            )
    return path


def _atomic_write_set(
    writes: "Mapping[Union[str, Path], bytes]", validate=None
) -> List[str]:
    """Mirror ``.ms_sdp_extension_atomic_write_set``: one rollback-capable write.

    Every replacement is staged before any current file moves out of the way,
    so a malformed descriptor, an unwritable directory, or a serialization
    error cannot leave a half-applied extension behind. ``validate`` runs
    *after* installation and against the bytes on disk; if it raises, every
    installed file is removed and every backup restored before the error
    propagates.
    """
    paths = [str(path) for path in writes]
    if not paths or any(not path for path in paths) or len(set(paths)) != len(paths):
        raise SdpExtensionError(
            "Atomic SDP metadata writes require a named, non-empty set of files."
        )
    if validate is not None and not callable(validate):
        raise SdpExtensionError("validate must be a function or None.")

    payloads = [writes[key] for key in writes]
    stages: List[Optional[str]] = [None] * len(paths)
    backups: List[Optional[str]] = [None] * len(paths)
    installed = [False] * len(paths)
    original_exists = [os.path.exists(path) for path in paths]

    def cleanup() -> None:
        for candidate in list(stages) + list(backups):
            if candidate and os.path.exists(candidate):
                try:
                    os.unlink(candidate)
                except OSError:  # pragma: no cover - already gone
                    pass

    def rollback() -> None:
        for index in reversed(range(len(paths))):
            path = paths[index]
            if installed[index] and os.path.exists(path):
                os.unlink(path)
            backup = backups[index]
            if backup and os.path.exists(backup):
                if os.path.exists(path):
                    os.unlink(path)
                try:
                    os.replace(backup, path)
                except OSError:  # pragma: no cover - unwritable directory
                    warnings.warn(
                        f"Could not restore SDP metadata backup for '{path}'.",
                        stacklevel=2,
                    )

    try:
        for index, path in enumerate(paths):
            payload = payloads[index]
            if not isinstance(payload, (bytes, bytearray)):
                raise SdpExtensionError(
                    f"Atomic SDP metadata content for {path} must be raw bytes."
                )
            directory = os.path.dirname(path)
            if not os.path.isdir(directory):
                raise SdpExtensionError(
                    f"Atomic SDP metadata directory {directory} does not exist."
                )
            if _is_symlink(path):
                raise SdpExtensionError(
                    f"Refusing to atomically replace SDP metadata symlink {path}."
                )
            if os.path.isdir(path):
                raise SdpExtensionError(
                    f"Expected SDP metadata file but found a directory at {path}."
                )
            handle, stage = tempfile.mkstemp(
                prefix=f".{os.path.basename(path)}-stage-", dir=directory
            )
            stages[index] = stage
            try:
                with os.fdopen(handle, "wb") as stream:
                    stream.write(bytes(payload))
            except OSError as error:
                raise SdpExtensionError(
                    f"Could not stage SDP metadata file {path}: {error}"
                ) from None
            # ``mkstemp`` hard-codes 0600; restore the umask default R's
            # ``writeBin`` would have produced (PARITY.md row 24).
            apply_default_file_mode(stage)

        try:
            for index, path in enumerate(paths):
                if original_exists[index]:
                    handle, backup = tempfile.mkstemp(
                        prefix=f".{os.path.basename(path)}-backup-",
                        dir=os.path.dirname(path),
                    )
                    os.close(handle)
                    os.unlink(backup)
                    try:
                        os.replace(path, backup)
                    except OSError:
                        raise SdpExtensionError(
                            f"Could not preserve existing SDP metadata file {path}."
                        ) from None
                    backups[index] = backup
                stage = stages[index]
                try:
                    os.replace(stage, path)
                except OSError:
                    raise SdpExtensionError(
                        f"Could not atomically install SDP metadata file {path}."
                    ) from None
                installed[index] = True
                stages[index] = None
            if validate is not None:
                validate()
        except BaseException:
            rollback()
            raise

        for backup in backups:
            if backup and os.path.exists(backup):
                os.unlink(backup)
        backups = [None] * len(paths)
        return paths
    finally:
        cleanup()


def _csv_field(value: str) -> str:
    """One CSV field, quoted exactly when ``readr::write_csv`` would quote."""
    if any(character in value for character in ',"\n\r'):
        return '"' + value.replace('"', '""') + '"'
    return value


def _csv_bytes(columns: Sequence[str], rows: pd.DataFrame) -> bytes:
    """Mirror ``.ms_sdp_extension_csv_bytes`` (``readr::write_csv(na = "")``)."""
    lines = [",".join(columns)]
    cells = {name: rows[name].tolist() for name in columns}
    for index in range(len(rows)):
        lines.append(
            ",".join(_csv_field(_csv_cell(cells[name][index])) for name in columns)
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _is_na(value: object) -> bool:
    """True only for R's ``NA`` analogues -- never for a whitespace string."""
    if value is None or value is pd.NA:
        return True
    return isinstance(value, float) and value != value


def _csv_cell(value: object) -> str:
    """Render one cell the way ``readr::write_csv(na = "")`` does.

    Only a true ``NA`` becomes the empty field. A whitespace-only *string* is
    data and survives the round trip, exactly as it does through R's
    ``as.character()`` + ``na = ""`` pair — the blank test in
    ``_is_blank`` is a *validation* predicate, not a serialization one.
    """
    if _is_na(value):
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _json_bytes(value: object) -> bytes:
    """Mirror ``.ms_sdp_extension_json_bytes`` (2-space pretty, final LF)."""
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _read_extension_csv(
    root: Path, relative_path: str, columns: Sequence[str]
) -> pd.DataFrame:
    """Mirror ``.ms_sdp_extension_read_csv``: exact schema, no type coercion.

    R reads these with ``trim_ws = FALSE`` (unlike every other SDP CSV), so
    leading and trailing whitespace inside a field is data. ``read_sdp_csv``
    trims, so the raw ``csv`` module is used here instead.
    """
    path = _assert_safe_file(root, relative_path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise SdpExtensionError(
            f"Could not parse {relative_path}: {error}"
        ) from None
    try:
        records = [row for row in csv.reader(io.StringIO(text)) if row]
    except csv.Error as error:
        raise SdpExtensionError(f"Could not parse {relative_path}: {error}") from None
    header = records[0] if records else []
    if list(header) != list(columns):
        raise SdpExtensionError(
            f"{relative_path} does not have the exact SDP schema. "
            f"Expected columns, in order: {', '.join(columns)}. "
            f"Found columns: {', '.join(header)}."
        )
    width = len(header)
    body = []
    for row in records[1:]:
        if len(row) < width:
            row = row + [""] * (width - len(row))
        elif len(row) > width:
            row = row[: width - 1] + [",".join(row[width - 1 :])]
        body.append(row)
    return pd.DataFrame(body, columns=list(columns), dtype=object)


def _validate_closed_rows(
    rows: object, columns: Sequence[str], label: str
) -> pd.DataFrame:
    """Mirror ``.ms_sdp_extension_validate_closed_rows``."""
    if isinstance(rows, Mapping):
        rows = pd.DataFrame(rows)
    if not isinstance(rows, pd.DataFrame):
        raise SdpExtensionError(f"{label} must be a data frame.")
    missing = [name for name in columns if name not in rows.columns]
    extra = [name for name in rows.columns if name not in columns]
    if missing or extra:
        details = []
        if missing:
            details.append("Missing: " + ", ".join(missing) + ".")
        if extra:
            details.append("Unexpected: " + ", ".join(extra) + ".")
        raise SdpExtensionError(
            f"{label} must match the exact SDP schema. " + " ".join(details)
        )
    return rows.loc[:, list(columns)].reset_index(drop=True)


def _extension_dataset_id(root: Path) -> str:
    """Mirror ``.ms_sdp_extension_dataset_id``."""
    dataset_path = _assert_safe_file(root, "metadata/dataset.csv")
    dataset = read_sdp_csv(dataset_path)
    if (
        "dataset_id" not in dataset.columns
        or len(dataset) != 1
        or _is_blank(dataset["dataset_id"].iloc[0])
    ):
        raise SdpExtensionError(
            "metadata/dataset.csv must contain one non-empty dataset_id."
        )
    return str(dataset["dataset_id"].iloc[0])


def _extension_resource(
    name: str, path: str, title: str, description: str, schema_file: str
) -> Dict[str, str]:
    """Mirror ``.ms_sdp_extension_resource``."""
    return {
        "profile": "tabular-data-resource",
        "name": name,
        "path": path,
        "title": title,
        "description": description,
        "schema": sdp_schema_url(schema_file),
    }


def _read_descriptor(root: Path) -> Optional[dict]:
    """Parse ``datapackage.json``, refusing a symlink, returning None if absent."""
    descriptor_path = root / "datapackage.json"
    if _is_symlink(descriptor_path):
        raise SdpExtensionError("Refusing symlinked datapackage.json.")
    if not descriptor_path.exists():
        return None
    try:
        with descriptor_path.open("r", encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError, UnicodeDecodeError) as error:
        raise SdpExtensionError(
            f"Could not parse datapackage.json: {error}"
        ) from None


def _descriptor_bytes(
    root: Path, resources: Sequence[dict], metadata: Mapping[str, str]
) -> Optional[bytes]:
    """Mirror ``.ms_sdp_extension_descriptor_bytes``.

    Replaces only the resources this writer manages (matched by name or path),
    appends them in declared order, and merges the ``sdp.metadata`` inventory.
    Returns ``None`` when the package has no descriptor, which is R's explicit
    "nothing to update" signal.
    """
    if _is_symlink(root / "datapackage.json"):
        raise SdpExtensionError("Refusing to replace symlinked datapackage.json.")
    descriptor = _read_descriptor(root)
    if descriptor is None:
        return None
    existing = descriptor.get("resources") or []
    managed_names = {resource["name"] for resource in resources}
    managed_paths = {resource["path"] for resource in resources}
    kept = [
        resource
        for resource in existing
        if resource.get("name", "") not in managed_names
        and resource.get("path", "") not in managed_paths
    ]
    descriptor["resources"] = kept + list(resources)
    sdp_block = descriptor.get("sdp") or {}
    metadata_block = sdp_block.get("metadata") or {}
    for field, value in metadata.items():
        metadata_block[field] = value
    sdp_block["metadata"] = metadata_block
    descriptor["sdp"] = sdp_block
    return _json_bytes(descriptor)


def _validate_descriptor_resource(descriptor: dict, expected: Mapping[str, str]) -> None:
    """Mirror ``.ms_sdp_extension_validate_descriptor_resource``."""
    resources = descriptor.get("resources") or []
    matches = [
        resource
        for resource in resources
        if resource.get("path") == expected["path"]
    ]
    if len(matches) != 1:
        raise SdpExtensionError(
            "datapackage.json must declare exactly one resource for "
            f"{expected['path']}."
        )
    actual = matches[0]
    for field in ("name", "path", "profile", "schema"):
        if actual.get(field) != expected[field]:
            raise SdpExtensionError(
                f"datapackage.json resource {expected['path']} field {field} "
                f"must be {expected[field]}."
            )


def _methods_resource() -> Dict[str, str]:
    return _extension_resource(
        "sdp_methods",
        SDP_METHODS_PATH,
        "SDP methods metadata",
        "Optional registry of procedures associated with measurements in the package.",
        "methods.schema.json",
    )


# --- registry normalization and validation ------------------------------------------


def _normalize_methods(methods: object) -> pd.DataFrame:
    """Mirror ``.ms_sdp_methods_normalize``: character columns, canonical order."""
    rows = _validate_closed_rows(methods, SDP_METHODS_COLUMNS, "methods")
    frame = pd.DataFrame(index=range(len(rows)))
    for column in SDP_METHODS_COLUMNS:
        values = []
        for value in rows[column].tolist():
            if isinstance(value, (list, tuple, dict, set)):
                raise SdpExtensionError(
                    f"SDP method column {column} must be an atomic vector."
                )
            values.append(_csv_cell(value))
        frame[column] = values
    # ``dplyr::arrange(dataset_id, method_iri)``; codepoint order is R's radix
    # order and, for these ASCII IRIs, its locale order too.
    order = sorted(
        range(len(frame)),
        key=lambda index: (
            frame["dataset_id"].iloc[index],
            frame["method_iri"].iloc[index],
        ),
    )
    return frame.iloc[order].reset_index(drop=True)


def _validate_method_rows(
    root: Path, methods: pd.DataFrame, check_descriptor: bool = True
) -> None:
    """Mirror ``.ms_sdp_methods_validate_rows``."""
    for column in (
        "dataset_id",
        "method_iri",
        "method_label",
        "method_description",
    ):
        if any(_is_blank(value) for value in methods[column]):
            raise SdpExtensionError(
                f"Every SDP method row requires non-empty {column}."
            )
    if any(not _is_absolute_iri(value) for value in methods["method_iri"]):
        raise SdpExtensionError("Every method_iri must be an absolute IRI.")
    for value in methods["protocol_iri"]:
        if not _is_blank(value) and not _is_absolute_iri(value):
            raise SdpExtensionError(
                "Every non-empty protocol_iri must be an absolute IRI."
            )
    keys = list(zip(methods["dataset_id"], methods["method_iri"]))
    if len(set(keys)) != len(keys):
        raise SdpExtensionError("method_iri must be unique within each dataset.")

    dataset_id = _extension_dataset_id(root)
    if any(str(value) != dataset_id for value in methods["dataset_id"]):
        raise SdpExtensionError(
            "Every SDP method dataset_id must match metadata/dataset.csv."
        )

    dictionary_path = _assert_safe_file(root, "metadata/column_dictionary.csv")
    dictionary = read_sdp_csv(dictionary_path)
    if "method_iri" in dictionary.columns:
        registered = set(methods["method_iri"])
        fixed = []
        for value in dictionary["method_iri"]:
            if not _is_blank(value) and str(value) not in fixed:
                fixed.append(str(value))
        missing = [value for value in fixed if value not in registered]
        if missing:
            raise SdpExtensionError(
                "Static procedure references are missing from "
                "metadata/methods.csv. Unregistered method_iri: "
                + ", ".join(missing)
                + "."
            )

    if check_descriptor:
        _validate_methods_descriptor(root)


def _validate_methods_descriptor(root: Path) -> None:
    """Mirror ``.ms_sdp_methods_validate_descriptor``."""
    descriptor = _read_descriptor(root)
    if descriptor is None:
        return
    _validate_descriptor_resource(descriptor, _methods_resource())
    declared = (descriptor.get("sdp") or {}).get("metadata") or {}
    if declared.get("methods") != SDP_METHODS_PATH:
        raise SdpExtensionError(
            "datapackage.json must declare metadata/methods.csv as an SDP "
            "metadata resource."
        )


# --- public API ----------------------------------------------------------------------


def read_sdp_methods(
    path: Union[str, Path], validate: bool = True
) -> pd.DataFrame:
    """Read an SDP SOSA procedure registry.

    Parameters
    ----------
    path:
        Existing Salmon Data Package directory.
    validate:
        When ``True``, validate package bindings (dataset identity, unique
        IRIs, static ``column_dictionary.method_iri`` coverage) and the
        ``datapackage.json`` resource inventory. A ``False`` read still
        enforces the exact closed column schema and canonical ordering.

    Returns
    -------
    pandas.DataFrame
        The registry with the exact SDP methods schema, in canonical
        ``(dataset_id, method_iri)`` order. Every column is text; an empty
        field reads back as ``""``, matching R's ``na = ""``.
    """
    root = _extension_root(path)
    if not isinstance(validate, bool):
        raise SdpExtensionError("validate must be True or False.")
    methods = _read_extension_csv(root, SDP_METHODS_PATH, SDP_METHODS_COLUMNS)
    methods = _normalize_methods(methods)
    if validate:
        _validate_method_rows(root, methods)
    return methods


def validate_sdp_methods(path: Union[str, Path]) -> bool:
    """Validate an SDP SOSA procedure registry.

    Returns
    -------
    bool
        ``True`` when the registry and its package bindings are valid;
        otherwise an exception is raised.
    """
    read_sdp_methods(path, validate=True)
    return True


def write_sdp_methods(*args, **kwargs):
    """Not implemented here — deliberately, and permanently.

    metasalmon v0.1.8 exports ``write_sdp_methods()``. This package
    implements only the reader and the validator, because Python receives
    R-written packages that carry a registry (and 0.1.8-era EML documents
    quote procedures out of one) but no Python user has ever needed to
    *author* one: the mirror was at 0.1.6 parity for the entire life of this
    surface.

    **The absence is not a gap that will later be filled.** metasalmon 0.3.0
    removes ``metadata/methods.csv`` from the specification altogether and
    replaces it with ``migrate_sdp_methods()``. A writer added here would be
    written only to be deleted in the same catch-up stream, and every package
    it produced would need migrating. The reader and validator survive that
    transition — they are what a migration needs.

    Logged as a decision in metasalmon's S10 execplan (2026-08-15) and as
    row 9 of ``PARITY.md``.

    **Retirement condition for this stub:** it is removed when the replay
    reaches the 0.3.0 milestone and the registry stops existing. If the
    ecosystem ever reverses that decision and keeps per-package registries,
    this stub is the place the writer goes — do not add it elsewhere.

    Raises
    ------
    NotImplementedError
        Always.
    """
    raise NotImplementedError(
        "metasalmonpy does not write metadata/methods.csv. The registry is "
        "read and validated here so R-written packages stay usable, but it is "
        "removed from the specification at SDP 0.3.0 and replaced by a "
        "migration, so a writer would exist only to be deleted. Author the "
        "registry with metasalmon (R) if you need one today, or wait for the "
        "0.3.0 migration path. See PARITY.md row 9."
    )


__all__ = [
    "SDP_METHODS_COLUMNS",
    "SDP_METHODS_PATH",
    "SdpExtensionError",
    "read_sdp_methods",
    "validate_sdp_methods",
    "write_sdp_methods",
]
