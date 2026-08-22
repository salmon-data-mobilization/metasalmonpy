"""SDP methods migration (sdp-0.2.0 -> sdp-0.3.0), the legacy registry reader,
and the shared SDP metadata-extension helpers.

Mirrors metasalmon's ``R/sdp-methods.R`` on the post-0.3.0 tree: sdp-0.3.0
removed both the ``metadata/methods.csv`` registry and the column-dictionary
``method_iri`` field. Method labels and descriptions belong to the shared
vocabulary; a table-constant procedure belongs in ``tables.csv$method_iri``; a
row-varying procedure lives in the data with its codes resolved through
``codes.csv$term_iri``; protocols are cited through the
``protocol_iri``/``protocol_citation`` fields on ``tables.csv`` and
``dataset.csv``. :func:`migrate_sdp_methods` migrates sdp-0.2.0 packages to
that shape.

**One deliberate difference from R** (PARITY.md row 9): metasalmon 0.3.0
removed ``read_sdp_methods()`` and ``validate_sdp_methods()`` outright; here
they survive as legacy *read* support, because this package receives packages
written by metasalmon 0.2.x that still carry a registry. Every current-package
surface treats a lingering ``metadata/methods.csv`` exactly as R does — an
error pointing at the migration.

Like R's ``sdp-methods.R`` before the 0.3.0 split (R moved them to
``sdp-extension-helpers.R``; module granularity is not part of the mirror
contract), this file also owns the helpers that every SDP metadata extension
shares — safe path resolution, symlink refusal, the all-or-nothing multi-file
writer with rollback, and canonical CSV/JSON byte emission.
``observation_structures.py`` imports them from here rather than duplicating
them.
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
from .metadata import R_SPACE_CLASS, csv_na_token, read_sdp_csv
from .sdp_schema import sdp_metadata_resource_schema

SDP_METHODS_PATH = "metadata/methods.csv"

# The sdp-0.2.0 registry schema, kept only to read legacy packages and
# migration input. sdp-0.3.0 removed the registry from the specification, so
# the vendored schema bundle no longer defines a ``methods`` table — this
# tuple is the frozen legacy contract, not a read of the bundle.
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
#
# Whitespace is ``metadata.R_SPACE_CLASS`` — R's TRE-resolved ``[[:space:]]``
# membership — exactly as ``eml.py`` and ``sssom.py`` already build their
# patterns. Python's ``\s`` disagrees with TRE on 8 codepoints (U+001C-001F,
# U+0085, U+00A0, U+2007, U+202F), all of which ``\s`` rejected where R
# accepts, so ``\s`` here made this validator the stricter side and refused
# SDP-extension IRIs metasalmon accepts (hub backlog #86, PARITY.md row 33 —
# discharged by this constant import plus the membership test in
# ``tests/test_sdp_methods.py``).
_ABSOLUTE_IRI_RE = re.compile(
    rf"^[A-Za-z][A-Za-z0-9+.\-]*:[^{R_SPACE_CLASS}]+$"
)
_HTTP_SCHEME_RE = re.compile(r"^https?:", re.IGNORECASE)
_HTTP_AUTHORITY_RE = re.compile(
    rf"^https?://[^/{R_SPACE_CLASS}]+", re.IGNORECASE
)
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
            # ``unlink()`` mirrors R's, whose status is ignored: a path that
            # cannot be removed (for example, something replaced it with a
            # directory) must still fall through to the restore attempt below
            # rather than crash the rollback that protects the backup.
            if installed[index] and os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass
            backup = backups[index]
            if backup and os.path.exists(backup):
                if os.path.exists(path):
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
                try:
                    os.replace(backup, path)
                except OSError:
                    # Detach the backup from the cleanup list and name it.
                    # ``cleanup()`` unlinks every backup it still knows about,
                    # which would destroy the only surviving copy of the
                    # original in exactly the case where the restore already
                    # failed. Mirrors metasalmon 0.3.0's fix to
                    # ``.ms_sdp_extension_atomic_write_set()``.
                    backups[index] = None
                    warnings.warn(
                        f"Could not restore SDP metadata backup for '{path}'; "
                        f"the original bytes are preserved at '{backup}'.",
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

    Only a true ``NA`` becomes the empty field — ``csv_na_token()``, the one
    missing-value authority (metasalmon 0.2.4). A whitespace-only *string* is
    data and survives the round trip, exactly as it does through R's
    ``as.character()`` + ``na = ""`` pair — the blank test in
    ``_is_blank`` is a *validation* predicate, not a serialization one.
    """
    if _is_na(value):
        return csv_na_token()
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
        # Derived from the loaded bundle rather than composed from a constant
        # (metasalmon 0.2.1): every URI in a written descriptor — profile,
        # rules, and per-resource schemas — comes from one validated document.
        # The composition survives as the fallback for a bundle published
        # before this resource existed.
        "schema": sdp_metadata_resource_schema(name, schema_file),
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
    """Read a **legacy** (sdp-0.2.0) SDP SOSA procedure registry.

    sdp-0.3.0 removed ``metadata/methods.csv`` from the specification, and
    metasalmon 0.3.0 removed its ``read_sdp_methods()`` with it. This reader
    survives here — a deliberate, registered difference (PARITY.md row 9) —
    because this package receives packages written by metasalmon 0.2.x that
    still carry a registry, and a migration needs to read what it relocates.
    A *current* package carrying one is an error on every validation and
    publication surface, pointing at :func:`migrate_sdp_methods`.

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
    """Validate a **legacy** (sdp-0.2.0) SDP SOSA procedure registry.

    Survives 0.3.0 for the same legacy-read reason as
    :func:`read_sdp_methods` (PARITY.md row 9). A current package carrying a
    registry fails validation elsewhere with a pointer at
    :func:`migrate_sdp_methods`; this function checks the *registry's own*
    contract, which is what a migration or a legacy consumer needs.

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

    SDP 0.3.0 **removed** ``metadata/methods.csv`` from the specification.
    Method labels and descriptions belong in the shared vocabulary, not in
    per-package registries that restate it; a table-constant procedure lives
    in ``tables.csv$method_iri``, a row-varying one in the data resolved
    through ``codes.csv$term_iri``, and protocols are cited through
    ``protocol_iri``/``protocol_citation``. There is nothing left for a
    registry writer to write, on either side of the mirror — metasalmon
    removed its ``write_sdp_methods()`` at 0.3.0.

    The reader and validator survive here for legacy packages (PARITY.md
    row 9); use :func:`migrate_sdp_methods` to relocate a legacy registry's
    content and remove the file.

    **Retirement condition for this stub:** it is removed at a rung permitted
    to break this package's own callers, once no caller can reasonably still
    look for the 0.1.8-era name. If the ecosystem ever reverses the 0.3.0
    decision and reinstates per-package registries, this stub is the place
    the writer goes — do not add it elsewhere.

    Raises
    ------
    NotImplementedError
        Always.
    """
    raise NotImplementedError(
        "metasalmonpy does not write metadata/methods.csv. SDP 0.3.0 removed "
        "the registry from the specification: method labels and descriptions "
        "belong in the shared vocabulary, a table-constant procedure lives in "
        "tables.csv method_iri, and a row-varying one resolves through "
        "codes.csv term_iri. The registry is still read and validated here so "
        "legacy R-written packages stay usable; run migrate_sdp_methods() to "
        "relocate a legacy registry's content and remove it. See PARITY.md "
        "row 9."
    )


# --- sdp-0.2.0 -> sdp-0.3.0 migration -------------------------------------------------


def _coalesce(*values):
    """R's ``%||%``: the first non-``None`` value (an empty string is a value)."""
    for value in values:
        if value is not None:
            return value
    return None


def _read_legacy_registry(root: Path) -> Optional[pd.DataFrame]:
    """Mirror ``.ms_sdp_methods_read_legacy``.

    Tolerant legacy reader: migration input, not a validation surface. The
    symlink refusals stay (we are about to delete this file), but column
    drift in a hand-edited registry must not block the migration that
    removes it.
    """
    target = root / SDP_METHODS_PATH
    if _is_symlink(target):
        raise SdpExtensionError("Refusing symlinked metadata/methods.csv.")
    if not target.exists() or target.is_dir():
        return None
    try:
        return read_sdp_csv(target)
    except Exception as error:  # noqa: BLE001 - any parse failure is the stop
        raise SdpExtensionError(
            f"Could not parse metadata/methods.csv: {error}"
        ) from None


def _read_migration_metadata_csv(root: Path, relative: str) -> Optional[pd.DataFrame]:
    """One trimmed all-character metadata CSV, or ``None`` when absent."""
    path = root / relative
    if not path.exists() or path.is_dir():
        return None
    return read_sdp_csv(path)


def _measurement_universe(root: Path) -> List[tuple]:
    """Mirror ``.ms_sdp_methods_measurement_universe``.

    The measurement columns of each table, from the same carriers the
    bindings come from. The agreement check needs this universe: a method is
    promoted to the table only when EVERY measurement column carries it, not
    merely every column that happens to have a binding.
    """
    rows: List[tuple] = []

    dictionary = _read_migration_metadata_csv(root, "metadata/column_dictionary.csv")
    if dictionary is not None and all(
        name in dictionary.columns for name in ("table_id", "column_name", "column_role")
    ):
        for position in range(len(dictionary)):
            role = dictionary["column_role"].iloc[position]
            if not _is_blank(role) and str(role).strip(_TRIM_CHARS).lower() == "measurement":
                rows.append(
                    (
                        str(dictionary["table_id"].iloc[position]),
                        str(dictionary["column_name"].iloc[position]),
                    )
                )

    descriptor_path = root / "datapackage.json"
    if descriptor_path.exists() and not _is_symlink(descriptor_path):
        try:
            with descriptor_path.open("r", encoding="utf-8") as stream:
                descriptor = json.load(stream)
        except (OSError, ValueError, UnicodeDecodeError):
            descriptor = None
        for resource in (descriptor or {}).get("resources") or []:
            schema = resource.get("schema")
            fields = schema.get("fields") or [] if isinstance(schema, dict) else []
            for field in fields:
                custom = field.get("custom") if isinstance(field.get("custom"), dict) else {}
                role = _coalesce(custom.get("sdp:columnRole"), field.get("column_role"))
                if role is not None and str(role).strip(_TRIM_CHARS).lower() == "measurement":
                    rows.append(
                        (
                            str(_coalesce(resource.get("name"), "")),
                            str(_coalesce(field.get("name"), "")),
                        )
                    )

    seen = set()
    unique_rows = []
    for row in rows:
        if row not in seen:
            seen.add(row)
            unique_rows.append(row)
    return unique_rows


def _method_column_bindings(root: Path) -> pd.DataFrame:
    """Mirror ``.ms_sdp_methods_column_bindings``.

    One method binding per measurement column, from both sdp-0.2.0 carriers:
    the canonical dictionary CSV and, for descriptor-first packages, the
    per-field ``iAdopt:methodIri`` custom key (or a bare ``method_iri`` field
    property). Identical claims collapse; disagreements stop the migration.
    """
    frames = []

    dictionary = _read_migration_metadata_csv(root, "metadata/column_dictionary.csv")
    if dictionary is not None and all(
        name in dictionary.columns for name in ("table_id", "column_name", "method_iri")
    ):
        frames.append(
            pd.DataFrame(
                {
                    "table_id": [str(value) for value in dictionary["table_id"]],
                    "column_name": [str(value) for value in dictionary["column_name"]],
                    "method_iri": [str(value) for value in dictionary["method_iri"]],
                    "source": "metadata/column_dictionary.csv",
                }
            )
        )

    # A descriptor the migration cannot read or safely rewrite is a stop, not
    # a skip: proceeding would relocate the CSV bindings and delete the
    # registry while the descriptor keeps claiming the old shape.
    descriptor_path = root / "datapackage.json"
    if _is_symlink(descriptor_path):
        raise SdpExtensionError(
            "Refusing symlinked datapackage.json; migration must be able to "
            "rewrite the descriptor."
        )
    if descriptor_path.exists():
        try:
            with descriptor_path.open("r", encoding="utf-8") as stream:
                descriptor = json.load(stream)
        except (OSError, ValueError, UnicodeDecodeError) as error:
            raise SdpExtensionError(
                f"Could not parse datapackage.json: {error}"
            ) from None
        rows = []
        for resource in descriptor.get("resources") or []:
            # Metadata resources declare ``schema`` as a URL string; only
            # inline (dict) schemas can carry per-field method bindings.
            schema = resource.get("schema")
            fields = schema.get("fields") or [] if isinstance(schema, dict) else []
            for field in fields:
                custom = field.get("custom") if isinstance(field.get("custom"), dict) else {}
                method_iri = _coalesce(
                    custom.get("iAdopt:methodIri"), field.get("method_iri")
                )
                if not _is_blank(method_iri):
                    rows.append(
                        {
                            "table_id": str(_coalesce(resource.get("name"), "")),
                            "column_name": str(_coalesce(field.get("name"), "")),
                            "method_iri": str(method_iri),
                            "source": "datapackage.json",
                        }
                    )
        if rows:
            frames.append(pd.DataFrame(rows))

    if not frames:
        return pd.DataFrame(
            columns=["table_id", "column_name", "method_iri", "source"], dtype=object
        )

    merged = pd.concat(frames, ignore_index=True)
    merged = merged.loc[
        [not _is_blank(value) for value in merged["method_iri"]]
    ].reset_index(drop=True)
    # A binding with no table or column to attach to cannot be placed.
    unplaceable = [
        _is_blank(merged["table_id"].iloc[i]) or _is_blank(merged["column_name"].iloc[i])
        for i in range(len(merged))
    ]
    if any(unplaceable):
        details = [
            f"{merged['source'].iloc[i]}: table {merged['table_id'].iloc[i]}, "
            f"column {merged['column_name'].iloc[i]}"
            for i in range(len(merged))
            if unplaceable[i]
        ]
        raise SdpExtensionError(
            "Method bindings without a table and column cannot be migrated. "
            + " ".join(details)
            + " Fix the identifiers in the legacy metadata, then re-run."
        )
    # Two carriers claiming the same column is a judgement call, not something
    # to resolve by precedence: dropping one would erase it from the package.
    # Identical claims collapse; disagreements stop the migration.
    claim_keys = [
        (merged["table_id"].iloc[i], merged["column_name"].iloc[i], merged["method_iri"].iloc[i])
        for i in range(len(merged))
    ]
    keep = []
    seen_claims = set()
    for position, claim in enumerate(claim_keys):
        if claim not in seen_claims:
            seen_claims.add(claim)
            keep.append(position)
    merged = merged.iloc[keep].reset_index(drop=True)
    column_keys = [
        (merged["table_id"].iloc[i], merged["column_name"].iloc[i])
        for i in range(len(merged))
    ]
    duplicated = {key for key in column_keys if column_keys.count(key) > 1}
    if duplicated:
        details = [
            f"{merged['table_id'].iloc[i]}.{merged['column_name'].iloc[i]} = "
            f"{merged['method_iri'].iloc[i]} ({merged['source'].iloc[i]})"
            for i in range(len(merged))
            if column_keys[i] in duplicated
        ]
        raise SdpExtensionError(
            "Method migration stopped: two carriers disagree about one "
            "column's method. "
            + " ".join(details)
            + " Resolve the disagreement in the legacy metadata, then re-run."
        )
    return merged


def migrate_sdp_methods(path: Union[str, Path], dry_run: bool = False) -> dict:
    """Migrate an sdp-0.2.0 package's method metadata to sdp-0.3.0.

    Mirror of metasalmon's ``migrate_sdp_methods()`` (post-0.3.0 tree, so
    **every stop fires in the dry run as well as the real run**). sdp-0.3.0
    removed the ``metadata/methods.csv`` registry and the column-dictionary
    ``method_iri`` field. This tool relocates what can be relocated
    mechanically and **stops and reports** on anything that needs a judgement
    call, rather than guessing:

    * A ``method_iri`` shared by every bound measurement column of a table
      becomes that table's ``tables.csv$method_iri``.
    * Columns of one table bound to *different* methods stop the migration:
      you decide whether to split the table, cite a protocol, or move the
      method into the data as a code column (see the methods section of the
      SDP specification).
    * ``REVIEW:``-marked values are dropped, not migrated, and reported.
    * Registry labels and descriptions are reported, not relocated — they
      belong in the shared vocabulary. A registry ``method_version`` or
      ``citation`` is offered in the report as ``protocol_citation`` material.

    The rewrite is atomic: either every affected metadata file is updated and
    ``metadata/methods.csv`` removed, or nothing changes.

    Parameters
    ----------
    path:
        Existing Salmon Data Package directory.
    dry_run:
        When ``True``, report what would change without touching any file.

    Returns
    -------
    dict
        A report with ``tables`` (the table-level method placements applied),
        ``dropped_review`` (unresolved ``REVIEW:`` bindings dropped), and
        ``registry`` (the legacy registry rows, for relocating
        labels/descriptions to the shared vocabulary and citations to
        ``protocol_citation``; ``None`` when the package had no registry).
    """
    from .metadata import (
        DATASET_META_COLUMNS,
        DICTIONARY_COLUMNS,
        TABLE_META_COLUMNS,
        align_columns,
    )
    from .sdp_schema import load_sdp_schema

    root = _extension_root(path)
    # The write/preview decision must be made on a real logical: a truthy
    # non-bool from a caller who plainly asked for a preview must not take
    # the destructive branch (R checks ``is.logical`` for the same reason —
    # ``isTRUE(1)`` is FALSE there).
    if not isinstance(dry_run, bool):
        raise SdpExtensionError("dry_run must be True or False.")

    bindings = _method_column_bindings(root)
    registry = _read_legacy_registry(root)

    review_marked = [
        bool(_REVIEW_RE.match(str(value))) for value in bindings["method_iri"]
    ]
    dropped_review = bindings.loc[review_marked].reset_index(drop=True)
    # Canonical order: ``dropped_review`` is part of the exported report.
    if len(dropped_review) > 0:
        order = sorted(
            range(len(dropped_review)),
            key=lambda i: (
                dropped_review["table_id"].iloc[i],
                dropped_review["column_name"].iloc[i],
                dropped_review["method_iri"].iloc[i],
            ),
        )
        dropped_review = dropped_review.iloc[order].reset_index(drop=True)
    bindings = bindings.loc[[not flag for flag in review_marked]].reset_index(drop=True)

    # "Nothing to migrate" means the package already has the v0.3 shape.
    # REVIEW:-only bindings and a lingering dictionary method_iri column both
    # still require the rewrite, or the obsolete schema would survive the run
    # that reported dropping its values.
    dictionary_probe = _read_migration_metadata_csv(
        root, "metadata/column_dictionary.csv"
    )
    dictionary_has_method_column = (
        dictionary_probe is not None and "method_iri" in dictionary_probe.columns
    )
    if (
        len(bindings) == 0
        and len(dropped_review) == 0
        and registry is None
        and not dictionary_has_method_column
    ):
        print(
            "Nothing to migrate: no method bindings and no metadata/methods.csv."
        )
        return {
            # Two columns, not three: R's nothing-to-migrate report frame has
            # no ``columns`` column (unlike the empty placements frame the
            # stop-free path returns), and the differential run showed it.
            "tables": pd.DataFrame(
                columns=["table_id", "method_iri"], dtype=object
            ),
            "dropped_review": dropped_review,
            "registry": None,
        }

    # Per-table agreement check: one method per table proceeds, disagreement
    # stops. The whole report is assembled before stopping so one run surfaces
    # every decision the contributor has to make. Canonical (codepoint) order
    # throughout: the report and the conflict text are user-facing and must
    # not depend on the order rows happened to appear in the legacy metadata.
    placements_rows = []
    conflicts: List[str] = []
    universe = _measurement_universe(root)
    for tbl in sorted(set(bindings["table_id"])):
        rows = bindings.loc[bindings["table_id"] == tbl]
        iris = sorted(set(rows["method_iri"]))
        # Promotion claims the method for the WHOLE table, so every
        # measurement column must carry it — a column with no resolved binding
        # (including one whose binding was just dropped as REVIEW:) is a
        # judgement call, not silent agreement.
        bound_columns = set(rows["column_name"])
        unbound = sorted(
            column
            for table_id, column in universe
            if table_id == tbl and column not in bound_columns
        )
        if unbound:
            verb = "carries" if len(unbound) == 1 else "carry"
            conflicts.append(
                f"Table {tbl}: {', '.join(iris)} is bound to only some "
                f"measurement columns; {', '.join(unbound)} {verb} no "
                "resolved method binding."
            )
        elif len(iris) == 1:
            placements_rows.append(
                {
                    "table_id": tbl,
                    "method_iri": iris[0],
                    "columns": ", ".join(sorted(rows["column_name"])),
                }
            )
        else:
            detail = [
                iri
                + " ("
                + ", ".join(
                    sorted(rows.loc[rows["method_iri"] == iri, "column_name"])
                )
                + ")"
                for iri in iris
            ]
            conflicts.append(f"Table {tbl}: {' vs '.join(detail)}")
    placements = pd.DataFrame(
        placements_rows or None, columns=["table_id", "method_iri", "columns"]
    ).astype(object)

    # An existing non-blank tables.csv method_iri that disagrees with the
    # dictionary-derived placement is also a stop: two carriers, two claims.
    tables = _read_migration_metadata_csv(root, "metadata/tables.csv")
    if tables is not None and "method_iri" in tables.columns and len(placements) > 0:
        for index in range(len(placements)):
            tbl = placements["table_id"].iloc[index]
            existing = [
                str(value)
                for value in tables.loc[tables["table_id"] == tbl, "method_iri"]
                if not _is_blank(value)
            ]
            if existing and any(
                value != placements["method_iri"].iloc[index] for value in existing
            ):
                conflicts.append(
                    f"Table {tbl}: tables.csv already claims {existing[0]} but "
                    f"the dictionary columns claim "
                    f"{placements['method_iri'].iloc[index]}"
                )

    if conflicts:
        raise SdpExtensionError(
            "Method migration stopped: measurement columns disagree about "
            "their table's method. "
            + " ".join(conflicts)
            + " Split the table, cite a protocol instead, or move the method "
            "into the data as a code column, then re-run. See the methods "
            "section of the SDP specification for the three placements."
        )

    # ---- Report -------------------------------------------------------------
    if len(placements) > 0:
        lines = [
            f"{placements['table_id'].iloc[i]} -> "
            f"{placements['method_iri'].iloc[i]} "
            f"(from {placements['columns'].iloc[i]})"
            for i in range(len(placements))
        ]
        print("Table-level method placements:\n" + "\n".join(lines))
    if len(dropped_review) > 0:
        lines = [
            f"{dropped_review['table_id'].iloc[i]}."
            f"{dropped_review['column_name'].iloc[i]} = "
            f"{dropped_review['method_iri'].iloc[i]}"
            for i in range(len(dropped_review))
        ]
        print(
            "Unresolved REVIEW: method bindings dropped (resolve them via "
            "term search before publishing):\n" + "\n".join(lines)
        )
    if registry is not None and len(registry) > 0:
        labels = (
            registry["method_label"]
            if "method_label" in registry.columns
            else [""] * len(registry)
        )
        iris = (
            registry["method_iri"]
            if "method_iri" in registry.columns
            else [""] * len(registry)
        )
        lines = [f"{iri} ({label})" for iri, label in zip(iris, labels)]
        print(
            "metadata/methods.csv is removed by this migration. Its labels "
            "and descriptions belong in the shared vocabulary; its version "
            "and citation belong beside protocol_iri:\n"
            + "\n".join(lines)
            + "\nRequest missing vocabulary terms through the ontology's "
            "shared-term admission policy, and copy any registry citation "
            "into protocol_citation."
        )

    report = {
        "tables": placements,
        "dropped_review": dropped_review,
        "registry": registry,
    }

    # Every stop the real run would raise must also stop the preview, or a
    # clean dry run would promise a migration that then refuses to apply.
    if len(placements) > 0 and (tables is None or "table_id" not in tables.columns):
        raise SdpExtensionError(
            "Cannot migrate table-level methods: metadata/tables.csv is "
            "missing or has no table_id."
        )
    if len(placements) > 0 and tables is not None:
        declared = {str(value) for value in tables["table_id"]}
        unmatched = [
            value for value in placements["table_id"] if str(value) not in declared
        ]
        if unmatched:
            raise SdpExtensionError(
                "Method bindings name tables that metadata/tables.csv does "
                "not declare: "
                + ", ".join(unmatched)
                + ". Fix the table identifiers in the legacy metadata, then "
                "re-run."
            )

    if dry_run:
        print("Dry run: no files were changed.")
        return report

    # ---- Rewrite -------------------------------------------------------------
    writes: Dict[str, bytes] = {}

    # The placement destination was already proved to exist above, before the
    # dry-run return, on this same unchanged placements/tables pair — so a
    # repeat of those checks here would be unreachable. Deliberately not
    # duplicated: a dead guard invites someone to weaken the live one.
    if tables is not None:
        new_tables = tables.copy()
        if "method_iri" not in new_tables.columns:
            new_tables["method_iri"] = pd.NA
        for index in range(len(placements)):
            hit = new_tables["table_id"] == placements["table_id"].iloc[index]
            new_tables.loc[hit, "method_iri"] = placements["method_iri"].iloc[index]
        new_tables = align_columns(new_tables, TABLE_META_COLUMNS)
        writes[str(root / "metadata" / "tables.csv")] = _csv_bytes(
            list(new_tables.columns), new_tables
        )

    if dictionary_probe is not None:
        new_dictionary = dictionary_probe.copy()
        if "method_iri" in new_dictionary.columns:
            new_dictionary = new_dictionary.drop(columns=["method_iri"])
        new_dictionary = align_columns(new_dictionary, DICTIONARY_COLUMNS)
        writes[str(root / "metadata" / "column_dictionary.csv")] = _csv_bytes(
            list(new_dictionary.columns), new_dictionary
        )

    dataset = _read_migration_metadata_csv(root, "metadata/dataset.csv")
    if dataset is not None:
        new_dataset = dataset.copy()
        if "spec_version" in new_dataset.columns:
            from .sdp_schema import sdp_profile_version

            new_dataset["spec_version"] = sdp_profile_version()
        new_dataset = align_columns(new_dataset, DATASET_META_COLUMNS)
        writes[str(root / "metadata" / "dataset.csv")] = _csv_bytes(
            list(new_dataset.columns), new_dataset
        )

    # The gather phase already aborted on a symlinked or unparseable
    # descriptor, so reaching here means it is safe to rewrite.
    descriptor_path = root / "datapackage.json"
    if descriptor_path.exists():
        try:
            with descriptor_path.open("r", encoding="utf-8") as stream:
                descriptor = json.load(stream)
        except (OSError, ValueError, UnicodeDecodeError) as error:
            raise SdpExtensionError(
                f"Could not parse datapackage.json: {error}"
            ) from None
        sdp_schema = load_sdp_schema(quiet=True)
        descriptor["resources"] = [
            resource
            for resource in descriptor.get("resources") or []
            if resource.get("name", "") != "sdp_methods"
            and resource.get("path", "") != SDP_METHODS_PATH
        ]
        for resource in descriptor["resources"]:
            # Metadata resources declare ``schema`` as a URL string, not a dict.
            schema = resource.get("schema")
            if not isinstance(schema, dict) or not schema.get("fields"):
                continue
            for field in schema["fields"]:
                custom = field.get("custom")
                if isinstance(custom, dict):
                    custom.pop("iAdopt:methodIri", None)
                    if not custom:
                        field.pop("custom", None)
                field.pop("method_iri", None)
        sdp_block = descriptor.get("sdp")
        if isinstance(sdp_block, dict):
            metadata_block = sdp_block.get("metadata")
            if isinstance(metadata_block, dict):
                metadata_block.pop("methods", None)
        descriptor["profile"] = _coalesce(
            sdp_schema.get("profile_uri"), descriptor.get("profile")
        )
        if isinstance(sdp_block, dict):
            sdp_block["specVersion"] = _coalesce(
                sdp_schema.get("version"), sdp_block.get("specVersion")
            )
            # The writer emits the profile URI twice, top level and under
            # ``sdp``. Updating only one leaves a descriptor that contradicts
            # itself.
            sdp_block["profile"] = _coalesce(
                sdp_schema.get("profile_uri"), sdp_block.get("profile")
            )
            sdp_block["rules"] = _coalesce(
                sdp_schema.get("rules_uri"), sdp_block.get("rules")
            )
        writes[str(descriptor_path)] = _json_bytes(descriptor)

    # Registry removal is part of the transaction: the registry is renamed
    # aside BEFORE the metadata rewrite, restored if the rewrite fails, and
    # discarded only after it succeeds. A package can therefore never end up
    # with v0.3 metadata beside a registry that v0.3 validation rejects.
    registry_path = root / SDP_METHODS_PATH
    registry_backup: Optional[str] = None
    if registry_path.exists():
        handle, registry_backup = tempfile.mkstemp(
            prefix=".methods.csv-migrate-", dir=str(registry_path.parent)
        )
        os.close(handle)
        os.unlink(registry_backup)
        try:
            os.replace(str(registry_path), registry_backup)
        except OSError:
            raise SdpExtensionError(
                "Could not remove metadata/methods.csv; migration aborted "
                "before any changes."
            ) from None

    if writes:
        try:
            _atomic_write_set(writes)
        except BaseException:
            if registry_backup and os.path.exists(registry_backup):
                try:
                    os.replace(registry_backup, str(registry_path))
                except OSError:
                    warnings.warn(
                        "Could not restore metadata/methods.csv after a "
                        "failed migration; recover it from "
                        f"'{os.path.basename(registry_backup)}'.",
                        stacklevel=2,
                    )
            raise
    if registry_backup and os.path.exists(registry_backup):
        os.unlink(registry_backup)

    print(
        "Migration complete.\n"
        f'Run validate_salmon_datapackage("{path}") to confirm the package.'
    )
    return report


__all__ = [
    "SDP_METHODS_COLUMNS",
    "SDP_METHODS_PATH",
    "SdpExtensionError",
    "migrate_sdp_methods",
    "read_sdp_methods",
    "validate_sdp_methods",
    "write_sdp_methods",
]
