from __future__ import annotations

import json
import shutil
import datetime as _dt
from pathlib import Path
from typing import Dict, Mapping, Optional

try:
    import pandas as pd
except ImportError as exc:  # pragma: no cover - import guard
    raise ImportError("salmonpy requires pandas; install via `pip install pandas`.") from exc

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
    df.to_csv(path, index=False, na_rep="")


def _read_metadata_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[])


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
    """
    Write canonical Salmon Data Package CSV metadata plus datapackage.json.
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

    target = Path(path)
    if target.exists() and not overwrite:
        raise FileExistsError(f"Directory {target} already exists. Set overwrite=True to replace.")
    if target.exists() and overwrite:
        for child in target.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    target.mkdir(parents=True, exist_ok=True)

    dataset_id = dataset_meta["dataset_id"].iloc[0]

    resource_entries = []
    for resource_name, resource_df in resources.items():
        table_info = table_meta[table_meta["table_id"] == resource_name]
        if table_info.empty:
            continue
        file_name = table_info["file_name"].iloc[0] if "file_name" in table_info else f"{resource_name}.{format}"
        if not _has_value(file_name):
            file_name = f"{resource_name}.{format}"
            table_meta.loc[table_meta["table_id"] == resource_name, "file_name"] = file_name
        file_path = target / file_name
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
            "profile": "data-resource",
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
        "profile": "data-package",
        "name": _clean(dataset_id),
        "title": _clean(dataset_meta.get("title", pd.Series([None])).iloc[0]),
        "description": _clean(dataset_meta.get("description", pd.Series([None])).iloc[0]),
        "resources": resource_entries,
    }

    # Optional metadata
    for key in ["creator", "license"]:
        if key in dataset_meta and pd.notna(dataset_meta[key].iloc[0]):
            datapackage[key] = _clean(dataset_meta[key].iloc[0])
    if "temporal_start" in dataset_meta and pd.notna(dataset_meta["temporal_start"].iloc[0]):
        datapackage["temporal"] = {"start": _clean(dataset_meta["temporal_start"].iloc[0])}
        if "temporal_end" in dataset_meta and pd.notna(dataset_meta["temporal_end"].iloc[0]):
            datapackage["temporal"]["end"] = _clean(dataset_meta["temporal_end"].iloc[0])

    with (target / "datapackage.json").open("w", encoding="utf-8") as fp:
        json.dump(datapackage, fp, indent=2)

    _write_metadata_csv(dataset_meta, target / "dataset.csv")
    _write_metadata_csv(table_meta, target / "tables.csv")
    _write_metadata_csv(dict_valid, target / "column_dictionary.csv")
    if codes is not None:
        _write_metadata_csv(codes, target / "codes.csv")

    return target


def read_salmon_datapackage(path: str) -> Dict[str, object]:
    """
    Read a Salmon Data Package from canonical CSV metadata when available.
    """
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(f"Directory {target} does not exist.")

    dataset_path = target / "dataset.csv"
    tables_path = target / "tables.csv"
    dict_path = target / "column_dictionary.csv"
    codes_path = target / "codes.csv"
    json_path = target / "datapackage.json"

    if dataset_path.exists() and tables_path.exists() and dict_path.exists():
        dataset_meta = normalize_dataset_meta(_read_metadata_csv(dataset_path))
        table_meta = normalize_table_meta(_read_metadata_csv(tables_path))
        dictionary = normalize_dictionary(_read_metadata_csv(dict_path))
        dictionary["required"] = parse_logical(dictionary["required"]).fillna(False).astype(bool)
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
                    "dataset_id": [datapackage.get("name")],
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
            table_rows.append(
                {
                    "dataset_id": datapackage.get("name"),
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
                        "dataset_id": datapackage.get("name"),
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
        dictionary["required"] = parse_logical(dictionary["required"]).fillna(False).astype(bool)

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
        if file_path.exists():
            resources[str(resource_name)] = pd.read_csv(file_path)

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
    semantic_sources: tuple[str, ...] = ("smn", "gcdfo", "ols", "nvs"),
    semantic_max_per_role: int = 1,
    seed_verbose: bool = True,
    seed_codes: Optional[pd.DataFrame] = None,
    seed_table_meta: Optional[pd.DataFrame] = None,
    seed_dataset_meta: Optional[pd.DataFrame] = None,
) -> Dict[str, object]:
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
    if seed_semantics:
        if seed_verbose:
            print("Seeding semantic suggestions during infer_salmon_datapackage_artifacts().")
        from .semantics import suggest_semantics

        dict_df = suggest_semantics(
            next(iter(resource_map.values())),
            dict_df,
            sources=semantic_sources,
            max_per_role=semantic_max_per_role,
            include_dwc=False,
            codes=codes,
            table_meta=table_meta,
            dataset_meta=dataset_meta,
        )
        semantic_suggestions = dict_df.attrs.get("semantic_suggestions")

    return {
        "resources": resource_map,
        "dataset_id": dataset_id,
        "dict": dict_df,
        "table_meta": table_meta,
        "codes": codes,
        "dataset_meta": dataset_meta,
        "semantic_suggestions": semantic_suggestions,
    }


def create_salmon_datapackage_from_data(
    resources,
    path: str,
    dataset_id: str = "dataset-1",
    table_id: str = "table-1",
    guess_types: bool = True,
    seed_semantics: bool = True,
    semantic_sources: tuple[str, ...] = ("smn", "gcdfo", "ols", "nvs"),
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
) -> Path:
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
    )
    pkg_path = create_salmon_datapackage(
        resources=artifacts["resources"],
        dataset_meta=artifacts["dataset_meta"],
        table_meta=artifacts["table_meta"],
        dict_df=artifacts["dict"],
        codes=artifacts["codes"],
        path=path,
        format=format,
        overwrite=overwrite,
    )

    if include_edh_xml:
        from .edh_xml import edh_build_iso19139_xml

        default_name = "metadata-edh-hnap.xml" if edh_profile == "dfo_edh_hnap" else "metadata-iso19139.xml"
        output = edh_xml_path or str(pkg_path / default_name)
        edh_build_iso19139_xml(artifacts["dataset_meta"], output_path=output, profile=edh_profile)

    print(
        "Used one-shot bootstrap flow create_salmon_datapackage_from_data(); "
        "semantic quality is provisional until reviewed and validated."
    )
    return pkg_path


__all__ = [
    "create_salmon_datapackage",
    "create_salmon_datapackage_from_data",
    "infer_salmon_datapackage_artifacts",
    "read_salmon_datapackage",
]
