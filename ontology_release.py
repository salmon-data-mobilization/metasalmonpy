"""
Pinned ontology releases (tern ECOSYSTEM M-12).

Mirrors metasalmon's ``R/ontology-release.R``. smn and gcdfo publish each
release as an immutable snapshot directory, ``docs/releases/<version>/`` in
their repositories, which GitHub Pages serves and w3id routes from the version
IRI ``<ontology IRI>/<version>``. A pinned read reads that snapshot and nothing
else: a local copy of the directory (``snapshot_dir``), or the release itself,
downloaded into a cache directory and then read the same way. It never falls
back to the latest ontology. A snapshot that carries ``MANIFEST.sha256`` (lines
in ``sha256sum`` format) must list the file read with the digest of the bytes
read; one that carries none is read unverified, and says so.

The term indexes ``find_terms()`` builds from a release live beside the latest
indexes, in ``term_search.py``; this module finds, downloads and checks the
release files.
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Dict, List, Mapping, Optional, Union

import requests

from . import ontology_fetch
from .atomic_io import atomic_write
from .text_safety import redact_secrets


class OntologyReleaseError(RuntimeError):
    """A pinned ontology release could not be read as asked.

    Raised for a malformed pin, a snapshot that cannot be found, downloaded or
    parsed, and bytes the snapshot's ``MANIFEST.sha256`` does not vouch for.
    metasalmon signals the same failures as a condition of class
    ``metasalmon_ontology_release_error``.
    """


_RELEASE_REGISTRY: Dict[str, Dict[str, str]] = {
    "smn": {
        "iri": "https://w3id.org/smn",
        "stem": "smn",
        "pages": "https://salmon-data-mobilization.github.io/salmon-domain-ontology/releases/",
    },
    "gcdfo": {
        "iri": "https://w3id.org/gcdfo/salmon",
        "stem": "gcdfo",
        "pages": "https://dfo-pacific-science.github.io/dfo-salmon-ontology/releases/",
    },
}

# The representations a snapshot carries, by the media type that asks for each.
_RELEASE_MEDIA_TYPES: Dict[str, str] = {
    "text/turtle": "ttl",
    "application/rdf+xml": "owl",
    "application/ld+json": "jsonld",
}

# A version as the w3id routes accept it: three dot-separated numbers.
_VERSION_PATTERN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")

# One `sha256sum` line: the digest, a space, a space or `*` (text or binary
# mode), and the path.
_MANIFEST_LINE = re.compile(r"([0-9A-Fa-f]{64}) [ *](.+)")

# The characters R's `trimws()` trims, so a pin reads the same in both packages.
_TRIM = " \t\r\n"

_ABSENT_MANIFEST_NOTE = b"no MANIFEST.sha256 was served for this release\n"


def _check_release_version(version: object, ontology: str) -> str:
    if not isinstance(version, str) or not _VERSION_PATTERN.fullmatch(version):
        raise OntologyReleaseError(
            "A release is named by its version, three numbers such as '0.0.3'; "
            f"the {ontology} release was given {version!r}."
        )
    return version


def _release_ontology_for_url(url: object) -> str:
    """The ontology ``url`` names, for ``fetch_salmon_ontology()``'s pinned read."""
    bare = re.sub(r"/+$", "", "" if url is None else str(url))
    for ontology, entry in _RELEASE_REGISTRY.items():
        if bare == entry["iri"]:
            return ontology
    raise OntologyReleaseError(
        "A pinned read needs url to name smn or gcdfo: use https://w3id.org/smn/ "
        "or https://w3id.org/gcdfo/salmon."
    )


def _release_files_for_accept(accept: object, stem: str) -> Dict[str, str]:
    """The release files ``accept`` asks for, most preferred first.

    Keyed by the media type that asks for each. A higher ``q`` comes first and a
    tie keeps the order written; a type a snapshot does not carry is skipped.
    """
    entries = []
    parts = [part.strip(_TRIM) for part in ("" if accept is None else str(accept)).split(",")]
    for position, part in enumerate(part for part in parts if part):
        media_type = part.split(";", 1)[0].strip(_TRIM).lower()
        quality = 1.0
        match = re.search(r";[ \t\n\r\f\v]*q[ \t\n\r\f\v]*=[ \t\n\r\f\v]*([0-9.]+)", part)
        if match:
            try:
                quality = float(match.group(1))
            except ValueError:
                quality = 0.0
        if media_type in _RELEASE_MEDIA_TYPES and quality > 0:
            entries.append((-quality, position, media_type))
    files: Dict[str, str] = {}
    for _, _, media_type in sorted(entries):
        files.setdefault(media_type, f"{stem}.{_RELEASE_MEDIA_TYPES[media_type]}")
    if not files:
        raise OntologyReleaseError(
            "accept names no representation a release snapshot carries; a snapshot "
            "carries text/turtle, application/rdf+xml and application/ld+json."
        )
    return files


def _release_pin_map(value: object, arg: str, paths: bool = False) -> Dict[str, str]:
    """``release`` or ``snapshot_dir`` as ``find_terms()`` takes them.

    One entry per ontology, keyed by it: ``{"smn": "0.0.3"}``. Keys are trimmed
    and lower-cased, and values trimmed, as metasalmon reads its named vector.
    """
    if value is None:
        return {}
    example = "{'smn': '0.0.3'}" if not paths else "{'smn': 'docs/releases/0.0.3'}"
    if not isinstance(value, Mapping) or not value:
        raise OntologyReleaseError(f"{arg} must be a mapping from ontology to value, such as {example}.")
    keys = list(value.keys())
    if not all(isinstance(key, str) and key.strip(_TRIM) for key in keys):
        raise OntologyReleaseError(f"Each entry of {arg} must be named by its ontology, such as {example}.")
    ontologies = [key.strip(_TRIM).lower() for key in keys]
    unknown = [ontology for ontology in ontologies if ontology not in _RELEASE_REGISTRY]
    if unknown:
        raise OntologyReleaseError(
            f"{arg} can name only smn and gcdfo; not an ontology with release snapshots: "
            + ", ".join(repr(ontology) for ontology in unknown)
        )
    if len(set(ontologies)) != len(ontologies):
        raise OntologyReleaseError(f"{arg} names an ontology more than once.")
    pins: Dict[str, str] = {}
    for ontology, item in zip(ontologies, value.values()):
        if paths and isinstance(item, os.PathLike):
            item = os.fspath(item)
        if not isinstance(item, str) or not item.strip(_TRIM):
            raise OntologyReleaseError(f"Every entry of {arg} must be a non-empty string.")
        pins[ontology] = item.strip(_TRIM)
    return pins


def _find_terms_release_pins(
    release: Optional[Mapping[str, str]],
    snapshot_dir: Optional[Mapping[str, Union[str, "os.PathLike[str]"]]],
) -> Dict[str, Dict[str, Optional[str]]]:
    """The pins one ``find_terms()`` call asks for, keyed by ontology.

    Each has its ``version`` and its ``snapshot_dir``, ``None`` where not given.
    """
    versions = _release_pin_map(release, "release")
    directories = _release_pin_map(snapshot_dir, "snapshot_dir", paths=True)
    pins: Dict[str, Dict[str, Optional[str]]] = {}
    for ontology in _RELEASE_REGISTRY:
        if ontology not in versions and ontology not in directories:
            continue
        version = versions.get(ontology)
        if version is not None:
            _check_release_version(version, ontology)
        pins[ontology] = {
            "ontology": ontology,
            "version": version,
            "snapshot_dir": directories.get(ontology),
        }
    return pins


def _read_sha256_manifest(path: str) -> Dict[str, str]:
    """Reads ``MANIFEST.sha256``: the digests, lower-cased, keyed by path.

    One ``<64 hex digits> <space or *><path>`` line per file, as ``sha256sum``
    writes them. A line in any other shape, or a path listed twice with two
    digests, is an error: a manifest that cannot be read cannot verify anything.
    """
    with open(path, "rb") as handle:
        text = handle.read().decode("utf-8", errors="replace")
    # The line endings R's readLines() accepts.
    lines = [line for line in re.split(r"\r\n|\r|\n", text) if line.strip(_TRIM)]
    matches = [(line, _MANIFEST_LINE.fullmatch(line)) for line in lines]
    malformed = [line for line, match in matches if match is None]
    if malformed:
        raise OntologyReleaseError(
            f"MANIFEST.sha256 at {path} is not in sha256sum format; unreadable line: "
            + "; ".join(repr(line) for line in malformed[:3])
        )
    digests: Dict[str, str] = {}
    conflicting: List[str] = []
    for _, match in matches:
        digest = match.group(1).lower()
        name = match.group(2)
        if name.startswith("./"):
            name = name[2:]
        if name in digests:
            if digests[name] != digest and name not in conflicting:
                conflicting.append(name)
            continue
        digests[name] = digest
    if conflicting:
        raise OntologyReleaseError(
            f"MANIFEST.sha256 at {path} gives two digests for one file: " + ", ".join(conflicting)
        )
    return digests


def _release_download(urls: List[str], accept: str, dest: str, timeout_seconds: float) -> str:
    """GETs the first of ``urls`` that answers 200 and stores its body at ``dest``.

    The body replaces ``dest`` whole. Returns ``"stored"``, or ``"absent"`` when
    every URL answered 404 or 410. Any other answer, or a request that fails,
    raises, because it does not say whether the file exists. Each request is
    bounded as ``fetch_salmon_ontology()`` bounds one: ``timeout_seconds`` for
    the connection, for each wait for bytes, and for the whole transfer.
    """
    failures: List[str] = []
    for url in urls:
        deadline = ontology_fetch._monotonic() + timeout_seconds
        try:
            response = requests.get(
                url,
                headers={"Accept": accept},
                timeout=(timeout_seconds, timeout_seconds),
                stream=True,
            )
        except requests.RequestException as exc:
            failures.append(f"{url}: {redact_secrets(str(exc))}")
            continue
        with response:
            status = response.status_code
            if status == 200:
                try:
                    body = ontology_fetch._read_body(response, deadline, timeout_seconds)
                except requests.RequestException as exc:
                    failures.append(f"{url}: {redact_secrets(str(exc))}")
                    continue
                try:
                    atomic_write(body, dest)
                except OSError as exc:
                    raise OntologyReleaseError(
                        f"Failed to store the downloaded release file at {dest}: {exc}"
                    ) from exc
                return "stored"
            if status not in (404, 410):
                failures.append(f"{url}: HTTP {status}")
    if not failures:
        return "absent"
    raise OntologyReleaseError(
        f"Could not establish whether the release serves {os.path.basename(dest)}: "
        + "; ".join(failures)
    )


def _materialize_release(
    ontology: str,
    version: str,
    files: Mapping[str, str],
    directory: str,
    timeout_seconds: float,
) -> str:
    """Downloads the release into ``directory``, once, and returns the file name read.

    The first of ``files`` the release serves, then its manifest, or a marker
    recording that it serves none. A directory with neither holds an
    interrupted download and is fetched again.
    """
    entry = _RELEASE_REGISTRY[ontology]
    release_url = f"{entry['pages']}{version}/"
    manifest = os.path.join(directory, "MANIFEST.sha256")
    absent_marker = manifest + ".absent"
    complete = os.path.exists(manifest) or os.path.exists(absent_marker)
    if not complete and os.path.isdir(directory):
        every_file = [f"{entry['stem']}.{extension}" for extension in _RELEASE_MEDIA_TYPES.values()]
        for name in every_file + ["MANIFEST.sha256"]:
            path = os.path.join(directory, name)
            if os.path.exists(path):
                os.remove(path)
    os.makedirs(directory, exist_ok=True)

    chosen: Optional[str] = None
    for media_type, name in files.items():
        if os.path.exists(os.path.join(directory, name)):
            chosen = name
            break
        outcome = _release_download(
            [f"{entry['iri']}/{version}", f"{release_url}{name}"],
            accept=media_type,
            dest=os.path.join(directory, name),
            timeout_seconds=timeout_seconds,
        )
        if outcome == "stored":
            chosen = name
            break
    if chosen is None:
        raise OntologyReleaseError(
            f"The {ontology} {version} release serves none of the files asked for: "
            + ", ".join(f"{release_url}{name}" for name in files.values())
        )

    if not complete:
        outcome = _release_download(
            [f"{release_url}MANIFEST.sha256"],
            accept="text/plain",
            dest=manifest,
            timeout_seconds=timeout_seconds,
        )
        if outcome == "absent":
            atomic_write(_ABSENT_MANIFEST_NOTE, absent_marker)
    return chosen


def _resolve_release_file(
    ontology: str,
    version: Optional[str],
    snapshot_dir: Optional[str],
    files: Mapping[str, str],
    cache_root: str,
    timeout_seconds: float = 30,
) -> dict:
    """Locates the release file to read, reads it, and checks its bytes.

    ``files`` are the candidate file names, most preferred first, keyed by media
    type. Returns the path, the file name, the bytes read and their SHA-256,
    whether a manifest verified them, and where the snapshot came from. The
    bytes hashed are the bytes returned, so a caller that parses them parses
    what was checked.
    """
    entry = _RELEASE_REGISTRY[ontology]
    owned = snapshot_dir is None
    if not owned:
        directory = snapshot_dir
        if not os.path.isdir(directory):
            raise OntologyReleaseError(
                f"The {ontology} snapshot directory {directory} does not exist."
            )
        present = [name for name in files.values() if os.path.exists(os.path.join(directory, name))]
        if not present:
            raise OntologyReleaseError(
                f"The {ontology} snapshot directory {directory} holds none of the files "
                "asked for: " + ", ".join(files.values())
            )
        name = present[0]
        source = directory
    else:
        if version is None:
            raise OntologyReleaseError(
                f"A pinned {ontology} read needs a release version or a snapshot directory."
            )
        directory = os.path.join(cache_root, ontology, version)
        name = _materialize_release(ontology, version, files, directory, timeout_seconds)
        source = f"{entry['iri']}/{version}"

    path = os.path.join(directory, name)
    with open(path, "rb") as handle:
        data = handle.read()
    sha256 = hashlib.sha256(data).hexdigest()
    manifest = os.path.join(directory, "MANIFEST.sha256")
    verified = False
    if os.path.exists(manifest):
        expected = _read_sha256_manifest(manifest).get(name)
        if expected is None:
            raise OntologyReleaseError(
                f"The {ontology} snapshot's MANIFEST.sha256 does not list {name} "
                f"(snapshot: {directory})."
            )
        if expected != sha256:
            # A copy this package downloaded is removed, so the next call fetches
            # it again; a snapshot the caller supplied is never touched.
            if owned and os.path.exists(path):
                os.remove(path)
            raise OntologyReleaseError(
                f"{name} does not match the {ontology} snapshot's MANIFEST.sha256: "
                f"expected SHA-256 {expected}; the file read has {sha256} "
                f"(snapshot: {directory})."
            )
        verified = True

    return {
        "path": path,
        "file": name,
        "data": data,
        "sha256": sha256,
        "manifest_verified": verified,
        "source": source,
    }


def _release_version_from_iri(version_iri: str, ontology_iri: str) -> Optional[str]:
    """The version a declared ``owl:versionIRI`` names, or ``None``.

    ``<ontology IRI>/<X.Y.Z>``, with either scheme and an optional final slash.
    """
    bare = re.sub(r"^https?://", "", re.sub(r"/+$", "", version_iri))
    prefix = re.sub(r"^https?://", "", ontology_iri) + "/"
    if not bare.startswith(prefix):
        return None
    version = bare[len(prefix):]
    return version if _VERSION_PATTERN.fullmatch(version) else None


def _fetch_release_file(
    url: str,
    accept: str,
    cache_dir: str,
    timeout_seconds: float,
    release: Optional[str],
    snapshot_dir: Optional[Union[str, "os.PathLike[str]"]],
) -> str:
    """``fetch_salmon_ontology()`` with ``release`` or ``snapshot_dir``.

    The path to the release file ``accept`` prefers, checked against the
    snapshot's manifest.
    """
    ontology = _release_ontology_for_url(url)
    if release is not None and snapshot_dir is not None:
        raise OntologyReleaseError(
            "Give release or snapshot_dir, not both: this call does not parse the "
            "file, so it cannot check that the directory holds that release."
        )
    version = _check_release_version(release, ontology) if release is not None else None
    directory: Optional[str] = None
    if snapshot_dir is not None:
        if isinstance(snapshot_dir, os.PathLike):
            snapshot_dir = os.fspath(snapshot_dir)
        if not isinstance(snapshot_dir, str) or not snapshot_dir.strip(_TRIM):
            raise OntologyReleaseError("snapshot_dir must be one directory path.")
        directory = snapshot_dir
    entry = _RELEASE_REGISTRY[ontology]
    resolved = _resolve_release_file(
        ontology,
        version,
        directory,
        _release_files_for_accept(accept, entry["stem"]),
        cache_root=os.path.join(cache_dir, "releases"),
        timeout_seconds=timeout_seconds,
    )
    return resolved["path"]


__all__ = ["OntologyReleaseError"]
