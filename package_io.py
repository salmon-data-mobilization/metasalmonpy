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
    SDP_PROFILE_VERSION,
)
from .sdp_schema import (
    SDP_PROFILE_URL as _SDP_PROFILE_URL,
    SDP_RULES_URL as _SDP_RULES_URL,
)

# Canonical, publicly resolvable SDP 0.2 contract identifiers. These are
# values stamped into ``datapackage.json``; nothing here ever fetches them.
# metasalmon corrected the same constants from the retired
# ``dfo-pacific-science`` organization at v0.1.8, and the vendored bundle under
# ``data/`` now carries the matching ``$id``/``profile`` fields.
SDP_PROFILE_URL = _SDP_PROFILE_URL
SDP_RULES_URL = _SDP_RULES_URL
PACKAGE_SENTINEL = ".metasalmonpy-package"


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
    df.to_csv(path, index=False, na_rep="")


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


def _prepare_package_dir(target: Path, overwrite: bool) -> None:
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
    for child in entries:
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


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
    entries = [
        ("sdp_dataset", "metadata/dataset.csv", "Dataset metadata"),
        ("sdp_tables", "metadata/tables.csv", "Table metadata"),
        (
            "sdp_column_dictionary",
            "metadata/column_dictionary.csv",
            "Column dictionary",
        ),
    ]
    if include_codes:
        entries.append(("sdp_codes", "metadata/codes.csv", "Code metadata"))
    return [
        {
            "profile": "tabular-data-resource",
            "name": name,
            "path": path,
            "title": title,
        }
        for name, path, title in entries
    ]


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
        dataset_meta.loc[missing, "spec_version"] = SDP_PROFILE_VERSION

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
) -> Path:
    """
    Write the canonical Salmon Data Package layout.

    Metadata is written under ``metadata/``, table resources under ``data/``,
    and the Frictionless descriptor at the package root.
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
    _prepare_package_dir(target, overwrite=overwrite)
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
        resource_df.to_csv(file_path, index=False)

        table_dict = dict_valid[
            (dict_valid["dataset_id"] == dataset_id) & (dict_valid["table_id"] == resource_name)
        ]
        fields = []
        for _, row in table_dict.iterrows():
            field = {
                "name": _clean(row["column_name"]),
                "type": _clean(row["value_type"]),
                "description": _clean(row["column_description"]),
            }
            if _has_value(row.get("column_label")) and row.get("column_label") != row.get("column_name"):
                field["title"] = _clean(row.get("column_label"))
            if _has_value(row.get("required")):
                field["constraints"] = {"required": bool(row.get("required"))}
            for optional_key in [
                "unit_iri",
                "term_iri",
                "term_type",
                "property_iri",
                "entity_iri",
                "constraint_iri",
                "method_iri",
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
            resource_entry["schema"]["primaryKey"] = [
                value.strip() for value in str(table_info["primary_key"].iloc[0]).split(",") if value.strip()
            ]
        resource_entries.append(resource_entry)

    datapackage = {
        "profile": SDP_PROFILE_URL,
        "name": re.sub(r"[^a-z0-9._-]+", "-", str(dataset_id).lower()).strip("-"),
        "id": _clean(dataset_id),
        "title": _clean(dataset_meta.get("title", pd.Series([None])).iloc[0]),
        "description": _clean(dataset_meta.get("description", pd.Series([None])).iloc[0]),
        "sdp": {
            "specVersion": SDP_PROFILE_VERSION,
            "profile": SDP_PROFILE_URL,
            "rules": SDP_RULES_URL,
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
    if "license" in dataset_meta and _has_value(dataset_meta["license"].iloc[0]):
        license_value = dataset_meta["license"].iloc[0]
        if not _is_review_value(license_value):
            datapackage["licenses"] = [_license_descriptor(license_value)]
    if "temporal_start" in dataset_meta and pd.notna(dataset_meta["temporal_start"].iloc[0]):
        datapackage["temporal"] = {"start": _clean(dataset_meta["temporal_start"].iloc[0])}
        if "temporal_end" in dataset_meta and pd.notna(dataset_meta["temporal_end"].iloc[0]):
            datapackage["temporal"]["end"] = _clean(dataset_meta["temporal_end"].iloc[0])

    if write_datapackage:
        with (target / "datapackage.json").open("w", encoding="utf-8") as fp:
            json.dump(datapackage, fp, indent=2)

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
                dict_rows.append(
                    {
                        "dataset_id": datapackage.get("id") or datapackage.get("name"),
                        "table_id": resource_name,
                        "column_name": field.get("name"),
                        "column_label": field.get("title") or field.get("name"),
                        "column_description": field.get("description"),
                        "column_role": None,
                        "value_type": field.get("type", "string"),
                        "unit_label": None,
                        "unit_iri": field.get("unit_iri"),
                        "term_iri": field.get("term_iri"),
                        "term_type": field.get("term_type"),
                        "required": required,
                        "property_iri": field.get("property_iri"),
                        "entity_iri": field.get("entity_iri"),
                        "constraint_iri": field.get("constraint_iri"),
                        "method_iri": field.get("method_iri"),
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
            # Data resources go through the same reader as every other SDP CSV.
            # A bare pd.read_csv() applied pandas' full default NA vocabulary
            # ("null", "N/A", "nan", "<NA>", "None", "-1.#IND", …) and skipped
            # readr's trim_ws, so a gear code of "null" was destroyed on read
            # and a padded header survived into the parsed frame. metasalmon
            # reads resources with readr's na = c("", "NA") + trim_ws = TRUE;
            # the literal "NA" stays data here by PARITY.md row 21.
            resources[str(resource_name)] = read_sdp_csv(file_path)

    return {
        "dataset": dataset_meta,
        "tables": table_meta,
        "dictionary": dictionary,
        "codes": codes,
        "resources": resources,
    }


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
    (package_path / "README-review.txt").write_text(
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
) -> Path:
    """
    Create a review-ready Salmon Data Package in one call.

    The function infers package artifacts, optionally seeds semantic
    suggestions, writes the canonical ``data/`` and ``metadata/`` layout, and
    adds a review checklist. Inferred IRIs retain the ``REVIEW:`` marker.

    LLM assessment is strictly opt-in through ``llm_assess=True``. Supplying
    context without enabling assessment warns and makes no provider request.
    ``overwrite=True`` replaces only directories recognized as owned package
    directories.

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
    )

    suggestions = artifacts.get("semantic_suggestions")
    if isinstance(suggestions, pd.DataFrame) and not suggestions.empty:
        suggestions.to_csv(
            pkg_path / "semantic_suggestions.csv",
            index=False,
            na_rep="",
        )
    _write_review_readme(
        pkg_path,
        has_suggestions=isinstance(suggestions, pd.DataFrame)
        and not suggestions.empty,
    )

    if include_edh_xml:
        from .edh_xml import edh_build_hnap_xml

        output = pkg_path / "metadata" / "metadata-edh-hnap.xml"
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
    if require_iris:
        review_issues = _collect_review_issues(package)
        if review_issues:
            preview = " ".join(review_issues[:5])
            raise ValueError(
                f"Final validation failed with {len(review_issues)} unresolved "
                f"review issue(s). {preview}"
            )

    return {
        "package": package,
        "semantic_validation": semantic_validation,
        "issues": pd.DataFrame(columns=["message"]),
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
