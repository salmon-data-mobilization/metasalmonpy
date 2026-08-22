"""Loading the Salmon Data Package schema bundle, remote or vendored.

Mirrors metasalmon's ``R/schema-helpers.R``. Two properties matter more than
the mechanics:

* **Identity is derived from the loaded bundle, never asserted against a
  constant here.** metasalmon 0.2.0 fixed a defect where upstream
  ``smn-data-pkg`` migrated every profile ``$id`` and metasalmon compared the
  new value against its own constant: ``source="remote"`` aborted and the
  default ``"auto"`` silently fell back to a stale vendored bundle. The checks
  below are all *internal self-consistency* — the profile must agree with
  itself and with the rules file — so an upstream identifier change is
  followable rather than fatal. The module constants are fallbacks only.
* **One bundle answers every question in a session.** ``dataset.csv``'s
  ``spec_version`` and ``datapackage.json``'s ``sdp.specVersion`` used to read
  different sources, so one package could carry two disagreeing versions. The
  loader caches per process and both now resolve identically.

**The remote base URL is pinned to the ``sdp-0.3.0`` tag, not to ``main``.**
metasalmon's default is pinned to the same tag (``R/schema-helpers.R``,
``.ms_default_sdp_schema_base_url()``): tracking ``main`` meant every upstream
spec release broke networked schema loads — ``sdp-0.3.0`` deleted
``methods.schema.json`` and the remote fetch 404ed. Advancing the pin is part
of implementing a new spec version, and it moves **together with the vendored
bundle, never separately** (PARITY.md rows 27 and 38 record why: the two must
never name different spec eras). It is overridable, so nothing is locked away:
``METASALMONPY_SDP_SCHEMA_BASE_URL`` (or :func:`set_sdp_schema_base_url`)
names any ref or host.

The vendored bundle under ``data/schema`` and ``data/profiles`` is a verbatim
copy of the upstream ``sdp-0.3.0`` git tag. That tag has no
``methods.schema.json``: sdp-0.3.0 removed the ``metadata/methods.csv``
registry from the specification, so the legacy registry *reader* in
``sdp_methods`` carries its own frozen column contract instead of reading one
from this bundle.
"""

from __future__ import annotations

import json
import os
import re
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

_DATA_DIR = Path(__file__).resolve().parent / "data"

# The upstream ref this package's parity claim is measured against. Moves
# only together with the vendored bundle under ``data/`` (PARITY.md rows 27
# and 38).
SDP_SPEC_TAG = "sdp-0.3.0"
DEFAULT_SDP_SCHEMA_BASE_URL = (
    "https://raw.githubusercontent.com/salmon-data-mobilization/smn-data-pkg/"
    + SDP_SPEC_TAG
)

# Fallbacks only, for a bundle that omits the values. They must agree with the
# vendored files under ``data/``. ``sdp_profile_uri()`` / ``sdp_rules_uri()``
# are what callers use.
SDP_PROFILE_URL = (
    "https://salmon-data-mobilization.github.io/smn-data-pkg/"
    "profiles/salmon-data-package/v0.3/profile.json"
)
SDP_PUBLIC_SCHEMA_BASE = (
    "https://salmon-data-mobilization.github.io/smn-data-pkg/"
    "schema/frictionless/metadata"
)
SDP_RULES_URL = (
    "https://salmon-data-mobilization.github.io/smn-data-pkg/schema/sdp.rules.yaml"
)

# sdp-0.3.0 removed the ``methods`` table: the registry left the
# specification, so the bundle no longer defines (and the upstream tag no
# longer serves) ``methods.schema.json``.
SDP_METADATA_SCHEMA_PATHS = {
    "dataset": "schema/frictionless/metadata/dataset.schema.json",
    "tables": "schema/frictionless/metadata/tables.schema.json",
    "column_dictionary": "schema/frictionless/metadata/column_dictionary.schema.json",
    "codes": "schema/frictionless/metadata/codes.schema.json",
    "observation_structures": (
        "schema/frictionless/metadata/observation_structures.schema.json"
    ),
    "observation_components": (
        "schema/frictionless/metadata/observation_components.schema.json"
    ),
}

SDP_RULES_PATH = "schema/sdp.rules.yaml"
SDP_PROFILE_PATH = "profiles/salmon-data-package/v0.3/profile.json"

# The four core metadata resources a descriptor always declares, in the order
# metasalmon writes them.
_CORE_METADATA_RESOURCES = (
    "sdp_dataset",
    "sdp_tables",
    "sdp_column_dictionary",
    "sdp_codes",
)


class SdpSchemaError(RuntimeError):
    """A schema bundle that cannot be loaded, or that disagrees with itself."""


# --- the rules document ----------------------------------------------------

# ``version:`` and ``profile:`` are the only two fields of ``sdp.rules.yaml``
# this package reads, and both are plain top-level scalars in every published
# version of that machine-generated document. metasalmon parses the whole file
# with ``yaml::read_yaml``; core dependencies here are pandas + requests, and
# PyYAML lives in the ``[eml]`` extra (PARITY.md row 14). Pulling it into the
# core would make the pure-stdlib SDP writer require the extra — exactly the
# regression PARITY.md row 34 records. So the scan stays, generalised from one
# key to any top-level scalar, and **raises rather than guessing** when a key
# it needs is absent, so a format change surfaces as an error instead of a
# silently stale version.
_TOP_LEVEL_SCALAR_RE = re.compile(
    r"^(?P<key>[A-Za-z0-9_-]+):[ \t]*(?:\"(?P<dq>[^\"]*)\"|'(?P<sq>[^']*)'|(?P<bare>\S.*?))[ \t]*$"
)


def _rules_scalars(text: str) -> Dict[str, str]:
    """Every top-level scalar key of a rules document, in file order."""
    found: Dict[str, str] = {}
    for line in text.splitlines():
        match = _TOP_LEVEL_SCALAR_RE.match(line)
        if match is None:
            continue
        key = match.group("key")
        if key in found:
            continue
        value = match.group("dq")
        if value is None:
            value = match.group("sq")
        if value is None:
            value = match.group("bare")
        found[key] = value
    return found


# --- identifier normalisation ---------------------------------------------


def _schema_identifier(value: Any) -> Optional[str]:
    """A schema identifier: a single non-blank string, trimmed, or ``None``.

    Mirrors ``.ms_sdp_schema_identifier()``. Normalising at the boundary is
    what makes the consistency checks sound — testing the trimmed form while
    comparing and storing the raw one let a consistently padded
    ``" https://example.org/profile "`` pass every check and reach the written
    ``datapackage.json`` with its spaces intact.
    """
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed or None


def _uri_authority_has_host(authority: str) -> bool:
    """Whether an RFC 3986 authority (``[userinfo@]host[:port]``) has a host."""
    host = re.sub(r"^.*@", "", authority)
    host = re.sub(r":[0-9]*$", "", host)
    if not host:
        return False
    # An IP-literal is bracketed and is the one host form that may contain ':'.
    if re.match(r"^\[[0-9A-Fa-f:.]+\]$", host):
        return True
    # reg-name: unreserved / pct-encoded / sub-delims. A stray ':', a bracket
    # or a slash means this is not a host.
    if not re.match(r"^[A-Za-z0-9._~%!$&'()*+,;=-]+$", host):
        return False
    return not re.search(r"%(?![0-9A-Fa-f]{2})", host)


def _schema_uri(value: Any) -> Optional[str]:
    """The identifiers that are URIs rather than versions.

    Mirrors ``.ms_sdp_schema_uri()``. Cardinality and blankness are not enough
    for these: they are written verbatim into ``datapackage.json``, where
    Frictionless expects a dereferenceable ``profile`` URL, so a non-blank
    scalar like ``"not a URI"`` would otherwise be emitted instead of letting
    ``auto`` fall back to the vendored bundle. Deliberately not restricted to
    http/https — a ``file://`` bundle is a legitimate offline arrangement.
    """
    uri = _schema_identifier(value)
    if uri is None or re.search(r"\s", uri):
        return None
    match = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*)://([^/?#]*)", uri)
    if match is None:
        return None
    # ``file://`` legitimately has an empty authority (``file:///path``); every
    # other scheme written with ``://`` needs a host.
    if match.group(1).lower() == "file":
        return uri
    if not _uri_authority_has_host(match.group(2)):
        return None
    return uri


# --- bundle validation -----------------------------------------------------


def _validate_sdp_schema(bundle: Dict[str, Any]) -> Dict[str, Any]:
    """Mirror ``.ms_validate_sdp_schema()``, including its derived fields."""
    metadata_schemas = bundle.get("metadata_schemas")
    if not isinstance(metadata_schemas, dict):
        raise SdpSchemaError(
            "Invalid SDP schema: expected Frictionless metadata_schemas."
        )
    missing = [
        name for name in SDP_METADATA_SCHEMA_PATHS if name not in metadata_schemas
    ]
    if missing:
        raise SdpSchemaError(
            "Invalid SDP schema: missing table(s) " + ", ".join(missing) + "."
        )

    for table_name in SDP_METADATA_SCHEMA_PATHS:
        table_schema = metadata_schemas[table_name]
        if table_schema.get("sdp:table") != table_name:
            raise SdpSchemaError(
                f"Invalid SDP schema: {table_name!r} has mismatched sdp:table."
            )
        fields = table_schema.get("fields")
        if not isinstance(fields, list) or not fields:
            raise SdpSchemaError(
                f"Invalid SDP schema: table {table_name!r} has no fields."
            )
        names = [field.get("name") for field in fields]
        if any(not isinstance(name, str) or not name for name in names):
            raise SdpSchemaError(
                f"Invalid SDP schema: table {table_name!r} has unnamed fields."
            )
        if len(set(names)) != len(names):
            raise SdpSchemaError(
                f"Invalid SDP schema: table {table_name!r} has duplicate fields."
            )

    profile = bundle.get("profile") or {}
    profile_uri = _schema_uri(profile.get("$id"))
    if profile_uri is None:
        raise SdpSchemaError(
            "Invalid SDP schema: profile $id is missing or is not a single absolute URI."
        )
    # Compare the normalised forms: two identifiers padded differently denote
    # the same URI, and one padded consistently would otherwise pass every
    # check and be emitted with its spaces intact.
    declared_const = (profile.get("properties") or {}).get("profile") or {}
    if _schema_identifier(declared_const.get("const")) != profile_uri:
        raise SdpSchemaError(
            "Invalid SDP schema: profile properties.profile.const does not match profile $id."
        )
    rules = bundle.get("rules") or {}
    if _schema_identifier(rules.get("profile")) != profile_uri:
        raise SdpSchemaError(
            "Invalid SDP schema: rules profile does not match profile $id."
        )
    # Each version must exist before comparing them: two absent versions would
    # agree and the bundle would be accepted with no usable ``version`` at all.
    schema_version = _schema_identifier(rules.get("version"))
    profile_version = _schema_identifier(profile.get("sdp:version"))
    if schema_version is None or profile_version is None:
        raise SdpSchemaError(
            "Invalid SDP schema: profile sdp:version and rules version must each "
            "be a single non-empty string."
        )
    if profile_version != schema_version:
        raise SdpSchemaError(
            "Invalid SDP schema: profile sdp:version does not match rules version."
        )

    raw_rules_uri = profile.get("sdp:rules")
    rules_uri = _schema_uri(raw_rules_uri)
    if raw_rules_uri is not None and rules_uri is None:
        raise SdpSchemaError(
            "Invalid SDP schema: profile sdp:rules must be a single absolute URI "
            "when present."
        )

    bundle = dict(bundle)
    bundle["version"] = schema_version
    bundle["profile_uri"] = profile_uri
    bundle["rules_uri"] = rules_uri if rules_uri is not None else SDP_RULES_URL
    return bundle


# --- loading ---------------------------------------------------------------

_CACHE_LOCK = threading.Lock()
_CACHE: Dict[str, Any] = {"key": None, "schema": None, "warned_fallback": False}

_BASE_URL_OVERRIDE: Optional[str] = None
_SOURCE_OVERRIDE: Optional[str] = None


def set_sdp_schema_base_url(url: Optional[str]) -> None:
    """Override the remote bundle location for this process.

    Mirrors R's ``metasalmon.sdp_schema_base_url`` option. ``None`` restores
    the ``METASALMONPY_SDP_SCHEMA_BASE_URL`` environment variable, and then the
    pinned default.
    """
    global _BASE_URL_OVERRIDE
    _BASE_URL_OVERRIDE = url
    reset_schema_cache()


def set_sdp_schema_source(source: Optional[str]) -> None:
    """Override ``"auto"`` / ``"remote"`` / ``"vendored"`` for this process.

    Mirrors R's ``metasalmon.sdp_schema_source`` option.
    """
    global _SOURCE_OVERRIDE
    if source is not None and source not in ("auto", "remote", "vendored"):
        raise ValueError(
            "source must be one of 'auto', 'remote', 'vendored'; got " + repr(source)
        )
    _SOURCE_OVERRIDE = source
    reset_schema_cache()


def reset_schema_cache() -> None:
    """Drop the per-process bundle cache."""
    with _CACHE_LOCK:
        _CACHE["key"] = None
        _CACHE["schema"] = None
        _CACHE["warned_fallback"] = False


def default_sdp_schema_base_url() -> str:
    """The remote bundle location, resolved at call time.

    Read at **call** time, never at import time: an environment variable read
    once at import cannot be changed by the caller who imports the module, and
    a test that sets it in ``setUp`` would silently have no effect. metasalmon
    0.2.2 fixed exactly this bug class on its cache path.
    """
    if _BASE_URL_OVERRIDE:
        return _BASE_URL_OVERRIDE
    return os.environ.get(
        "METASALMONPY_SDP_SCHEMA_BASE_URL", DEFAULT_SDP_SCHEMA_BASE_URL
    ) or DEFAULT_SDP_SCHEMA_BASE_URL


def default_sdp_schema_source() -> str:
    """``"auto"`` unless overridden, resolved at call time."""
    if _SOURCE_OVERRIDE:
        return _SOURCE_OVERRIDE
    source = os.environ.get("METASALMONPY_SDP_SCHEMA_SOURCE", "auto") or "auto"
    if source not in ("auto", "remote", "vendored"):
        raise ValueError(
            "METASALMONPY_SDP_SCHEMA_SOURCE must be one of 'auto', 'remote', "
            "'vendored'; got " + repr(source)
        )
    return source


def vendored_path(relative: str) -> Path:
    """Absolute path to one file of the vendored ``sdp-0.3.0`` bundle."""
    return _DATA_DIR / relative


def _load_vendored_sdp_schema() -> Dict[str, Any]:
    metadata_schemas = {}
    for name, relative in SDP_METADATA_SCHEMA_PATHS.items():
        path = vendored_path(relative)
        if not path.exists():
            raise SdpSchemaError(f"Vendored SDP metadata schema is missing: {relative}.")
        with path.open("r", encoding="utf-8") as stream:
            metadata_schemas[name] = json.load(stream)

    profile_path = vendored_path(SDP_PROFILE_PATH)
    if not profile_path.exists():
        raise SdpSchemaError(f"Vendored SDP profile is missing: {SDP_PROFILE_PATH}.")
    rules_path = vendored_path(SDP_RULES_PATH)
    if not rules_path.exists():
        raise SdpSchemaError(f"Vendored SDP rules are missing: {SDP_RULES_PATH}.")

    with profile_path.open("r", encoding="utf-8") as stream:
        profile = json.load(stream)
    with rules_path.open("r", encoding="utf-8") as stream:
        rules = _rules_scalars(stream.read())

    return _validate_sdp_schema(
        {"metadata_schemas": metadata_schemas, "profile": profile, "rules": rules}
    )


def _fetch_text(base_url: str, path: str, timeout: float) -> str:
    import requests

    url = re.sub(r"/+$", "", base_url) + "/" + path
    response = requests.get(url, timeout=timeout, headers={"User-Agent": "metasalmonpy"})
    response.raise_for_status()
    return response.text


def _fetch_remote_sdp_schema(base_url: str, timeout: float = 2.0) -> Dict[str, Any]:
    metadata_schemas = {
        name: json.loads(_fetch_text(base_url, path, timeout))
        for name, path in SDP_METADATA_SCHEMA_PATHS.items()
    }
    profile = json.loads(_fetch_text(base_url, SDP_PROFILE_PATH, timeout))
    rules = _rules_scalars(_fetch_text(base_url, SDP_RULES_PATH, timeout))
    return _validate_sdp_schema(
        {"metadata_schemas": metadata_schemas, "profile": profile, "rules": rules}
    )


def load_sdp_schema(
    source: Optional[str] = None,
    refresh: bool = False,
    quiet: bool = False,
    fetch_fn: Optional[Callable[[str, float], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Load the SDP Frictionless bundle, remote-first with a vendored fallback.

    Mirrors ``.ms_load_sdp_schema()``. ``fetch_fn`` is the injection point the
    R side gets from mocking ``httr2``; it takes ``(base_url, timeout)`` and
    returns a validated bundle.
    """
    from .text_safety import redact_secrets

    resolved = source or default_sdp_schema_source()
    if resolved not in ("auto", "remote", "vendored"):
        raise ValueError(
            "source must be one of 'auto', 'remote', 'vendored'; got " + repr(resolved)
        )
    base_url = default_sdp_schema_base_url()
    cache_key = resolved + "|" + base_url

    with _CACHE_LOCK:
        if not refresh and _CACHE["key"] == cache_key and _CACHE["schema"] is not None:
            return _CACHE["schema"]

    if resolved in ("auto", "remote"):
        fetcher = fetch_fn or _fetch_remote_sdp_schema
        try:
            remote = fetcher(base_url, 2.0)
        except Exception as error:  # noqa: BLE001 - any failure falls back
            remote = None
            failure = redact_secrets(error)
        if remote is not None:
            remote = dict(remote)
            remote["source"] = "remote"
            with _CACHE_LOCK:
                _CACHE["key"] = cache_key
                _CACHE["schema"] = remote
            return remote
        if resolved == "remote":
            raise SdpSchemaError(
                "Unable to load remote SDP Frictionless schema bundle: " + failure
            )
        if not quiet:
            with _CACHE_LOCK:
                already_warned = _CACHE["warned_fallback"]
                _CACHE["warned_fallback"] = True
            if not already_warned:
                import warnings

                warnings.warn(
                    "Unable to load remote SDP Frictionless schema bundle; using "
                    "the vendored schemas bundled with metasalmonpy: " + failure,
                    RuntimeWarning,
                    stacklevel=2,
                )

    # The vendored bundle is cached under the requested source's key on
    # purpose: once a session resolves an identity, every package it writes
    # carries the same profile URI, even if the network recovers mid-script.
    schema = dict(_load_vendored_sdp_schema())
    schema["source"] = "vendored"
    with _CACHE_LOCK:
        _CACHE["key"] = cache_key
        _CACHE["schema"] = schema
    return schema


# --- derived accessors -----------------------------------------------------


def sdp_profile_version() -> str:
    """The profile version declared by the loaded bundle.

    Deliberately not pinned to the vendored bundle: pinning made
    ``dataset.csv$spec_version`` and ``datapackage.json``'s ``sdp.specVersion``
    read different sources, so one package could carry two disagreeing
    versions.
    """
    return load_sdp_schema(quiet=True)["version"]


def sdp_profile_uri() -> str:
    return load_sdp_schema(quiet=True).get("profile_uri") or SDP_PROFILE_URL


def sdp_rules_uri() -> str:
    return load_sdp_schema(quiet=True).get("rules_uri") or SDP_RULES_URL


@lru_cache(maxsize=None)
def _vendored_schema_document(table_name: str) -> dict:
    try:
        relative = SDP_METADATA_SCHEMA_PATHS[table_name]
    except KeyError:
        raise KeyError(
            f"Unknown SDP metadata table {table_name!r}; expected one of "
            + ", ".join(sorted(SDP_METADATA_SCHEMA_PATHS))
            + "."
        ) from None
    with vendored_path(relative).open("r", encoding="utf-8") as stream:
        return json.load(stream)


def sdp_schema_field_names(table_name: str) -> List[str]:
    """Field names, in order, from one metadata table schema.

    Mirrors ``.ms_sdp_schema_field_names()``. The writers and readers in
    ``sdp_methods`` and ``observation_structures`` declare their own column
    tuples for clarity; a test asserts those tuples equal what this returns,
    which is how a spec/implementation drift becomes a failing test instead of
    a package that writes columns the profile does not define.
    """
    schema = load_sdp_schema(quiet=True)
    document = schema["metadata_schemas"].get(table_name)
    if document is None:
        raise KeyError(f"Unknown SDP metadata table {table_name!r}.")
    return [str(field["name"]) for field in document.get("fields", [])]


def sdp_schema_url(schema_file: str) -> str:
    """The canonical published URL for one Frictionless metadata schema.

    The fallback composition, for a bundle that predates the resource whose
    schema is wanted. :func:`sdp_metadata_resource_schema` is what callers use.
    """
    return f"{SDP_PUBLIC_SCHEMA_BASE}/{schema_file}"


def sdp_metadata_resource_schema(name: str, fallback_file: str) -> str:
    """The schema URL for one metadata resource, taken from the loaded bundle.

    Mirrors ``.ms_sdp_metadata_resource_schema()`` (metasalmon 0.2.1). Deriving
    it means every URI in a written ``datapackage.json`` — profile, rules, and
    now per-resource schemas — comes from one validated bundle.

    The fallback is not dead code: a bundle published before the v0.2 extension
    resources existed has no ``sdp_methods`` entry, and composing the public
    base with the caller's filename is the same URL that shipped before this
    was derived.
    """
    schema = load_sdp_schema(quiet=True)
    resources = (schema.get("profile") or {}).get("sdp:metadataResources") or []
    for resource in resources:
        if resource.get("name") == name:
            declared = _schema_uri(resource.get("schema"))
            if declared is not None:
                return declared
            break
    return sdp_schema_url(fallback_file)


def sdp_metadata_resource_entries(include_codes: bool = False) -> List[Dict[str, Any]]:
    """The core metadata resource entries a descriptor declares.

    Mirrors ``.ms_sdp_metadata_resource_entries()``: name, path, title,
    description and schema all come from the loaded bundle rather than from
    literals in this file.
    """
    schema = load_sdp_schema(quiet=True)
    resources = (schema.get("profile") or {}).get("sdp:metadataResources") or []
    entries = []
    for resource in resources:
        name = resource.get("name")
        if name not in _CORE_METADATA_RESOURCES:
            continue
        if name == "sdp_codes" and not include_codes:
            continue
        entry = {
            "profile": resource.get("profile") or "tabular-data-resource",
            "name": name,
            "path": resource.get("path"),
            "title": resource.get("title") or name,
        }
        description = resource.get("description")
        if description:
            entry["description"] = description
        declared = _schema_uri(resource.get("schema"))
        if declared is not None:
            entry["schema"] = declared
        entries.append(entry)
    return entries


__all__ = [
    "DEFAULT_SDP_SCHEMA_BASE_URL",
    "SDP_METADATA_SCHEMA_PATHS",
    "SDP_PROFILE_PATH",
    "SDP_PROFILE_URL",
    "SDP_PUBLIC_SCHEMA_BASE",
    "SDP_RULES_PATH",
    "SDP_RULES_URL",
    "SDP_SPEC_TAG",
    "SdpSchemaError",
    "default_sdp_schema_base_url",
    "default_sdp_schema_source",
    "load_sdp_schema",
    "reset_schema_cache",
    "sdp_metadata_resource_entries",
    "sdp_metadata_resource_schema",
    "sdp_profile_uri",
    "sdp_profile_version",
    "sdp_rules_uri",
    "sdp_schema_field_names",
    "sdp_schema_url",
    "set_sdp_schema_base_url",
    "set_sdp_schema_source",
    "vendored_path",
]
