"""SSSOM mapping-set support (mirrors metasalmon's ``R/sssom.R``).

A Salmon Data Package may carry reviewed vocabulary alignments, but those
alignments are not another representation of its variable decompositions.
This module therefore implements a deliberately small, strict SSSOM 1.1
profile: approved mapping sets go in; no mappings are inferred from semantic
suggestions, dictionary literals, or component columns.

Byte-parity contract: ``_canonical_bytes`` must produce output byte-identical
to metasalmon's ``.ms_sssom_canonical_bytes`` for the same mapping set —
deterministic UTF-8, LF-only, trailing-LF TSV with radix-sorted (C-collation)
curie-map header lines and mapping rows. Python's default ``sorted()`` on
``str`` compares Unicode code points, which matches R's radix (C locale,
UTF-8 byte) order for all of Unicode, so no locale machinery is needed and
``locale.strxfrm`` stays banned.

The embedded metadata header is parsed with a restricted YAML-subset parser
(scalars, one-level block mappings, block sequences) rather than a full YAML
library; this is the subset the canonical writer emits and the SDP profile
uses (see PARITY.md entry 10).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Union

import pandas as pd

from . import provenance as _provenance
from .atomic_io import atomic_write
from .metadata import R_SPACE_CLASS

SSSOM_VERSION = "1.1"
SSSOM_MANIFEST_VERSION = "1.0"

_REQUIRED_METADATA = (
    "sssom_version",
    "mapping_set_id",
    "mapping_set_version",
    "license",
    "subject_source",
    "subject_source_version",
    "object_source",
    "object_source_version",
    "curie_map",
)

_METADATA_ORDER = (
    "sssom_version",
    "mapping_set_id",
    "mapping_set_version",
    "mapping_set_source",
    "mapping_set_title",
    "mapping_set_description",
    "mapping_set_confidence",
    "creator_id",
    "creator_label",
    "license",
    "subject_type",
    "subject_source",
    "subject_source_version",
    "object_type",
    "object_source",
    "object_source_version",
    "predicate_type",
    "mapping_provider",
    "cardinality_scope",
    "mapping_tool",
    "mapping_tool_id",
    "mapping_tool_version",
    "mapping_date",
    "publication_date",
    "subject_match_field",
    "object_match_field",
    "subject_preprocessing",
    "object_preprocessing",
    "similarity_measure",
    "curation_rule",
    "curation_rule_text",
    "see_also",
    "issue_tracker",
    "other",
    "comment",
    "curie_map",
)

# These are the mapping slots in the SSSOM 1.1 model. Rejecting unknown table
# columns is intentional: an extension field called, for example,
# ``component_id`` must not turn a mapping table into an undocumented
# decomposition table.
_MAPPING_COLUMNS = (
    "record_id",
    "subject_id",
    "subject_label",
    "subject_category",
    "predicate_id",
    "predicate_label",
    "predicate_modifier",
    "object_id",
    "object_label",
    "object_category",
    "mapping_justification",
    "author_id",
    "author_label",
    "reviewer_id",
    "reviewer_label",
    "creator_id",
    "creator_label",
    "license",
    "subject_type",
    "subject_source",
    "subject_source_version",
    "object_type",
    "object_source",
    "object_source_version",
    "predicate_type",
    "mapping_provider",
    "mapping_source",
    "mapping_cardinality",
    "cardinality_scope",
    "mapping_tool",
    "mapping_tool_id",
    "mapping_tool_version",
    "mapping_date",
    "publication_date",
    "review_date",
    "confidence",
    "reviewer_agreement",
    "curation_rule",
    "curation_rule_text",
    "subject_match_field",
    "object_match_field",
    "match_string",
    "subject_preprocessing",
    "object_preprocessing",
    "similarity_score",
    "similarity_measure",
    "see_also",
    "issue_tracker_item",
    "other",
    "comment",
)

_LEADING_COLUMNS = (
    "record_id",
    "subject_id",
    "subject_label",
    "subject_category",
    "predicate_id",
    "predicate_label",
    "predicate_modifier",
    "object_id",
    "object_label",
    "object_category",
    "mapping_justification",
)

_COLUMN_ORDER = _LEADING_COLUMNS + tuple(
    column for column in _MAPPING_COLUMNS if column not in _LEADING_COLUMNS
)

_REQUIRED_COLUMNS = (
    "subject_id",
    "predicate_id",
    "object_id",
    "mapping_justification",
)

_JUSTIFICATIONS = tuple(
    "semapv:" + name
    for name in (
        "MappingReview",
        "ManualMappingCuration",
        "LogicalReasoning",
        "LexicalMatching",
        "CompositeMatching",
        "UnspecifiedMatching",
        "SemanticSimilarityThresholdMatching",
        "LexicalSimilarityThresholdMatching",
        "MappingChaining",
        "MappingInversion",
        "StructuralMatching",
        "InstanceBasedMatching",
        "BackgroundKnowledgeBasedMatching",
    )
)

_CARDINALITIES = ("1:1", "1:n", "n:1", "n:n", "1:0", "0:1", "0:0")

# Columns whose (possibly pipe-separated) values must each be an absolute URI
# or a CURIE declared by the curie_map.
_REFERENCE_COLUMNS = (
    "record_id",
    "subject_id",
    "subject_category",
    "predicate_id",
    "object_id",
    "object_category",
    "mapping_justification",
    "author_id",
    "reviewer_id",
    "creator_id",
    "license",
    "subject_source",
    "object_source",
    "predicate_type",
    "mapping_provider",
    "mapping_source",
    "mapping_tool_id",
    "curation_rule",
    "subject_match_field",
    "object_match_field",
    "similarity_measure",
    "see_also",
    "issue_tracker_item",
)

_NO_TERM_FOUND = "sssom:NoTermFound"

_PREFIX_RE = re.compile(r"[A-Za-z_][A-Za-z0-9._-]*\Z")

# ``sssom.R`` writes ``[^[:space:]]`` and ``[[:space:]]`` in five validators
# (lines 374, 381, 382, 387 and 395 at v0.1.7) and calls ``grepl()`` WITHOUT
# ``perl = TRUE``, so TRE resolves those classes -- see ``metadata`` for the
# enumerated membership and the retirement condition. Python's ``\S`` was
# rejecting five codepoints era R accepts (U+00A0, U+0085, U+2007, U+202F,
# U+001C), which made an ``author_id`` R validated unreadable here.
_NOT_R_SPACE = "[^" + R_SPACE_CLASS + "]"
_ABSOLUTE_URI_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*:" + _NOT_R_SPACE + r"+\Z")
_SCHEME_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://" + _NOT_R_SPACE + r"+\Z")
_NON_HIERARCHICAL_URI_RE = re.compile(
    r"(urn|mailto|doi|tag|data):" + _NOT_R_SPACE + r"+\Z"
)
_CURIE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9._-]*:" + _NOT_R_SPACE + r"+\Z")
_R_SPACE_RE = re.compile("[" + R_SPACE_CLASS + "]")
_SAFE_FILENAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.sssom\.tsv\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_MANIFEST_PATH_RE = re.compile(
    r"metadata/semantic/[A-Za-z0-9][A-Za-z0-9._-]*\.sssom\.tsv\Z"
)
_UNSAFE_PATH_SEGMENT_RE = re.compile(r"(^|/)\.\.?(/|$)|\\")


@dataclass
class SssomMappingSet:
    """A parsed SSSOM 1.1 mapping set (metadata, mappings table, source path).

    Mirrors metasalmon's ``metasalmon_sssom_mapping_set`` list: ``metadata``
    is a dict whose ``curie_map`` value is a prefix→URI dict sorted by
    prefix; ``mappings`` is a string-valued DataFrame; ``path`` is the
    normalized source path, or ``None`` for in-memory sets.
    """

    metadata: Dict[str, object]
    mappings: pd.DataFrame = field(repr=False)
    path: Optional[str] = None


def _is_missing(value: object) -> bool:
    """True for None/NaN cell values (the R ``NA`` analogue)."""
    return value is None or (isinstance(value, float) and value != value) or value is pd.NA


def _cell(value: object) -> Optional[str]:
    """Coerce a mapping cell to str, preserving missing values as None."""
    if _is_missing(value):
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        # Mirror R's as.character(TRUE) -> "TRUE" for in-memory frames.
        return "TRUE" if value else "FALSE"
    return str(value)


def _split_multivalued(value: str) -> List[str]:
    """Mirror ``strsplit(value, "|", fixed = TRUE)[[1]]``.

    ``base::strsplit`` drops **exactly one** trailing empty field, so R reads
    ``"psc:PSC-CV-000900|"`` as a single well-formed reference. Python's
    ``str.split`` keeps that empty piece, which made this validator reject
    SDPs that R had written and accepted. Leading and interior empties survive
    in both implementations and must still be rejected, and ``""`` yields no
    pieces at all rather than one empty piece.
    """
    pieces = value.split("|")
    if pieces and pieces[-1] == "":
        pieces.pop()
    return pieces


def _scalar_metadata(value: object, name: str) -> str:
    """Mirror ``.ms_sssom_scalar``: one non-empty, trimmed string."""
    if value is None:
        raise ValueError(
            f"SSSOM metadata field {name} must contain one non-empty value."
        )
    text = str(value).strip()
    if not text:
        raise ValueError(
            f"SSSOM metadata field {name} must contain one non-empty value."
        )
    return text


def _read_bytes(path: Union[str, Path], label: str = "SSSOM mapping set") -> bytes:
    """Mirror ``.ms_sssom_read_bytes``: strict byte-level input contract.

    Checks run in the same order as R so the same defect reports the same
    failure: existence, emptiness, UTF-8 BOM, carriage returns, NUL bytes,
    trailing LF, UTF-8 validity.
    """
    path = Path(path)
    if not path.exists() or path.is_dir():
        raise FileNotFoundError(f"{label} does not exist at {path}.")
    data = path.read_bytes()
    if len(data) == 0:
        raise ValueError(f"{label} at {path} is empty.")
    if data[:3] == b"\xef\xbb\xbf":
        raise ValueError(f"{label} at {path} must not contain a UTF-8 BOM.")
    if b"\r" in data:
        raise ValueError(
            f"{label} at {path} must use LF line endings without carriage returns."
        )
    if b"\x00" in data:
        raise ValueError(f"{label} at {path} contains a NUL byte.")
    if data[-1:] != b"\n":
        raise ValueError(f"{label} at {path} must end with an LF newline.")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"{label} at {path} is not valid UTF-8.") from None
    return data


# --- restricted YAML-subset parsing for the embedded metadata header --------


def _parse_scalar(text: str, fail) -> str:
    """Parse one scalar value: JSON/double-quoted, single-quoted, or plain.

    The canonical writer emits JSON-encoded scalars (a YAML subset), and
    hand-authored headers use plain scalars; both round-trip here.
    """
    text = text.strip()
    if text.startswith('"'):
        try:
            value = json.loads(text)
        except ValueError:
            fail(f"malformed double-quoted scalar {text!r}")
        if not isinstance(value, str):
            fail(f"malformed double-quoted scalar {text!r}")
        return value
    if text.startswith("'"):
        if len(text) < 2 or not text.endswith("'"):
            fail(f"malformed single-quoted scalar {text!r}")
        return text[1:-1].replace("''", "'")
    # A plain scalar ends at a whitespace-preceded "#" (a YAML comment);
    # a "#" glued to text (e.g. an IRI fragment) is part of the value.
    return re.split(r"\s+#", text, maxsplit=1)[0].strip()


def _split_key_line(line: str, fail):
    """Split ``key: value`` (or ``key:``) or return None if not that shape."""
    match = re.match(r"([^\s:]+):(.*)\Z", line)
    if match is None:
        return None
    rest = match.group(2)
    if rest and not rest.startswith((" ", "\t")):
        fail(f"missing space after ':' in {line!r}")
    return match.group(1), rest.strip()


def _parse_yaml_subset(lines: Sequence[str], path: object) -> Dict[str, object]:
    """Parse the comment-stripped metadata header lines.

    Supports the restricted subset the SDP profile uses: a top-level block
    mapping of scalars, plus one level of nested block mappings (curie_map)
    and block sequences (multivalued fields). Anything else raises the same
    "not valid YAML" report R produces for a malformed header.
    """

    def fail(reason: str) -> None:
        raise ValueError(
            f"Embedded SSSOM metadata in {path} is not valid YAML: {reason}"
        )

    result: Dict[str, object] = {}
    index = 0
    total = len(lines)
    while index < total:
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        if line.startswith((" ", "\t")):
            fail(f"unexpected indentation at {line.strip()!r}")
        split = _split_key_line(line, fail)
        if split is None:
            fail(f"expected a 'key: value' mapping entry, got {line.strip()!r}")
        key, rest = split
        if key in result:
            fail(f"duplicate key {key!r}")
        index += 1
        if rest:
            result[key] = _parse_scalar(rest, fail)
            continue
        # Empty value: collect the indented block that follows, if any.
        block: List[str] = []
        while index < total and (
            not lines[index].strip() or lines[index].startswith(" ")
        ):
            if lines[index].strip():
                block.append(lines[index])
            index += 1
        if not block:
            result[key] = None
            continue
        indent = len(block[0]) - len(block[0].lstrip(" "))
        stripped = []
        for entry in block:
            if len(entry) - len(entry.lstrip(" ")) != indent:
                fail(f"inconsistent indentation under {key!r}")
            stripped.append(entry.strip())
        if all(entry.startswith("- ") or entry == "-" for entry in stripped):
            result[key] = [
                _parse_scalar(entry[1:], fail) for entry in stripped
            ]
            continue
        nested: Dict[str, str] = {}
        for entry in stripped:
            split = _split_key_line(entry, fail)
            if split is None or not split[1]:
                fail(f"expected 'key: value' entries under {key!r}")
            nested_key, nested_rest = split
            if nested_key in nested:
                fail(f"duplicate key {nested_key!r}")
            nested[nested_key] = _parse_scalar(nested_rest, fail)
        result[key] = nested
    if not result:
        raise ValueError(
            f"Embedded SSSOM metadata in {path} must be a named YAML mapping."
        )
    return result


def _parse_metadata(comment_lines: Sequence[str], path: object) -> Dict[str, object]:
    """Mirror ``.ms_sssom_parse_metadata``."""
    yaml_lines = [re.sub(r"^# ?", "", line, count=1) for line in comment_lines]
    metadata = _parse_yaml_subset(yaml_lines, path)

    unknown = [name for name in metadata if name not in _METADATA_ORDER]
    if unknown:
        raise ValueError(
            f"Embedded metadata in {path} contains unsupported SSSOM fields: "
            f"{', '.join(unknown)}."
        )
    missing = [name for name in _REQUIRED_METADATA if name not in metadata]
    if missing:
        raise ValueError(
            f"Embedded metadata in {path} is missing required fields: "
            f"{', '.join(missing)}."
        )

    for name in metadata:
        if name == "curie_map":
            continue
        value = metadata[name]
        if isinstance(value, dict):
            # Mirror R's unlist(): a nested mapping flattens to its values.
            value = "|".join(str(item) for item in value.values())
        elif isinstance(value, (list, tuple)):
            # Multivalued SSSOM/TSV metadata uses the same vertical-bar
            # encoding as propagated multivalued cells.
            value = "|".join(str(item) for item in value)
        metadata[name] = _scalar_metadata(value, name)

    curie_map = metadata["curie_map"]
    if not isinstance(curie_map, dict) or not curie_map:
        raise ValueError("SSSOM metadata curie_map must not be empty.")
    for prefix in curie_map:
        if not prefix or _PREFIX_RE.match(str(prefix)) is None:
            raise ValueError(
                "SSSOM metadata curie_map contains an invalid prefix name."
            )
    expansions = {
        str(prefix): str(value).strip() for prefix, value in curie_map.items()
    }
    if any(
        _ABSOLUTE_URI_RE.match(value) is None for value in expansions.values()
    ):
        raise ValueError(
            "Every SSSOM curie_map expansion must be an absolute URI."
        )
    metadata["curie_map"] = {
        prefix: expansions[prefix] for prefix in sorted(expansions)
    }
    return metadata


# --- TSV table parsing -------------------------------------------------------


def _parse_table(
    lines: Sequence[str], header_index: int, path: object
) -> pd.DataFrame:
    """Mirror ``.ms_sssom_parse_table``: strict tab-delimited body."""
    header = lines[header_index].split("\t")
    if len(header) < 2:
        raise ValueError(
            f"SSSOM mapping table in {path} must be tab-delimited."
        )
    if any(not name for name in header) or len(set(header)) != len(header):
        raise ValueError(
            f"SSSOM mapping table in {path} has blank or duplicate column names."
        )
    unknown = [name for name in header if name not in _MAPPING_COLUMNS]
    if unknown:
        raise ValueError(
            f"SSSOM mapping table in {path} contains unsupported columns: "
            f"{', '.join(unknown)}. Variable decomposition fields such as "
            "component_id belong in SDP semantic artifacts, not SSSOM."
        )
    missing = [name for name in _REQUIRED_COLUMNS if name not in header]
    if missing:
        raise ValueError(
            f"SSSOM mapping table in {path} is missing required columns: "
            f"{', '.join(missing)}."
        )

    data_lines = list(lines[header_index + 1 :])
    if any(not line for line in data_lines):
        raise ValueError(f"SSSOM mapping table in {path} contains a blank row.")
    if any(line.startswith("#") for line in data_lines):
        raise ValueError(
            "SSSOM comments are only allowed in embedded metadata before the "
            "TSV header."
        )
    if not data_lines:
        return pd.DataFrame({name: pd.Series(dtype=object) for name in header})

    rows = [line.split("\t") for line in data_lines]
    if any(len(row) != len(header) for row in rows):
        raise ValueError(
            f"Every row in the SSSOM mapping table at {path} must contain "
            f"{len(header) - 1} tab delimiters."
        )
    return pd.DataFrame(rows, columns=header, dtype=object)


# --- reference and profile validation ---------------------------------------


def _is_absolute_uri(value: object) -> bool:
    return isinstance(value, str) and _ABSOLUTE_URI_RE.match(value) is not None


def _is_unambiguous_uri(value: str) -> bool:
    # A colon alone is ambiguous between an RFC 3986 scheme and a CURIE
    # prefix. Treat network URLs and these common non-hierarchical URI
    # schemes as URIs; all other ``prefix:reference`` values must be declared
    # by curie_map.
    return (
        _SCHEME_URL_RE.match(value) is not None
        or _NON_HIERARCHICAL_URI_RE.match(value) is not None
    )


def _validate_reference(
    value: str,
    curie_map: Dict[str, str],
    field_name: str,
    row: Optional[int] = None,
) -> None:
    where = "" if row is None else f" in row {row}"
    if not value or _R_SPACE_RE.search(value):
        raise ValueError(
            f"SSSOM {field_name}{where} must be an absolute URI or compact CURIE."
        )
    if _is_unambiguous_uri(value):
        return
    if _CURIE_RE.match(value) is None:
        raise ValueError(
            f"SSSOM {field_name}{where} must be an absolute URI or compact CURIE."
        )
    prefix = value.split(":", 1)[0]
    if prefix not in curie_map:
        raise ValueError(
            f"SSSOM {field_name}{where} uses unknown CURIE prefix '{prefix}'."
        )


def _validate_metadata(metadata: Dict[str, object], path: object) -> None:
    """Mirror ``.ms_sssom_validate_metadata``."""
    if metadata.get("sssom_version") != SSSOM_VERSION:
        raise ValueError(
            f"SSSOM metadata in {path} must declare sssom_version: 1.1."
        )
    for field_name in ("mapping_set_id", "license"):
        if not _is_absolute_uri(metadata.get(field_name)):
            raise ValueError(
                f"SSSOM metadata {field_name} in {path} must be an absolute URI."
            )
    for field_name in ("subject_source", "object_source"):
        _validate_reference(
            str(metadata[field_name]), metadata["curie_map"], field_name
        )
    for field_name in ("subject_type", "object_type"):
        if field_name in metadata and re.search(
            "literal", str(metadata[field_name]), re.IGNORECASE
        ):
            raise ValueError(
                f"SSSOM {field_name} cannot declare a raw literal assignment "
                "in this SDP profile."
            )


def _column_values(mappings: pd.DataFrame, name: str) -> List[Optional[str]]:
    return [_cell(value) for value in mappings[name].tolist()]


def _validate_mappings(
    mappings: pd.DataFrame, metadata: Dict[str, object], path: object
) -> None:
    """Mirror ``.ms_sssom_validate_mappings`` — same checks, same order."""
    columns = {name: _column_values(mappings, name) for name in mappings.columns}
    row_count = len(mappings)

    for field_name in _REQUIRED_COLUMNS:
        if any(
            value is None or not value.strip() for value in columns[field_name]
        ):
            raise ValueError(
                f"SSSOM required column {field_name} contains a blank value "
                f"in {path}."
            )

    for field_name in ("subject_type", "object_type"):
        if field_name in columns and any(
            value is not None and re.search("literal", value, re.IGNORECASE)
            for value in columns[field_name]
        ):
            raise ValueError(
                f"SSSOM {field_name} cannot declare raw literal assignments "
                "in this SDP profile."
            )

    # Tabs and newlines are structural in embedded TSV. The parser has
    # already split tabs, while this catches other controls before
    # deterministic writing.
    for field_name, values in columns.items():
        if any(
            value is not None and re.search(r"[\t\r\n]", value)
            for value in values
        ):
            raise ValueError(
                f"SSSOM column {field_name} contains a forbidden control "
                "character."
            )

    curie_map = metadata["curie_map"]
    for field_name in _REFERENCE_COLUMNS:
        if field_name not in columns:
            continue
        for row, value in enumerate(columns[field_name], start=1):
            if value is None or not value:
                continue
            for piece in _split_multivalued(value):
                _validate_reference(piece, curie_map, field_name, row)

    for field_name, values in columns.items():
        if field_name in ("subject_id", "object_id"):
            continue
        if any(
            value is not None
            and value
            and _NO_TERM_FOUND in _split_multivalued(value)
            for value in values
        ):
            raise ValueError(
                f"'{_NO_TERM_FOUND}' is only valid in subject_id or object_id."
            )

    if any(
        value not in _JUSTIFICATIONS for value in columns["mapping_justification"]
    ):
        raise ValueError(
            "SSSOM mapping_justification must use a SSSOM 1.1 SEMAPV "
            "justification."
        )

    if "mapping_cardinality" in columns:
        cardinality = columns["mapping_cardinality"]
        if any(
            (value is None or value)  # nzchar(NA) is TRUE in R: NA is invalid
            and value not in _CARDINALITIES
            and (value is None or value != "")
            for value in cardinality
        ):
            raise ValueError(
                "SSSOM mapping_cardinality contains an invalid value."
            )
        cardinality = [value if value is not None else "" for value in cardinality]
    else:
        cardinality = [""] * row_count

    subject_id = columns["subject_id"]
    object_id = columns["object_id"]
    subject_gap = [value == _NO_TERM_FOUND for value in subject_id]
    object_gap = [value == _NO_TERM_FOUND for value in object_id]
    for row in range(row_count):
        if subject_gap[row] and object_gap[row]:
            expected: Optional[str] = "0:0"
        elif subject_gap[row]:
            expected = "0:1"
        elif object_gap[row]:
            expected = "1:0"
        else:
            expected = None
        if expected is not None and cardinality[row] != expected:
            raise ValueError(
                f"Mappings using '{_NO_TERM_FOUND}' must use the corresponding "
                "mapping_cardinality value (including '1:0' for an object gap)."
            )
        if expected is None and cardinality[row] in ("1:0", "0:1", "0:0"):
            raise ValueError(
                f"SSSOM zero-cardinality mappings must use '{_NO_TERM_FOUND}'."
            )
    if any(object_gap) and not metadata["object_source"]:
        raise ValueError(
            f"A '{_NO_TERM_FOUND}' object requires object_source."
        )
    if any(subject_gap) and not metadata["subject_source"]:
        raise ValueError(
            f"A '{_NO_TERM_FOUND}' subject requires subject_source."
        )

    # Metadata source and version values propagate to every row. A row-level
    # source override, however, needs its own version because the mapping-set
    # version cannot describe a different vocabulary release.
    effective_source = [str(metadata["object_source"])] * row_count
    effective_version = [str(metadata["object_source_version"])] * row_count
    if "object_source" in columns:
        for row, value in enumerate(columns["object_source"]):
            if value is not None and value:
                effective_source[row] = value
                if value != metadata["object_source"]:
                    effective_version[row] = ""
    if "object_source_version" in columns:
        for row, value in enumerate(columns["object_source_version"]):
            if value is not None and value:
                effective_version[row] = value
    if any(
        object_gap[row]
        and (not effective_source[row] or not effective_version[row])
        for row in range(row_count)
    ):
        raise ValueError(
            f"A '{_NO_TERM_FOUND}' object requires an effective object_source "
            "and object_source_version."
        )

    # A 1:0 row asserts that the subject has no term in the target source. It
    # is contradictory to carry a positive mapping for that subject and
    # target source in the same mapping set, even when the two rows use
    # different SKOS predicates.
    scope_key = [
        "\x1f".join(
            (subject_id[row] or "", effective_source[row], effective_version[row])
        )
        for row in range(row_count)
    ]
    gap_scopes = {scope_key[row] for row in range(row_count) if object_gap[row]}
    positive_scopes = {
        scope_key[row] for row in range(row_count) if not object_gap[row]
    }
    if gap_scopes & positive_scopes:
        raise ValueError(
            "A subject/object-source scope cannot contain both a positive "
            f"mapping and '{_NO_TERM_FOUND}'; the records contradict each other."
        )

    identities = [
        "\x1f".join(
            (
                subject_id[row] or "",
                columns["predicate_id"][row] or "",
                object_id[row] or "",
            )
        )
        for row in range(row_count)
    ]
    if len(set(identities)) != len(identities):
        raise ValueError(
            f"SSSOM mapping set at {path} contains a duplicate "
            "subject/predicate/object mapping."
        )


def _validate_mapping_set(mapping_set: SssomMappingSet) -> None:
    path = mapping_set.path or "<in-memory mapping set>"
    _validate_metadata(mapping_set.metadata, path)
    _validate_mappings(mapping_set.mappings, mapping_set.metadata, path)


# --- reading -----------------------------------------------------------------


def read_sssom_mapping_set(
    path: Union[str, Path], validate: bool = True
) -> SssomMappingSet:
    """Read a reviewed SSSOM mapping set.

    Reads the SSSOM 1.1 embedded-TSV serialization used by Salmon Data
    Packages. The reader enforces UTF-8 without a byte-order mark, LF line
    endings, tab delimiters, complete CURIE declarations, and the package's
    alignment-only profile. In particular, decomposition fields and raw
    literal assignments are refused because they belong in separate SDP
    semantic artifacts.

    Parameters
    ----------
    path:
        Path to one ``.sssom.tsv`` file.
    validate:
        Validate metadata, CURIEs, mappings, and no-match cardinalities after
        parsing. The byte and table structure is always checked.

    Returns
    -------
    SssomMappingSet
        ``metadata`` dict, ``mappings`` DataFrame, and the normalized source
        ``path``.
    """
    if isinstance(path, (list, tuple)) or path is None or not str(path):
        raise ValueError("path must name one SSSOM mapping-set file.")
    if not isinstance(validate, bool):
        raise ValueError("validate must be True or False.")
    resolved = Path(path)
    data = _read_bytes(resolved)
    resolved = resolved.resolve()
    text = data.decode("utf-8")
    # The byte validator already requires the terminal LF. Remove that one
    # structural character before splitting so a one-row table cannot be
    # mistaken for a two-row table by split()'s trailing-empty rules.
    lines = text[:-1].split("\n")
    # R's strsplit() omits exactly one trailing empty field, so a single
    # extra blank line at the end of the file is tolerated there (two are
    # not). Mirror that quirk precisely: parity beats strictness here.
    if lines and lines[-1] == "":
        lines.pop()

    non_comment = [
        index
        for index, line in enumerate(lines)
        if line and not line.startswith("#")
    ]
    if not non_comment:
        raise ValueError(f"SSSOM file {resolved} does not contain a TSV header.")
    header_index = non_comment[0]
    if header_index == 0:
        raise ValueError(
            f"SSSOM file {resolved} must begin with embedded YAML metadata "
            "comments."
        )
    comment_lines = [line for line in lines[:header_index] if line]
    metadata = _parse_metadata(comment_lines, resolved)
    mappings = _parse_table(lines, header_index, resolved)

    result = SssomMappingSet(
        metadata=metadata, mappings=mappings, path=str(resolved)
    )
    if validate:
        _validate_mapping_set(result)
    return result


def _normalize_in_memory(mapping_set: object) -> SssomMappingSet:
    """Mirror ``.ms_sssom_normalize_in_memory`` for dicts and dataclasses."""
    if isinstance(mapping_set, SssomMappingSet):
        metadata = mapping_set.metadata
        mappings = mapping_set.mappings
        path = mapping_set.path
    elif isinstance(mapping_set, dict) and {"metadata", "mappings"} <= set(
        mapping_set
    ):
        metadata = mapping_set["metadata"]
        mappings = mapping_set["mappings"]
        path = mapping_set.get("path")
    else:
        raise ValueError(
            "Each mapping_sets entry must be a path or a parsed SSSOM "
            "mapping set."
        )
    normalized = SssomMappingSet(
        metadata=metadata,
        mappings=pd.DataFrame(mappings),
        path=path if isinstance(path, str) and path else None,
    )
    _validate_mapping_set(normalized)
    return normalized


def _input_sets(mapping_sets: object) -> List[SssomMappingSet]:
    """Mirror ``.ms_sssom_input_sets``: paths, parsed sets, or a mix."""
    if isinstance(mapping_sets, (str, Path)):
        return [read_sssom_mapping_set(mapping_sets)]
    if isinstance(mapping_sets, SssomMappingSet) or (
        isinstance(mapping_sets, dict)
        and {"metadata", "mappings"} <= set(mapping_sets)
    ):
        return [_normalize_in_memory(mapping_sets)]
    if isinstance(mapping_sets, (list, tuple)):
        return [
            read_sssom_mapping_set(entry)
            if isinstance(entry, (str, Path))
            else _normalize_in_memory(entry)
            for entry in mapping_sets
        ]
    raise ValueError(
        "mapping_sets must be None, path(s), or parsed SSSOM mapping set(s)."
    )


# --- canonical serialization --------------------------------------------------


def _json_scalar(value: object) -> str:
    """Mirror ``.ms_sssom_json_scalar`` (jsonlite auto-unboxed string)."""
    return json.dumps(str(value), ensure_ascii=False)


def _canonical_bytes(mapping_set: SssomMappingSet) -> bytes:
    """Mirror ``.ms_sssom_canonical_bytes`` byte for byte.

    Deterministic UTF-8, LF-only, trailing-LF TSV: metadata comments in the
    fixed field order with JSON-encoded scalars, curie-map lines sorted by
    prefix, canonical column order, and rows sorted as tuples of column
    values (missing values sort last within each column, then serialize as
    empty fields). ``sorted()`` on ``str`` matches R's radix (C-locale)
    order because UTF-8 byte order equals code-point order.
    """
    metadata = mapping_set.metadata
    curie_map = metadata["curie_map"]

    metadata_lines: List[str] = []
    for field_name in _METADATA_ORDER:
        if field_name not in metadata:
            continue
        if field_name == "curie_map":
            metadata_lines.append("# curie_map:")
            for prefix in sorted(curie_map):
                metadata_lines.append(
                    f"#   {prefix}: {_json_scalar(curie_map[prefix])}"
                )
        else:
            metadata_lines.append(
                f"# {field_name}: {_json_scalar(metadata[field_name])}"
            )

    columns = [
        name for name in _COLUMN_ORDER if name in mapping_set.mappings.columns
    ]
    cells = {name: _column_values(mapping_set.mappings, name) for name in columns}
    row_count = len(mapping_set.mappings)
    order = sorted(
        range(row_count),
        key=lambda row: tuple(
            (1, "") if cells[name][row] is None else (0, cells[name][row])
            for name in columns
        ),
    )

    table_lines = ["\t".join(columns)]
    for row in order:
        table_lines.append(
            "\t".join(
                "" if cells[name][row] is None else cells[name][row]
                for name in columns
            )
        )
    return ("\n".join(metadata_lines + table_lines) + "\n").encode("utf-8")


def _safe_filename(mapping_set: SssomMappingSet) -> str:
    """Mirror ``.ms_sssom_safe_filename``."""
    if mapping_set.path:
        candidate = os.path.basename(mapping_set.path)
        if _SAFE_FILENAME_RE.match(candidate) is not None:
            return candidate

    mapping_set_id = str(mapping_set.metadata["mapping_set_id"])
    candidate = re.sub(r"^.*[/#]", "", mapping_set_id)
    candidate = re.sub(r"[^A-Za-z0-9._-]+", "-", candidate)
    candidate = re.sub(r"^-+|-+$", "", candidate)
    if not candidate:
        digest = hashlib.sha256(mapping_set_id.encode("utf-8")).hexdigest()
        candidate = f"mapping-set-{digest[:12]}"
    return f"{candidate}.sssom.tsv"


def _atomic_write(data: bytes, path: Path) -> None:
    """Mirror ``.ms_sssom_atomic_write``: write-then-rename in place.

    The shared helper restores the umask-default mode that R's ``writeBin``
    would have produced; ``tempfile.mkstemp`` would otherwise publish the
    mapping set as 0600.
    """
    atomic_write(data, path)


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


def _manifest_bytes(entries: List[Dict[str, object]]) -> bytes:
    """Build ``metadata/semantic/mapping-sets.json`` bytes.

    Same structure and field order as R's ``.ms_sssom_manifest_bytes``; the
    provenance block honestly names this implementation (PARITY.md entry 11),
    so manifest bytes differ from R's only in the provenance values.
    """
    manifest = {
        "schema_version": SSSOM_MANIFEST_VERSION,
        "sssom_version": SSSOM_VERSION,
        "mapping_sets": entries,
        "provenance": {
            "generated_by": "metasalmonpy.write_sdp_sssom",
            "metasalmonpy_version": _package_version(),
            "specification": "https://mapping-commons.github.io/sssom/1.1/",
        },
    }
    return (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def _assert_contained(root: Path, candidate: Path, label: str) -> Path:
    """Mirror ``.ms_sssom_assert_contained`` using resolved real paths."""
    real_root = Path(os.path.realpath(str(root)))
    real_candidate = Path(os.path.realpath(str(candidate)))
    if real_candidate != real_root and real_root not in real_candidate.parents:
        raise ValueError(f"{label} resolves outside the SDP root and is unsafe.")
    return real_candidate


# --- writing -----------------------------------------------------------------


def write_sdp_sssom(
    path: Union[str, Path],
    mapping_sets: object = None,
    overwrite: bool = False,
) -> Optional[str]:
    """Write reviewed SSSOM mapping sets into a Salmon Data Package.

    Writes explicitly supplied SSSOM 1.1 mapping sets under
    ``metadata/semantic/`` and records their paths, hashes, row counts,
    source versions, licenses, and writer provenance in
    ``metadata/semantic/mapping-sets.json``. Bytes and manifest ordering are
    deterministic. This function does not turn semantic suggestions or
    variable decompositions into mappings; ``mapping_sets=None`` is therefore
    a no-op.

    Parameters
    ----------
    path:
        Existing Salmon Data Package directory.
    mapping_sets:
        ``None``, one or more paths to reviewed ``.sssom.tsv`` files, or
        parsed sets returned by :func:`read_sssom_mapping_set`.
    overwrite:
        Replace files managed by this writer when ``True``.

    Returns
    -------
    Optional[str]
        The manifest path, or ``None`` when ``mapping_sets`` is ``None``.
    """
    if mapping_sets is None:
        return None
    if isinstance(path, (list, tuple)) or path is None or not Path(path).is_dir():
        raise ValueError("path must be an existing SDP directory.")
    if not isinstance(overwrite, bool):
        raise ValueError("overwrite must be True or False.")
    root = Path(path).resolve()
    sets = _input_sets(mapping_sets)
    if not sets:
        return None

    ids = [str(entry.metadata["mapping_set_id"]) for entry in sets]
    if len(set(ids)) != len(ids):
        raise ValueError(
            "mapping_sets contains duplicate mapping_set_id values."
        )
    sets = [entry for _, entry in sorted(zip(ids, sets), key=lambda pair: pair[0])]

    filenames = [_safe_filename(entry) for entry in sets]
    if len(set(filenames)) != len(filenames):
        raise ValueError(
            "mapping_sets resolves to duplicate output filenames; use "
            "distinct safe source filenames."
        )
    if any(_SAFE_FILENAME_RE.match(name) is None for name in filenames):
        raise ValueError("A generated SSSOM output filename is unsafe.")

    payloads = [_canonical_bytes(entry) for entry in sets]
    entries: List[Dict[str, object]] = []
    for index, entry in enumerate(sets):
        metadata = entry.metadata
        entries.append(
            {
                "path": f"metadata/semantic/{filenames[index]}",
                "sha256": hashlib.sha256(payloads[index]).hexdigest(),
                "row_count": int(len(entry.mappings)),
                "mapping_set_id": metadata["mapping_set_id"],
                "mapping_set_version": metadata["mapping_set_version"],
                "license": metadata["license"],
                "subject_source": metadata["subject_source"],
                "subject_source_version": metadata["subject_source_version"],
                "object_source": metadata["object_source"],
                "object_source_version": metadata["object_source_version"],
            }
        )
    manifest_payload = _manifest_bytes(entries)

    semantic_directory = root / "metadata" / "semantic"
    manifest_path = semantic_directory / "mapping-sets.json"
    output_paths = [semantic_directory / name for name in filenames]
    managed_paths = output_paths + [manifest_path]
    existing = [
        candidate
        for candidate in managed_paths
        if candidate.exists() or candidate.is_symlink()
    ]
    if existing and not overwrite:
        raise FileExistsError(
            "SSSOM output already exists and overwrite is False. Existing: "
            + ", ".join(str(candidate) for candidate in existing)
            + "."
        )
    symlinks = [candidate for candidate in existing if candidate.is_symlink()]
    if symlinks:
        raise ValueError(
            "Refusing to overwrite SSSOM symlinks: "
            + ", ".join(str(candidate) for candidate in symlinks)
            + "."
        )

    semantic_directory.mkdir(parents=True, exist_ok=True)
    _assert_contained(root, semantic_directory, "SSSOM output directory")
    for index, output_path in enumerate(output_paths):
        _atomic_write(payloads[index], output_path)
    _atomic_write(manifest_payload, manifest_path)

    # Read back the exact artifacts rather than trusting an in-memory plan.
    validate_sdp_sssom(root)
    return str(manifest_path)


# --- SDP-level validation ------------------------------------------------------


def _manifest_safe_path(value: object) -> bool:
    return (
        isinstance(value, str)
        and _MANIFEST_PATH_RE.match(value) is not None
        and _UNSAFE_PATH_SEGMENT_RE.search(value) is None
    )


# The accepted writer set has one owner (``provenance.py``); see the note
# there and ``tests/test_provenance.py``.
_MANIFEST_WRITER = "write_sdp_sssom"


def _validate_manifest(root: Path) -> None:
    """Mirror ``.ms_sssom_validate_manifest``.

    The provenance check accepts artifacts written by either mirror
    implementation (PARITY.md entry 11): R's writer stamps
    ``metasalmon::write_sdp_sssom`` + ``metasalmon_version``; this writer
    stamps ``metasalmonpy.write_sdp_sssom`` + ``metasalmonpy_version``.
    """
    manifest_path = root / "metadata" / "semantic" / "mapping-sets.json"
    data = _read_bytes(manifest_path, "SSSOM manifest")
    try:
        manifest = json.loads(data.decode("utf-8"))
    except ValueError as error:
        raise ValueError(
            f"SSSOM manifest at {manifest_path} is not valid JSON: {error}"
        ) from None
    required_top = ("schema_version", "sssom_version", "mapping_sets", "provenance")
    if not isinstance(manifest, dict) or any(
        name not in manifest for name in required_top
    ):
        raise ValueError("SSSOM manifest is missing required top-level fields.")
    if (
        manifest["schema_version"] != SSSOM_MANIFEST_VERSION
        or manifest["sssom_version"] != SSSOM_VERSION
    ):
        raise ValueError(
            "SSSOM manifest declares an unsupported schema or SSSOM version."
        )
    provenance = manifest["provenance"]
    version_key = _provenance.version_field(provenance, _MANIFEST_WRITER)
    # Presence only, deliberately: metasalmon's SSSOM validator asks the
    # same weaker question, and the two readers of one artifact must accept
    # the same manifests. Both sides tighten together or neither does
    # (``provenance.version_ok``'s retirement condition).
    if version_key is None or provenance.get(version_key) is None:
        raise ValueError("SSSOM manifest provenance is incomplete.")
    if not isinstance(manifest["mapping_sets"], list) or not manifest["mapping_sets"]:
        raise ValueError("SSSOM manifest must contain at least one mapping set.")

    required_entry = (
        "path",
        "sha256",
        "row_count",
        "mapping_set_id",
        "mapping_set_version",
        "license",
        "subject_source",
        "subject_source_version",
        "object_source",
        "object_source_version",
    )
    paths: List[str] = []
    ids: List[str] = []
    for index, entry in enumerate(manifest["mapping_sets"], start=1):
        if not isinstance(entry, dict) or any(
            name not in entry for name in required_entry
        ):
            raise ValueError(
                f"SSSOM manifest mapping-set entry {index} is incomplete."
            )
        if not _manifest_safe_path(entry["path"]):
            raise ValueError(
                f"SSSOM manifest entry {index} does not use a safe relative "
                "mapping-set path."
            )
        paths.append(entry["path"])
        ids.append(entry["mapping_set_id"])
        mapping_path = root / entry["path"]
        if not mapping_path.exists() or mapping_path.is_dir():
            raise FileNotFoundError(
                f"SSSOM manifest references missing file {mapping_path}."
            )
        _assert_contained(root, mapping_path, "SSSOM mapping-set path")
        file_bytes = _read_bytes(mapping_path)
        actual_sha256 = hashlib.sha256(file_bytes).hexdigest()
        if (
            not isinstance(entry["sha256"], str)
            or _SHA256_RE.match(entry["sha256"]) is None
            or actual_sha256 != entry["sha256"]
        ):
            raise ValueError(
                f"SSSOM mapping set {mapping_path} does not match its "
                "manifest SHA-256 hash."
            )

        mapping_set = read_sssom_mapping_set(mapping_path)
        row_count = entry["row_count"]
        if (
            isinstance(row_count, bool)
            or not isinstance(row_count, (int, float))
            or row_count != int(row_count)
            or int(row_count) < 0
            or int(row_count) != len(mapping_set.mappings)
        ):
            raise ValueError(
                f"SSSOM mapping set {mapping_path} does not match its "
                "manifest row count."
            )
        for field_name in required_entry[3:]:
            if entry[field_name] != mapping_set.metadata.get(field_name):
                raise ValueError(
                    f"SSSOM manifest field {field_name} does not match "
                    f"{mapping_path}."
                )
    if len(set(paths)) != len(paths) or len(set(ids)) != len(ids):
        raise ValueError(
            "SSSOM manifest contains duplicate paths or mapping_set_id values."
        )
    if ids != sorted(ids):
        raise ValueError(
            "SSSOM manifest mapping sets must be ordered by mapping_set_id."
        )


def validate_sdp_sssom(path: Union[str, Path]) -> bool:
    """Validate SDP SSSOM artifacts.

    Validates either one SSSOM 1.1 embedded-TSV file or an SDP directory.
    For an SDP directory, the function validates
    ``metadata/semantic/mapping-sets.json``, safe relative paths, byte
    hashes, row counts, metadata provenance, and every referenced mapping
    set.

    Parameters
    ----------
    path:
        Path to an SDP directory or one ``.sssom.tsv`` mapping set.

    Returns
    -------
    bool
        ``True`` when validation succeeds; otherwise an exception is raised.
    """
    if isinstance(path, (list, tuple)) or path is None or not str(path):
        raise ValueError("path must name one SDP directory or SSSOM file.")
    target = Path(path)
    if target.is_dir():
        _validate_manifest(target.resolve())
    else:
        read_sssom_mapping_set(target, validate=True)
    return True
