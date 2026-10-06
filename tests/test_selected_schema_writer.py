"""B-252: selected-schema writes preserve caller extras without inventing them.

The original queue fixture used create_sdp(), whose inference already supplies
update_frequency and constraint_iri on BOTH sides. The full writer must keep
those caller columns. The real gap is a direct writer given metadata that lacks
an optional field also absent from the selected schema; R keeps it absent.
Dictionary validation still adds its frozen optional semantic fields, in both
packages. The paired executable probes are retained in .hub/evidence/.
"""
from copy import deepcopy
import csv
import hashlib
import io
import json
from pathlib import Path

import pandas as pd
import pytest

from metasalmonpy import (
    apply_sdp_semantics, create_sdp, infer_salmon_datapackage_artifacts,
    read_salmon_datapackage, set_sdp_code, set_sdp_column, set_sdp_dataset,
    set_sdp_table, write_salmon_datapackage,
)
from metasalmonpy import sdp_schema
from metasalmonpy.review_console import SemanticReview

OMITTED = {
    "dataset": "update_frequency",
    "tables": "method_iri",
    "column_dictionary": "constraint_iri",
    "codes": "vocabulary_iri",
}


def _resources():
    return {"observations": pd.DataFrame({
        "stream_name": ["Goldstream", "Craigflower"], "spawner_count": [1, 2],
    })}


@pytest.fixture
def selected_schema(monkeypatch):
    selected = deepcopy(sdp_schema.load_sdp_schema(source="vendored", quiet=True))
    for table, field in OMITTED.items():
        fields = selected["metadata_schemas"][table]["fields"]
        assert sum(entry["name"] == field for entry in fields) == 1
        selected["metadata_schemas"][table]["fields"] = [
            entry for entry in fields if entry["name"] != field
        ]
    sdp_schema.set_sdp_schema_source("remote")
    # Exercise the real source option, stubbing only the remote fetch boundary.
    # Retires when the loader supports an in-memory bundle source directly.
    monkeypatch.setattr(sdp_schema, "_fetch_remote_sdp_schema", lambda *args: selected)
    try:
        sdp_schema.load_sdp_schema(quiet=True)
        for table, field in OMITTED.items():
            assert field not in sdp_schema.sdp_schema_field_names(table)
        yield selected
    finally:
        sdp_schema.set_sdp_schema_source(None)


def _artifacts():
    return infer_salmon_datapackage_artifacts(
        _resources(), dataset_id="probe", seed_semantics=False, seed_verbose=False,
    )


def _write(path, artifacts):
    return write_salmon_datapackage(
        resources=_resources(), dataset_meta=artifacts["dataset_meta"],
        table_meta=artifacts["table_meta"], dict_df=artifacts["dict"],
        codes=artifacts["codes"], path=str(path),
    )


def _without_optional_fields():
    artifacts = _artifacts()
    for key, field in (("dataset_meta", "update_frequency"),
                       ("table_meta", "method_iri"),
                       ("dict", "constraint_iri"),
                       ("codes", "vocabulary_iri")):
        artifacts[key] = artifacts[key].drop(columns=[field], errors="ignore")
        artifacts[key]["caller_extra"] = "keep this caller value"
    return artifacts


def _headers(path):
    return {
        table: list(pd.read_csv(path / "metadata" / f"{table}.csv", nrows=0).columns)
        for table in OMITTED
    }


def _assert_absent_except_validated_dictionary(path):
    headers = _headers(path)
    for table in ("dataset", "tables", "codes"):
        assert OMITTED[table] not in headers[table]
        frame = pd.read_csv(path / "metadata" / f"{table}.csv")
        assert frame["caller_extra"].eq("keep this caller value").all()
    # R's validate_dictionary() independently synthesizes this semantic field.
    assert "constraint_iri" in headers["column_dictionary"]


@pytest.mark.parametrize("table", ("dataset", "tables", "codes"))
def test_direct_writer_does_not_add_unselected_optional_fields(selected_schema, tmp_path, table):
    path = _write(tmp_path / "direct", _without_optional_fields())
    assert OMITTED[table] not in _headers(path)[table]
    _assert_absent_except_validated_dictionary(path)
    # The reader remains normalized to its public, static return contract.
    package = read_salmon_datapackage(str(path))
    assert "update_frequency" in package["dataset"]
    assert "method_iri" in package["tables"]
    assert "vocabulary_iri" in package["codes"]
    _assert_absent_except_validated_dictionary(path)


def test_inference_supplied_and_caller_supplied_extras_are_preserved(selected_schema, tmp_path):
    path = create_sdp(
        _resources(), path=str(tmp_path / "inferred"), dataset_id="probe",
        seed_semantics=False, seed_verbose=False, check_updates=False,
    )
    headers = _headers(path)
    assert "update_frequency" in headers["dataset"]
    assert "constraint_iri" in headers["column_dictionary"]
    # A selected schema omitting a field is not authority to discard caller data.
    artifacts = _without_optional_fields()
    artifacts["dataset_meta"]["update_frequency"] = "Annual"
    path = _write(tmp_path / "caller", artifacts)
    assert pd.read_csv(path / "metadata" / "dataset.csv")["update_frequency"].tolist() == ["Annual"]


def test_table_inference_adds_only_the_selected_schemas_missing_fields(selected_schema, tmp_path):
    from metasalmonpy.metadata import infer_table_metadata_from_resources

    # R builds this minimal frame, then aligns to its selected schema. It does
    # not supply method_iri explicitly, unlike the other inference extras.
    assert "method_iri" not in infer_table_metadata_from_resources(_resources()).columns
    path = create_sdp(
        _resources(), path=str(tmp_path / "table-inference"), dataset_id="probe",
        seed_semantics=False, seed_verbose=False, check_updates=False,
    )
    assert "method_iri" not in _headers(path)["tables"]


def test_setters_and_apply_do_not_add_fields_to_raw_csvs(selected_schema, tmp_path):
    path = _write(tmp_path / "edited", _without_optional_fields())
    for table, field in OMITTED.items():
        located = path / "metadata" / f"{table}.csv"
        frame = pd.read_csv(located).drop(columns=[field], errors="ignore")
        frame.to_csv(located, index=False)
    dictionary_path = path / "metadata" / "column_dictionary.csv"
    # A package already lacking the validator's optional field is legitimate
    # setter/apply input. These surgical paths do not invoke the full validator.
    dictionary = pd.read_csv(dictionary_path)
    dictionary.to_csv(dictionary_path, index=False)
    resources_before = {
        p.name: p.read_bytes() for p in (path / "data").glob("*.csv")
    }
    set_sdp_dataset(str(path), title="Updated title", quiet=True)
    set_sdp_table(str(path), table="observations", table_label="Observed spawners", quiet=True)
    set_sdp_column(str(path), table="observations", column="spawner_count", column_label="Count", quiet=True)
    set_sdp_code(str(path), table="observations", column="stream_name", code_value="Goldstream", code_label="Goldstream", quiet=True)
    for table, field in OMITTED.items():
        assert field not in _headers(path)[table]
    review = SemanticReview(pd.DataFrame([{
        "slot_id": "probe-slot", "dataset_id": "probe", "table_id": "observations",
        "column_name": "spawner_count", "code_value": pd.NA,
        "target_file": "column_dictionary.csv", "target_field": "property_iri",
        "target_row_key": "probe|observations|spawner_count", "decision": "reject",
        "decision_iri": pd.NA,
    }]))
    apply_sdp_semantics(str(path), review, quiet=True)
    for table, field in OMITTED.items():
        assert field not in _headers(path)[table]
    assert resources_before == {
        p.name: p.read_bytes() for p in (path / "data").glob("*.csv")
    }


def _historical_spec_identity_bytes(relative, raw):
    """Invert only B-199's two owned spec values for the frozen e81cacd oracle."""
    current = b"sdp-0.3.2"
    historical = b"sdp-0.3.0"
    if relative == "metadata/dataset.csv":
        rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8"))))
        assert len(rows) == 1
        assert rows[0]["spec_version"] == current.decode("ascii")
    elif relative == "datapackage.json":
        descriptor = json.loads(raw)
        assert descriptor["sdp"]["specVersion"] == current.decode("ascii")
    else:
        raise AssertionError("Only the two declared spec-version owners may be inverted")
    # A unique byte token plus its parsed owner prevents an unrelated field or
    # duplicate version string from being hidden by a blanket replacement.
    assert raw.count(current) == 1
    return raw.replace(current, historical, 1)


@pytest.mark.parametrize("relative,raw", [
    ("metadata/dataset.csv", b"spec_version,title\nsdp-0.3.0,sdp-0.3.2\n"),
    ("datapackage.json", b'{"sdp":{"specVersion":"sdp-0.3.0"},"title":"sdp-0.3.2"}'),
    ("metadata/dataset.csv", b"spec_version,title\nsdp-0.3.2,sdp-0.3.2\n"),
    ("datapackage.json", b'{"sdp":{"specVersion":"sdp-0.3.2"},"title":"sdp-0.3.2"}'),
    ("metadata/dataset.csv", b"spec_version,title\nsdp-0.3.1,ordinary\n"),
    ("datapackage.json", b'{"sdp":{"specVersion":"sdp-0.3.1"},"title":"ordinary"}'),
    ("metadata/dataset.csv", b"spec_version,title\nsdp-0.3.2,first\nsdp-0.3.2,second\n"),
    ("metadata/other.csv", b"spec_version\nsdp-0.3.2\n"),
])
def test_historical_spec_identity_inverse_refuses_unowned_or_ambiguous_values(relative, raw):
    with pytest.raises(AssertionError):
        _historical_spec_identity_bytes(relative, raw)


@pytest.mark.parametrize("relative,raw", [
    ("metadata/dataset.csv", b"spec_version,title\nsdp-0.3.2,ordinary\n"),
    ("datapackage.json", b'{"sdp":{"specVersion":"sdp-0.3.2"},"title":"ordinary"}'),
])
def test_historical_spec_identity_inverse_keeps_unrelated_byte_changes(relative, raw):
    historical = raw.replace(b"sdp-0.3.2", b"sdp-0.3.0", 1)
    assert _historical_spec_identity_bytes(relative, raw) == historical
    changed = raw.replace(b"ordinary", b"changed!")
    inverted = _historical_spec_identity_bytes(relative, changed)
    assert inverted == historical.replace(b"ordinary", b"changed!")
    assert hashlib.sha256(inverted).hexdigest() != hashlib.sha256(historical).hexdigest()


def test_shipped_settings_preserve_baseline_written_bytes(tmp_path):
    # Digests were captured before the fix at e81cacd, using this exact input.
    baseline = json.loads((Path(__file__).parent / "data" /
                           "selected_schema_writer" / "shipped-output-sha256.json").read_text())
    # Keep the e81cacd historical oracle unchanged. Q14/B-127 replaces only
    # its ownership marker with the ruled shared ten-byte line. B-199 advances
    # only the dataset/descriptor spec identity; invert those owned values for
    # this historical comparison, leaving all other bytes and paths strict.
    assert baseline.pop(".metasalmonpy-package") == (
        "387f000f947f671ae160d03c3dd5669b9eacb81b9c5af5d0a08d86b544db1013"
    )
    baseline[".sdp-package"] = hashlib.sha256(b"sdp-owned\n").hexdigest()
    assert sdp_schema.sdp_profile_version() == "sdp-0.3.2"
    path = _write(tmp_path / "shipped", _without_optional_fields())
    actual = {
        p.relative_to(path).as_posix(): hashlib.sha256(
            _historical_spec_identity_bytes(p.relative_to(path).as_posix(), p.read_bytes())
            if p.relative_to(path).as_posix() in ("metadata/dataset.csv", "datapackage.json")
            else p.read_bytes()
        ).hexdigest()
        for p in path.rglob("*") if p.is_file()
    }
    assert actual == baseline
