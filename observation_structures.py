"""Measure-specific SDP observation structures.

Mirrors metasalmon's ``R/observation-structures.R`` at the **v0.1.8** tag.

A physical row can contain measures at different logical grains. These paired
metadata tables declare one virtual observation structure per measure:
dimensions define the logical key, one measure carries the value, and
attributes qualify the observation without changing its grain. The role names
are Data Cube-informed, but this CSV profile is not itself an RDF Data Cube
Data Structure Definition, and nothing here claims Data Cube conformance.

The shared extension helpers (safe paths, symlink refusal, the rollback-capable
multi-file writer, canonical CSV/JSON bytes) live in ``sdp_methods`` because
that is where ``R/sdp-methods.R`` keeps them.

Byte-parity contract: ``observation_structures.csv`` and
``observation_components.csv`` written here are byte-identical to metasalmon's
for the same rows, including canonical ordering
(``dataset_id, table_id, observation_structure_id[, component_order]``) and
``TRUE``/``FALSE`` logical rendering. Asserted against R-generated fixtures in
``tests/data/sdp-extensions/``.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Union

import pandas as pd

from .sdp_methods import (
    SDP_METHODS_PATH,
    SdpExtensionError,
    _assert_safe_directory,
    _atomic_write_set,
    _csv_bytes,
    _csv_cell,
    _descriptor_bytes,
    _extension_dataset_id,
    _extension_resource,
    _extension_root,
    _is_absolute_iri,
    _is_blank,
    _is_na,
    _is_symlink,
    _read_descriptor,
    _read_extension_csv,
    _validate_closed_rows,
    _validate_descriptor_resource,
    read_sdp_methods,
)

SDP_OBSERVATION_STRUCTURES_PATH = "metadata/structure/observation_structures.csv"
SDP_OBSERVATION_COMPONENTS_PATH = "metadata/structure/observation_components.csv"
SDP_OBSERVATION_STRUCTURES_COLUMNS = (
    "dataset_id",
    "table_id",
    "observation_structure_id",
    "structure_label",
    "structure_description",
)
SDP_OBSERVATION_COMPONENTS_COLUMNS = (
    "dataset_id",
    "table_id",
    "observation_structure_id",
    "component_order",
    "column_name",
    "component_role",
    "component_relation_iri",
    "required_when_observed",
)
SOSA_USED_PROCEDURE = "http://www.w3.org/ns/sosa/usedProcedure"

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_INTEGER_RE = re.compile(r"^[+-]?[0-9]+$")
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_DATETIME_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)


def _observation_paths(root: Path) -> Dict[str, Path]:
    return {
        "structures": root / SDP_OBSERVATION_STRUCTURES_PATH,
        "components": root / SDP_OBSERVATION_COMPONENTS_PATH,
    }


def _parse_logical(values: Sequence[object], field: str) -> List[bool]:
    """Mirror ``.ms_sdp_observation_parse_logical``: only TRUE/FALSE."""
    parsed: List[bool] = []
    for value in values:
        if isinstance(value, bool):
            parsed.append(value)
            continue
        if _is_na(value):
            raise SdpExtensionError(f"{field} must contain only TRUE or FALSE.")
        text = str(value).strip().upper()
        if text == "TRUE":
            parsed.append(True)
        elif text == "FALSE":
            parsed.append(False)
        else:
            raise SdpExtensionError(f"{field} must contain only TRUE or FALSE.")
    return parsed


def _parse_order(values: Sequence[object]) -> List[int]:
    """Mirror ``.ms_sdp_observation_parse_order``: positive whole numbers."""
    parsed: List[int] = []
    for value in values:
        if isinstance(value, (list, tuple, dict, set)):
            raise SdpExtensionError(
                "component_order must be an atomic integer vector."
            )
        text = "" if _is_na(value) else str(_csv_cell(value)).strip()
        try:
            number = float(text)
        except ValueError:
            raise SdpExtensionError(
                "component_order must contain positive whole numbers."
            ) from None
        if number != number or number in (float("inf"), float("-inf")):
            raise SdpExtensionError(
                "component_order must contain positive whole numbers."
            )
        if number < 1 or number != int(number):
            raise SdpExtensionError(
                "component_order must contain positive whole numbers."
            )
        parsed.append(int(number))
    return parsed


def _normalize_structures(structures: object) -> pd.DataFrame:
    """Mirror ``.ms_sdp_observation_normalize_structures``."""
    rows = _validate_closed_rows(
        structures, SDP_OBSERVATION_STRUCTURES_COLUMNS, "structures"
    )
    frame = pd.DataFrame(index=range(len(rows)))
    for column in SDP_OBSERVATION_STRUCTURES_COLUMNS:
        values = []
        for value in rows[column].tolist():
            if isinstance(value, (list, tuple, dict, set)):
                raise SdpExtensionError(
                    f"Observation-structure column {column} must be atomic."
                )
            values.append(_csv_cell(value))
        frame[column] = values
    order = sorted(
        range(len(frame)),
        key=lambda index: (
            frame["dataset_id"].iloc[index],
            frame["table_id"].iloc[index],
            frame["observation_structure_id"].iloc[index],
        ),
    )
    return frame.iloc[order].reset_index(drop=True)


def _normalize_components(components: object) -> pd.DataFrame:
    """Mirror ``.ms_sdp_observation_normalize_components``."""
    rows = _validate_closed_rows(
        components, SDP_OBSERVATION_COMPONENTS_COLUMNS, "components"
    )
    frame = pd.DataFrame(index=range(len(rows)))
    for column in SDP_OBSERVATION_COMPONENTS_COLUMNS:
        if column in ("component_order", "required_when_observed"):
            continue
        values = []
        for value in rows[column].tolist():
            if isinstance(value, (list, tuple, dict, set)):
                raise SdpExtensionError(
                    f"Observation-component column {column} must be atomic."
                )
            values.append(_csv_cell(value))
        frame[column] = values
    frame["component_order"] = _parse_order(rows["component_order"].tolist())
    frame["required_when_observed"] = _parse_logical(
        rows["required_when_observed"].tolist(), "required_when_observed"
    )
    frame = frame.loc[:, list(SDP_OBSERVATION_COMPONENTS_COLUMNS)]
    order = sorted(
        range(len(frame)),
        key=lambda index: (
            frame["dataset_id"].iloc[index],
            frame["table_id"].iloc[index],
            frame["observation_structure_id"].iloc[index],
            frame["component_order"].iloc[index],
        ),
    )
    return frame.iloc[order].reset_index(drop=True)


def _structure_keys(frame: pd.DataFrame) -> List[tuple]:
    return list(
        zip(
            frame["dataset_id"],
            frame["table_id"],
            frame["observation_structure_id"],
        )
    )


def _column_keys(frame: pd.DataFrame) -> List[tuple]:
    return list(zip(frame["dataset_id"], frame["table_id"], frame["column_name"]))


def _structure_rows(components: pd.DataFrame, structure: pd.Series) -> pd.DataFrame:
    """Mirror ``.ms_sdp_observation_structure_rows``."""
    mask = (
        (components["dataset_id"] == structure["dataset_id"])
        & (components["table_id"] == structure["table_id"])
        & (
            components["observation_structure_id"]
            == structure["observation_structure_id"]
        )
    )
    return components.loc[mask]


def _validate_required_fields(
    structures: pd.DataFrame, components: pd.DataFrame
) -> None:
    """Mirror ``.ms_sdp_observation_validate_required_fields``."""
    for column in SDP_OBSERVATION_STRUCTURES_COLUMNS:
        if any(_is_blank(value) for value in structures[column]):
            raise SdpExtensionError(
                f"Every observation-structure row requires non-empty {column}."
            )
    for column in SDP_OBSERVATION_COMPONENTS_COLUMNS:
        if column == "component_relation_iri":
            continue
        if any(_is_blank(value) for value in components[column]):
            raise SdpExtensionError(
                f"Every observation-component row requires non-empty {column}."
            )
    identifiers = list(structures["observation_structure_id"]) + list(
        components["observation_structure_id"]
    )
    if any(not _IDENTIFIER_RE.match(str(value)) for value in identifiers):
        raise SdpExtensionError(
            "Every observation_structure_id must match ^[A-Za-z_][A-Za-z0-9_]*$."
        )
    if any(
        value not in ("measure", "dimension", "attribute")
        for value in components["component_role"]
    ):
        raise SdpExtensionError(
            "component_role must be measure, dimension, or attribute."
        )
    for value in components["component_relation_iri"]:
        if not _is_blank(value) and not _is_absolute_iri(value):
            raise SdpExtensionError(
                "Every non-empty component_relation_iri must be an absolute IRI."
            )


def _validate_bindings(
    root: Path,
    structures: pd.DataFrame,
    components: pd.DataFrame,
    package: Dict[str, object],
) -> None:
    """Mirror ``.ms_sdp_observation_validate_bindings``."""
    dataset_id = _extension_dataset_id(root)
    if any(str(value) != dataset_id for value in structures["dataset_id"]) or any(
        str(value) != dataset_id for value in components["dataset_id"]
    ):
        raise SdpExtensionError(
            "Observation-structure dataset_id values must match metadata/dataset.csv."
        )

    keys = _structure_keys(structures)
    if len(set(keys)) != len(keys):
        raise SdpExtensionError(
            "observation_structure_id must be unique within each dataset and table."
        )

    tables = package["tables"]
    table_keys = set(zip(tables["dataset_id"], tables["table_id"]))
    structure_table_keys = set(
        zip(structures["dataset_id"], structures["table_id"])
    )
    if structure_table_keys - table_keys:
        raise SdpExtensionError(
            "Observation structures reference a table that is not declared in "
            "metadata/tables.csv."
        )

    if set(_structure_keys(components)) - set(keys):
        raise SdpExtensionError(
            "Observation components reference an unknown observation structure."
        )

    dictionary = package["dictionary"]
    dictionary_keys = _column_keys(dictionary)
    if set(_column_keys(components)) - set(dictionary_keys):
        raise SdpExtensionError(
            "Observation components must reference columns in the same declared table."
        )

    dictionary_index = {key: position for position, key in enumerate(dictionary_keys)}
    measure_bindings: List[tuple] = []
    for _, structure in structures.iterrows():
        bound = _structure_rows(components, structure)
        if len(bound) == 0:
            raise SdpExtensionError(
                "Observation structure "
                f"{structure['observation_structure_id']} has no components."
            )
        orders = list(bound["component_order"])
        if len(set(orders)) != len(orders):
            raise SdpExtensionError(
                "component_order must be unique within each observation structure."
            )
        if sorted(orders) != list(range(1, len(bound) + 1)):
            raise SdpExtensionError(
                "component_order must be contiguous from 1 within each "
                "observation structure."
            )
        names = list(bound["column_name"])
        if len(set(names)) != len(names):
            raise SdpExtensionError(
                "A column can be bound at most once within an observation structure."
            )
        measures = bound.loc[bound["component_role"] == "measure"]
        if len(measures) != 1:
            raise SdpExtensionError(
                "Each observation structure must have exactly one measure component."
            )
        if int((bound["component_role"] == "dimension").sum()) < 1:
            raise SdpExtensionError(
                "Each observation structure must have at least one dimension component."
            )
        measure_key = _column_keys(measures)[0]
        if measure_key in measure_bindings:
            raise SdpExtensionError(
                "A measurement column can be the measure of at most one "
                "observation structure."
            )
        measure_bindings.append(measure_key)

        bound_roles = [
            str(dictionary["column_role"].iloc[dictionary_index[key]])
            for key in _column_keys(bound)
        ]
        for position, role in enumerate(bound["component_role"]):
            if role == "measure" and bound_roles[position] != "measurement":
                raise SdpExtensionError(
                    "Every measure component must bind a measurement dictionary column."
                )
        for position, role in enumerate(bound["component_role"]):
            if role in ("measure", "dimension") and not bool(
                bound["required_when_observed"].iloc[position]
            ):
                raise SdpExtensionError(
                    "Measure and dimension components must set "
                    "required_when_observed to TRUE."
                )
        for position, relation in enumerate(bound["component_relation_iri"]):
            if _is_blank(relation) or str(relation) != SOSA_USED_PROCEDURE:
                continue
            if bound["component_role"].iloc[position] != "attribute":
                raise SdpExtensionError(
                    "A sosa:usedProcedure component must have the attribute role."
                )
            if bound_roles[position] != "categorical":
                raise SdpExtensionError(
                    "A sosa:usedProcedure component must bind a categorical "
                    "dictionary column."
                )

    # Once the optional extension is present it is a complete measure-level
    # structural inventory, not a selective annotation. Partial coverage would
    # leave consumers unable to tell whether an omitted measure shares a table
    # grain or was simply forgotten.
    measurement_rows = dictionary.loc[dictionary["column_role"] == "measurement"]
    expected = _column_keys(measurement_rows)
    missing = [key for key in expected if key not in measure_bindings]
    if missing:
        labels = [f"{key[1]}.{key[2]}" for key in missing]
        raise SdpExtensionError(
            "When observation structures are present, every measurement column "
            "must be bound as exactly one measure. Not bound as a measure: "
            + ", ".join(labels)
            + "."
        )


def _method_registry(root: Path) -> pd.DataFrame:
    """Mirror ``.ms_sdp_observation_method_registry``."""
    if not (root / SDP_METHODS_PATH).exists():
        return pd.DataFrame({"dataset_id": [], "method_iri": []}, dtype=object)
    return read_sdp_methods(root, validate=True)


def _normalize_typed_values(
    values: Sequence[object], value_type: object, column_name: str
) -> List[str]:
    """Mirror ``.ms_sdp_observation_normalize_typed_values``.

    Grain identity and repeated-value invariance are decided on the
    dictionary-declared *type*, not the source lexical form: ``02019`` and
    ``2019`` are the same integer dimension value, and ``100`` and ``100.0``
    are the same number.
    """
    text = ["" if _is_na(value) else str(value) for value in values]
    normalized = list(text)
    present = [index for index, value in enumerate(text) if not _is_blank(value)]
    if not present:
        return ["" if _is_blank(value) else value for value in normalized]
    for index, value in enumerate(normalized):
        if _is_blank(value):
            normalized[index] = ""

    declared = str(value_type).strip().lower() if not _is_na(value_type) else ""

    def fail() -> None:
        raise SdpExtensionError(
            "Observation component values do not match their dictionary "
            f"value_type. Column {column_name} declares {declared}."
        )

    if declared == "integer":
        for index in present:
            candidate = text[index]
            if not _INTEGER_RE.match(candidate):
                fail()
            normalized[index] = str(int(candidate))
    elif declared == "number":
        for index in present:
            try:
                parsed = float(text[index])
            except ValueError:
                fail()
                return normalized  # pragma: no cover - fail() always raises
            if parsed != parsed or parsed in (float("inf"), float("-inf")):
                fail()
            normalized[index] = _format_number(parsed)
    elif declared == "boolean":
        for index in present:
            candidate = text[index].strip().upper()
            if candidate not in ("TRUE", "FALSE"):
                fail()
            normalized[index] = candidate
    elif declared == "date":
        for index in present:
            candidate = text[index]
            if not _DATE_RE.match(candidate):
                fail()
            try:
                parsed_date = _dt.date.fromisoformat(candidate)
            except ValueError:
                fail()
                return normalized  # pragma: no cover - fail() always raises
            normalized[index] = parsed_date.isoformat()
    elif declared == "datetime":
        for index in present:
            candidate = text[index]
            if not _DATETIME_RE.match(candidate):
                fail()
            parsed_datetime = _parse_datetime(candidate)
            if parsed_datetime is None:
                fail()
                return normalized  # pragma: no cover - fail() always raises
            normalized[index] = parsed_datetime.strftime("%Y-%m-%dT%H:%M:%SZ")
    elif declared != "string":
        fail()
    return normalized


def _format_number(value: float) -> str:
    """R's ``format(value, scientific = FALSE, trim = TRUE, digits = 17)``.

    Numeric equality, not the source spelling, decides grain and invariance,
    so the canonical form must be identical for every spelling of one value.
    """
    if value == 0:
        return "0"
    if value == int(value) and abs(value) < 1e16:
        return str(int(value))
    return repr(value)


def _parse_datetime(value: str) -> Optional[_dt.datetime]:
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = _dt.datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(_dt.timezone.utc).replace(tzinfo=None)


def _cast_typed_values(
    values: Sequence[object], value_type: object, column_name: str
) -> List[object]:
    """Mirror ``.ms_sdp_observation_cast_typed_values``."""
    normalized = _normalize_typed_values(values, value_type, column_name)
    declared = str(value_type).strip().lower() if not _is_na(value_type) else ""
    cast: List[object] = []
    for value in normalized:
        if value == "":
            cast.append(None)
            continue
        if declared == "integer":
            cast.append(int(value))
        elif declared == "number":
            cast.append(float(value))
        elif declared == "boolean":
            cast.append(value == "TRUE")
        elif declared == "date":
            cast.append(_dt.date.fromisoformat(value))
        elif declared == "datetime":
            cast.append(_parse_datetime(value))
        else:
            cast.append(value)
    return cast


def _validate_procedure_codes(
    structure: pd.Series,
    bound: pd.DataFrame,
    data: pd.DataFrame,
    observed_rows: List[int],
    codes: Optional[pd.DataFrame],
    methods: pd.DataFrame,
) -> None:
    """Mirror ``.ms_sdp_observation_validate_procedure_codes``."""
    procedures = bound.loc[
        [
            (not _is_blank(value)) and str(value) == SOSA_USED_PROCEDURE
            for value in bound["component_relation_iri"]
        ]
    ]
    registered = set(zip(methods["dataset_id"], methods["method_iri"]))
    for _, procedure in procedures.iterrows():
        column = procedure["column_name"]
        if codes is None or len(codes) == 0:
            enumerated = pd.DataFrame(columns=["code_value", "term_iri"], dtype=object)
        else:
            mask = (
                (codes["dataset_id"] == structure["dataset_id"])
                & (codes["table_id"] == structure["table_id"])
                & (codes["column_name"] == column)
                & pd.Series(
                    [not _is_blank(value) for value in codes["code_value"]],
                    index=codes.index,
                )
            )
            enumerated = codes.loc[mask]
        code_values = [str(value) for value in enumerated.get("code_value", [])]
        if len(set(code_values)) != len(code_values):
            raise SdpExtensionError(
                "Enumerated sosa:usedProcedure code values must be unique per column."
            )
        for position, code_value in enumerate(code_values):
            method_iri = enumerated["term_iri"].iloc[position]
            if _is_blank(method_iri):
                raise SdpExtensionError(
                    "Every enumerated sosa:usedProcedure code must resolve "
                    "through exactly one metadata/codes.csv row with a "
                    f"term_iri. Column {column}, code {code_value}."
                )
            if (structure["dataset_id"], str(method_iri)) not in registered:
                raise SdpExtensionError(
                    "An enumerated sosa:usedProcedure code resolves to an "
                    f"unregistered method. Column {column}, code {code_value}."
                )

        observed_values: List[str] = []
        for row in observed_rows:
            value = data[column].iloc[row]
            if _is_blank(value):
                continue
            text = str(value)
            if text not in observed_values:
                observed_values.append(text)
        missing = [value for value in observed_values if value not in code_values]
        if missing:
            raise SdpExtensionError(
                "Every observed sosa:usedProcedure code must resolve through "
                "exactly one metadata/codes.csv row with a registered term_iri. "
                f"Column {column}, unregistered code(s) "
                + ", ".join(missing)
                + "."
            )


def _validate_data(
    root: Path,
    structures: pd.DataFrame,
    components: pd.DataFrame,
    package: Dict[str, object],
) -> None:
    """Mirror ``.ms_sdp_observation_validate_data``."""
    methods = _method_registry(root)
    dictionary = package["dictionary"]
    static_methods: List[str] = []
    if "method_iri" in dictionary.columns:
        for value in dictionary["method_iri"]:
            if not _is_blank(value) and str(value) not in static_methods:
                static_methods.append(str(value))
    if static_methods:
        registered = set(methods["method_iri"])
        missing = [value for value in static_methods if value not in registered]
        if missing:
            raise SdpExtensionError(
                "Static procedure references used by observation structures "
                "require metadata/methods.csv registry rows. Unregistered "
                "method_iri: " + ", ".join(missing) + "."
            )

    codes = package.get("codes")
    dictionary_index = {
        key: position for position, key in enumerate(_column_keys(dictionary))
    }

    for _, structure in structures.iterrows():
        bound = _structure_rows(components, structure)
        table_id = structure["table_id"]
        data = package["resources"].get(table_id)
        if data is None:
            raise SdpExtensionError(
                f"Could not load data table {table_id} for observation-structure "
                "validation."
            )
        measure = list(bound.loc[bound["component_role"] == "measure", "column_name"])[0]
        observed_rows = [
            index
            for index in range(len(data))
            if not _is_blank(data[measure].iloc[index])
        ]
        _validate_procedure_codes(
            structure, bound, data, observed_rows, codes, methods
        )
        if not observed_rows:
            continue

        for position, column in enumerate(bound["column_name"]):
            if not bool(bound["required_when_observed"].iloc[position]):
                continue
            empty = [
                index for index in observed_rows if _is_blank(data[column].iloc[index])
            ]
            if empty:
                raise SdpExtensionError(
                    "A required observation component is empty where its measure "
                    f"is observed. Table {table_id}, structure "
                    f"{structure['observation_structure_id']}, column {column}, "
                    "data row(s) "
                    + ", ".join(str(index + 1) for index in empty)
                    + "."
                )

        normalized_components: Dict[str, List[str]] = {}
        for position, column in enumerate(bound["column_name"]):
            key = _column_keys(bound)[position]
            value_type = dictionary["value_type"].iloc[dictionary_index[key]]
            normalized_components[column] = _normalize_typed_values(
                [data[column].iloc[index] for index in observed_rows],
                value_type,
                column,
            )

        dimensions = list(bound.loc[bound["component_role"] == "dimension", "column_name"])
        attributes = list(
            bound.loc[bound["component_role"] == "attribute", "column_name"]
        )
        invariants = [measure] + attributes
        grain_keys = [
            json.dumps(
                {column: normalized_components[column][row] for column in dimensions},
                ensure_ascii=False,
            )
            for row in range(len(observed_rows))
        ]
        by_grain: Dict[str, set] = {}
        for row, grain_key in enumerate(grain_keys):
            value_key = "\r".join(
                normalized_components[column][row] for column in invariants
            )
            by_grain.setdefault(grain_key, set()).add(value_key)
        for grain_key, value_keys in by_grain.items():
            if len(value_keys) > 1:
                raise SdpExtensionError(
                    "Repeated observations at one declared dimension grain are "
                    f"not invariant. Table {table_id}, structure "
                    f"{structure['observation_structure_id']}, dimension tuple "
                    f"{grain_key} has conflicting measure or attribute values."
                )


def _observation_resources() -> List[Dict[str, str]]:
    return [
        _extension_resource(
            "sdp_observation_structures",
            SDP_OBSERVATION_STRUCTURES_PATH,
            "SDP observation structures metadata",
            "Optional logical observation structures that declare the grain "
            "of individual measures in wide or mixed-grain tables.",
            "observation_structures.schema.json",
        ),
        _extension_resource(
            "sdp_observation_components",
            SDP_OBSERVATION_COMPONENTS_PATH,
            "SDP observation components metadata",
            "Ordered bindings from table columns to the measure, dimensions, "
            "and attributes of a logical observation structure.",
            "observation_components.schema.json",
        ),
    ]


def _validate_descriptor(root: Path) -> None:
    """Mirror ``.ms_sdp_observation_validate_descriptor``."""
    descriptor = _read_descriptor(root)
    if descriptor is None:
        return
    for expected in _observation_resources():
        _validate_descriptor_resource(descriptor, expected)
    declared = (descriptor.get("sdp") or {}).get("metadata") or {}
    if (
        declared.get("observationStructures") != SDP_OBSERVATION_STRUCTURES_PATH
        or declared.get("observationComponents") != SDP_OBSERVATION_COMPONENTS_PATH
    ):
        raise SdpExtensionError(
            "datapackage.json must declare both paired observation-structure "
            "metadata resources."
        )


def _validate_rows(
    root: Path,
    structures: pd.DataFrame,
    components: pd.DataFrame,
    check_descriptor: bool = True,
) -> None:
    """Mirror ``.ms_sdp_observation_validate_rows``."""
    if len(structures) == 0 or len(components) == 0:
        raise SdpExtensionError(
            "Present observation-structure files must each contain at least one row."
        )
    _validate_required_fields(structures, components)
    from .package_io import read_salmon_datapackage

    package = read_salmon_datapackage(str(root))
    _validate_bindings(root, structures, components, package)
    _validate_data(root, structures, components, package)
    if check_descriptor:
        _validate_descriptor(root)


def _assert_paired(root: Path, require_present: bool = True) -> bool:
    """Mirror ``.ms_sdp_observation_assert_paired``."""
    paths = _observation_paths(root)
    present = {name: path.exists() for name, path in paths.items()}
    if any(_is_symlink(path) for path in paths.values()):
        raise SdpExtensionError(
            "Refusing symlinked observation-structure metadata files."
        )
    if present["structures"] != present["components"]:
        raise SdpExtensionError(
            "observation_structures.csv and observation_components.csv must be "
            "present together."
        )
    if require_present and not all(present.values()):
        raise SdpExtensionError(
            "The paired observation-structure metadata files are absent."
        )
    return all(present.values())


def validate_optional_sdp_observation_metadata(path: Union[str, Path]) -> bool:
    """Mirror ``.ms_validate_optional_sdp_observation_metadata``.

    Called from ``validate_salmon_datapackage``. The absence of both optional
    extensions preserves the historic validation path exactly; presence of
    either validates the canonical files and their data-level bindings.
    """
    root = _extension_root(path)
    methods_path = root / SDP_METHODS_PATH
    if methods_path.exists() or _is_symlink(methods_path):
        from .sdp_methods import validate_sdp_methods

        validate_sdp_methods(root)
    structure_paths = _observation_paths(root)
    if any(
        path.exists() or _is_symlink(path) for path in structure_paths.values()
    ):
        validate_sdp_observation_structures(root)
    return True


# --- public API ----------------------------------------------------------------------


def read_sdp_observation_structures(
    path: Union[str, Path], validate: bool = True
) -> Dict[str, pd.DataFrame]:
    """Read measure-specific SDP observation structures.

    Parameters
    ----------
    path:
        Existing Salmon Data Package directory.
    validate:
        When ``True``, validate package references, logical grain, procedure
        bindings, and the ``datapackage.json`` resource inventory.

    Returns
    -------
    dict
        ``{"structures": DataFrame, "components": DataFrame}`` in canonical
        order. ``component_order`` is ``int``; ``required_when_observed`` is
        ``bool``; every other column is text.
    """
    root = _extension_root(path)
    if not isinstance(validate, bool):
        raise SdpExtensionError("validate must be True or False.")
    _assert_paired(root, require_present=True)
    structures = _read_extension_csv(
        root, SDP_OBSERVATION_STRUCTURES_PATH, SDP_OBSERVATION_STRUCTURES_COLUMNS
    )
    components = _read_extension_csv(
        root, SDP_OBSERVATION_COMPONENTS_PATH, SDP_OBSERVATION_COMPONENTS_COLUMNS
    )
    structures = _normalize_structures(structures)
    components = _normalize_components(components)
    if validate:
        _validate_rows(root, structures, components)
    return {"structures": structures, "components": components}


def validate_sdp_observation_structures(path: Union[str, Path]) -> bool:
    """Validate measure-specific SDP observation structures.

    Returns
    -------
    bool
        ``True`` when the paired resources and all data-level bindings are
        valid; otherwise an exception is raised.
    """
    read_sdp_observation_structures(path, validate=True)
    return True


def write_sdp_observation_structures(
    path: Union[str, Path],
    structures: object = None,
    components: object = None,
    overwrite: bool = False,
) -> Optional[Dict[str, str]]:
    """Write measure-specific SDP observation structures.

    Writes the paired canonical resources under ``metadata/structure/``. Each
    structure has one measure; dimensions define that measure's grain and
    attributes qualify it. Supplying both row arguments as ``None`` is an
    explicit no-op. Supplying only one is an error because the files are a
    pair.

    The two CSVs and any ``datapackage.json`` update are staged and installed
    as one rollback-capable transaction, then re-validated from the bytes on
    disk; a failure anywhere leaves the previous file set byte-identical.

    Parameters
    ----------
    path:
        Existing Salmon Data Package directory.
    structures:
        ``None``, or rows matching the exact SDP observation-structures schema.
    components:
        ``None``, or rows matching the exact SDP observation-components schema.
    overwrite:
        Replace both managed resources when ``True``.

    Returns
    -------
    Optional[dict]
        ``{"structures": path, "components": path}``, or ``None`` for an
        explicit no-op.
    """
    if structures is None and components is None:
        return None
    if structures is None or components is None:
        raise SdpExtensionError(
            "structures and components must be supplied together."
        )
    root = _extension_root(path)
    if not isinstance(overwrite, bool):
        raise SdpExtensionError("overwrite must be True or False.")
    structure_rows = _normalize_structures(structures)
    component_rows = _normalize_components(components)
    _validate_rows(root, structure_rows, component_rows, check_descriptor=False)

    paths = _observation_paths(root)
    existing = [
        candidate
        for candidate in paths.values()
        if candidate.exists() or _is_symlink(candidate)
    ]
    if existing and not overwrite:
        raise FileExistsError(
            "Observation-structure output already exists and overwrite is False."
        )
    _assert_safe_directory(root, "metadata/structure", create=True)
    if any(_is_symlink(candidate) for candidate in paths.values()):
        raise SdpExtensionError(
            "Refusing to overwrite symlinked observation-structure metadata files."
        )

    descriptor_bytes = _descriptor_bytes(
        root,
        resources=_observation_resources(),
        metadata={
            "observationStructures": SDP_OBSERVATION_STRUCTURES_PATH,
            "observationComponents": SDP_OBSERVATION_COMPONENTS_PATH,
        },
    )
    writes: Dict[Union[str, Path], bytes] = {
        str(paths["structures"]): _csv_bytes(
            SDP_OBSERVATION_STRUCTURES_COLUMNS, structure_rows
        ),
        str(paths["components"]): _csv_bytes(
            SDP_OBSERVATION_COMPONENTS_COLUMNS, component_rows
        ),
    }
    if descriptor_bytes is not None:
        writes[str(root / "datapackage.json")] = descriptor_bytes
    _atomic_write_set(
        writes, validate=lambda: validate_sdp_observation_structures(root)
    )
    return {name: str(candidate) for name, candidate in paths.items()}


def extract_sdp_observations(
    path: Union[str, Path],
    table_id: Optional[str] = None,
    observation_structure_id: Optional[str] = None,
) -> Dict[str, pd.DataFrame]:
    """Extract normalized logical observations from a Salmon Data Package.

    Validates the paired structure metadata, drops rows where each structure's
    measure is absent, selects components in declared order, casts each column
    through its dictionary ``value_type``, and collapses exact repeats at a
    coarser declared grain. The result stays a mapping because different
    structures have different component columns and types.

    This produces a normalized table per declared structure. It does **not**
    claim RDF Data Cube conformance.

    Parameters
    ----------
    path:
        Existing Salmon Data Package directory.
    table_id:
        Optional table identifier used to select structures.
    observation_structure_id:
        Optional structure identifier used to select structures. Supply
        ``table_id`` too when the identifier is not unique across tables.

    Returns
    -------
    dict
        One DataFrame per selected structure, keyed
        ``table_id::observation_structure_id`` in canonical order.
    """
    root = _extension_root(path)
    metadata = read_sdp_observation_structures(root, validate=True)
    structures = metadata["structures"]

    def _selector(value: object, argument: str) -> Optional[str]:
        if value is None:
            return None
        if (
            isinstance(value, (list, tuple, dict, set))
            or _is_na(value)
            or not str(value).strip()
        ):
            raise SdpExtensionError(
                f"{argument} must be None or one non-empty string."
            )
        return str(value)

    table_id = _selector(table_id, "table_id")
    observation_structure_id = _selector(
        observation_structure_id, "observation_structure_id"
    )
    if table_id is not None:
        structures = structures.loc[structures["table_id"] == table_id]
    if observation_structure_id is not None:
        structures = structures.loc[
            structures["observation_structure_id"] == observation_structure_id
        ]
    if len(structures) == 0:
        raise SdpExtensionError(
            "No observation structure matches the requested selector(s)."
        )

    from .package_io import read_salmon_datapackage

    package = read_salmon_datapackage(str(root))
    dictionary = package["dictionary"]
    dictionary_index = {
        key: position for position, key in enumerate(_column_keys(dictionary))
    }
    output: Dict[str, pd.DataFrame] = {}
    for _, structure in structures.iterrows():
        components = _structure_rows(metadata["components"], structure)
        components = components.sort_values("component_order")
        measure = list(
            components.loc[components["component_role"] == "measure", "column_name"]
        )[0]
        dimensions = list(
            components.loc[components["component_role"] == "dimension", "column_name"]
        )
        data = package["resources"][structure["table_id"]]
        keep = [
            index
            for index in range(len(data))
            if not _is_blank(data[measure].iloc[index])
        ]
        selected = {}
        for position, column in enumerate(components["column_name"]):
            key = _column_keys(components)[position]
            value_type = dictionary["value_type"].iloc[dictionary_index[key]]
            selected[column] = _cast_typed_values(
                [data[column].iloc[index] for index in keep], value_type, column
            )
        frame = pd.DataFrame(selected, columns=list(components["column_name"]))
        frame = frame.drop_duplicates().reset_index(drop=True)
        if dimensions and len(frame) > 0:
            frame = frame.sort_values(
                dimensions, kind="mergesort", na_position="last"
            ).reset_index(drop=True)
        name = f"{structure['table_id']}::{structure['observation_structure_id']}"
        output[name] = frame
    return output


__all__ = [
    "SDP_OBSERVATION_COMPONENTS_COLUMNS",
    "SDP_OBSERVATION_COMPONENTS_PATH",
    "SDP_OBSERVATION_STRUCTURES_COLUMNS",
    "SDP_OBSERVATION_STRUCTURES_PATH",
    "SOSA_USED_PROCEDURE",
    "extract_sdp_observations",
    "read_sdp_observation_structures",
    "validate_optional_sdp_observation_metadata",
    "validate_sdp_observation_structures",
    "write_sdp_observation_structures",
]
