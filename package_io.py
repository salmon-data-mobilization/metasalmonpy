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
    ensure_resource_mapping,
    infer_codes_from_resources,
    infer_dataset_metadata_from_resources,
    infer_table_metadata_from_resources,
    normalize_codes,
    normalize_dataset_meta,
    normalize_dictionary,
    normalize_table_meta,
    parse_logical,
    read_sdp_csv,
    READR_TRIM_CHARS,
)
from .resource_types import (
    VALUE_TYPES,
    canonical_value_tokens,
    convert_declared_tokens,
    render_resource_frame,
    typed_series,
    value_type_mismatch_record,
)
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


def _write_metadata_csv(df: pd.DataFrame, path: Path) -> None:
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
                "" if value is pd.NA or value is None else ("TRUE" if value else "FALSE")
                for value in series
            ]
    out.to_csv(path, index=False, na_rep="")


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


def _prepare_package_dir(
    target: Path,
    overwrite: bool,
    managed_paths=None,
    prune: bool = False,
) -> None:
    if prune and not overwrite:
        raise ValueError("prune=True requires overwrite=True.")
    if not target.exists():
        target.mkdir(parents=True, exist_ok=True)
        return
    entries = list(target.iterdir())
    if not entries:
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
    if prune:
        for child in entries:
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        return

    managed_paths = list(managed_paths or [])
    _assert_managed_paths_contained(target, managed_paths)
    for candidate in managed_paths:
        # No recursive removal: if a managed path ever resolves to a directory
        # this is a no-op rather than a recursive wipe.
        if candidate.is_symlink() or (candidate.exists() and candidate.is_file()):
            candidate.unlink()


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
    dataset_meta = dataset_meta.copy()
    table_meta = table_meta.copy()
    dictionary = dictionary.copy()

    for column, label in (
        ("title", "dataset title"),
        ("description", "dataset description"),
    ):
        if column in dataset_meta:
            missing = dataset_meta[column].isna() | (
                dataset_meta[column].astype(str).str.strip() == ""
            )
            dataset_meta.loc[missing, column] = f"MISSING METADATA: {label}"

    for column, label in (
        ("description", "table description"),
        ("observation_unit", "table observation unit"),
    ):
        if column in table_meta:
            missing = table_meta[column].isna() | (
                table_meta[column].astype(str).str.strip() == ""
            )
            table_meta.loc[missing, column] = f"MISSING METADATA: {label}"

    # metasalmon v0.1.7 stopped defaulting a blank spec_version to the frozen
    # literal "sdp-0.1.0" and started taking it from the vendored profile rules,
    # so a package never claims an older profile than the one it was written to.
    if "spec_version" in dataset_meta:
        missing = dataset_meta["spec_version"].isna() | (
            dataset_meta["spec_version"].astype(str).str.strip() == ""
        )
        dataset_meta.loc[missing, "spec_version"] = load_sdp_schema(quiet=True)["version"]

    if "column_description" in dictionary:
        missing = dictionary["column_description"].isna() | (
            dictionary["column_description"].astype(str).str.strip() == ""
        )
        dictionary.loc[missing, "column_description"] = dictionary.loc[
            missing, "column_name"
        ].map(lambda value: f"MISSING DESCRIPTION: {value}")

    return dataset_meta, table_meta, dictionary


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

    _prepare_package_dir(
        target, overwrite=overwrite, managed_paths=managed_paths, prune=prune
    )
    if not prune and orphaned:
        warnings.warn(
            "Removed data resource(s) no longer declared in tables.csv: "
            + ", ".join(sorted(orphaned)),
            UserWarning,
            stacklevel=2,
        )
    (target / "metadata").mkdir(parents=True, exist_ok=True)
    (target / "data").mkdir(parents=True, exist_ok=True)

    dataset_id = dataset_meta["dataset_id"].iloc[0]

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
        file_path = target / file_name
        file_path.parent.mkdir(parents=True, exist_ok=True)
        # Typed columns are rendered canonically rather than handed to
        # ``to_csv``'s repr: a float 100000.0 would otherwise be written as
        # "100000.0" and a logical as "True", so a package read by
        # ``read_salmon_datapackage()`` and written straight back would not
        # reproduce its own bytes. See ``resource_types.render_resource_frame``
        # for the one deliberate difference from ``readr::write_csv``.
        render_resource_frame(resource_df).to_csv(file_path, index=False)

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
                "title": _clean(row.get("column_label")),
                "type": _clean(row["value_type"]),
                "description": _clean(row["column_description"]),
            }
            if not _has_value(row.get("column_label")):
                field.pop("title")
            if bool(row.get("required")) is True:
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
        if not _is_review_value(license_value):
            datapackage["licenses"] = [_license_descriptor(license_value)]
    # ``_has_value`` rather than ``pd.notna``: an empty ``temporal_start``
    # is not missing to pandas, so a descriptor carried ``"temporal": {"start":
    # "", "end": ""}``. R has always tested both conditions.
    if "temporal_start" in dataset_meta and _has_value(dataset_meta["temporal_start"].iloc[0]):
        datapackage["temporal"] = {"start": _clean(dataset_meta["temporal_start"].iloc[0])}
        if "temporal_end" in dataset_meta and _has_value(dataset_meta["temporal_end"].iloc[0]):
            datapackage["temporal"]["end"] = _clean(dataset_meta["temporal_end"].iloc[0])

    if write_datapackage:
        with (target / "datapackage.json").open("w", encoding="utf-8") as fp:
            json.dump(datapackage, fp, indent=2)
            # ``jsonlite::write_json`` terminates the file; ``json.dump`` does
            # not, and that single byte was the last difference between an
            # R-written and a Python-written descriptor for the same package.
            fp.write("\n")

    _write_metadata_csv(dataset_meta, target / "metadata" / "dataset.csv")
    _write_metadata_csv(table_meta, target / "metadata" / "tables.csv")
    _write_metadata_csv(
        dict_valid,
        target / "metadata" / "column_dictionary.csv",
    )
    if codes is not None:
        _write_metadata_csv(codes, target / "metadata" / "codes.csv")
    (target / PACKAGE_SENTINEL).write_text("metasalmonpy-owned\n", encoding="utf-8")

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
            na_rep="",
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


def _collect_review_issues(package: Dict[str, object]) -> list[str]:
    issues = []
    tables = package["tables"]
    if isinstance(tables, pd.DataFrame):
        for idx, row in tables.iterrows():
            if not _has_value(row.get("observation_unit_iri")):
                issues.append(
                    f"metadata/tables.csv row {idx + 1} has a blank "
                    "observation_unit_iri."
                )

    for key, file_name in (
        ("dataset", "metadata/dataset.csv"),
        ("tables", "metadata/tables.csv"),
        ("dictionary", "metadata/column_dictionary.csv"),
        ("codes", "metadata/codes.csv"),
    ):
        frame = package.get(key)
        if not isinstance(frame, pd.DataFrame):
            continue
        for column in frame.columns:
            for idx, value in frame[column].items():
                if _is_review_value(value):
                    issues.append(
                        f"{file_name} row {idx + 1} field {column} "
                        f"contains unresolved review value {value!r}."
                    )
    return issues


def _validation_row_context(frame: pd.DataFrame, position: int, id_fields) -> str:
    """Mirror ``.ms_validation_row_context``: ``row N (field=value, ...)``."""
    bits = []
    for field in id_fields:
        if field not in frame.columns:
            continue
        value = frame[field].iloc[position]
        if _has_value(value):
            bits.append(f"{field}={value}")
    if not bits:
        return f"row {position + 1}"
    return f"row {position + 1} ({', '.join(bits)})"


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


# The ``issues`` frame this validator returns was an unconditionally empty
# ``DataFrame(columns=["message"])`` until the 0.2.0 rung — every finding was
# raised instead. The typed reader needs a place to *report* rather than
# raise: a value that does not satisfy its declared ``value_type`` keeps its
# raw token and the package stays readable, so the mismatch is a structured
# issue exactly as it is in ``.ms_validate_salmon_datapackage()``. The columns
# match R's issue tibble; only the ``columns`` category is populated here,
# because the remaining categories R reports have no Python counterpart yet.
_ISSUE_COLUMNS = ["issue_type", "table_id", "column_name", "value", "message"]


def _value_type_issues(package: Dict[str, object]) -> pd.DataFrame:
    """Structured issues for every declared type the data did not satisfy."""
    rows = []
    resources = package.get("resources") or {}
    for table_id, frame in resources.items():
        if not isinstance(frame, pd.DataFrame):
            continue
        for mismatch in frame.attrs.get("ms_value_type_mismatches", []):
            examples = ", ".join(mismatch["examples"])
            plural = "" if mismatch["count"] == 1 else "s"
            rows.append(
                {
                    "issue_type": "columns",
                    "message": (
                        f"Table {table_id!r} column {mismatch['column']!r} declares "
                        f"value_type {mismatch['declared']!r} but {mismatch['count']} "
                        f"value{plural} did not satisfy it ({mismatch['reason']}): "
                        f"{examples}."
                    ),
                    "table_id": table_id,
                    "column_name": mismatch["column"],
                    "value": examples,
                }
            )
    return pd.DataFrame(rows, columns=_ISSUE_COLUMNS)


def validate_salmon_datapackage(
    path: Union[str, Path],
    require_iris: bool = False,
) -> Dict[str, object]:
    """Validate package structure, ID alignment, and semantic review state."""
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

    for _, row in tables.iterrows():
        table_id_value = str(row.get("table_id") or "")
        if table_id_value not in package["resources"]:
            raise ValueError(
                f"Resource file is missing for table {table_id_value!r}."
            )
        expected = set(
            dictionary.loc[
                dictionary["table_id"].astype(str) == table_id_value,
                "column_name",
            ].astype(str)
        )
        actual = set(package["resources"][table_id_value].columns.astype(str))
        if expected != actual:
            raise ValueError(
                f"Resource columns for table {table_id_value!r} do not match "
                f"the dictionary: expected {sorted(expected)}, got "
                f"{sorted(actual)}."
            )

    # SDP procedure and observation-structure resources are optional. Their
    # absence preserves the historic validation path exactly; when present,
    # validate the canonical files and their data-level bindings before the
    # semantic checks, as metasalmon does.
    from .observation_structures import validate_optional_sdp_observation_metadata

    validate_optional_sdp_observation_metadata(target)

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
    if placement_issues:
        issue_frame = pd.DataFrame({"message": placement_issues})
        existing = semantic_validation.get("issues")
        if isinstance(existing, pd.DataFrame) and len(existing) > 0:
            issue_frame = pd.concat([existing, issue_frame], ignore_index=True)
        semantic_validation["issues"] = issue_frame

    if require_iris:
        # A malformed placement IRI is worse than an unreviewed one: strict
        # validation must block it, exactly as it blocks a REVIEW: marker.
        review_issues = _collect_review_issues(package) + placement_issues
        if review_issues:
            preview = " ".join(review_issues[:5])
            raise ValueError(
                f"Final validation failed with {len(review_issues)} unresolved "
                f"review issue(s). {preview}"
            )

    return {
        "package": package,
        "semantic_validation": semantic_validation,
        "issues": _value_type_issues(package),
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
