from __future__ import annotations

"""
Prototype helpers to assemble a DwC-DP datapackage descriptor and optionally
run Frictionless validation if the `frictionless` package is available.

This keeps DwC-DP as an export/interoperability layer. It assumes the caller
already has DwC-shaped CSVs (e.g., occurrence.csv, event.csv) and knows the
canonical DwC-DP table schema names to reference.
"""

import json
from pathlib import Path
from typing import Iterable, Optional


def build_dwc_dp_descriptor(
    resources: Iterable[dict],
    profile_version: str = "master",
    profile_url: str = "http://rs.tdwg.org/dwc/dwc-dp",
) -> dict:
    """
    Build a minimal DwC-DP datapackage descriptor.

    Each resource dict should include:
      - name: resource name (e.g., "occurrence")
      - path: path to the CSV file
      - schema: DwC-DP table schema name (e.g., "occurrence", "event", "material")

    The schema URL is resolved to the canonical GitHub location for the given
    profile_version.
    """
    if hasattr(resources, "to_dict"):
        resources_iter = resources.to_dict(orient="records")
    else:
        resources_iter = resources

    res_list = []
    for res in resources_iter:
        if "name" not in res or "path" not in res or "schema" not in res:
            raise ValueError("Each resource must include 'name', 'path', and 'schema'")
        schema_name = res["schema"]
        schema_url = (
            f"https://raw.githubusercontent.com/gbif/dwc-dp/{profile_version}/dwc-dp/table-schemas/{schema_name}.json"
        )
        res_list.append(
            {
                "name": res["name"],
                "path": res["path"],
                "profile": "tabular-data-resource",
                "schema": schema_url,
            }
        )

    descriptor = {
        "profile": profile_url,
        "name": "dwc-dp-export",
        "resources": res_list,
    }
    return descriptor


def dwc_dp_build_descriptor(
    resources: Iterable[dict],
    profile_version: str = "master",
    profile_url: str = "http://rs.tdwg.org/dwc/dwc-dp",
    output_path: Optional[str] = None,
    validate: bool = False,
    python: str = "python3",
) -> dict:
    descriptor = build_dwc_dp_descriptor(
        resources=resources,
        profile_version=profile_version,
        profile_url=profile_url,
    )
    if output_path is not None:
        save_descriptor(descriptor, output_path)
    if validate:
        validate_descriptor(descriptor)
    return descriptor


def save_descriptor(descriptor: dict, output_path: str) -> None:
    """
    Save the descriptor to disk as JSON.
    """
    Path(output_path).write_text(json.dumps(descriptor, indent=2))


def validate_descriptor(descriptor: dict) -> Optional[object]:
    """
    Run frictionless validation if available. Returns the frictionless report
    object, or None if frictionless is not installed.
    """
    try:
        import frictionless
    except ImportError:
        return None

    # Write to a temp file in-memory via Resource/Package interfaces
    package = frictionless.Package(descriptor=descriptor)
    return package.validate()


__all__ = ["build_dwc_dp_descriptor", "dwc_dp_build_descriptor", "save_descriptor", "validate_descriptor"]
