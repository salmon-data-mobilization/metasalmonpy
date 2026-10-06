"""Run from this task checkout; only temporary packages are written.

Bind the package to this exact checkout before imports, then select a schema
through the real option with its remote loader stubbed. No network is needed.
"""
from copy import deepcopy
import importlib.util
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch
import warnings

import pandas as pd

root = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "metasalmonpy", root / "__init__.py", submodule_search_locations=[str(root)]
)
package = importlib.util.module_from_spec(spec)
sys.modules["metasalmonpy"] = package
spec.loader.exec_module(package)
assert Path(package.__file__).resolve().parent == root

from metasalmonpy import (
    create_sdp, infer_salmon_datapackage_artifacts, set_sdp_dataset,
    write_salmon_datapackage,
)
from metasalmonpy import sdp_schema

configured = deepcopy(sdp_schema.load_sdp_schema(source="vendored", quiet=True))
for table, field in (("dataset", "update_frequency"), ("column_dictionary", "constraint_iri"), ("tables", "method_iri"), ("codes", "vocabulary_iri")):
    fields = configured["metadata_schemas"][table]["fields"]
    assert sum(item["name"] == field for item in fields) == 1
    configured["metadata_schemas"][table]["fields"] = [
        item for item in fields if item["name"] != field
    ]
resources = {"observations": pd.DataFrame({"stream_name": ["Goldstream", "Craigflower"], "spawner_count": [1, 2]})}
cases = {}

def capture_fields(path):
    return {
        "dataset": list(pd.read_csv(path / "metadata" / "dataset.csv", nrows=0).columns),
        "dictionary": list(pd.read_csv(path / "metadata" / "column_dictionary.csv", nrows=0).columns),
        "tables": list(pd.read_csv(path / "metadata" / "tables.csv", nrows=0).columns),
        "codes": list(pd.read_csv(path / "metadata" / "codes.csv", nrows=0).columns),
    }

sdp_schema.set_sdp_schema_source("remote")
with tempfile.TemporaryDirectory(prefix="b252-python-probe-") as directory:
    output_root = Path(directory)
    with patch("metasalmonpy.sdp_schema.load_sdp_schema", return_value=configured) as loader, \
         patch("metasalmonpy.package_io.load_sdp_schema", return_value=configured), \
         warnings.catch_warnings():
        # The probe intentionally leaves semantic slots unfilled; suppress that
        # authoring warning only. Retires when this fixture supplies reviewed IRIs.
        warnings.filterwarnings("ignore", message="Hey, you definitely should fill")
        assert "update_frequency" not in sdp_schema.sdp_schema_field_names("dataset")
        assert "constraint_iri" not in sdp_schema.sdp_schema_field_names("column_dictionary")
        path = output_root / "create"
        create_sdp(
            resources, path=str(path), dataset_id="probe", seed_semantics=False,
            seed_verbose=False, check_updates=False,
        )
        cases["create"] = capture_fields(path)
        artifacts = infer_salmon_datapackage_artifacts(
            resources, dataset_id="probe", seed_semantics=False, seed_verbose=False,
        )
        dataset = artifacts["dataset_meta"].drop(columns=["update_frequency"])
        dictionary = artifacts["dict"].drop(columns=["constraint_iri"])
        tables = artifacts["table_meta"].drop(columns=["method_iri"], errors="ignore")
        codes = artifacts["codes"].drop(columns=["vocabulary_iri"])
        dataset["caller_extra"] = "preserve caller data"
        dictionary["caller_extra"] = "preserve caller data"
        assert "update_frequency" not in dataset
        assert "constraint_iri" not in dictionary
        path = output_root / "direct"
        write_salmon_datapackage(
            resources, dataset_meta=dataset, table_meta=tables,
            dict_df=dictionary, codes=codes, path=str(path),
        )
        cases["direct_writer"] = capture_fields(path)
        set_sdp_dataset(str(path), title="Updated title", quiet=True)
        cases["after_setter"] = capture_fields(path)
        assert loader.call_count > 0
        result = {
            "Python": sys.version.split()[0], "pandas": pd.__version__,
            "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
            "selected_loader_calls": loader.call_count,
            "source_sha256": {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                              for name in ("metadata.py", "package_io.py")},
            "cases": cases,
        }
print(json.dumps(result, indent=2))
