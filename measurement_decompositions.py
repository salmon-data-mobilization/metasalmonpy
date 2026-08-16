"""Ordered SDP measurement-decomposition artifacts.

Mirrors metasalmon's ``R/measurement-decompositions.R`` at the 0.1.7 era
(the file is byte-identical at the v0.1.7 and v0.1.8 tags). The SDP
dictionary keeps one interoperable value per semantic slot; a compound
measurement can need more detail than those frozen columns permit,
including repeated constraints and explicit vocabulary gaps. This module
stores that detail in a separate, manifest-bound artifact. Its roles are
informed by I-ADOPT, but the artifact does not claim native I-ADOPT
conformance and is not an SSSOM mapping set.

Era note: the component-role vocabulary here deliberately includes the
transitional ``method`` role. metasalmon 0.3.0 later replaced it with
``statistical_modifier``; that change lands at this replay's own 0.3.0
milestone, so 0.1.7-era artifacts with method components must read, write,
and validate here exactly as they did in R 0.1.7.

Byte-parity contract: ``_csv_bytes`` must reproduce R's
``.ms_sdp_decomposition_csv_bytes`` (``readr::format_csv(rows, na = "")``)
byte for byte — UTF-8, LF-only, trailing LF, fields quoted only when they
contain a comma, double quote, or newline (quotes doubled), missing values
as empty fields, and rows sorted by the binding keys then component order
in C collation (Python's codepoint ``sorted()`` matches dplyr's radix
default; ``locale.strxfrm`` stays banned). Verified against R-generated
fixtures under ``tests/data/decompositions/``.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import warnings
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

import pandas as pd

from .atomic_io import atomic_write
from .metadata import normalize_dictionary, read_sdp_csv

SDP_DECOMPOSITION_SCHEMA_VERSION = "1.0"
SDP_DECOMPOSITION_CSV_PATH = "metadata/semantic/measurement-decompositions.csv"
SDP_DECOMPOSITION_MANIFEST_PATH = (
    "metadata/semantic/measurement-decompositions.json"
)

_COLUMNS = (
    "dataset_id",
    "table_id",
    "column_name",
    "measurement_concept_iri",
    "component_order",
    "component_role",
    "component_status",
    "component_relation",
    "related_component_order",
    "component_iri",
    "component_label",
    "rationale",
    "source",
    "source_version",
    "source_url",
    "provenance",
)

_ORDER_COLUMNS = ("component_order", "related_component_order")
_CHARACTER_COLUMNS = tuple(
    column for column in _COLUMNS if column not in _ORDER_COLUMNS
)

# The 0.1.7-era closed role vocabulary. ``method`` is transitional: it is
# replaced by ``statistical_modifier`` at the 0.3.0 milestone and must be
# accepted (and ``statistical_modifier`` rejected) until then.
_ALLOWED_ROLES = ("property", "entity", "constraint", "method", "unit")

_BINDING_FIELDS = ("dataset_id", "table_id", "column_name")
_MEASUREMENT_FIELDS = _BINDING_FIELDS + ("measurement_concept_iri",)

# Dictionary slot column -> required component role (era order, method
# included).
_SLOT_ROLES = (
    ("property_iri", "property"),
    ("entity_iri", "entity"),
    ("constraint_iri", "constraint"),
    ("method_iri", "method"),
    ("unit_iri", "unit"),
)

# Mirror of R's .ms_sdp_decomposition_is_absolute_iri: scheme:// or urn:,
# then no ASCII whitespace ([[:space:]] in a non-UCP PCRE is ASCII-only).
_ABSOLUTE_IRI_RE = re.compile(
    r"^(?:[A-Za-z][A-Za-z0-9+.\-]*://|urn:)[^ \t\n\v\f\r]+$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_ACCEPTED_PROVENANCE = {
    # generated_by -> the provenance key that must carry a version. The
    # validator accepts artifacts written by either mirror implementation
    # (PARITY.md entry 12); R's validator accepts only R's writer.
    "metasalmon::write_sdp_measurement_decompositions": "metasalmon_version",
    "metasalmonpy.write_sdp_measurement_decompositions": "metasalmonpy_version",
}

_TRIM_CHARS = " \t\r\n"  # R trimws() default character class


def _trim(value: str) -> str:
    return value.strip(_TRIM_CHARS)


def _is_missing(value: object) -> bool:
    """True for None/NaN/pd.NA cell values (the R ``NA`` analogue)."""
    if value is None or value is pd.NA:
        return True
    return isinstance(value, float) and value != value


def _is_absolute_iri(value: str) -> bool:
    return _ABSOLUTE_IRI_RE.match(value) is not None


def _assert_scalar(value: object, column: str) -> None:
    """Mirror R's ``is.atomic`` column requirement, one cell at a time."""
    if value is None or value is pd.NA:
        return
    if isinstance(value, (str, bool, int, float)):
        return
    flag = pd.isna(value)
    if isinstance(flag, bool):
        return
    raise ValueError(
        f"Decomposition column {column} must contain only scalar values."
    )


def _as_character(value: object) -> str:
    """Mirror ``as.character()`` with NA -> "" (R normalizes NA to "")."""
    if _is_missing(value):
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        # R prints whole numerics without a decimal point.
        return str(int(value))
    return str(value)


def _order_text(value: object) -> Optional[str]:
    """The trimmed character form R coerces order columns through."""
    if _is_missing(value):
        return None
    return _trim(_as_character(value))


def _root(path: Union[str, Path]) -> Path:
    if (
        isinstance(path, (list, tuple))
        or path is None
        or not str(path)
        or not Path(path).is_dir()
    ):
        raise ValueError(
            "path must name one existing Salmon Data Package directory."
        )
    return Path(os.path.realpath(str(path)))


def _paths(root: Path) -> Tuple[Path, Path]:
    return root / SDP_DECOMPOSITION_CSV_PATH, root / SDP_DECOMPOSITION_MANIFEST_PATH


# --- row validation (era rules, same order as R) ------------------------------


def _validate_row_states(columns: Dict[str, List[str]]) -> None:
    """Mirror ``.ms_sdp_decomposition_validate_row_states``."""
    if any(role not in _ALLOWED_ROLES for role in columns["component_role"]):
        raise ValueError(
            "component_role must be one of: "
            + ", ".join(_ALLOWED_ROLES)
            + "."
        )
    statuses = columns["component_status"]
    if any(status not in ("matched", "gap") for status in statuses):
        raise ValueError('component_status must be "matched" or "gap".')

    matched = [status == "matched" for status in statuses]
    gap = [status == "gap" for status in statuses]
    iris = columns["component_iri"]
    if any(
        matched[row] and not _is_absolute_iri(_trim(iris[row]))
        for row in range(len(statuses))
    ):
        raise ValueError(
            "Every matched component must have an absolute component_iri IRI."
        )
    if any(gap[row] and _trim(iris[row]) for row in range(len(statuses))):
        raise ValueError("Every gap row must have a blank component_iri.")
    labels = columns["component_label"]
    if any(gap[row] and not _trim(labels[row]) for row in range(len(statuses))):
        raise ValueError("Every gap row must have a non-empty component_label.")
    rationales = columns["rationale"]
    if any(
        gap[row] and not _trim(rationales[row]) for row in range(len(statuses))
    ):
        raise ValueError("Every gap row must have a non-empty rationale.")

    for field in ("source", "source_version", "provenance"):
        if any(not _trim(value) for value in columns[field]):
            raise ValueError(
                f"Decomposition field {field} must be non-empty on every row."
            )
    if any(
        not _is_absolute_iri(_trim(value)) for value in columns["source_url"]
    ):
        raise ValueError("Every source_url must be an absolute IRI.")


def _measurement_keys(columns: Dict[str, List[str]]) -> List[Tuple[str, ...]]:
    return [
        tuple(columns[field][row] for field in _MEASUREMENT_FIELDS)
        for row in range(len(columns["dataset_id"]))
    ]


def _validate_order_and_uniqueness(
    columns: Dict[str, List[str]], component_order: List[int]
) -> None:
    """Mirror ``.ms_sdp_decomposition_validate_order_and_uniqueness``."""
    keys = _measurement_keys(columns)
    for measurement in dict.fromkeys(keys):
        orders = [
            component_order[row]
            for row in range(len(keys))
            if keys[row] == measurement
        ]
        if len(set(orders)) != len(orders):
            raise ValueError(
                "component_order must be unique within each bound measurement."
            )
        if sorted(orders) != list(range(1, len(orders) + 1)):
            raise ValueError(
                "component_order must be contiguous from 1 within each "
                "bound measurement."
            )

    identities = [
        keys[row]
        + (
            columns["component_role"][row],
            columns["component_status"][row],
            columns["component_iri"][row]
            if columns["component_status"][row] == "matched"
            else columns["component_label"][row],
            columns["source"][row],
            columns["source_version"][row],
        )
        for row in range(len(keys))
    ]
    if len(set(identities)) != len(identities):
        raise ValueError(
            "Duplicate semantic component found within one bound measurement."
        )


def _validate_relations(
    columns: Dict[str, List[str]],
    component_order: List[int],
    related_component_order: List[Optional[int]],
) -> None:
    """Mirror ``.ms_sdp_decomposition_validate_relations``."""
    relations = columns["component_relation"]
    row_count = len(relations)
    has_relation = [bool(_trim(value)) for value in relations]
    has_target = [value is not None for value in related_component_order]
    if any(
        has_relation[row] != has_target[row] for row in range(row_count)
    ):
        raise ValueError(
            "component_relation and related_component_order must either "
            "both be populated or both be blank together."
        )
    if any(
        has_relation[row] and relations[row] != "value_of_dimension"
        for row in range(row_count)
    ):
        raise ValueError(
            "component_relation currently supports only 'value_of_dimension'."
        )

    keys = _measurement_keys(columns)
    statuses = columns["component_status"]
    roles = columns["component_role"]
    for row in range(row_count):
        if not has_relation[row]:
            continue
        related_order = related_component_order[row]
        if related_order >= component_order[row]:
            raise ValueError(
                "A component relation must target an earlier component in "
                "the same measurement."
            )
        targets = [
            index
            for index in range(row_count)
            if keys[index] == keys[row]
            and component_order[index] == related_order
        ]
        if len(targets) != 1:
            raise ValueError(
                "A component relation must target an earlier component in "
                "the same measurement."
            )
        target = targets[0]
        if (
            statuses[row] != "matched"
            or roles[row] != "constraint"
            or statuses[target] != "matched"
            or roles[target] != "constraint"
        ):
            raise ValueError(
                "'value_of_dimension' must connect two matched constraint "
                "components."
            )


# --- normalization -------------------------------------------------------------


def _normalize_rows(decompositions: object) -> pd.DataFrame:
    """Mirror ``.ms_sdp_decomposition_normalize_rows``.

    Accepts a DataFrame (or a dict of columns, the Pythonic analogue of an
    in-memory data frame), enforces the closed 16-column schema, coerces
    order columns to positive whole numbers, validates row states,
    per-measurement order, uniqueness, and relations, then returns the
    canonical frame sorted by binding keys and component order.
    """
    if isinstance(decompositions, dict):
        decompositions = pd.DataFrame(decompositions)
    if not isinstance(decompositions, pd.DataFrame) or len(decompositions) == 0:
        raise ValueError("decompositions must be a non-empty data frame.")

    names = [str(name) for name in decompositions.columns]
    missing_columns = [name for name in _COLUMNS if name not in names]
    unknown_columns = [name for name in names if name not in _COLUMNS]
    duplicate_columns = len(set(names)) != len(names)
    if missing_columns or unknown_columns or duplicate_columns:
        details = []
        if missing_columns:
            details.append(
                "Missing columns: " + ", ".join(missing_columns) + "."
            )
        if unknown_columns:
            details.append(
                "Unknown columns: " + ", ".join(unknown_columns) + "."
            )
        if duplicate_columns:
            details.append("Column names must be unique.")
        raise ValueError(
            "decompositions does not match the ordered SDP decomposition "
            "schema. " + " ".join(details)
        )

    columns: Dict[str, List[str]] = {}
    for name in _CHARACTER_COLUMNS:
        values = decompositions[name].tolist()
        cells = []
        for value in values:
            _assert_scalar(value, name)
            cells.append(_as_character(value))
        columns[name] = cells

    component_order: List[int] = []
    for value in decompositions["component_order"].tolist():
        _assert_scalar(value, "component_order")
        text = _order_text(value)
        parsed: Optional[int] = None
        if text is not None:
            try:
                parsed = int(text)
            except ValueError:
                parsed = None
        if text is None or parsed is None or parsed < 1 or text != str(parsed):
            raise ValueError(
                "component_order must contain positive whole numbers."
            )
        component_order.append(parsed)

    related_component_order: List[Optional[int]] = []
    for value in decompositions["related_component_order"].tolist():
        _assert_scalar(value, "related_component_order")
        text = _order_text(value)
        if text is None or text == "":
            related_component_order.append(None)
            continue
        try:
            parsed = int(text)
        except ValueError:
            parsed = None
        if parsed is None or parsed < 1 or text != str(parsed):
            raise ValueError(
                "related_component_order must be blank or a positive whole "
                "number."
            )
        related_component_order.append(parsed)

    _validate_row_states(columns)
    _validate_order_and_uniqueness(columns, component_order)
    _validate_relations(columns, component_order, related_component_order)

    # Canonical order: binding keys then component order. Codepoint string
    # comparison matches dplyr's radix (C-locale) arrange.
    order = sorted(
        range(len(component_order)),
        key=lambda row: (
            columns["dataset_id"][row],
            columns["table_id"][row],
            columns["column_name"][row],
            columns["measurement_concept_iri"][row],
            component_order[row],
        ),
    )

    result: Dict[str, object] = {}
    for name in _COLUMNS:
        if name == "component_order":
            result[name] = pd.Series(
                [component_order[row] for row in order], dtype="int64"
            )
        elif name == "related_component_order":
            result[name] = pd.array(
                [related_component_order[row] for row in order], dtype="Int64"
            )
        else:
            result[name] = pd.Series(
                [columns[name][row] for row in order], dtype=object
            )
    return pd.DataFrame(result, columns=list(_COLUMNS))


# --- canonical serialization ----------------------------------------------------


def _csv_field(value: str) -> str:
    """One CSV field, quoted exactly when readr::format_csv would quote."""
    if any(character in value for character in ',"\n\r'):
        return '"' + value.replace('"', '""') + '"'
    return value


def _csv_bytes(rows: pd.DataFrame) -> bytes:
    """Mirror ``.ms_sdp_decomposition_csv_bytes`` byte for byte."""
    lines = [",".join(_COLUMNS)]
    orders = rows["component_order"].tolist()
    related = rows["related_component_order"].tolist()
    cells = {name: rows[name].tolist() for name in _CHARACTER_COLUMNS}
    for row in range(len(rows)):
        fields = []
        for name in _COLUMNS:
            if name == "component_order":
                fields.append(str(int(orders[row])))
            elif name == "related_component_order":
                value = related[row]
                fields.append("" if _is_missing(value) else str(int(value)))
            else:
                fields.append(_csv_field(cells[name][row]))
        lines.append(",".join(fields))
    return ("\n".join(lines) + "\n").encode("utf-8")


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


def _manifest(data: bytes, row_count: int) -> Dict[str, object]:
    """Mirror ``.ms_sdp_decomposition_manifest``.

    The provenance block honestly names this implementation (PARITY.md
    entry 12), so manifest bytes differ from R's only in the provenance
    values; the artifact binding (path, sha256, row_count) is identical.
    """
    return {
        "schema_version": SDP_DECOMPOSITION_SCHEMA_VERSION,
        "artifact": {
            "path": SDP_DECOMPOSITION_CSV_PATH,
            "sha256": hashlib.sha256(data).hexdigest(),
            "row_count": int(row_count),
        },
        "provenance": {
            "generated_by": "metasalmonpy.write_sdp_measurement_decompositions",
            "metasalmonpy_version": _package_version(),
            "semantic_profile": (
                "Ordered SDP semantic profile with I-ADOPT-informed roles; "
                "not native I-ADOPT conformance."
            ),
        },
    }


def _json_bytes(manifest: Dict[str, object]) -> bytes:
    return (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def _atomic_write(data: bytes, path: Path) -> None:
    """Mirror ``.ms_sdp_decomposition_atomic_write``: write-then-rename.

    The shared helper restores the umask-default mode that R's ``writeBin``
    would have produced; ``tempfile.mkstemp`` would otherwise publish the
    decomposition CSV and manifest as 0600.
    """
    atomic_write(data, path)


def _assert_output_directory(root: Path, directory: Path) -> None:
    """Mirror ``.ms_sdp_decomposition_assert_output_directory``."""
    if directory.is_symlink():
        raise ValueError(
            "Refusing to write measurement decompositions through a "
            "semantic-directory symlink."
        )
    real_root = Path(os.path.realpath(str(root)))
    real_directory = Path(os.path.realpath(str(directory)))
    if real_directory != real_root and real_root not in real_directory.parents:
        raise ValueError(
            "Measurement-decomposition output directory resolves outside "
            "the SDP and is unsafe."
        )


# --- byte-level reading ----------------------------------------------------------


def _read_raw(path: Path, label: str) -> bytes:
    if not path.exists() or path.is_dir():
        raise FileNotFoundError(f"Missing {label} at {path}.")
    return path.read_bytes()


def _text_from_bytes(data: bytes, label: str) -> str:
    if data[:3] == b"\xef\xbb\xbf":
        raise ValueError(f"{label} must not contain a UTF-8 BOM.")
    if b"\r" in data:
        raise ValueError(
            f"{label} must use LF line endings without carriage returns."
        )
    if len(data) == 0 or data[-1:] != b"\n":
        raise ValueError(f"{label} must end with a final LF newline.")
    # R fails on NUL bytes at rawToChar(); reject them here the same way.
    if b"\x00" in data:
        raise ValueError(f"{label} must not contain NUL bytes.")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"{label} must contain valid UTF-8 text.") from None


def _read_manifest(path: Path) -> Dict[str, object]:
    data = _read_raw(path, "decomposition manifest")
    text = _text_from_bytes(data, "Measurement-decomposition manifest")
    try:
        return json.loads(text)
    except ValueError as error:
        raise ValueError(
            f"Measurement-decomposition manifest is not valid JSON: {error}"
        ) from None


def _parse_csv(text: str) -> Tuple[List[str], List[List[str]]]:
    """Parse CSV text with readr-equivalent recovery semantics.

    Empty lines are skipped (readr ``skip_empty_rows``); a short row is
    padded with empty fields and an overlong row folds its extra fields
    into the last column, both with a warning — the same repairs
    ``readr::read_csv`` applies while reporting parsing problems.
    """
    records = [row for row in csv.reader(io.StringIO(text)) if row]
    if not records:
        return [], []
    header = records[0]
    width = len(header)
    body: List[List[str]] = []
    repaired = False
    for row in records[1:]:
        if len(row) < width:
            row = row + [""] * (width - len(row))
            repaired = True
        elif len(row) > width:
            row = row[: width - 1] + [",".join(row[width - 1 :])]
            repaired = True
        body.append(row)
    if repaired:
        warnings.warn(
            "One or more measurement-decomposition CSV rows did not match "
            "the header width and were repaired (mirrors readr's parsing-"
            "problem recovery).",
            stacklevel=2,
        )
    return header, body


def _read_csv(path: Path) -> Tuple[bytes, pd.DataFrame]:
    data = _read_raw(path, "measurement decompositions")
    text = _text_from_bytes(data, "Measurement-decomposition CSV")
    try:
        header, body = _parse_csv(text)
    except csv.Error as error:
        raise ValueError(
            f"Measurement-decomposition CSV could not be parsed: {error}"
        ) from None
    frame = pd.DataFrame(body, columns=header if header else None, dtype=object)
    return data, _normalize_rows(frame)


# --- manifest validation ----------------------------------------------------------


def _validate_manifest(
    manifest: object, data: bytes, rows: pd.DataFrame
) -> None:
    """Mirror ``.ms_sdp_decomposition_validate_manifest``.

    The provenance check accepts artifacts written by either mirror
    implementation (PARITY.md entry 12): R stamps
    ``metasalmon::write_sdp_measurement_decompositions`` +
    ``metasalmon_version``; this writer stamps
    ``metasalmonpy.write_sdp_measurement_decompositions`` +
    ``metasalmonpy_version``. Both must carry a non-empty version and
    ``semantic_profile``.
    """
    if not isinstance(manifest, dict) or any(
        name not in manifest
        for name in ("schema_version", "artifact", "provenance")
    ):
        raise ValueError(
            "Measurement-decomposition manifest is missing required fields."
        )
    if manifest["schema_version"] != SDP_DECOMPOSITION_SCHEMA_VERSION:
        raise ValueError(
            "Measurement-decomposition manifest has an unsupported schema "
            "version."
        )
    artifact = manifest["artifact"]
    if (
        not isinstance(artifact, dict)
        or any(name not in artifact for name in ("path", "sha256", "row_count"))
        or artifact["path"] != SDP_DECOMPOSITION_CSV_PATH
    ):
        raise ValueError(
            "Measurement-decomposition manifest artifact binding is "
            "incomplete or unsafe."
        )

    row_count = artifact["row_count"]
    if (
        isinstance(row_count, bool)
        or not isinstance(row_count, (int, float))
        or (isinstance(row_count, float) and not math.isfinite(row_count))
        or row_count < 0
        or row_count != int(row_count)
    ):
        raise ValueError(
            "Manifest artifact.row_count must be one non-negative whole "
            "number."
        )

    actual_sha256 = hashlib.sha256(data).hexdigest()
    if (
        not isinstance(artifact["sha256"], str)
        or _SHA256_RE.match(artifact["sha256"]) is None
        or artifact["sha256"] != actual_sha256
    ):
        raise ValueError(
            "Measurement-decomposition CSV does not match its manifest "
            "SHA-256 hash."
        )
    if int(row_count) != len(rows):
        raise ValueError(
            "Measurement-decomposition CSV does not match its manifest "
            "row count."
        )

    provenance = manifest["provenance"]
    version_key = (
        _ACCEPTED_PROVENANCE.get(provenance.get("generated_by"))
        if isinstance(provenance, dict)
        else None
    )
    if (
        version_key is None
        or not isinstance(provenance.get(version_key), str)
        or not _trim(provenance[version_key])
        or not isinstance(provenance.get("semantic_profile"), str)
        or not _trim(provenance["semantic_profile"])
    ):
        raise ValueError(
            "Measurement-decomposition manifest writer provenance is "
            "incomplete."
        )


# --- dictionary closure ------------------------------------------------------------


def _read_dictionary(root: Path) -> pd.DataFrame:
    """Mirror ``.ms_sdp_decomposition_dictionary``.

    The dictionary is read with readr's missing-value defaults ("" and
    "NA" are missing) and aligned to the SDP dictionary schema so every
    slot column exists.
    """
    candidates = (
        root / "metadata" / "column_dictionary.csv",
        root / "column_dictionary.csv",
    )
    dictionary_path = next(
        (candidate for candidate in candidates if candidate.is_file()), None
    )
    if dictionary_path is None:
        raise FileNotFoundError(
            "The SDP does not contain metadata/column_dictionary.csv."
        )
    return normalize_dictionary(read_sdp_csv(dictionary_path))


def _dictionary_values(value: object, field: str) -> List[str]:
    """Mirror ``.ms_sdp_decomposition_dictionary_values``."""
    if _is_missing(value):
        return []
    text = str(value)
    if not _trim(text):
        return []
    pieces = text.split(";") if field == "constraint_iri" else [text]
    return [_trim(piece) for piece in pieces if _trim(piece)]


def _validate_dictionary(root: Path, rows: pd.DataFrame) -> None:
    """Mirror ``.ms_sdp_decomposition_validate_dictionary``."""
    columns = {
        name: [str(value) for value in rows[name].tolist()]
        for name in _CHARACTER_COLUMNS
    }
    if any(
        not _trim(value)
        for field in _BINDING_FIELDS
        for value in columns[field]
    ):
        raise ValueError(
            "Decomposition binding fields must be non-empty: "
            + ", ".join(_BINDING_FIELDS)
            + "."
        )
    if any(
        not _is_absolute_iri(_trim(value))
        for value in columns["measurement_concept_iri"]
    ):
        raise ValueError("measurement_concept_iri must be an absolute IRI.")

    dictionary = _read_dictionary(root)
    dictionary_columns = {
        name: [_as_character(value) for value in dictionary[name].tolist()]
        for name in ("dataset_id", "table_id", "column_name", "column_role",
                     "term_iri")
        + tuple(field for field, _ in _SLOT_ROLES)
    }
    keys = _measurement_keys(columns)
    statuses = columns["component_status"]
    for binding in dict.fromkeys(keys):
        dataset_id, table_id, column_name, concept_iri = binding
        dictionary_indices = [
            index
            for index in range(len(dictionary_columns["dataset_id"]))
            if dictionary_columns["dataset_id"][index] == dataset_id
            and dictionary_columns["table_id"][index] == table_id
            and dictionary_columns["column_name"][index] == column_name
        ]
        if not dictionary_indices:
            raise ValueError(
                f"The bound measurement {dataset_id}/{table_id}/{column_name} "
                "does not exist in the SDP dictionary."
            )
        if len(dictionary_indices) > 1:
            raise ValueError(
                "The SDP dictionary contains an ambiguous duplicate bound "
                "measurement."
            )
        dictionary_index = dictionary_indices[0]
        if dictionary_columns["column_role"][dictionary_index] != "measurement":
            raise ValueError(
                "The bound dictionary row has column_role other than "
                '"measurement".'
            )
        if dictionary_columns["term_iri"][dictionary_index] != concept_iri:
            raise ValueError(
                "measurement_concept_iri must equal the bound dictionary "
                "term_iri."
            )

        matched_rows = [
            row
            for row in range(len(keys))
            if keys[row] == binding and statuses[row] == "matched"
        ]
        for field, role in _SLOT_ROLES:
            dictionary_values = _dictionary_values(
                dictionary_columns[field][dictionary_index], field
            )
            if not dictionary_values:
                continue
            matched_values = [
                columns["component_iri"][row]
                for row in matched_rows
                if columns["component_role"][row] == role
            ]
            missing_values = [
                value
                for value in dictionary_values
                if value not in matched_values
            ]
            if missing_values:
                raise ValueError(
                    f"Dictionary {field} value must appear as a matched "
                    f"{role} component: " + ", ".join(missing_values) + "."
                )


# --- public API ----------------------------------------------------------------------


def read_sdp_measurement_decompositions(
    path: Union[str, Path], validate: bool = True
) -> pd.DataFrame:
    """Read ordered measurement decompositions from a Salmon Data Package.

    Reads the manifest-bound decomposition artifact that preserves repeated
    semantic components and explicit gaps beyond the frozen SDP dictionary
    columns. The profile uses I-ADOPT-informed roles, but does not claim
    native I-ADOPT conformance and is separate from SSSOM vocabulary
    mappings.

    Parameters
    ----------
    path:
        Existing Salmon Data Package directory.
    validate:
        When ``True``, validate the exact-byte manifest binding and the
        decomposition rows against the package dictionary. A ``False`` read
        still requires the closed row schema, valid row states,
        deterministic order, UTF-8, LF endings, and no BOM.

    Returns
    -------
    pandas.DataFrame
        The decomposition rows in canonical component order. The parsed
        manifest is attached as ``result.attrs["manifest"]``.
    """
    root = _root(path)
    csv_path, manifest_path = _paths(root)
    semantic_directory = csv_path.parent
    if semantic_directory.is_dir() or semantic_directory.is_symlink():
        _assert_output_directory(root, semantic_directory)
    symlinks = [
        str(candidate)
        for candidate in (csv_path, manifest_path)
        if candidate.exists() and candidate.is_symlink()
    ]
    if symlinks:
        raise ValueError(
            "Refusing to read measurement-decomposition symlinks: "
            + ", ".join(symlinks)
            + "."
        )
    manifest = _read_manifest(manifest_path)
    data, rows = _read_csv(csv_path)

    if not isinstance(validate, bool):
        raise ValueError("validate must be True or False.")
    if validate:
        _validate_manifest(manifest, data, rows)
        _validate_dictionary(root, rows)
    rows.attrs["manifest"] = manifest
    return rows


def validate_sdp_measurement_decompositions(path: Union[str, Path]) -> bool:
    """Validate ordered SDP measurement-decomposition artifacts.

    Parameters
    ----------
    path:
        Existing Salmon Data Package directory.

    Returns
    -------
    bool
        ``True`` when validation succeeds; otherwise an exception is raised.
    """
    read_sdp_measurement_decompositions(path, validate=True)
    return True


def write_sdp_measurement_decompositions(
    path: Union[str, Path],
    decompositions: object = None,
    overwrite: bool = False,
) -> Optional[str]:
    """Write ordered measurement decompositions into a Salmon Data Package.

    Writes explicit decomposition rows to
    ``metadata/semantic/measurement-decompositions.csv`` and binds the
    exact deterministic bytes to ``measurement-decompositions.json``. The
    writer never infers components, splits labels, or converts
    decompositions into SSSOM mappings. Supplying ``decompositions=None``
    is an explicit no-op.

    Each row has the following closed schema:

    - ``dataset_id``, ``table_id``, and ``column_name`` bind the
      decomposition to one measurement row in
      ``metadata/column_dictionary.csv``.
    - ``measurement_concept_iri`` must exactly equal that dictionary row's
      ``term_iri``.
    - ``component_order`` is a positive, contiguous, per-measurement
      sequence.
    - ``component_role`` is one of ``property``, ``entity``,
      ``constraint``, ``method``, or ``unit``; repeated roles are allowed.
      (``method`` is the transitional 0.1.7-era role replaced by
      ``statistical_modifier`` at 0.3.0.)
    - ``component_status`` is ``matched`` or ``gap``. A matched row
      requires an absolute ``component_iri``. A gap requires a blank
      ``component_iri`` plus a non-empty ``component_label`` and
      ``rationale``.
    - ``component_relation`` and ``related_component_order`` are normally
      blank. ``value_of_dimension`` links a matched constraint value to an
      earlier matched constraint dimension in the same measurement.
    - ``source``, ``source_version``, ``source_url``, and ``provenance``
      identify the pinned source and review evidence. ``component_label``
      and ``rationale`` preserve caller-supplied text; they are never
      tokenized or inferred.

    Every non-empty dictionary ``property_iri``, ``entity_iri``,
    ``constraint_iri``, ``method_iri``, and ``unit_iri`` must appear as a
    matched component of the same role. Semicolon-separated dictionary
    constraints are checked separately. Additional same-role components and
    explicit gaps stay only in this artifact, leaving the frozen SDP
    dictionary columns unchanged.

    Parameters
    ----------
    path:
        Existing Salmon Data Package directory.
    decompositions:
        ``None``, or a non-empty DataFrame (or dict of columns) matching
        the ordered measurement-decomposition schema.
    overwrite:
        Replace artifacts managed by this writer when ``True``.

    Returns
    -------
    Optional[str]
        The manifest path, or ``None`` when ``decompositions`` is ``None``.
    """
    if decompositions is None:
        return None
    root = _root(path)
    if not isinstance(overwrite, bool):
        raise ValueError("overwrite must be True or False.")

    rows = _normalize_rows(decompositions)
    _validate_dictionary(root, rows)
    data = _csv_bytes(rows)
    manifest = _manifest(data, len(rows))
    manifest_data = _json_bytes(manifest)
    csv_path, manifest_path = _paths(root)
    managed = (csv_path, manifest_path)
    existing = [
        candidate
        for candidate in managed
        if candidate.exists() or candidate.is_symlink()
    ]
    if existing and not overwrite:
        raise FileExistsError(
            "Measurement-decomposition output already exists and overwrite "
            "is False. Existing: "
            + ", ".join(str(candidate) for candidate in existing)
            + "."
        )
    symlinks = [candidate for candidate in existing if candidate.is_symlink()]
    if symlinks:
        raise ValueError(
            "Refusing to overwrite measurement-decomposition symlinks: "
            + ", ".join(str(candidate) for candidate in symlinks)
            + "."
        )

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    _assert_output_directory(root, csv_path.parent)
    _atomic_write(data, csv_path)
    _atomic_write(manifest_data, manifest_path)

    # Re-open the exact bytes written to disk before reporting success.
    validate_sdp_measurement_decompositions(root)
    return str(manifest_path)
