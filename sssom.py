"""SSSOM mapping-set support (mirrors metasalmon's ``R/sssom.R``).

A Salmon Data Package may carry reviewed vocabulary alignments, but those
alignments are not another representation of its variable decompositions.
This module therefore implements a deliberately small, strict SSSOM 1.1
profile: approved mapping sets go in; no mappings are inferred from semantic
suggestions, dictionary literals, or component columns.

Byte-parity contract: ``_canonical_bytes`` must produce output byte-identical
to metasalmon's ``.ms_sssom_canonical_bytes`` for the same mapping set: the
canonical SSSOM/TSV format (hub B-350 there, B-351 here), as deterministic
UTF-8, LF-only, trailing-LF bytes with radix-sorted (C-collation) curie-map
lines and mapping rows. Python's default ``sorted()`` on ``str`` compares
Unicode code points, which matches R's radix (C locale, UTF-8 byte) order for
all of Unicode, so no locale machinery is needed and ``locale.strxfrm`` stays
banned.

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
from collections.abc import Mapping
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
)

# The order of the MappingSet class's slots in the SSSOM schema, which is the
# order of the "Slots" table the canonical SSSOM/TSV format writes them in, so
# ``curie_map`` is second. Source: ``classes: mapping set: slots:`` in
# https://github.com/mapping-commons/sssom/blob/667d3c579d92ad2e1a480503625eeef1e6af8e6d/src/sssom_schema/schema/sssom_schema.yaml
# (``mappings`` and ``extension_definitions`` are left out: the first is the
# TSV table, and this profile supports no extension slots). Mirrors
# ``.ms_sssom_metadata_order``; hub B-351, the mirror half of B-350.
_METADATA_ORDER = (
    "sssom_version",
    "curie_map",
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
)

# The MappingSet slots the same schema marks ``multivalued``. In memory each
# holds one string with its values joined by ``|``, the encoding the reader
# gives a YAML sequence; the canonical writer turns it back into a block
# sequence. Mirrors ``.ms_sssom_multivalued_metadata``.
_MULTIVALUED_METADATA = (
    "mapping_set_source",
    "creator_id",
    "creator_label",
    "cardinality_scope",
    "subject_match_field",
    "object_match_field",
    "subject_preprocessing",
    "object_preprocessing",
    "curation_rule",
    "curation_rule_text",
    "see_also",
)

# The Mapping slots whose schema range is ``double``. The canonical format
# writes a floating point value "with up to three digits as needed after the
# decimal point, rounding the last digit to the nearest neighbour (rounding up
# if both neighbours are equidistant)". Mirrors ``.ms_sssom_double_columns``.
_DOUBLE_COLUMNS = ("confidence", "reviewer_agreement", "similarity_score")

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

# The SSSOM built-in prefixes, copied in order from the table in the
# specification's IRI prefixes section
# (https://mapping-commons.github.io/sssom/1.0/spec-intro/#iri-prefixes; the
# 1.1 draft at https://mapping-commons.github.io/sssom/dev/spec-intro/ has the
# same table). The model's Identifiers section says what they allow: "By
# exception, prefix names listed in the table found in the IRI prefixes section
# are considered 'built-in'. As such, they MAY be omitted from the curie_map. If
# they are not omitted, they MUST point to the same IRI prefixes as in the
# aforementioned table." So a set may use these without declaring them, and may
# not declare them with any other expansion. Every other prefix still has to be
# declared: SSSOM/TSV parsers "MUST reject a file with undeclared, non-built-in
# prefix names". The prefixes block of the SSSOM LinkML schema is a different
# list and is not this one. Mirrors ``.ms_sssom_builtin_prefixes`` in
# metasalmon's ``R/sssom.R``; hub B-234, the mirror half of B-233.
_BUILTIN_PREFIXES = {
    "owl": "http://www.w3.org/2002/07/owl#",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "semapv": "https://w3id.org/semapv/vocab/",
    "skos": "http://www.w3.org/2004/02/skos/core#",
    "sssom": "https://w3id.org/sssom/",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
    "linkml": "https://w3id.org/linkml/",
}

# Columns whose (possibly pipe-separated) values must each be an absolute URI
# or a CURIE whose prefix the curie_map declares or SSSOM builds in.
_REFERENCE_COLUMNS = (
    "record_id",
    "subject_id",
    "predicate_id",
    "object_id",
    "mapping_justification",
    "author_id",
    "reviewer_id",
    "creator_id",
    "license",
    "subject_source",
    "object_source",
    "mapping_provider",
    "mapping_source",
    "mapping_tool_id",
    "curation_rule",
    "subject_match_field",
    "object_match_field",
    "see_also",
    "issue_tracker_item",
)

# Mirror R B-269's pinned SSSOM 1.1 entity_type_enum. The schema forbids
# ``rdfs literal`` and ``composed entity expression`` in predicate_type.
# Categories and similarity_measure have string ranges, so they are not
# reference columns and need no prefix declaration. Update this set only when
# this profile adopts a schema whose enum changes.
# https://github.com/mapping-commons/sssom/blob/667d3c579d92ad2e1a480503625eeef1e6af8e6d/src/sssom_schema/schema/sssom_schema.yaml
_PREDICATE_TYPES = (
    "owl class", "owl object property", "owl data property",
    "owl annotation property", "owl named individual", "skos concept",
    "rdfs resource", "rdfs class", "rdfs datatype", "rdf property",
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
    prefix, empty for a file that declares none; ``mappings`` is a
    string-valued DataFrame; ``path`` is the normalized source path, or
    ``None`` for in-memory sets.
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

def _yaml_quoted_nodes(text: str):
    """Yield quoted-node spans; quotes inside a plain scalar stay plain text.

    This only tracks node boundaries for masking, not YAML values or types.
    A quote may start a node after a collection delimiter, mapping separator
    or anchor. An apostrophe in ``a'b`` cannot open a quoted node.
    """
    index, flow_depth = 0, 0
    node_start, after_quote = True, False
    while index < len(text):
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if node_start and char in ('"', "'"):
            end = index + 1
            while end < len(text):
                if char == '"' and text[end] == "\\":
                    end += 2
                elif char == "'" and text[end:end + 2] == "''":
                    end += 2
                elif text[end] == char:
                    break
                else:
                    end += 1
            else:
                return
            yield index, end + 1
            index, node_start, after_quote = end + 1, False, True
            continue
        separated = index + 1 == len(text) or text[index + 1].isspace()
        if char in "[{" and node_start:
            flow_depth += 1
            node_start = True
        elif char in "}]" and flow_depth:
            flow_depth -= 1
            node_start = False
        elif char == "," and flow_depth:
            node_start = True
        elif char == ":" and (after_quote or separated):
            node_start = True
        elif node_start and char in "-?" and separated:
            pass
        elif node_start and char == "&":
            anchor = re.match(r"&[^\s\[\]{},]+", text[index:])
            if anchor is not None:
                index += anchor.end()
                continue
            node_start = False
        else:
            node_start = False
        index += 1
        after_quote = False


def _strip_yaml_comment(text: str) -> str:
    """Exclude a separated comment, without treating a quoted hash as one."""
    quoted = list(_yaml_quoted_nodes(text))
    for comment in re.finditer(r"\s+#", text):
        if not any(start <= comment.end() - 1 < end for start, end in quoted):
            return text[:comment.start()].strip()
    return text


def _scalar_has_yaml_tag(text: str) -> bool:
    """Detect explicit node tags without loading or resolving YAML values.

    Node properties may put an anchor before a tag (YAML 1.2 section 6.9).
    Flow values are left as text by this restricted reader, but must not hide
    tags that a YAML reader would resolve. Quoted exclamations and exclamations
    inside a plain scalar are text, so only node-property positions count.
    """
    anchor = r"&[^\s\[\]{},]+\s+"
    content = text
    while True:
        # A block-sequence item can itself be a compact mapping (key: value)
        # or an explicit key (? node). Inspect its value without interpreting
        # ordinary embedded exclamations such as "Good !foo title" as tags.
        content = re.sub(r"\A(?:[-?]\s+)*(?:" + anchor + r")?", "", content, count=1)
        if content.startswith("!"):
            return True
        if content.startswith(("[", "{")):
            break
        if content.startswith(('"', "'")):
            return False
        value_start = re.search(r":\s+", content)
        if value_start is None:
            return False
        content = content[value_start.end():]
    # Mask complete quoted nodes first. Keep a placeholder for quoted mapping
    # keys, whose colon may directly precede a tagged value in flow syntax.
    fragments, previous = [], 0
    for start, end in _yaml_quoted_nodes(content):
        fragments.extend((content[previous:start], '""'))
        previous = end
    masked = "".join((*fragments, content[previous:]))
    return re.search(
        r'(?:\A|[\[{,]\s*|:\s+|""\s*:\s*)(?:\?\s+)?(?:' + anchor + r")?!",
        masked,
    ) is not None


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
    # Comments are presentation, not nodes (YAML 1.2 section 6.6). Exclude
    # them before the refusal guard as well as from the returned plain value.
    text = _strip_yaml_comment(text)
    if _scalar_has_yaml_tag(text):
        fail("explicit YAML tags are not supported in SSSOM metadata")
    return text


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

    # A canonical writer leaves out every built-in and every unused prefix, so
    # a set that uses only built-in prefixes is written with no curie_map at
    # all. The slot is optional in the SSSOM model; an absent one is an empty
    # map. (A ``curie_map:`` key with nothing under it is the same absence:
    # the subset parser gives it ``None``, as R's yaml gives it NULL.)
    if metadata.get("curie_map") is None:
        metadata["curie_map"] = {}
        return metadata
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


def _unquote_cell(value: Optional[str]) -> Optional[str]:
    """Mirror ``.ms_sssom_unquote_cell``: strip well-formed RFC 4180 quoting.

    The SSSOM/TSV Quoting section: "SSSOM/TSV parsers MUST strip any enclosing
    double quotes and escaping double quotes". A cell is decoded only when it
    is a well-formed quoted value, opening and closing with ``"`` and with
    every inner ``"`` doubled. Anything else is kept byte for byte, so a cell
    an earlier writer of this package emitted with a bare ``"`` inside it
    reads as it always did. Quoted tabs and line breaks are not supported: the
    table is split on them first, and validation refuses them in a cell.
    """
    if (
        value is None
        or len(value) < 2
        or not (value.startswith('"') and value.endswith('"'))
    ):
        return value
    inner = value[1:-1]
    if '"' in inner.replace('""', ""):
        return value
    return inner.replace('""', '"')


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
    rows = [[_unquote_cell(cell) for cell in row] for row in rows]
    return pd.DataFrame(rows, columns=header, dtype=object)


# --- reference and profile validation ---------------------------------------


def _is_absolute_uri(value: object) -> bool:
    return isinstance(value, str) and _ABSOLUTE_URI_RE.match(value) is not None


def _is_unambiguous_uri(value: str) -> bool:
    # A colon alone is ambiguous between an RFC 3986 scheme and a CURIE
    # prefix. Treat network URLs and these common non-hierarchical URI
    # schemes as URIs; all other ``prefix:reference`` values must use a prefix
    # curie_map declares or an SSSOM built-in one.
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
    if prefix not in curie_map and prefix not in _BUILTIN_PREFIXES:
        raise ValueError(
            f"SSSOM {field_name}{where} uses unknown CURIE prefix '{prefix}'."
        )


def _validate_builtin_prefixes(curie_map: object, path: object) -> None:
    """Mirror ``.ms_sssom_validate_builtin_prefixes``.

    A curie_map may declare a built-in prefix only with its built-in
    expansion. Every such entry is checked, including one for a prefix
    nothing uses, because the rule is on the declaration. R walks its list
    entry by entry so that a repeated name in an in-memory set cannot hide
    behind a correct first entry; a mapping holds one entry per name, so there
    is nothing to hide behind here. A value is trimmed as R's ``trimws()``
    trims, of spaces, tabs, carriage returns and line feeds only, so an
    in-memory value gets R's verdict; a value read from a file was trimmed by
    the parser already. A curie_map that is not a mapping names no prefix,
    as one with no names does in R, and fails where it is first used.
    """
    if not isinstance(curie_map, Mapping):
        return
    for prefix, value in curie_map.items():
        expected = _BUILTIN_PREFIXES.get(prefix)
        if expected is None:
            continue
        expansion = value.strip(" \t\r\n") if isinstance(value, str) else value
        if isinstance(expansion, str) and expansion == expected:
            continue
        raise ValueError(
            f"SSSOM curie_map in {path} redefines built-in prefix "
            f"'{prefix}' as {expansion!r}. The SSSOM specification fixes "
            f"'{prefix}' to '{expected}'; declare it with that expansion or "
            "leave it out."
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
    # The curie_map itself is checked before any CURIE is looked up in it.
    _validate_builtin_prefixes(metadata.get("curie_map"), path)
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
    if "predicate_type" in metadata:
        _validate_predicate_type(str(metadata["predicate_type"]))


def _validate_predicate_type(value: Optional[str], row: Optional[int] = None) -> None:
    """Check the same schema range in row slots and propagated metadata."""
    # Optional table blanks remain blank; a supplied enum spelling is exact.
    if value is not None and value and value not in _PREDICATE_TYPES:
        where = "" if row is None else f" in row {row}"
        raise ValueError(
            f"SSSOM predicate_type{where} must be an allowed SSSOM entity_type_enum value."
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

    if "predicate_type" in columns:
        for row, value in enumerate(columns["predicate_type"], start=1):
            _validate_predicate_type(value, row)

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
    endings, tab delimiters, declared CURIE prefixes, and the package's
    alignment-only profile. In particular, decomposition fields and raw
    literal assignments are refused because they belong in separate SDP
    semantic artifacts.

    Every CURIE prefix must be declared in ``curie_map`` except the SSSOM
    built-in prefixes (``owl``, ``rdf``, ``rdfs``, ``semapv``, ``skos``,
    ``sssom``, ``xsd`` and ``linkml``), which the SSSOM specification lets a
    file omit, so a canonical SSSOM/TSV file that leaves them out is read. A
    ``curie_map`` that does declare a built-in prefix must give it the
    expansion the specification fixes for it (for example
    ``http://www.w3.org/2004/02/skos/core#`` for ``skos``); any other
    expansion is refused. A file with no ``curie_map`` at all, which is how
    the canonical writer writes a set that uses only built-in prefixes, reads
    with an empty map. A table cell enclosed in double quotes with every inner
    quote doubled, the RFC 4180 form the canonical writer uses for a cell that
    contains a double quote, is decoded; any other cell is read byte for byte.

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
#
# Canonical SSSOM/TSV (hub B-351, the mirror half of metasalmon's B-350). The
# specification's "Canonical SSSOM/TSV format" section says writers SHOULD
# write it; Brett ruled on 2026-09-25 that both packages do, together, at a
# minor version. The rules applied, in the section's order
# (src/docs/spec-formats-tsv.md in mapping-commons/sssom at
# 667d3c579d92ad2e1a480503625eeef1e6af8e6d):
#
# * no space between ``#`` and the metadata YAML;
# * slots in the order of the MappingSet "Slots" table (``_METADATA_ORDER``);
# * a scalar in "plain style whenever possible, otherwise in double-quoted
#   style" (``_yaml_scalar``);
# * a multivalued slot as a block sequence (``_MULTIVALUED_METADATA``);
# * no built-in prefix and no unused prefix in ``curie_map``, and no
#   ``curie_map`` at all when nothing is left;
# * a mapping cell quoted only when it must be, RFC 4180 style
#   (``_quote_cell``);
# * a ``double`` slot rounded to at most three decimals
#   (``_canonical_double``);
# * mappings sorted on all their slots in slot order, with a missing value
#   as the empty string it is written as, so it sorts first.
#
# Not applied: condensation of a mapping slot into the set, because it is the
# inverse of propagation, which this profile does not do; and the
# extension-slot rules, because this profile supports no extension slots.
# ``sssom.R`` says the same.

# The YAML 1.1 and 1.2 implicit types, as regular expressions over one plain
# scalar, so a value a YAML reader would type is quoted and reads back as the
# same string. The metadata block is YAML 1.2, but metasalmon reads it with the
# ``yaml`` package, a YAML 1.1 reader, and a user may read it with PyYAML,
# another, so a value is quoted if EITHER version's implicit types would read
# it as something other than a string. The patterns are spelled out, not
# delegated to a parser, so that the two halves match byte for byte: YAML 1.2
# core schema null, bool, int and float; YAML 1.1 null, bool, int (binary,
# octal, decimal with ``_`` and ``,``, hex, sexagesimal), float (PyYAML's
# reading, so ``0.0.8`` stays a string and ``1.1`` does not) and merge key. A
# YAML 1.1 timestamp such as ``2026-07-31`` stays plain: neither the ``yaml``
# package nor YAML 1.2 types it, and it is the form the SSSOM examples write.
# R's ``.ms_sssom_yaml_nonstring_patterns``, with its ``$`` as ``\Z`` because
# Python's ``$`` also matches before a final newline and TRE's does not.
_YAML_NONSTRING_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"^(~|null|Null|NULL)\Z",
        r"^(y|Y|yes|Yes|YES|n|N|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF)\Z",
        r"^[-+]?[0-9]+\Z",
        r"^0o[0-7]+\Z",
        r"^0x[0-9a-fA-F]+\Z",
        r"^[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)([eE][-+]?[0-9]+)?\Z",
        r"^[-+]?\.(inf|Inf|INF)\Z",
        r"^\.(nan|NaN|NAN)\Z",
        r"^[-+]?0b[01_]+\Z",
        r"^[-+]?[0-9][0-9_,]*\Z",
        r"^[-+]?0x[0-9a-fA-F_]+\Z",
        r"^[-+]?[0-9][0-9_]*(:[0-5]?[0-9])+(\.[0-9_]*)?\Z",
        r"^[-+]?([0-9][0-9_]*)?\.[0-9_]*([eE][-+][0-9]+)?\Z",
        r"^<<\Z",
    )
)
_YAML_EDGE_SPACE_RE = re.compile(r"^[ \t]|[ \t]\Z")
_YAML_INDICATOR_RE = re.compile(r"^[][{}#&*!|>'\"%@`=?:,-]")
# Only characters YAML may carry unescaped in a plain scalar: no control
# characters (tab included), no DEL, no C1 controls, no byte-order mark and no
# Unicode line or paragraph separator. R's class starts at U+0001 because an R
# string cannot hold NUL; a Python string can, and it is quoted here too.
_YAML_UNSAFE_CHAR_RE = re.compile("[\u0000-\u001f\u007f-\u009f\ufeff\u2028\u2029]")
_DECIMAL_RE = re.compile(r"[0-9]+(\.[0-9]+)?\Z")
_USED_PREFIX_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9._-]*(?=:)")


def _yaml_plain_ok(value: str) -> bool:
    """Mirror ``.ms_sssom_yaml_plain_ok``.

    Whether ``value`` can be written as a YAML plain scalar in block context
    with no change to what a reader gets back. Deliberately conservative
    about syntax: a value starting with any YAML indicator character is
    quoted, although YAML would allow some of them (``-x``) plain.
    """
    if not value:
        return False
    # Leading or trailing whitespace would be trimmed; a first character that
    # is a YAML indicator would start another construct.
    if _YAML_EDGE_SPACE_RE.search(value) or _YAML_INDICATOR_RE.match(value):
        return False
    # ``: `` starts a mapping value, `` #`` a comment, and a trailing ``:`` a
    # key.
    if ": " in value or " #" in value or value.endswith(":"):
        return False
    if _YAML_UNSAFE_CHAR_RE.search(value):
        return False
    return not any(pattern.match(value) for pattern in _YAML_NONSTRING_PATTERNS)


def _json_scalar(value: object) -> str:
    """Mirror ``.ms_sssom_json_scalar`` (jsonlite auto-unboxed string)."""
    return json.dumps(str(value), ensure_ascii=False)


def _yaml_scalar(value: object) -> str:
    """Mirror ``.ms_sssom_yaml_scalar``.

    One scalar in "plain style whenever possible, otherwise in double-quoted
    style". The double-quoted form is the JSON string, which is valid YAML.
    """
    value = str(value)
    return value if _yaml_plain_ok(value) else _json_scalar(value)


def _used_prefixes(mapping_set: SssomMappingSet) -> set:
    """Mirror ``.ms_sssom_used_prefixes``.

    The prefixes a set uses: those that begin some value, metadata or cell,
    once each multivalued value is split on ``|``.
    """
    values: List[Optional[str]] = []
    for name, value in mapping_set.metadata.items():
        if name == "curie_map":
            continue
        if isinstance(value, dict):
            values.extend(str(item) for item in value.values())
        elif isinstance(value, (list, tuple)):
            values.extend(str(item) for item in value)
        else:
            values.append(_cell(value))
    for name in mapping_set.mappings.columns:
        values.extend(_column_values(mapping_set.mappings, name))
    used = set()
    for value in values:
        if value is None or not value:
            continue
        for piece in _split_multivalued(value):
            match = _USED_PREFIX_RE.match(piece)
            if match is not None:
                used.add(match.group(0))
    return used


def _canonical_double(value: Optional[str]) -> Optional[str]:
    """Mirror ``.ms_sssom_canonical_double``.

    "with up to three digits as needed after the decimal point, rounding the
    last digit to the nearest neighbour (rounding up if both neighbours are
    equidistant)". Done on the decimal digits rather than on a float, so
    ``0.0005`` rounds up as written and not by its binary value. A cell that
    is not a plain decimal number is left as it is: this profile does not
    type these columns.
    """
    if value is None or _DECIMAL_RE.match(value) is None:
        return value
    whole, _, fraction = value.partition(".")
    if len(fraction) > 3:
        round_up = fraction[3] >= "5"
        fraction = fraction[:3]
        if round_up:
            digits = [int(digit) for digit in whole + fraction]
            position = len(digits) - 1
            while True:
                if position < 0:
                    digits.insert(0, 1)
                    break
                if digits[position] < 9:
                    digits[position] += 1
                    break
                digits[position] = 0
                position -= 1
            joined = "".join(str(digit) for digit in digits)
            whole, fraction = joined[:-3], joined[-3:]
    fraction = fraction.rstrip("0")
    whole = re.sub(r"^0+(?=[0-9])", "", whole)
    return f"{whole}.{fraction}" if fraction else whole


def _quote_cell(value: str) -> str:
    """Mirror ``.ms_sssom_quote_cell``.

    RFC 4180 quoting as the SSSOM/TSV Quoting section adapts it: quote only a
    value that must be quoted. Tabs and line breaks never reach here, because
    validation refuses them in a cell, so the one trigger is a double quote.
    """
    if '"' in value:
        return '"' + value.replace('"', '""') + '"'
    return value


def _canonical_bytes(mapping_set: SssomMappingSet) -> bytes:
    """Mirror ``.ms_sssom_canonical_bytes`` byte for byte.

    Canonical SSSOM/TSV as deterministic UTF-8, LF-only, trailing-LF bytes:
    metadata comments in MappingSet slot order with no space after ``#`` and
    scalars plain unless a YAML reader would type them, ``curie_map`` holding
    only the used, non-built-in prefixes sorted by name (and left out when
    empty), a multivalued slot as a block sequence, canonical column order,
    ``double`` slots rounded to three decimals, cells quoted only when they
    contain a double quote, and rows sorted as tuples of their written
    values (a missing value is the empty string, so it sorts first).
    ``sorted()`` on ``str`` matches R's radix (C-locale) order because UTF-8
    byte order equals code-point order.

    ONE rendering, read by BOTH the sort key and the emitted bytes: ``cells``
    is built once and indexed by the sort key and the row writer, so row
    order and row content cannot disagree about a cell (the shape of R's
    backlog #93 item 3). The sort key is the value as written before quoting.
    """
    metadata = mapping_set.metadata
    # No built-in prefix and no unused prefix, sorted by name.
    curie_map = metadata.get("curie_map") or {}
    used = _used_prefixes(mapping_set)
    kept = sorted(
        prefix
        for prefix in curie_map
        if prefix not in _BUILTIN_PREFIXES and prefix in used
    )

    metadata_lines: List[str] = []
    for field_name in _METADATA_ORDER:
        if field_name == "curie_map":
            if not kept:
                continue
            metadata_lines.append("#curie_map:")
            for prefix in kept:
                metadata_lines.append(
                    f"#  {_yaml_scalar(prefix)}: {_yaml_scalar(curie_map[prefix])}"
                )
            continue
        if field_name not in metadata:
            continue
        value = metadata[field_name]
        if field_name in _MULTIVALUED_METADATA:
            metadata_lines.append(f"#{field_name}:")
            metadata_lines.extend(
                f"#  - {_yaml_scalar(item)}"
                for item in _split_multivalued(str(value))
            )
        else:
            metadata_lines.append(f"#{field_name}: {_yaml_scalar(value)}")

    columns = [
        name for name in _COLUMN_ORDER if name in mapping_set.mappings.columns
    ]
    cells = {name: _column_values(mapping_set.mappings, name) for name in columns}
    for name in columns:
        if name in _DOUBLE_COLUMNS:
            cells[name] = [_canonical_double(value) for value in cells[name]]
    cells = {
        name: ["" if value is None else value for value in values]
        for name, values in cells.items()
    }
    row_count = len(mapping_set.mappings)
    order = sorted(
        range(row_count),
        key=lambda row: tuple(cells[name][row] for name in columns),
    )

    table_lines = ["\t".join(columns)]
    for row in order:
        table_lines.append(
            "\t".join(_quote_cell(cells[name][row]) for name in columns)
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
    ``metadata/semantic/mapping-sets.json``. Each mapping set is written in
    the canonical SSSOM/TSV format, byte for byte as metasalmon writes it, so
    bytes and manifest ordering are deterministic. This function does not
    turn semantic suggestions or variable decompositions into mappings;
    ``mapping_sets=None`` is therefore a no-op.

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
