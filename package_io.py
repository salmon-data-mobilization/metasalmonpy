from __future__ import annotations

import json
import shutil
import datetime as _dt
import re
import urllib.parse
import warnings
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence, Union

try:
    import pandas as pd
except ImportError as exc:  # pragma: no cover - import guard
    raise ImportError("metasalmonpy requires pandas; install via `pip install pandas`.") from exc

from .dictionary import infer_dictionary, validate_dictionary
from .metadata import (
    csv_na_token,
    ensure_resource_mapping,
    fill_review_placeholders_dataset_meta,
    fill_review_placeholders_dictionary,
    fill_review_placeholders_table_meta,
    infer_codes_from_resources,
    infer_dataset_metadata_from_resources,
    infer_table_metadata_from_resources,
    is_review_placeholder,
    normalize_codes,
    normalize_dataset_meta,
    normalize_dictionary,
    normalize_table_meta,
    parse_logical,
    read_sdp_csv,
    scalar_text,
    READR_TRIM_CHARS,
)
from .nuseds import (
    nuseds_enumeration_method_crosswalk,
    nuseds_estimate_classification_crosswalk,
    nuseds_estimate_method_crosswalk,
)
from .resource_types import (
    VALUE_TYPES,
    canonical_value_tokens,
    convert_declared_tokens,
    render_resource_frame,
    typed_series,
    value_type_mismatch_record,
)
from .sdp_methods import _atomic_write_set
from .sdp_schema import (
    SDP_PROFILE_URL as _SDP_PROFILE_URL,
    SDP_RULES_URL as _SDP_RULES_URL,
    load_sdp_schema,
    sdp_metadata_resource_entries,
)

# Fallback contract identifiers. Since the 0.2.0 rung the values actually
# written come from the loaded bundle (``load_sdp_schema()``), so metasalmonpy
# can follow an upstream identifier change rather than abort on it; these
# remain for a bundle that omits them, and for callers importing them by name.
SDP_PROFILE_URL = _SDP_PROFILE_URL
SDP_RULES_URL = _SDP_RULES_URL
PACKAGE_SENTINEL = ".metasalmonpy-package"
METADATA_CSV_NAMES = (
    "dataset.csv",
    "tables.csv",
    "column_dictionary.csv",
    "codes.csv",
)


def _clean(value):
    """
    Normalize pandas/NumPy missing values to None for JSON serialization.
    """
    try:
        import pandas as pd  # type: ignore

        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, (pd.Timestamp, _dt.date, _dt.datetime)):
        return value.isoformat()
    return value


def _has_value(value) -> bool:
    return not (value is None or pd.isna(value) or value == "")


def _csv_value(value):
    cleaned = _clean(value)
    return "" if cleaned is None else cleaned


def _metadata_csv_bytes(df: pd.DataFrame) -> bytes:
    """Render one SDP metadata CSV to bytes, installed later by the commit step.

    Mirrors ``.ms_sdp_extension_csv_bytes()``. Rendering to bytes rather than
    to a path is what lets ``write_salmon_datapackage()`` decide whether to
    touch the caller's package *after* every input-dependent computation has
    succeeded (hub backlog #96's ordering half).

    The bytes are the exact bytes the former ``to_csv(path, ...)`` call
    produced — same frame preparation, same keyword arguments, only the
    destination changed. ``to_csv(None)`` and ``to_csv(path)`` share one
    encoder, and ``test_metadata_csv_bytes_match_a_direct_to_csv_write``
    pins that rather than trusting it: the writer's job here is to reorder
    when bytes are installed, never to change what they are.
    """
    # A logical column renders as ``TRUE``/``FALSE``, not Python's
    # ``True``/``False``: ``column_dictionary.csv$required`` is written by both
    # implementations and read back by both, and the two spellings made every
    # Python-written dictionary differ from R's byte-for-byte. Found by driving
    # both writers over the same package at the 0.2.0 rung.
    out = df.copy()
    for column in out.columns:
        series = out[column]
        if pd.api.types.is_bool_dtype(series.dtype):
            out[column] = [
                csv_na_token()
                if value is pd.NA or value is None
                else ("TRUE" if value else "FALSE")
                for value in series
            ]
    # ``csv_na_token()`` is the one authority for the missing-value token
    # (metasalmon 0.2.4): a missing value writes as the empty field, so a
    # literal "NA" — a real fisheries gear code — stays distinguishable in the
    # bytes.
    return out.to_csv(index=False, na_rep=csv_na_token()).encode("utf-8")


def _resource_csv_bytes(resource_df: pd.DataFrame) -> bytes:
    """Render one data resource to bytes, installed later by the commit step.

    Typed columns are rendered canonically rather than handed to ``to_csv``'s
    repr: a float 100000.0 would otherwise be written as "100000.0" and a
    logical as "True", so a package read by ``read_salmon_datapackage()`` and
    written straight back would not reproduce its own bytes. See
    ``resource_types.render_resource_frame`` for the one deliberate difference
    from ``readr::write_csv``. ``na_rep=csv_na_token()``: a missing value is
    the empty field — the single token authority every canonical read and
    write shares.
    """
    return (
        render_resource_frame(resource_df)
        .to_csv(index=False, na_rep=csv_na_token())
        .encode("utf-8")
    )


def _datapackage_json_bytes(datapackage: Dict[str, object]) -> bytes:
    """Render ``datapackage.json`` with the exact writer it has always used.

    ``json.dumps(..., indent=2)`` plus the terminating newline is byte-for-byte
    the former ``json.dump(datapackage, fp, indent=2)`` followed by
    ``fp.write("\\n")`` into a UTF-8 handle: one encoder, and ``ensure_ascii``
    defaults to True on both, so nothing above U+007F reaches the encoding
    step.

    Deliberately NOT ``sdp_methods._json_bytes()`` or any other JSON helper in
    this package. Mirrors R's reason for keeping ``.ms_datapackage_json_bytes()``
    separate from ``.ms_sdp_extension_json_bytes()``: the sibling helpers differ
    in separators, sort order or NA handling, and changing the descriptor's
    bytes is an observable behaviour change this fix must not smuggle in. The
    terminating newline is itself the last byte that was ever wrong here — it
    was the final difference between an R-written and a Python-written
    descriptor for the same package.
    """
    return (json.dumps(datapackage, indent=2) + "\n").encode("utf-8")


def _package_ownership_bytes() -> bytes:
    """Byte-identical to the ``write_text("metasalmonpy-owned\\n")`` call that
    wrote the sentinel before the write path became transactional."""
    return "metasalmonpy-owned\n".encode("utf-8")


def _read_metadata_csv(path: Path) -> pd.DataFrame:
    return read_sdp_csv(path)


def _is_review_value(value) -> bool:
    if not _has_value(value):
        return False
    text = str(value).strip()
    return text.upper().startswith(("REVIEW:", "MISSING ", "MISSING:"))


def _metadata_path(target: Path, name: str) -> Path:
    canonical = target / "metadata" / name
    if canonical.exists():
        return canonical
    return target / name


def _is_owned_package_dir(target: Path) -> bool:
    if (target / PACKAGE_SENTINEL).exists():
        return True
    canonical = target / "metadata"
    if all((canonical / name).exists() for name in ("dataset.csv", "tables.csv", "column_dictionary.csv")):
        return True
    return all((target / name).exists() for name in ("dataset.csv", "tables.csv", "column_dictionary.csv"))


def _lexical_dir(path: Path) -> Path:
    """A directory path whose final component is a real name.

    Mirrors ``.ms_lexical_dir()``. Trailing ``/`` and ``/.`` spellings are the
    ones that matter: ``Path("link/.").is_symlink()`` inspects the resolved
    target, so a symlinked root spelled ``pkg-link/.`` was accepted.
    Deliberately not ``resolve()``, which resolves the final component too and
    would make a symlinked root come back as its target and pass the check it
    exists to fail.
    """
    text = str(path)
    while True:
        stripped = re.sub(r"(?<=.)/+$", "", text)
        stripped = re.sub(r"(?<=.)/+\.$", "", stripped)
        if stripped == text:
            return Path(text)
        text = stripped


def _ends_in_parent_ref(path: Path) -> bool:
    """A trailing ``..`` is the one spelling no lexical check can make safe.

    ``readlink(2)`` resolves every component but the last, so ``a/../link``
    correctly inspects ``link`` — but ``link/..`` resolves ``link`` as an
    intermediate component and then reads ``..`` inside the target, which is a
    directory, so the check sees nothing and the root then denotes the
    *target's* parent. Collapsing ``..`` lexically would be wrong precisely
    when an earlier component is a symlink, and resolving it would follow the
    link this check exists to reject. Refusing the spelling costs nothing:
    ``a/../b`` and every other ``..`` position still works.
    """
    parts = [part for part in str(path).split("/") if part and part != "."]
    return bool(parts) and parts[-1] == ".."


def _assert_managed_paths_contained(target: Path, managed_paths) -> None:
    """Refuse to delete through a symbolic link.

    Mirrors ``.ms_assert_managed_path_contained()``. ``Path.exists()`` follows
    links, so a ``data/`` or ``metadata/`` replaced by a symlink would make
    every managed child resolve outside the package and be deleted there. The
    KNB archive already fails closed on symlinked path components; the writer
    must do the same before it removes anything.
    """
    root = _lexical_dir(target)
    if _ends_in_parent_ref(root):
        raise ValueError(
            f"Refusing to update {target}: the package root ends in '..'. Which "
            "directory that names depends on whether an earlier component is a "
            "symbolic link. Write to the directory itself instead."
        )
    # Only ``target`` is checked, never its ancestors: on macOS ``/tmp`` is a
    # link to ``/private/tmp``, so walking ancestors would reject every
    # ordinary tempdir write.
    if root.is_symlink():
        raise ValueError(
            f"Refusing to update {target}: the package root is a symbolic link. "
            "Write to the directory the link points at, or replace the link "
            "with a real directory."
        )

    prefix = str(root).rstrip("/") + "/"
    for candidate in managed_paths:
        text = str(candidate)
        if not text.startswith(prefix):
            continue
        relative = text[len(prefix):]
        current = root
        for part in relative.split("/"):
            if not part or part == ".":
                continue
            current = current / part
            if current.is_symlink():
                raise ValueError(
                    f"Refusing to update {target}: {relative} contains a "
                    "symbolic-link path component. Replace the link with a real "
                    "directory or file, or write to a new directory."
                )
            if not current.exists():
                break


def _previous_declared_data_paths(target: Path) -> list[str]:
    """Data resources declared by a previous write.

    Mirrors ``.ms_previous_declared_data_paths()``. Retaining an orphan would
    leave undeclared data in ``data/`` that validation never looks at but a
    hand-made ZIP would carry. Degrades to nothing if the previous
    ``tables.csv`` is absent or unreadable — a corrupt file must never widen
    the deletion set.
    """
    tables_path = _metadata_path(target, "tables.csv")
    if not tables_path.exists():
        return []
    try:
        previous = read_sdp_csv(tables_path)
    except Exception:  # noqa: BLE001 - a corrupt file deletes nothing
        return []
    if "file_name" not in previous.columns:
        return []
    names = []
    for value in previous["file_name"]:
        text = str(value).strip()
        if not text:
            continue
        try:
            # Normalise (which rejects '..' and absolute paths) but do NOT
            # force into ``data/``: a previous write may legitimately have
            # declared ``exports/x.csv``. Relocating it would leave the real
            # orphan behind and delete an unrelated ``data/x.csv`` this write
            # does not own.
            normalized = text.replace("\\", "/").strip()
            if re.match(r"^(?:[A-Za-z]:)?/", normalized) or ".." in normalized.split("/"):
                continue
            if not normalized or normalized.endswith("/"):
                continue
        except Exception:  # noqa: BLE001
            continue
        if normalized not in names:
            names.append(normalized)
    return names


def _package_managed_paths(target: Path, data_file_names) -> list[Path]:
    """Every path this call is authoritative for, written or not.

    Mirrors ``.ms_package_managed_paths()``. Anything absent from this list
    survives a rewrite. Deliberately NOT the KNB artifact inventory: that
    helper answers "what gets published" and aborts when a reviewed sidecar is
    absent; this one answers "what this call owns", and must degrade rather
    than abort.
    """
    managed = [target / "datapackage.json", target / PACKAGE_SENTINEL]
    for name in METADATA_CSV_NAMES:
        managed.append(target / "metadata" / name)
        # Legacy root-level shadows, which ``_metadata_path()`` still accepts.
        managed.append(target / name)
    seen = set()
    for name in list(data_file_names) + _previous_declared_data_paths(target):
        text = str(name).strip()
        if text and text not in seen:
            seen.add(text)
            managed.append(target / text)
    unique: list[Path] = []
    for candidate in managed:
        if candidate not in unique:
            unique.append(candidate)
    return unique


def _replace_create_output(path: Path) -> None:
    """Remove a create-owned output before recreating it.

    Mirrors ``.ms_replace_create_output()``. The containment check catches
    symbolic links, but it does not see HARD links, and writing through one
    truncates the shared inode outside the package. The pre-0.2.0
    full-directory wipe unlinked these entries first; preserving the directory
    removed that protection, so it has to be explicit — and it belongs next to
    each write, not in one caller, so it holds however the writer is reached.
    """
    if path.exists() or path.is_symlink():
        path.unlink()


def _check_package_write_dir(
    target: Path,
    overwrite: bool,
    prune: bool = False,
) -> None:
    """Non-destructive preflight for a package write.

    Creates a missing directory and refuses the calls that must not proceed
    (missing ``overwrite``, ``prune`` without ``overwrite``, a non-metasalmonpy
    target). Deliberately performs **no deletion** — that is
    ``_commit_package_write()``'s job, and only after the entire write set has
    been rendered to bytes.

    Keeping deletion out of this function is the fix for hub backlog #96's
    ordering half. Its predecessor, ``_prepare_package_dir()``, unlinked the
    managed paths here — before the resource rendering, the schema load, the
    descriptor build and every metadata write — so any exception in that window
    destroyed the caller's package.
    """
    if prune and not overwrite:
        raise ValueError("prune=True requires overwrite=True.")
    if not target.exists():
        target.mkdir(parents=True, exist_ok=True)
        return
    if not list(target.iterdir()):
        return
    if not overwrite:
        raise FileExistsError(
            f"Directory {target} already exists. Set overwrite=True to replace."
        )
    if not _is_owned_package_dir(target):
        raise ValueError(
            f"Refusing to overwrite non-metasalmonpy directory {target}. "
            "Use a new or empty directory, or clean it manually."
        )


def _commit_package_write(
    target: Path,
    writes: "Mapping[Path, bytes]",
    managed_paths=None,
    prune: bool = False,
) -> Path:
    """The single destructive step of a package write.

    Mirrors ``.ms_commit_package_write()``. ``writes`` is the complete,
    already-rendered write set (bytes keyed by absolute target path), so
    nothing user-input-dependent can abort past this point.

    Non-prune: install through ``sdp_methods._atomic_write_set()`` — every
    replacement is fully staged as a same-directory sibling before any current
    file moves, each replaced file is renamed aside first, and a failure
    mid-install restores the originals — then unlink the managed paths this
    call did not rewrite (orphaned data resources, legacy root-level metadata
    shadows, a stale ``codes.csv``). An abort anywhere leaves the previous
    package intact.

    ``_atomic_write_set()`` is reused rather than reimplemented here for the
    same reason R reuses ``.ms_sdp_extension_atomic_write_set()``: it already
    carries the staged-sibling install, the symlink and directory-at-path
    refusals, the umask-default mode restore (``atomic_io.apply_default_file_mode``,
    PARITY.md row 24) and the backup-detach rollback fix. A second transactional
    writer would be a second thing to keep hardened, and the two would drift.

    **``prune=True`` is honestly weaker, and says so.** Prune wipes files this
    writer does not own, which is exactly what makes the rollback guarantee
    unavailable there: the wiped sidecars are not in the write set, so nothing
    exists to restore them from. The wipe therefore runs as late as possible —
    after every input-dependent computation and the full byte rendering have
    succeeded — and the residual window is pure filesystem failure (disk full,
    permissions revoked) between the wipe and the install. That difference is
    deliberate: ``prune=True`` is an explicit request to delete everything this
    call does not write.
    """
    managed_paths = list(managed_paths or [])
    # Containment before anything destructive: refuse to delete or replace
    # through a symbolic link. The same guard the pre-#96 unlink ran, now also
    # covering the prune wipe, which previously relied on the writer's earlier
    # metadata-subset check alone.
    _assert_managed_paths_contained(target, managed_paths)

    if prune:
        for child in list(target.iterdir()):
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()

    # ``metadata/`` and ``data/`` unconditionally, matching what the writer
    # body created before this step existed: a package with every resource
    # skipped still gets an empty ``data/``.
    for directory in [target / "metadata", target / "data"] + [
        path.parent for path in writes
    ]:
        directory.mkdir(parents=True, exist_ok=True)

    _atomic_write_set({str(path): payload for path, payload in writes.items()})

    if not prune:
        for candidate in managed_paths:
            if candidate in writes:
                continue
            # No recursive removal: if a managed path ever resolves to a
            # directory this is a no-op rather than a recursive wipe.
            if candidate.is_symlink() or (candidate.exists() and candidate.is_file()):
                candidate.unlink()

    return target


def _force_data_path(file_name, resource_name: str, format: str) -> str:
    if not _has_value(file_name):
        file_name = f"{resource_name}.{format}"
    normalized = str(file_name).replace("\\", "/").strip()
    if re.match(r"^(?:[A-Za-z]:)?/", normalized):
        raise ValueError("Resource file_name must be a relative package path.")
    if ".." in normalized.split("/"):
        raise ValueError("Resource file_name must not contain '..' path segments.")
    if not normalized or normalized.endswith("/"):
        raise ValueError("Resource file_name must name a file.")
    if normalized.startswith("data/"):
        return normalized
    return f"data/{Path(normalized).name}"


def _is_semantic_code_candidate(column_name: str, series: pd.Series) -> bool:
    if not (
        pd.api.types.is_object_dtype(series)
        or pd.api.types.is_string_dtype(series)
        or isinstance(series.dtype, pd.CategoricalDtype)
    ):
        return False
    if re.search(
        r"comment|note|remark|description|details?|memo|narrative|summary|"
        r"reason|explanation|text",
        str(column_name),
        flags=re.I,
    ):
        return False
    values = series.dropna().astype(str).str.strip()
    values = values[values != ""]
    if values.empty:
        return False
    unique = values.drop_duplicates()
    if len(unique) > 30:
        return False
    if len(unique) / len(values) <= 0.5:
        return True
    code_like = unique.map(
        lambda value: len(value) <= 24
        and (
            re.search(r"\s", value) is None
            or re.fullmatch(r"[A-Za-z0-9_./-]+", value) is not None
        )
    ).all()
    return len(unique) <= 5 and bool(code_like)


def _metadata_resource_entries(include_codes: bool) -> list[dict]:
    """The metadata resource entries, derived from the loaded SDP bundle.

    Mirrors ``.ms_sdp_metadata_resource_entries()``. Until the 0.2.0 rung
    these were three literal tuples in this file carrying no ``schema`` and no
    ``description``, so a descriptor written here declared metadata resources
    with no schema while ``sdp_methods`` declared its extension resources with
    one. metasalmon 0.2.1 closed the same gap from the other side, deriving
    every per-resource schema URL from the bundle: profile, rules, and
    per-resource schemas now all come from one validated document.
    """
    return sdp_metadata_resource_entries(include_codes=include_codes)


_NAMED_LICENSES = {
    "Open Government Licence - Canada": {
        "name": "OGL-Canada-2.0",
        "title": "Open Government Licence - Canada",
        "path": "https://open.canada.ca/en/open-government-licence-canada",
    },
    "CC-BY-4.0": {
        "name": "CC-BY-4.0",
        "title": "Creative Commons Attribution 4.0 International",
        "path": "https://creativecommons.org/licenses/by/4.0/",
    },
    "MIT": {
        "name": "MIT",
        "title": "MIT License",
        "path": "https://opensource.org/license/mit",
    },
}

_DOT_SEGMENT_RE = re.compile(r"(^|/)\.\.?(/|$)")


def _is_canonical_rights_url(value: str) -> bool:
    """Mirror metasalmon v0.1.7's ``httr2`` round-trip test for a rights URL.

    R accepts a custom licence only when ``url_parse()`` yields an ``http``/
    ``https`` scheme with a hostname *and* ``url_build()`` reproduces the input
    byte for byte — that is, the value is already in canonical form. This is a
    behavioural mirror, not a transliteration: Python has no curl URL parser,
    so the same rule is expressed as the conditions curl's normalization would
    otherwise change. Verified to give R's verdict on all 21 probe values in
    ``tests/test_package_io.py::test_licence_descriptors_match_era_r`` --
    13 accepted (userinfo, port, query, fragment, mixed-case host, padded
    input, trailing slash, the three named licences) and 8 rejected
    (uppercase scheme, missing path, dot segments, embedded space, empty
    host, non-http scheme, free text, empty).

    **Known boundary, measured not assumed:** percent-encoding is *not* among
    the agreeing cases. curl decodes an encoded separator and uppercases hex
    digits, so R rejects ``%2F`` and lowercase ``%c3%a9`` while these
    conditions accept both; ``%20`` and uppercase ``%C3%A9`` agree. The four
    probes are pinned in
    ``test_percent_encoded_rights_urls_are_a_documented_divergence`` so the
    boundary cannot move silently. No SDP licence field has carried a
    percent-encoded path, which is why the divergence is documented rather
    than closed by vendoring curl's normalizer (PARITY.md row 26).
    """
    if not value or any(character.isspace() for character in value):
        return False
    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in ("http", "https"):
        return False
    # curl lowercases the scheme, so an uppercase one never round-trips.
    if not value.startswith(parts.scheme + "://"):
        return False
    if not parts.netloc or not parts.hostname:
        return False
    # curl always emits a path, and resolves "." / ".." away.
    if not parts.path or _DOT_SEGMENT_RE.search(parts.path):
        return False
    return urllib.parse.urlunsplit(parts) == value


def _license_descriptor(license_value) -> dict:
    """Mirror ``.ms_license_descriptor`` (metasalmon v0.1.7).

    0.1.7 added the final branch: a custom HTTP(S) rights URL stays a URL
    licence descriptor instead of being rejected as an unknown licence.
    """
    # R: trimws(as.character(license[[1]])) -- the named lookup is exact, so it
    # runs on the raw text, and only the URL branch sees the trimmed value.
    raw = "" if license_value is None or pd.isna(license_value) else str(license_value)
    named = _NAMED_LICENSES.get(raw)
    if named is not None:
        return dict(named)
    text = raw.strip(READR_TRIM_CHARS)
    if _is_canonical_rights_url(text):
        return {"path": text}
    raise ValueError(f"Unknown SDP publication license: {raw!r}.")


def _fill_review_placeholders(
    dataset_meta: pd.DataFrame,
    table_meta: pd.DataFrame,
    dictionary: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Fill blank metadata with metasalmon's exact placeholder prose.

    Coverage and prose were converged on current metasalmon by a byte
    differential of ``write_salmon_datapackage()`` output over identical
    blank input (S10 chunk D), retiring PARITY.md row 48. The three fill
    helpers live in ``metadata.py`` because the ``infer_*`` functions apply
    them too, exactly as metasalmon's do.
    """
    return (
        fill_review_placeholders_dataset_meta(dataset_meta),
        fill_review_placeholders_table_meta(table_meta),
        fill_review_placeholders_dictionary(dictionary),
    )


def write_salmon_datapackage(
    resources: Mapping[str, pd.DataFrame],
    dataset_meta: pd.DataFrame,
    table_meta: pd.DataFrame,
    dict_df: pd.DataFrame,
    codes: Optional[pd.DataFrame] = None,
    path: str = ".",
    format: str = "csv",
    overwrite: bool = False,
    write_datapackage: bool = True,
    prune: bool = False,
) -> Path:
    """
    Write the canonical Salmon Data Package layout.

    Metadata is written under ``metadata/``, table resources under ``data/``,
    and the Frictionless descriptor at the package root.

    ``overwrite=True`` updates the package in place. Since the 0.2.0 rung it
    **replaces only the files this writer owns** — the ``metadata/`` SDP CSVs,
    the ``data/`` resources declared in ``tables.csv`` (including any a
    previous write declared and this one does not), ``datapackage.json``, and
    the ownership sentinel. Everything else is preserved: reviewed SSSOM
    mappings and measurement decompositions under ``metadata/semantic/``, EML
    and EDH XML, ``eml-mapping.yml``, review notes, ``publication/`` artifacts,
    and the reproducibility manifest. A read → edit → write loop used to delete
    all of them.

    ``prune=True`` restores the previous behaviour, deleting every entry in the
    directory first. It requires ``overwrite=True``.

    **The write is transactional over the files it owns.** The full write set —
    data resources, metadata CSVs, ``datapackage.json`` and the ownership
    sentinel — is rendered to bytes before anything on disk is touched, then
    installed through a staged-sibling write set that rolls the originals back
    if any install fails. An abort at any point therefore leaves the caller's
    previous package byte-intact and readable. Before this, the managed paths
    were unlinked *first* and the replacements written afterwards, so any
    exception in between destroyed the package (hub backlog #96's ordering
    half; metasalmon PR #77).

    ``prune=True`` is the one honest exception, and it is narrower rather than
    absent. The wipe removes files this writer does not own, so those sidecars
    are not in the write set and nothing can restore them. The wipe now runs as
    late as possible — after every input-dependent computation and the full
    byte rendering have succeeded, so an input-triggered abort still leaves
    everything intact — but a *pure filesystem* failure (disk full, permissions
    revoked) between the wipe and the install remains unrecoverable. That is
    deliberate: ``prune=True`` is an explicit request to delete everything this
    call does not write.
    """
    if format != "csv":
        raise ValueError("Only CSV format is supported. Use format='csv'.")

    if not isinstance(dataset_meta, pd.DataFrame) or len(dataset_meta) != 1:
        raise ValueError("dataset_meta must be a single-row DataFrame.")
    if not isinstance(table_meta, pd.DataFrame) or len(table_meta) == 0:
        raise ValueError("table_meta must be a non-empty DataFrame.")
    if not isinstance(resources, Mapping) or len(resources) == 0:
        raise ValueError("resources must be a named mapping of DataFrames.")
    if any(not isinstance(v, pd.DataFrame) for v in resources.values()):
        raise ValueError("All resources must be pandas DataFrames.")

    dict_valid = normalize_dictionary(validate_dictionary(dict_df, require_iris=False))
    dataset_meta = normalize_dataset_meta(dataset_meta)
    table_meta = normalize_table_meta(table_meta)
    codes = normalize_codes(codes)
    dataset_meta, table_meta, dict_valid = _fill_review_placeholders(
        dataset_meta,
        table_meta,
        dict_valid,
    )
    for resource_name in resources:
        table_rows = table_meta["table_id"] == resource_name
        if not table_rows.any():
            continue
        file_name = table_meta.loc[table_rows, "file_name"].iloc[0]
        table_meta.loc[table_rows, "file_name"] = _force_data_path(
            file_name,
            resource_name,
            format,
        )

    target = Path(path)

    # Containment BEFORE reading anything. ``_package_managed_paths()`` parses
    # the previous ``tables.csv``, so a ``metadata/`` or ``metadata/tables.csv``
    # replaced by a symlink would be read before the guard ran. The metadata
    # paths are known without reading, so they are checked first — including
    # the legacy root-level shadows, which ``_metadata_path()`` still accepts
    # and ``_previous_declared_data_paths()`` will therefore read.
    _assert_managed_paths_contained(
        target,
        [target / "metadata" / name for name in METADATA_CSV_NAMES]
        + [target / name for name in METADATA_CSV_NAMES]
        + [target / "datapackage.json", target / PACKAGE_SENTINEL],
    )
    resolved_file_names = [
        table_meta.loc[table_meta["table_id"] == name, "file_name"].iloc[0]
        for name in resources
        if (table_meta["table_id"] == name).any()
    ]
    managed_paths = _package_managed_paths(target, resolved_file_names)
    orphaned = [
        name
        for name in _previous_declared_data_paths(target)
        if name not in resolved_file_names and (target / name).exists()
    ]

    # Non-destructive preflight only. Nothing on disk is deleted or replaced
    # until every input-dependent computation below has succeeded: the old
    # ordering unlinked the managed paths here and wrote replacements
    # afterwards, so ANY abort in between — a typed metadata column, a broken
    # schema bundle, a serialization error — destroyed the caller's package
    # (hub backlog #96). The entire write set is rendered to bytes first and
    # every deletion and replacement happens in one place,
    # ``_commit_package_write()``, at the end.
    _check_package_write_dir(target, overwrite=overwrite, prune=prune)

    dataset_id = dataset_meta["dataset_id"].iloc[0]

    # Keyed by absolute target path. Assigning by key keeps the last rendering
    # when two resources resolve to one file, matching the last-write-wins
    # behaviour of the sequential writes it replaced.
    writes: Dict[Path, bytes] = {}
    resource_entries = []
    for resource_name, resource_df in resources.items():
        table_info = table_meta[table_meta["table_id"] == resource_name]
        if table_info.empty:
            warnings.warn(
                f"No table metadata found for resource {resource_name!r}; "
                "skipping it.",
                UserWarning,
                stacklevel=2,
            )
            continue
        file_name = (
            table_info["file_name"].iloc[0]
            if "file_name" in table_info
            else f"{resource_name}.{format}"
        )
        file_name = _force_data_path(file_name, resource_name, format)
        # Rendered to bytes now, installed later by ``_commit_package_write()``.
        writes[target / file_name] = _resource_csv_bytes(resource_df)

        table_dict = dict_valid[
            (dict_valid["dataset_id"] == dataset_id) & (dict_valid["table_id"] == resource_name)
        ]
        fields = []
        for _, row in table_dict.iterrows():
            # Key order and emission rules mirror the R field builder exactly.
            # Three differences were found by driving both writers over the
            # same package at the 0.2.0 rung, and all three were accidental:
            # the title was suppressed when it equalled the column name (R
            # emits it whenever ``column_label`` is non-blank), ``constraints``
            # was emitted with ``required: false`` (R emits the block only for
            # a required column), and a single-column primary key was written
            # as a one-element array (R writes the scalar).
            field = {
                "name": _clean(row["column_name"]),
                # A blank column_label stays as an explicit null, exactly as
                # R's builder leaves the NA in place and jsonlite renders it —
                # popping the key made the two descriptors differ on any
                # unlabeled column (S10 chunk D byte differential).
                "title": _clean(row.get("column_label")),
                "type": _clean(row["value_type"]),
                "description": _clean(row["column_description"]),
            }
            # Mirror R's isTRUE(): only a genuine True emits the constraints
            # block. ``bool(...)`` alone read a missing ``required`` as true —
            # iterrows() hands a boolean-dtype NA back as a truthy float nan —
            # so every blank ``required`` claimed the column was required
            # (found by the S10 chunk D descriptor byte differential; the
            # shipped example's RUN_TYPE and ESTIMATE_STAGE rows hit it).
            required_flag = row.get("required")
            if not pd.isna(required_flag) and bool(required_flag) is True:
                field["constraints"] = {"required": True}
            for optional_key in [
                "unit_iri",
                "term_iri",
                "term_type",
                "property_iri",
                "entity_iri",
                "constraint_iri",
                "statistical_modifier_iri",
            ]:
                value = row.get(optional_key)
                if pd.notna(value) and value not in ("", None):
                    field[optional_key] = _clean(value)
            fields.append(field)

        resource_entry = {
            "name": resource_name,
            "path": file_name,
            "profile": "tabular-data-resource",
            "schema": {"fields": fields},
        }
        if _has_value(table_info["table_label"].iloc[0]):
            resource_entry["title"] = _clean(table_info["table_label"].iloc[0])
        if "description" in table_info and _has_value(table_info["description"].iloc[0]):
            resource_entry["description"] = _clean(table_info["description"].iloc[0])
        if "primary_key" in table_info and _has_value(table_info["primary_key"].iloc[0]):
            primary_key = [
                value.strip()
                for value in str(table_info["primary_key"].iloc[0]).split(",")
                if value.strip()
            ]
            resource_entry["schema"]["primaryKey"] = (
                primary_key[0] if len(primary_key) == 1 else primary_key
            )
        resource_entries.append(resource_entry)

    # Every URI written here comes from the one loaded, self-consistent bundle,
    # so the descriptor can never declare a profile the bundle disagrees with.
    sdp_bundle = load_sdp_schema(quiet=True)
    declared_spec_version = (
        str(dataset_meta["spec_version"].iloc[0]).strip()
        if "spec_version" in dataset_meta and _has_value(dataset_meta["spec_version"].iloc[0])
        else ""
    )
    if declared_spec_version and declared_spec_version != sdp_bundle["version"]:
        warnings.warn(
            f"metadata/dataset.csv declares {declared_spec_version!r} but the loaded "
            f"SDP schema is {sdp_bundle['version']!r}. The package will carry both "
            "values; clear spec_version to adopt the loaded schema version.",
            UserWarning,
            stacklevel=2,
        )

    datapackage = {
        "profile": sdp_bundle["profile_uri"],
        "name": re.sub(r"[^a-z0-9._-]+", "-", str(dataset_id).lower()).strip("-"),
        "id": _clean(dataset_id),
        "title": _clean(dataset_meta.get("title", pd.Series([None])).iloc[0]),
        "description": _clean(dataset_meta.get("description", pd.Series([None])).iloc[0]),
        "sdp": {
            "specVersion": sdp_bundle["version"],
            "profile": sdp_bundle["profile_uri"],
            "rules": sdp_bundle["rules_uri"],
            "metadata": {
                "dataset": "metadata/dataset.csv",
                "tables": "metadata/tables.csv",
                "columnDictionary": "metadata/column_dictionary.csv",
                "codes": "metadata/codes.csv" if codes is not None else None,
            },
        },
        "resources": _metadata_resource_entries(codes is not None) + resource_entries,
    }

    # Optional metadata
    if "creator" in dataset_meta and _has_value(dataset_meta["creator"].iloc[0]):
        datapackage["contributors"] = [
            {"title": _clean(dataset_meta["creator"].iloc[0]), "role": "creator"}
        ]
    # The contact contributor was simply missing here until the 0.2.0 rung; R
    # has emitted it since 0.1.x, so a Python-written descriptor silently
    # dropped the dataset contact.
    if "contact_name" in dataset_meta and _has_value(dataset_meta["contact_name"].iloc[0]):
        contact = {
            "title": _clean(dataset_meta["contact_name"].iloc[0]),
            "role": "contact",
        }
        if "contact_email" in dataset_meta and _has_value(dataset_meta["contact_email"].iloc[0]):
            contact["email"] = _clean(dataset_meta["contact_email"].iloc[0])
        if "contact_org" in dataset_meta and _has_value(dataset_meta["contact_org"].iloc[0]):
            contact["organization"] = _clean(dataset_meta["contact_org"].iloc[0])
        datapackage["contributors"] = datapackage.get("contributors", []) + [contact]
    if "license" in dataset_meta and _has_value(dataset_meta["license"].iloc[0]):
        license_value = dataset_meta["license"].iloc[0]
        # R gates on ``.ms_is_review_placeholder()`` — the three placeholder
        # spellings — not on the broader review-value test: a bare
        # ``REVIEW:`` licence reaches the descriptor and aborts there, in
        # both implementations.
        if not is_review_placeholder(license_value):
            datapackage["licenses"] = [_license_descriptor(license_value)]
    # ``_has_value`` rather than ``pd.notna``: an empty ``temporal_start``
    # is not missing to pandas, so a descriptor carried ``"temporal": {"start":
    # "", "end": ""}``. R has always tested both conditions.
    if "temporal_start" in dataset_meta and _has_value(dataset_meta["temporal_start"].iloc[0]):
        datapackage["temporal"] = {"start": _clean(dataset_meta["temporal_start"].iloc[0])}
        if "temporal_end" in dataset_meta and _has_value(dataset_meta["temporal_end"].iloc[0]):
            datapackage["temporal"]["end"] = _clean(dataset_meta["temporal_end"].iloc[0])

    # Render canonical SDP metadata after any file_name defaults were resolved.
    metadata_dir = target / "metadata"
    writes[metadata_dir / "dataset.csv"] = _metadata_csv_bytes(dataset_meta)
    writes[metadata_dir / "tables.csv"] = _metadata_csv_bytes(table_meta)
    writes[metadata_dir / "column_dictionary.csv"] = _metadata_csv_bytes(dict_valid)
    if codes is not None:
        writes[metadata_dir / "codes.csv"] = _metadata_csv_bytes(codes)

    if write_datapackage:
        writes[target / "datapackage.json"] = _datapackage_json_bytes(datapackage)
    writes[target / PACKAGE_SENTINEL] = _package_ownership_bytes()

    # The single destructive step: everything above this line is pure
    # computation over the caller's inputs, everything below it is filesystem
    # installation of already-final bytes.
    _commit_package_write(
        target,
        writes,
        managed_paths=managed_paths,
        prune=prune,
    )

    # Reported only once the replacement is actually installed: before the fix
    # this warned about deletions the caller might never receive a package for.
    if not prune and orphaned:
        warnings.warn(
            "Removed data resource(s) no longer declared in tables.csv: "
            + ", ".join(sorted(orphaned)),
            UserWarning,
            stacklevel=2,
        )

    return target


def create_salmon_datapackage(
    resources: Mapping[str, pd.DataFrame],
    dataset_meta: pd.DataFrame,
    table_meta: pd.DataFrame,
    dict_df: pd.DataFrame,
    codes: Optional[pd.DataFrame] = None,
    path: str = ".",
    format: str = "csv",
    overwrite: bool = False,
) -> Path:
    """Compatibility alias for :func:`write_salmon_datapackage`."""
    warnings.warn(
        "create_salmon_datapackage() is deprecated; use "
        "write_salmon_datapackage() for manual writes or create_sdp() "
        "for the one-shot workflow.",
        DeprecationWarning,
        stacklevel=2,
    )
    return write_salmon_datapackage(
        resources=resources,
        dataset_meta=dataset_meta,
        table_meta=table_meta,
        dict_df=dict_df,
        codes=codes,
        path=path,
        format=format,
        overwrite=overwrite,
    )


def read_salmon_datapackage(path: str) -> Dict[str, object]:
    """
    Read a Salmon Data Package from canonical CSV metadata when available.
    """
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(f"Directory {target} does not exist.")

    dataset_path = _metadata_path(target, "dataset.csv")
    tables_path = _metadata_path(target, "tables.csv")
    dict_path = _metadata_path(target, "column_dictionary.csv")
    codes_path = _metadata_path(target, "codes.csv")
    json_path = target / "datapackage.json"

    if dataset_path.exists() and tables_path.exists() and dict_path.exists():
        dataset_meta = normalize_dataset_meta(_read_metadata_csv(dataset_path))
        table_meta = normalize_table_meta(_read_metadata_csv(tables_path))
        dictionary = normalize_dictionary(_read_metadata_csv(dict_path))
        dictionary["required"] = parse_logical(dictionary["required"])
    else:
        if not json_path.exists():
            raise FileNotFoundError(
                f"No Salmon Data Package metadata found in {target}; expected canonical CSV metadata or datapackage.json."
            )

        with json_path.open("r", encoding="utf-8") as fp:
            datapackage = json.load(fp)

        dataset_meta = normalize_dataset_meta(
            pd.DataFrame(
                {
                    "dataset_id": [datapackage.get("id") or datapackage.get("name")],
                    "title": [datapackage.get("title")],
                    "description": [datapackage.get("description")],
                    "creator": [datapackage.get("creator")],
                    "license": [datapackage.get("license")],
                    "temporal_start": [datapackage.get("temporal", {}).get("start") if datapackage.get("temporal") else None],
                    "temporal_end": [datapackage.get("temporal", {}).get("end") if datapackage.get("temporal") else None],
                }
            )
        )

        table_rows = []
        dict_rows = []
        for resource in datapackage.get("resources", []):
            resource_name = resource.get("name")
            resource_path = str(resource.get("path") or "")
            if resource_path.startswith("metadata/"):
                continue
            table_rows.append(
                {
                    "dataset_id": datapackage.get("id") or datapackage.get("name"),
                    "table_id": resource_name,
                    "file_name": resource.get("path"),
                    "table_label": resource.get("title") or resource_name,
                    "description": resource.get("description"),
                    "observation_unit": None,
                    "observation_unit_iri": None,
                    "primary_key": ",".join(resource.get("schema", {}).get("primaryKey", []))
                    if isinstance(resource.get("schema", {}).get("primaryKey"), list)
                    else resource.get("schema", {}).get("primaryKey"),
                }
            )

            for field in resource.get("schema", {}).get("fields", []) or []:
                required = None
                if isinstance(field.get("constraints"), dict) and "required" in field["constraints"]:
                    required = bool(field["constraints"]["required"])
                # Older descriptor-first packages carry the semantic bindings
                # under per-field ``custom`` keys; current writers emit bare
                # keys. R coalesces custom-first at every field
                # (``.ms_read_salmon_datapackage``), so this reader does too.
                custom = field.get("custom") if isinstance(field.get("custom"), dict) else {}
                dict_rows.append(
                    {
                        "dataset_id": datapackage.get("id") or datapackage.get("name"),
                        "table_id": resource_name,
                        "column_name": field.get("name"),
                        "column_label": field.get("title") or field.get("name"),
                        "column_description": field.get("description"),
                        "column_role": custom.get("sdp:columnRole", field.get("column_role")),
                        "value_type": field.get("type", "string"),
                        "unit_label": custom.get("sdp:unitLabel", field.get("unit_label")),
                        "unit_iri": custom.get("sdp:unitIri", field.get("unit_iri")),
                        "term_iri": custom.get("sdp:termIri", field.get("term_iri")),
                        "term_type": custom.get("sdp:termType", field.get("term_type")),
                        "required": required,
                        "property_iri": custom.get("iAdopt:propertyIri", field.get("property_iri")),
                        "entity_iri": custom.get("iAdopt:entityIri", field.get("entity_iri")),
                        "constraint_iri": custom.get("iAdopt:constraintIri", field.get("constraint_iri")),
                        # The legacy iAdopt:methodIri key is deliberately NOT
                        # read here: migrate_sdp_methods() reads old
                        # descriptors directly, so a descriptor-only sdp-0.2.0
                        # package keeps its method binding until migration
                        # relocates it to tables.csv.
                        "statistical_modifier_iri": custom.get(
                            "iAdopt:statisticalModifierIri",
                            field.get("statistical_modifier_iri"),
                        ),
                    }
                )

        table_meta = normalize_table_meta(pd.DataFrame(table_rows))
        dictionary = normalize_dictionary(pd.DataFrame(dict_rows))
        dictionary["required"] = parse_logical(dictionary["required"])

    codes = None
    if codes_path.exists():
        codes = normalize_codes(_read_metadata_csv(codes_path))

    resources = {}
    for _, row in table_meta.iterrows():
        resource_name = row.get("table_id")
        file_name = row.get("file_name")
        if not _has_value(resource_name) or not _has_value(file_name):
            continue
        file_path = target / str(file_name)
        if not file_path.exists() and not str(file_name).startswith("data/"):
            file_path = target / "data" / str(file_name)
        if file_path.exists():
            table_dict = dictionary[dictionary["table_id"] == resource_name]
            resources[str(resource_name)] = _read_resource_csv(
                file_path, table_dict, str(resource_name)
            )
        else:
            # Mirrors R's read warning; the validator reports the missing
            # resource as a typed issue on top of this.
            warnings.warn(
                f"Resource file '{file_path}' not found, skipping",
                UserWarning,
                stacklevel=2,
            )

    return {
        "dataset": dataset_meta,
        "tables": table_meta,
        "dictionary": dictionary,
        "codes": codes,
        "resources": resources,
    }


def _read_resource_csv(
    file_path: Path, table_dict: pd.DataFrame, table_id: str = ""
) -> pd.DataFrame:
    """Read a data resource with the types its dictionary declares.

    Mirrors ``.ms_read_resource_csv()`` (metasalmon 0.2.0). **The dictionary is
    the sole type authority**: a column the dictionary does not declare stays
    character rather than being guessed, which is what makes the write → read
    round trip lossless.

    One text read, then in-memory conversion — rather than a typed read plus a
    re-read when something looks wrong. That keeps the original token available
    for every fidelity check, and it is one pass over the file instead of two.

    Data resources go through the same reader as every other SDP CSV. A bare
    ``pd.read_csv()`` applied pandas' full default NA vocabulary ("null",
    "N/A", "nan", "<NA>", "None", "-1.#IND", …) and skipped readr's
    ``trim_ws``, so a gear code of "null" was destroyed on read and a padded
    header survived into the parsed frame. The literal ``"NA"`` stays data here
    by PARITY.md row 21 — which is also why a literal ``NA`` in a *declared
    numeric* column is reported as a value-type mismatch here and was silently
    missing under era R, whose reader still took ``na = c("", "NA")``. That
    matches metasalmon 0.2.4 onward, and is the one behaviour this reader does
    not share with the 0.2.0 release it mirrors.
    """
    raw = read_sdp_csv(file_path)
    declared_types = {}
    if (
        isinstance(table_dict, pd.DataFrame)
        and len(table_dict) > 0
        and {"column_name", "value_type"}.issubset(table_dict.columns)
    ):
        names = [str(value).strip() for value in table_dict["column_name"]]
        types = [str(value).strip() for value in table_dict["value_type"]]
        lookup = {}
        for name, value_type in zip(names, types):
            lookup.setdefault(name, value_type)
        for column in raw.columns:
            if column in lookup:
                declared_types[column] = lookup[column]

    parsed = raw.copy()
    mismatches = []
    for column, value_type in declared_types.items():
        if value_type not in VALUE_TYPES or value_type == "string":
            continue
        outcome = convert_declared_tokens(list(raw[column]), value_type)
        if outcome.reason is None:
            parsed[column] = typed_series(outcome.values, value_type)
            continue
        # The declared type is not satisfied: keep the exact token so the
        # code-value check still sees it, and report the declaration as wrong.
        mismatches.append(
            value_type_mismatch_record(table_id, column, value_type, outcome)
        )

    if mismatches:
        parsed.attrs["ms_value_type_mismatches"] = mismatches
    return parsed


def _prefill_legacy_code_terms(
    codes: Optional[pd.DataFrame],
    dictionary: Optional[pd.DataFrame],
    required_words: Sequence[str],
    crosswalk: pd.DataFrame,
) -> Optional[pd.DataFrame]:
    """Shared engine for the legacy NuSEDS code-term prefills.

    A codes.csv row gets its ``term_iri`` filled from ``crosswalk`` when
    (a) its column's name — or its dictionary name/label/description —
    contains every word in ``required_words``, (b) the row has no explicit
    ``term_iri``, and (c) the code value has a crosswalk row with a
    non-missing ontology term. Explicit values always win; crosswalk rows
    that map to missing (recorded non-mappings, e.g. ``NO SURVEY THIS YEAR``)
    never fill anything. Mirrors metasalmon's ``.ms_prefill_legacy_code_terms``.
    """
    codes = normalize_codes(codes)
    if codes is None or codes.empty:
        return codes

    def normalize_text(value) -> Optional[str]:
        if pd.isna(value):
            return None
        text = str(value).strip()
        return text.lower() if text else None

    def expand_gcdfo_term(value) -> Optional[str]:
        if pd.isna(value):
            return None
        text = str(value).strip()
        if not text:
            return None
        if text.startswith("gcdfo:"):
            return "https://w3id.org/gcdfo/salmon#" + text[len("gcdfo:"):]
        return text

    def column_flag(column_name, column_label=None, column_description=None) -> bool:
        parts = [
            "" if pd.isna(value) else str(value)
            for value in (column_name, column_label, column_description)
        ]
        text = normalize_text(re.sub(r"[^0-9a-zA-Z]+", " ", " ".join(parts)))
        if text is None:
            return False
        return all(
            re.search(rf"\b{re.escape(str(word))}\b", text)
            for word in required_words
        )

    target_rows = codes["column_name"].apply(column_flag)
    if dictionary is not None and len(dictionary) > 0:
        dictionary = normalize_dictionary(dictionary)
        flag_lookup = {}
        for _, dict_row in dictionary.iterrows():
            key = "\r".join(
                "" if pd.isna(dict_row.get(column)) else str(dict_row.get(column))
                for column in ("dataset_id", "table_id", "column_name")
            )
            flag_lookup[key] = column_flag(
                dict_row.get("column_name"),
                dict_row.get("column_label"),
                dict_row.get("column_description"),
            )
        code_keys = codes.apply(
            lambda row: "\r".join(
                "" if pd.isna(row.get(column)) else str(row.get(column))
                for column in ("dataset_id", "table_id", "column_name")
            ),
            axis=1,
        )
        target_rows = target_rows | code_keys.map(
            lambda key: flag_lookup.get(key, False)
        )

    crosswalk_lookup = {
        normalize_text(value): expand_gcdfo_term(term)
        for value, term in zip(
            crosswalk["nuseds_value"], crosswalk["ontology_term"]
        )
    }
    mapped_terms = codes["code_value"].map(
        lambda value: crosswalk_lookup.get(normalize_text(value))
    )
    existing = codes["term_iri"].map(
        lambda value: "" if pd.isna(value) else str(value).strip()
    )
    fill_rows = (
        target_rows
        & (existing == "")
        & mapped_terms.map(lambda term: term is not None and term != "")
    )
    if fill_rows.any():
        codes = codes.copy()
        codes.loc[fill_rows, "term_iri"] = mapped_terms[fill_rows]
    return codes


def _prefill_legacy_estimate_method_code_terms(codes, dictionary=None):
    return _prefill_legacy_code_terms(
        codes,
        dictionary,
        required_words=("estimate", "method"),
        crosswalk=nuseds_estimate_method_crosswalk(),
    )


def _prefill_legacy_estimate_classification_code_terms(codes, dictionary=None):
    return _prefill_legacy_code_terms(
        codes,
        dictionary,
        required_words=("estimate", "classification"),
        crosswalk=nuseds_estimate_classification_crosswalk(),
    )


# "enumeration" alone, not ("enumeration", "method"): NuSEDS names the column
# ENUMERATION_METHODS (plural), and the engine's \b word test would never
# match "methods" with the singular. The single word is specific enough —
# crosswalk keys ("Fence", "Bank Walk", ...) gate what actually fills.
def _prefill_legacy_enumeration_method_code_terms(codes, dictionary=None):
    return _prefill_legacy_code_terms(
        codes,
        dictionary,
        required_words=("enumeration",),
        crosswalk=nuseds_enumeration_method_crosswalk(),
    )


def infer_salmon_datapackage_artifacts(
    resources,
    dataset_id: str = "dataset-1",
    table_id: str = "table-1",
    guess_types: bool = True,
    seed_semantics: bool = True,
    semantic_sources: Optional[Sequence[str]] = None,
    semantic_max_per_role: int = 1,
    seed_verbose: bool = True,
    seed_codes: Optional[pd.DataFrame] = None,
    seed_table_meta: Optional[pd.DataFrame] = None,
    seed_dataset_meta: Optional[pd.DataFrame] = None,
    semantic_code_scope: str = "factor",
    llm_assess: bool = False,
    llm_provider: str = "openai",
    llm_model: Optional[str] = None,
    llm_api_key: Optional[str] = None,
    llm_base_url: Optional[str] = None,
    llm_reasoning_effort: Optional[str] = None,
    llm_top_n: int = 5,
    llm_context_files=None,
    llm_context_text=None,
    llm_timeout_seconds: int = 60,
    llm_request_fn=None,
) -> Dict[str, object]:
    """
    Infer the in-memory artifacts needed to write a Salmon Data Package.

    This is the inspect-before-write entry point behind :func:`create_sdp`.
    It normalizes one DataFrame or a named resource mapping, infers dataset,
    table, dictionary, and code metadata, and optionally attaches deterministic
    or opt-in LLM semantic review output.

    Parameters
    ----------
    resources
        A DataFrame or named mapping of DataFrames.
    seed_semantics
        Retrieve semantic candidates when ``True``.
    semantic_sources
        ``None`` uses role-aware source defaults. Explicit values form a strict
        retrieval allowlist.
    llm_assess
        Enable LLM review. It has no effect when ``seed_semantics=False``.
    llm_context_files
        Local context paths used only when both semantic seeding and LLM review
        are enabled.

    Returns
    -------
    dict
        Resource and metadata DataFrames plus ``semantic_suggestions`` and
        ``semantic_llm_assessments`` when available.
    """
    if semantic_code_scope not in {"factor", "all", "none"}:
        raise ValueError(
            "semantic_code_scope must be 'factor', 'all', or 'none'."
        )
    resource_map = ensure_resource_mapping(resources, table_id=table_id)
    dict_df = infer_dictionary(
        resource_map,
        guess_types=guess_types,
        dataset_id=dataset_id,
        table_id=table_id,
        seed_semantics=False,
        semantic_sources=semantic_sources,
        semantic_max_per_role=semantic_max_per_role,
        seed_verbose=seed_verbose,
    )
    table_meta = normalize_table_meta(seed_table_meta) if seed_table_meta is not None else infer_table_metadata_from_resources(resource_map, dataset_id)
    codes = normalize_codes(seed_codes) if seed_codes is not None else infer_codes_from_resources(resource_map, dataset_id)
    # The legacy NuSEDS crosswalk prefills run exactly where metasalmon runs
    # them (.ms_infer_resource_artifact_context, package mode): after the codes
    # are settled and before semantic seeding, so a crosswalk-filled term_iri
    # both lands in codes.csv and suppresses a redundant code-level search.
    # Hub backlog #101/#102 and PARITY row 47: until S10 chunk B, NO crosswalk
    # was wired into this path at all — not even the estimate one R has wired
    # since the crosswalks landed.
    codes = _prefill_legacy_estimate_method_code_terms(codes, dictionary=dict_df)
    codes = _prefill_legacy_estimate_classification_code_terms(codes, dictionary=dict_df)
    codes = _prefill_legacy_enumeration_method_code_terms(codes, dictionary=dict_df)
    dataset_meta = (
        normalize_dataset_meta(seed_dataset_meta)
        if seed_dataset_meta is not None
        else infer_dataset_metadata_from_resources(resource_map, dataset_id)
    )

    semantic_suggestions = None
    semantic_llm_assessments = None
    if seed_semantics:
        if seed_verbose:
            print("Seeding semantic suggestions during infer_salmon_datapackage_artifacts().")
        from .semantics import suggest_semantics

        semantic_codes = codes
        if semantic_code_scope == "none":
            semantic_codes = None
        elif semantic_code_scope == "factor" and codes is not None:
            categorical_keys = []
            for resource_name, resource_df in resource_map.items():
                for column in resource_df.columns:
                    if _is_semantic_code_candidate(
                        str(column),
                        resource_df[column],
                    ):
                        categorical_keys.append((dataset_id, resource_name, column))
            if categorical_keys:
                allowed = pd.MultiIndex.from_tuples(
                    categorical_keys,
                    names=["dataset_id", "table_id", "column_name"],
                )
                code_keys = pd.MultiIndex.from_frame(
                    codes[["dataset_id", "table_id", "column_name"]]
                )
                semantic_codes = codes.loc[code_keys.isin(allowed)].copy()
            else:
                semantic_codes = codes.iloc[0:0].copy()

        dict_df = suggest_semantics(
            resource_map,
            dict_df,
            sources=semantic_sources,
            max_per_role=semantic_max_per_role,
            include_dwc=False,
            codes=semantic_codes,
            table_meta=table_meta,
            dataset_meta=dataset_meta,
            llm_assess=llm_assess,
            llm_provider=llm_provider,
            llm_model=llm_model,
            llm_api_key=llm_api_key,
            llm_base_url=llm_base_url,
            llm_reasoning_effort=llm_reasoning_effort,
            llm_top_n=llm_top_n,
            llm_context_files=llm_context_files,
            llm_context_text=llm_context_text,
            llm_timeout_seconds=llm_timeout_seconds,
            llm_request_fn=llm_request_fn,
        )
        semantic_suggestions = dict_df.attrs.get("semantic_suggestions")
        semantic_llm_assessments = dict_df.attrs.get(
            "semantic_llm_assessments"
        )
    elif llm_assess:
        warnings.warn(
            "LLM review options are ignored when seed_semantics=False.",
            UserWarning,
            stacklevel=2,
        )

    return {
        "resources": resource_map,
        "dataset_id": dataset_id,
        "dict": dict_df,
        "table_meta": table_meta,
        "codes": codes,
        "dataset_meta": dataset_meta,
        "semantic_suggestions": semantic_suggestions,
        "semantic_llm_assessments": semantic_llm_assessments,
    }


def _write_review_readme(package_path: Path, has_suggestions: bool) -> None:
    suggestion_line = (
        "Use semantic_suggestions.csv only as a fallback shortlist after "
        "reviewing the authoritative metadata files."
        if has_suggestions
        else "No semantic_suggestions.csv was written for this package."
    )
    lines = [
        "SALMON DATA PACKAGE REVIEW",
        "",
        "1. Review metadata/dataset.csv and metadata/tables.csv.",
        "2. Review metadata/column_dictionary.csv and metadata/codes.csv.",
        "3. Replace every MISSING placeholder and REVIEW: IRI.",
        "4. Run validate_salmon_datapackage(path, require_iris=True).",
        "5. Rebuild EDH XML with write_edh_xml_from_sdp(path), if needed.",
        "",
        suggestion_line,
        "",
        "Share the complete package directory or a zip of that directory.",
    ]
    readme_path = package_path / "README-review.txt"
    _replace_create_output(readme_path)
    readme_path.write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def _mark_review_iri(value):
    if not _has_value(value):
        return value
    text = str(value)
    return text if text.startswith("REVIEW:") else f"REVIEW:{text}"


def _auto_apply_package_suggestions(artifacts: dict, llm_assess: bool) -> None:
    suggestions = artifacts.get("semantic_suggestions")
    if not isinstance(suggestions, pd.DataFrame) or suggestions.empty:
        return
    from .semantics import (
        _table_suggestion_is_compatible,
        apply_semantic_suggestions,
    )

    strategy = "llm" if llm_assess else "top"
    auto_roles = ["variable", "property", "entity", "unit"]
    before = artifacts["dict"].copy()
    applied = apply_semantic_suggestions(
        artifacts["dict"],
        suggestions=suggestions,
        strategy=strategy,
        roles=auto_roles,
        overwrite=False,
        verbose=False,
    )
    role_fields = {
        "variable": "term_iri",
        "property": "property_iri",
        "entity": "entity_iri",
        "unit": "unit_iri",
    }
    for field in role_fields.values():
        was_missing = before[field].apply(lambda value: not _has_value(value))
        is_filled = applied[field].apply(_has_value)
        applied.loc[was_missing & is_filled, field] = applied.loc[
            was_missing & is_filled, field
        ].map(_mark_review_iri)
    artifacts["dict"] = applied

    selected = suggestions.copy()
    if llm_assess:
        if {"llm_selected", "llm_decision"} <= set(selected.columns):
            selected = selected[
                selected["llm_selected"].fillna(False).astype(bool)
                & (selected["llm_decision"] == "accept")
            ]
        else:
            selected = selected.iloc[0:0]
    table_contract = {
        "target_scope",
        "target_sdp_file",
        "target_sdp_field",
        "dictionary_role",
    }
    if table_contract <= set(selected.columns):
        table_rows = selected[
            (selected["target_scope"] == "table")
            & (selected["target_sdp_file"] == "tables.csv")
            & (selected["target_sdp_field"] == "observation_unit_iri")
            & (selected["dictionary_role"] == "entity")
        ]
    else:
        table_rows = selected.iloc[0:0]
    for _, suggestion in table_rows.iterrows():
        mask = (
            artifacts["table_meta"]["dataset_id"].astype(str)
            == str(suggestion.get("dataset_id"))
        ) & (
            artifacts["table_meta"]["table_id"].astype(str)
            == str(suggestion.get("table_id"))
        )
        missing = artifacts["table_meta"]["observation_unit_iri"].apply(
            lambda value: not _has_value(value)
        )
        compatible = artifacts["table_meta"].apply(
            lambda table_row: _table_suggestion_is_compatible(
                suggestion,
                table_row,
            ),
            axis=1,
        )
        rows = mask & missing & compatible
        if rows.any() and _has_value(suggestion.get("iri")):
            artifacts["table_meta"].loc[
                rows, "observation_unit_iri"
            ] = _mark_review_iri(suggestion["iri"])
            if "label" in suggestion and _has_value(suggestion.get("label")):
                label_missing = artifacts["table_meta"][
                    "observation_unit"
                ].apply(lambda value: not _has_value(value) or _is_review_value(value))
                artifacts["table_meta"].loc[
                    rows & label_missing, "observation_unit"
                ] = suggestion["label"]


def create_sdp(
    resources,
    path: Optional[str] = None,
    dataset_id: str = "dataset-1",
    table_id: str = "table_1",
    guess_types: bool = True,
    seed_semantics: bool = True,
    semantic_sources: Optional[Sequence[str]] = None,
    semantic_max_per_role: int = 1,
    seed_verbose: bool = True,
    seed_codes: Optional[pd.DataFrame] = None,
    seed_table_meta: Optional[pd.DataFrame] = None,
    seed_dataset_meta: Optional[pd.DataFrame] = None,
    semantic_code_scope: str = "factor",
    llm_assess: bool = False,
    llm_provider: str = "openai",
    llm_model: Optional[str] = None,
    llm_api_key: Optional[str] = None,
    llm_base_url: Optional[str] = None,
    llm_reasoning_effort: Optional[str] = None,
    llm_top_n: int = 5,
    llm_context_files=None,
    llm_context_text=None,
    llm_timeout_seconds: int = 60,
    llm_request_fn=None,
    check_updates: bool = False,
    format: str = "csv",
    overwrite: bool = False,
    include_edh_xml: bool = False,
    prune: bool = False,
) -> Path:
    """
    Create a review-ready Salmon Data Package in one call.

    The function infers package artifacts, optionally seeds semantic
    suggestions, writes the canonical ``data/`` and ``metadata/`` layout, and
    adds a review checklist. Inferred IRIs retain the ``REVIEW:`` marker.

    LLM assessment is strictly opt-in through ``llm_assess=True``. Supplying
    context without enabling assessment warns and makes no provider request.
    ``overwrite=True`` replaces only directories recognized as owned package
    directories, and since the 0.2.0 rung it replaces only the files the writer
    owns within them: reviewed sidecars in an existing package survive a
    rewrite. Pass ``prune=True`` (which requires ``overwrite=True``) for the
    previous delete-everything behaviour. See
    :func:`write_salmon_datapackage`.

    Returns
    -------
    pathlib.Path
        Path to the created package directory.
    """
    if llm_context_files is not None:
        from .llm_review import validate_context_files

        validate_context_files(llm_context_files)
    if (llm_context_files is not None or llm_context_text is not None) and not llm_assess:
        warnings.warn(
            "LLM context is ignored unless llm_assess=True.",
            UserWarning,
            stacklevel=2,
        )
    if path is None or not str(path).strip():
        safe_id = re.sub(r"[^A-Za-z0-9._-]+", "-", dataset_id).strip("-")
        path = str(Path.cwd() / f"{safe_id}-sdp")
    if check_updates:
        from .version_check import check_for_updates

        check_for_updates(quiet=False)

    artifacts = infer_salmon_datapackage_artifacts(
        resources=resources,
        dataset_id=dataset_id,
        table_id=table_id,
        guess_types=guess_types,
        seed_semantics=seed_semantics,
        semantic_sources=semantic_sources,
        semantic_max_per_role=semantic_max_per_role,
        seed_verbose=seed_verbose,
        seed_codes=seed_codes,
        seed_table_meta=seed_table_meta,
        seed_dataset_meta=seed_dataset_meta,
        semantic_code_scope=semantic_code_scope,
        llm_assess=llm_assess,
        llm_provider=llm_provider,
        llm_model=llm_model,
        llm_api_key=llm_api_key,
        llm_base_url=llm_base_url,
        llm_reasoning_effort=llm_reasoning_effort,
        llm_top_n=llm_top_n,
        llm_context_files=llm_context_files,
        llm_context_text=llm_context_text,
        llm_timeout_seconds=llm_timeout_seconds,
        llm_request_fn=llm_request_fn,
    )
    _auto_apply_package_suggestions(artifacts, llm_assess=llm_assess)
    pkg_path = write_salmon_datapackage(
        resources=artifacts["resources"],
        dataset_meta=artifacts["dataset_meta"],
        table_meta=artifacts["table_meta"],
        dict_df=artifacts["dict"],
        codes=artifacts["codes"],
        path=path,
        format=format,
        overwrite=overwrite,
        prune=prune,
    )

    # ``create_sdp()`` writes these itself, after the generic writer has run,
    # so they are deliberately absent from the writer's managed paths — that is
    # what preserves a reviewed copy on a rewrite. They still need the same
    # containment check: without it a symlinked ``README-review.txt`` is
    # followed and an external file is truncated.
    _assert_managed_paths_contained(
        pkg_path,
        [
            pkg_path / "README-review.txt",
            pkg_path / "semantic_suggestions.csv",
            pkg_path / "metadata" / "metadata-edh-hnap.xml",
        ],
    )

    suggestions = artifacts.get("semantic_suggestions")
    suggestions_path = pkg_path / "semantic_suggestions.csv"
    if isinstance(suggestions, pd.DataFrame) and not suggestions.empty:
        # ``create_sdp()`` owns this file, so it clears its own stale copy
        # rather than writing through a hard link the pre-0.2.0 full-directory
        # wipe used to unlink implicitly.
        _replace_create_output(suggestions_path)
        suggestions.to_csv(
            suggestions_path,
            index=False,
            na_rep=csv_na_token(),
        )
    elif suggestions_path.exists() or suggestions_path.is_symlink():
        suggestions_path.unlink()
    _write_review_readme(
        pkg_path,
        has_suggestions=isinstance(suggestions, pd.DataFrame)
        and not suggestions.empty,
    )

    if include_edh_xml:
        from .edh_xml import edh_build_hnap_xml

        output = pkg_path / "metadata" / "metadata-edh-hnap.xml"
        _replace_create_output(output)
        package = read_salmon_datapackage(pkg_path)
        edh_build_hnap_xml(
            package["dataset"],
            output_path=output,
        )
        if _collect_review_issues(package):
            warnings.warn(
                "Created EDH XML is a draft because package metadata still "
                "contains unresolved review values. Review the package and "
                "run write_edh_xml_from_sdp() to rebuild it.",
                UserWarning,
                stacklevel=2,
            )
    return pkg_path


def create_salmon_datapackage_from_data(
    resources,
    path: str,
    dataset_id: str = "dataset-1",
    table_id: str = "table-1",
    guess_types: bool = True,
    seed_semantics: bool = True,
    semantic_sources: Optional[Sequence[str]] = (
        "smn",
        "gcdfo",
        "ols",
        "nvs",
    ),
    semantic_max_per_role: int = 1,
    seed_verbose: bool = True,
    seed_codes: Optional[pd.DataFrame] = None,
    seed_table_meta: Optional[pd.DataFrame] = None,
    seed_dataset_meta: Optional[pd.DataFrame] = None,
    format: str = "csv",
    overwrite: bool = False,
    include_edh_xml: bool = False,
    edh_profile: str = "dfo_edh_hnap",
    edh_xml_path: Optional[str] = None,
    **kwargs,
) -> Path:
    """Deprecated one-shot wrapper with its legacy call contract preserved."""
    warnings.warn(
        "create_salmon_datapackage_from_data() is deprecated; use create_sdp().",
        DeprecationWarning,
        stacklevel=2,
    )
    package_path = create_sdp(
        resources=resources,
        path=path,
        dataset_id=dataset_id,
        table_id=table_id,
        guess_types=guess_types,
        seed_semantics=seed_semantics,
        semantic_sources=semantic_sources,
        semantic_max_per_role=semantic_max_per_role,
        seed_verbose=seed_verbose,
        seed_codes=seed_codes,
        seed_table_meta=seed_table_meta,
        seed_dataset_meta=seed_dataset_meta,
        format=format,
        overwrite=overwrite,
        include_edh_xml=False,
        **kwargs,
    )
    if include_edh_xml:
        from .edh_xml import edh_build_iso19139_xml

        default_name = (
            "metadata-edh-hnap.xml"
            if edh_profile == "dfo_edh_hnap"
            else "metadata-iso19139.xml"
        )
        output = (
            Path(edh_xml_path)
            if edh_xml_path is not None
            else package_path / default_name
        )
        package = read_salmon_datapackage(package_path)
        edh_build_iso19139_xml(
            package["dataset"],
            output_path=output,
            profile=edh_profile,
        )
    return package_path


def _validation_row_context(frame: pd.DataFrame, position: int, id_fields) -> str:
    """Mirror ``.ms_validation_row_context``: ``row N (field=value, ...)``."""
    bits = []
    for field in id_fields:
        if field not in frame.columns:
            continue
        value = scalar_text(frame[field].iloc[position])
        if value:
            bits.append(f"{field}={value}")
    if not bits:
        return f"row {position + 1}"
    return f"row {position + 1} ({', '.join(bits)})"


def _collect_review_placeholder_issues(
    frame: object, source_name: str, id_fields=()
) -> list[str]:
    """Mirror ``.ms_collect_review_placeholder_issues``: strict-mode messages
    for every unresolved ``MISSING METADATA:`` / ``MISSING DESCRIPTION:`` /
    ``REVIEW REQUIRED:`` placeholder left in one metadata file."""
    if not isinstance(frame, pd.DataFrame) or len(frame) == 0:
        return []
    messages = []
    for field in frame.columns:
        for position in range(len(frame)):
            value = frame[field].iloc[position]
            if pd.isna(value) or not is_review_placeholder(value):
                continue
            context = _validation_row_context(frame, position, id_fields)
            messages.append(
                f"{source_name} {context} field {field} still contains an "
                f"unresolved review placeholder ({value}). Replace it before "
                "final validation."
            )
    return messages


def _collect_missing_table_observation_unit_iri_issues(
    table_meta: object, source_name: str = "metadata/tables.csv"
) -> list[str]:
    """Mirror ``.ms_collect_missing_table_observation_unit_iri_issues``."""
    if (
        not isinstance(table_meta, pd.DataFrame)
        or len(table_meta) == 0
        or "observation_unit_iri" not in table_meta.columns
    ):
        return []
    messages = []
    for position in range(len(table_meta)):
        if scalar_text(table_meta["observation_unit_iri"].iloc[position]):
            continue
        context = _validation_row_context(
            table_meta, position, ("table_id", "file_name")
        )
        messages.append(
            f"{source_name} {context} field observation_unit_iri is blank. "
            "Final validation requires a resolved table observation-unit IRI."
        )
    return messages


_REVIEW_IRI_RE = re.compile(r"^\s*REVIEW\s*:", re.IGNORECASE)


def _collect_review_iri_issues(frame: object, source_name: str) -> list[str]:
    """Mirror ``.ms_collect_review_iri_issues``: REVIEW-prefixed values left
    in any ``*_iri`` column of one metadata file."""
    if not isinstance(frame, pd.DataFrame) or len(frame) == 0:
        return []
    messages = []
    for field in frame.columns:
        if not str(field).endswith("_iri"):
            continue
        for position in range(len(frame)):
            value = frame[field].iloc[position]
            if pd.isna(value) or not _REVIEW_IRI_RE.match(str(value)):
                continue
            messages.append(
                f"{source_name} row {position + 1} field {field} still "
                f"contains a REVIEW-prefixed IRI ({value}). Remove the REVIEW "
                "prefix only after final manual validation."
            )
    return messages


def _collect_review_issues(package: Dict[str, object]) -> list[str]:
    """Every unresolved review signal across the package's metadata frames.

    Placeholders, blank table observation-unit IRIs, and REVIEW-prefixed
    IRIs, with R's message texts. The EDH XML gates build on this;
    ``validate_salmon_datapackage()``'s strict path composes the same
    collectors itself so its issue set stays R-shaped.
    """
    dataset = package.get("dataset")
    tables = package.get("tables")
    dictionary = package.get("dictionary")
    codes = package.get("codes")
    return (
        _collect_review_placeholder_issues(
            dataset, "metadata/dataset.csv", ("dataset_id",)
        )
        + _collect_review_placeholder_issues(
            tables, "metadata/tables.csv", ("table_id", "file_name")
        )
        + _collect_missing_table_observation_unit_iri_issues(tables)
        + _collect_review_placeholder_issues(
            dictionary,
            "metadata/column_dictionary.csv",
            ("table_id", "column_name"),
        )
        + _collect_review_placeholder_issues(
            codes, "metadata/codes.csv", ("table_id", "column_name", "code_value")
        )
        + _collect_review_iri_issues(tables, "metadata/tables.csv")
        + _collect_review_iri_issues(dictionary, "metadata/column_dictionary.csv")
        + _collect_review_iri_issues(codes, "metadata/codes.csv")
    )


def _collect_placement_iri_issues(
    meta: object,
    source_name: str,
    id_fields,
    fields=("method_iri", "protocol_iri"),
) -> list[str]:
    """Mirror ``.ms_collect_placement_iri_issues``.

    sdp-0.3.0 moved methods and protocols onto ``tables.csv`` and
    ``dataset.csv``, so those fields need the same absolute-IRI check the
    dictionary's IRI columns get. Without it a table could claim
    ``methods/weir-count`` and validate cleanly: the base schema accepts any
    string, and the observation-structure validator that does check IRI shape
    only runs when the optional structure sidecars exist.
    """
    from .sdp_methods import _is_absolute_iri, _is_blank as _placement_blank

    if not isinstance(meta, pd.DataFrame) or len(meta) == 0:
        return []
    messages = []
    for field in fields:
        if field not in meta.columns:
            continue
        for position in range(len(meta)):
            value = meta[field].iloc[position]
            if _placement_blank(value):
                continue
            text = str(value).strip()
            # ``REVIEW:`` markers have their own dedicated reporting path.
            if text.upper().startswith("REVIEW:"):
                continue
            if not _is_absolute_iri(value):
                context = _validation_row_context(meta, position, id_fields)
                messages.append(
                    f"{source_name} {context} field {field} is not an "
                    f"absolute IRI: '{text}'."
                )
    return messages


# The five columns of R's issue tibble, in R's order. Until S10 chunk D only
# the ``columns`` category was populated here and every other finding raised
# at the first structural problem with an untyped string (PARITY.md row 41 /
# hub backlog #91); ``_collect_package_validation_issues`` below now
# accumulates all eight typed categories and the validator aborts once,
# exactly as ``.ms_collect_package_validation_issues()`` does.
_ISSUE_COLUMNS = ["issue_type", "table_id", "column_name", "value", "message"]


def _trimmed_unique(values) -> list[str]:
    """R's ``trimmed_unique()``: trim, drop NA/blank, unique preserving order."""
    out: list[str] = []
    for value in values:
        if pd.isna(value):
            continue
        text = str(value).strip(READR_TRIM_CHARS)
        if text and text not in out:
            out.append(text)
    return out


def _drop_blank(values) -> list[str]:
    """R's ``drop_blank()``: the ``trimmed_unique`` tail for canonical tokens."""
    out: list[str] = []
    for value in values:
        if value is None or (isinstance(value, float) and value != value):
            continue
        if value != "" and value not in out:
            out.append(value)
    return out


def _detect_wide_columns(column_names) -> list[str]:
    """Mirror ``.ms_detect_wide_columns()`` — thresholds exact.

    Column names that look like data values rather than variable names: bare
    year-like names, or a shared stem with numeric suffixes, across **three
    or more** columns (so an ordinary ``x2``/``x3`` pair is not flagged).
    Feeds a warning, never an issue: the SDP may accept untidy data, it must
    simply stop implying it checked (metasalmon 0.2.6).
    """
    names: list[str] = []
    for name in column_names:
        if pd.isna(name):
            continue
        text = str(name).strip(READR_TRIM_CHARS)
        if text:
            names.append(text)
    if len(names) < 3:
        return []

    year_like = [name for name in names if re.match(r"^[Xx]?(19|20)[0-9]{2}$", name)]
    if len(year_like) >= 3:
        return sorted(year_like)

    # A shared stem with numeric tails: count_1998, count_1999, count_2000.
    stems = [re.sub(r"[_.-]?[0-9]+$", "", name) for name in names]
    numeric_tail = [stem != name for stem, name in zip(stems, names)]
    if not any(numeric_tail):
        return []
    tally: dict[str, int] = {}
    for stem, tail in zip(stems, numeric_tail):
        if tail:
            tally[stem] = tally.get(stem, 0) + 1
    repeated = {stem for stem, count in tally.items() if count >= 3}
    if not repeated:
        return []
    return sorted(
        name
        for name, stem, tail in zip(names, stems, numeric_tail)
        if tail and stem in repeated
    )


def _collect_unresolved_placeholders(package: Dict[str, object]) -> list[str]:
    """Mirror ``.ms_collect_unresolved_placeholders()``.

    Metadata fields still holding a ``MISSING METADATA:`` / ``MISSING
    DESCRIPTION:`` / ``REVIEW REQUIRED:`` marker, reported as
    ``file$column`` so a user can go straight to the cell.
    """
    found: list[str] = []
    for file_name, key in (
        ("dataset.csv", "dataset"),
        ("tables.csv", "tables"),
        ("column_dictionary.csv", "dictionary"),
        # The strict path scans codes for the same markers, so omitting it
        # here would leave part of the default-mode behaviour silently
        # conditional on ``require_iris``.
        ("codes.csv", "codes"),
    ):
        frame = package.get(key)
        if not isinstance(frame, pd.DataFrame) or len(frame) == 0:
            continue
        for column in frame.columns:
            if any(is_review_placeholder(value) for value in frame[column]):
                entry = f"{file_name}${column}"
                if entry not in found:
                    found.append(entry)
    return sorted(found)


def _nonempty_text_values(value) -> list[str]:
    """R's ``.ms_nonempty_text_values()``: flatten, trim, drop blanks, unique."""
    flat: list = []

    def _flatten(item):
        if isinstance(item, (list, tuple, set, pd.Series)):
            for element in item:
                _flatten(element)
        elif isinstance(item, dict):
            for element in item.values():
                _flatten(element)
        else:
            flat.append(item)

    _flatten(value)
    out: list[str] = []
    for item in flat:
        if item is None or pd.isna(item):
            continue
        text = str(item).strip(READR_TRIM_CHARS)
        if text and text not in out:
            out.append(text)
    return out


def _collect_composite_hint_values(
    dataset_meta,
    table_meta,
    datapackage_path,
    hint_fields,
    optional_hint_fields=(),
) -> list[dict]:
    """Mirror ``.ms_collect_composite_hint_values()``: (source, field, value)
    triples from dataset.csv, tables.csv, and the descriptor (top level and
    per resource), de-duplicated preserving first occurrence."""
    all_fields = list(dict.fromkeys(list(hint_fields) + list(optional_hint_fields)))
    rows: list[dict] = []

    def add(source, field, values):
        for value in values:
            entry = {"source": source, "field": field, "value": value}
            if entry not in rows:
                rows.append(entry)

    def collect_from_frame(frame, source_label):
        if not isinstance(frame, pd.DataFrame) or len(frame) == 0:
            return
        for field in [column for column in frame.columns if column in all_fields]:
            add(source_label, field, _nonempty_text_values(frame[field]))

    collect_from_frame(dataset_meta, "dataset.csv")
    collect_from_frame(table_meta, "tables.csv")

    if datapackage_path:
        descriptor_path = Path(datapackage_path)
        datapackage = None
        if descriptor_path.exists():
            try:
                with descriptor_path.open("r", encoding="utf-8") as fp:
                    datapackage = json.load(fp)
            except (OSError, ValueError):
                datapackage = None
        if isinstance(datapackage, dict):
            for field in all_fields:
                if datapackage.get(field) is not None:
                    add(
                        "datapackage",
                        field,
                        _nonempty_text_values(datapackage[field]),
                    )
            for resource in datapackage.get("resources") or []:
                if not isinstance(resource, dict):
                    continue
                resource_name = resource.get("name") or "<unnamed_resource>"
                for field in all_fields:
                    if resource.get(field) is not None:
                        add(
                            f"datapackage_resource:{resource_name}",
                            field,
                            _nonempty_text_values(resource[field]),
                        )
    return rows


def _values_indicate_composite_intent(values) -> bool:
    """R's ``.ms_values_indicate_composite_intent()``: substring, any case."""
    return any("composite" in str(value).lower() for value in values)


def _column_has_populated_values(series: pd.Series) -> bool:
    """Mirror ``.ms_column_has_populated_values()``."""
    values = series.dropna()
    if len(values) == 0:
        return False
    if (
        pd.api.types.is_object_dtype(values)
        or pd.api.types.is_string_dtype(values)
        or isinstance(values.dtype, pd.CategoricalDtype)
    ):
        return any(str(value).strip(READR_TRIM_CHARS) for value in values)
    return True


def _detect_wsp_composite_signal(resources) -> dict:
    """Mirror ``.ms_detect_wsp_composite_signal()``."""
    required_columns = ["SPN_ABD_WILD", "SPN_TREND_WILD", "RAPID_STATUS"]
    resources = resources or {}
    matches = [name for name in resources if str(name).lower() == "cu_timeseries"]
    if not matches:
        return {
            "cu_timeseries_present": False,
            "required_columns": required_columns,
            "populated_columns": [],
            "any_populated": False,
        }
    frame = resources[matches[0]]
    present = [column for column in required_columns if column in frame.columns]
    populated = [
        column for column in present if _column_has_populated_values(frame[column])
    ]
    return {
        "cu_timeseries_present": True,
        "required_columns": required_columns,
        "populated_columns": populated,
        "any_populated": len(populated) > 0,
    }


def _collect_package_validation_issues(
    package: Dict[str, object],
    path: Optional[Union[str, Path]] = None,
    require_iris: bool = False,
) -> pd.DataFrame:
    """Mirror ``.ms_collect_package_validation_issues()`` — the typed,
    accumulate-then-report collector behind ``validate_salmon_datapackage()``.

    Every finding is tagged with one of R's eight ``issue_type`` values —
    ``dataset``, ``tables``, ``dictionary``, ``codes``, ``resource``,
    ``columns``, ``primary_key``, ``composite_intent`` — and all findings are
    collected before the caller aborts once (hub backlog #91 / PARITY.md
    row 41). Issue messages are byte-identical to R's, verified by
    differential fixtures in ``tests/test_validation_hardening.py``.

    Two warnings fire during collection, mirroring metasalmon 0.2.6's tidy
    checks: unresolved metadata placeholders (default mode only — the strict
    path reports them as errors instead) and column names that look like
    data values (a warning in both modes, never an issue).
    """
    issues: list[dict] = []

    def add_issue(issue_type, message, table_id=None, column_name=None, value=None):
        issues.append(
            {
                "issue_type": issue_type,
                "table_id": table_id,
                "column_name": column_name,
                "value": value,
                "message": message,
            }
        )

    dataset = package.get("dataset")
    tables = package.get("tables")
    dictionary = package.get("dictionary")
    codes = package.get("codes")
    resources = package.get("resources") or {}

    dataset_rows = len(dataset) if isinstance(dataset, pd.DataFrame) else 0
    if dataset_rows != 1:
        add_issue(
            "dataset",
            f"dataset.csv should contain exactly one row; found {dataset_rows}.",
        )
    if not isinstance(tables, pd.DataFrame) or len(tables) == 0:
        add_issue("tables", "No rows found in tables.csv.")

    # Tidy check 3 (metasalmon 0.2.6): surface ``MISSING METADATA:`` markers
    # in the *default* mode. The strict path already reports these as errors,
    # so this adds only the missing half — an ordinary call previously
    # returned zero issues and said nothing, letting a package look clean
    # while stating in its own metadata that its metadata is missing. No
    # issue is raised here; the strict path stays the single error channel.
    if not require_iris:
        placeholder_fields = _collect_unresolved_placeholders(package)
        if placeholder_fields:
            count = len(placeholder_fields)
            warnings.warn(
                f"{count} metadata field{'' if count == 1 else 's'} still "
                f"hold{'s' if count == 1 else ''} a placeholder: "
                + ", ".join(placeholder_fields[:6])
                + ". Replace them before publication; require_iris=True "
                "reports these as errors.",
                UserWarning,
                stacklevel=3,
            )
    if not isinstance(dictionary, pd.DataFrame) or len(dictionary) == 0:
        add_issue("dictionary", "No rows found in column_dictionary.csv.")

    if isinstance(tables, pd.DataFrame) and "table_id" in tables.columns:
        seen: list = []
        dup_tables: list[str] = []
        for value in tables["table_id"]:
            key = None if pd.isna(value) else str(value)
            if key in seen:
                if (
                    key is not None
                    and key.strip(READR_TRIM_CHARS)
                    and key not in dup_tables
                ):
                    dup_tables.append(key)
            else:
                seen.append(key)
        if dup_tables:
            add_issue(
                "tables",
                "Duplicate table_id values in tables.csv: "
                + ", ".join(dup_tables)
                + ".",
            )

    table_ids = (
        _trimmed_unique(tables["table_id"])
        if isinstance(tables, pd.DataFrame) and "table_id" in tables.columns
        else []
    )
    dict_table_ids = (
        _trimmed_unique(dictionary["table_id"])
        if isinstance(dictionary, pd.DataFrame) and "table_id" in dictionary.columns
        else []
    )
    extra_dict_tables = [t for t in dict_table_ids if t not in table_ids]
    if extra_dict_tables:
        add_issue(
            "dictionary",
            "column_dictionary.csv references table_id values not present in "
            "tables.csv: " + ", ".join(extra_dict_tables) + ".",
        )

    if isinstance(codes, pd.DataFrame) and len(codes) > 0:
        code_table_ids = _trimmed_unique(codes["table_id"])
        extra_code_tables = [t for t in code_table_ids if t not in table_ids]
        if extra_code_tables:
            add_issue(
                "codes",
                "codes.csv references table_id values not present in "
                "tables.csv: " + ", ".join(extra_code_tables) + ".",
            )

    n_table_rows = len(tables) if isinstance(tables, pd.DataFrame) else 0
    for position in range(n_table_rows):
        table_id = (
            scalar_text(tables["table_id"].iloc[position])
            if "table_id" in tables.columns
            else ""
        )
        if not table_id:
            continue

        file_name = (
            scalar_text(tables["file_name"].iloc[position])
            if "file_name" in tables.columns
            else ""
        )
        if table_id not in resources:
            add_issue(
                "resource",
                f"Table '{table_id}' points to resource '{file_name}', but "
                "that file could not be loaded.",
                table_id=table_id,
            )
            continue

        table_dict = (
            dictionary[dictionary["table_id"] == table_id]
            if isinstance(dictionary, pd.DataFrame)
            and "table_id" in dictionary.columns
            else pd.DataFrame(columns=["column_name", "value_type"])
        )
        dict_cols = (
            _trimmed_unique(table_dict["column_name"])
            if "column_name" in table_dict.columns
            else []
        )
        if not dict_cols:
            add_issue(
                "dictionary",
                f"No dictionary rows found for table '{table_id}'.",
                table_id=table_id,
            )
            continue

        data_df = resources[table_id]
        data_cols = [str(column) for column in data_df.columns]

        # Tidy check 1 (metasalmon 0.2.6): a declared primary key must
        # actually identify a row. The field was declared in tables.csv and
        # read by nothing that tested it, so a table could claim a key and
        # ship duplicates. Skipped, as in R, when the table_id row is not
        # unique — the duplicate-table_id issue already covers that state.
        matching = tables[tables["table_id"] == table_id]
        if len(matching) == 1 and "primary_key" in matching.columns:
            key_text = scalar_text(matching["primary_key"].iloc[0])
            key_cols = [
                part.strip(READR_TRIM_CHARS) for part in re.split(r"[,;|]", key_text)
            ]
            key_cols = [part for part in key_cols if part]
            present = list(
                dict.fromkeys(part for part in key_cols if part in data_cols)
            )
            if key_cols and len(present) == len(key_cols):
                # A missing component is as fatal as a duplicate: the row has
                # no identity at all. Checked separately because the key join
                # renders a missing value as text, which is unlikely to
                # collide and so would pass the duplicate test while
                # identifying nothing.
                missing_key = []
                for column in present:
                    for value in data_df[column]:
                        if pd.isna(value) or not str(value).strip(READR_TRIM_CHARS):
                            missing_key.append(column)
                            break
                if missing_key:
                    n_missing = len(missing_key)
                    add_issue(
                        "tables",
                        f"Table '{table_id}' declares primary key "
                        f"'{', '.join(key_cols)}' but "
                        f"column{'' if n_missing == 1 else 's'} "
                        f"{', '.join(missing_key)} "
                        f"contain{'s' if n_missing == 1 else ''} missing values.",
                        table_id=table_id,
                    )

                key_values = [
                    "\r".join(str(data_df[column].iloc[i]) for column in present)
                    for i in range(len(data_df))
                ]
                seen_keys: set = set()
                duplicated_keys: list[str] = []
                for value in key_values:
                    if value in seen_keys:
                        if value not in duplicated_keys:
                            duplicated_keys.append(value)
                    else:
                        seen_keys.add(value)
                if duplicated_keys:
                    n_dup = len(duplicated_keys)
                    add_issue(
                        "tables",
                        f"Table '{table_id}' declares primary key "
                        f"'{', '.join(key_cols)}' but {n_dup} "
                        f"row{'' if n_dup == 1 else 's'} "
                        f"repeat{'s' if n_dup == 1 else ''} it.",
                        table_id=table_id,
                    )

        # Tidy check 2 (metasalmon 0.2.6): column names that look like
        # values. A warning, never an issue — the SDP accepts untidy data,
        # it just stops implying it checked. R points at
        # tidyr::pivot_longer(); the pandas counterpart is melt().
        wide_cols = _detect_wide_columns(data_cols)
        if wide_cols:
            n_wide = len(wide_cols)
            warnings.warn(
                f"Table '{table_id}' may not be tidy: {n_wide} column "
                f"name{'' if n_wide == 1 else 's'} "
                f"look{'s' if n_wide == 1 else ''} like data values: "
                + ", ".join(wide_cols[:6])
                + ". Tidy data puts each variable in a column and each "
                "observation in a row. Consider pandas.melt() before "
                "packaging.",
                UserWarning,
                stacklevel=3,
            )

        # Values that do not satisfy their declared ``value_type``. The
        # reader keeps the raw token rather than NA-ing it, so the code-value
        # check below still sees the offending value; this reports the
        # declaration mismatch itself.
        for mismatch in data_df.attrs.get("ms_value_type_mismatches", []):
            examples = ", ".join(str(example) for example in mismatch["examples"])
            count = mismatch["count"]
            add_issue(
                "columns",
                f"Table '{table_id}' column '{mismatch['column']}' declares "
                f"value_type '{mismatch['declared']}' but {count} "
                f"value{'' if count == 1 else 's'} did not satisfy it "
                f"({mismatch['reason']}): {examples}.",
                table_id=table_id,
                column_name=mismatch["column"],
                value=examples,
            )

        missing_in_data = [c for c in dict_cols if c not in data_cols]
        if missing_in_data:
            add_issue(
                "columns",
                f"Table '{table_id}' is missing dictionary columns in data: "
                + ", ".join(missing_in_data)
                + ".",
                table_id=table_id,
                column_name=", ".join(missing_in_data),
            )

        extra_in_data = list(
            dict.fromkeys(c for c in data_cols if c not in dict_cols)
        )
        if extra_in_data:
            add_issue(
                "columns",
                f"Table '{table_id}' has data columns not listed in "
                "column_dictionary.csv: " + ", ".join(extra_in_data) + ".",
                table_id=table_id,
                column_name=", ".join(extra_in_data),
            )

        primary_key = (
            scalar_text(matching["primary_key"].iloc[0])
            if "primary_key" in matching.columns and len(matching)
            else ""
        )
        if primary_key:
            pk_cols = [
                part.strip(READR_TRIM_CHARS) for part in primary_key.split(",")
            ]
            pk_cols = [part for part in pk_cols if part]
            missing_pk = list(
                dict.fromkeys(part for part in pk_cols if part not in data_cols)
            )
            if missing_pk:
                add_issue(
                    "primary_key",
                    f"Table '{table_id}' primary_key references columns not "
                    "present in data: " + ", ".join(missing_pk) + ".",
                    table_id=table_id,
                    column_name=", ".join(missing_pk),
                )

        if isinstance(codes, pd.DataFrame) and len(codes) > 0:
            table_codes = codes[codes["table_id"] == table_id]
            code_columns = _trimmed_unique(table_codes["column_name"])

            for column_name in code_columns:
                if column_name not in dict_cols:
                    add_issue(
                        "codes",
                        f"codes.csv references table '{table_id}' column "
                        f"'{column_name}', but that column is not in "
                        "column_dictionary.csv.",
                        table_id=table_id,
                        column_name=column_name,
                    )

                if column_name not in data_cols:
                    add_issue(
                        "codes",
                        f"codes.csv references table '{table_id}' column "
                        f"'{column_name}', but that column is not present in "
                        "data.",
                        table_id=table_id,
                        column_name=column_name,
                    )
                    continue

                # Canonicalize both sides through the declared type. The data
                # column is a parsed vector and ``code_value`` is always raw
                # CSV text, so comparing raw text of each made a package fail
                # against its own codes.
                dict_names = [
                    str(value).strip(READR_TRIM_CHARS)
                    for value in table_dict["column_name"]
                ]
                column_value_type = (
                    table_dict["value_type"].iloc[dict_names.index(column_name)]
                    if column_name in dict_names
                    else None
                )
                raw_code_values = list(
                    table_codes["code_value"][
                        table_codes["column_name"] == column_name
                    ]
                )
                # The data resource is fidelity-checked when it is read, but
                # code values are raw text that never passes through that
                # path. Without the same check, a code token carrying more
                # precision than its declared type can hold canonicalizes
                # onto a different data value and the comparison silently
                # succeeds.
                code_outcome = convert_declared_tokens(
                    raw_code_values, column_value_type
                )
                if code_outcome.reason is not None:
                    offenders = list(
                        dict.fromkeys(
                            str(offender) for offender in code_outcome.offenders
                        )
                    )[:3]
                    n_bad = len(code_outcome.offenders)
                    add_issue(
                        "codes",
                        f"Table '{table_id}' column '{column_name}' declares "
                        f"value_type '{column_value_type}' but {n_bad} "
                        f"codes.csv value{'' if n_bad == 1 else 's'} did not "
                        f"satisfy it ({code_outcome.reason}): "
                        + ", ".join(offenders)
                        + ".",
                        table_id=table_id,
                        column_name=column_name,
                    )
                data_values = _drop_blank(
                    canonical_value_tokens(
                        list(data_df[column_name]), column_value_type
                    )
                )
                code_values = _drop_blank(
                    canonical_value_tokens(raw_code_values, column_value_type)
                )
                missing_code_values = [
                    value for value in data_values if value not in code_values
                ]
                if missing_code_values:
                    add_issue(
                        "codes",
                        f"Table '{table_id}' column '{column_name}' has data "
                        "values not listed in codes.csv: "
                        + ", ".join(missing_code_values)
                        + ".",
                        table_id=table_id,
                        column_name=column_name,
                        value=", ".join(missing_code_values),
                    )

    composite_hints = _collect_composite_hint_values(
        dataset_meta=dataset,
        table_meta=tables,
        datapackage_path=(
            Path(path) / "datapackage.json" if path is not None else None
        ),
        hint_fields=("route", "route_key", "upload_route", "data_level"),
        optional_hint_fields=("source_name",),
    )
    if _values_indicate_composite_intent(
        [hint["value"] for hint in composite_hints]
    ):
        wsp_signal = _detect_wsp_composite_signal(resources)
        if not wsp_signal["any_populated"]:
            hint_fields_detected = ", ".join(
                dict.fromkeys(hint["field"] for hint in composite_hints)
            )
            hint_values_detected = ", ".join(
                dict.fromkeys(hint["value"] for hint in composite_hints)
            )
            required = ", ".join(wsp_signal["required_columns"])
            add_issue(
                "composite_intent",
                "Explicit composite route intent detected in "
                f"{hint_fields_detected} ({hint_values_detected}), but no "
                "populated WSP composite signal columns were found in "
                f"cu_timeseries. Populate at least one of: {required}.",
                table_id="cu_timeseries",
                column_name=required,
                value=hint_values_detected,
            )

    return pd.DataFrame(issues, columns=_ISSUE_COLUMNS)


def _abort_package_validation_issues(issues: pd.DataFrame) -> None:
    """Mirror ``.ms_abort_package_validation_issues()``.

    One abort naming the total, previewing up to ten messages, and — a
    Python-side affordance R's cli abort cannot offer — carrying the full
    typed frame on the raised error as ``.issues``.
    """
    total = len(issues)
    preview_n = min(10, total)
    lines = [
        f"Salmon Data Package validation failed with {total} structural "
        f"issue{'' if total == 1 else 's'}."
    ]
    lines.extend(str(message) for message in issues["message"].iloc[:preview_n])
    if total > preview_n:
        remaining = total - preview_n
        lines.append(f"{remaining} more issue{'' if remaining == 1 else 's'} not shown.")
    error = ValueError("\n".join(lines))
    error.issues = issues
    raise error


def validate_salmon_datapackage(
    path: Union[str, Path],
    require_iris: bool = False,
) -> Dict[str, object]:
    """Validate package structure, ID alignment, and semantic review state.

    Mirrors ``validate_salmon_datapackage()`` in metasalmon: every structural
    finding is collected into one typed issue frame (eight ``issue_type``
    categories, five columns) and reported in a single raise whose ``.issues``
    attribute carries the frame — never one untyped error at the first
    problem (hub backlog #91 / PARITY.md row 41, converged at S10 chunk D).
    The returned ``issues`` frame is therefore empty whenever the call
    returns, exactly as in R.
    """
    target = Path(path)
    package = read_salmon_datapackage(target)
    dataset = package["dataset"]
    tables = package["tables"]
    dictionary = package["dictionary"]
    codes = package["codes"]

    dataset_ids = {
        str(value)
        for frame in (dataset, tables, dictionary, codes)
        if isinstance(frame, pd.DataFrame) and "dataset_id" in frame
        for value in frame["dataset_id"].dropna()
        if str(value).strip()
    }
    if len(dataset_ids) > 1:
        raise ValueError(
            f"Dataset IDs are not aligned across package metadata: "
            f"{sorted(dataset_ids)}"
        )

    issues = _collect_package_validation_issues(
        package, path=target, require_iris=require_iris
    )
    if len(issues) > 0:
        _abort_package_validation_issues(issues)

    # SDP procedure and observation-structure resources are optional. Their
    # absence preserves the historic validation path exactly; when present,
    # validate the canonical files and their data-level bindings before the
    # semantic checks, as metasalmon does.
    from .observation_structures import validate_optional_sdp_observation_metadata

    validate_optional_sdp_observation_metadata(target)

    if require_iris:
        final_review_issues = (
            _collect_review_placeholder_issues(
                dataset, "metadata/dataset.csv", ("dataset_id",)
            )
            + _collect_review_placeholder_issues(
                tables, "metadata/tables.csv", ("table_id", "file_name")
            )
            + _collect_missing_table_observation_unit_iri_issues(tables)
            + _collect_review_placeholder_issues(
                dictionary,
                "metadata/column_dictionary.csv",
                ("table_id", "column_name"),
            )
            + _collect_review_placeholder_issues(
                codes,
                "metadata/codes.csv",
                ("table_id", "column_name", "code_value"),
            )
        )
    else:
        final_review_issues = []

    normalized = validate_dictionary(
        dictionary,
        require_iris=require_iris,
    )
    package["dictionary"] = normalized
    from .validation import validate_semantics

    semantic_validation = validate_semantics(
        normalized,
        require_iris=require_iris,
    )

    table_review_issues = _collect_review_iri_issues(
        tables, source_name="metadata/tables.csv"
    )
    # Unconditional: a method or protocol placement that is not an absolute
    # IRI is malformed in every validation mode, not only under
    # ``require_iris`` — exactly as in ``.ms_validate_salmon_datapackage()``.
    placement_issues = _collect_placement_iri_issues(
        tables,
        source_name="metadata/tables.csv",
        id_fields=("table_id", "file_name"),
    ) + _collect_placement_iri_issues(
        dataset,
        source_name="metadata/dataset.csv",
        id_fields=("dataset_id",),
        fields=("protocol_iri",),
    )
    appended_semantic_issues = table_review_issues + placement_issues
    if appended_semantic_issues:
        issue_frame = pd.DataFrame({"message": appended_semantic_issues})
        existing = semantic_validation.get("issues")
        if isinstance(existing, pd.DataFrame) and len(existing) > 0:
            issue_frame = pd.concat([existing, issue_frame], ignore_index=True)
        semantic_validation["issues"] = issue_frame

    if require_iris:
        # A malformed placement IRI is worse than an unreviewed one: strict
        # validation must block it, exactly as it blocks a REVIEW: marker.
        final_review_issues = (
            final_review_issues + table_review_issues + placement_issues
        )
        if final_review_issues:
            total = len(final_review_issues)
            preview = list(dict.fromkeys(final_review_issues))[:10]
            lines = [
                f"Final validation failed with {total} unresolved review "
                f"issue{'' if total == 1 else 's'}."
            ]
            lines.extend(preview)
            lines.append(
                "Resolve placeholder metadata, blank table observation-unit "
                "IRIs, and any REVIEW-prefixed IRIs before strict validation."
            )
            if total > len(preview):
                remaining = total - len(preview)
                lines.append(
                    f"{remaining} more unresolved review "
                    f"issue{'' if remaining == 1 else 's'} not shown."
                )
            raise ValueError("\n".join(lines))

    sem_issues = semantic_validation.get("issues")
    if isinstance(sem_issues, pd.DataFrame) and len(sem_issues) > 0:
        total = len(sem_issues)
        preview = list(
            dict.fromkeys(str(message) for message in sem_issues["message"])
        )[:3]
        lines = [
            f"Package structure is valid, but validate_semantics() reported "
            f"{total} semantic issue{'' if total == 1 else 's'}."
        ]
        lines.extend("- " + message for message in preview)
        if total > len(preview):
            remaining = total - len(preview)
            lines.append(
                f"{remaining} more semantic issue{'' if remaining == 1 else 's'} "
                "returned in the result."
            )
        warnings.warn("\n".join(lines), UserWarning, stacklevel=2)

    return {
        "package": package,
        "semantic_validation": semantic_validation,
        "issues": issues,
    }


__all__ = [
    "create_sdp",
    "create_salmon_datapackage",
    "create_salmon_datapackage_from_data",
    "infer_salmon_datapackage_artifacts",
    "read_salmon_datapackage",
    "validate_salmon_datapackage",
    "write_salmon_datapackage",
]
