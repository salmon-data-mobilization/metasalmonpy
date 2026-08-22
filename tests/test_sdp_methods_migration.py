"""``migrate_sdp_methods()``: sdp-0.2.0 -> sdp-0.3.0 method relocation.

This file is the pytest port of metasalmon's ``tests/testthat/test-sdp-methods.R``
as it stands on the post-0.3.0 ``main`` tree (the v0.3.0 release suite plus the
dry-run stop-parity regression test and the rollback-backup regression test).
Per the S10 execplan's logged decision, the FINAL R fixture suite lands as
pytest BEFORE the migration code, so every behaviour below is R's, established
from R's own test suite and re-verified by running both implementations over
the same inputs — not from reading R source alone.

sdp-0.3.0 removed the ``metadata/methods.csv`` registry, R's registry APIs
(``write_sdp_methods`` / ``read_sdp_methods`` / ``validate_sdp_methods``), and
the column-dictionary ``method_iri`` field. On the R side the migration tool is
the only surviving methods API; here the legacy *reader* additionally survives
(PARITY.md row 9) so R-0.2.x-written packages stay readable. Fixtures are made
with the current writers and then hand-edited back into the legacy sdp-0.2.0
shape the migration accepts as input, exactly as the R suite does.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pd = pytest.importorskip("pandas")

from metasalmonpy import create_sdp, migrate_sdp_methods, validate_salmon_datapackage
from metasalmonpy.sdp_methods import (
    SdpExtensionError,
    _atomic_write_set,
    _json_bytes,
)

LEGACY_V02_PROFILE = (
    "https://salmon-data-mobilization.github.io/smn-data-pkg/"
    "profiles/salmon-data-package/v0.2/profile.json"
)
V03_PROFILE = (
    "https://salmon-data-mobilization.github.io/smn-data-pkg/"
    "profiles/salmon-data-package/v0.3/profile.json"
)
RULES_URL = (
    "https://salmon-data-mobilization.github.io/smn-data-pkg/schema/sdp.rules.yaml"
)


def read_file_bytes(path: Path) -> bytes:
    return Path(path).read_bytes()


def migration_metadata_paths(root: Path) -> dict:
    return {
        "tables": root / "metadata" / "tables.csv",
        "dictionary": root / "metadata" / "column_dictionary.csv",
        "dataset": root / "metadata" / "dataset.csv",
        "descriptor": root / "datapackage.json",
        "registry": root / "metadata" / "methods.csv",
    }


def snapshot(paths: dict) -> dict:
    return {
        name: read_file_bytes(path) if path.exists() else None
        for name, path in paths.items()
    }


def legacy_registry_rows() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "dataset_id": ["methods-test"],
            "method_iri": ["https://ex.org/m/mark-recapture"],
            "method_label": ["Mark-recapture estimate"],
            "method_description": [
                "Estimates abundance from marked and recaptured fish."
            ],
            "method_version": ["2026"],
            "protocol_iri": ["https://ex.org/protocols/mark-recapture"],
            "citation": ["Example Program. 2026."],
        }
    )


def make_migration_test_sdp(path: Path) -> Path:
    # A real, current package with two measurement columns in one table, so
    # per-table method agreement and disagreement are both expressible.
    resources = {
        "stock_recruit": pd.DataFrame(
            {
                "stock_id": ["fraser", "fraser"],
                "brood_year": [2019, 2020],
                "abundance": [100.0, 120.0],
                "density": [0.5, 0.6],
            }
        )
    }
    create_sdp(
        resources,
        path=str(path),
        dataset_id="methods-test",
        seed_semantics=False,
        seed_verbose=False,
        check_updates=False,
        overwrite=True,
    )
    return path


def _read_character_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[])


def add_legacy_dictionary_methods(root: Path, bindings: dict) -> Path:
    """Hand-add the legacy dictionary ``method_iri`` column.

    ``bindings`` maps column_name -> method IRI; unbound columns stay blank.
    """
    dictionary_path = root / "metadata" / "column_dictionary.csv"
    dictionary = _read_character_csv(dictionary_path)
    dictionary["method_iri"] = [
        bindings.get(name, "") for name in dictionary["column_name"]
    ]
    dictionary.to_csv(dictionary_path, index=False)
    return dictionary_path


def add_legacy_registry(root: Path, rows: pd.DataFrame = None) -> Path:
    registry_path = root / "metadata" / "methods.csv"
    if rows is None:
        rows = legacy_registry_rows()
    rows.to_csv(registry_path, index=False)
    return registry_path


def add_legacy_descriptor_state(root: Path, field_methods: dict = None) -> Path:
    """Rewind the descriptor to its sdp-0.2.0 shape.

    A declared registry resource, the metadata pointer, the v0.2 identity, and
    optionally per-field custom ``iAdopt:methodIri`` bindings (``field_methods``
    is column_name -> method IRI).
    """
    descriptor_path = root / "datapackage.json"
    descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
    descriptor["profile"] = LEGACY_V02_PROFILE
    descriptor.setdefault("sdp", {})["specVersion"] = "sdp-0.2.0"
    descriptor["resources"].append(
        {
            "name": "sdp_methods",
            "path": "metadata/methods.csv",
            "profile": "tabular-data-resource",
            "schema": (
                "https://salmon-data-mobilization.github.io/smn-data-pkg/"
                "schema/frictionless/metadata/methods.schema.json"
            ),
        }
    )
    descriptor["sdp"].setdefault("metadata", {})["methods"] = "metadata/methods.csv"
    if field_methods is not None:
        for resource in descriptor["resources"]:
            schema = resource.get("schema")
            if not isinstance(schema, dict):
                continue
            for field in schema.get("fields") or []:
                name = field.get("name")
                if name in field_methods:
                    custom = field.get("custom") or {}
                    custom["iAdopt:methodIri"] = field_methods[name]
                    field["custom"] = custom
    descriptor_path.write_bytes(_json_bytes(descriptor))
    return descriptor_path


def set_dataset_spec_version(root: Path, version: str) -> Path:
    dataset_path = root / "metadata" / "dataset.csv"
    dataset = _read_character_csv(dataset_path)
    dataset["spec_version"] = version
    dataset.to_csv(dataset_path, index=False)
    return dataset_path


def test_an_agreeing_legacy_package_migrates_end_to_end(tmp_path):
    root = make_migration_test_sdp(tmp_path / "sdp")
    set_dataset_spec_version(root, "sdp-0.2.0")
    add_legacy_dictionary_methods(
        root,
        {
            "abundance": "https://ex.org/m/mark-recapture",
            "density": "https://ex.org/m/mark-recapture",
        },
    )
    add_legacy_registry(root)
    add_legacy_descriptor_state(root)

    report = migrate_sdp_methods(root)

    assert set(report) == {"tables", "dropped_review", "registry"}
    assert list(report["tables"]["table_id"]) == ["stock_recruit"]
    assert list(report["tables"]["method_iri"]) == ["https://ex.org/m/mark-recapture"]
    assert len(report["dropped_review"]) == 0
    assert list(report["registry"]["method_iri"]) == ["https://ex.org/m/mark-recapture"]

    # tables.csv carries the relocated table-constant method in v0.3 order.
    tables = _read_character_csv(root / "metadata" / "tables.csv")
    assert list(tables.columns) == [
        "dataset_id",
        "table_id",
        "file_name",
        "table_label",
        "description",
        "observation_unit",
        "observation_unit_iri",
        "primary_key",
        "protocol_iri",
        "protocol_citation",
        "method_iri",
    ]
    assert (
        tables.loc[tables["table_id"] == "stock_recruit", "method_iri"].tolist()
        == ["https://ex.org/m/mark-recapture"]
    )

    # The dictionary lost method_iri and is aligned to the v0.3 order ending
    # in statistical_modifier_iri.
    dictionary = _read_character_csv(root / "metadata" / "column_dictionary.csv")
    assert list(dictionary.columns) == [
        "dataset_id",
        "table_id",
        "column_name",
        "column_label",
        "column_description",
        "column_role",
        "value_type",
        "required",
        "unit_label",
        "unit_iri",
        "term_iri",
        "term_type",
        "property_iri",
        "entity_iri",
        "constraint_iri",
        "statistical_modifier_iri",
    ]

    # dataset.csv advances the spec pin.
    dataset = _read_character_csv(root / "metadata" / "dataset.csv")
    assert dataset["spec_version"].tolist() == ["sdp-0.3.0"]

    # The descriptor loses the registry resource and pointer and carries the
    # v0.3 identity.
    descriptor = json.loads((root / "datapackage.json").read_text(encoding="utf-8"))
    resource_names = [resource.get("name", "") for resource in descriptor["resources"]]
    assert "sdp_methods" not in resource_names
    resource_paths = [resource.get("path", "") for resource in descriptor["resources"]]
    assert "metadata/methods.csv" not in resource_paths
    assert "methods" not in (descriptor.get("sdp", {}).get("metadata") or {})
    assert descriptor["profile"] == V03_PROFILE
    assert descriptor["sdp"]["specVersion"] == "sdp-0.3.0"
    assert descriptor["sdp"]["rules"] == RULES_URL

    # The registry file itself is gone, and the migrated package validates.
    assert not (root / "metadata" / "methods.csv").exists()
    validate_salmon_datapackage(root, require_iris=False)

    # A successful migration leaves no scratch behind. The rollback path
    # deliberately preserves a backup when a restore fails, so the success
    # path needs its own assertion that it does not.
    leftovers = [
        name
        for name in os.listdir(root / "metadata")
        if name.startswith(".") and ("backup" in name or "stage" in name)
    ]
    assert leftovers == []


def test_method_disagreement_stops_the_migration_with_nothing_changed(tmp_path):
    root = make_migration_test_sdp(tmp_path / "sdp")
    add_legacy_dictionary_methods(
        root,
        {"abundance": "https://ex.org/m/a", "density": "https://ex.org/m/b"},
    )
    add_legacy_registry(root)
    add_legacy_descriptor_state(root)

    paths = migration_metadata_paths(root)
    before = snapshot(paths)

    with pytest.raises(SdpExtensionError, match="disagree about their table's method") as excinfo:
        migrate_sdp_methods(root)
    # Stop AND report: the abort lists every conflicting IRI.
    assert "https://ex.org/m/a" in str(excinfo.value)
    assert "https://ex.org/m/b" in str(excinfo.value)

    assert snapshot(paths) == before
    assert paths["registry"].exists()


def test_an_existing_tables_csv_method_claim_that_disagrees_also_stops(tmp_path):
    root = make_migration_test_sdp(tmp_path / "sdp")
    add_legacy_dictionary_methods(
        root,
        {"abundance": "https://ex.org/m/a", "density": "https://ex.org/m/a"},
    )
    add_legacy_registry(root)

    tables_path = root / "metadata" / "tables.csv"
    tables = _read_character_csv(tables_path)
    if "method_iri" not in tables.columns:
        tables["method_iri"] = ""
    tables["method_iri"] = "https://ex.org/m/already-claimed"
    tables.to_csv(tables_path, index=False)

    paths = migration_metadata_paths(root)
    before = snapshot(paths)

    with pytest.raises(SdpExtensionError, match="tables.csv already claims") as excinfo:
        migrate_sdp_methods(root)
    assert "https://ex.org/m/already-claimed" in str(excinfo.value)
    assert "https://ex.org/m/a" in str(excinfo.value)

    assert snapshot(paths) == before


def test_unresolved_review_bindings_are_dropped_and_reported_not_migrated(tmp_path):
    root = make_migration_test_sdp(tmp_path / "sdp")
    add_legacy_dictionary_methods(root, {"abundance": "REVIEW: https://ex.org/m/a"})
    add_legacy_registry(root)

    report = migrate_sdp_methods(root)

    assert len(report["tables"]) == 0
    assert len(report["dropped_review"]) == 1
    assert list(report["dropped_review"]["column_name"]) == ["abundance"]
    assert list(report["dropped_review"]["method_iri"]) == ["REVIEW: https://ex.org/m/a"]

    # Nothing was placed at the table level, the dictionary slot is gone, and
    # the registry was still removed.
    tables = _read_character_csv(root / "metadata" / "tables.csv")
    assert all(value == "" for value in tables["method_iri"])
    dictionary = _read_character_csv(root / "metadata" / "column_dictionary.csv")
    assert "method_iri" not in dictionary.columns
    assert not (root / "metadata" / "methods.csv").exists()


def test_dry_run_reports_the_migration_without_touching_any_file(tmp_path, capsys):
    root = make_migration_test_sdp(tmp_path / "sdp")
    set_dataset_spec_version(root, "sdp-0.2.0")
    add_legacy_dictionary_methods(
        root,
        {
            "abundance": "https://ex.org/m/mark-recapture",
            "density": "https://ex.org/m/mark-recapture",
        },
    )
    add_legacy_registry(root)
    add_legacy_descriptor_state(root)

    paths = migration_metadata_paths(root)
    before = snapshot(paths)

    report = migrate_sdp_methods(root, dry_run=True)
    assert "Dry run: no files were changed." in capsys.readouterr().out

    assert list(report["tables"]["table_id"]) == ["stock_recruit"]
    assert list(report["tables"]["method_iri"]) == ["https://ex.org/m/mark-recapture"]
    assert snapshot(paths) == before
    assert paths["registry"].exists()


def test_descriptor_only_iadopt_method_iri_bindings_migrate(tmp_path):
    # A descriptor-first sdp-0.2.0 package bound methods through the per-field
    # custom key with no dictionary method_iri column and no registry.
    root = make_migration_test_sdp(tmp_path / "sdp")
    add_legacy_descriptor_state(
        root,
        field_methods={
            "abundance": "https://ex.org/m/expanded-count",
            "density": "https://ex.org/m/expanded-count",
        },
    )
    # The descriptor still declared the registry resource; the file itself was
    # never written, which the migration must tolerate.
    assert not (root / "metadata" / "methods.csv").exists()

    report = migrate_sdp_methods(root)

    assert list(report["tables"]["table_id"]) == ["stock_recruit"]
    assert list(report["tables"]["method_iri"]) == ["https://ex.org/m/expanded-count"]
    assert report["registry"] is None

    tables = _read_character_csv(root / "metadata" / "tables.csv")
    assert (
        tables.loc[tables["table_id"] == "stock_recruit", "method_iri"].tolist()
        == ["https://ex.org/m/expanded-count"]
    )

    descriptor_text = (root / "datapackage.json").read_text(encoding="utf-8")
    assert "iAdopt:methodIri" not in descriptor_text
    assert "sdp_methods" not in descriptor_text


def test_the_migration_rejects_only_a_symlinked_package_root(tmp_path):
    target = tmp_path / "real-sdp"
    target.mkdir()
    make_migration_test_sdp(target)
    add_legacy_dictionary_methods(
        target,
        {
            "abundance": "https://ex.org/m/mark-recapture",
            "density": "https://ex.org/m/mark-recapture",
        },
    )
    add_legacy_registry(target)
    link_parent = tmp_path / "links"
    link_parent.mkdir()
    linked_root = link_parent / "linked-sdp"
    try:
        linked_root.symlink_to(target)
    except OSError:
        pytest.skip("Filesystem does not permit directory symlink creation")

    with pytest.raises(SdpExtensionError, match="symlink|unsafe"):
        migrate_sdp_methods(linked_root)
    with pytest.raises(SdpExtensionError, match="symlink|unsafe"):
        migrate_sdp_methods(str(linked_root) + "/")
    # The refusal happened before any work: the legacy registry is untouched.
    assert (target / "metadata" / "methods.csv").exists()

    # On macOS the temporary directory is commonly spelled through the harmless
    # /var -> /private/var system alias. Only the supplied package-root entry,
    # not its ancestors, is part of this trust boundary.
    migrate_sdp_methods(target)
    assert not (target / "metadata" / "methods.csv").exists()


def test_a_package_with_nothing_to_migrate_is_reported_as_a_no_op(tmp_path, capsys):
    root = make_migration_test_sdp(tmp_path / "sdp")

    paths = migration_metadata_paths(root)
    del paths["registry"]
    before = snapshot(paths)

    report = migrate_sdp_methods(root)
    assert "Nothing to migrate" in capsys.readouterr().out
    assert len(report["tables"]) == 0
    assert len(report["dropped_review"]) == 0
    assert report["registry"] is None
    assert snapshot(paths) == before


def test_a_package_with_only_review_bindings_still_migrates_to_the_v03_shape(tmp_path):
    root = make_migration_test_sdp(tmp_path / "sdp")
    # Route through the legacy helper like every other legacy test: the
    # fixture is a current package, so 0.3.0 already removed the field.
    add_legacy_dictionary_methods(
        root,
        {
            "abundance": "REVIEW: https://example.org/methods/unresolved",
            "density": "REVIEW: https://example.org/methods/unresolved",
        },
    )

    report = migrate_sdp_methods(root)

    assert len(report["dropped_review"]) > 0
    migrated = _read_character_csv(root / "metadata" / "column_dictionary.csv")
    assert "method_iri" not in migrated.columns
    assert "statistical_modifier_iri" in migrated.columns


def test_migration_aborts_before_any_writes_when_the_descriptor_cannot_be_parsed(
    tmp_path,
):
    root = make_migration_test_sdp(tmp_path / "sdp")
    add_legacy_dictionary_methods(
        root, {"abundance": "https://example.org/methods/weir-count"}
    )
    add_legacy_registry(root)
    (root / "datapackage.json").write_text("{ not json", encoding="utf-8")
    dictionary_path = root / "metadata" / "column_dictionary.csv"
    before = read_file_bytes(dictionary_path)

    with pytest.raises(SdpExtensionError, match="Could not parse"):
        migrate_sdp_methods(root)

    assert read_file_bytes(dictionary_path) == before
    assert (root / "metadata" / "methods.csv").exists()


def test_migration_aborts_before_any_writes_on_a_symlinked_descriptor(tmp_path):
    root = make_migration_test_sdp(tmp_path / "sdp")
    add_legacy_dictionary_methods(
        root, {"abundance": "https://example.org/methods/weir-count"}
    )
    add_legacy_registry(root)
    descriptor_path = root / "datapackage.json"
    real_descriptor = root / "datapackage-real.json"
    descriptor_path.rename(real_descriptor)
    descriptor_path.symlink_to(real_descriptor)

    with pytest.raises(SdpExtensionError, match="symlink"):
        migrate_sdp_methods(root)
    assert (root / "metadata" / "methods.csv").exists()


def test_a_failed_metadata_rewrite_restores_the_methods_registry(tmp_path):
    # The registry is renamed aside before the atomic write set; a rewrite
    # failure must put it back so the package is left exactly as found.
    root = make_migration_test_sdp(tmp_path / "sdp")
    add_legacy_dictionary_methods(
        root,
        {
            "abundance": "https://example.org/methods/weir-count",
            "density": "https://example.org/methods/weir-count",
        },
    )
    add_legacy_registry(root)
    registry_path = root / "metadata" / "methods.csv"
    registry_before = read_file_bytes(registry_path)
    # A symlinked dataset.csv reads fine during the rewrite but makes the
    # atomic writer refuse the replacement, failing the transaction mid-flight.
    dataset_path = root / "metadata" / "dataset.csv"
    real_dataset = root / "metadata" / "dataset-real.csv"
    dataset_path.rename(real_dataset)
    dataset_path.symlink_to(real_dataset)

    with pytest.raises(SdpExtensionError):
        migrate_sdp_methods(root)

    assert registry_path.exists()
    assert read_file_bytes(registry_path) == registry_before


def test_dry_run_rejects_non_logical_input_instead_of_migrating(tmp_path):
    # R's isTRUE(1) is FALSE, so a truthy non-logical would have taken the
    # destructive branch from a caller who plainly asked for a preview. The
    # same trap exists for Python truthiness, so the same type check applies.
    root = make_migration_test_sdp(tmp_path / "sdp")
    add_legacy_dictionary_methods(
        root, {"abundance": "https://example.org/methods/weir-count"}
    )
    add_legacy_registry(root)

    with pytest.raises(SdpExtensionError, match="must be True or False"):
        migrate_sdp_methods(root, dry_run=1)
    with pytest.raises(SdpExtensionError, match="must be True or False"):
        migrate_sdp_methods(root, dry_run="True")
    assert (root / "metadata" / "methods.csv").exists()


def test_two_carriers_disagreeing_about_one_column_stop_the_migration(tmp_path):
    # The dictionary used to win silently on the R side, which erased the
    # descriptor's IRI from the package while the contract promises
    # stop-and-report.
    root = make_migration_test_sdp(tmp_path / "sdp")
    add_legacy_dictionary_methods(
        root, {"abundance": "https://example.org/methods/weir-count"}
    )
    add_legacy_descriptor_state(
        root,
        field_methods={"abundance": "https://example.org/methods/aerial-survey"},
    )

    with pytest.raises(SdpExtensionError, match="two carriers disagree"):
        migrate_sdp_methods(root)
    dictionary = _read_character_csv(root / "metadata" / "column_dictionary.csv")
    assert "method_iri" in dictionary.columns


def test_a_binding_naming_an_undeclared_table_stops_before_any_writes(tmp_path):
    root = make_migration_test_sdp(tmp_path / "sdp")
    dictionary_path = root / "metadata" / "column_dictionary.csv"
    dictionary = _read_character_csv(dictionary_path)
    dictionary["method_iri"] = ""
    dictionary.loc[0, "method_iri"] = "https://example.org/methods/weir-count"
    dictionary.loc[0, "table_id"] = "not_a_declared_table"
    dictionary.to_csv(dictionary_path, index=False)
    before = read_file_bytes(dictionary_path)

    with pytest.raises(SdpExtensionError, match="does not declare"):
        migrate_sdp_methods(root)
    assert read_file_bytes(dictionary_path) == before


def test_the_legacy_reader_refuses_a_symlinked_methods_registry(tmp_path):
    # Kept from the pre-0.3.0 hardening: the migration reads and then deletes
    # this path, so following a symlink would delete through it.
    root = make_migration_test_sdp(tmp_path / "sdp")
    registry_path = root / "metadata" / "methods.csv"
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside = outside_dir / "outside-methods.csv"
    legacy_registry_rows().to_csv(outside, index=False)
    registry_path.symlink_to(outside)

    with pytest.raises(SdpExtensionError, match="symlink"):
        migrate_sdp_methods(root)
    assert outside.exists()


def test_migration_rewrites_the_nested_descriptor_profile_too(tmp_path):
    # The writer emits the profile URI twice (top level and under ``sdp``);
    # updating one leaves a descriptor contradicting itself.
    root = make_migration_test_sdp(tmp_path / "sdp")
    add_legacy_dictionary_methods(
        root,
        {
            "abundance": "https://example.org/methods/weir-count",
            "density": "https://example.org/methods/weir-count",
        },
    )
    add_legacy_registry(root)
    descriptor_path = root / "datapackage.json"
    descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
    descriptor["profile"] = LEGACY_V02_PROFILE
    descriptor.setdefault("sdp", {})["profile"] = LEGACY_V02_PROFILE
    descriptor["sdp"]["specVersion"] = "sdp-0.2.0"
    descriptor_path.write_bytes(_json_bytes(descriptor))

    migrate_sdp_methods(root)

    migrated = json.loads(descriptor_path.read_text(encoding="utf-8"))
    assert "v0.3" in migrated["profile"]
    assert "v0.3" in migrated["sdp"]["profile"]
    assert migrated["sdp"]["specVersion"] == "sdp-0.3.0"


def test_the_placement_report_is_in_canonical_order_regardless_of_input_order(
    tmp_path,
):
    def build(order, name):
        root = make_migration_test_sdp(tmp_path / name)
        dictionary_path = root / "metadata" / "column_dictionary.csv"
        dictionary = _read_character_csv(dictionary_path)
        dictionary["method_iri"] = ""
        dictionary.loc[
            dictionary["column_name"] == "abundance", "method_iri"
        ] = "https://example.org/methods/weir-count"
        dictionary.loc[
            dictionary["column_name"] == "density", "method_iri"
        ] = "https://example.org/methods/weir-count"
        dictionary.iloc[order].to_csv(dictionary_path, index=False)
        return migrate_sdp_methods(root, dry_run=True)["tables"]

    forward = build(list(range(4)), "forward")
    reversed_ = build(list(reversed(range(4))), "reversed")
    assert list(forward["table_id"]) == list(reversed_["table_id"])
    assert list(forward["method_iri"]) == list(reversed_["method_iri"])
    assert list(forward["columns"]) == list(reversed_["columns"])


def test_a_backup_whose_restore_fails_survives_the_cleanup_that_follows(tmp_path):
    # Regression (fixed in metasalmon 0.3.0, mirrored here): the rollback
    # branch used to leave the failed-restore backup on the cleanup list, so
    # the on-exit unlink destroyed the only surviving copy of the original
    # bytes.
    directory = tmp_path / "writes"
    directory.mkdir()
    target = directory / "keep.csv"
    target.write_text("original\n", encoding="utf-8")

    def failing_validate():
        # Make the restore itself fail by replacing the destination with a
        # directory the rename cannot overwrite, then fail validation so the
        # writer rolls back.
        os.unlink(target)
        os.mkdir(target)
        raise RuntimeError("forced validation failure")

    with pytest.raises(RuntimeError, match="forced validation failure"):
        with pytest.warns(UserWarning, match="preserved at"):
            _atomic_write_set({str(target): b"replacement\n"}, validate=failing_validate)

    # The backup is a dot-prefixed sibling in the same directory.
    leftovers = [
        name for name in os.listdir(directory) if name.startswith(".keep.csv-backup-")
    ]
    assert len(leftovers) == 1
    assert (directory / leftovers[0]).read_text(encoding="utf-8") == "original\n"


def test_a_method_bound_to_only_some_measurement_columns_stops_the_migration(
    tmp_path,
):
    # Promotion claims the method for the WHOLE table, so a measurement column
    # with no resolved binding — including one whose binding was dropped as
    # REVIEW: — is a judgement call, not silent agreement.
    root = make_migration_test_sdp(tmp_path / "sdp")
    add_legacy_dictionary_methods(
        root, {"abundance": "https://example.org/methods/weir-count"}
    )
    dictionary_path = root / "metadata" / "column_dictionary.csv"
    before = read_file_bytes(dictionary_path)

    with pytest.raises(SdpExtensionError, match="carries no resolved method binding"):
        migrate_sdp_methods(root)
    assert read_file_bytes(dictionary_path) == before

    # The REVIEW-shadow variant: one resolved, one just dropped.
    root2 = make_migration_test_sdp(tmp_path / "sdp2")
    add_legacy_dictionary_methods(
        root2,
        {
            "abundance": "https://example.org/methods/weir-count",
            "density": "REVIEW: https://example.org/methods/aerial-survey",
        },
    )
    with pytest.raises(SdpExtensionError, match="carries no resolved method binding"):
        migrate_sdp_methods(root2)


def test_dry_run_previews_the_undeclared_table_stop_instead_of_promising_a_migration(
    tmp_path,
):
    # In R the check used to sit after the dry-run return, so a clean preview
    # promised a migration the real run then refused; this suite ports the
    # fixed behaviour (metasalmon main, post-0.3.0), where every stop fires in
    # the dry run as well as the real run.
    root = make_migration_test_sdp(tmp_path / "sdp")
    dictionary_path = root / "metadata" / "column_dictionary.csv"
    dictionary = _read_character_csv(dictionary_path)
    dictionary["method_iri"] = ""
    dictionary.loc[0, "method_iri"] = "https://example.org/methods/weir-count"
    dictionary.loc[0, "table_id"] = "not_a_declared_table"
    dictionary.to_csv(dictionary_path, index=False)

    with pytest.raises(SdpExtensionError, match="does not declare"):
        migrate_sdp_methods(root, dry_run=True)
