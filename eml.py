"""Reviewed EML 2.2.0 export (mirrors metasalmon's ``R/eml-export.R`` at v0.1.7).

EML is deliberately a reviewed, derived representation of a Salmon Data
Package (SDP). ``create_sdp`` produces a review-ready package; this exporter
starts only after the package passes strict semantic validation and a human
has supplied the EML-specific facts that cannot be inferred safely.

Era note: the EML surface changed at metasalmon v0.1.8 (methods-registry
procedures and more); this module ports the **v0.1.7 tag** exactly. In
particular, 0.1.7's EML consumes only the mapping sidecar, the semantic
review ledger, the reviewed vocabulary, and the data resources — it does NOT
read ``metadata/methods.csv`` (that behaviour is 0.1.8+ and lands at the next
replay milestone), and the initial profile must not emit a ``usedProcedure``
annotation.

Parity contract: parity with R is STRUCTURAL, never byte-level (PARITY.md
entry 4). The document is built with stdlib :mod:`xml.etree.ElementTree` —
one namespaced ``eml:eml`` root with unqualified descendants, exactly R's
shape — and pytest asserts ``ET.canonicalize`` equality against R-generated
documents. All deterministic identifiers (UUIDv5 package/series/attribute
IDs, data-object PIDs) are byte-identical to R's by construction.

Optional dependencies: parsing the ``metadata/eml-mapping.yml`` sidecar
(full YAML, like R's ``yaml::read_yaml``) and XSD validation (lxml — the
same libxml2 engine behind ``emld::eml_validate``, so accept/reject
semantics match R by construction) live in the ``metasalmonpy[eml]`` extra
(PARITY.md entries 2, 14, 15). The bundled EML 2.2.0 XSD set under
``data/xsd/eml-2.2.0/`` is the exact schema set metasalmon validates
against (vendored from emld 0.5.3; see the README there).
"""

from __future__ import annotations

import csv
import hashlib
import math
import os
import re
import tempfile
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Union

import pandas as pd

from .atomic_io import apply_default_file_mode
from .metadata import R_CNTRL_CLASS, R_SPACE_CLASS, read_sdp_csv

EML_VERSION = "2.2.0"
_EML_NAMESPACE = "https://eml.ecoinformatics.org/eml-2.2.0"
_EML_FORMAT_ID = _EML_NAMESPACE
_EML_SYSTEM = "knb"
_XSI_NAMESPACE = "http://www.w3.org/2001/XMLSchema-instance"
_KNB_OBJECT_ENDPOINT = "https://knb.ecoinformatics.org/knb/d1/mn/v2/object/"

ET.register_namespace("eml", _EML_NAMESPACE)
ET.register_namespace("xsi", _XSI_NAMESPACE)

_MEASUREMENT_PREDICATES = {
    "variable_topic": "http://purl.org/dc/terms/subject",
    "unit": "http://qudt.org/schema/qudt/hasUnit",
}

_DATA_DIR = Path(__file__).resolve().parent / "data"

# The reviewed semantic-selection ledger. The v0.2 extended layout keeps the
# ledger with the workflow, provenance, and source records it qualifies; the
# root-level path stays as a compatibility route for packages reviewed before
# that layout existed. Order matters only for the error message.
SUPPORTED_REVIEW_PATHS = (
    "reproducibility/reviewed_semantic_selections.csv",
    "reviewed_semantic_selections.csv",
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REVISION_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_ORCID_RE = re.compile(
    r"^https://orcid\.org/[0-9]{4}-[0-9]{4}-[0-9]{4}-[0-9]{3}[0-9X]$"
)
_PUBLICATION_DATE_RE = re.compile(r"^[0-9]{4}(-[0-9]{2}-[0-9]{2})?$")
# R's POSIX character classes as TRE (not Python's ``\s``) resolves them.
# The enumeration and the retirement condition live with the constants in
# ``metadata`` so that every validator mirroring an unescaped ``grepl()``
# shares one definition; approximating either class here is what let U+0085
# in an entity name and U+3000 in a PID through while R rejected both.
_R_SPACE_CLASS = R_SPACE_CLASS
_R_CNTRL_CLASS = R_CNTRL_CLASS

# R: ^[A-Za-z][A-Za-z0-9+.-]*:[^[:space:]]+$
_ABSOLUTE_URI_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9+.\-]*:[^" + _R_SPACE_CLASS + r"]+$"
)
_CONTROL_CHAR_RE = re.compile("[" + _R_CNTRL_CLASS + "]")

_TRIM_CHARS = " \t\r\n"  # R trimws() default character class

# R's as.numeric() is C strtod(): ASCII decimal, C hexadecimal (with an
# optional binary exponent), or an inf/nan spelling, over the whole string
# after C-whitespace trimming. `[0-9]` is deliberate -- Python's `\d` would
# admit the non-ASCII digits R refuses.
_STRTOD_TRIM_CHARS = " \t\n\x0b\f\r"
_STRTOD_HEX_RE = re.compile(r"[+-]?0[xX]")
_STRTOD_RE = re.compile(
    r"""[+-]?(?:
          (?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?
        | 0[xX](?:[0-9A-Fa-f]+\.?[0-9A-Fa-f]*|\.[0-9A-Fa-f]+)(?:[pP][+-]?[0-9]+)?
        | [iI][nN][fF](?:[iI][nN][iI][tT][yY])?
        | [nN][aA][nN]
    )""",
    re.VERBOSE,
)

# readr's era missing-token set, used ONLY to reproduce how metasalmon 0.1.7
# parsed a data *resource* (readr defaults, na = c("", "NA")) when auditing raw
# CSV tokens against declared EML missing-value codes. Reviewed sidecars go
# through read_sdp_csv instead, where a literal "NA" is data (metasalmon
# 0.2.4). Both are narrower than pandas' default NA vocabulary, which would
# treat tokens like "null" as missing where R does not.
_ERA_NA_TOKENS = ("", "NA")

_VALID_SCALES = ("nominal", "ordinal", "interval", "ratio", "dateTime")
_VALID_NUMBER_TYPES = ("natural", "whole", "integer", "real")

_STORAGE_TYPES = {
    "string": "string",
    "integer": "integer",
    "number": "double",
    "boolean": "boolean",
    "date": "date",
    "datetime": "dateTime",
}


# --- small R-semantics helpers -------------------------------------------------


def _trim(value: str) -> str:
    return value.strip(_TRIM_CHARS)


def _is_missing(value: object) -> bool:
    """True for None/NaN/pd.NA cell values (the R ``NA`` analogue)."""
    if value is None or value is pd.NA:
        return True
    return isinstance(value, float) and value != value


def _as_character(value: object) -> str:
    """Mirror ``as.character()``: TRUE/FALSE logicals, whole doubles as ints."""
    if _is_missing(value):
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return str(int(value))
    return str(value)


def _first(value: object) -> object:
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    if isinstance(value, pd.Series):
        return value.iloc[0] if len(value) else None
    return value


def _length(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, (list, tuple, pd.Series)):
        return len(value)
    return 1


def _nonempty(value: object) -> bool:
    """Mirror ``.ms_eml_nonempty``."""
    if value is None or _length(value) == 0:
        return False
    first = _first(value)
    if _is_missing(first):
        return False
    return bool(_trim(_as_character(first)))


def _scalar(mapping: object, field: str, required: bool = True) -> Optional[str]:
    """Mirror ``.ms_eml_scalar``: one non-empty trimmed character value."""
    value = mapping.get(field) if isinstance(mapping, dict) else None
    if not _nonempty(value):
        if required:
            raise ValueError(
                f"EML mapping field {field} must contain one non-empty value."
            )
        return None
    if _length(value) != 1:
        raise ValueError(
            f"EML mapping field {field} must contain exactly one value."
        )
    return _trim(_as_character(_first(value)))


def _as_numeric(value: object) -> Optional[float]:
    """Mirror ``suppressWarnings(as.numeric(...))`` for one value.

    R's coercion is C ``strtod`` over the whole whitespace-trimmed string.
    Python's ``float()`` implements a *different* grammar, and the two
    disagree in both directions: ``float()`` accepts PEP 515 underscores
    (``"1_000"`` -> 1000.0) and non-ASCII digits (``"１２３"``),
    both of which R rejects, and rejects C hexadecimal (``"0x1A"``), which R
    reads as 26. The underscore direction is the dangerous one -- it turns a
    thousands-separated typo into a silently validated observation -- so the
    strtod grammar is screened first and only then handed to Python.
    """
    if _is_missing(value):
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip(_STRTOD_TRIM_CHARS)
    if _STRTOD_RE.fullmatch(text) is None:
        return None
    try:
        if _STRTOD_HEX_RE.match(text) is not None:
            return float.fromhex(text)
        return float(text)
    except (ValueError, OverflowError):
        return None


def _as_integer(value: object) -> Optional[int]:
    """Mirror ``suppressWarnings(as.integer(...))`` (truncating) for one value."""
    numeric = _as_numeric(value)
    if numeric is None or not math.isfinite(numeric):
        return None
    return int(numeric)


def _revision_key(mapping: dict, required: bool = False) -> Optional[str]:
    """Mirror ``.ms_eml_revision_key``."""
    if not isinstance(required, bool):
        raise ValueError(
            "Internal EML export argument required must be one logical value."
        )
    publication = mapping.get("publication")
    key = _scalar(
        publication if isinstance(publication, dict) else {},
        "revision_key",
        required=required,
    )
    if key is None:
        return None
    if len(key.encode("utf-8")) > 128 or _REVISION_KEY_RE.fullmatch(key) is None:
        raise ValueError(
            "EML mapping publication.revision_key must be 1-128 ASCII "
            "letters, numbers, periods, underscores, or hyphens, starting "
            "with a letter or number."
        )
    return key


def _split_iris(value: object) -> List[str]:
    """Mirror ``.ms_eml_split_iris`` (semicolon-separated, trimmed, unique)."""
    if value is None:
        return []
    values = value if isinstance(value, (list, tuple)) else [value]
    pieces: List[str] = []
    for item in values:
        if _is_missing(item):
            continue
        pieces.extend(_as_character(item).split(";"))
    seen: List[str] = []
    for piece in pieces:
        token = _trim(piece)
        if token and token not in seen:
            seen.append(token)
    return seen


def _uuid5(name: object) -> str:
    """Mirror ``.ms_eml_uuid5``: RFC 9562 UUIDv5 in the URL namespace."""
    if not _nonempty(name):
        raise ValueError("A non-empty name is required to construct a UUIDv5.")
    return str(uuid.uuid5(uuid.NAMESPACE_URL, _as_character(_first(name))))


def _eml_id(prefix: str, name: str) -> str:
    return prefix + "-" + _uuid5(name).replace("-", "")


def _attribute_id(dataset_id: str, table_id: str, column_name: str) -> str:
    return _eml_id(
        "attribute", ":".join([dataset_id, table_id, column_name])
    )


def _knb_object_url(pid: str) -> str:
    """Mirror ``.ms_eml_knb_object_url``.

    Keep the URN colons literal: MetacatUI matches an EML distribution to
    its DataONE object by finding the unescaped PID as a substring of this
    URL.
    """
    from urllib.parse import quote

    encoded = quote(pid, safe="")
    encoded = encoded.replace("%3A", ":")
    return _KNB_OBJECT_ENDPOINT + encoded


# --- XML node helpers ------------------------------------------------------------


def _add_text(
    parent: ET.Element,
    name: str,
    value: object,
    attrs: Optional[Dict[str, str]] = None,
) -> Optional[ET.Element]:
    """Mirror ``.ms_eml_add_text``: skip silently when the value is empty."""
    if not _nonempty(value):
        return None
    node = ET.SubElement(parent, name)
    node.text = _as_character(_first(value))
    if attrs:
        for key, attr_value in attrs.items():
            node.set(key, attr_value)
    return node


def _add_para(parent: ET.Element, name: str, value: object) -> Optional[ET.Element]:
    if not _nonempty(value):
        return None
    node = ET.SubElement(parent, name)
    _add_text(node, "para", value)
    return node


def _add_party(
    parent: ET.Element, element: str, party: object, id_name: str
) -> ET.Element:
    """Mirror ``.ms_eml_add_party``."""
    if not isinstance(party, dict):
        raise ValueError(
            f"Each {element} entry in the EML mapping must be a mapping."
        )

    surname = _scalar(party, "surname", required=False)
    given_name = _scalar(party, "given_name", required=False)
    organization = _scalar(party, "organization_name", required=False)
    position = _scalar(party, "position_name", required=False)

    has_individual = _nonempty(surname)
    if _nonempty(given_name) and not has_individual:
        raise ValueError(
            "An EML party with given_name must also provide surname."
        )
    if not has_individual and not _nonempty(organization) and not _nonempty(position):
        raise ValueError(
            "Each EML party must provide surname, organization_name, or "
            "position_name."
        )

    node = ET.SubElement(parent, element)
    node.set("id", _eml_id("party", id_name))

    if has_individual:
        individual = ET.SubElement(node, "individualName")
        _add_text(individual, "givenName", given_name)
        _add_text(individual, "surName", surname)
    _add_text(node, "organizationName", organization)
    _add_text(node, "positionName", position)

    email = party.get("email")
    if email is not None:
        values = email if isinstance(email, (list, tuple)) else [email]
        for value in values:
            _add_text(node, "electronicMailAddress", _as_character(value))

    orcid = _scalar(party, "orcid", required=False)
    if _nonempty(orcid):
        if _ORCID_RE.fullmatch(orcid) is None:
            raise ValueError(
                "EML party orcid must be a full https://orcid.org/ URI."
            )
        _add_text(node, "userId", orcid, attrs={"directory": "https://orcid.org"})
    return node


# --- mapping sidecar -------------------------------------------------------------


def _default_mapping_path(path: Union[str, Path]) -> Path:
    """Mirror ``.ms_eml_default_mapping_path``."""
    yml = Path(path) / "metadata" / "eml-mapping.yml"
    yaml_path = Path(path) / "metadata" / "eml-mapping.yaml"
    if yml.exists() and yaml_path.exists():
        raise ValueError(
            "Both eml-mapping.yml and eml-mapping.yaml exist. Keep one "
            "canonical sidecar; eml-mapping.yml is the default."
        )
    return yml


def _attribute_configs(mapping: dict, dictionary: pd.DataFrame) -> List[dict]:
    """Mirror ``.ms_eml_attribute_configs``."""
    tables = mapping.get("tables")
    if not isinstance(tables, dict) or not tables:
        raise ValueError("EML mapping tables must be keyed by table ID.")

    dictionary_tables = [_as_character(value) for value in dictionary["table_id"]]
    dictionary_columns = [
        _as_character(value) for value in dictionary["column_name"]
    ]
    expected_tables = list(dict.fromkeys(dictionary_tables))
    actual_tables = [str(name) for name in tables.keys()]
    if set(expected_tables) != set(actual_tables):
        raise ValueError(
            "EML mapping tables must describe exactly the SDP tables. "
            f"Expected: {sorted(expected_tables)}. "
            f"Found: {sorted(actual_tables)}."
        )

    configs: List[dict] = []
    for row in range(len(dictionary)):
        table_id = dictionary_tables[row]
        column_name = dictionary_columns[row]
        table_entry = tables[table_id]
        if not isinstance(table_entry, dict):
            raise ValueError(f"EML mapping tables.{table_id} must be a mapping.")
        table_mapping = table_entry.get("attributes")
        if not isinstance(table_mapping, dict) or not table_mapping:
            raise ValueError(
                f"EML mapping tables.{table_id}.attributes must be keyed by "
                "column name."
            )

        expected_columns = [
            dictionary_columns[index]
            for index in range(len(dictionary))
            if dictionary_tables[index] == table_id
        ]
        actual_columns = [str(name) for name in table_mapping.keys()]
        if set(expected_columns) != set(actual_columns):
            raise ValueError(
                f"EML mapping tables.{table_id}.attributes must describe "
                "exactly the SDP columns. "
                f"Expected: {sorted(expected_columns)}. "
                f"Found: {sorted(actual_columns)}."
            )

        config = table_mapping.get(column_name)
        if not isinstance(config, dict):
            raise ValueError(
                f"EML mapping for {table_id}.{column_name} must be a mapping."
            )
        configs.append(config)
    return configs


# --- mapping schema (the bundled eml-mapping JSON Schema, enforced in code) -------
#
# R validates the sidecar against inst/extdata/schema/eml-mapping.schema.json
# via jsonvalidate/ajv. The same schema document is vendored verbatim under
# data/schema/eml-mapping.schema.json, and this validator enforces its
# constraints structurally so no JSON Schema engine is needed (PARITY.md
# entry 16): the same sidecars pass and fail, with a Python-worded report.


def _is_nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(re.search(r"\S", value))


def _is_schema_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_schema_integer(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and value.is_integer()


def _schema_party(errors: List[str], where: str, party: object) -> None:
    allowed = (
        "given_name",
        "surname",
        "organization_name",
        "position_name",
        "email",
        "orcid",
    )
    if not isinstance(party, dict) or not party:
        errors.append(f"{where}: must be a party mapping with at least one field")
        return
    for key in party:
        if key not in allowed:
            errors.append(f"{where}.{key}: is not an allowed party field")
    for key in ("given_name", "surname", "organization_name", "position_name"):
        if key in party and not _is_nonempty_string(party[key]):
            errors.append(f"{where}.{key}: must be a non-empty string")
    if "email" in party:
        email = party["email"]
        if isinstance(email, list):
            if not email or any(not _is_nonempty_string(v) for v in email):
                errors.append(
                    f"{where}.email: must be a non-empty string or a "
                    "non-empty array of non-empty strings"
                )
        elif not _is_nonempty_string(email):
            errors.append(
                f"{where}.email: must be a non-empty string or a non-empty "
                "array of non-empty strings"
            )
    if "orcid" in party and (
        not isinstance(party["orcid"], str)
        or _ORCID_RE.fullmatch(party["orcid"]) is None
    ):
        errors.append(f"{where}.orcid: must be a full https://orcid.org/ URI")


def _schema_hash_sidecar(
    errors: List[str], where: str, value: object, expected_path: object
) -> None:
    expected = (
        (expected_path,) if isinstance(expected_path, str) else tuple(expected_path)
    )
    if not isinstance(value, dict):
        errors.append(f"{where}: must be a path/sha256 mapping")
        return
    for key in value:
        if key not in ("path", "sha256"):
            errors.append(f"{where}.{key}: is not an allowed field")
    if value.get("path") not in expected:
        errors.append(f"{where}.path: must be {' or '.join(expected)}")
    sha256 = value.get("sha256")
    if not isinstance(sha256, str) or _SHA256_RE.fullmatch(sha256) is None:
        errors.append(f"{where}.sha256: must be a lowercase SHA-256 digest")


def _schema_attribute(errors: List[str], where: str, config: object) -> None:
    allowed = (
        "measurement_scale",
        "eml_unit",
        "number_type",
        "minimum",
        "minimum_exclusive",
        "maximum",
        "maximum_exclusive",
        "precision",
        "format_string",
        "missing_values",
        "code_order",
    )
    if not isinstance(config, dict):
        errors.append(f"{where}: must be an attribute mapping")
        return
    for key in config:
        if key not in allowed:
            errors.append(f"{where}.{key}: is not an allowed attribute field")
    scale = config.get("measurement_scale")
    if scale not in _VALID_SCALES:
        errors.append(
            f"{where}.measurement_scale: must be one of "
            + ", ".join(_VALID_SCALES)
        )
    if "eml_unit" in config and not _is_nonempty_string(config["eml_unit"]):
        errors.append(f"{where}.eml_unit: must be a non-empty string")
    if "number_type" in config and config["number_type"] not in _VALID_NUMBER_TYPES:
        errors.append(
            f"{where}.number_type: must be one of "
            + ", ".join(_VALID_NUMBER_TYPES)
        )
    for key in ("minimum", "maximum"):
        if key in config and not _is_schema_number(config[key]):
            errors.append(f"{where}.{key}: must be a number")
    for key in ("minimum_exclusive", "maximum_exclusive"):
        if key in config and not isinstance(config[key], bool):
            errors.append(f"{where}.{key}: must be a boolean")
    if "precision" in config and (
        not _is_schema_number(config["precision"]) or config["precision"] <= 0
    ):
        errors.append(f"{where}.precision: must be a positive number")
    if "format_string" in config and not _is_nonempty_string(
        config["format_string"]
    ):
        errors.append(f"{where}.format_string: must be a non-empty string")
    if "missing_values" in config:
        missing_values = config["missing_values"]
        if not isinstance(missing_values, list) or not missing_values:
            errors.append(
                f"{where}.missing_values: must be a non-empty array of "
                "code/explanation mappings"
            )
        else:
            for index, entry in enumerate(missing_values):
                entry_where = f"{where}.missing_values[{index}]"
                if not isinstance(entry, dict):
                    errors.append(f"{entry_where}: must be a mapping")
                    continue
                for key in entry:
                    if key not in ("code", "explanation"):
                        errors.append(
                            f"{entry_where}.{key}: is not an allowed field"
                        )
                for key in ("code", "explanation"):
                    if not _is_nonempty_string(entry.get(key)):
                        errors.append(
                            f"{entry_where}.{key}: must be a non-empty string"
                        )
    if "code_order" in config:
        code_order = config["code_order"]
        if not isinstance(code_order, dict) or not code_order:
            errors.append(
                f"{where}.code_order: must be a non-empty mapping of "
                "code values to integers"
            )
        else:
            for key, order in code_order.items():
                if not _is_schema_integer(order):
                    errors.append(f"{where}.code_order.{key}: must be an integer")
    if isinstance(config, dict) and scale in ("interval", "ratio"):
        for key in ("eml_unit", "number_type"):
            if key not in config:
                errors.append(
                    f"{where}.{key}: is required when measurement_scale is "
                    f"{scale}"
                )
    if isinstance(config, dict) and scale == "dateTime":
        if "format_string" not in config:
            errors.append(
                f"{where}.format_string: is required when measurement_scale "
                "is dateTime"
            )


def _validate_mapping_schema(mapping: dict) -> None:
    """Mirror ``.ms_eml_validate_mapping_schema`` (the bundled JSON Schema)."""
    errors: List[str] = []
    required = (
        "version",
        "status",
        "dataset_id",
        "series_key",
        "system",
        "language",
        "publication_date",
        "semantic_vocabulary",
        "semantic_review",
        "publication",
        "rights_authorization",
        "source_provenance",
        "creators",
        "metadata_providers",
        "contacts",
        "publisher",
        "intellectual_rights",
        "methods",
        "tables",
    )
    allowed = required + ("taxonomic_coverage", "geographic_coverage")
    for key in required:
        if key not in mapping:
            errors.append(f"{key}: is required")
    for key in mapping:
        if key not in allowed:
            errors.append(f"{key}: is not an allowed EML mapping field")

    if "version" in mapping and (
        isinstance(mapping["version"], bool) or mapping["version"] != 1
    ):
        errors.append("version: must be the constant 1")
    if "status" in mapping and mapping["status"] not in (
        "draft",
        "review",
        "final",
    ):
        errors.append("status: must be one of draft, review, final")
    for key in ("dataset_id", "series_key", "language"):
        if key in mapping and not _is_nonempty_string(mapping[key]):
            errors.append(f"{key}: must be a non-empty string")
    if "system" in mapping and mapping["system"] != "knb":
        errors.append("system: must be the constant knb")
    if "publication_date" in mapping and (
        not isinstance(mapping["publication_date"], str)
        or _PUBLICATION_DATE_RE.fullmatch(mapping["publication_date"]) is None
    ):
        errors.append("publication_date: must match YYYY or YYYY-MM-DD")

    if "semantic_vocabulary" in mapping:
        _schema_hash_sidecar(
            errors,
            "semantic_vocabulary",
            mapping["semantic_vocabulary"],
            "metadata/semantic_vocabulary.csv",
        )
    if "semantic_review" in mapping:
        _schema_hash_sidecar(
            errors,
            "semantic_review",
            mapping["semantic_review"],
            SUPPORTED_REVIEW_PATHS,
        )

    if "publication" in mapping:
        publication = mapping["publication"]
        if not isinstance(publication, dict):
            errors.append("publication: must be a mapping")
        else:
            for key in publication:
                if key not in ("public", "revision_key"):
                    errors.append(f"publication.{key}: is not an allowed field")
            if not isinstance(publication.get("public"), bool):
                errors.append("publication.public: must be a boolean")
            if "revision_key" in publication:
                revision_key = publication["revision_key"]
                if (
                    not isinstance(revision_key, str)
                    or len(revision_key) > 128
                    or _REVISION_KEY_RE.fullmatch(revision_key) is None
                ):
                    errors.append(
                        "publication.revision_key: must be 1-128 ASCII "
                        "letters, numbers, periods, underscores, or hyphens"
                    )

    if "rights_authorization" in mapping:
        rights_authorization = mapping["rights_authorization"]
        if not isinstance(rights_authorization, dict):
            errors.append("rights_authorization: must be a mapping")
        else:
            for key in rights_authorization:
                if key not in ("status", "evidence"):
                    errors.append(
                        f"rights_authorization.{key}: is not an allowed field"
                    )
            if rights_authorization.get("status") not in (
                "unconfirmed",
                "confirmed",
            ):
                errors.append(
                    "rights_authorization.status: must be unconfirmed or "
                    "confirmed"
                )
            if not _is_nonempty_string(rights_authorization.get("evidence")):
                errors.append(
                    "rights_authorization.evidence: must be a non-empty string"
                )

    if "source_provenance" in mapping:
        source_provenance = mapping["source_provenance"]
        if not isinstance(source_provenance, dict):
            errors.append("source_provenance: must be a mapping")
        else:
            for key in source_provenance:
                if key not in (
                    "source_citation",
                    "provenance_note",
                    "supporting_document",
                ):
                    errors.append(
                        f"source_provenance.{key}: is not an allowed field"
                    )
            for key in ("source_citation", "provenance_note"):
                if not _is_nonempty_string(source_provenance.get(key)):
                    errors.append(
                        f"source_provenance.{key}: must be a non-empty string"
                    )
            supporting = source_provenance.get("supporting_document")
            if not isinstance(supporting, dict):
                errors.append(
                    "source_provenance.supporting_document: must be a "
                    "citation/url/sha256 mapping"
                )
            else:
                for key in supporting:
                    if key not in ("citation", "url", "sha256"):
                        errors.append(
                            "source_provenance.supporting_document."
                            f"{key}: is not an allowed field"
                        )
                if not _is_nonempty_string(supporting.get("citation")):
                    errors.append(
                        "source_provenance.supporting_document.citation: "
                        "must be a non-empty string"
                    )
                url = supporting.get("url")
                if not isinstance(url, str) or not re.match(r"^https?://", url):
                    errors.append(
                        "source_provenance.supporting_document.url: must be "
                        "an HTTP(S) URL"
                    )
                sha256 = supporting.get("sha256")
                if (
                    not isinstance(sha256, str)
                    or _SHA256_RE.fullmatch(sha256) is None
                ):
                    errors.append(
                        "source_provenance.supporting_document.sha256: must "
                        "be a lowercase SHA-256 digest"
                    )

    for field in ("creators", "metadata_providers", "contacts"):
        if field in mapping:
            parties = mapping[field]
            if not isinstance(parties, list) or not parties:
                errors.append(f"{field}: must be a non-empty array of parties")
            else:
                for index, party in enumerate(parties):
                    _schema_party(errors, f"{field}[{index}]", party)
    if "publisher" in mapping:
        _schema_party(errors, "publisher", mapping["publisher"])

    if "intellectual_rights" in mapping:
        rights = mapping["intellectual_rights"]
        if not isinstance(rights, dict):
            errors.append("intellectual_rights: must be a mapping")
        else:
            for key in rights:
                if key != "paragraphs":
                    errors.append(
                        f"intellectual_rights.{key}: is not an allowed field"
                    )
            paragraphs = rights.get("paragraphs")
            # R coerces a scalar YAML paragraph into a one-element array
            # before schema validation (as.list on a length-one character
            # vector); accept the same.
            if isinstance(paragraphs, str):
                paragraphs = [paragraphs]
            if not isinstance(paragraphs, list) or not paragraphs or any(
                not _is_nonempty_string(paragraph) for paragraph in paragraphs
            ):
                errors.append(
                    "intellectual_rights.paragraphs: must be a non-empty "
                    "array of non-empty strings"
                )

    if "methods" in mapping:
        methods = mapping["methods"]
        if not isinstance(methods, list) or not methods:
            errors.append(
                "methods: must be a non-empty array of method-step mappings"
            )
        else:
            for index, method in enumerate(methods):
                where = f"methods[{index}]"
                if not isinstance(method, dict):
                    errors.append(f"{where}: must be a mapping")
                    continue
                for key in method:
                    if key != "description":
                        errors.append(f"{where}.{key}: is not an allowed field")
                if not _is_nonempty_string(method.get("description")):
                    errors.append(
                        f"{where}.description: must be a non-empty string"
                    )

    if "taxonomic_coverage" in mapping:
        taxon = mapping["taxonomic_coverage"]
        if not isinstance(taxon, dict):
            errors.append("taxonomic_coverage: must be a mapping")
        else:
            for key in taxon:
                if key not in ("scientific_name", "common_name", "rank"):
                    errors.append(
                        f"taxonomic_coverage.{key}: is not an allowed field"
                    )
            if not _is_nonempty_string(taxon.get("scientific_name")):
                errors.append(
                    "taxonomic_coverage.scientific_name: must be a non-empty "
                    "string"
                )
            for key in ("common_name", "rank"):
                if key in taxon and not _is_nonempty_string(taxon[key]):
                    errors.append(
                        f"taxonomic_coverage.{key}: must be a non-empty string"
                    )

    if "geographic_coverage" in mapping:
        geographic = mapping["geographic_coverage"]
        if not isinstance(geographic, dict):
            errors.append("geographic_coverage: must be a mapping")
        else:
            required_bounds = ("description", "west", "east", "south", "north")
            for key in geographic:
                if key not in required_bounds:
                    errors.append(
                        f"geographic_coverage.{key}: is not an allowed field"
                    )
            for key in required_bounds:
                if key not in geographic:
                    errors.append(f"geographic_coverage.{key}: is required")
            if not _is_nonempty_string(geographic.get("description")):
                errors.append(
                    "geographic_coverage.description: must be a non-empty "
                    "string"
                )
            for key, low, high in (
                ("west", -180, 180),
                ("east", -180, 180),
                ("south", -90, 90),
                ("north", -90, 90),
            ):
                if key in geographic:
                    value = geographic[key]
                    if not _is_schema_number(value) or not (
                        low <= value <= high
                    ):
                        errors.append(
                            f"geographic_coverage.{key}: must be a number "
                            f"between {low} and {high}"
                        )

    if "tables" in mapping:
        tables = mapping["tables"]
        if not isinstance(tables, dict) or not tables:
            errors.append("tables: must be a non-empty mapping keyed by table ID")
        else:
            for table_id, table_entry in tables.items():
                where = f"tables.{table_id}"
                if not isinstance(table_entry, dict):
                    errors.append(f"{where}: must be a mapping")
                    continue
                for key in table_entry:
                    if key != "attributes":
                        errors.append(f"{where}.{key}: is not an allowed field")
                attributes = table_entry.get("attributes")
                if not isinstance(attributes, dict) or not attributes:
                    errors.append(
                        f"{where}.attributes: must be a non-empty mapping "
                        "keyed by column name"
                    )
                    continue
                for column_name, config in attributes.items():
                    _schema_attribute(
                        errors, f"{where}.attributes.{column_name}", config
                    )

    if errors:
        detail = "\n".join(errors[:12])
        raise ValueError(
            "EML mapping sidecar failed the bundled JSON Schema.\n" + detail
        )


# --- reviewed unit crosswalk ------------------------------------------------------


def _read_character_csv(path: Union[str, Path]) -> pd.DataFrame:
    """Read a reviewed sidecar CSV through the shared SDP reader.

    Previously this reader treated a literal ``NA`` as missing while
    ``package_io`` preserved it, which made a dictionary cell of ``NA`` name a
    canonical semantic target that no ``semantic_vocabulary.csv`` row could
    ever satisfy. All SDP readers now share ``read_sdp_csv``.
    """
    return read_sdp_csv(path)


def _unit_crosswalk() -> pd.DataFrame:
    """Mirror ``.ms_eml_unit_crosswalk`` (the bundled reviewed crosswalk)."""
    path = _DATA_DIR / "eml-unit-crosswalk.csv"
    if not path.is_file():
        raise FileNotFoundError(
            "Could not locate the bundled reviewed EML unit crosswalk."
        )
    crosswalk = _read_character_csv(path)
    required = ["unit_iri", "eml_standard_unit", "review_status", "profile_version"]
    values = crosswalk.values.tolist()
    malformed = (
        list(crosswalk.columns) != required
        or len(crosswalk) == 0
        or any(_is_missing(cell) for row in values for cell in row)
        or any(not _trim(str(cell)) for row in values for cell in row)
        or any(status != "reviewed" for status in crosswalk["review_status"])
        or crosswalk["unit_iri"].duplicated().any()
    )
    if malformed:
        raise ValueError(
            "The bundled EML unit crosswalk is malformed or contains "
            "unreviewed/duplicate entries."
        )
    return crosswalk


# --- mapping validation ------------------------------------------------------------


def _dictionary_row(dictionary: pd.DataFrame, row: int) -> Dict[str, object]:
    return {column: dictionary.iloc[row][column] for column in dictionary.columns}


def _validate_mapping(
    mapping: object, pkg: Dict[str, object], require_final: bool = True
) -> List[dict]:
    """Mirror ``.ms_eml_validate_mapping``."""
    if not isinstance(mapping, dict):
        raise ValueError("The EML mapping sidecar must contain a YAML mapping.")
    _validate_mapping_schema(mapping)
    if _as_integer(mapping.get("version")) != 1:
        raise ValueError("EML mapping version must be 1.")
    status = _scalar(mapping, "status")
    if require_final and status != "final":
        raise ValueError('EML mapping status must be "final" before export.')

    dataset = pkg["dataset"]
    dataset_id = _scalar(mapping, "dataset_id")
    package_dataset_id = _trim(_as_character(dataset.iloc[0]["dataset_id"]))
    if dataset_id != package_dataset_id:
        raise ValueError(
            f"EML mapping dataset_id {dataset_id!r} does not match SDP "
            f"dataset ID {package_dataset_id!r}."
        )

    _scalar(mapping, "series_key")
    system = _scalar(mapping, "system")
    if system != _EML_SYSTEM:
        raise ValueError(
            f"EML mapping system must be {_EML_SYSTEM!r} for the KNB "
            "publication profile."
        )
    _scalar(mapping, "language")
    publication_date = _scalar(mapping, "publication_date")
    if _PUBLICATION_DATE_RE.fullmatch(publication_date) is None:
        raise ValueError(
            "EML mapping publication_date must be YYYY or YYYY-MM-DD."
        )
    calendar_value = (
        publication_date + "-01-01"
        if len(publication_date) == 4
        else publication_date
    )
    try:
        datetime.strptime(calendar_value, "%Y-%m-%d")
    except ValueError:
        raise ValueError(
            "EML mapping publication_date is not a valid calendar date."
        ) from None

    if not isinstance(mapping.get("publisher"), dict):
        raise ValueError("EML mapping publisher must be a party mapping.")
    rights = mapping.get("intellectual_rights")
    paragraphs = rights.get("paragraphs") if isinstance(rights, dict) else None
    if isinstance(paragraphs, str):
        paragraphs = [paragraphs]
    if (
        not isinstance(rights, dict)
        or not isinstance(paragraphs, list)
        or len(paragraphs) == 0
        or any(not isinstance(paragraph, str) for paragraph in paragraphs)
        or any(not _trim(paragraph) for paragraph in paragraphs)
    ):
        raise ValueError(
            "EML mapping intellectual_rights.paragraphs must contain "
            "non-empty text."
        )
    methods = mapping.get("methods")
    if not isinstance(methods, list) or len(methods) == 0:
        raise ValueError(
            "EML mapping methods must contain at least one method-step "
            "mapping."
        )
    for method in methods:
        if not isinstance(method, dict):
            raise ValueError("Each EML method step must be a mapping.")
        _scalar(method, "description")

    semantic_vocabulary = mapping.get("semantic_vocabulary")
    if not isinstance(semantic_vocabulary, dict):
        raise ValueError(
            "EML mapping semantic_vocabulary must be a path/hash mapping."
        )
    vocabulary_path = _scalar(semantic_vocabulary, "path")
    if vocabulary_path != "metadata/semantic_vocabulary.csv":
        raise ValueError(
            "EML mapping semantic_vocabulary.path must be "
            "metadata/semantic_vocabulary.csv."
        )
    vocabulary_sha256 = _scalar(semantic_vocabulary, "sha256")
    if _SHA256_RE.fullmatch(vocabulary_sha256) is None:
        raise ValueError(
            "EML mapping semantic_vocabulary.sha256 must be a lowercase "
            "SHA-256 digest."
        )

    semantic_review = mapping.get("semantic_review")
    if not isinstance(semantic_review, dict):
        raise ValueError(
            "EML mapping semantic_review must be a path/hash mapping."
        )
    review_path = _scalar(semantic_review, "path")
    if review_path not in SUPPORTED_REVIEW_PATHS:
        raise ValueError(
            "EML mapping semantic_review.path must use the canonical "
            "reproducibility ledger or its legacy root-level compatibility "
            "path."
        )
    review_sha256 = _scalar(semantic_review, "sha256")
    if _SHA256_RE.fullmatch(review_sha256) is None:
        raise ValueError(
            "EML mapping semantic_review.sha256 must be a lowercase SHA-256 "
            "digest."
        )

    publication = mapping.get("publication")
    if not isinstance(publication, dict) or not isinstance(
        publication.get("public"), bool
    ):
        raise ValueError(
            "EML mapping publication.public must be one explicit logical "
            "value."
        )
    _revision_key(mapping)

    rights_authorization = mapping.get("rights_authorization")
    if not isinstance(rights_authorization, dict) or _scalar(
        rights_authorization, "status"
    ) not in ("unconfirmed", "confirmed"):
        raise ValueError(
            "EML mapping rights_authorization.status must be "
            '"unconfirmed" or "confirmed".'
        )
    _scalar(rights_authorization, "evidence")

    source_provenance = mapping.get("source_provenance")
    if not isinstance(source_provenance, dict):
        raise ValueError(
            "EML mapping source_provenance must be a structured mapping."
        )
    source_citation = _scalar(source_provenance, "source_citation")
    provenance_note = _scalar(source_provenance, "provenance_note")
    package_source_citation = _trim(_as_character(dataset.iloc[0]["source_citation"]))
    package_provenance_note = _trim(_as_character(dataset.iloc[0]["provenance_note"]))
    if source_citation != package_source_citation:
        raise ValueError(
            "EML mapping source_provenance.source_citation does not match "
            "SDP source_citation."
        )
    if provenance_note != package_provenance_note:
        raise ValueError(
            "EML mapping source_provenance.provenance_note does not match "
            "SDP provenance_note."
        )
    supporting_document = source_provenance.get("supporting_document")
    if not isinstance(supporting_document, dict):
        raise ValueError(
            "EML mapping source_provenance.supporting_document must be a "
            "citation/URL/hash mapping."
        )
    _scalar(supporting_document, "citation")
    supporting_url = _scalar(supporting_document, "url")
    if not re.match(r"^https?://", supporting_url):
        raise ValueError(
            "EML mapping source_provenance.supporting_document.url must be "
            "an HTTP(S) URL."
        )
    supporting_sha256 = _scalar(supporting_document, "sha256")
    if _SHA256_RE.fullmatch(supporting_sha256) is None:
        raise ValueError(
            "EML mapping source_provenance.supporting_document.sha256 must "
            "be a lowercase SHA-256 digest."
        )

    for field in ("creators", "metadata_providers", "contacts"):
        parties = mapping.get(field)
        if not isinstance(parties, list) or len(parties) == 0:
            raise ValueError(
                f"EML mapping {field} must contain at least one party."
            )

    geographic = mapping.get("geographic_coverage")
    if geographic is not None:
        if not isinstance(geographic, dict):
            raise ValueError("EML mapping geographic_coverage must be a mapping.")
        _scalar(geographic, "description")
        numeric_bounds: Dict[str, float] = {}
        for field in ("west", "east", "south", "north"):
            value = _as_numeric(geographic.get(field))
            if value is None or not math.isfinite(value):
                raise ValueError(
                    f"EML mapping geographic_coverage.{field} must be one "
                    "finite number."
                )
            numeric_bounds[field] = value
        if (
            numeric_bounds["west"] > numeric_bounds["east"]
            or numeric_bounds["south"] > numeric_bounds["north"]
            or numeric_bounds["west"] < -180
            or numeric_bounds["east"] < -180
            or numeric_bounds["west"] > 180
            or numeric_bounds["east"] > 180
            or numeric_bounds["south"] < -90
            or numeric_bounds["north"] < -90
            or numeric_bounds["south"] > 90
            or numeric_bounds["north"] > 90
        ):
            raise ValueError(
                "EML geographic_coverage bounds are out of range or reversed."
            )

    dictionary = pkg["dictionary"]
    configs = _attribute_configs(mapping, dictionary)
    unit_crosswalk = _unit_crosswalk()

    for row, config in enumerate(configs):
        field = (
            _as_character(dictionary.iloc[row]["table_id"])
            + "."
            + _as_character(dictionary.iloc[row]["column_name"])
        )
        scale = _scalar(config, "measurement_scale")
        if scale not in _VALID_SCALES:
            raise ValueError(
                f"EML mapping {field}.measurement_scale must be one of "
                + ", ".join(_VALID_SCALES)
                + "."
            )

        if scale in ("interval", "ratio"):
            value_type = _as_character(dictionary.iloc[row]["value_type"])
            if value_type not in ("integer", "number"):
                raise ValueError(
                    f"EML {scale} scale for {field} requires SDP value_type "
                    f'"integer" or "number", not {value_type!r}.'
                )
            unit = _scalar(config, "eml_unit")
            unit_iri = _scalar(_dictionary_row(dictionary, row), "unit_iri")
            crosswalk_rows = unit_crosswalk[
                unit_crosswalk["unit_iri"] == unit_iri
            ]
            if len(crosswalk_rows) != 1:
                raise ValueError(
                    "No reviewed EML standard-unit mapping exists for "
                    f"canonical unit IRI {unit_iri!r} on {field}. Add and "
                    "review an exact crosswalk entry before extending the "
                    "exporter."
                )
            expected_unit = crosswalk_rows.iloc[0]["eml_standard_unit"]
            if unit != expected_unit:
                raise ValueError(
                    f"EML mapping {field}.eml_unit must be "
                    f"{expected_unit!r} for canonical unit IRI {unit_iri!r}, "
                    f"not {unit!r}."
                )
            number_type = _scalar(config, "number_type")
            if number_type not in _VALID_NUMBER_TYPES:
                raise ValueError(
                    f"EML mapping {field}.number_type must be one of "
                    + ", ".join(_VALID_NUMBER_TYPES)
                    + "."
                )

            minimum = config.get("minimum")
            maximum = config.get("maximum")
            has_minimum = minimum is not None
            has_maximum = maximum is not None
            if has_minimum:
                minimum = _as_numeric(minimum)
                if minimum is None or not math.isfinite(minimum):
                    raise ValueError(
                        f"EML mapping {field}.minimum must be one finite "
                        "number."
                    )
            if has_maximum:
                maximum = _as_numeric(maximum)
                if maximum is None or not math.isfinite(maximum):
                    raise ValueError(
                        f"EML mapping {field}.maximum must be one finite "
                        "number."
                    )
            for bound in ("minimum", "maximum"):
                exclusive_field = bound + "_exclusive"
                exclusive = config.get(exclusive_field)
                if exclusive is not None and not isinstance(exclusive, bool):
                    raise ValueError(
                        f"EML mapping {field}.{exclusive_field} must be one "
                        "logical value."
                    )
                if exclusive is not None and config.get(bound) is None:
                    raise ValueError(
                        f"EML mapping {field}.{exclusive_field} requires "
                        f"{field}.{bound}."
                    )
            if (
                has_minimum
                and has_maximum
                and (
                    minimum > maximum
                    or (
                        minimum == maximum
                        and (
                            config.get("minimum_exclusive") is True
                            or config.get("maximum_exclusive") is True
                        )
                    )
                )
            ):
                raise ValueError(
                    f"EML mapping {field}.minimum must not exceed "
                    f"{field}.maximum or define an empty exclusive interval."
                )

        if scale == "dateTime":
            value_type = _as_character(dictionary.iloc[row]["value_type"])
            if value_type not in ("string", "integer", "number", "date", "datetime"):
                raise ValueError(
                    f"EML dateTime scale for {field} is incompatible with "
                    f"SDP value_type {value_type!r}."
                )
            format_string = _scalar(config, "format_string")
            if format_string not in ("YYYY", "YYYY-MM-DD"):
                raise ValueError(
                    f"EML mapping {field}.format_string must currently be "
                    '"YYYY" or "YYYY-MM-DD" so actual values can be '
                    "validated exactly."
                )

        if config.get("precision") is not None:
            precision = _as_numeric(config.get("precision"))
            if precision is None or precision <= 0:
                raise ValueError(
                    f"EML mapping {field}.precision must be a positive, "
                    "evidence-backed measurement repeatability value."
                )

    return configs


# --- canonical semantic targets ------------------------------------------------------

_ROLE_FIELDS = (
    ("term_iri", "variable"),
    ("property_iri", "property"),
    ("entity_iri", "entity"),
    ("constraint_iri", "constraint"),
    ("method_iri", "method"),
    ("unit_iri", "unit"),
)

_TARGET_FIELDS = (
    "dataset_id",
    "table_id",
    "column_name",
    "target_scope",
    "target_sdp_field",
    "dictionary_role",
    "iri",
)


def _measurement_rows(dictionary: pd.DataFrame) -> pd.DataFrame:
    mask = dictionary["column_role"].map(
        lambda value: not _is_missing(value) and str(value) == "measurement"
    )
    return dictionary[mask]


def _canonical_measurement_iris(dictionary: pd.DataFrame) -> List[str]:
    """Mirror ``.ms_eml_canonical_measurement_iris``."""
    measurement = _measurement_rows(dictionary)
    fields = (
        "term_iri",
        "property_iri",
        "entity_iri",
        "constraint_iri",
        "method_iri",
        "unit_iri",
    )
    iris: List[str] = []
    for field in fields:
        for iri in _split_iris(list(measurement[field])):
            if iri not in iris:
                iris.append(iri)
    return iris


def _canonical_review_targets(pkg: Dict[str, object]) -> List[Dict[str, str]]:
    """Mirror ``.ms_eml_canonical_review_targets`` (table targets first)."""
    dictionary = pkg["dictionary"]
    tables = pkg["tables"]
    targets: List[Dict[str, str]] = []

    for row in range(len(tables)):
        for iri in _split_iris(tables.iloc[row]["observation_unit_iri"]):
            targets.append(
                {
                    "dataset_id": _as_character(tables.iloc[row]["dataset_id"]),
                    "table_id": _as_character(tables.iloc[row]["table_id"]),
                    "column_name": "",
                    "target_scope": "table",
                    "target_sdp_field": "observation_unit_iri",
                    "dictionary_role": "entity",
                    "iri": iri,
                }
            )

    measurement = _measurement_rows(dictionary)
    for position in range(len(measurement)):
        row = measurement.iloc[position]
        for field, role in _ROLE_FIELDS:
            for iri in _split_iris(row[field]):
                targets.append(
                    {
                        "dataset_id": _as_character(row["dataset_id"]),
                        "table_id": _as_character(row["table_id"]),
                        "column_name": _as_character(row["column_name"]),
                        "target_scope": "column",
                        "target_sdp_field": field,
                        "dictionary_role": role,
                        "iri": iri,
                    }
                )

    distinct: List[Dict[str, str]] = []
    seen = set()
    for target in targets:
        key = tuple(target[field] for field in _TARGET_FIELDS)
        if key not in seen:
            seen.add(key)
            distinct.append(target)
    return distinct


def _file_sha256(path: Union[str, Path]) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _target_label(table_id: str, column_name: str, target_sdp_field: str) -> str:
    label = table_id
    if column_name:
        label += "." + column_name
    return label + "." + target_sdp_field


def _read_semantic_review(
    path: Path, pkg: Dict[str, object], mapping: dict
) -> pd.DataFrame:
    """Mirror ``.ms_eml_read_semantic_review`` (the exact hashed ledger gate)."""
    review_path = _resource_path(path, mapping["semantic_review"]["path"])
    actual_sha256 = _file_sha256(review_path)
    if actual_sha256 != mapping["semantic_review"]["sha256"]:
        raise ValueError(
            "The semantic-review ledger SHA-256 does not match the reviewed "
            "EML mapping sidecar."
        )

    review = _read_character_csv(review_path)
    required = [
        "dataset_id",
        "table_id",
        "column_name",
        "target_scope",
        "target_sdp_field",
        "dictionary_role",
        "decision",
        "confidence",
        "review_rationale",
        "iri",
    ]
    missing = [column for column in required if column not in review.columns]
    if missing:
        raise ValueError(
            "The semantic-review ledger is missing required column(s): "
            + ", ".join(missing)
            + "."
        )
    columns: Dict[str, List[str]] = {}
    for column in required:
        columns[column] = [
            "" if _is_missing(value) else _trim(_as_character(value))
            for value in review[column]
        ]
    row_count = len(review)
    nonempty_fields = [column for column in required if column != "column_name"]
    if any(
        not columns[column][row]
        for column in nonempty_fields
        for row in range(row_count)
    ):
        raise ValueError(
            "The semantic-review ledger must provide non-empty target, "
            "decision, and IRI fields on every row."
        )

    expected = _canonical_review_targets(pkg)
    unresolved_rows = [
        row for row in range(row_count) if columns["decision"][row] != "accepted"
    ]
    if unresolved_rows:
        unresolved_labels = list(
            dict.fromkeys(
                _target_label(
                    columns["table_id"][row],
                    columns["column_name"][row],
                    columns["target_sdp_field"][row],
                )
                for row in unresolved_rows
            )
        )
        unresolved_decisions = list(
            dict.fromkeys(columns["decision"][row] for row in unresolved_rows)
        )
        raise ValueError(
            "The semantic-review ledger contains non-accepted decision "
            + ", ".join(repr(value) for value in unresolved_decisions)
            + " for target(s) "
            + ", ".join(unresolved_labels)
            + "; a final ledger must contain accepted decisions only."
        )

    def target_key(values: Dict[str, str]) -> str:
        return "\r".join(values[field] for field in _TARGET_FIELDS)

    expected_keys = [target_key(target) for target in expected]
    review_keys = [
        target_key({field: columns[field][row] for field in _TARGET_FIELDS})
        for row in range(row_count)
    ]

    for target in expected:
        matches = [
            row
            for row in range(row_count)
            if all(columns[field][row] == target[field] for field in _TARGET_FIELDS)
        ]
        unresolved = list(
            dict.fromkeys(
                columns["decision"][row]
                for row in matches
                if columns["decision"][row] != "accepted"
            )
        )
        label = _target_label(
            target["table_id"], target["column_name"], target["target_sdp_field"]
        )
        if unresolved:
            raise ValueError(
                "The semantic-review ledger contains unresolved decision "
                + ", ".join(repr(value) for value in unresolved)
                + f" for required semantic target {label} and IRI "
                + target["iri"]
                + "."
            )
        if len(matches) != 1 or columns["decision"][matches[0]] != "accepted":
            raise ValueError(
                "The semantic-review ledger must contain exactly one "
                f"accepted row for required semantic target {label} and IRI "
                + target["iri"]
                + "."
            )

    unexpected = list(
        dict.fromkeys(key for key in review_keys if key not in expected_keys)
    )
    if (
        unexpected
        or row_count != len(expected)
        or len(set(review_keys)) != len(review_keys)
    ):
        unexpected_rows = [
            row for row in range(row_count) if review_keys[row] in unexpected
        ]
        if unexpected_rows:
            unexpected_labels = list(
                dict.fromkeys(
                    _target_label(
                        columns["table_id"][row],
                        columns["column_name"][row],
                        columns["target_sdp_field"][row],
                    )
                    + "="
                    + columns["iri"][row]
                    for row in unexpected_rows
                )
            )
        else:
            unexpected_labels = ["duplicate canonical target rows"]
        raise ValueError(
            "The final semantic-review ledger must equal the canonical "
            "non-empty table and measurement semantic target set exactly. "
            "Unexpected or duplicate row(s): "
            + ", ".join(unexpected_labels)
            + "."
        )
    return review


# --- reviewed vocabulary ---------------------------------------------------------

_VOCABULARY_SNAPSHOT_FIELDS = (
    "iri",
    "label",
    "definition",
    "source",
    "ontology",
    "resource_kind",
    "type_iris",
    "native_type",
    "source_url",
    "source_artifact_sha256",
)


def _vocabulary_snapshot_sha256(row: Dict[str, object]) -> str:
    """Mirror ``.ms_eml_vocabulary_snapshot_sha256``."""
    missing = [
        field for field in _VOCABULARY_SNAPSHOT_FIELDS if field not in row
    ]
    if missing:
        raise ValueError(
            "Cannot hash reviewed vocabulary snapshot; missing field(s): "
            + ", ".join(missing)
            + "."
        )
    values = [
        "" if _is_missing(row[field]) else _as_character(row[field])
        for field in _VOCABULARY_SNAPSHOT_FIELDS
    ]
    return hashlib.sha256("\r".join(values).encode("utf-8")).hexdigest()


def _read_vocabulary(
    path: Path, dictionary: pd.DataFrame, mapping: dict
) -> pd.DataFrame:
    """Mirror ``.ms_eml_read_vocabulary``."""
    vocabulary_path = _resource_path(path, mapping["semantic_vocabulary"]["path"])
    if not Path(vocabulary_path).exists():
        raise FileNotFoundError(
            f"Required reviewed vocabulary file {vocabulary_path} does not "
            "exist."
        )

    vocabulary = _read_character_csv(vocabulary_path)
    required = [
        "iri",
        "label",
        "definition",
        "source",
        "ontology",
        "resource_kind",
        "type_iris",
        "native_type",
        "source_url",
        "source_artifact_sha256",
        "reviewed_snapshot_sha256",
    ]
    missing = [column for column in required if column not in vocabulary.columns]
    if missing:
        raise ValueError(
            "semantic_vocabulary.csv is missing required column(s): "
            + ", ".join(missing)
            + "."
        )

    vocabulary = vocabulary.copy()
    for column in ("iri", "label"):
        vocabulary[column] = [
            value if _is_missing(value) else _trim(_as_character(value))
            for value in vocabulary[column]
        ]
    iris = list(vocabulary["iri"])
    if (
        any(_is_missing(value) for value in iris)
        or any(not value for value in iris if not _is_missing(value))
        or len(set(iris)) != len(iris)
    ):
        raise ValueError(
            "semantic_vocabulary.csv must contain one unique, non-empty row "
            "per IRI."
        )
    labels = list(vocabulary["label"])
    if any(_is_missing(value) or not value for value in labels):
        raise ValueError(
            "semantic_vocabulary.csv must provide a non-empty label for "
            "every IRI."
        )
    evidence_fields = (
        "definition",
        "source",
        "ontology",
        "resource_kind",
        "native_type",
        "source_url",
        "reviewed_snapshot_sha256",
    )
    for field in evidence_fields:
        values = [
            None if _is_missing(value) else _trim(_as_character(value))
            for value in vocabulary[field]
        ]
        if any(value is None or not value for value in values):
            raise ValueError(
                "semantic_vocabulary.csv must provide non-empty "
                f"{field} evidence for every IRI."
            )
    if any(
        not re.match(r"^https?://", _as_character(value))
        for value in vocabulary["source_url"]
    ):
        raise ValueError(
            "semantic_vocabulary.csv source_url values must be HTTP(S) URLs."
        )
    source_artifact_sha256 = [
        "" if _is_missing(value) else _trim(_as_character(value))
        for value in vocabulary["source_artifact_sha256"]
    ]
    if any(
        value and _SHA256_RE.fullmatch(value) is None
        for value in source_artifact_sha256
    ):
        raise ValueError(
            "semantic_vocabulary.csv non-empty source_artifact_sha256 "
            "values must be lowercase SHA-256 digests."
        )
    reviewed_snapshot_sha256 = [
        "" if _is_missing(value) else _trim(_as_character(value))
        for value in vocabulary["reviewed_snapshot_sha256"]
    ]
    if any(
        _SHA256_RE.fullmatch(value) is None for value in reviewed_snapshot_sha256
    ):
        raise ValueError(
            "semantic_vocabulary.csv reviewed_snapshot_sha256 values must "
            "be lowercase SHA-256 digests."
        )
    expected_snapshot_sha256 = [
        _vocabulary_snapshot_sha256(
            {
                column: vocabulary.iloc[row][column]
                for column in vocabulary.columns
            }
        )
        for row in range(len(vocabulary))
    ]
    if reviewed_snapshot_sha256 != expected_snapshot_sha256:
        raise ValueError(
            "semantic_vocabulary.csv contains a reviewed vocabulary "
            "snapshot hash that does not match its row."
        )
    actual_sha256 = _file_sha256(vocabulary_path)
    if actual_sha256 != mapping["semantic_vocabulary"]["sha256"]:
        raise ValueError(
            "semantic_vocabulary.csv SHA-256 does not match the reviewed "
            "EML mapping sidecar."
        )

    expected = sorted(_canonical_measurement_iris(dictionary))
    actual = sorted(set(iris))
    if expected != actual:
        raise ValueError(
            "semantic_vocabulary.csv must describe exactly the canonical "
            "measurement IRI set. Missing: "
            + ", ".join(sorted(set(expected) - set(actual)))
            + ". Unexpected: "
            + ", ".join(sorted(set(actual) - set(expected)))
            + "."
        )
    return vocabulary


def _vocabulary_label(vocabulary: pd.DataFrame, iri: object) -> str:
    """Mirror ``.ms_eml_vocabulary_label``."""
    matches = vocabulary[vocabulary["iri"] == iri]
    if len(matches) != 1:
        raise ValueError(
            f"No reviewed vocabulary label exists for canonical IRI {iri}."
        )
    label = matches.iloc[0]["label"]
    if _is_missing(label) or not str(label):
        raise ValueError(
            f"No reviewed vocabulary label exists for canonical IRI {iri}."
        )
    return str(label)


def _measurement_term_annotation(
    dictionary_row: Dict[str, object],
    vocabulary: Optional[pd.DataFrame] = None,
) -> Dict[str, str]:
    """Mirror ``.ms_eml_measurement_term_annotation``."""
    raw_term_type = dictionary_row.get("term_type")
    term_type = _trim(_as_character(raw_term_type)).lower()
    if term_type not in ("owl_class", "skos_concept"):
        raise ValueError(
            "EML export requires measurement term_type to be "
            f'"owl_class" or "skos_concept"; found {term_type!r}.'
        )

    if vocabulary is not None:
        term_iri = _trim(_as_character(dictionary_row.get("term_iri")))
        vocabulary_rows = vocabulary[
            vocabulary["iri"].map(
                lambda value: not _is_missing(value) and value == term_iri
            )
        ]
        if len(vocabulary_rows) != 1:
            raise ValueError(
                "Reviewed vocabulary evidence for measurement term "
                f"{term_iri} is missing or duplicated."
            )
        vocabulary_row = vocabulary_rows.iloc[0]

        def evidence_value(field: str) -> str:
            value = vocabulary_row[field]
            # R: paste() renders NA cells as the literal string "NA".
            return "NA" if _is_missing(value) else str(value)

        type_evidence = " ".join(
            [
                evidence_value("native_type"),
                evidence_value("resource_kind"),
                evidence_value("type_iris"),
            ]
        ).lower()
        evidence_is_skos = bool(
            re.search(r"skos[:/#].*concept|\bconcept\b", type_evidence)
        )
        evidence_is_owl = bool(
            re.search(r"owl[:/#].*class|\bclass\b", type_evidence)
        )
        if (
            (term_type == "skos_concept" and not evidence_is_skos)
            or (term_type == "owl_class" and not evidence_is_owl)
            or (evidence_is_skos and evidence_is_owl)
        ):
            raise ValueError(
                f"Measurement term_type for {term_iri} conflicts with "
                "reviewed vocabulary native-type evidence."
            )

    return {
        "iri": _MEASUREMENT_PREDICATES["variable_topic"],
        "label": "Subject",
    }


# --- data objects -----------------------------------------------------------------


def _resource_path(package_path: Union[str, Path], file_name: str) -> str:
    """Mirror ``.ms_eml_resource_path`` (must resolve inside the package)."""
    package_root = os.path.realpath(str(package_path))
    candidate = os.path.join(package_root, str(file_name))
    if not os.path.exists(candidate):
        raise FileNotFoundError(f"SDP data object {candidate} does not exist.")
    resolved = os.path.realpath(candidate)
    prefix = package_root + os.sep
    if not resolved.startswith(prefix):
        raise ValueError(
            f"SDP resource {file_name} resolves outside the package "
            "directory."
        )
    return resolved


def _data_objects(
    path: Union[str, Path], pkg: Dict[str, object], mapping: dict
) -> pd.DataFrame:
    """Mirror ``.ms_eml_data_objects`` (deterministic content-bound PIDs)."""
    tables = pkg["tables"]
    rows = []
    for row in range(len(tables)):
        table_id = _as_character(tables.iloc[row]["table_id"])
        file_name = _as_character(tables.iloc[row]["file_name"])
        file_path = _resource_path(path, file_name)
        checksum = _file_sha256(file_path)
        pid = "urn:uuid:" + _uuid5(
            ":".join(
                [
                    "data",
                    _as_character(mapping["dataset_id"]),
                    table_id,
                    os.path.basename(file_name),
                    checksum,
                ]
            )
        )
        rows.append(
            {
                "table_id": table_id,
                "file_name": file_name,
                "path": file_path,
                "pid": pid,
                "format_id": "text/csv",
                "checksum_algorithm": "SHA-256",
                "checksum": checksum,
                "size": os.path.getsize(file_path),
            }
        )
    return pd.DataFrame(
        rows,
        columns=[
            "table_id",
            "file_name",
            "path",
            "pid",
            "format_id",
            "checksum_algorithm",
            "checksum",
            "size",
        ],
    )


_SUPPLEMENTARY_COLUMNS = (
    "path",
    "pid",
    "format_id",
    "checksum_algorithm",
    "checksum",
    "size",
    "object_name",
    "entity_name",
    "description",
    "compression_method",
    "entity_type",
    "online_url",
)


def _empty_supplementary_objects() -> pd.DataFrame:
    return pd.DataFrame(columns=list(_SUPPLEMENTARY_COLUMNS))


def _supplementary_objects(objects: object) -> pd.DataFrame:
    """Mirror ``.ms_eml_supplementary_objects``.

    Accepts ``None``, a DataFrame, or a dict of columns (the Pythonic
    analogue of an in-memory data frame, as elsewhere in this package).
    """
    if objects is None:
        return _empty_supplementary_objects()
    if isinstance(objects, dict):
        objects = pd.DataFrame(objects)
    if not isinstance(objects, pd.DataFrame):
        raise ValueError(
            "supplementary_objects must be a data frame with one row per "
            "supplementary object."
        )
    if len(objects) == 0:
        return _empty_supplementary_objects()

    required = (
        "path",
        "pid",
        "format_id",
        "checksum",
        "object_name",
        "entity_name",
        "description",
    )
    allowed = required + ("size", "compression_method", "entity_type")
    missing = [column for column in required if column not in objects.columns]
    unexpected = [column for column in objects.columns if column not in allowed]
    if missing:
        raise ValueError(
            "supplementary_objects is missing required column(s): "
            + ", ".join(missing)
            + "."
        )
    if unexpected:
        raise ValueError(
            "supplementary_objects has unexpected column(s): "
            + ", ".join(unexpected)
            + "."
        )

    values: Dict[str, List[str]] = {}
    for field in required:
        cells = []
        for value in objects[field]:
            if isinstance(value, (list, tuple, dict, set)):
                raise ValueError(
                    f"Supplementary-object {field} must be one atomic value "
                    "per row."
                )
            cells.append(
                None if _is_missing(value) else _trim(_as_character(value))
            )
        values[field] = cells
    if any(
        value is None or not value or _CONTROL_CHAR_RE.search(value)
        for field in required
        for value in values[field]
    ):
        raise ValueError(
            "Every required supplementary-object field must contain a "
            "non-empty value without control characters."
        )

    if any(_ABSOLUTE_URI_RE.fullmatch(value) is None for value in values["pid"]):
        raise ValueError(
            "Every supplementary-object pid must be an absolute URI without "
            "whitespace."
        )
    if any(
        _SHA256_RE.fullmatch(value) is None for value in values["checksum"]
    ):
        raise ValueError(
            "Every supplementary-object checksum must be a lowercase "
            "SHA-256 digest."
        )

    # The expanded representation names each artifact by its package-relative
    # path, so ``object_name`` is no longer a basename. It must still be a
    # *safe* relative path: absolute, drive-rooted, doubled-separator, empty,
    # ``.`` and ``..`` segments are all refused, because a consumer
    # reconstructing the SDP from these names must not be able to write
    # outside it.
    slash_names = [name.replace("\\", "/") for name in values["object_name"]]
    unsafe_names = [
        name
        for name in slash_names
        if name.startswith("/")
        or re.match(r"^[A-Za-z]:/", name)
        or "//" in name
        or any(part in ("", ".", "..") for part in name.split("/"))
    ]
    if unsafe_names:
        raise ValueError(
            "Every supplementary-object object_name must be a safe relative "
            "object path."
        )
    archive = [value == "application/zip" for value in values["format_id"]]
    invalid_archive_names = [
        name
        for name, is_archive in zip(slash_names, archive)
        if is_archive
        and ("/" in name or not re.search(r"\.zip$", name, flags=re.IGNORECASE))
    ]
    if invalid_archive_names:
        raise ValueError(
            'An "application/zip" supplementary-object object_name must be a '
            "basename ending in .zip."
        )
    if len(set(values["pid"])) != len(values["pid"]) or len(
        set(values["object_name"])
    ) != len(values["object_name"]):
        raise ValueError(
            "Supplementary-object pid and object_name values must each be "
            "unique."
        )

    paths: List[str] = []
    for candidate in values["path"]:
        candidate = os.path.expanduser(candidate)
        if not os.path.exists(candidate) or os.path.isdir(candidate):
            raise ValueError(
                f"Supplementary object {candidate} is not a readable file."
            )
        paths.append(os.path.realpath(candidate))
    actual_sizes = [os.path.getsize(path) for path in paths]

    if "size" in objects.columns:
        supplied_sizes: List[Optional[float]] = []
        for value in objects["size"]:
            if isinstance(value, (list, tuple, dict, set)):
                raise ValueError(
                    "Supplementary-object size must be one atomic value per "
                    "row."
                )
            supplied_sizes.append(_as_numeric(value))
        invalid_size = any(
            value is None
            or not math.isfinite(value)
            or value < 0
            or value != math.floor(value)
            for value in supplied_sizes
        )
        if invalid_size or any(
            supplied != actual
            for supplied, actual in zip(supplied_sizes, actual_sizes)
        ):
            raise ValueError(
                "Supplementary-object size must exactly match the file size "
                "in bytes."
            )

    actual_checksums = [_file_sha256(path) for path in paths]
    mismatched = [
        values["object_name"][row]
        for row in range(len(paths))
        if actual_checksums[row] != values["checksum"][row]
    ]
    if mismatched:
        raise ValueError(
            "Supplementary-object SHA-256 does not match file bytes for "
            + ", ".join(mismatched)
            + "."
        )

    # ``compressionMethod`` describes a container, so only a ZIP may declare
    # one. An expanded artifact that claimed ``zip`` would tell a consumer to
    # unpack a plain CSV.
    if "compression_method" in objects.columns:
        compression_method: List[Optional[str]] = []
        for value in objects["compression_method"]:
            text = None if _is_missing(value) else _trim(_as_character(value))
            compression_method.append(text or None)
    else:
        compression_method = [
            "zip" if is_archive else None for is_archive in archive
        ]
    if any(
        (is_archive and method != "zip") or (not is_archive and method is not None)
        for is_archive, method in zip(archive, compression_method)
    ):
        raise ValueError(
            'Only "application/zip" supplementary objects may declare '
            "compression_method = zip."
        )

    if "entity_type" in objects.columns:
        entity_type: List[str] = []
        for value in objects["entity_type"]:
            text = None if _is_missing(value) else _trim(_as_character(value))
            if not text or _CONTROL_CHAR_RE.search(text):
                raise ValueError(
                    "Every supplementary-object entity_type must be non-empty "
                    "and contain no control characters."
                )
            entity_type.append(text)
    else:
        entity_type = [
            "Salmon Data Package archive"
            if is_archive
            else "Salmon Data Package artifact"
            for is_archive in archive
        ]

    result = pd.DataFrame(
        {
            "path": paths,
            "pid": values["pid"],
            "format_id": values["format_id"],
            "checksum_algorithm": "SHA-256",
            "checksum": values["checksum"],
            "size": actual_sizes,
            "object_name": slash_names,
            "entity_name": values["entity_name"],
            "description": values["description"],
            "compression_method": compression_method,
            "entity_type": entity_type,
            "online_url": [_knb_object_url(pid) for pid in values["pid"]],
        },
        columns=list(_SUPPLEMENTARY_COLUMNS),
    )
    # Deterministic order: object_name then pid, codepoint order (matches
    # dplyr::arrange's radix/C collation; locale.strxfrm stays banned).
    return result.sort_values(
        ["object_name", "pid"], kind="mergesort", ignore_index=True
    )


# --- coverage / codes / attribute building ----------------------------------------


def _add_coverage(
    dataset: ET.Element, dataset_meta: pd.DataFrame, mapping: dict
) -> Optional[ET.Element]:
    """Mirror ``.ms_eml_add_coverage``."""
    temporal_start = dataset_meta.iloc[0]["temporal_start"]
    temporal_end = dataset_meta.iloc[0]["temporal_end"]
    geographic = mapping.get("geographic_coverage")
    has_geographic = isinstance(geographic, dict)
    taxon = mapping.get("taxonomic_coverage")
    has_taxon = isinstance(taxon, dict) and _nonempty(taxon.get("scientific_name"))

    if (
        not has_geographic
        and not _nonempty(temporal_start)
        and not _nonempty(temporal_end)
        and not has_taxon
    ):
        return None

    coverage = ET.SubElement(dataset, "coverage")
    if has_geographic:
        geographic_node = ET.SubElement(coverage, "geographicCoverage")
        _add_text(
            geographic_node, "geographicDescription", geographic.get("description")
        )
        bounding = ET.SubElement(geographic_node, "boundingCoordinates")
        _add_text(
            bounding,
            "westBoundingCoordinate",
            _as_character(geographic.get("west")),
        )
        _add_text(
            bounding,
            "eastBoundingCoordinate",
            _as_character(geographic.get("east")),
        )
        _add_text(
            bounding,
            "northBoundingCoordinate",
            _as_character(geographic.get("north")),
        )
        _add_text(
            bounding,
            "southBoundingCoordinate",
            _as_character(geographic.get("south")),
        )

    if _nonempty(temporal_start) or _nonempty(temporal_end):
        if not _nonempty(temporal_start) or not _nonempty(temporal_end):
            raise ValueError(
                "EML temporal coverage requires both temporal_start and "
                "temporal_end."
            )
        temporal = ET.SubElement(coverage, "temporalCoverage")
        date_range = ET.SubElement(temporal, "rangeOfDates")
        begin = ET.SubElement(date_range, "beginDate")
        _add_text(begin, "calendarDate", _as_character(temporal_start))
        end = ET.SubElement(date_range, "endDate")
        _add_text(end, "calendarDate", _as_character(temporal_end))

    if has_taxon:
        taxonomic = ET.SubElement(coverage, "taxonomicCoverage")
        classification = ET.SubElement(taxonomic, "taxonomicClassification")
        _add_text(
            classification,
            "taxonRankName",
            _scalar(taxon, "rank", required=False),
        )
        _add_text(
            classification, "taxonRankValue", _scalar(taxon, "scientific_name")
        )
        _add_text(
            classification,
            "commonName",
            _scalar(taxon, "common_name", required=False),
        )
    return coverage


def _code_rows(
    pkg: Dict[str, object], table_id: str, column_name: str
) -> pd.DataFrame:
    """Mirror ``.ms_eml_code_rows``."""
    codes = pkg.get("codes")
    if codes is None or not isinstance(codes, pd.DataFrame) or len(codes) == 0:
        return pd.DataFrame()
    mask = [
        _as_character(codes.iloc[row]["table_id"]) == table_id
        and _as_character(codes.iloc[row]["column_name"]) == column_name
        for row in range(len(codes))
    ]
    return codes[pd.Series(mask, index=codes.index)]


def _add_non_numeric_domain(
    scale_node: ET.Element,
    config: dict,
    dictionary_row: Dict[str, object],
    pkg: Dict[str, object],
) -> ET.Element:
    """Mirror ``.ms_eml_add_non_numeric_domain``."""
    domain = ET.SubElement(scale_node, "nonNumericDomain")
    table_id = _as_character(dictionary_row["table_id"])
    column_name = _as_character(dictionary_row["column_name"])
    codes = _code_rows(pkg, table_id, column_name)

    if len(codes) == 0:
        text_domain = ET.SubElement(domain, "textDomain")
        _add_text(
            text_domain,
            "definition",
            "Values documented by the "
            + column_name
            + " attribute definition.",
        )
        return domain

    enumerated = ET.SubElement(domain, "enumeratedDomain")
    scale = _scalar(config, "measurement_scale")
    order_map = config.get("code_order")
    if scale == "ordinal":
        if not isinstance(order_map, dict) or not order_map:
            raise ValueError(
                f"Ordinal EML attribute {table_id}.{column_name} requires "
                "named code_order values."
            )
        code_values = [
            _as_character(value) for value in codes["code_value"]
        ]
        if set(code_values) != set(str(key) for key in order_map.keys()):
            raise ValueError(
                f"Ordinal code_order for {table_id}.{column_name} must name "
                "exactly the SDP code values."
            )

    for row in range(len(codes)):
        value = _as_character(codes.iloc[row]["code_value"])
        label = codes.iloc[row]["code_label"]
        description = codes.iloc[row]["code_description"]
        if _nonempty(description):
            definition = _as_character(description)
        elif _nonempty(label):
            definition = _as_character(label)
        else:
            definition = "Code value " + value
        code = ET.SubElement(enumerated, "codeDefinition")
        if scale == "ordinal":
            order = _as_integer(order_map.get(value))
            if order is None:
                raise ValueError(
                    f"Ordinal order for code {value!r} in "
                    f"{table_id}.{column_name} must be an integer."
                )
            code.set("order", str(order))
        _add_text(code, "code", value)
        _add_text(code, "definition", definition)
        if _nonempty(codes.iloc[row]["vocabulary_iri"]):
            _add_text(code, "source", codes.iloc[row]["vocabulary_iri"])
    return domain


def _add_measurement_scale(
    attribute: ET.Element,
    config: dict,
    dictionary_row: Dict[str, object],
    pkg: Dict[str, object],
) -> ET.Element:
    """Mirror ``.ms_eml_add_measurement_scale``."""
    measurement_scale = ET.SubElement(attribute, "measurementScale")
    scale = _scalar(config, "measurement_scale")
    scale_node = ET.SubElement(measurement_scale, scale)

    if scale in ("nominal", "ordinal"):
        _add_non_numeric_domain(scale_node, config, dictionary_row, pkg)
    elif scale in ("interval", "ratio"):
        unit = ET.SubElement(scale_node, "unit")
        _add_text(unit, "standardUnit", config.get("eml_unit"))
        if config.get("precision") is not None:
            _add_text(scale_node, "precision", _as_character(config["precision"]))
        numeric_domain = ET.SubElement(scale_node, "numericDomain")
        _add_text(numeric_domain, "numberType", config.get("number_type"))

        minimum = config.get("minimum")
        maximum = config.get("maximum")
        has_minimum = minimum is not None and not _is_missing(minimum)
        has_maximum = maximum is not None and not _is_missing(maximum)
        if has_minimum or has_maximum:
            bounds = ET.SubElement(numeric_domain, "bounds")
            if has_minimum:
                exclusive = config.get("minimum_exclusive") is True
                _add_text(
                    bounds,
                    "minimum",
                    _as_character(minimum),
                    attrs={"exclusive": "true" if exclusive else "false"},
                )
            if has_maximum:
                exclusive = config.get("maximum_exclusive") is True
                _add_text(
                    bounds,
                    "maximum",
                    _as_character(maximum),
                    attrs={"exclusive": "true" if exclusive else "false"},
                )
    elif scale == "dateTime":
        _add_text(scale_node, "formatString", config.get("format_string"))

    return measurement_scale


def _missing_values(config: dict) -> List[object]:
    """Mirror ``.ms_eml_missing_values``."""
    values = config.get("missing_values")
    if values is None:
        return []
    if isinstance(values, dict) and _nonempty(values.get("code")):
        values = [values]
    elif isinstance(values, dict):
        values = list(values.values())
    if not isinstance(values, list):
        raise ValueError(
            "EML missing_values must be a list of code/explanation mappings."
        )
    return values


def _missing_value_entry_scalar(value: object, field: str, name: str) -> str:
    if not isinstance(value, dict):
        raise ValueError(
            f"Each missing_values entry for {field} must be a mapping."
        )
    return _scalar(value, name)


def _read_raw_csv_tokens(path: str, table_id: str) -> pd.DataFrame:
    """Mirror ``.ms_eml_read_raw_csv_tokens`` (exact tokens, nothing missing)."""
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        try:
            records = [row for row in csv.reader(handle) if row]
        except csv.Error as error:
            raise ValueError(
                f"Could not audit the exact CSV tokens for EML table "
                f"{table_id!r}. The first parse problem is: {error}."
            ) from None
    if not records:
        return pd.DataFrame()
    header = records[0]
    width = len(header)
    for index, row in enumerate(records[1:], start=1):
        if len(row) != width:
            raise ValueError(
                f"Could not audit the exact CSV tokens for EML table "
                f"{table_id!r}. The first parse problem is at row {index}: "
                f"expected {width} field(s) but found {len(row)}."
            )
    return pd.DataFrame(records[1:], columns=header, dtype=object)


def _validate_raw_table(
    raw: pd.DataFrame, parsed: pd.DataFrame, table_id: str
) -> pd.DataFrame:
    """Mirror ``.ms_eml_validate_raw_table``."""
    raw_names = [str(name) for name in raw.columns]
    parsed_names = [str(name) for name in parsed.columns]
    if raw_names != parsed_names:
        raise ValueError(
            f"Raw-token audit and parsed SDP table {table_id!r} have "
            f"different columns. Raw CSV: {raw_names}. Parsed SDP: "
            f"{parsed_names}."
        )
    if len(raw) != len(parsed):
        raise ValueError(
            f"Raw-token audit found {len(raw)} row(s) for EML table "
            f"{table_id!r}, but the parsed SDP resource has {len(parsed)}."
        )
    return raw


def _era_parsed_missing(raw_values: List[str]) -> List[bool]:
    """Missingness of the parsed resource under readr's era defaults.

    metasalmon 0.1.7 reads package resources with ``readr::read_csv``
    defaults, so a parsed cell is missing exactly when its raw token is ""
    or "NA" **after readr's ``trim_ws = TRUE`` has run**. Deriving this from
    the raw tokens keeps the missing-value contract identical to R 0.1.7
    instead of inheriting pandas' larger default NA vocabulary.

    R computes missingness from the parsed frame, so it is the *trimmed*
    token that decides: a cell of three spaces and a cell of ``" NA "`` both
    parse to ``NA`` in R and are therefore undeclared missing tokens. Matching
    the untrimmed token let those cells through here while R rejected them.
    """
    return [_trim(value) in _ERA_NA_TOKENS for value in raw_values]


def _add_missing_values(
    attribute: ET.Element,
    config: dict,
    parsed_values: List[object],
    raw_values: List[str],
    field: str,
) -> ET.Element:
    """Mirror ``.ms_eml_add_missing_values``."""
    values = _missing_values(config)
    if len(parsed_values) != len(raw_values):
        raise ValueError(
            "Internal EML export error: parsed and raw values differ in "
            f"length for {field}."
        )

    codes: List[str] = []
    for value in values:
        codes.append(_missing_value_entry_scalar(value, field, "code"))
    duplicates = list(
        dict.fromkeys(code for code in codes if codes.count(code) > 1)
    )
    if duplicates:
        raise ValueError(
            f"EML attribute {field} declares duplicate missing-value "
            "code(s): " + ", ".join(repr(code) for code in duplicates) + "."
        )

    absent = [code for code in codes if code not in set(raw_values)]
    if absent:
        raise ValueError(
            f"EML attribute {field} declares missing-value code(s) "
            + ", ".join(repr(code) for code in absent)
            + " that do(es) not occur in the raw CSV bytes."
        )

    parsed_missing = _era_parsed_missing(raw_values)
    declared_but_present = list(
        dict.fromkeys(
            raw_values[row]
            for row in range(len(raw_values))
            if raw_values[row] in codes and not parsed_missing[row]
        )
    )
    if declared_but_present:
        raise ValueError(
            f"EML attribute {field} declares missing-value code(s) "
            + ", ".join(repr(code) for code in declared_but_present)
            + " where the parsed value is not missing."
        )
    undeclared = list(
        dict.fromkeys(
            raw_values[row]
            for row in range(len(raw_values))
            if parsed_missing[row]
            and raw_values[row]
            and raw_values[row] not in codes
        )
    )
    if undeclared:
        raise ValueError(
            f"EML attribute {field} contains undeclared non-empty missing "
            "token(s): "
            + ", ".join(repr(token) for token in undeclared)
            + ". Empty CSV fields are treated as implicit absence and do "
            "not require a fabricated EML missing-value code."
        )

    for value in values:
        code = _missing_value_entry_scalar(value, field, "code")
        explanation = _missing_value_entry_scalar(value, field, "explanation")
        node = ET.SubElement(attribute, "missingValueCode")
        _add_text(node, "code", code)
        _add_text(node, "codeExplanation", explanation)
    return attribute


def _validate_observed_domain(
    config: dict,
    dictionary_row: Dict[str, object],
    parsed_values: List[object],
    raw_values: List[str],
    pkg: Dict[str, object],
    field: str,
) -> None:
    """Mirror ``.ms_eml_validate_observed_domain``."""
    scale = _scalar(config, "measurement_scale")
    missing_codes = [
        _missing_value_entry_scalar(value, field, "code")
        for value in _missing_values(config)
    ]
    observed = [
        bool(raw_values[row]) and raw_values[row] not in missing_codes
        for row in range(len(raw_values))
    ]

    if scale in ("nominal", "ordinal"):
        table_id = _as_character(dictionary_row["table_id"])
        column_name = _as_character(dictionary_row["column_name"])
        codes = _code_rows(pkg, table_id, column_name)
        if len(codes) > 0:
            code_values = [_as_character(value) for value in codes["code_value"]]
            exact_tokens = list(
                dict.fromkeys(
                    raw_values[row]
                    for row in range(len(raw_values))
                    if observed[row]
                )
            )
            undeclared = [
                token for token in exact_tokens if token not in code_values
            ]
            if undeclared:
                raise ValueError(
                    f"EML enumerated domain for {field} does not contain "
                    "exact raw CSV token(s): "
                    + ", ".join(repr(token) for token in undeclared)
                    + ". Code values are lexically significant after CSV "
                    "parsing; leading or trailing whitespace must not be "
                    "normalized silently."
                )

    if scale in ("interval", "ratio"):
        tokens = [
            raw_values[row] for row in range(len(raw_values)) if observed[row]
        ]
        numeric_values = [_as_numeric(token) for token in tokens]
        offending_tokens = list(
            dict.fromkeys(
                tokens[index]
                for index in range(len(tokens))
                if numeric_values[index] is None
                or not math.isfinite(numeric_values[index])
            )
        )
        if offending_tokens:
            raise ValueError(
                f"EML numeric domain for {field} contains non-numeric or "
                "non-finite observed value(s): "
                + ", ".join(repr(token) for token in offending_tokens)
                + "."
            )

        number_type = _scalar(config, "number_type")
        non_integer = [
            value for value in numeric_values if value != math.floor(value)
        ]
        if number_type in ("natural", "whole", "integer") and non_integer:
            offending = list(
                dict.fromkeys(_as_character(value) for value in non_integer)
            )
            raise ValueError(
                f"EML {number_type!r} number type for {field} requires "
                "integer-valued observations, but found "
                + ", ".join(offending)
                + "."
            )
        if number_type == "natural" and any(
            value <= 0 for value in numeric_values
        ):
            offending = list(
                dict.fromkeys(
                    _as_character(value)
                    for value in numeric_values
                    if value <= 0
                )
            )
            raise ValueError(
                f'EML "natural" number type for {field} requires strictly '
                "positive observations, but found "
                + ", ".join(offending)
                + "."
            )
        if number_type == "whole" and any(value < 0 for value in numeric_values):
            offending = list(
                dict.fromkeys(
                    _as_character(value)
                    for value in numeric_values
                    if value < 0
                )
            )
            raise ValueError(
                f'EML "whole" number type for {field} requires nonnegative '
                "observations, but found " + ", ".join(offending) + "."
            )

        if config.get("minimum") is not None:
            minimum = _as_numeric(config["minimum"])
            exclusive = config.get("minimum_exclusive") is True
            violating = [
                value
                for value in numeric_values
                if (value <= minimum if exclusive else value < minimum)
            ]
            if violating:
                offending = list(
                    dict.fromkeys(_as_character(value) for value in violating)
                )
                qualifier = "exclusive minimum" if exclusive else "minimum"
                raise ValueError(
                    f"EML numeric domain for {field} has observed value(s) "
                    + ", ".join(offending)
                    + f" outside {qualifier} {_as_character(minimum)}."
                )
        if config.get("maximum") is not None:
            maximum = _as_numeric(config["maximum"])
            exclusive = config.get("maximum_exclusive") is True
            violating = [
                value
                for value in numeric_values
                if (value >= maximum if exclusive else value > maximum)
            ]
            if violating:
                offending = list(
                    dict.fromkeys(_as_character(value) for value in violating)
                )
                qualifier = "exclusive maximum" if exclusive else "maximum"
                raise ValueError(
                    f"EML numeric domain for {field} has observed value(s) "
                    + ", ".join(offending)
                    + f" outside {qualifier} {_as_character(maximum)}."
                )

    if scale == "dateTime":
        format_string = _scalar(config, "format_string")
        tokens = [
            raw_values[row] for row in range(len(raw_values)) if observed[row]
        ]
        if format_string == "YYYY":
            invalid = [
                token
                for token in tokens
                if re.fullmatch(r"[0-9]{4}", token) is None
            ]
        else:
            invalid = []
            for token in tokens:
                if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", token) is None:
                    invalid.append(token)
                    continue
                try:
                    parsed_date = datetime.strptime(token, "%Y-%m-%d")
                except ValueError:
                    invalid.append(token)
                    continue
                # ``date.isoformat()`` rather than ``strftime`` because the
                # latter's year is not zero-padded by every C library, which
                # would reject a valid pre-1000 calendar value on Linux only.
                if parsed_date.date().isoformat() != token:
                    invalid.append(token)
        if invalid:
            offending = list(dict.fromkeys(invalid))
            raise ValueError(
                f"EML {field}.format_string {format_string!r} does not "
                "match observed calendar value(s): "
                + ", ".join(repr(token) for token in offending)
                + "."
            )


def _add_annotation(
    parent: ET.Element,
    predicate: str,
    predicate_label: str,
    value: str,
    value_label: str,
) -> ET.Element:
    """Mirror ``.ms_eml_add_annotation``."""
    annotation = ET.SubElement(parent, "annotation")
    _add_text(annotation, "propertyURI", predicate, attrs={"label": predicate_label})
    _add_text(annotation, "valueURI", value, attrs={"label": value_label})
    return annotation


def _add_attribute(
    attribute_list: ET.Element,
    dictionary_row: Dict[str, object],
    config: dict,
    data: pd.DataFrame,
    raw_data: pd.DataFrame,
    pkg: Dict[str, object],
    vocabulary: pd.DataFrame,
    dataset_id: str,
) -> ET.Element:
    """Mirror ``.ms_eml_add_attribute``."""
    table_id = _as_character(dictionary_row["table_id"])
    column_name = _as_character(dictionary_row["column_name"])
    field = table_id + "." + column_name
    attribute = ET.SubElement(attribute_list, "attribute")
    attribute.set("id", _attribute_id(dataset_id, table_id, column_name))
    _add_text(attribute, "attributeName", column_name)
    _add_text(attribute, "attributeLabel", dictionary_row.get("column_label"))
    _add_text(
        attribute, "attributeDefinition", dictionary_row.get("column_description")
    )

    value_type = _as_character(dictionary_row.get("value_type"))
    if value_type not in _STORAGE_TYPES:
        raise ValueError(
            f"EML export does not support SDP value_type {value_type!r}."
        )
    _add_text(attribute, "storageType", _STORAGE_TYPES[value_type])

    parsed_values = list(data[column_name])
    raw_values = [str(value) for value in raw_data[column_name]]
    _validate_observed_domain(
        config, dictionary_row, parsed_values, raw_values, pkg, field
    )
    _add_measurement_scale(attribute, config, dictionary_row, pkg)
    _add_missing_values(attribute, config, parsed_values, raw_values, field)

    if _as_character(dictionary_row.get("column_role")) == "measurement":
        term_iri = _as_character(dictionary_row.get("term_iri"))
        unit_iri = _as_character(dictionary_row.get("unit_iri"))
        term_annotation = _measurement_term_annotation(dictionary_row, vocabulary)
        _add_annotation(
            attribute,
            term_annotation["iri"],
            term_annotation["label"],
            term_iri,
            _vocabulary_label(vocabulary, term_iri),
        )
        _add_annotation(
            attribute,
            _MEASUREMENT_PREDICATES["unit"],
            "has unit",
            unit_iri,
            _vocabulary_label(vocabulary, unit_iri),
        )
    return attribute


def _add_primary_key(
    data_table: ET.Element,
    table_row: Dict[str, object],
    dictionary: pd.DataFrame,
    dataset_id: str,
) -> Optional[ET.Element]:
    """Mirror ``.ms_eml_add_primary_key``."""
    primary_key = table_row.get("primary_key")
    if not _nonempty(primary_key):
        return None
    columns = [
        _trim(piece) for piece in _as_character(primary_key).split(",")
    ]
    table_id = _as_character(table_row.get("table_id"))
    known = [
        _as_character(dictionary.iloc[row]["column_name"])
        for row in range(len(dictionary))
        if _as_character(dictionary.iloc[row]["table_id"]) == table_id
    ]
    unknown = [column for column in columns if column not in known]
    if unknown:
        raise ValueError(
            f"EML primary key for table {table_id!r} names unknown "
            "column(s): " + ", ".join(unknown) + "."
        )

    constraint = ET.SubElement(data_table, "constraint")
    key_node = ET.SubElement(constraint, "primaryKey")
    _add_text(key_node, "constraintName", "PrimaryKey_" + table_id)
    key = ET.SubElement(key_node, "key")
    for column in columns:
        _add_text(
            key, "attributeReference", _attribute_id(dataset_id, table_id, column)
        )
    return constraint


def _add_not_null(
    data_table: ET.Element,
    table_id: str,
    dictionary: pd.DataFrame,
    dataset_id: str,
) -> Optional[ET.Element]:
    """Mirror ``.ms_eml_add_not_null``."""
    columns = []
    for row in range(len(dictionary)):
        if _as_character(dictionary.iloc[row]["table_id"]) != table_id:
            continue
        required = dictionary.iloc[row]["required"]
        if not _is_missing(required) and bool(required):
            columns.append(_as_character(dictionary.iloc[row]["column_name"]))
    if not columns:
        return None

    constraint = ET.SubElement(data_table, "constraint")
    not_null = ET.SubElement(constraint, "notNullConstraint")
    _add_text(not_null, "constraintName", "NotNull_" + table_id)
    key = ET.SubElement(not_null, "key")
    for column in columns:
        _add_text(
            key, "attributeReference", _attribute_id(dataset_id, table_id, column)
        )
    return constraint


def _add_supplementary_objects(
    dataset: ET.Element, objects: pd.DataFrame, mapping: dict
) -> None:
    """Mirror ``.ms_eml_add_supplementary_objects``."""
    if len(objects) == 0:
        return

    for row in range(len(objects)):
        item = objects.iloc[row]
        id_key = ":".join([_as_character(mapping["dataset_id"]), str(item["pid"])])

        other_entity = ET.SubElement(dataset, "otherEntity")
        other_entity.set("id", _eml_id("other-entity", id_key))
        _add_text(
            other_entity,
            "alternateIdentifier",
            item["pid"],
            attrs={"system": "DataONE"},
        )
        _add_text(other_entity, "entityName", item["entity_name"])
        _add_text(other_entity, "entityDescription", item["description"])

        physical = ET.SubElement(other_entity, "physical")
        physical.set(
            "id",
            _eml_id("physical", ":".join([id_key, str(item["checksum"])])),
        )
        _add_text(physical, "objectName", item["object_name"])
        _add_text(
            physical,
            "size",
            _as_character(item["size"]),
            attrs={"unit": "byte"},
        )
        _add_text(
            physical,
            "authentication",
            item["checksum"],
            attrs={"method": str(item["checksum_algorithm"])},
        )
        if item["compression_method"] is not None and not _is_missing(
            item["compression_method"]
        ):
            _add_text(
                physical, "compressionMethod", item["compression_method"]
            )
        data_format = ET.SubElement(physical, "dataFormat")
        external_format = ET.SubElement(data_format, "externallyDefinedFormat")
        _add_text(external_format, "formatName", item["format_id"])
        distribution = ET.SubElement(physical, "distribution")
        online = ET.SubElement(distribution, "online")
        _add_text(
            online,
            "onlineDescription",
            "Download " + _as_character(item["entity_name"]),
        )
        _add_text(online, "url", item["online_url"])

        _add_text(other_entity, "entityType", item["entity_type"])


# --- document build ---------------------------------------------------------------


def _read_bytes(path: Union[str, Path]) -> bytes:
    return Path(path).read_bytes()


def _record_delimiter(path: str) -> str:
    """Mirror ``.ms_eml_record_delimiter`` (byte-level detection)."""
    data = _read_bytes(path)
    first = data.find(b"\n")
    if first < 0:
        raise ValueError(
            f"CSV resource {path} has no detectable record delimiter."
        )
    if first > 0 and data[first - 1 : first] == b"\r":
        return "\\r\\n"
    return "\\n"


def _present_values(values) -> List[bool]:
    """Mirror ``.ms_eml_present_values``: non-NA and non-blank after trimming."""
    return [
        not _is_missing(value) and bool(_trim(_as_character(value)))
        for value in values
    ]


def _used_sdp_methods(
    path: Path, pkg: Dict[str, object], registry: pd.DataFrame
) -> pd.DataFrame:
    """Mirror ``.ms_eml_used_sdp_methods``.

    EML ``methodStep`` asserts that a procedure *was performed* to produce
    these data. A registry is an inventory of procedures the package knows
    about, which is not the same claim: an alternative that no observed
    measurement references must not be asserted as performed. This narrows the
    registry to procedures actually bound to an observed value, through either
    route the specification allows:

    * a static ``column_dictionary.method_iri`` on a **measurement** column
      that has at least one non-empty value (a method annotated on an
      attribute or identifier column is a legacy dictionary annotation, not a
      measurement procedure, and is excluded);
    * a row-varying ``sosa:usedProcedure`` component, resolved through the
      code values observed where that structure's measure is present.
    """
    if not isinstance(registry, pd.DataFrame) or len(registry) == 0:
        return registry

    used: List[str] = []
    dictionary = pkg["dictionary"]
    if "column_role" in dictionary.columns and "method_iri" in dictionary.columns:
        present_methods = _present_values(dictionary["method_iri"])
        for index in range(len(dictionary)):
            role = dictionary["column_role"].iloc[index]
            if _is_missing(role) or _as_character(role) != "measurement":
                continue
            if not present_methods[index]:
                continue
            table_id = _as_character(dictionary["table_id"].iloc[index])
            column = _as_character(dictionary["column_name"].iloc[index])
            data = pkg["resources"].get(table_id)
            if data is None or column not in data.columns:
                continue
            if any(_present_values(data[column])):
                used.append(_as_character(dictionary["method_iri"].iloc[index]))

    from .observation_structures import (
        SDP_OBSERVATION_COMPONENTS_PATH,
        SDP_OBSERVATION_STRUCTURES_PATH,
        SOSA_USED_PROCEDURE,
        read_sdp_observation_structures,
    )

    structure_paths = (
        Path(path) / SDP_OBSERVATION_STRUCTURES_PATH,
        Path(path) / SDP_OBSERVATION_COMPONENTS_PATH,
    )
    if all(candidate.exists() for candidate in structure_paths):
        structure = read_sdp_observation_structures(path, validate=True)
        components = structure["components"]
        codes = pkg.get("codes")
        present_relations = _present_values(components["component_relation_iri"])
        for index in range(len(components)):
            if not present_relations[index]:
                continue
            if (
                _as_character(components["component_relation_iri"].iloc[index])
                != SOSA_USED_PROCEDURE
            ):
                continue
            procedure = components.iloc[index]
            bound = components[
                (components["dataset_id"] == procedure["dataset_id"])
                & (components["table_id"] == procedure["table_id"])
                & (
                    components["observation_structure_id"]
                    == procedure["observation_structure_id"]
                )
            ]
            measure = list(
                bound.loc[bound["component_role"] == "measure", "column_name"]
            )[0]
            table_id = procedure["table_id"]
            column = procedure["column_name"]
            data = pkg["resources"][table_id]
            observed = _present_values(data[measure])
            code_values: List[str] = []
            for row, is_observed in enumerate(observed):
                if not is_observed:
                    continue
                value = data[column].iloc[row]
                if _is_missing(value) or not _trim(_as_character(value)):
                    continue
                text = _as_character(value)
                if text not in code_values:
                    code_values.append(text)
            if not code_values or codes is None or len(codes) == 0:
                continue
            matched = codes[
                (codes["dataset_id"] == procedure["dataset_id"])
                & (codes["table_id"] == table_id)
                & (codes["column_name"] == column)
                & codes["code_value"].astype(str).isin(code_values)
            ]
            for value in matched["term_iri"]:
                if not _is_missing(value) and _trim(_as_character(value)):
                    used.append(_as_character(value))

    return registry[registry["method_iri"].isin(set(used))]


def _add_sdp_method_steps(methods: ET.Element, sdp_methods: pd.DataFrame) -> None:
    """Emit one ``methodStep`` per procedure actually used by these data."""
    if not isinstance(sdp_methods, pd.DataFrame) or len(sdp_methods) == 0:
        return
    optional = (
        ("method_version", "Method version"),
        ("protocol_iri", "Protocol IRI"),
        ("citation", "Citation"),
    )
    for index in range(len(sdp_methods)):
        method = sdp_methods.iloc[index]
        method_step = ET.SubElement(methods, "methodStep")
        description = ET.SubElement(method_step, "description")
        paragraphs = [
            "Method: " + _as_character(method["method_label"]),
            _as_character(method["method_description"]),
            "Method IRI: " + _as_character(method["method_iri"]),
        ]
        for field, label in optional:
            value = method[field]
            if not _is_missing(value) and _trim(_as_character(value)):
                paragraphs.append(label + ": " + _as_character(value))
        for paragraph in paragraphs:
            _add_text(description, "para", paragraph)


def _build_document(
    path: Path,
    pkg: Dict[str, object],
    mapping: dict,
    configs: List[dict],
    vocabulary: pd.DataFrame,
    data_objects: pd.DataFrame,
    supplementary_objects: pd.DataFrame,
    sdp_methods: pd.DataFrame,
) -> Dict[str, object]:
    """Mirror ``.ms_eml_build_document``."""
    root = ET.Element("{" + _EML_NAMESPACE + "}eml")
    root.set(
        "{" + _XSI_NAMESPACE + "}schemaLocation",
        _EML_NAMESPACE + " " + _EML_NAMESPACE + "/eml.xsd",
    )
    revision_key = _revision_key(mapping)
    package_id_preimage = [
        "metasalmon-eml-profile-1",
        _as_character(mapping["system"]),
        _as_character(mapping["dataset_id"]),
    ]
    if revision_key is not None:
        package_id_preimage.extend(["revision", revision_key])
    package_id = "urn:uuid:" + _uuid5(":".join(package_id_preimage))
    series_id = "urn:uuid:" + _uuid5(
        ":".join(["series", _as_character(mapping["series_key"])])
    )
    root.set("packageId", package_id)
    root.set("system", _as_character(mapping["system"]))

    dataset_meta = pkg["dataset"]
    dictionary = pkg["dictionary"]
    tables = pkg["tables"]
    dataset_id = _as_character(mapping["dataset_id"])

    dataset = ET.SubElement(root, "dataset")
    dataset.set("id", _eml_id("dataset", dataset_id))
    _add_text(dataset, "alternateIdentifier", mapping["dataset_id"])
    _add_text(dataset, "title", dataset_meta.iloc[0]["title"])

    creators = mapping["creators"]
    for index, creator in enumerate(creators, start=1):
        _add_party(
            dataset,
            "creator",
            creator,
            ":".join([dataset_id, "creator", str(index)]),
        )
    for index, provider in enumerate(mapping["metadata_providers"], start=1):
        _add_party(
            dataset,
            "metadataProvider",
            provider,
            ":".join([dataset_id, "metadata-provider", str(index)]),
        )
    _add_text(dataset, "pubDate", mapping["publication_date"])
    _add_text(dataset, "language", mapping["language"])
    _add_para(dataset, "abstract", dataset_meta.iloc[0]["description"])

    keywords_value = dataset_meta.iloc[0]["keywords"]
    keywords = _split_iris(
        None
        if _is_missing(keywords_value)
        else _as_character(keywords_value).replace(",", ";")
    )
    if keywords:
        keyword_set = ET.SubElement(dataset, "keywordSet")
        for keyword in keywords:
            _add_text(keyword_set, "keyword", keyword)
        _add_text(keyword_set, "keywordThesaurus", "None")

    supporting_document = mapping["source_provenance"]["supporting_document"]
    additional_info = ET.SubElement(dataset, "additionalInfo")
    provenance_lines = [
        "Source citation: "
        + _as_character(mapping["source_provenance"]["source_citation"]),
        "Provenance note: "
        + _as_character(mapping["source_provenance"]["provenance_note"]),
        "Supporting document citation: "
        + _as_character(supporting_document["citation"]),
        "Supporting document URL: " + _as_character(supporting_document["url"]),
        "Supporting document SHA-256: "
        + _as_character(supporting_document["sha256"]),
    ]
    for line in provenance_lines:
        _add_text(additional_info, "para", line)

    intellectual_rights = ET.SubElement(dataset, "intellectualRights")
    paragraphs = mapping["intellectual_rights"]["paragraphs"]
    if isinstance(paragraphs, str):
        paragraphs = [paragraphs]
    for paragraph in paragraphs:
        _add_text(intellectual_rights, "para", paragraph)
    _add_coverage(dataset, dataset_meta, mapping)

    for index, contact in enumerate(mapping["contacts"], start=1):
        _add_party(
            dataset,
            "contact",
            contact,
            ":".join([dataset_id, "contact", str(index)]),
        )
    _add_party(
        dataset,
        "publisher",
        mapping["publisher"],
        ":".join([dataset_id, "publisher"]),
    )

    methods = ET.SubElement(dataset, "methods")
    for method in mapping["methods"]:
        method_step = ET.SubElement(methods, "methodStep")
        description = ET.SubElement(method_step, "description")
        _add_text(description, "para", method.get("description"))

    _add_sdp_method_steps(methods, sdp_methods)

    for table_index in range(len(tables)):
        table_row = {
            column: tables.iloc[table_index][column] for column in tables.columns
        }
        table_id = _as_character(table_row["table_id"])
        data_object_rows = data_objects[data_objects["table_id"] == table_id]
        if len(data_object_rows) != 1:
            raise ValueError(
                "Internal EML export error: expected one data object for "
                f"table {table_id!r}."
            )
        data_object = data_object_rows.iloc[0]
        data = pkg["resources"].get(table_id)
        if data is None:
            raise ValueError(
                f"SDP table {table_id!r} has no loaded data resource."
            )
        raw_data = _read_raw_csv_tokens(str(data_object["path"]), table_id)
        _validate_raw_table(raw_data, data, table_id)

        data_table = ET.SubElement(dataset, "dataTable")
        data_table.set(
            "id", _eml_id("table", ":".join([dataset_id, table_id]))
        )
        _add_text(data_table, "alternateIdentifier", table_id)
        _add_text(data_table, "entityName", table_row.get("table_label"))
        _add_text(data_table, "entityDescription", table_row.get("description"))

        physical = ET.SubElement(data_table, "physical")
        physical.set(
            "id", _eml_id("physical", ":".join([dataset_id, table_id]))
        )
        _add_text(
            physical, "objectName", os.path.basename(str(data_object["file_name"]))
        )
        _add_text(
            physical,
            "size",
            _as_character(data_object["size"]),
            attrs={"unit": "byte"},
        )
        _add_text(
            physical,
            "authentication",
            data_object["checksum"],
            attrs={"method": "SHA-256"},
        )
        _add_text(physical, "characterEncoding", "UTF-8")
        data_format = ET.SubElement(physical, "dataFormat")
        text_format = ET.SubElement(data_format, "textFormat")
        _add_text(text_format, "numHeaderLines", "1")
        _add_text(
            text_format,
            "recordDelimiter",
            _record_delimiter(str(data_object["path"])),
        )
        _add_text(text_format, "attributeOrientation", "column")
        delimited = ET.SubElement(text_format, "simpleDelimited")
        _add_text(delimited, "fieldDelimiter", ",")
        _add_text(delimited, "quoteCharacter", '"')
        distribution = ET.SubElement(physical, "distribution")
        online = ET.SubElement(distribution, "online")
        _add_text(online, "url", _knb_object_url(str(data_object["pid"])))

        attribute_list = ET.SubElement(data_table, "attributeList")
        attribute_list.set(
            "id", _eml_id("attributes", ":".join([dataset_id, table_id]))
        )
        for row in range(len(dictionary)):
            if _as_character(dictionary.iloc[row]["table_id"]) != table_id:
                continue
            _add_attribute(
                attribute_list,
                _dictionary_row(dictionary, row),
                configs[row],
                data,
                raw_data,
                pkg,
                vocabulary,
                dataset_id,
            )
        _add_primary_key(data_table, table_row, dictionary, dataset_id)
        _add_not_null(data_table, table_id, dictionary, dataset_id)
        _add_text(data_table, "numberOfRecords", str(len(data)))

    _add_supplementary_objects(dataset, supplementary_objects, mapping)

    return {
        "document": root,
        "package_id": package_id,
        "series_id": series_id,
    }


# --- document-level validation -----------------------------------------------------


def _validate_document_links(
    document: ET.Element, dictionary: pd.DataFrame, dataset_id: str
) -> None:
    """Mirror ``.ms_eml_validate_document_links``."""
    parents = {
        child: parent for parent in document.iter() for child in parent
    }
    elements_with_ids = [
        element for element in document.iter() if element.get("id") is not None
    ]
    ids = [element.get("id") for element in elements_with_ids]
    duplicates = list(
        dict.fromkeys(value for value in ids if ids.count(value) > 1)
    )
    if duplicates:
        raise ValueError(
            "Generated EML contains duplicate XML ID(s): "
            + ", ".join(duplicates)
            + "."
        )

    references = [
        element.text or ""
        for element in document.iter()
        if element.tag == "attributeReference"
    ]
    unknown = list(
        dict.fromkeys(value for value in references if value not in ids)
    )
    if unknown:
        raise ValueError(
            "Generated EML contains dangling attribute reference(s): "
            + ", ".join(unknown)
            + "."
        )

    annotations = [
        element for element in document.iter() if element.tag == "annotation"
    ]
    annotated_parents = [parents[annotation] for annotation in annotations]
    if any(parent.get("id") is None for parent in annotated_parents):
        raise ValueError(
            "Every EML semantic annotation subject must have a unique XML ID."
        )

    measurement = _measurement_rows(dictionary)
    expected_measurement_ids = [
        _attribute_id(
            dataset_id,
            _as_character(measurement.iloc[row]["table_id"]),
            _as_character(measurement.iloc[row]["column_name"]),
        )
        for row in range(len(measurement))
    ]
    actual_measurement_ids = list(
        dict.fromkeys(parent.get("id") for parent in annotated_parents)
    )
    if set(expected_measurement_ids) != set(actual_measurement_ids):
        raise ValueError(
            "Generated EML annotation subjects do not exactly match the SDP "
            "measurement columns."
        )

    elements_by_id = {
        element.get("id"): element for element in elements_with_ids
    }
    for row, attribute_id in enumerate(expected_measurement_ids):
        subject = elements_by_id[attribute_id]
        predicates = [
            child.text or ""
            for annotation in subject
            if annotation.tag == "annotation"
            for child in annotation
            if child.tag == "propertyURI"
        ]
        term_annotation = _measurement_term_annotation(
            {
                column: measurement.iloc[row][column]
                for column in measurement.columns
            }
        )
        expected_predicates = [
            term_annotation["iri"],
            _MEASUREMENT_PREDICATES["unit"],
        ]
        if predicates != expected_predicates:
            raise ValueError(
                f"Generated EML attribute {attribute_id!r} does not contain "
                "exactly the approved semantic predicates in profile order."
            )

    xml_text = ET.tostring(document, encoding="unicode")
    if "REVIEW:" in xml_text:
        raise ValueError(
            'Generated EML contains an unresolved "REVIEW:" marker.'
        )
    if "usedProcedure" in xml_text:
        raise ValueError(
            "The initial EML profile must not emit a procedure annotation."
        )


def _serialize(document: ET.Element) -> bytes:
    """Deterministic UTF-8 serialization (2-space indent, trailing LF)."""
    ET.indent(document, space="  ")
    body = ET.tostring(document, encoding="unicode")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n").encode(
        "utf-8"
    )


# --- XSD validation (optional lxml extra) -------------------------------------------

_XSD_SCHEMA_CACHE: Dict[str, object] = {}


def _require_eml_extra() -> None:
    """Mirror R's up-front ``requireNamespace("emld")`` gate.

    The mapping sidecar is full YAML (like R's ``yaml::read_yaml``) and the
    generated document is XSD-validated before it is written; both live in
    the optional ``metasalmonpy[eml]`` extra.
    """
    missing = []
    try:
        import lxml.etree  # noqa: F401
    except ImportError:
        missing.append("lxml")
    try:
        import yaml  # noqa: F401
    except ImportError:
        missing.append("PyYAML")
    if missing:
        raise ImportError(
            "write_eml_from_sdp requires the optional EML dependencies ("
            + ", ".join(missing)
            + ") to parse the mapping sidecar and validate EML against the "
            'bundled XSD set. Install them with: pip install "metasalmonpy[eml]".'
        )


def _eml_schema_path() -> Path:
    schema_path = _DATA_DIR / "xsd" / "eml-2.2.0" / "eml.xsd"
    if not schema_path.is_file():
        raise FileNotFoundError(
            "Could not locate the bundled EML 2.2.0 schema."
        )
    return schema_path


def _xsd_validate(xml_path: Union[str, Path]) -> bool:
    """Validate a written document against the bundled EML 2.2.0 XSD set.

    lxml is libxml2 — the identical engine behind ``emld::eml_validate`` —
    so accept/reject semantics match R by construction.
    """
    try:
        from lxml import etree
    except ImportError:
        raise ImportError(
            "EML 2.2.0 XSD validation requires the optional lxml "
            'dependency. Install it with: pip install "metasalmonpy[eml]".'
        ) from None

    schema = _XSD_SCHEMA_CACHE.get("eml-2.2.0")
    if schema is None:
        schema = etree.XMLSchema(etree.parse(str(_eml_schema_path())))
        _XSD_SCHEMA_CACHE["eml-2.2.0"] = schema
    document = etree.parse(str(xml_path))
    if not schema.validate(document):
        detail = "\n".join(
            str(entry) for entry in list(schema.error_log)[:10]
        ) or "Unknown EML schema validation error."
        raise ValueError(
            "Generated EML 2.2.0 failed schema validation.\n" + detail
        )
    return True


# --- export pipeline ----------------------------------------------------------------


def _export_reviewed(
    root: Path,
    pkg: Dict[str, object],
    mapping: dict,
    supplementary_objects: object = None,
    require_revision_key: bool = False,
) -> Dict[str, object]:
    """The reviewed-export pipeline shared by the writer and the tests.

    Everything except sidecar YAML parsing, XSD validation, and the atomic
    file write: mapping validation, the semantic-review and vocabulary
    gates, deterministic object plans, document build, and link validation.
    """
    configs = _validate_mapping(mapping, pkg)
    revision_key = _revision_key(mapping, required=require_revision_key)
    _read_semantic_review(root, pkg, mapping)
    vocabulary = _read_vocabulary(root, pkg["dictionary"], mapping)
    data_objects = _data_objects(root, pkg, mapping)
    supplementary = _supplementary_objects(supplementary_objects)

    from .sdp_methods import SDP_METHODS_PATH, read_sdp_methods

    if (root / SDP_METHODS_PATH).exists():
        sdp_methods = read_sdp_methods(root, validate=True)
    else:
        sdp_methods = pd.DataFrame()
    used_methods = _used_sdp_methods(root, pkg, sdp_methods)

    built = _build_document(
        root,
        pkg,
        mapping,
        configs,
        vocabulary,
        data_objects,
        supplementary,
        used_methods,
    )
    _validate_document_links(
        built["document"], pkg["dictionary"], _as_character(mapping["dataset_id"])
    )
    xml_bytes = _serialize(built["document"])
    return {
        "document": built["document"],
        "xml_bytes": xml_bytes,
        "package_id": built["package_id"],
        "series_id": built["series_id"],
        "revision_key": revision_key,
        "public": mapping["publication"]["public"],
        "data_objects": data_objects,
        "supplementary_objects": supplementary,
        # The complete registry stays available to callers; only the subset
        # actually bound to observed measurements is asserted in the document.
        "methods": sdp_methods,
        "used_methods": used_methods,
    }


def _export_from_mapping(
    path: Union[str, Path],
    mapping: dict,
    supplementary_objects: object = None,
    require_revision_key: bool = False,
) -> Dict[str, object]:
    """Validate the package strictly, then run the reviewed-export pipeline."""
    from .package_io import validate_salmon_datapackage

    root = Path(os.path.realpath(str(path)))
    validation = validate_salmon_datapackage(str(root), require_iris=True)
    pkg = validation["package"]
    if len(pkg["dataset"]) != 1:
        raise ValueError("EML export requires exactly one SDP dataset row.")
    return _export_reviewed(
        root,
        pkg,
        mapping,
        supplementary_objects=supplementary_objects,
        require_revision_key=require_revision_key,
    )


def _strict_yaml_loader():
    """A SafeLoader that refuses duplicate keys and never invents dates.

    Two PyYAML defaults are wrong for a *reviewed* sidecar, and both are
    silent:

    * **Duplicate mapping keys.** R's ``yaml::read_yaml`` aborts with
      "Duplicate map key"; PyYAML keeps the last one. A reviewer who edits
      ``status: draft`` to ``status: final`` by pasting a second line gets a
      different document out of each implementation, with no warning from
      either the parser or the schema.
    * **Implicit timestamps.** R's yaml returns ``publication_date:
      2026-01-01`` as the *character* string R's validator expects. PyYAML
      resolves it to ``datetime.date``, which then fails the sidecar's
      JSON-Schema string check — so an unquoted date that R accepts was a hard
      error here. Timestamps are un-resolved rather than post-coerced so the
      value never becomes a date in the first place.

    This mirrors the duplicate-key rejection the SSSOM subset parser already
    performs on its own metadata block.
    """
    import yaml

    class _StrictSafeLoader(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):
            # flatten_mapping resolves YAML merge keys, which R's yaml also
            # supports; run it first so `<<` is not counted as a real key.
            self.flatten_mapping(node)
            seen: List[object] = []
            for key_node, _ in node.value:
                key = self.construct_object(key_node, deep=deep)
                if key in seen:
                    raise yaml.constructor.ConstructorError(
                        "while constructing a mapping",
                        node.start_mark,
                        f"Duplicate map key: '{key}'",
                        key_node.start_mark,
                    )
                seen.append(key)
            return super().construct_mapping(node, deep=deep)

    # R's yaml returns every timestamp -- implicit `2026-01-01` or explicitly
    # tagged -- as the verbatim character string, so do the same rather than
    # constructing a datetime.date and coercing it back later.
    _StrictSafeLoader.add_constructor(
        "tag:yaml.org,2002:timestamp",
        lambda loader, node: loader.construct_scalar(node),
    )
    return _StrictSafeLoader


def _read_mapping_yaml(mapping_path: Union[str, Path]) -> object:
    """Parse the reviewed sidecar with full YAML (R: ``yaml::read_yaml``)."""
    import yaml

    text = Path(mapping_path).read_text(encoding="utf-8")
    try:
        return yaml.load(text, Loader=_strict_yaml_loader())
    except yaml.YAMLError as error:
        raise ValueError(
            f"EML mapping sidecar {mapping_path} is not valid YAML: {error}"
        ) from None


def write_eml_from_sdp(
    path: Union[str, Path],
    output_path: Optional[Union[str, Path]] = None,
    mapping_path: Optional[Union[str, Path]] = None,
    overwrite: bool = False,
    supplementary_objects: object = None,
    require_revision_key: bool = False,
) -> Dict[str, object]:
    """Write reviewed EML 2.2.0 metadata from a Salmon Data Package.

    Builds deterministic EML 2.2.0 XML from a strictly valid Salmon Data
    Package and an explicit EML mapping sidecar. The sidecar is required
    because EML concepts such as measurement scale, structured parties,
    methods, and rights cannot be inferred defensibly from the canonical SDP
    tables.

    Measurement attributes receive exactly two semantic annotations in the
    initial profile. Both reviewed OWL measurement-datum classes and SKOS
    compound-variable concepts use Dublin Core Terms ``subject`` with the
    reviewed ``term_iri``, followed by QUDT ``hasUnit`` using the reviewed
    ``unit_iri``. The broader topic predicate is intentional: an OWL class
    is not necessarily an OBOE ``MeasurementType``, and schema-valid EML
    must not silently assert that unsupported range. The exporter
    deliberately does not project incomplete I-ADOPT roles or procedure
    annotations into EML.

    Requires the ``metasalmonpy[eml]`` extra (PyYAML for the sidecar, lxml
    for XSD validation against the bundled EML 2.2.0 schema set).

    Parameters
    ----------
    path:
        Directory containing a Salmon Data Package.
    output_path:
        Output XML path. Defaults to ``metadata/eml.xml`` inside ``path``.
    mapping_path:
        Reviewed YAML mapping. Defaults to ``metadata/eml-mapping.yml``
        inside ``path``.
    overwrite:
        Replace a different existing output only when ``True``. An
        identical existing file is treated as an idempotent success.
    supplementary_objects:
        Optional DataFrame (or dict of columns) describing canonical SDP
        archives or expanded artifacts to expose as EML ``otherEntity``
        elements. Required columns are ``path``, ``pid``, ``format_id``,
        ``checksum``, ``object_name``, ``entity_name``, and ``description``;
        optional ``size``, when supplied, must match the file.
        ``entity_type`` may distinguish an expanded artifact from an archive.
        Objects use lowercase SHA-256 checksums and safe relative
        ``object_name`` paths; only ``application/zip`` objects receive
        ``compressionMethod = zip``. ``publish_sdp_to_knb()`` supplies this
        plan automatically; ordinary standalone EML export leaves it ``None``.
    require_revision_key:
        When ``True``, require a reviewed ``publication.revision_key`` in
        the EML mapping sidecar. The key creates a new deterministic
        metadata package ID without changing the series ID.

    Returns
    -------
    dict
        The XML text, normalized output path, EML version, metadata package
        ID, stable series ID, validation result, revision key, the
        deterministic data and supplementary-object plans, the complete
        method registry (``methods``), and the subset asserted in EML
        (``used_methods``).
    """
    if not isinstance(require_revision_key, bool):
        raise ValueError("require_revision_key must be one logical value.")
    _require_eml_extra()
    if not Path(path).is_dir():
        raise FileNotFoundError(f"SDP directory {path} does not exist.")

    root = Path(os.path.realpath(str(path)))
    if mapping_path is None:
        mapping_path = _default_mapping_path(root)
    if not Path(mapping_path).is_file():
        raise FileNotFoundError(
            f"EML mapping sidecar {mapping_path} does not exist."
        )
    mapping_path = os.path.realpath(str(mapping_path))

    if output_path is None:
        output_path = root / "metadata" / "eml.xml"
    output_path = os.path.expanduser(str(output_path))
    # R's dirname("eml.xml") is ".", so a bare basename writes to the working
    # directory; os.path.dirname() returns "" instead, and makedirs("") then
    # fails with ENOENT -- which surfaced as "Could not create EML output
    # directory" for an output path R accepts. Normalize "" to "." to keep
    # the mirror honest.
    output_dir = os.path.dirname(output_path) or "."
    if not os.path.isdir(output_dir):
        try:
            os.makedirs(output_dir, exist_ok=True)
        except OSError:
            pass
    if not os.path.isdir(output_dir):
        raise ValueError(
            f"Could not create EML output directory {output_dir}."
        )
    output_path = os.path.join(
        os.path.realpath(output_dir), os.path.basename(output_path)
    )

    from .package_io import validate_salmon_datapackage

    validation = validate_salmon_datapackage(str(root), require_iris=True)
    pkg = validation["package"]
    if len(pkg["dataset"]) != 1:
        raise ValueError("EML export requires exactly one SDP dataset row.")

    mapping = _read_mapping_yaml(mapping_path)
    exported = _export_reviewed(
        root,
        pkg,
        mapping,
        supplementary_objects=supplementary_objects,
        require_revision_key=require_revision_key,
    )

    handle, temporary = tempfile.mkstemp(
        prefix=".metasalmon-eml-", suffix=".xml", dir=output_dir
    )
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(exported["xml_bytes"])

        eml_validation = _xsd_validate(temporary)

        if os.path.exists(output_path):
            identical_bytes = _read_bytes(output_path) == _read_bytes(temporary)
            if identical_bytes:
                os.unlink(temporary)
            elif overwrite is not True:
                # R gates this on isTRUE(overwrite), so only the literal TRUE
                # authorizes replacing a differing document. A truthiness test
                # let overwrite="no" destroy the existing file.
                raise ValueError(
                    f"EML output {output_path} already exists with different "
                    "bytes; set overwrite=True to replace it."
                )
        if os.path.exists(temporary):
            # mkstemp() creates 0600 and os.replace preserves it; R's
            # writeBin + file.rename leaves the umask default.
            apply_default_file_mode(temporary)
            os.replace(temporary, output_path)
    finally:
        if os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass

    xml_text = Path(output_path).read_text(encoding="utf-8")
    if xml_text.endswith("\n"):
        xml_text = xml_text[:-1]
    return {
        "xml": xml_text,
        "path": os.path.realpath(output_path),
        "eml_version": EML_VERSION,
        "format_id": _EML_FORMAT_ID,
        "package_id": exported["package_id"],
        "series_id": exported["series_id"],
        "revision_key": exported["revision_key"],
        "public": exported["public"],
        "validation": eml_validation,
        "data_objects": exported["data_objects"],
        "supplementary_objects": exported["supplementary_objects"],
        "methods": exported["methods"],
        "used_methods": exported["used_methods"],
    }


__all__ = ["write_eml_from_sdp", "EML_VERSION", "SUPPORTED_REVIEW_PATHS"]
