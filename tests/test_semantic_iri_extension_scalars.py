"""Exact scalar IRI ownership in the three extension files already collected.

Observation/decomposition fixtures are accepted by their real current public
validators. Methods exercises retained legacy read support, not reinstatement
of the removed SDP-0.2 registry in the current package profile.
"""

import re
import shutil
from pathlib import Path

import pandas as pd
import pytest

import metasalmonpy as ms
from metasalmonpy.sdp_methods import SDP_METHODS_COLUMNS

DATA = Path(__file__).parent / "data"
REPORT = "reproducibility/provenance/semantic-iri-dereference.csv"
EXTENSIONS = (
    ("metadata/methods.csv", "method_iri"),
    ("metadata/methods.csv", "protocol_iri"),
    ("metadata/semantic/measurement-decompositions.csv", "measurement_concept_iri"),
    ("metadata/semantic/measurement-decompositions.csv", "component_iri"),
    ("metadata/structure/observation_components.csv", "component_relation_iri"),
)


def core(root, dataset_id="extension-scalar", table_id="observations"):
    target = root / "metadata"
    target.mkdir(parents=True)
    pd.DataFrame({"dataset_id": [dataset_id]}).to_csv(target / "dataset.csv", index=False)
    pd.DataFrame({"table_id": [table_id]}).to_csv(target / "tables.csv", index=False)
    pd.DataFrame({"term_iri": [None]}).to_csv(target / "column_dictionary.csv", index=False)


def native_fixture(root, relative, field, iri):
    if relative == "metadata/methods.csv":
        core(root)
        row = dict.fromkeys(SDP_METHODS_COLUMNS, "")
        row.update(dataset_id="extension-scalar", method_iri="https://legacy.invalid/procedure",
                   method_label="Recorded procedure", method_description="Legacy registry control.")
        row[field] = iri
        pd.DataFrame([row]).to_csv(root / relative, index=False)
        assert ms.validate_sdp_methods(root) is True  # Explicitly legacy API.
    elif relative == "metadata/structure/observation_components.csv":
        shutil.copytree(DATA / "sdp-extensions" / "structure-sdp", root)
        native = ms.read_sdp_observation_structures(root)
        native["components"].loc[0, field] = iri
        ms.write_sdp_observation_structures(root, native["structures"], native["components"],
                                           overwrite=True)
        assert ms.validate_sdp_observation_structures(root) is True
    else:
        core(root, "demo-salmon-2026", "counts")
        dictionary = pd.read_csv(DATA / "decompositions" / "era-sdp" / "metadata" /
                                 "column_dictionary.csv", dtype=str, keep_default_na=False)
        rows = pd.read_csv(DATA / "decompositions" / "src" / "era-rows.csv",
                           dtype=str, keep_default_na=False)
        if field == "measurement_concept_iri":
            # The public validator requires equality with the dictionary term.
            # That canonical slot already checks the exact IRI; this regression
            # is an illicit *additional* prefix request, not total false success.
            dictionary.loc[dictionary.column_name == "count", "term_iri"] = iri
            rows[field] = iri
        else:
            rows.loc[rows.component_role == "entity", field] = iri
        dictionary.to_csv(root / "metadata/column_dictionary.csv", index=False)
        ms.write_sdp_measurement_decompositions(root, rows)
        assert ms.validate_sdp_measurement_decompositions(root) is True


@pytest.mark.parametrize("relative, field", EXTENSIONS)
def test_native_valid_scalar_is_checked_exactly_without_prefix_success(tmp_path, relative, field):
    root = tmp_path / "sdp"
    iri = f"https://extension.invalid/{field};legal-variant#exact"
    native_fixture(root, relative, field, iri)
    before = {file: file.read_bytes() for file in root.rglob("*") if file.is_file()}
    seen = []
    def request(exact):
        seen.append(exact)
        # Four standalone slots would otherwise pass by checking a nonexistent
        # identifier's successful prefix. A bound concept already appears in
        # the canonical dictionary; its extra prefix must not cause refusal.
        if field == "measurement_concept_iri":
            status = 404 if exact == iri.split(";")[0] else 200
        else:
            status = 404 if exact == iri else 200
        return {"status": status, "final_url": exact}
    if field == "measurement_concept_iri":
        result = ms.verify_sdp_semantic_iris(root, requester=request)
        assert result.loc[result.iri == iri, "status"].tolist() == [200]
    else:
        with pytest.raises(ValueError, match=re.escape(iri)):
            ms.verify_sdp_semantic_iris(root, requester=request)
        result = pd.read_csv(root / REPORT)
        assert result.loc[result.iri == iri, "status"].tolist() == [404]
    assert seen.count(iri) == 1
    assert iri.split(";")[0] not in seen
    assert seen == sorted(set(seen), key=lambda value: value.encode("utf-8"))
    assert list(result) == ["iri", "status", "final_url", "error", "attempts"]
    assert result.loc[result.iri == iri, "attempts"].tolist() == [1]
    assert all(file.read_bytes() == raw for file, raw in before.items())


@pytest.mark.parametrize("relative", sorted({relative for relative, _ in EXTENSIONS}))
def test_unknown_extension_field_keeps_its_existing_separator(tmp_path, relative):
    core(tmp_path)
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"custom_extension_iri": [
        "https://unknown.invalid/first;https://unknown.invalid/second"]}).to_csv(target, index=False)
    seen = []
    rows = ms.verify_sdp_semantic_iris(tmp_path, requester=lambda iri:
                                     (seen.append(iri) or {"status": 200, "final_url": iri}))
    assert seen == ["https://unknown.invalid/first", "https://unknown.invalid/second"]
    assert rows.iri.tolist() == seen
