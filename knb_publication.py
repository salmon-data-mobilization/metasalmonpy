"""KNB/DataONE publication (mirrors metasalmon's ``R/knb-publication.R`` at v0.1.7).

The local planner is intentionally independent of DataONE credentials and
client libraries. It creates the exact immutable-object plan, deterministic
OAI-ORE resource map, and recovery manifest before any network boundary is
constructed. The default is a credential-free, network-free dry run.

Era note: this module ports the **v0.1.7 tag** of ``R/knb-publication.R``.
Later metasalmon releases add expanded object representations, archive
overwrite semantics, and upload retry hardening; those belong to the 0.1.8 /
0.2.3 replay milestones and are deliberately absent here. The era plan uses
one named SDP archive (``representation = "archive"``); the legacy
``sdp_artifact`` role is still read, aggregated, and audited so older
manifests stay inspectable.

Transport: metasalmon reaches DataONE through the ``dataone``/``datapack``
R packages, but bypasses them for much of the surface (it builds the ORE by
hand with xml2 and issues raw ``httr2`` requests for every anonymous probe,
the format registry, and the Solr catalog). There is no maintained Python
equivalent of ``dataone``/``datapack``, so the Python adapter speaks the
DataONE v2 REST API directly with ``requests`` behind the identical
14-method adapter boundary (PARITY.md entry 16). The SystemMetadata and ORE
documents are built with stdlib :mod:`xml.etree.ElementTree`, the same way
``eml.py`` does, and are asserted ``ET.canonicalize``-equal to R's.

Parity contract: identifiers, checksums, fingerprints, and the manifest JSON
encoding are **byte-exact** with R (asserted against R-generated fixtures in
``tests/data/knb/``). The three artifacts whose bytes come from a formatter
or compressor — the EML document, the ORE document, and the SDP ZIP — are
contract-level only (PARITY.md entries 4, 15, 17).

Optional dependencies: publication always rebuilds reviewed EML, so it
inherits the ``metasalmonpy[eml]`` requirements through the ``[knb]`` extra
(PARITY.md entry 2).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
import warnings
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union
from urllib.parse import quote

from . import eml as _eml
from .atomic_io import atomic_write

# --- constants ----------------------------------------------------------------------

ENVIRONMENT = "PROD"
NODE_ID = "urn:node:KNB"
MN_ENDPOINT = "https://knb.ecoinformatics.org/knb/d1/mn/v2"
#: R resolves the Coordinating Node through ``dataone::D1Client``; the raw
#: REST adapter needs the production CN base URL as an explicit constant.
CN_ENDPOINT = "https://cn.dataone.org/cn/v2"
ORE_FORMAT_ID = "http://www.openarchives.org/ore/terms"
ORE_MEDIA_TYPE = "application/rdf+xml"
RESOLVER = "https://cn.dataone.org/cn/v2/resolve/"
ORE_PROFILE = "metasalmon-dataone-ore-v2"

_D1_V2_NAMESPACE = "http://ns.dataone.org/service/types/v2.0"
_D1_V1_NAMESPACE = "http://ns.dataone.org/service/types/v1"

_ORE_NAMESPACES = (
    ("xmlns:rdf", "http://www.w3.org/1999/02/22-rdf-syntax-ns#"),
    ("xmlns:ore", "http://www.openarchives.org/ore/terms/"),
    ("xmlns:cito", "http://purl.org/spar/cito/"),
    ("xmlns:prov", "http://www.w3.org/ns/prov#"),
    ("xmlns:dcterms", "http://purl.org/dc/terms/"),
    ("xmlns:xsd", "http://www.w3.org/2001/XMLSchema#"),
)

_XSD_STRING = "http://www.w3.org/2001/XMLSchema#string"

_REQUIRED_SDP_ARTIFACTS = (
    "datapackage.json",
    "metadata/dataset.csv",
    "metadata/tables.csv",
    "metadata/column_dictionary.csv",
    "metadata/codes.csv",
    "metadata/semantic_vocabulary.csv",
)

_CANONICAL_REVIEW_LEDGER = "reproducibility/reviewed_semantic_selections.csv"
_LEGACY_REVIEW_LEDGER = "reviewed_semantic_selections.csv"
_REPRODUCIBILITY_MANIFEST = "reproducibility/manifest.json"
_OBSERVATION_STRUCTURE_FILES = (
    "metadata/structure/observation_structures.csv",
    "metadata/structure/observation_components.csv",
)

_ARTIFACT_FORMAT_IDS = {
    "csv": "text/csv",
    "json": "application/json",
    "tsv": "text/tsv",
    "yml": "text/plain",
    "yaml": "text/plain",
}

_ARTIFACT_MEDIA_TYPES = {
    "csv": "text/csv",
    "json": "application/json",
    "tsv": "text/tab-separated-values",
    "yml": "text/plain",
    "yaml": "text/plain",
}

_STATUS_RANKS = {
    "dry_run": 1,
    "pending": 2,
    "published_pending_catalog": 3,
    "complete": 4,
}

_ORCID_SUBJECT_RE = re.compile(
    r"^https?://orcid\.org/([0-9]{4}-[0-9]{4}-[0-9]{4}-[0-9]{3}[0-9X])/?$",
    re.IGNORECASE,
)

_REVIEW_CANDIDATE_RE = re.compile(
    r"(^|[^a-z])(candidate|review[ _-]?candidate)([^a-z]|$)"
)

_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})$"
)


class KnbHttpError(RuntimeError):
    """An HTTP failure that carries its status code.

    Mirrors the ``http_<status>`` condition classes httr2 attaches, which
    ``_anonymous_denial_status`` reads to prove non-disclosure.
    """

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


# --- process-local credentials and adapter seam ---------------------------------------

_KNB_ADAPTER: object = None
_DATAONE_TOKEN: Optional[str] = None


def set_knb_adapter(adapter: object) -> None:
    """Install a KNB adapter (or zero-argument factory) for this process.

    The Pythonic form of R's private ``metasalmon.knb_adapter`` option: it is
    a test seam, not public API. Pass ``None`` to restore the default REST
    adapter.
    """
    global _KNB_ADAPTER
    _KNB_ADAPTER = adapter


def set_dataone_token(token: Optional[str]) -> None:
    """Supply a short-lived DataONE JWT for this process only.

    The Pythonic form of R's supported ``dataone_token`` option. Credentials
    are never accepted as function arguments and never written to a manifest.
    """
    global _DATAONE_TOKEN
    _DATAONE_TOKEN = token


def _adapter() -> object:
    """Mirror ``.ms_knb_adapter``."""
    adapter = _KNB_ADAPTER
    if adapter is None:
        return _default_adapter()
    if callable(adapter) and not _has_adapter_methods(adapter):
        adapter = adapter()
    if adapter is None:
        raise ValueError(
            "set_knb_adapter must provide a KNB adapter object or a "
            "constructor returning one."
        )
    return adapter


def _required_adapter_methods() -> Tuple[str, ...]:
    """Mirror ``.ms_knb_required_adapter_methods`` (the v0.1.7 method set)."""
    return (
        "connect",
        "preflight",
        "list_formats",
        "lookup_system_metadata",
        "lookup_series_id",
        "create_object",
        "update_object",
        "get_bytes",
        "get_system_metadata",
        "get_checksum",
        "get_anonymous_bytes",
        "get_anonymous_system_metadata",
        "catalog_lookup",
        "anonymous_catalog_lookup",
    )


def _has_adapter_methods(adapter: object) -> bool:
    return all(
        callable(getattr(adapter, name, None))
        for name in _required_adapter_methods()
    )


def _validate_adapter(adapter: object) -> object:
    """Mirror ``.ms_knb_validate_adapter``."""
    missing = [
        name
        for name in _required_adapter_methods()
        if not callable(getattr(adapter, name, None))
    ]
    if missing:
        raise ValueError(
            "KNB adapter is missing method(s): " + ", ".join(missing) + "."
        )
    return adapter


# --- scalar / flag helpers ------------------------------------------------------------


def _validate_flag(value: object, field: str, allow_null: bool = False) -> None:
    """Mirror ``.ms_knb_validate_flag``: one explicit, non-missing logical."""
    if allow_null and value is None:
        return
    if not isinstance(value, bool):
        raise ValueError(
            f"{field} must be one explicit, non-missing logical value."
        )


def _optional_scalar(value: object) -> Optional[str]:
    """Mirror ``.ms_knb_optional_scalar``: NA-ish becomes ``None``."""
    if isinstance(value, (list, tuple)):
        if not value:
            return None
        value = value[0]
    if value is None:
        return None
    if isinstance(value, float) and value != value:  # NaN
        return None
    text = str(value)
    if not text.strip():
        return None
    return text


def _nonempty_scalar(value: object, field: str) -> str:
    """Mirror ``.ms_knb_nonempty_scalar``."""
    scalar = _optional_scalar(value)
    if scalar is None:
        raise ValueError(f"KNB adapter preflight returned invalid {field}.")
    return scalar.strip()


def _valid_timestamp(value: object) -> bool:
    """Mirror ``.ms_knb_valid_timestamp``."""
    scalar = _optional_scalar(value)
    if scalar is None or _TIMESTAMP_RE.match(scalar) is None:
        return False
    normalized = re.sub(r"Z$", "+0000", scalar)
    normalized = re.sub(r"([+-][0-9]{2}):([0-9]{2})$", r"\1\2", normalized)
    # strptime's %f accepts at most six digits; R's %OS is unbounded.
    normalized = re.sub(
        r"\.([0-9]{1,6})[0-9]*", lambda match: "." + match.group(1), normalized
    )
    for pattern in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            datetime.strptime(normalized, pattern)
            return True
        except ValueError:
            continue
    return False


# --- path helpers ---------------------------------------------------------------------


def _lexical_absolute_path(path: object) -> str:
    """Mirror ``.ms_knb_lexical_absolute_path``: collapse without touching disk."""
    if path is None:
        raise ValueError("Publication paths must be non-empty scalar values.")
    text = os.path.expanduser(str(path))
    if not text:
        raise ValueError("Publication paths must be non-empty scalar values.")
    slash_path = text.replace("\\", "/")
    is_absolute = slash_path.startswith("/") or re.match(
        r"^[A-Za-z]:/", slash_path
    )
    if not is_absolute:
        slash_path = os.getcwd().replace("\\", "/") + "/" + slash_path

    drive = slash_path[:2] if re.match(r"^[A-Za-z]:/", slash_path) else ""
    without_root = (
        slash_path[3:] if drive else re.sub(r"^/+", "", slash_path)
    )
    collapsed: List[str] = []
    for part in without_root.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            if collapsed:
                collapsed.pop()
            continue
        collapsed.append(part)
    prefix = drive + "/" if drive else "/"
    return prefix + "/".join(collapsed)


def _resolve_target_path(path: object, must_work: bool = True) -> str:
    """Mirror ``.ms_knb_resolve_target_path``."""
    lexical = _lexical_absolute_path(path)
    if must_work:
        if not os.path.exists(lexical):
            raise FileNotFoundError(f"Publication path {path} does not exist.")
        return os.path.realpath(lexical)

    ancestor = lexical
    suffix: List[str] = []
    while not os.path.exists(ancestor):
        parent = os.path.dirname(ancestor)
        if parent == ancestor:
            raise FileNotFoundError(
                "Could not resolve an existing ancestor for publication path "
                f"{path}."
            )
        suffix.insert(0, os.path.basename(ancestor))
        ancestor = parent
    resolved = os.path.realpath(ancestor)
    if suffix:
        resolved = os.path.join(resolved, *suffix)
    return resolved


def _package_root(path: object) -> str:
    """Mirror ``.ms_knb_package_root``: an existing, non-symlinked SDP root."""
    lexical = _lexical_absolute_path(path)
    if not os.path.isdir(lexical):
        raise FileNotFoundError(f"SDP directory {path} does not exist.")
    if Path(lexical).is_symlink():
        raise ValueError("The SDP directory itself must not be a symbolic link.")
    return os.path.realpath(lexical)


def _inside_path(root: object, target: object, must_work: bool = True) -> str:
    """Mirror ``.ms_knb_inside_path``.

    Returns the artifact's **lexical** in-package path, not its realpath.

    The expanded representation publishes each artifact under its
    package-relative name, so the name must survive harmless platform aliases
    (macOS spells a temporary directory through ``/var -> /private/var``)
    without an in-package symlink being silently renamed to whatever it points
    at. Recovering the caller's spelling of the root and refusing every
    symlink on the way down gives both: aliases are transparent, in-package
    symlinks are refused.
    """
    root_path = os.path.realpath(str(root))
    lexical = _lexical_absolute_path(target)

    ancestor = lexical
    lexical_root: Optional[str] = None
    while True:
        if os.path.exists(ancestor) and os.path.realpath(ancestor) == root_path:
            lexical_root = ancestor
            break
        parent = os.path.dirname(ancestor)
        if parent == ancestor:
            break
        ancestor = parent
    prefix = (lexical_root or "") + os.sep
    if lexical_root is None or not lexical.startswith(prefix):
        raise ValueError(
            f"Publication artifact {target} must remain inside the SDP "
            "directory."
        )

    relative = lexical[len(prefix):]
    candidate = os.path.join(root_path, relative)
    parts = relative.replace("\\", "/").split("/")
    symlinks = [
        os.path.join(root_path, *parts[: index + 1])
        for index in range(len(parts))
        if Path(os.path.join(root_path, *parts[: index + 1])).is_symlink()
    ]
    if symlinks:
        raise ValueError(
            "Publication artifacts cannot be reached through a symlink: "
            + ", ".join(symlinks)
            + "."
        )
    resolved = _resolve_target_path(candidate, must_work=must_work)
    if not resolved.startswith(root_path + os.sep):
        raise ValueError(
            f"Publication artifact {target} must remain inside the SDP "
            "directory."
        )
    return candidate


def _relative_path(root: object, target: object, must_work: bool = True) -> str:
    """Mirror ``.ms_knb_relative_path``."""
    root_path = os.path.realpath(str(root))
    target = _inside_path(root, target, must_work=must_work)
    prefix = root_path + os.sep
    if not target.startswith(prefix):
        raise ValueError(
            f"Publication object {target} resolves outside the SDP directory."
        )
    return target[len(prefix):].replace("\\", "/")


def _reject_dot_segments(path: object, field: str) -> None:
    """Mirror ``.ms_knb_reject_dot_segments``."""
    parts = str(path).replace("\\", "/").split("/")
    if any(part in (".", "..") for part in parts):
        raise ValueError(
            f"Publication path {path} in {field} contains a forbidden dot "
            "path segment."
        )


def _locate_metadata_file(path: Union[str, Path], file_name: str) -> Optional[str]:
    """Mirror ``.ms_locate_metadata_file``."""
    for candidate in (
        os.path.join(str(path), "metadata", file_name),
        os.path.join(str(path), file_name),
    ):
        if os.path.exists(candidate):
            return candidate
    return None


def _read_metadata_csv(path: str):
    from .metadata import read_sdp_csv

    return read_sdp_csv(path)


def _declared_data_paths(path: str) -> "Dict[str, str]":
    """Mirror ``.ms_knb_declared_data_paths``: only files ``tables.csv`` names."""
    tables_path = _locate_metadata_file(path, "tables.csv")
    if tables_path is None:
        raise FileNotFoundError(
            "KNB publication requires canonical metadata/tables.csv."
        )
    tables = _read_metadata_csv(tables_path)
    if "file_name" not in tables.columns or len(tables) == 0:
        raise ValueError(
            "KNB publication requires non-empty tables.csv$file_name values."
        )
    paths: Dict[str, str] = {}
    for file_name in [str(value) for value in tables["file_name"]]:
        _reject_dot_segments(file_name, "tables.csv$file_name")
        paths["data:" + file_name] = _eml._resource_path(path, file_name)
    return paths


def _require_review_ledger_binding(path: str, mapping: object) -> None:
    """Assert the EML mapping binds the reviewed ledger this package uses.

    Split out of ``_sdp_artifact_paths`` deliberately (PARITY.md row 34). R
    keeps this check inside ``.ms_knb_sdp_artifact_paths()``, which it can
    because ``yaml`` is a hard Import for metasalmon -- R has no core/optional
    dependency split to respect. Python does: PyYAML lives in the ``[eml]``
    extra. Leaving the mapping read inside the inventory helper made the
    deterministic SDP archive, which needs nothing beyond pandas, require that
    extra to build, and took its core-deps test coverage with it.

    Nothing about the inventory needed the mapping: which ledger is published
    is decided by what is on disk. Only these two coherence assertions need
    it, and ``publish_sdp_to_knb()`` has already required the extra long
    before they run. Running them here fires them ahead of both representation
    branches and before any archive is written -- strictly earlier than they
    fired when they lived in the helper.

    A package with neither ledger is deliberately left alone here so the
    inventory helper still reports R's "requires a reviewed semantic-selection
    ledger" message rather than a binding complaint about a file that does not
    exist.
    """
    mapped_review = str(
        ((mapping or {}).get("semantic_review") or {}).get("path") or ""
    )
    if os.path.exists(os.path.join(path, _REPRODUCIBILITY_MANIFEST)):
        if mapped_review != _CANONICAL_REVIEW_LEDGER:
            raise ValueError(
                "EML mapping semantic_review.path must bind the reviewed "
                "ledger declared by the reproducibility manifest."
            )
    elif os.path.exists(os.path.join(path, _LEGACY_REVIEW_LEDGER)):
        if mapped_review != _LEGACY_REVIEW_LEDGER:
            raise ValueError(
                "Legacy KNB packages must bind the root-level reviewed ledger "
                "in EML mapping semantic_review.path."
            )


def _sdp_artifact_paths(path: str) -> "Dict[str, str]":
    """Mirror ``.ms_knb_sdp_artifact_paths``: a closed, manifest-bound inventory."""
    missing = [
        name
        for name in _REQUIRED_SDP_ARTIFACTS
        if not os.path.exists(os.path.join(path, name))
    ]
    if missing:
        raise FileNotFoundError(
            "KNB publication requires canonical SDP artifact(s): "
            + ", ".join(missing)
            + "."
        )

    # The v0.2 extended layout keeps reviewed selections with the workflow,
    # provenance, and source records they qualify. Retain the root-level ledger
    # only as a compatibility path for already reviewed packages. A canonical
    # reproducibility tree is valid only when its exact contents are declared
    # by the checksum-bound manifest; publication never discovers extra files.
    #
    # Which ledger gets published is decided entirely by what is on disk. The
    # paired assertion -- that the EML mapping *binds* the ledger this package
    # actually uses -- needs the mapping sidecar, and therefore PyYAML, so it
    # lives in ``_require_review_ledger_binding`` and runs in the publication
    # preflight instead. See PARITY.md row 34: keeping it here made the
    # deterministic SDP archive require the ``[eml]`` extra to build.
    if os.path.exists(os.path.join(path, _REPRODUCIBILITY_MANIFEST)):
        from .reproducibility import (
            read_sdp_reproducibility_manifest,
            validate_sdp_reproducibility_manifest,
        )

        validate_sdp_reproducibility_manifest(path)
        manifest = read_sdp_reproducibility_manifest(path, validate=False)
        declared_paths = [
            str(artifact["path"]) for artifact in manifest["artifacts"]
        ]
        if _CANONICAL_REVIEW_LEDGER not in declared_paths:
            raise ValueError(
                "KNB publication requires the canonical reviewed-selection "
                "ledger to be declared by reproducibility/manifest.json."
            )
        reproducibility_relative = [_REPRODUCIBILITY_MANIFEST] + declared_paths
    else:
        if not os.path.exists(os.path.join(path, _LEGACY_REVIEW_LEDGER)):
            raise FileNotFoundError(
                "KNB publication requires a reviewed semantic-selection "
                "ledger. Use the extended reproducibility/manifest.json layout "
                "or the legacy root-level ledger."
            )
        reproducibility_relative = [_LEGACY_REVIEW_LEDGER]

    # SSSOM supplements are optional, but when present they are closed by the
    # metasalmon-generated manifest. Validate that manifest and include only
    # the files it names; never scan the semantic directory and accidentally
    # publish an editor backup, private review note, or unapproved draft.
    semantic_relative: List[str] = []
    semantic_manifest = os.path.join(
        path, "metadata", "semantic", "mapping-sets.json"
    )
    if os.path.exists(semantic_manifest):
        from .sssom import validate_sdp_sssom

        validate_sdp_sssom(path)
        with open(semantic_manifest, encoding="utf-8") as handle:
            manifest = json.load(handle)
        semantic_relative = ["metadata/semantic/mapping-sets.json"] + [
            str(mapping_set["path"])
            for mapping_set in manifest.get("mapping_sets", [])
        ]

    # Ordered measurement decompositions use their own closed manifest because
    # they are semantic components, not SSSOM term mappings.
    decomposition_relative: List[str] = []
    decomposition_manifest = os.path.join(
        path, "metadata", "semantic", "measurement-decompositions.json"
    )
    if os.path.exists(decomposition_manifest):
        from .measurement_decompositions import (
            validate_sdp_measurement_decompositions,
        )

        validate_sdp_measurement_decompositions(path)
        with open(decomposition_manifest, encoding="utf-8") as handle:
            manifest = json.load(handle)
        decomposition_relative = [
            "metadata/semantic/measurement-decompositions.json",
            str(manifest["artifact"]["path"]),
        ]

    # Mixed-grain observation structures are optional metadata. When present
    # they are validated as one complete contract and become named objects in
    # the expanded representation. A methods.csv is an sdp-0.2.0 registry and
    # must be migrated, not published.
    methods_relative: List[str] = []
    if os.path.exists(os.path.join(path, "metadata", "methods.csv")):
        raise ValueError(
            "metadata/methods.csv is an sdp-0.2.0 registry; sdp-0.3.0 "
            "packages must not carry one. Run migrate_sdp_methods() to "
            "relocate its content and remove it."
        )

    structure_present = [
        os.path.exists(os.path.join(path, name))
        for name in _OBSERVATION_STRUCTURE_FILES
    ]
    structure_relative: List[str] = []
    if any(structure_present):
        if not all(structure_present):
            raise ValueError(
                "KNB publication requires both canonical observation-structure "
                "files when either is present."
            )
        from .observation_structures import validate_sdp_observation_structures

        validate_sdp_observation_structures(path)
        structure_relative = list(_OBSERVATION_STRUCTURE_FILES)

    relative = sorted(
        list(_REQUIRED_SDP_ARTIFACTS)
        + reproducibility_relative
        + semantic_relative
        + decomposition_relative
        + methods_relative
        + structure_relative
    )
    return {
        "sdp_artifact:"
        + item: _inside_path(path, os.path.join(path, item), must_work=True)
        for item in relative
    }


def _publication_paths(
    path: str, eml_path: str, manifest_path: str
) -> Dict[str, object]:
    """Mirror ``.ms_knb_publication_paths``."""
    from . import knb_archive

    _reject_dot_segments(eml_path, "eml_path")
    _reject_dot_segments(manifest_path, "manifest_path")
    eml_path = _inside_path(
        path, eml_path, must_work=os.path.exists(eml_path)
    )
    manifest_path = _inside_path(
        path, manifest_path, must_work=os.path.exists(manifest_path)
    )
    resource_map_candidate = os.path.join(
        os.path.dirname(manifest_path), "resource-map.rdf"
    )
    resource_map_path = _inside_path(
        path,
        resource_map_candidate,
        must_work=os.path.exists(resource_map_candidate),
    )
    archive_candidate = os.path.join(
        path,
        "publication",
        knb_archive._sdp_archive_filename(
            knb_archive._sdp_archive_dataset_id(path)
        ),
    )
    archive_path = _inside_path(
        path, archive_candidate, must_work=os.path.exists(archive_candidate)
    )
    data_paths = _declared_data_paths(path)
    all_paths = dict(data_paths)
    all_paths["eml"] = eml_path
    all_paths["manifest"] = manifest_path
    all_paths["resource_map"] = resource_map_path
    all_paths["archive"] = archive_path
    seen: Dict[str, List[str]] = {}
    for label, value in all_paths.items():
        seen.setdefault(value, []).append(label)
    colliding = [
        label
        for labels in seen.values()
        if len(labels) > 1
        for label in labels
    ]
    if colliding:
        raise ValueError(
            "KNB publication path collision among " + ", ".join(colliding) + "."
        )
    return {
        "eml_path": eml_path,
        "manifest_path": manifest_path,
        "resource_map_path": resource_map_path,
        "archive_path": archive_path,
        "data_paths": list(data_paths.values()),
    }


# --- bytes, hashes, identifiers -------------------------------------------------------


def _object_bytes(path: Union[str, Path]) -> bytes:
    with open(str(path), "rb") as handle:
        return handle.read()


def _sha256_raw(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_text(value: object) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _resolve_url(pid: str) -> str:
    """Mirror ``.ms_knb_resolve_url`` (R's ``URLencode(reserved = TRUE)``)."""
    return RESOLVER + quote(str(pid), safe="")


def _resource_map_pid(
    package_id: str,
    publication_date: object,
    member_objects: Sequence[Dict[str, object]],
) -> str:
    """Mirror ``.ms_knb_resource_map_pid``: a UUIDv5 over the exact membership."""
    member_lines = [
        "\t".join(
            [
                str(member["role"]),
                str(member["path"]),
                str(member["pid"]),
                str(member["format_id"]),
                _format_size(member["size"]),
                str(member["sha256"]),
            ]
        )
        for member in member_objects
    ]
    preimage = "\n".join(
        [ORE_PROFILE, str(package_id), str(publication_date)]
        + sorted(member_lines)
    )
    return "urn:uuid:" + _eml._uuid5("resource-map:" + preimage)


def _format_size(size: object) -> str:
    """R pastes a numeric size; a whole double prints without a decimal part."""
    number = float(size)
    if number == int(number):
        return str(int(number))
    return repr(number)


# --- OAI-ORE resource map --------------------------------------------------------------


def _add_resource(parent: ET.Element, name: str, resource: str) -> ET.Element:
    """Mirror ``.ms_knb_add_resource``."""
    node = ET.SubElement(parent, name)
    node.set("rdf:resource", resource)
    return node


def _add_identifier(parent: ET.Element, identifier: str) -> ET.Element:
    """Mirror ``.ms_knb_add_identifier``."""
    node = ET.SubElement(parent, "dcterms:identifier")
    node.text = identifier
    node.set("rdf:datatype", _XSD_STRING)
    return node


def _build_ore(
    resource_map_pid: str,
    package_id: str,
    publication_date: object,
    member_objects: Sequence[Dict[str, object]],
) -> ET.Element:
    """Mirror ``.ms_knb_build_ore``.

    Built with literal ``prefix:local`` names and explicit ``xmlns:*``
    attributes, exactly as R's xml2 code constructs it, so the serialized
    document carries the same five prefixes in the same places.
    """
    root = ET.Element("rdf:RDF")
    for name, uri in _ORE_NAMESPACES:
        root.set(name, uri)

    resource_map_url = _resolve_url(resource_map_pid)
    aggregation_pid = resource_map_pid + "#aggregation"
    aggregation_url = resource_map_url + "#aggregation"
    metadata_url = _resolve_url(package_id)

    resource_map = ET.SubElement(root, "rdf:Description")
    resource_map.set("rdf:about", resource_map_url)
    _add_identifier(resource_map, resource_map_pid)
    _add_resource(
        resource_map,
        "rdf:type",
        "http://www.openarchives.org/ore/terms/ResourceMap",
    )
    _add_resource(resource_map, "ore:describes", aggregation_url)
    _add_resource(
        resource_map,
        "dcterms:creator",
        "https://github.com/salmon-data-mobilization/metasalmon",
    )
    modified = ET.SubElement(resource_map, "dcterms:modified")
    modified.text = str(publication_date)

    aggregation = ET.SubElement(root, "rdf:Description")
    aggregation.set("rdf:about", aggregation_url)
    _add_identifier(aggregation, aggregation_pid)
    _add_resource(
        aggregation,
        "rdf:type",
        "http://www.openarchives.org/ore/terms/Aggregation",
    )
    _add_resource(aggregation, "ore:isDescribedBy", resource_map_url)

    role_order = (
        "metadata",
        "data",
        "sdp_archive",
        "sdp_artifact",
    )

    def role_rank(member: Dict[str, object]) -> int:
        role = str(member["role"])
        return role_order.index(role) if role in role_order else len(role_order)

    for member in sorted(member_objects, key=role_rank):
        _add_resource(
            aggregation, "ore:aggregates", _resolve_url(str(member["pid"]))
        )

    metadata = ET.SubElement(root, "rdf:Description")
    metadata.set("rdf:about", metadata_url)
    _add_identifier(metadata, package_id)
    _add_resource(metadata, "ore:isAggregatedBy", aggregation_url)
    # Every aggregated member records where it sits inside the package. That
    # is what lets a consumer of the expanded representation rebuild the SDP
    # hierarchy from flat DataONE objects; the archive representation carries
    # the same statement for its single ZIP.
    metadata_member = [
        member
        for member in member_objects
        if str(member["role"]) == "metadata"
    ][0]
    location = ET.SubElement(metadata, "prov:atLocation")
    location.text = str(metadata_member["path"])

    documented = [
        member
        for member in member_objects
        if str(member["role"]) in ("data", "sdp_archive", "sdp_artifact")
    ]
    for member in documented:
        object_url = _resolve_url(str(member["pid"]))
        _add_resource(metadata, "cito:documents", object_url)
        description = ET.SubElement(root, "rdf:Description")
        description.set("rdf:about", object_url)
        _add_resource(description, "cito:isDocumentedBy", metadata_url)
        _add_identifier(description, str(member["pid"]))
        _add_resource(description, "ore:isAggregatedBy", aggregation_url)
        member_location = ET.SubElement(description, "prov:atLocation")
        member_location.text = str(member["path"])

    return root


def _xml_bytes(document: ET.Element) -> bytes:
    """Deterministic UTF-8 serialization (2-space indent, trailing LF).

    R renders through libxml2's ``format`` writer; the bytes differ from
    ElementTree's while the document is identical (PARITY.md entry 4).
    """
    ET.indent(document, space="  ")
    body = ET.tostring(document, encoding="unicode")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n").encode(
        "utf-8"
    )


def _local_name(tag: str) -> str:
    if tag.startswith("{"):
        tag = tag.split("}", 1)[1]
    return tag.rsplit(":", 1)[-1]


def _find_all_local(root: ET.Element, name: str) -> List[ET.Element]:
    return [node for node in root.iter() if _local_name(node.tag) == name]


def _rdf_attr(node: ET.Element, name: str) -> Optional[str]:
    for key, value in node.attrib.items():
        if _local_name(key) == name:
            return value
    return None


def _validate_ore(
    document: ET.Element,
    resource_map_pid: str,
    member_objects: Sequence[Dict[str, object]],
) -> bool:
    """Mirror ``.ms_knb_validate_ore``: the emitted graph must be the plan."""
    aggregates = [
        _rdf_attr(node, "resource")
        for node in _find_all_local(document, "aggregates")
    ]
    expected = [_resolve_url(str(member["pid"])) for member in member_objects]
    if (
        set(aggregates) != set(expected)
        or len(set(aggregates)) != len(aggregates)
        or len(aggregates) != len(expected)
    ):
        raise ValueError(
            "Generated OAI-ORE aggregate set does not exactly match the "
            "planned EML/data objects."
        )

    resource_map_url = _resolve_url(resource_map_pid)
    aggregation_url = resource_map_url + "#aggregation"
    descriptions = _find_all_local(document, "Description")
    expected_identifiers = [(resource_map_url, resource_map_pid)]
    expected_identifiers.append(
        (aggregation_url, resource_map_pid + "#aggregation")
    )
    for member, url in zip(member_objects, expected):
        expected_identifiers.append((url, str(member["pid"])))
    for url, identifier in expected_identifiers:
        matches = [
            node
            for node in descriptions
            if _rdf_attr(node, "about") == url
        ]
        identifiers = [
            (node.text or "")
            for match in matches
            for node in match
            if _local_name(node.tag) == "identifier"
        ]
        if (
            len(matches) != 1
            or len(identifiers) != 1
            or identifiers[0] != identifier
        ):
            raise ValueError(
                "Generated OAI-ORE lacks the exact DataONE identifier for "
                f"represented resource {url}."
            )

    described_by = [
        _rdf_attr(child, "resource")
        for node in descriptions
        for child in node
        if _local_name(child.tag) == "isDescribedBy"
        and any(
            _local_name(sibling.tag) == "type"
            and _rdf_attr(sibling, "resource")
            == "http://www.openarchives.org/ore/terms/Aggregation"
            for sibling in node
        )
    ]
    if described_by != [resource_map_url]:
        raise ValueError(
            "Generated OAI-ORE aggregation must be described by its resource "
            "map."
        )

    documented_urls = [
        _resolve_url(str(member["pid"]))
        for member in member_objects
        if str(member["role"]) in ("data", "sdp_archive", "sdp_artifact")
    ]
    metadata_object = [
        member
        for member in member_objects
        if str(member["role"]) == "metadata"
    ][0]
    metadata_url = _resolve_url(str(metadata_object["pid"]))
    documents = [
        _rdf_attr(node, "resource")
        for node in _find_all_local(document, "documents")
    ]
    documented_by = [
        _rdf_attr(node, "resource")
        for node in _find_all_local(document, "isDocumentedBy")
    ]
    aggregated_by = [
        _rdf_attr(node, "resource")
        for node in _find_all_local(document, "isAggregatedBy")
    ]
    locations = [
        (node.text or "")
        for node in _find_all_local(document, "atLocation")
    ]
    expected_locations = [str(member["path"]) for member in member_objects]
    if (
        set(documents) != set(documented_urls)
        or len(documents) != len(documented_urls)
        or len(documented_by) != len(documented_urls)
        or any(value != metadata_url for value in documented_by)
        or len(aggregated_by) != len(expected)
        or any(value != aggregation_url for value in aggregated_by)
        or set(locations) != set(expected_locations)
        or len(locations) != len(expected_locations)
        or len(set(locations)) != len(locations)
    ):
        raise ValueError(
            "Generated OAI-ORE package relationships do not match the "
            "publication profile."
        )

    xml = _xml_bytes(document).decode("utf-8")
    if (
        "file:" in xml
        or "REVIEW:" in xml
        or quote(resource_map_pid, safe="") not in xml
    ):
        raise ValueError(
            "Generated OAI-ORE contains a local/review marker or does not "
            "identify its resource map."
        )
    return True


# --- atomic writes and JSON encoding ---------------------------------------------------


def _atomic_write_raw(payload: bytes, path: str) -> str:
    """Mirror ``.ms_knb_atomic_write_raw``: identical bytes are a no-op."""
    directory = os.path.dirname(path)
    if not os.path.isdir(directory):
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError:
            pass
    if not os.path.isdir(directory):
        raise ValueError(
            f"Could not create publication artifact directory {directory}."
        )
    # R writes the staging file first and only then compares, but its
    # comparison is against ``bytes``, not against the staged file -- so the
    # staged write contributes nothing to the identical-bytes decision. Making
    # the check first is the same behaviour with one fewer temporary file.
    if os.path.exists(path) and _object_bytes(path) == payload:
        return path
    # Goes through the shared writer so publication artifacts land at the
    # umask default, exactly as R's writeBin + file.rename does. Writing
    # ``mkstemp`` + ``os.replace`` inline here is what published the manifest,
    # the resource map and the SDP archive as 0600 (PARITY.md row 24).
    atomic_write(payload, path)
    return path


def _json_bytes(value: object) -> bytes:
    """Mirror ``.ms_knb_json_bytes``.

    ``jsonlite::toJSON(pretty = TRUE)`` and ``json.dumps(indent=2)`` produce
    byte-identical output for these documents; the trailing newline matches
    R's. Fingerprints hash these bytes, so this encoding is a contract.
    """
    return (
        json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    ).encode("utf-8")


# --- plan fingerprint and recovery manifest -------------------------------------------


def _fingerprint_object(obj: Dict[str, object]) -> Dict[str, object]:
    """Mirror ``.ms_knb_fingerprint_object``: reduce to wire-level scalars."""

    def scalar(field: str) -> Optional[str]:
        value = obj.get(field)
        if isinstance(value, (list, tuple)):
            value = value[0] if value else None
        if value is None:
            return None
        return str(value)

    size = obj.get("size")
    if isinstance(size, (list, tuple)):
        size = size[0] if size else None
    size = None if size is None else _numeric(size)

    return {
        "role": scalar("role"),
        "path": scalar("path"),
        "pid": scalar("pid"),
        "format_id": scalar("format_id"),
        "media_type": scalar("media_type"),
        "size": size,
        "sha256": scalar("sha256"),
        "obsoletes": _optional_scalar(obj.get("obsoletes")),
    }


def _numeric(value: object) -> Union[int, float]:
    number = float(value)
    return int(number) if number == int(number) else number


def _plan_fingerprint(plan: Dict[str, object]) -> str:
    """Mirror ``.ms_knb_plan_fingerprint``."""
    revision_of = plan.get("revision_of")
    fingerprint = {
        "schema_version": 3,
        "environment": plan.get("environment"),
        "node_id": plan.get("node_id"),
        "public": plan.get("public"),
        "replication_policy": plan.get("replication_policy"),
        "expected_subject": plan.get("expected_subject"),
        "rights_authorization": plan.get("rights_authorization"),
        "package_id": plan.get("package_id"),
        "series_id": plan.get("series_id"),
        "representation": plan.get("representation"),
        "revision_of": revision_of if revision_of else None,
        "ore_profile": ORE_PROFILE,
        "objects": [
            _fingerprint_object(obj) for obj in plan.get("objects") or []
        ],
    }
    return _sha256_raw(_json_bytes(fingerprint))


def _status_rank(status: object) -> int:
    """Mirror ``.ms_knb_status_rank``."""
    return _STATUS_RANKS.get(str(status), 0)


def _advance_status(current: object, candidate: object) -> object:
    """Mirror ``.ms_knb_advance_status``."""
    if _status_rank(current) >= _status_rank(candidate):
        return current
    return candidate


def _manifest(
    plan: Dict[str, object],
    status: str = "dry_run",
    previous: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    """Mirror ``.ms_knb_manifest``: the durable recovery record."""
    previous_objects = _manifest_objects(previous)
    previous_states = {
        str(obj.get("pid")): obj.get("state") for obj in previous_objects
    }
    objects = []
    for obj in plan.get("objects") or []:
        state = previous_states.get(str(obj.get("pid")), "planned")
        objects.append(
            {
                "role": obj.get("role"),
                "path": obj.get("path"),
                "pid": obj.get("pid"),
                "format_id": obj.get("format_id"),
                "media_type": obj.get("media_type"),
                "size": obj.get("size"),
                "sha256": obj.get("sha256"),
                "obsoletes": _optional_scalar(obj.get("obsoletes")),
                "state": state,
            }
        )
    previous_status = None if previous is None else previous.get("status")
    durable_status = (
        previous_status
        if _status_rank(previous_status) > _status_rank(status)
        else status
    )
    return {
        "schema_version": 3,
        "status": durable_status,
        "environment": plan.get("environment"),
        "node_id": plan.get("node_id"),
        "public": plan.get("public"),
        "replication_policy": plan.get("replication_policy"),
        "expected_subject": plan.get("expected_subject"),
        "rights_authorization": plan.get("rights_authorization"),
        "package_id": plan.get("package_id"),
        "series_id": plan.get("series_id"),
        "representation": plan.get("representation"),
        "revision_of": plan.get("revision_of"),
        "plan_sha256": plan.get("plan_sha256"),
        "metadata_pid": plan.get("metadata_pid"),
        "resource_map_pid": plan.get("resource_map_pid"),
        "objects": objects,
        "catalog_verified": bool(
            previous is not None and previous.get("catalog_verified") is True
        ),
        "catalog_evidence": (
            []
            if previous is None or previous.get("catalog_evidence") is None
            else previous.get("catalog_evidence")
        ),
    }


def _existing_manifest(path: str) -> Optional[Dict[str, object]]:
    """Mirror ``.ms_knb_existing_manifest``."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except ValueError as error:
        raise ValueError(
            f"Existing publication manifest {path} is not valid JSON: {error}"
        ) from None


def _manifest_objects(
    manifest: Optional[Dict[str, object]],
) -> List[Dict[str, object]]:
    """Mirror ``.ms_knb_manifest_objects``."""
    if manifest is None or manifest.get("objects") is None:
        return []
    return list(manifest["objects"])


def _manifest_fingerprint(manifest: Dict[str, object]) -> Optional[str]:
    """Mirror ``.ms_knb_manifest_fingerprint`` (schema 2 and 3)."""
    try:
        schema_version = int(manifest.get("schema_version"))
    except (TypeError, ValueError):
        return None
    objects = _manifest_objects(manifest)
    if schema_version == 2:
        fingerprint = {
            "schema_version": 2,
            "environment": manifest.get("environment"),
            "node_id": manifest.get("node_id"),
            "public": manifest.get("public"),
            "replication_policy": manifest.get("replication_policy"),
            "expected_subject": manifest.get("expected_subject"),
            "rights_authorization": manifest.get("rights_authorization"),
            "package_id": manifest.get("package_id"),
            "series_id": manifest.get("series_id"),
            "ore_profile": ORE_PROFILE,
            "objects": [
                {
                    field: obj.get(field)
                    for field in (
                        "role",
                        "path",
                        "pid",
                        "format_id",
                        "media_type",
                        "size",
                        "sha256",
                    )
                }
                for obj in objects
            ],
        }
        return _sha256_raw(_json_bytes(fingerprint))
    if schema_version == 3:
        candidate = dict(manifest)
        candidate["objects"] = objects
        return _plan_fingerprint(candidate)
    return None


def _revision_manifest(path: Optional[str]) -> Optional[Dict[str, object]]:
    """Mirror ``.ms_knb_revision_manifest``."""
    if path is None:
        return None
    _reject_dot_segments(path, "revision_manifest")
    if not os.path.exists(path) or os.path.isdir(path):
        raise FileNotFoundError(
            f"Prior KNB revision manifest {path} does not exist."
        )
    manifest = _existing_manifest(os.path.realpath(path))
    objects = _manifest_objects(manifest)
    roles = [str(obj.get("role")) for obj in objects]
    states = [str(obj.get("state")) for obj in objects]
    try:
        schema_version = int(manifest.get("schema_version"))
    except (TypeError, ValueError):
        schema_version = None
    valid = (
        schema_version in (2, 3)
        and str(manifest.get("status"))
        in ("published_pending_catalog", "complete")
        and str(manifest.get("environment")) == ENVIRONMENT
        and str(manifest.get("node_id")) == NODE_ID
        and len(objects) > 0
        and roles.count("metadata") == 1
        and roles.count("resource_map") == 1
        and all(state == "verified" for state in states)
        and _manifest_fingerprint(manifest)
        == _optional_scalar(manifest.get("plan_sha256"))
    )
    if not valid:
        raise ValueError(
            "A KNB revision requires a verified schema-version 2 or 3 "
            "published manifest with an intact plan fingerprint."
        )
    manifest["objects"] = objects
    return manifest


def _revision_context(
    prior: Optional[Dict[str, object]], plan: Dict[str, object]
) -> Optional[Dict[str, object]]:
    """Mirror ``.ms_knb_revision_context``."""
    if prior is None:
        return None
    if bool(prior.get("public")) is not bool(plan.get("public")):
        raise ValueError(
            "KNB revision planning cannot also change public/private access."
        )
    if _optional_scalar(prior.get("series_id")) != plan.get("series_id"):
        raise ValueError(
            "The prior KNB manifest belongs to a different metadata series."
        )
    objects = _manifest_objects(prior)
    prior_metadata = [
        obj for obj in objects if str(obj.get("role")) == "metadata"
    ][0]
    prior_resource_map = [
        obj for obj in objects if str(obj.get("role")) == "resource_map"
    ][0]
    return {
        "schema_version": int(prior.get("schema_version")),
        "plan_sha256": _optional_scalar(prior.get("plan_sha256")),
        "metadata_pid": _optional_scalar(prior_metadata.get("pid")),
        "resource_map_pid": _optional_scalar(prior_resource_map.get("pid")),
    }


def _require_new_revision_pids(
    revision: Optional[Dict[str, object]],
    metadata_pid: str,
    resource_map_pid: str,
) -> None:
    """Mirror ``.ms_knb_require_new_revision_pids``."""
    if revision is None:
        return
    reused = []
    if revision.get("metadata_pid") == metadata_pid:
        reused.append("metadata")
    if revision.get("resource_map_pid") == resource_map_pid:
        reused.append("resource-map")
    if not reused:
        return
    role_text = " and ".join(reused)
    raise ValueError(
        f"KNB revision planning would reuse the prior {role_text} PID(s). "
        "Choose a new publication.revision_key so the revision mints new "
        "immutable metadata and resource-map PIDs."
    )


def _assert_resource_map_owned(
    plan: Dict[str, object], previous: Optional[Dict[str, object]]
) -> None:
    """Mirror ``.ms_knb_assert_resource_map_owned``."""
    resource_map_path = str(plan["resource_map_path"])
    if not os.path.exists(resource_map_path):
        return
    previous_objects = _manifest_objects(previous)
    resource_maps = [
        obj
        for obj in previous_objects
        if str(obj.get("role")) == "resource_map"
    ]
    owned = (
        previous is not None
        and _optional_scalar(previous.get("plan_sha256"))
        == plan.get("plan_sha256")
        and len(resource_maps) == 1
        and _optional_scalar(resource_maps[0].get("path"))
        == _relative_path(plan["package_path"], resource_map_path)
        and _optional_scalar(resource_maps[0].get("pid"))
        == plan.get("resource_map_pid")
        and _optional_scalar(resource_maps[0].get("sha256"))
        == _sha256_raw(_object_bytes(resource_map_path))
        and _object_bytes(resource_map_path) == plan.get("resource_map_bytes")
    )
    if not owned:
        raise ValueError(
            "The pre-existing resource map file is not owned by the exact "
            "matching publication manifest."
        )


def _require_reviewed_manifest(
    previous: Optional[Dict[str, object]], plan: Dict[str, object]
) -> None:
    """Mirror ``.ms_knb_require_reviewed_manifest``."""
    reviewed_fingerprint = None
    if previous is not None:
        reviewed = dict(previous)
        reviewed["objects"] = _manifest_objects(reviewed)
        try:
            reviewed_fingerprint = _plan_fingerprint(reviewed)
        except Exception:
            reviewed_fingerprint = None
    try:
        schema_version = int(previous.get("schema_version"))
    except (TypeError, ValueError, AttributeError):
        schema_version = None
    valid = (
        previous is not None
        and schema_version == 3
        and str(previous.get("status"))
        in ("dry_run", "pending", "published_pending_catalog", "complete")
        and previous.get("replication_policy")
        == plan.get("replication_policy")
        and _optional_scalar(previous.get("plan_sha256"))
        == plan.get("plan_sha256")
        and reviewed_fingerprint == plan.get("plan_sha256")
    )
    if not valid:
        raise ValueError(
            "Live KNB publication requires a reviewed schema version 3 "
            "manifest with the exact replication policy and recomputed plan "
            "fingerprint."
        )


def _require_rights_authorization(plan: Dict[str, object]) -> None:
    """Mirror ``.ms_knb_require_rights_authorization``."""
    if plan.get("public") is not True:
        return
    authorization = plan.get("rights_authorization")
    status = (
        _optional_scalar(authorization.get("status"))
        if isinstance(authorization, dict)
        else None
    )
    evidence_value = (
        authorization.get("evidence") if isinstance(authorization, dict) else None
    )
    if evidence_value is None:
        evidence: List[str] = []
    elif isinstance(evidence_value, (list, tuple)):
        evidence = [str(item).strip() for item in evidence_value]
    else:
        evidence = [str(evidence_value).strip()]
    evidence = [item for item in evidence if item]
    if status != "confirmed" or not evidence:
        raise ValueError(
            "Public KNB publication requires confirmed redistribution rights "
            "in the reviewed EML sidecar. confirm=True approves the exact "
            "plan; it is not rights evidence."
        )


def _reject_review_candidate_annotations(path: str) -> None:
    """Mirror ``.ms_knb_reject_review_candidate_annotations``."""
    vocabulary_path = os.path.join(path, "metadata", "semantic_vocabulary.csv")
    vocabulary = _read_metadata_csv(vocabulary_path)
    required = ("iri", "source", "ontology")
    missing = [name for name in required if name not in vocabulary.columns]
    if missing:
        raise ValueError(
            "KNB publication requires semantic_vocabulary.csv fields: "
            + ", ".join(required)
            + "."
        )

    candidate_iris = []
    for index in range(len(vocabulary)):
        status_text = (
            str(vocabulary["source"].iloc[index])
            + " "
            + str(vocabulary["ontology"].iloc[index])
        ).lower()
        if _REVIEW_CANDIDATE_RE.search(status_text):
            iri = str(vocabulary["iri"].iloc[index]).strip()
            if iri and iri not in candidate_iris:
                candidate_iris.append(iri)
    if not candidate_iris:
        return

    text_parts = []
    for name in ("dataset.csv", "tables.csv", "column_dictionary.csv", "codes.csv"):
        with open(
            os.path.join(path, "metadata", name), encoding="utf-8"
        ) as handle:
            text_parts.append(handle.read())
    text = "\n".join(text_parts)
    referenced = [iri for iri in candidate_iris if iri in text]
    if referenced:
        raise ValueError(
            "KNB publication cannot emit annotations to review-candidate "
            "vocabulary IRIs: "
            + ", ".join(referenced)
            + ". Publish those concepts in a governed provisional/stable "
            "vocabulary release, rebuild the SDP against that release, or "
            "remove the annotations."
        )


# --- planned objects -------------------------------------------------------------------


def _sdp_artifact_object(
    local_path: str, path: str, dataset_id: str
) -> Dict[str, object]:
    """Mirror ``.ms_knb_sdp_artifact_object`` (the legacy expanded shape)."""
    relative = _relative_path(path, local_path)
    extension = os.path.splitext(relative)[1].lstrip(".").lower()
    payload = _object_bytes(local_path)
    sha256 = _sha256_raw(payload)
    return {
        "role": "sdp_artifact",
        "path": relative,
        "local_path": local_path,
        "pid": "urn:uuid:"
        + _eml._uuid5(
            ":".join(["sdp-artifact", dataset_id, relative, sha256])
        ),
        "format_id": _ARTIFACT_FORMAT_IDS.get(
            extension, "application/octet-stream"
        ),
        "media_type": _ARTIFACT_MEDIA_TYPES.get(
            extension, "application/octet-stream"
        ),
        "size": len(payload),
        "sha256": sha256,
        "series_id": None,
    }


def _sdp_archive_object(
    archive: Dict[str, object], path: str, dataset_id: str
) -> Dict[str, object]:
    """Mirror ``.ms_knb_sdp_archive_object``."""
    relative = _relative_path(path, archive["path"])
    return {
        "role": "sdp_archive",
        "path": relative,
        "local_path": archive["path"],
        "pid": "urn:uuid:"
        + _eml._uuid5(
            ":".join(["sdp-archive", dataset_id, str(archive["sha256"])])
        ),
        "format_id": archive["format_id"],
        "media_type": archive["media_type"],
        "size": _numeric(archive["size"]),
        "sha256": archive["sha256"],
        "series_id": None,
        "obsoletes": None,
        "obsoleted_by": None,
    }


def _sdp_artifact_objects(path: str, dataset_id: str) -> List[Dict[str, object]]:
    """Mirror ``.ms_knb_sdp_artifact_objects``: the closed expanded inventory."""
    objects = []
    for local_path in _sdp_artifact_paths(path).values():
        artifact = _sdp_artifact_object(local_path, path, dataset_id)
        artifact["obsoletes"] = None
        artifact["obsoleted_by"] = None
        objects.append(artifact)
    return objects


def _supplementary_object_plan(
    objects: Sequence[Dict[str, object]]
) -> Optional[Dict[str, List[object]]]:
    """Mirror ``.ms_knb_supplementary_object_plan``.

    An archive is named by its basename and described as the whole package; an
    expanded artifact is named by its package-relative path, which is what
    lets a consumer rebuild the SDP hierarchy from the deposited objects.
    """
    if not objects:
        return None

    def is_archive(item: Dict[str, object]) -> bool:
        return str(item["role"]) == "sdp_archive"

    return {
        "path": [str(item["local_path"]) for item in objects],
        "pid": [str(item["pid"]) for item in objects],
        "format_id": [str(item["format_id"]) for item in objects],
        "checksum": [str(item["sha256"]) for item in objects],
        "object_name": [
            os.path.basename(str(item["path"]))
            if is_archive(item)
            else str(item["path"])
            for item in objects
        ],
        "entity_name": [
            "Canonical Salmon Data Package"
            if is_archive(item)
            else "Salmon Data Package artifact: " + str(item["path"])
            for item in objects
        ],
        "description": [
            (
                "A complete, validated Salmon Data Package containing the "
                "source data, canonical SDP metadata, reviewed semantic "
                "selections, SSSOM mapping sets, and measurement-decomposition "
                "artifacts."
            )
            if is_archive(item)
            else (
                "Canonical file from the expanded Salmon Data Package at "
                "'" + str(item["path"]) + "'."
            )
            for item in objects
        ],
        "size": [_numeric(item["size"]) for item in objects],
        "entity_type": [
            "Salmon Data Package archive"
            if is_archive(item)
            else "Salmon Data Package artifact"
            for item in objects
        ],
    }


def _result_archive_path(plan: Dict[str, object]) -> Optional[str]:
    """Mirror ``.ms_knb_result_archive_path``: no ZIP means no archive path."""
    archive_path = plan.get("sdp_archive_path")
    if archive_path is None:
        return None
    return os.path.realpath(str(archive_path))


def _build_plan(
    path: str,
    eml_path: str,
    manifest_path: str,
    public: bool,
    representation: str = "archive",
    prior_manifest: Optional[Dict[str, object]] = None,
    resource_map_path: Optional[str] = None,
) -> Dict[str, object]:
    """Mirror ``.ms_knb_build_plan``: the whole pure, offline planner."""
    if representation not in ("archive", "expanded"):
        raise ValueError(
            "representation must be one of \"archive\" or \"expanded\"."
        )
    from . import knb_archive
    from .eml import write_eml_from_sdp

    if resource_map_path is None:
        resource_map_path = os.path.join(
            os.path.dirname(manifest_path), "resource-map.rdf"
        )

    _reject_review_candidate_annotations(path)
    mapping = _eml._read_mapping_yaml(
        os.path.join(path, "metadata", "eml-mapping.yml")
    )
    # Preflight: reuse the mapping already parsed here rather than re-reading
    # it, and assert the reviewed-ledger binding before either representation
    # branch touches the package.
    _require_review_ledger_binding(path, mapping)
    archive: Optional[Dict[str, object]] = None
    if representation == "archive":
        archive = knb_archive._write_sdp_archive(path)
        package_objects = [
            _sdp_archive_object(archive, path, str(mapping["dataset_id"]))
        ]
    else:
        package_objects = _sdp_artifact_objects(
            path, str(mapping["dataset_id"])
        )
    supplementary_objects = _supplementary_object_plan(package_objects)
    # R calls the writer with its default ``overwrite = FALSE``: an identical
    # document re-writes idempotently, a different one must be reviewed.
    eml = write_eml_from_sdp(
        path,
        output_path=eml_path,
        supplementary_objects=supplementary_objects,
        require_revision_key=prior_manifest is not None,
    )
    if eml["public"] is not public:
        raise ValueError(
            "Reviewed sidecar publication.public must exactly equal public."
        )

    eml_document = ET.parse(eml["path"]).getroot()
    provider_orcids = []
    for provider in _find_all_local(eml_document, "metadataProvider"):
        for node in provider.iter():
            if (
                _local_name(node.tag) == "userId"
                and node.get("directory") == "https://orcid.org"
            ):
                value = (node.text or "").strip()
                if value and value not in provider_orcids:
                    provider_orcids.append(value)
    if len(provider_orcids) != 1 or _orcid_key(provider_orcids[0]) is None:
        raise ValueError(
            "Live-publication EML must identify exactly one metadata-provider "
            "ORCID URI for authenticated-subject verification."
        )
    expected_subject = provider_orcids[0]

    data_frame = eml["data_objects"]
    records = data_frame.to_dict("records")
    order = sorted(
        range(len(records)), key=lambda index: str(records[index]["file_name"])
    )
    data_objects = []
    for index in order:
        record = records[index]
        data_objects.append(
            {
                "role": "data",
                "path": _relative_path(path, str(record["path"])),
                "local_path": str(record["path"]),
                "pid": str(record["pid"]),
                "format_id": str(record["format_id"]),
                "media_type": "text/csv",
                "size": _numeric(record["size"]),
                "sha256": str(record["checksum"]),
                "series_id": None,
                "obsoletes": None,
                "obsoleted_by": None,
            }
        )

    revision_context = _revision_context(
        prior_manifest, {"public": public, "series_id": eml["series_id"]}
    )

    eml_bytes = _object_bytes(eml["path"])
    metadata_object = {
        "role": "metadata",
        "path": _relative_path(path, eml["path"]),
        "local_path": eml["path"],
        "pid": eml["package_id"],
        "format_id": eml["format_id"],
        "media_type": "application/xml",
        "size": len(eml_bytes),
        "sha256": _sha256_raw(eml_bytes),
        "series_id": eml["series_id"],
        "obsoletes": (
            None if revision_context is None else revision_context["metadata_pid"]
        ),
        "obsoleted_by": None,
    }
    members = data_objects + package_objects + [metadata_object]

    resource_map_pid = _resource_map_pid(
        eml["package_id"], mapping["publication_date"], members
    )
    _require_new_revision_pids(
        revision_context, eml["package_id"], resource_map_pid
    )
    ore = _build_ore(
        resource_map_pid,
        eml["package_id"],
        mapping["publication_date"],
        members,
    )
    _validate_ore(ore, resource_map_pid, members)

    resource_map_path = _inside_path(path, resource_map_path, must_work=False)
    ore_bytes = _xml_bytes(ore)
    resource_map_object = {
        "role": "resource_map",
        "path": _relative_path(
            path,
            os.path.join(
                os.path.realpath(os.path.dirname(resource_map_path)),
                os.path.basename(resource_map_path),
            ),
            must_work=False,
        ),
        "local_path": resource_map_path,
        "pid": resource_map_pid,
        "format_id": ORE_FORMAT_ID,
        "media_type": ORE_MEDIA_TYPE,
        "size": len(ore_bytes),
        "sha256": _sha256_raw(ore_bytes),
        "series_id": None,
        "obsoletes": (
            None
            if revision_context is None
            else revision_context["resource_map_pid"]
        ),
        "obsoleted_by": None,
    }

    plan = {
        "package_path": path,
        "environment": ENVIRONMENT,
        "node_id": NODE_ID,
        "public": public,
        "replication_policy": _replication_policy(public),
        "expected_subject": expected_subject,
        "rights_authorization": mapping.get("rights_authorization")
        or {"status": "unconfirmed", "evidence": []},
        "package_id": eml["package_id"],
        "series_id": eml["series_id"],
        "representation": representation,
        "revision_of": revision_context,
        "prior_manifest": prior_manifest,
        "metadata_pid": eml["package_id"],
        "resource_map_pid": resource_map_pid,
        "objects": data_objects
        + package_objects
        + [metadata_object, resource_map_object],
        "resource_map_document": ore,
        "resource_map_bytes": ore_bytes,
        "resource_map_path": resource_map_path,
        "sdp_archive_path": None if archive is None else archive["path"],
        "eml": eml,
    }
    plan["plan_sha256"] = _plan_fingerprint(plan)
    return plan


# --- replication policy ----------------------------------------------------------------


def _replication_policy(public: bool) -> Dict[str, object]:
    """Mirror ``.ms_knb_replication_policy``."""
    _validate_flag(public, "public")
    # Private review is deliberately KNB-only. Public deposits retain
    # DataONE's three-replica preservation policy, made explicit so it is
    # part of the exact reviewed plan instead of an unreviewed client default.
    return {
        "replication_allowed": bool(public),
        "number_replicas": 3 if public else 0,
        "preferred_member_nodes": [],
        "blocked_member_nodes": [],
    }


def _require_replication_policy(policy: object, public: bool) -> Dict[str, object]:
    """Mirror ``.ms_knb_require_replication_policy``."""
    expected = _replication_policy(public)
    if policy != expected:
        raise ValueError(
            "The publication plan has an invalid replication policy for the "
            "selected public value."
        )
    return expected


# --- redaction and safe aborts ---------------------------------------------------------


def _redact(value: object) -> str:
    """Mirror ``.ms_knb_redact``: redact where external text is captured."""
    text = str(value)
    text = re.sub(
        r"Bearer[ \t\r\n\f\v]+[A-Za-z0-9._~-]+",
        "Bearer [REDACTED]",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
        "[REDACTED JWT]",
        text,
    )
    text = re.sub(
        r"(dataone_token|authorization|cookie)[=:][^ \t\r\n\f\v]+",
        lambda match: match.group(1) + "=[REDACTED]",
        text,
        flags=re.IGNORECASE,
    )
    return text


def _abort_safe(error: BaseException) -> None:
    """Mirror ``.ms_knb_abort_safe``: remote text is data, never a template."""
    raise RuntimeError("KNB publication failed: " + _redact(str(error))) from None


# --- SystemMetadata --------------------------------------------------------------------


def _normalize_access(access: object) -> List[Tuple[str, str]]:
    """Mirror ``.ms_knb_normalize_access``."""
    if access is None or (isinstance(access, (list, tuple)) and not access):
        return []
    if not isinstance(access, (list, tuple)):
        raise ValueError("Remote access policy has an unsupported representation.")
    rows = []
    for rule in access:
        if not isinstance(rule, dict):
            raise ValueError("Remote access policy has an invalid rule.")
        if "subject" not in rule or "permission" not in rule:
            raise ValueError(
                "Remote access policy lacks subject/permission fields."
            )
        rows.append((str(rule["subject"]), str(rule["permission"]).lower()))
    unique = []
    for row in rows:
        if row not in unique:
            unique.append(row)
    return sorted(unique)


def _normalize_member_nodes(nodes: object) -> List[str]:
    """Mirror ``.ms_knb_normalize_member_nodes``."""
    if nodes is None:
        return []
    if not isinstance(nodes, (list, tuple)):
        nodes = [nodes]
    if not nodes:
        return []
    values = []
    for node in nodes:
        if node is None:
            raise ValueError(
                "Remote replication policy has an invalid member-node "
                "reference."
            )
        text = str(node).strip()
        if not text:
            raise ValueError(
                "Remote replication policy has an invalid member-node "
                "reference."
            )
        values.append(text)
    return sorted(set(values))


def _orcid_key(subject: object) -> Optional[str]:
    """Mirror ``.ms_knb_orcid_key``."""
    match = _ORCID_SUBJECT_RE.match(str(subject).strip())
    if match is None:
        return None
    return match.group(1).upper()


def _same_subject(left: object, right: object) -> bool:
    """Mirror ``.ms_knb_same_subject``."""
    if left is None or right is None:
        return False
    left_text = str(left).strip()
    right_text = str(right).strip()
    if not left_text or not right_text:
        return False
    if left_text == right_text:
        return True
    left_orcid = _orcid_key(left_text)
    right_orcid = _orcid_key(right_text)
    return (
        left_orcid is not None
        and right_orcid is not None
        and left_orcid == right_orcid
    )


def _validate_system_metadata(
    remote: object,
    obj: Dict[str, object],
    subject: str,
    public: bool,
    replication_policy: Dict[str, object],
) -> bool:
    """Mirror ``.ms_knb_validate_system_metadata``."""
    if not isinstance(remote, dict):
        raise ValueError(
            f"Remote SystemMetadata for {obj.get('pid')} is missing or "
            "malformed."
        )
    _require_replication_policy(replication_policy, public)
    expected = {
        "identifier": obj.get("pid"),
        "format_id": obj.get("format_id"),
        "size": _numeric(obj.get("size")),
        "checksum": str(obj.get("sha256")).lower(),
        "rights_holder": subject,
        "series_id": _optional_scalar(obj.get("series_id")),
        "media_type": obj.get("media_type"),
        "file_name": os.path.basename(str(obj.get("path"))),
        "archived": False,
        "replication_allowed": replication_policy["replication_allowed"],
        "number_replicas": _numeric(replication_policy["number_replicas"]),
        "preferred_member_nodes": _normalize_member_nodes(
            replication_policy["preferred_member_nodes"]
        ),
        "blocked_member_nodes": _normalize_member_nodes(
            replication_policy["blocked_member_nodes"]
        ),
        "obsoletes": _optional_scalar(obj.get("obsoletes")),
        "obsoleted_by": _optional_scalar(obj.get("obsoleted_by")),
        "origin_member_node": NODE_ID,
        "authoritative_member_node": NODE_ID,
    }

    remote_checksum = _optional_scalar(remote.get("checksum"))
    remote_size = remote.get("size")
    try:
        remote_size = None if remote_size is None else _numeric(remote_size)
    except (TypeError, ValueError):
        remote_size = None
    remote_replicas = remote.get("number_replicas")
    try:
        remote_replicas = (
            None if remote_replicas is None else float(remote_replicas)
        )
    except (TypeError, ValueError):
        remote_replicas = None
    if remote_replicas is not None and (
        remote_replicas < 0 or remote_replicas != int(remote_replicas)
    ):
        remote_replicas = None
    actual = {
        "identifier": _optional_scalar(remote.get("identifier")),
        "format_id": _optional_scalar(remote.get("format_id")),
        "size": remote_size,
        "checksum": None if remote_checksum is None else remote_checksum.lower(),
        "rights_holder": _optional_scalar(remote.get("rights_holder")),
        "series_id": _optional_scalar(remote.get("series_id")),
        "media_type": _optional_scalar(remote.get("media_type")),
        "file_name": _optional_scalar(remote.get("file_name")),
        "archived": (
            bool(remote.get("archived"))
            if isinstance(remote.get("archived"), bool)
            else None
        ),
        "replication_allowed": (
            remote.get("replication_allowed")
            if isinstance(remote.get("replication_allowed"), bool)
            else None
        ),
        "number_replicas": (
            None if remote_replicas is None else _numeric(remote_replicas)
        ),
        "preferred_member_nodes": _normalize_member_nodes(
            remote.get("preferred_member_nodes")
        ),
        "blocked_member_nodes": _normalize_member_nodes(
            remote.get("blocked_member_nodes")
        ),
        "obsoletes": _optional_scalar(remote.get("obsoletes")),
        "obsoleted_by": _optional_scalar(remote.get("obsoleted_by")),
        "origin_member_node": _optional_scalar(remote.get("origin_member_node")),
        "authoritative_member_node": _optional_scalar(
            remote.get("authoritative_member_node")
        ),
    }

    mismatches = [
        field for field in expected if actual[field] != expected[field]
    ]
    remote_algorithm = _optional_scalar(remote.get("checksum_algorithm"))
    if (
        remote_algorithm is None
        or remote_algorithm.upper().replace("-", "") != "SHA256"
    ):
        mismatches.append("checksum_algorithm")
    if not _same_subject(remote.get("submitter"), subject):
        mismatches.append("submitter")
    serial_version = _optional_scalar(remote.get("serial_version"))
    try:
        serial_number = None if serial_version is None else float(serial_version)
    except ValueError:
        serial_number = None
    if (
        serial_number is None
        or serial_number < 0
        or serial_number != int(serial_number)
    ):
        mismatches.append("serial_version")
    if not _valid_timestamp(remote.get("date_uploaded")):
        mismatches.append("date_uploaded")
    if not _valid_timestamp(remote.get("date_sys_metadata_modified")):
        mismatches.append("date_sys_metadata_modified")
    if mismatches:
        raise ValueError(
            f"Remote PID {obj.get('pid')} collides on SystemMetadata "
            "field(s): " + ", ".join(sorted(set(mismatches))) + "."
        )

    access = _normalize_access(remote.get("access"))
    expected_access = [("public", "read")] if public else []
    if access != expected_access:
        raise ValueError(
            f"Remote PID {obj.get('pid')} has a different access policy."
        )
    return True


def _system_metadata_document(
    obj: Dict[str, object],
    subject: str,
    public: bool,
    node_id: str,
    replication_policy: Dict[str, object],
) -> ET.Element:
    """Mirror ``.ms_knb_new_system_metadata`` plus datapack's v2 serializer.

    ``datapack::SystemMetadata`` supplies local defaults for fields the
    DataONE service owns; those are never serialized. R lets
    ``dataone::createObject()`` attach ``authoritativeMemberNode`` from the
    resolved Member Node immediately before upload, so this builder fills the
    same field from its ``node_id`` argument (which R accepts and leaves
    unused) and the uploaded documents match.
    """
    _require_replication_policy(replication_policy, public)
    root = ET.Element("d1_v2.0:systemMetadata")
    root.set("xmlns:d1_v2.0", _D1_V2_NAMESPACE)
    root.set("xmlns:d1", _D1_V1_NAMESPACE)

    def text_node(name: str, value: object) -> ET.Element:
        node = ET.SubElement(root, name)
        node.text = str(value)
        return node

    text_node("identifier", obj["pid"])
    text_node("formatId", obj["format_id"])
    text_node("size", _format_size(obj["size"]))
    checksum = text_node("checksum", str(obj["sha256"]))
    checksum.set("algorithm", "SHA-256")
    text_node("submitter", subject)
    text_node("rightsHolder", subject)
    if public:
        access_policy = ET.SubElement(root, "accessPolicy")
        allow = ET.SubElement(access_policy, "allow")
        subject_node = ET.SubElement(allow, "subject")
        subject_node.text = "public"
        permission = ET.SubElement(allow, "permission")
        permission.text = "read"
    replication = ET.SubElement(root, "replicationPolicy")
    replication.set(
        "replicationAllowed",
        "true" if replication_policy["replication_allowed"] else "false",
    )
    replication.set(
        "numberReplicas", _format_size(replication_policy["number_replicas"])
    )
    for node in _normalize_member_nodes(
        replication_policy["preferred_member_nodes"]
    ):
        preferred = ET.SubElement(replication, "preferredMemberNode")
        preferred.text = node
    for node in _normalize_member_nodes(
        replication_policy["blocked_member_nodes"]
    ):
        blocked = ET.SubElement(replication, "blockedMemberNode")
        blocked.text = node

    obsoletes = _optional_scalar(obj.get("obsoletes"))
    if obsoletes is not None:
        text_node("obsoletes", obsoletes)
    obsoleted_by = _optional_scalar(obj.get("obsoleted_by"))
    if obsoleted_by is not None:
        text_node("obsoletedBy", obsoleted_by)
    text_node("authoritativeMemberNode", node_id)
    series_id = _optional_scalar(obj.get("series_id"))
    if series_id is not None:
        text_node("seriesId", series_id)
    media_type = _optional_scalar(obj.get("media_type"))
    if media_type is not None:
        media = ET.SubElement(root, "mediaType")
        media.set("name", media_type)
    text_node("fileName", os.path.basename(str(obj["path"])))
    return root


def _system_metadata_bytes(
    obj: Dict[str, object],
    subject: str,
    public: bool,
    node_id: str,
    replication_policy: Dict[str, object],
) -> bytes:
    return _xml_bytes(
        _system_metadata_document(obj, subject, public, node_id, replication_policy)
    )


def _parse_system_metadata(payload: Union[bytes, str]) -> Dict[str, object]:
    """Mirror ``.ms_knb_system_metadata_list`` over a DataONE response.

    R parses the response with ``datapack::SystemMetadata()`` and flattens the
    S4 object; this reads the same elements straight from the document.
    """
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    root = ET.fromstring(payload)

    def child_text(name: str) -> Optional[str]:
        for node in root:
            if _local_name(node.tag) == name:
                return None if node.text is None else node.text.strip()
        return None

    def child(name: str) -> Optional[ET.Element]:
        for node in root:
            if _local_name(node.tag) == name:
                return node
        return None

    checksum_node = child("checksum")
    replication_node = child("replicationPolicy")
    media_node = child("mediaType")
    access_node = child("accessPolicy")

    access: List[Dict[str, str]] = []
    if access_node is not None:
        for allow in access_node:
            if _local_name(allow.tag) != "allow":
                continue
            subjects = [
                (node.text or "").strip()
                for node in allow
                if _local_name(node.tag) == "subject"
            ]
            permissions = [
                (node.text or "").strip()
                for node in allow
                if _local_name(node.tag) == "permission"
            ]
            for allow_subject in subjects:
                for permission in permissions:
                    access.append(
                        {"subject": allow_subject, "permission": permission}
                    )

    replication_allowed = None
    number_replicas = None
    preferred: List[str] = []
    blocked: List[str] = []
    if replication_node is not None:
        raw_allowed = replication_node.get("replicationAllowed")
        if raw_allowed is not None:
            replication_allowed = raw_allowed.strip().lower() == "true"
        raw_replicas = replication_node.get("numberReplicas")
        if raw_replicas is not None:
            try:
                number_replicas = _numeric(raw_replicas)
            except ValueError:
                number_replicas = None
        for node in replication_node:
            value = (node.text or "").strip()
            if _local_name(node.tag) == "preferredMemberNode" and value:
                preferred.append(value)
            if _local_name(node.tag) == "blockedMemberNode" and value:
                blocked.append(value)

    serial_version = child_text("serialVersion")
    size = child_text("size")
    archived = child_text("archived")
    return {
        "serial_version": None if serial_version is None else _numeric(serial_version),
        "identifier": child_text("identifier"),
        "format_id": child_text("formatId"),
        "size": None if size is None else _numeric(size),
        "checksum": (
            None
            if checksum_node is None or checksum_node.text is None
            else checksum_node.text.strip()
        ),
        "checksum_algorithm": (
            None if checksum_node is None else checksum_node.get("algorithm")
        ),
        "submitter": child_text("submitter"),
        "rights_holder": child_text("rightsHolder"),
        "access": access,
        "replication_allowed": replication_allowed,
        "number_replicas": number_replicas,
        "preferred_member_nodes": preferred,
        "blocked_member_nodes": blocked,
        "series_id": child_text("seriesId"),
        "media_type": None if media_node is None else media_node.get("name"),
        "file_name": child_text("fileName"),
        "archived": None if archived is None else archived.strip().lower() == "true",
        "obsoletes": child_text("obsoletes"),
        "obsoleted_by": child_text("obsoletedBy"),
        "date_uploaded": child_text("dateUploaded"),
        "date_sys_metadata_modified": child_text("dateSysMetadataModified"),
        "origin_member_node": child_text("originMemberNode"),
        "authoritative_member_node": child_text("authoritativeMemberNode"),
    }


# --- remote verification ----------------------------------------------------------------


def _anonymous_denial_status(condition: BaseException) -> Optional[int]:
    """Mirror ``.ms_knb_anonymous_denial_status``."""
    status = getattr(condition, "status", None)
    if isinstance(status, int):
        return status
    response = getattr(condition, "response", None)
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int):
        return status_code
    return None


def _verify_anonymous_denial(
    adapter: object, endpoint: str, obj: Dict[str, object]
) -> bool:
    """Mirror ``.ms_knb_verify_anonymous_denial``."""
    probes = (
        ("byte", lambda: adapter.get_anonymous_bytes(endpoint, obj["pid"])),
        (
            "SystemMetadata",
            lambda: adapter.get_anonymous_system_metadata(endpoint, obj["pid"]),
        ),
    )
    for kind, probe in probes:
        try:
            probe()
        except Exception as condition:  # noqa: BLE001 - denial is the contract
            status = _anonymous_denial_status(condition)
            if status not in (401, 403, 404):
                raise ValueError(
                    f"Anonymous {kind} non-disclosure could not be verified "
                    f"for private-review PID {obj['pid']}."
                ) from None
            continue
        raise ValueError(
            f"Anonymous {kind} access unexpectedly succeeded for "
            f"private-review PID {obj['pid']}."
        )
    return True


def _verify_object(
    adapter: object,
    client: object,
    endpoint: str,
    obj: Dict[str, object],
    payload: bytes,
    subject: str,
    public: bool,
    replication_policy: Dict[str, object],
) -> bool:
    """Mirror ``.ms_knb_verify_object``."""
    remote_bytes = adapter.get_bytes(client, obj["pid"])
    if not isinstance(remote_bytes, (bytes, bytearray)) or bytes(
        remote_bytes
    ) != payload:
        raise ValueError(
            f"Remote byte read-back failed for PID {obj['pid']}."
        )
    remote_metadata = adapter.get_system_metadata(client, obj["pid"])
    _validate_system_metadata(
        remote_metadata, obj, subject, public, replication_policy
    )
    remote_checksum = adapter.get_checksum(client, obj["pid"], "SHA-256")
    checksum = _optional_scalar(remote_checksum)
    if checksum is None or checksum.lower() != str(obj["sha256"]).lower():
        raise ValueError(
            f"Independent remote checksum failed for PID {obj['pid']}."
        )

    if public:
        anonymous_bytes = adapter.get_anonymous_bytes(endpoint, obj["pid"])
        if not isinstance(anonymous_bytes, (bytes, bytearray)) or bytes(
            anonymous_bytes
        ) != payload:
            raise ValueError(
                f"Anonymous byte read-back failed for public PID {obj['pid']}."
            )
        anonymous_metadata = adapter.get_anonymous_system_metadata(
            endpoint, obj["pid"]
        )
        _validate_system_metadata(
            anonymous_metadata, obj, subject, public, replication_policy
        )
    else:
        _verify_anonymous_denial(adapter, endpoint, obj)
    return True


def _manifest_set_state(
    manifest: Dict[str, object], pid: str, state: str
) -> Dict[str, object]:
    """Mirror ``.ms_knb_manifest_set_state``."""
    for obj in manifest["objects"]:
        if obj.get("pid") == pid:
            obj["state"] = state
            return manifest
    raise ValueError(
        f"Internal manifest error: PID {pid} is not in the plan."
    )


def _persist_manifest(
    manifest: Dict[str, object], path: str
) -> Dict[str, object]:
    """Mirror ``.ms_knb_persist_manifest``."""
    _atomic_write_raw(_json_bytes(manifest), path)
    return manifest


def _local_object_spec(obj: Dict[str, object]) -> Dict[str, object]:
    """Mirror ``.ms_knb_local_object_spec``: freeze and re-hash local bytes."""
    payload = _object_bytes(obj["local_path"])
    if len(payload) != obj["size"] or _sha256_raw(payload) != obj["sha256"]:
        raise ValueError(
            f"Local publication object {obj['path']} changed after planning."
        )
    spec = dict(obj)
    spec["bytes"] = payload
    spec["series_id"] = _optional_scalar(obj.get("series_id"))
    return spec


# --- catalog evidence --------------------------------------------------------------------


def _catalog_records(records: object) -> List[Dict[str, object]]:
    """Mirror ``.ms_knb_catalog_records``."""
    if not records:
        return []
    if not isinstance(records, (list, tuple)):
        return []
    return [record for record in records if isinstance(record, dict)]


def _catalog_values(record: Dict[str, object], field: str) -> List[str]:
    """Mirror ``.ms_knb_catalog_values``."""
    values = record.get(field)
    if values is None:
        return []
    if not isinstance(values, (list, tuple)):
        values = [values]
    cleaned = [str(value).strip() for value in values if value is not None]
    return sorted({value for value in cleaned if value})


def _catalog_evidence(
    plan: Dict[str, object], records: object
) -> Dict[str, object]:
    """Mirror ``.ms_knb_catalog_evidence``."""
    records = _catalog_records(records)
    ids = [_optional_scalar(record.get("id")) for record in records]
    indexed_ids = [value for value in ids if value is not None]
    expected_pids = [str(obj["pid"]) for obj in plan["objects"]]
    member_pids = [
        str(obj["pid"])
        for obj in plan["objects"]
        if str(obj["role"]) != "resource_map"
    ]

    def record_for(pid: str) -> Dict[str, object]:
        matches = [
            record
            for record, value in zip(records, ids)
            if value is not None and value == pid
        ]
        return matches[0] if len(matches) == 1 else {}

    resource_map_members = [
        pid
        for pid in member_pids
        if plan["resource_map_pid"]
        in _catalog_values(record_for(pid), "resourceMap")
    ]
    metadata_documents = _catalog_values(
        record_for(str(plan["metadata_pid"])), "documents"
    )
    # Expanded artifacts are now EML-documented objects like data resources,
    # so the old "supplemental objects carry no cito relations" check would
    # contradict the document it is verifying. It is retired here rather than
    # inverted; the field stays in the evidence for manifest compatibility.
    documented_pids = [
        str(obj["pid"])
        for obj in plan["objects"]
        if str(obj["role"]) in ("data", "sdp_archive", "sdp_artifact")
    ]
    documented_objects = [
        pid
        for pid in documented_pids
        if plan["metadata_pid"]
        in _catalog_values(record_for(pid), "isDocumentedBy")
    ]
    verified = (
        set(indexed_ids) == set(expected_pids)
        and len(indexed_ids) == len(expected_pids)
        and len(set(indexed_ids)) == len(expected_pids)
        and set(resource_map_members) == set(member_pids)
        and len(resource_map_members) == len(member_pids)
        and sorted(metadata_documents) == sorted(documented_pids)
        and set(documented_objects) == set(documented_pids)
        and len(documented_objects) == len(documented_pids)
    )
    return {
        "verified": bool(verified),
        "indexed_pids": sorted(set(indexed_ids)),
        "resource_map_pid": plan["resource_map_pid"],
        "resource_map_members": sorted(resource_map_members),
        "metadata_pid": plan["metadata_pid"],
        "metadata_documents": sorted(metadata_documents),
        "documented_data_pids": sorted(documented_objects),
        "supplemental_relations_clean": True,
    }


def _anonymous_catalog_evidence(
    plan: Dict[str, object], records: object
) -> Dict[str, object]:
    """Mirror ``.ms_knb_anonymous_catalog_evidence``."""
    if plan.get("public") is True:
        return _catalog_evidence(plan, records)
    records = _catalog_records(records)
    indexed_ids = sorted(
        {
            value
            for value in (
                _optional_scalar(record.get("id")) for record in records
            )
            if value is not None
        }
    )
    planned_pids = [str(obj["pid"]) for obj in plan["objects"]]
    matching_pids = sorted(set(indexed_ids) & set(planned_pids))
    return {
        "verified": len(matching_pids) == 0,
        "matching_pids": matching_pids,
    }


# --- revision helpers ---------------------------------------------------------------------


def _prior_object_spec(
    plan: Dict[str, object], pid: str, obsoleted_by: Optional[str] = None
) -> Dict[str, object]:
    """Mirror ``.ms_knb_prior_object_spec``."""
    prior = plan.get("prior_manifest")
    if prior is None:
        raise ValueError(
            "Internal KNB revision error: no prior manifest is bound."
        )
    objects = _manifest_objects(prior)
    matches = [obj for obj in objects if _optional_scalar(obj.get("pid")) == pid]
    if len(matches) != 1:
        raise ValueError(
            f"The prior KNB manifest does not identify revision source PID "
            f"{pid} exactly once."
        )
    obj = dict(matches[0])
    obj["series_id"] = (
        _optional_scalar(prior.get("series_id"))
        if str(obj.get("role")) == "metadata"
        else None
    )
    obj["obsoletes"] = _optional_scalar(obj.get("obsoletes"))
    obj["obsoleted_by"] = _optional_scalar(obsoleted_by)
    return obj


def _validate_revision_source(
    remote: object,
    plan: Dict[str, object],
    old_pid: str,
    new_pid: str,
    subject: str,
) -> Optional[str]:
    """Mirror ``.ms_knb_validate_revision_source``."""
    if remote is None:
        raise ValueError(
            f"KNB revision source PID {old_pid} does not exist at KNB."
        )
    linked_to = _optional_scalar(remote.get("obsoleted_by"))
    if linked_to is not None and linked_to != new_pid:
        raise ValueError(
            f"KNB revision source PID {old_pid} is already obsoleted by a "
            "different PID."
        )
    prior_object = _prior_object_spec(plan, old_pid, obsoleted_by=linked_to)
    _validate_system_metadata(
        remote,
        prior_object,
        subject,
        plan["public"],
        plan["replication_policy"],
    )
    return linked_to


def _validate_series_binding(
    remote: object,
    metadata_object: Dict[str, object],
    subject: str,
    public: bool,
    replication_policy: Dict[str, object],
    plan: Optional[Dict[str, object]] = None,
) -> bool:
    """Mirror ``.ms_knb_validate_series_binding``."""
    if remote is None:
        return True
    remote_pid = _optional_scalar(remote.get("identifier"))
    revision_source = _optional_scalar(metadata_object.get("obsoletes"))
    allowed = (
        [metadata_object["pid"]]
        if revision_source is None
        else [revision_source, metadata_object["pid"]]
    )
    if remote_pid not in allowed:
        raise ValueError(
            "The metadata series identifier is already bound to a different "
            "metadata PID."
        )
    if revision_source is not None and remote_pid == revision_source:
        if plan is None:
            raise ValueError(
                "Internal KNB revision error: no plan is available."
            )
        _validate_revision_source(
            remote, plan, revision_source, str(metadata_object["pid"]), subject
        )
        return True
    _validate_system_metadata(
        remote, metadata_object, subject, public, replication_policy
    )
    return True


# --- the live state machine ----------------------------------------------------------------


def _run_publication(
    plan: Dict[str, object],
    manifest: Dict[str, object],
    manifest_path: str,
    adapter: object,
    previous_status: Optional[str] = None,
) -> Dict[str, object]:
    """Mirror ``.ms_knb_run_publication``.

    Every adapter warning is promoted to an error and every failure is
    redacted before it surfaces, exactly as R's ``withCallingHandlers`` /
    ``tryCatch`` pair does. A partial upload can never look successful: the
    manifest is persisted after each verified object and the status only
    reaches ``complete`` behind a fresh catalog check.
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            return _run_publication_body(
                plan, manifest, manifest_path, adapter, previous_status
            )
    except Warning as warning:
        raise RuntimeError(
            "Live KNB adapter warning: " + _redact(str(warning))
        ) from None
    except Exception as error:  # noqa: BLE001 - redaction is the contract
        _abort_safe(error)


def _run_publication_body(
    plan: Dict[str, object],
    manifest: Dict[str, object],
    manifest_path: str,
    adapter: object,
    previous_status: Optional[str],
) -> Dict[str, object]:
    # Freeze and re-hash every local byte stream before constructing a client
    # or observing any remote state. Nothing below this boundary re-reads a
    # local publication object.
    object_specs = []
    for obj in plan["objects"]:
        specification = _local_object_spec(obj)
        specification["replication_policy"] = plan["replication_policy"]
        object_specs.append(specification)
    _require_replication_policy(plan["replication_policy"], plan["public"])

    _validate_adapter(adapter)
    client = adapter.connect(plan["environment"], plan["node_id"])
    preflight = adapter.preflight(client)
    if not isinstance(preflight, dict):
        raise ValueError("KNB adapter preflight returned no result.")
    subject = _nonempty_scalar(preflight.get("subject"), "subject")
    endpoint = _nonempty_scalar(preflight.get("endpoint"), "endpoint")
    preflight_node_id = _nonempty_scalar(preflight.get("node_id"), "node_id")
    if preflight_node_id != plan["node_id"]:
        raise ValueError(
            "KNB preflight returned a different DataONE node identifier."
        )
    if not _same_subject(subject, plan["expected_subject"]):
        raise ValueError(
            "The server-verified DataONE subject does not match the EML "
            "metadata-provider ORCID."
        )
    available_formats = {
        str(value).strip() for value in adapter.list_formats(client)
    }
    planned_formats = {str(obj["format_id"]) for obj in object_specs}
    missing_formats = sorted(planned_formats - available_formats)
    if missing_formats:
        raise ValueError(
            "The live DataONE format registry lacks planned format ID(s): "
            + ", ".join(missing_formats)
            + "."
        )

    # Scan every immutable PID, including independent checksum evidence for
    # existing objects, before the first create. A collision on the last
    # planned object therefore cannot orphan earlier creates.
    remote_objects: List[Optional[Dict[str, object]]] = []
    for obj in object_specs:
        remote = adapter.lookup_system_metadata(client, obj["pid"])
        if remote is not None:
            _validate_system_metadata(
                remote, obj, subject, plan["public"], plan["replication_policy"]
            )
            checksum = _optional_scalar(
                adapter.get_checksum(client, obj["pid"], "SHA-256")
            )
            if checksum is None or checksum.lower() != str(obj["sha256"]).lower():
                raise ValueError(
                    f"Remote PID {obj['pid']} collides on independent checksum."
                )
        update_of = _optional_scalar(obj.get("obsoletes"))
        if update_of is not None:
            source_remote = adapter.lookup_system_metadata(client, update_of)
            linked_to = _validate_revision_source(
                source_remote, plan, update_of, str(obj["pid"]), subject
            )
            source_checksum = _optional_scalar(
                adapter.get_checksum(client, update_of, "SHA-256")
            )
            prior_object = _prior_object_spec(
                plan, update_of, obsoleted_by=linked_to
            )
            if (
                source_checksum is None
                or source_checksum.lower()
                != str(prior_object["sha256"]).lower()
            ):
                raise ValueError(
                    f"KNB revision source PID {update_of} collides on "
                    "independent checksum."
                )
            if remote is None and linked_to is not None:
                raise ValueError(
                    f"KNB revision source PID {update_of} names the planned "
                    "successor, but that successor cannot be read."
                )
            if remote is not None and linked_to is None:
                raise ValueError(
                    "The planned revision PID exists, but its predecessor "
                    "does not link to it."
                )
        remote_objects.append(remote)

    metadata_index = [
        index
        for index, obj in enumerate(object_specs)
        if str(obj["role"]) == "metadata"
    ][0]
    metadata_object = object_specs[metadata_index]
    series_remote = adapter.lookup_series_id(client, plan["series_id"])
    _validate_series_binding(
        series_remote,
        metadata_object,
        subject,
        plan["public"],
        plan["replication_policy"],
        plan=plan,
    )
    if remote_objects[metadata_index] is not None and series_remote is None:
        raise ValueError(
            "The existing metadata PID has an unresolved metadata series "
            "identifier."
        )

    for index, obj in enumerate(object_specs):
        remote = remote_objects[index]
        if remote is None:
            create_error: Optional[BaseException] = None
            try:
                update_of = _optional_scalar(obj.get("obsoletes"))
                if update_of is None:
                    adapter.create_object(client, obj, subject, plan["public"])
                else:
                    adapter.update_object(
                        client, update_of, obj, subject, plan["public"]
                    )
            except Exception as error:  # noqa: BLE001 - resolved below
                create_error = error
            if create_error is not None:
                # A timed-out create may have committed. Only an authoritative
                # follow-up lookup can resolve that ambiguity.
                remote = adapter.lookup_system_metadata(client, obj["pid"])
                if remote is None:
                    raise create_error
                _validate_system_metadata(
                    remote,
                    obj,
                    subject,
                    plan["public"],
                    plan["replication_policy"],
                )

        _verify_object(
            adapter,
            client,
            endpoint,
            obj,
            obj["bytes"],
            subject,
            plan["public"],
            plan["replication_policy"],
        )
        update_of = _optional_scalar(obj.get("obsoletes"))
        if update_of is not None:
            source_remote = adapter.lookup_system_metadata(client, update_of)
            linked_to = _validate_revision_source(
                source_remote, plan, update_of, str(obj["pid"]), subject
            )
            if linked_to != obj["pid"]:
                raise ValueError(
                    f"KNB did not link revision source PID {update_of} to its "
                    "planned successor."
                )
        manifest = _manifest_set_state(manifest, obj["pid"], "verified")
        manifest = _persist_manifest(manifest, manifest_path)

    authenticated_evidence = _catalog_evidence(
        plan, adapter.catalog_lookup(client, plan)
    )
    anonymous_evidence = _anonymous_catalog_evidence(
        plan, adapter.anonymous_catalog_lookup(plan)
    )
    # A local manifest is a recovery aid, not a signed attestation. Always
    # bind completion of this live call to the fresh catalog response; a stale
    # or edited catalog_verified value must never bypass a failed check.
    manifest["catalog_verified"] = bool(
        authenticated_evidence["verified"] and anonymous_evidence["verified"]
    )
    manifest["catalog_evidence"] = {
        "authenticated": authenticated_evidence,
        "anonymous": anonymous_evidence,
    }
    if plan["public"] is not True and not anonymous_evidence["verified"]:
        manifest["status"] = "published_pending_catalog"
        manifest = _persist_manifest(manifest, manifest_path)
        raise ValueError(
            "Anonymous catalog unexpectedly exposed private-review PID(s): "
            + ", ".join(anonymous_evidence["matching_pids"])
            + "."
        )
    if not manifest["catalog_verified"]:
        manifest["status"] = "published_pending_catalog"
        manifest = _persist_manifest(manifest, manifest_path)
        return _publication_result(
            "published_pending_catalog", plan, manifest, manifest_path
        )

    manifest["status"] = "complete"
    manifest = _persist_manifest(manifest, manifest_path)
    status = (
        "already_published" if previous_status == "complete" else "published"
    )
    return _publication_result(status, plan, manifest, manifest_path)


def _publication_result(
    status: str,
    plan: Dict[str, object],
    manifest: Dict[str, object],
    manifest_path: str,
) -> Dict[str, object]:
    return {
        "status": status,
        "package_id": plan["package_id"],
        "series_id": plan["series_id"],
        "resource_map_pid": plan["resource_map_pid"],
        "manifest_path": os.path.realpath(manifest_path),
        "resource_map_path": os.path.realpath(str(plan["resource_map_path"])),
        "sdp_archive_path": _result_archive_path(plan),
        "representation": plan["representation"],
        "manifest": manifest,
    }


# --- authenticated-subject verification ---------------------------------------------------


def _echo_subjects(credentials: object) -> List[str]:
    """Mirror ``.ms_knb_echo_subjects``.

    ``credentials`` mirrors R's ``xmlToList`` shape: a sequence of
    ``(name, value)`` pairs where repeated names are meaningful.
    """
    if not credentials:
        return []
    pairs = _as_pairs(credentials)
    subjects: List[str] = []
    for name, person in pairs:
        if name != "person":
            continue
        person_pairs = _as_pairs(person)
        if not person_pairs:
            continue
        verified = [
            str(value).strip().lower()
            for key, value in person_pairs
            if key == "verified"
        ]
        explicitly_verified = bool(verified) and all(
            value == "true" for value in verified
        )
        if verified and not explicitly_verified:
            continue
        primary = [value for key, value in person_pairs if key == "subject"]
        equivalent = (
            [
                value
                for key, value in person_pairs
                if key == "equivalentIdentity"
            ]
            if explicitly_verified
            else []
        )
        for value in list(primary) + list(equivalent):
            text = str(value).strip()
            if text and text not in subjects:
                subjects.append(text)
    return subjects


def _as_pairs(value: object) -> List[Tuple[str, object]]:
    if isinstance(value, dict):
        return list(value.items())
    if isinstance(value, (list, tuple)):
        pairs = []
        for item in value:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                pairs.append((str(item[0]), item[1]))
        return pairs
    return []


def _server_verified_subject(credentials: object, token_subject: object) -> str:
    """Mirror ``.ms_knb_server_verified_subject``."""
    token_subject = _nonempty_scalar(token_subject, "subject")
    for candidate in _echo_subjects(credentials):
        if _same_subject(candidate, token_subject):
            return candidate
    raise ValueError(
        "The DataONE Coordinating Node did not verify the JWT subject."
    )


def _decode_jwt_claims(token: str) -> Dict[str, object]:
    """Decode a JWT payload **without verifying its signature**.

    R's ``dataone::AuthenticationManager`` also only inspects local claims;
    the real check is server-side through ``echoCredentials``. Nothing here
    grants trust, so no signature library is needed (or wanted).
    """
    parts = str(token).split(".")
    if len(parts) != 3:
        raise ValueError(
            "The process-local DataONE JWT is absent, expired, or invalid for "
            "KNB."
        )
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except Exception:  # noqa: BLE001 - any decode failure is the same answer
        raise ValueError(
            "The process-local DataONE JWT is absent, expired, or invalid for "
            "KNB."
        ) from None
    if not isinstance(claims, dict):
        raise ValueError(
            "The process-local DataONE JWT is absent, expired, or invalid for "
            "KNB."
        )
    return claims


def _token_subject(token: str, now: Optional[float] = None) -> str:
    """Local claim check: the token must be unexpired and name a subject."""
    claims = _decode_jwt_claims(token)
    expiry = claims.get("exp")
    if now is None:
        now = datetime.now().timestamp()
    if expiry is not None:
        try:
            expired = float(expiry) <= float(now)
        except (TypeError, ValueError):
            expired = True
        if expired:
            raise ValueError(
                "The process-local DataONE JWT is absent, expired, or invalid "
                "for KNB."
            )
    subject = _optional_scalar(claims.get("sub"))
    if subject is None:
        raise ValueError(
            "The process-local DataONE JWT is absent, expired, or invalid for "
            "KNB."
        )
    return subject


# --- live capabilities -------------------------------------------------------------------


def _capabilities_document(capabilities: object) -> ET.Element:
    """Mirror ``.ms_knb_capabilities_document``."""
    if isinstance(capabilities, ET.Element):
        return capabilities
    if isinstance(capabilities, bytes):
        capabilities = capabilities.decode("utf-8")
    if isinstance(capabilities, str):
        return ET.fromstring(capabilities)
    raise ValueError("KNB returned an unreadable capabilities document.")


def _validate_live_capabilities(
    document: ET.Element, endpoint: str, node_id: str
) -> bool:
    """Mirror ``.ms_knb_validate_live_capabilities``."""
    if _local_name(document.tag) != "node":
        raise ValueError(
            f"Direct unauthenticated KNB capabilities did not identify "
            f"{node_id}."
        )
    identifiers = [
        (node.text or "").strip()
        for node in document
        if _local_name(node.tag) == "identifier"
    ]
    if len(identifiers) != 1 or identifiers[0] != node_id:
        raise ValueError(
            f"Direct unauthenticated KNB capabilities did not identify "
            f"{node_id}."
        )
    base_urls = [
        (node.text or "").strip()
        for node in document
        if _local_name(node.tag) == "baseURL"
    ]
    expected_endpoint = (
        re.sub(r"/+$", "", base_urls[0]) + "/v2" if len(base_urls) == 1 else None
    )
    if expected_endpoint is None or re.sub(r"/+$", "", endpoint) != re.sub(
        r"/+$", "", expected_endpoint
    ):
        raise ValueError(
            "Direct KNB capabilities returned an unexpected service endpoint."
        )

    storage = [
        node
        for node in document.iter()
        if _local_name(node.tag) == "service"
        and node.get("name") == "MNStorage"
        and node.get("available") == "true"
    ]
    versions = [node.get("version") or "" for node in storage]
    if not storage or not any(
        re.search(r"(^|/)v?2($|/)", version) for version in versions
    ):
        raise ValueError(
            "Direct KNB capabilities do not advertise available MNStorage v2."
        )
    read_only = [
        (node.text or "").strip().lower()
        for node in document.iter()
        if node.get("key") == "read_only_mode"
    ]
    if not read_only or any(value != "false" for value in read_only):
        raise ValueError(
            "Direct KNB capabilities do not explicitly report "
            "read_only_mode=false."
        )
    return True


# --- catalog query -------------------------------------------------------------------------


def _solr_quote(value: object) -> str:
    """Mirror ``.ms_knb_solr_quote``."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _catalog_url(plan: Dict[str, object]) -> str:
    """Mirror ``.ms_knb_catalog_url``."""
    pids = [str(obj["pid"]) for obj in plan["objects"]]
    query = " OR ".join('id:"' + _solr_quote(pid) + '"' for pid in pids)
    parameters = [
        ("q", query),
        ("fl", "id,resourceMap,documents,isDocumentedBy"),
        ("rows", str(len(pids))),
        ("wt", "json"),
    ]
    encoded = "&".join(
        name + "=" + quote(value, safe="") for name, value in parameters
    )
    return CN_ENDPOINT + "/query/solr/?" + encoded


def _catalog_docs(body: object) -> List[Dict[str, object]]:
    """Mirror ``.ms_knb_catalog_docs``."""
    if not isinstance(body, dict):
        return []
    response = body.get("response")
    if not isinstance(response, dict):
        return []
    docs = response.get("docs")
    if not isinstance(docs, list):
        return []
    return docs


# --- the default DataONE v2 REST adapter -----------------------------------------------------


def _require_knb_extra() -> None:
    """Mirror R's up-front dependency gate.

    Publication always rebuilds reviewed EML (PyYAML for the mapping sidecar,
    lxml for XSD validation) and the live adapter speaks REST with requests.
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
    missing.extend(_missing_transport())
    if missing:
        raise ImportError(
            "publish_sdp_to_knb requires the optional KNB dependencies ("
            + ", ".join(missing)
            + ") to parse the reviewed EML sidecar, validate EML against the "
            "bundled XSD set, and speak the DataONE v2 REST API. Install "
            'them with: pip install "metasalmonpy[knb]".'
        )


def _missing_transport() -> List[str]:
    try:
        import requests  # noqa: F401
    except ImportError:  # pragma: no cover - requests is a core dependency
        return ["requests"]
    return []


def _require_transport() -> None:
    """Mirror ``.ms_knb_default_adapter``'s client-package check.

    R's default adapter needs ``dataone``/``datapack``/``XML``; this one needs
    only ``requests`` and the standard library. The reviewed-EML dependencies
    are a *planning* requirement, gated in ``publish_sdp_to_knb``.
    """
    missing = _missing_transport()
    if missing:
        raise ImportError(
            "Live KNB publication requires ("
            + ", ".join(missing)
            + '). Install it with: pip install "metasalmonpy[knb]".'
        )


class KnbClient:
    """The connection state R keeps in its adapter's client environment."""

    def __init__(self, environment: str, node_id: str, endpoint: str) -> None:
        self.environment = environment
        self.node_id = node_id
        self.endpoint = endpoint
        self.cn_endpoint = CN_ENDPOINT
        self.subject: Optional[str] = None
        self.authenticated = False


class DataOneRestAdapter:
    """The v0.1.7 adapter boundary, spoken as DataONE v2 REST.

    Exactly the fourteen methods ``.ms_knb_required_adapter_methods()`` names
    at v0.1.7, with the same arguments and the same return shapes. Nothing in
    the planner knows this class exists; tests inject their own object with
    the same fourteen methods, mirroring R's ``metasalmon.knb_adapter``
    option.
    """

    timeout = 30

    # -- transport ---------------------------------------------------------

    def _session(self):
        import requests

        return requests.Session()

    def _token(self) -> str:
        if not _eml._nonempty(_DATAONE_TOKEN):
            raise ValueError(
                "A short-lived DataONE JWT is required; supply it with "
                "metasalmonpy.knb_publication.set_dataone_token()."
            )
        return str(_DATAONE_TOKEN)

    def _request(
        self,
        method: str,
        url: str,
        authenticated: bool,
        **kwargs: object,
    ):
        import requests

        headers = dict(kwargs.pop("headers", {}) or {})
        if authenticated:
            headers["Authorization"] = "Bearer " + self._token()
        try:
            response = requests.request(
                method, url, headers=headers, timeout=self.timeout, **kwargs
            )
        except requests.RequestException as error:
            raise KnbHttpError(
                f"DataONE request to {url} failed: {_redact(error)}"
            ) from None
        return response

    def _perform(
        self, method: str, url: str, authenticated: bool, **kwargs: object
    ):
        response = self._request(method, url, authenticated, **kwargs)
        if response.status_code != 200:
            raise KnbHttpError(
                f"DataONE request to {url} failed with HTTP "
                f"{response.status_code}.",
                status=response.status_code,
            )
        return response

    # -- the fourteen adapter methods ---------------------------------------

    def connect(self, environment: str, node_id: str) -> KnbClient:
        if environment != ENVIRONMENT or node_id != NODE_ID:
            raise ValueError(
                "The default publisher only supports production KNB."
            )
        _require_transport()
        return KnbClient(environment, node_id, MN_ENDPOINT)

    def preflight(self, client: KnbClient) -> Dict[str, object]:
        """Pin KNB's identity anonymously, then verify the subject server-side."""
        endpoint = _nonempty_scalar(client.endpoint, "endpoint")
        document = _capabilities_document(
            self._perform(
                "GET", re.sub(r"/+$", "", endpoint) + "/node", False
            ).content
        )
        _validate_live_capabilities(document, endpoint, NODE_ID)

        token = self._token()
        token_subject = _token_subject(token)

        ping = self._request(
            "GET", re.sub(r"/+$", "", endpoint) + "/monitor/ping", False
        )
        if ping.status_code != 200:
            raise ValueError("Direct KNB MN ping did not succeed.")

        # AuthenticationManager checks token claims locally but does not
        # verify the JWT signature. The CN diagnostic endpoint performs the
        # server-side check and echoes only identities accepted for the
        # session.
        credentials = self._echo_credentials(client)
        subject = _server_verified_subject(credentials, token_subject)
        client.subject = subject
        client.authenticated = True
        return {
            "subject": _nonempty_scalar(subject, "subject"),
            "endpoint": endpoint,
            "node_id": NODE_ID,
        }

    def list_formats(self, client: KnbClient) -> List[str]:
        response = self._perform(
            "GET", re.sub(r"/+$", "", client.cn_endpoint) + "/formats", False
        )
        document = ET.fromstring(response.content)
        formats = []
        for node in document.iter():
            if _local_name(node.tag) != "formatId":
                continue
            value = (node.text or "").strip()
            if value and value not in formats:
                formats.append(value)
        return formats

    def lookup_system_metadata(
        self, client: KnbClient, pid: str
    ) -> Optional[Dict[str, object]]:
        return self._lookup_metadata(client, pid, "PID")

    def lookup_series_id(
        self, client: KnbClient, series_id: str
    ) -> Optional[Dict[str, object]]:
        return self._lookup_metadata(client, series_id, "series identifier")

    def create_object(
        self,
        client: KnbClient,
        object_spec: Dict[str, object],
        subject: str,
        public: bool,
    ) -> str:
        self._require_authenticated(client)
        sysmeta = _system_metadata_bytes(
            object_spec,
            subject,
            public,
            client.node_id,
            object_spec["replication_policy"],
        )
        response = self._perform(
            "POST",
            re.sub(r"/+$", "", client.endpoint) + "/object",
            True,
            files={
                "pid": (None, str(object_spec["pid"])),
                "object": (
                    os.path.basename(str(object_spec["path"])),
                    object_spec["bytes"],
                    "application/octet-stream",
                ),
                "sysmeta": ("sysmeta.xml", sysmeta, "text/xml"),
            },
        )
        return self._returned_identifier(response, str(object_spec["pid"]))

    def update_object(
        self,
        client: KnbClient,
        old_pid: str,
        object_spec: Dict[str, object],
        subject: str,
        public: bool,
    ) -> str:
        self._require_authenticated(client)
        sysmeta = _system_metadata_bytes(
            object_spec,
            subject,
            public,
            client.node_id,
            object_spec["replication_policy"],
        )
        response = self._perform(
            "PUT",
            re.sub(r"/+$", "", client.endpoint)
            + "/object/"
            + quote(str(old_pid), safe=""),
            True,
            files={
                "newPid": (None, str(object_spec["pid"])),
                "object": (
                    os.path.basename(str(object_spec["path"])),
                    object_spec["bytes"],
                    "application/octet-stream",
                ),
                "sysmeta": ("sysmeta.xml", sysmeta, "text/xml"),
            },
        )
        return self._returned_identifier(response, str(object_spec["pid"]))

    def get_bytes(self, client: KnbClient, pid: str) -> bytes:
        self._require_authenticated(client)
        return self._perform(
            "GET",
            re.sub(r"/+$", "", client.endpoint)
            + "/object/"
            + quote(str(pid), safe=""),
            True,
        ).content

    def get_system_metadata(
        self, client: KnbClient, pid: str
    ) -> Dict[str, object]:
        self._require_authenticated(client)
        return _parse_system_metadata(
            self._perform(
                "GET",
                re.sub(r"/+$", "", client.endpoint)
                + "/meta/"
                + quote(str(pid), safe=""),
                True,
            ).content
        )

    def get_checksum(
        self, client: KnbClient, pid: str, algorithm: str
    ) -> Optional[str]:
        self._require_authenticated(client)
        response = self._perform(
            "GET",
            re.sub(r"/+$", "", client.endpoint)
            + "/checksum/"
            + quote(str(pid), safe="")
            + "?checksumAlgorithm="
            + quote(str(algorithm), safe=""),
            True,
        )
        document = ET.fromstring(response.content)
        return None if document.text is None else document.text.strip()

    def get_anonymous_bytes(self, endpoint: str, pid: str) -> bytes:
        return self._perform(
            "GET",
            re.sub(r"/+$", "", str(endpoint))
            + "/object/"
            + quote(str(pid), safe=""),
            False,
        ).content

    def get_anonymous_system_metadata(
        self, endpoint: str, pid: str
    ) -> Dict[str, object]:
        return _parse_system_metadata(
            self._perform(
                "GET",
                re.sub(r"/+$", "", str(endpoint))
                + "/meta/"
                + quote(str(pid), safe=""),
                False,
            ).content
        )

    def catalog_lookup(
        self, client: KnbClient, plan: Dict[str, object]
    ) -> List[Dict[str, object]]:
        self._require_authenticated(client)
        response = self._request("GET", _catalog_url(plan), True)
        if response.status_code != 200:
            raise ValueError(
                "Authenticated DataONE catalog lookup failed after HTTP "
                f"{response.status_code}."
            )
        return _catalog_docs(json.loads(response.content.decode("utf-8")))

    def anonymous_catalog_lookup(
        self, plan: Dict[str, object]
    ) -> List[Dict[str, object]]:
        response = self._perform("GET", _catalog_url(plan), False)
        return _catalog_docs(json.loads(response.content.decode("utf-8")))

    # -- internals ----------------------------------------------------------

    def _require_authenticated(self, client: KnbClient) -> None:
        if not getattr(client, "authenticated", False):
            raise ValueError(
                "The default KNB client has not completed authenticated "
                "preflight."
            )

    def _echo_credentials(self, client: KnbClient) -> List[Tuple[str, object]]:
        response = self._perform(
            "GET",
            re.sub(r"/+$", "", client.cn_endpoint) + "/diag/subject",
            True,
        )
        document = ET.fromstring(response.content)
        credentials: List[Tuple[str, object]] = []
        for node in document.iter():
            if _local_name(node.tag) != "person":
                continue
            person = [
                (_local_name(child.tag), (child.text or "").strip())
                for child in node
            ]
            credentials.append(("person", person))
        return credentials

    def _lookup_metadata(
        self, client: KnbClient, identifier: str, kind: str
    ) -> Optional[Dict[str, object]]:
        """Mirror ``.ms_knb_lookup_pid_default`` / ``_lookup_series_default``.

        Both the Member Node and the Coordinating Node are asked; an
        ambiguous binding is an error, and any status other than 200/404
        leaves existence unresolved so no create is safe.
        """
        self._require_authenticated(client)
        results = []
        for endpoint, where in (
            (client.endpoint, "at KNB"),
            (client.cn_endpoint, "at the Coordinating Node"),
        ):
            response = self._request(
                "GET",
                re.sub(r"/+$", "", endpoint)
                + "/meta/"
                + quote(str(identifier), safe=""),
                True,
            )
            state = _lookup_http_status(
                response.status_code, identifier, kind + " " + where
            )
            if state == "present":
                results.append(_parse_system_metadata(response.content))
        if not results:
            return None
        identifiers = {
            _optional_scalar(metadata.get("identifier")) for metadata in results
        }
        if kind.startswith("series"):
            if len(identifiers) != 1 or None in identifiers:
                raise ValueError(
                    "The metadata series identifier has an ambiguous DataONE "
                    "binding."
                )
        elif identifiers != {identifier}:
            raise ValueError("The planned PID has an ambiguous DataONE binding.")
        return results[0]

    @staticmethod
    def _returned_identifier(response, fallback: str) -> str:
        try:
            document = ET.fromstring(response.content)
        except ET.ParseError:
            return fallback
        for node in document.iter():
            if _local_name(node.tag) == "identifier" and node.text:
                return node.text.strip()
        if document.text:
            return document.text.strip()
        return fallback


def _lookup_http_status(status: object, identifier: str, kind: str) -> str:
    """Mirror ``.ms_knb_lookup_http_status``."""
    try:
        code = int(status)
    except (TypeError, ValueError):
        code = None
    if code == 200:
        return "present"
    if code == 404:
        return "absent"
    raise ValueError(
        f"DataONE {kind} existence for {identifier} is ambiguous after HTTP "
        f"{status}; no create is safe."
    )


def _default_adapter() -> DataOneRestAdapter:
    """Mirror ``.ms_knb_default_adapter``."""
    _require_transport()
    return DataOneRestAdapter()


# --- public API ------------------------------------------------------------------------------


def publish_sdp_to_knb(
    path: Union[str, Path],
    eml_path: Optional[Union[str, Path]] = None,
    public: Optional[bool] = None,
    manifest_path: Optional[Union[str, Path]] = None,
    dry_run: bool = True,
    confirm: Optional[bool] = None,
    revision_manifest: Optional[Union[str, Path]] = None,
    representation: str = "archive",
) -> Dict[str, object]:
    """Publish a reviewed Salmon Data Package to production KNB.

    Plans an immutable DataONE package containing the original data resources
    named by ``tables.csv``, one validated EML 2.2.0 metadata object, and a
    deterministic OAI-ORE resource map. The ``expanded`` representation
    publishes each allowlisted canonical SDP artifact as a named,
    EML-documented DataONE object and records its package-relative path with
    PROV-O ``atLocation``; it does not create a ZIP or duplicate the source
    table. The compatibility ``archive`` representation publishes one
    deterministic SDP ZIP instead. Neither mode scans arbitrary package files.
    The default operation is a credential-free, network-free dry run. Live
    publication requires a pre-existing exact dry-run manifest and an
    explicitly supplied ``confirm=True`` approving that plan. Redistribution
    authority is recorded separately in the reviewed EML sidecar.

    DataONE credentials are read only inside the live adapter. Supply a
    short-lived DataONE JWT through
    :func:`metasalmonpy.knb_publication.set_dataone_token`; credentials are
    never accepted as function arguments and never written to the manifest.

    A live restricted deposit is the KNB review/staging mechanism; KNB does
    not expose a separate server-side draft state. The persistent object
    identifiers remain even while access is private. This function does not
    call KNB's separate Publish action and never mints a DOI.

    Revisions must be built in a fresh versioned SDP directory. Keep the prior
    package and its verified manifest unchanged, write the corrected SDP to a
    new directory with a new ``publication.revision_key``, and choose a new
    local manifest path there.

    Publication materializes object bytes in memory for exact hashing and
    readback. It is intended for modest tabular SDPs; large packages should be
    tested in a dry run and may require a future streaming adapter.

    Requires the ``metasalmonpy[knb]`` extra (it always rebuilds reviewed EML).

    Parameters
    ----------
    path:
        Directory containing the reviewed Salmon Data Package.
    eml_path:
        Validated EML output path. Defaults to ``metadata/eml.xml`` inside
        ``path``; it is rebuilt deterministically before planning.
    public:
        Explicit access decision. ``True`` requests anonymous read access for
        every DataONE object and three DataONE preservation replicas.
        ``False`` creates a restricted KNB-only production deposit, disables
        peer replication, and requires authenticated exact-byte/SystemMetadata
        verification plus anonymous denial for every object and zero anonymous
        catalog matches. There is no implicit access default.
    manifest_path:
        Recovery manifest path inside ``path``. Defaults to
        ``publication/knb-manifest.json``.
    dry_run:
        When ``True`` (the default), write only local plan artifacts and never
        construct a DataONE adapter or read credentials.
    confirm:
        Explicit approval of the pre-existing exact dry-run plan and live
        mutation. Live mode requires exactly ``True``; the default can never
        authorize a live call.
    revision_manifest:
        Optional path to the verified manifest for the preceding KNB version.
        Supplying it plans an immutable DataONE revision: the reviewed sidecar
        must contain a new ``publication.revision_key``, the metadata series
        stays stable, and the new EML/resource-map objects obsolete their
        predecessors. Access cannot change in the same operation.
    representation:
        ``"expanded"`` publishes the closed SDP artifact inventory as
        individually named objects whose relative paths can reconstruct the
        package. ``"archive"`` (the compatibility default) publishes one
        deterministic ZIP in addition to each source data object.

    Returns
    -------
    dict
        Publication status, identifiers, normalized manifest and resource-map
        paths, the optional SDP-archive path, the representation, and the
        manifest itself.
    """
    if representation not in ("archive", "expanded"):
        raise ValueError(
            'representation must be one of "archive" or "expanded".'
        )
    _validate_flag(public, "public")
    _validate_flag(dry_run, "dry_run")
    if not dry_run and confirm is not True:
        raise ValueError(
            "Live KNB publication requires an explicit confirm=True. This "
            "approves the pre-existing exact dry-run manifest; redistribution "
            "authority is recorded separately."
        )
    _require_knb_extra()
    root = _package_root(path)
    prior_manifest = _revision_manifest(
        None if revision_manifest is None else str(revision_manifest)
    )
    if prior_manifest is not None:
        prior_manifest_path = os.path.realpath(str(revision_manifest))
        if prior_manifest_path.startswith(root + os.sep):
            raise ValueError(
                "A KNB revision requires a fresh versioned SDP directory. "
                "Keep the preceding package and verified manifest unchanged; "
                "build the revised SDP and its new manifest in a different "
                "directory."
            )

    if eml_path is None:
        eml_path = os.path.join(root, "metadata", "eml.xml")
    if manifest_path is None:
        manifest_path = os.path.join(root, "publication", "knb-manifest.json")
    publication_paths = _publication_paths(
        root, str(eml_path), str(manifest_path)
    )
    eml_path = str(publication_paths["eml_path"])
    manifest_path = str(publication_paths["manifest_path"])
    resource_map_path = str(publication_paths["resource_map_path"])
    previous = _existing_manifest(manifest_path)
    if not dry_run and previous is None:
        raise ValueError(
            "Live KNB publication requires a pre-existing exact matching "
            "reviewed dry-run manifest."
        )

    manifest_parent = os.path.dirname(manifest_path)
    if not os.path.isdir(manifest_parent):
        try:
            os.makedirs(manifest_parent, exist_ok=True)
        except OSError:
            pass
    if not os.path.isdir(manifest_parent):
        raise ValueError(
            "Could not create publication artifact directory "
            f"{manifest_parent}."
        )

    plan = _build_plan(
        root,
        eml_path,
        manifest_path,
        public,
        representation=representation,
        prior_manifest=prior_manifest,
        resource_map_path=resource_map_path,
    )
    if previous is not None and _optional_scalar(
        previous.get("plan_sha256")
    ) != plan["plan_sha256"]:
        raise ValueError(
            "The existing publication manifest describes a different plan. "
            "DataONE PIDs are immutable. Supply revision_manifest and a new "
            "manifest_path for a reviewed revision."
        )
    if not dry_run:
        _require_reviewed_manifest(previous, plan)
        _require_rights_authorization(plan)

    _assert_resource_map_owned(plan, previous)
    _atomic_write_raw(plan["resource_map_bytes"], str(plan["resource_map_path"]))
    manifest = _manifest(
        plan, status="dry_run" if dry_run else "pending", previous=previous
    )
    _atomic_write_raw(_json_bytes(manifest), manifest_path)

    if dry_run:
        return _publication_result("dry_run", plan, manifest, manifest_path)

    # The live state machine is implemented below this pure planning boundary.
    adapter = _adapter()
    return _run_publication(
        plan,
        manifest,
        manifest_path,
        adapter,
        previous_status=(
            None if previous is None else _optional_scalar(previous.get("status"))
        ),
    )


__all__ = [
    "publish_sdp_to_knb",
    "DataOneRestAdapter",
    "KnbHttpError",
    "set_dataone_token",
    "set_knb_adapter",
]
