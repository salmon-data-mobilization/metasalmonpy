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
from metasalmonpy.package_io import _has_value, _is_semantic_code_candidate


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
            # A reviewed package carries real contacts: since the S10 chunk D
            # placeholder convergence the writer fills blank contact fields
            # with MISSING METADATA: prose exactly as metasalmon does, and a
            # package holding placeholders is by definition not reviewed.
            "contact_name": ["Demo Contact"],
            "contact_email": ["demo@example.org"],
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
    # Strict validation now blocks in validate_dictionary() with metasalmon's
    # exact message — REVIEW-prefixed dictionary IRIs abort before the final
    # review sweep, as in R (S10 chunk D differential).
    with pytest.raises(
        ValueError, match="REVIEW-prefixed IRI values remain"
    ):
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


# --- The Q15 pair: what "already exists" means -------------------------------
#
# `test_writer_refuses_to_overwrite_an_unowned_directory` above pins a
# directory that HOLDS SOMETHING. The tests below pin the case that was
# undefined-by-omission on BOTH sides until 2026-08-24 (PARITY.md row 54): an
# existing directory with NOTHING in it. This package wrote into it, metasalmon
# aborted, neither suite tested it, and the divergence rode silently from
# before the 0.1.6 parity claim. Brett's Q15 ruling was "Go with the python
# implementation", so metasalmon adopted this package's guard order -- and this
# package, which had the behaviour but no test, finally pins it.
#
# The definition of "empty" is the whole content of the ruling, so it is
# enumerated rather than implied: empty means `list(target.iterdir())` is
# empty. Three near misses are therefore NOT empty and still need
# `overwrite=True`, one per test below -- a dot-file, a stale sentinel, and an
# empty `data/` subdirectory. metasalmon's `.ms_dir_entries()`
# (`list.files(all.files = TRUE, no.. = TRUE)`) is the same predicate, and its
# twin tests are in `tests/testthat/test-package-helpers.R` under
# "write_salmon_datapackage writes into an existing EMPTY directory".


def test_writer_writes_into_an_existing_empty_directory_without_overwrite(tmp_path):
    resources, dataset, tables, dictionary = _reviewed_artifacts()
    target = tmp_path / "existing-but-empty"
    target.mkdir()
    assert list(target.iterdir()) == []

    package_path = write_salmon_datapackage(
        resources,
        dataset,
        tables,
        dictionary,
        path=target,
        overwrite=False,
    )

    # Not merely "no exception": the package is really there and reads back.
    assert (package_path / "datapackage.json").exists()
    assert (package_path / "data" / "observations.csv").exists()
    assert read_salmon_datapackage(package_path)["dataset"]["dataset_id"].iloc[0] == "demo"


@pytest.mark.parametrize(
    "label, plant",
    [
        # A dot-file. `iterdir()` sees it, so the directory is not empty --
        # matching `list.files(all.files = TRUE)` on the R side.
        ("dotfile-only", lambda p: (p / ".hidden").write_text("x\n", encoding="utf-8")),
        # A stale ownership sentinel from an earlier write.
        (
            "stale-sentinel",
            lambda p: (p / ".metasalmonpy-package").write_text(
                "metasalmonpy-owned\n", encoding="utf-8"
            ),
        ),
        # An empty `data/` subdirectory: the SUBDIRECTORY is empty, the target
        # is not. Emptiness is never recursive.
        ("empty-data-subdir", lambda p: (p / "data").mkdir()),
    ],
)
def test_a_directory_holding_only_this_is_not_empty_for_overwrite(tmp_path, label, plant):
    resources, dataset, tables, dictionary = _reviewed_artifacts()
    target = tmp_path / label
    target.mkdir()
    plant(target)

    with pytest.raises(FileExistsError, match="already exists"):
        write_salmon_datapackage(
            resources,
            dataset,
            tables,
            dictionary,
            path=target,
            overwrite=False,
        )


def test_create_sdp_writes_into_an_existing_empty_directory_without_overwrite(tmp_path):
    target = tmp_path / "empty-create-sdp"
    target.mkdir()

    package_path = create_sdp(
        pd.DataFrame({"sample_id": ["a"], "catch_count": [2]}),
        path=target,
        dataset_id="demo",
        table_id="observations",
        seed_semantics=False,
        overwrite=False,
    )

    assert (package_path / "datapackage.json").exists()


# --- Q16: which slots the deterministic prefill may fill, and marking them ---
#
# PARITY.md row 57: this package restricted `create_sdp()`'s prefill to
# variable/property/entity/unit, metasalmon restricted it per-suggestion
# instead. Brett ruled 2026-08-24 ("Yeah lets go the R way"), so the role
# filter is gone from the deterministic path and the two qualifier slots are
# gated by evidence in the column's own text -- and MARKED, which is the
# property the ruling turned on.
#
# The three tests below are one claim each: the gate lets a qualified column
# through, the gate holds an unqualified one back, and a reviewed LLM accept
# still does not reach these two roles.


def _demo_search_fn(role_iris):
    """A `find_terms` stand-in returning one canonical hit per role."""

    def search_fn(query, role=None, sources=None, **kwargs):
        if role not in role_iris:
            return pd.DataFrame()
        label, iri = role_iris[role]
        return pd.DataFrame(
            [
                {
                    "label": label,
                    "iri": iri,
                    "source": "smn",
                    "ontology": "smn",
                    "role": role,
                    "match_type": "label_exact",
                    "definition": f"Demo {role} term",
                    "score": 4.9,
                }
            ]
        )

    return search_fn


_DEMO_ROLE_IRIS = {
    "variable": ("Mean wild spawner count", "https://w3id.org/smn/MeanWildSpawnerCount"),
    "property": ("Abundance", "https://w3id.org/smn/Abundance"),
    "entity": ("Spawner", "https://w3id.org/smn/Spawner"),
    "unit": ("Count", "http://qudt.org/vocab/unit/NUM"),
    "constraint": ("Wild origin", "https://w3id.org/smn/WildOrigin"),
    "statistical_modifier": ("Mean", "https://w3id.org/smn/Mean"),
}


def _seeded_dictionary(tmp_path, monkeypatch, column_name, name, **create_kwargs):
    """Run `create_sdp()` with retrieval stubbed, and return what it wrote.

    `package_io.create_sdp` imports `suggest_semantics` inside the function
    body, so replacing it on the module takes effect at call time. Binding
    `search_fn` through `functools.partial` is the sanctioned injection point
    (metasalmon reaches the same place by mocking `find_terms`).
    """
    import functools

    from metasalmonpy import semantics as sem

    monkeypatch.setattr(
        sem,
        "suggest_semantics",
        functools.partial(sem.suggest_semantics, search_fn=_demo_search_fn(_DEMO_ROLE_IRIS)),
    )
    package_path = create_sdp(
        {"escapement": pd.DataFrame({"stream_name": ["Alpha", "Beta"], column_name: [120, 340]})},
        path=tmp_path / name,
        dataset_id="q16-demo",
        seed_semantics=True,
        seed_verbose=False,
        check_updates=False,
        overwrite=True,
        **create_kwargs,
    )
    written = pd.read_csv(package_path / "metadata" / "column_dictionary.csv")
    return written[written["column_name"] == column_name].iloc[0]


def test_create_sdp_prefills_and_marks_constraint_and_statistical_modifier(
    tmp_path, monkeypatch
):
    # `mean_wild_spawner_count` carries both kinds of evidence in its own name:
    # "mean" for the modifier, "wild" for the constraint.
    row = _seeded_dictionary(
        tmp_path, monkeypatch, "mean_wild_spawner_count", "q16-qualified"
    )

    assert row["constraint_iri"] == "REVIEW:https://w3id.org/smn/WildOrigin"
    assert row["statistical_modifier_iri"] == "REVIEW:https://w3id.org/smn/Mean"
    # The four core roles are unchanged by the ruling and still marked.
    assert row["term_iri"] == "REVIEW:https://w3id.org/smn/MeanWildSpawnerCount"
    assert row["unit_iri"] == "REVIEW:http://qudt.org/vocab/unit/NUM"


def test_create_sdp_leaves_the_two_qualifier_slots_empty_without_column_evidence(
    tmp_path, monkeypatch
):
    # Same retrieval, same offered constraint and modifier hits. `catch_count`
    # names neither a qualifier nor an aggregation, so the slot gates hold both
    # back -- this is what makes the prefill evidence-gated rather than wide.
    row = _seeded_dictionary(tmp_path, monkeypatch, "catch_count", "q16-unqualified")

    assert pd.isna(row["constraint_iri"])
    assert pd.isna(row["statistical_modifier_iri"])
    # The core roles still fill, so an empty qualifier slot is the gate
    # working rather than retrieval having returned nothing.
    assert str(row["term_iri"]).startswith("REVIEW:")


def test_llm_auto_apply_still_refuses_the_two_qualifier_roles(tmp_path, monkeypatch):
    """The role split the Q16 port introduced, pinned at the function.

    Dropping the role filter applies to the DETERMINISTIC path only. metasalmon
    keeps the four-role restriction on its LLM path
    (`.ms_create_sdp_llm_auto_apply_roles()`), and porting `roles=None`
    unconditionally would have widened this package past R and re-opened row 57
    in the other direction. Driven through `_auto_apply_package_suggestions()`
    rather than `create_sdp(llm_assess=True)` so no provider contract is
    involved -- the role split is the claim, not the review round trip.
    """
    import functools

    from metasalmonpy import semantics as sem
    from metasalmonpy.package_io import (
        _auto_apply_package_suggestions,
        infer_salmon_datapackage_artifacts,
    )

    monkeypatch.setattr(
        sem,
        "suggest_semantics",
        functools.partial(sem.suggest_semantics, search_fn=_demo_search_fn(_DEMO_ROLE_IRIS)),
    )
    artifacts = infer_salmon_datapackage_artifacts(
        {"escapement": pd.DataFrame({"mean_wild_spawner_count": [120, 340]})},
        dataset_id="q16-demo",
        seed_semantics=True,
        seed_verbose=False,
    )
    suggestions = artifacts["semantic_suggestions"].copy()
    suggestions["llm_selected"] = True
    suggestions["llm_decision"] = "accept"
    suggestions["llm_confidence"] = 0.99
    artifacts["semantic_suggestions"] = suggestions

    _auto_apply_package_suggestions(artifacts, llm_assess=True)
    row = artifacts["dict"].iloc[0]

    assert row["term_iri"] == "REVIEW:https://w3id.org/smn/MeanWildSpawnerCount"
    assert not _has_value(row["constraint_iri"])
    assert not _has_value(row["statistical_modifier_iri"])
