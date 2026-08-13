from pathlib import Path

import pandas as pd
import pytest

from metasalmonpy import (
    create_sdp,
    read_salmon_datapackage,
    validate_salmon_datapackage,
    write_edh_xml_from_sdp,
    write_salmon_datapackage,
)
from metasalmonpy.package_io import _is_semantic_code_candidate


def _reviewed_artifacts():
    resources = {
        "observations": pd.DataFrame(
            {
                "sample_id": ["a", "b"],
                "catch_count": [2, 4],
            }
        )
    }
    dataset = pd.DataFrame(
        {
            "dataset_id": ["demo"],
            "title": ["Demo catches"],
            "description": ["Reviewed catch observations."],
            "creator": ["DFO"],
            "license": ["Open Government Licence - Canada"],
        }
    )
    tables = pd.DataFrame(
        {
            "dataset_id": ["demo"],
            "table_id": ["observations"],
            "file_name": ["observations.csv"],
            "table_label": ["Observations"],
            "description": ["One row per catch observation."],
            "observation_unit": ["catch observation"],
            "observation_unit_iri": ["https://w3id.org/smn/CatchObservation"],
            "primary_key": ["sample_id"],
        }
    )
    dictionary = pd.DataFrame(
        {
            "dataset_id": ["demo", "demo"],
            "table_id": ["observations", "observations"],
            "column_name": ["sample_id", "catch_count"],
            "column_label": ["Sample identifier", "Catch count"],
            "column_description": [
                "Unique sample identifier.",
                "Number of salmon in the catch.",
            ],
            "column_role": ["identifier", "measurement"],
            "value_type": ["string", "integer"],
            "required": [True, False],
            "unit_label": [pd.NA, "count"],
            "unit_iri": [pd.NA, "http://qudt.org/vocab/unit/NUM"],
            "term_iri": [
                "https://w3id.org/smn/SampleIdentifier",
                "https://w3id.org/smn/CatchAbundance",
            ],
            "term_type": ["skos_concept", "skos_concept"],
            "property_iri": [pd.NA, "https://w3id.org/smn/Count"],
            "entity_iri": [pd.NA, "https://w3id.org/smn/Catch"],
            "constraint_iri": [pd.NA, pd.NA],
            "method_iri": [pd.NA, pd.NA],
        }
    )
    return resources, dataset, tables, dictionary


def test_writer_uses_current_sdp_layout_and_reader_round_trips(tmp_path):
    resources, dataset, tables, dictionary = _reviewed_artifacts()

    package_path = write_salmon_datapackage(
        resources,
        dataset,
        tables,
        dictionary,
        path=tmp_path / "demo-sdp",
    )

    assert (package_path / "metadata" / "dataset.csv").exists()
    assert (package_path / "metadata" / "tables.csv").exists()
    assert (package_path / "metadata" / "column_dictionary.csv").exists()
    assert (package_path / "data" / "observations.csv").exists()
    assert (package_path / "datapackage.json").exists()
    assert (package_path / ".metasalmonpy-package").exists()

    package = read_salmon_datapackage(package_path)
    assert package["dataset"]["dataset_id"].iloc[0] == "demo"
    assert set(package["resources"]) == {"observations"}
    assert package["tables"]["file_name"].iloc[0] == "data/observations.csv"


def test_unknown_required_flags_survive_package_round_trip(tmp_path):
    resources, dataset, tables, dictionary = _reviewed_artifacts()
    dictionary["required"] = pd.Series(
        [True, pd.NA],
        dtype="boolean",
    )

    package_path = write_salmon_datapackage(
        resources,
        dataset,
        tables,
        dictionary,
        path=tmp_path / "unknown-required-sdp",
    )
    package = read_salmon_datapackage(package_path)
    count_row = package["dictionary"].loc[
        package["dictionary"]["column_name"] == "catch_count"
    ].iloc[0]

    assert pd.isna(count_row["required"])


def test_writer_refuses_to_overwrite_an_unowned_directory(tmp_path):
    resources, dataset, tables, dictionary = _reviewed_artifacts()
    target = tmp_path / "existing"
    target.mkdir()
    (target / "keep.txt").write_text("user data", encoding="utf-8")

    with pytest.raises(ValueError, match="Refusing to overwrite"):
        write_salmon_datapackage(
            resources,
            dataset,
            tables,
            dictionary,
            path=target,
            overwrite=True,
        )

    assert (target / "keep.txt").read_text(encoding="utf-8") == "user data"


def test_writer_rejects_resource_paths_that_escape_the_package(tmp_path):
    resources, dataset, tables, dictionary = _reviewed_artifacts()
    tables.loc[0, "file_name"] = "data/../../outside.csv"

    with pytest.raises(ValueError, match="must not contain"):
        write_salmon_datapackage(
            resources,
            dataset,
            tables,
            dictionary,
            path=tmp_path / "unsafe-sdp",
        )

    assert not (tmp_path / "outside.csv").exists()
    assert not (tmp_path / "unsafe-sdp").exists()


def test_writer_warns_when_resource_has_no_table_metadata(tmp_path):
    resources, dataset, tables, dictionary = _reviewed_artifacts()
    resources["unregistered"] = pd.DataFrame({"value": [1]})

    with pytest.warns(UserWarning, match="No table metadata found"):
        package_path = write_salmon_datapackage(
            resources,
            dataset,
            tables,
            dictionary,
            path=tmp_path / "missing-table-metadata-sdp",
        )

    assert not (package_path / "data" / "unregistered.csv").exists()


def test_create_sdp_writes_review_files_without_enabling_llm(tmp_path):
    calls = []

    def forbidden_request(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("LLM request must remain opt-in")

    with pytest.warns(UserWarning, match="ignored unless llm_assess=True"):
        package_path = create_sdp(
            pd.DataFrame({"sample_id": ["a"], "catch_count": [2]}),
            path=tmp_path / "created-sdp",
            dataset_id="demo",
            table_id="observations",
            seed_semantics=False,
            llm_context_text="Catch count is a whole-number abundance.",
            llm_request_fn=forbidden_request,
        )

    assert calls == []
    assert (package_path / "README-review.txt").exists()
    assert (package_path / "metadata" / "dataset.csv").exists()
    assert (package_path / "data" / "observations.csv").exists()


def test_create_sdp_rejects_parsed_context_objects_before_inference(tmp_path):
    with pytest.raises(TypeError, match="local file paths"):
        create_sdp(
            pd.DataFrame({"sample_id": ["a"], "catch_count": [2]}),
            path=tmp_path / "invalid-context-sdp",
            seed_semantics=False,
            llm_context_files=pd.DataFrame({"field": ["catch_count"]}),
        )

    assert not (tmp_path / "invalid-context-sdp").exists()


def test_factor_code_scope_includes_codes_but_excludes_free_text():
    assert _is_semantic_code_candidate(
        "life_stage",
        pd.Series(["adult", "juvenile", "adult", "juvenile"]),
    )
    assert not _is_semantic_code_candidate(
        "field_notes",
        pd.Series(["first observation", "second observation"]),
    )


def test_strict_package_validation_rejects_review_markers(tmp_path):
    resources, dataset, tables, dictionary = _reviewed_artifacts()
    dictionary.loc[1, "term_iri"] = "REVIEW:https://w3id.org/smn/CatchAbundance"
    package_path = write_salmon_datapackage(
        resources,
        dataset,
        tables,
        dictionary,
        path=tmp_path / "review-sdp",
    )

    validate_salmon_datapackage(package_path, require_iris=False)
    with pytest.raises(ValueError, match="unresolved review"):
        validate_salmon_datapackage(package_path, require_iris=True)


def test_edh_rebuild_requires_reviewed_metadata(tmp_path):
    resources, dataset, tables, dictionary = _reviewed_artifacts()
    package_path = write_salmon_datapackage(
        resources,
        dataset,
        tables,
        dictionary,
        path=tmp_path / "reviewed-sdp",
    )

    result = write_edh_xml_from_sdp(package_path, date_stamp="2026-07-28")
    assert result["path"] == package_path / "metadata" / "metadata-edh-hnap.xml"
    assert result["path"].exists()

    dataset_path = package_path / "metadata" / "dataset.csv"
    changed = pd.read_csv(dataset_path)
    changed.loc[0, "description"] = "MISSING METADATA: review this description"
    changed.to_csv(dataset_path, index=False)

    with pytest.raises(ValueError, match="unreviewed"):
        write_edh_xml_from_sdp(package_path)


def test_create_sdp_can_write_a_draft_edh_xml_from_written_metadata(tmp_path):
    with pytest.warns(UserWarning, match="draft"):
        package_path = create_sdp(
            pd.DataFrame({"sample_id": ["a"], "catch_count": [2]}),
            path=tmp_path / "draft-edh-sdp",
            dataset_id="demo",
            table_id="observations",
            seed_semantics=False,
            include_edh_xml=True,
        )

    assert (
        package_path / "metadata" / "metadata-edh-hnap.xml"
    ).exists()
