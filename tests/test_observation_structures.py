"""Measure-specific SDP observation structures (metasalmon 0.1.8 parity).

``tests/data/sdp-extensions/structure-sdp`` is genuine metasalmon **v0.1.8**
output under ``LC_COLLATE=C``: R wrote the paired CSVs from *reversed* row
order, so the committed bytes are the canonical ordering rather than the input
ordering, and this suite rewrites them from Python and asserts the bytes come
back identical -- including ``datapackage.json``.

The extraction expectations in ``expected-observations.json`` are R's
``extract_sdp_observations()`` return value for the same package, dumped as
text so a type difference cannot hide inside a JSON number.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

pd = pytest.importorskip("pandas")

from metasalmonpy import (
    extract_sdp_observations,
    read_sdp_observation_structures,
    validate_salmon_datapackage,
    validate_sdp_observation_structures,
    write_sdp_observation_structures,
)
from metasalmonpy.observation_structures import (
    SDP_OBSERVATION_COMPONENTS_COLUMNS,
    SDP_OBSERVATION_STRUCTURES_COLUMNS,
)
from metasalmonpy.sdp_methods import SdpExtensionError
from metasalmonpy.sdp_schema import sdp_schema_field_names

DATA = Path(__file__).parent / "data" / "sdp-extensions"
CHECKSUMS = json.loads((DATA / "checksums.json").read_text(encoding="utf-8"))
EXPECTED_OBSERVATIONS = json.loads(
    (DATA / "expected-observations.json").read_text(encoding="utf-8")
)


def _sdp(tmp_path: Path) -> Path:
    target = tmp_path / "structure-sdp"
    shutil.copytree(DATA / "structure-sdp", target)
    return target


def _read_data(root: Path) -> pd.DataFrame:
    from metasalmonpy.metadata import read_sdp_csv

    return read_sdp_csv(root / "data" / "stock_recruit.csv")


def _write_data(root: Path, frame: pd.DataFrame) -> None:
    frame.to_csv(root / "data" / "stock_recruit.csv", index=False, na_rep="")


def test_the_column_tuples_are_the_vendored_profile_contract():
    assert list(SDP_OBSERVATION_STRUCTURES_COLUMNS) == sdp_schema_field_names(
        "observation_structures"
    )
    assert list(SDP_OBSERVATION_COMPONENTS_COLUMNS) == sdp_schema_field_names(
        "observation_components"
    )


@pytest.mark.parametrize(
    "name",
    [
        "structure-sdp/metadata/structure/observation_structures.csv",
        "structure-sdp/metadata/structure/observation_components.csv",
        "structure-sdp/datapackage.json",
    ],
)
def test_the_committed_fixtures_are_unmodified_r_output(name):
    digest = hashlib.sha256((DATA / name).read_bytes()).hexdigest()
    assert digest == CHECKSUMS["sha256"][name]


def test_reads_r_written_structures_in_canonical_order(tmp_path):
    root = _sdp(tmp_path)
    metadata = read_sdp_observation_structures(root)

    structures = metadata["structures"]
    components = metadata["components"]
    assert list(structures.columns) == list(SDP_OBSERVATION_STRUCTURES_COLUMNS)
    assert list(components.columns) == list(SDP_OBSERVATION_COMPONENTS_COLUMNS)
    assert list(structures["observation_structure_id"]) == [
        "recruits_by_age",
        "total_spawners_by_brood",
    ]
    assert list(components["component_order"][:6]) == [1, 2, 3, 4, 5, 6]
    assert components["component_order"].map(type).eq(int).all()
    assert components["required_when_observed"].map(type).eq(bool).all()
    assert validate_sdp_observation_structures(root) is True


def test_rewriting_from_python_reproduces_r_bytes_exactly(tmp_path):
    root = _sdp(tmp_path)
    before = {
        name: (root / name).read_bytes()
        for name in (
            "metadata/structure/observation_structures.csv",
            "metadata/structure/observation_components.csv",
            "datapackage.json",
        )
    }
    metadata = read_sdp_observation_structures(root)

    # Reversed input order: what comes back must be the canonical order, not
    # the order it was handed.
    write_sdp_observation_structures(
        root,
        metadata["structures"].iloc[::-1].reset_index(drop=True),
        metadata["components"].iloc[::-1].reset_index(drop=True),
        overwrite=True,
    )

    for name, payload in before.items():
        assert (root / name).read_bytes() == payload, name


def test_the_writer_returns_both_managed_paths(tmp_path):
    root = _sdp(tmp_path)
    metadata = read_sdp_observation_structures(root)
    written = write_sdp_observation_structures(
        root, metadata["structures"], metadata["components"], overwrite=True
    )
    assert set(written) == {"structures", "components"}
    assert Path(written["structures"]).name == "observation_structures.csv"


def test_supplying_neither_table_is_an_explicit_no_op(tmp_path):
    root = _sdp(tmp_path)
    assert write_sdp_observation_structures(root) is None


def test_supplying_only_one_table_is_an_error(tmp_path):
    root = _sdp(tmp_path)
    metadata = read_sdp_observation_structures(root)
    with pytest.raises(SdpExtensionError, match="supplied together"):
        write_sdp_observation_structures(root, metadata["structures"])


def test_the_files_must_be_present_together(tmp_path):
    root = _sdp(tmp_path)
    (root / "metadata" / "structure" / "observation_components.csv").unlink()
    with pytest.raises(SdpExtensionError, match="present together"):
        read_sdp_observation_structures(root)


def test_writing_over_an_existing_pair_needs_overwrite(tmp_path):
    root = _sdp(tmp_path)
    metadata = read_sdp_observation_structures(root)
    with pytest.raises(FileExistsError, match="overwrite"):
        write_sdp_observation_structures(
            root, metadata["structures"], metadata["components"]
        )


def _components(root: Path) -> pd.DataFrame:
    return read_sdp_observation_structures(root)["components"]


def test_component_order_must_be_unique_and_contiguous(tmp_path):
    root = _sdp(tmp_path)
    metadata = read_sdp_observation_structures(root)
    components = _components(root)
    components.loc[components.index[1], "component_order"] = 1
    with pytest.raises(SdpExtensionError, match="unique"):
        write_sdp_observation_structures(
            root, metadata["structures"], components, overwrite=True
        )

    components = _components(root)
    components.loc[components.index[0], "component_order"] = 9
    with pytest.raises(SdpExtensionError, match="contiguous"):
        write_sdp_observation_structures(
            root, metadata["structures"], components, overwrite=True
        )


def test_a_measure_must_bind_a_measurement_column(tmp_path):
    root = _sdp(tmp_path)
    from metasalmonpy.metadata import read_sdp_csv

    dictionary_path = root / "metadata" / "column_dictionary.csv"
    dictionary = read_sdp_csv(dictionary_path)
    dictionary.loc[
        dictionary["column_name"] == "recruits", "column_role"
    ] = "attribute"
    dictionary.to_csv(dictionary_path, index=False, na_rep="")
    metadata = read_sdp_observation_structures(root, validate=False)
    with pytest.raises(SdpExtensionError, match="measure component"):
        write_sdp_observation_structures(
            root, metadata["structures"], metadata["components"], overwrite=True
        )


def test_measures_and_dimensions_must_be_required_when_observed(tmp_path):
    root = _sdp(tmp_path)
    metadata = read_sdp_observation_structures(root)
    components = metadata["components"]
    mask = (components["observation_structure_id"] == "recruits_by_age") & (
        components["column_name"] == "age"
    )
    components.loc[mask, "required_when_observed"] = False
    with pytest.raises(SdpExtensionError, match="required_when_observed"):
        write_sdp_observation_structures(
            root, metadata["structures"], components, overwrite=True
        )


def test_every_measurement_column_must_be_bound_as_a_measure(tmp_path):
    # A partial inventory would leave a consumer unable to tell whether an
    # omitted measure shares the table grain or was simply forgotten.
    root = _sdp(tmp_path)
    metadata = read_sdp_observation_structures(root)
    structures = metadata["structures"]
    components = metadata["components"]
    keep = structures["observation_structure_id"] == "recruits_by_age"
    with pytest.raises(SdpExtensionError, match="every measurement column"):
        write_sdp_observation_structures(
            root,
            structures[keep],
            components[components["observation_structure_id"] == "recruits_by_age"],
            overwrite=True,
        )


def test_static_procedures_require_registry_rows(tmp_path):
    root = _sdp(tmp_path)
    metadata = read_sdp_observation_structures(root)
    (root / "metadata" / "methods.csv").unlink()
    with pytest.raises(SdpExtensionError, match="Static procedure references"):
        write_sdp_observation_structures(
            root, metadata["structures"], metadata["components"], overwrite=True
        )


def test_row_varying_procedure_codes_resolve_to_registered_methods(tmp_path):
    root = _sdp(tmp_path)
    from metasalmonpy.metadata import read_sdp_csv

    codes_path = root / "metadata" / "codes.csv"
    codes = read_sdp_csv(codes_path)
    codes.loc[
        codes["code_value"] == "mark_recapture", "term_iri"
    ] = "https://example.org/methods/not-registered"
    codes.to_csv(codes_path, index=False, na_rep="")
    with pytest.raises(SdpExtensionError, match="unregistered method"):
        validate_sdp_observation_structures(root)

    codes.loc[codes["code_value"] == "mark_recapture", "term_iri"] = ""
    codes.to_csv(codes_path, index=False, na_rep="")
    with pytest.raises(SdpExtensionError, match="resolve through exactly one"):
        validate_sdp_observation_structures(root)


def test_an_enumerated_but_unobserved_code_must_still_resolve(tmp_path):
    # An allowed value nobody has used yet is still a promise about what the
    # column may contain; deferring the check to first use is how an
    # unresolvable code reaches a published package.
    root = _sdp(tmp_path)
    from metasalmonpy.metadata import read_sdp_csv

    codes_path = root / "metadata" / "codes.csv"
    codes = read_sdp_csv(codes_path)
    extra = codes.iloc[[0]].copy()
    extra["code_value"] = "unused_method"
    extra["code_label"] = "Unused method"
    extra["code_description"] = "Enumerated but absent from current data rows"
    extra["term_iri"] = "https://example.org/methods/not-registered"
    pd.concat([codes, extra], ignore_index=True).to_csv(
        codes_path, index=False, na_rep=""
    )
    with pytest.raises(SdpExtensionError, match="unregistered"):
        validate_sdp_observation_structures(root)


def test_repeated_observations_at_one_grain_must_be_invariant(tmp_path):
    root = _sdp(tmp_path)
    data = _read_data(root)
    data.loc[data.index[1], "total_spawners"] = "999"
    _write_data(root, data)
    with pytest.raises(SdpExtensionError, match="not invariant"):
        validate_sdp_observation_structures(root)


def test_grain_comparison_honours_the_declared_value_type(tmp_path):
    # "02019" and "2019" are the same integer dimension value, so these two
    # rows are one grain and their conflicting measures must be caught. A
    # string comparison would call them two grains and pass.
    root = _sdp(tmp_path)
    data = _read_data(root)
    data["brood_year"] = ["02019", "2019", "2020"]
    data.loc[data.index[1], "total_spawners"] = "101"
    _write_data(root, data)
    with pytest.raises(SdpExtensionError, match="not invariant"):
        validate_sdp_observation_structures(root)


def test_a_required_component_may_be_empty_where_the_measure_is_absent(tmp_path):
    root = _sdp(tmp_path)
    data = _read_data(root)
    data.loc[data.index[0], "age"] = ""
    _write_data(root, data)
    with pytest.raises(SdpExtensionError, match="required observation component"):
        validate_sdp_observation_structures(root)

    # Clearing the measure too makes the row simply not an observation of that
    # structure, so the empty dimension is no longer a contradiction.
    data.loc[data.index[0], "recruits"] = ""
    _write_data(root, data)
    assert validate_sdp_observation_structures(root) is True


def _as_r_character(value: object) -> str:
    """Render one cell the way R's ``as.character()`` would.

    The fixture was dumped through ``as.character()``, which prints a
    whole-valued double as ``40`` where Python's ``str()`` gives ``40.0``.
    That is a rendering difference in the *fixture dump*, not in the data, so
    normalizing it here keeps the comparison about values.
    """
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return str(value)


def test_extraction_matches_r_exactly(tmp_path):
    root = _sdp(tmp_path)
    observations = extract_sdp_observations(root)

    assert list(observations) == list(EXPECTED_OBSERVATIONS)
    for name, expected in EXPECTED_OBSERVATIONS.items():
        frame = observations[name]
        assert list(frame.columns) == list(expected), name
        assert len(frame) == len(next(iter(expected.values()))), name
        for column, values in expected.items():
            actual = [_as_r_character(value) for value in frame[column]]
            assert actual == list(values), f"{name}.{column}"


def test_extraction_casts_through_the_dictionary_value_type(tmp_path):
    root = _sdp(tmp_path)
    frame = extract_sdp_observations(root)["stock_recruit::recruits_by_age"]
    assert str(frame["brood_year"].dtype).startswith("int")
    assert str(frame["recruits"].dtype).startswith("float")


def test_extraction_selectors(tmp_path):
    root = _sdp(tmp_path)
    selected = extract_sdp_observations(
        root,
        table_id="stock_recruit",
        observation_structure_id="total_spawners_by_brood",
    )
    assert list(selected) == ["stock_recruit::total_spawners_by_brood"]
    with pytest.raises(SdpExtensionError, match="No observation structure matches"):
        extract_sdp_observations(root, observation_structure_id="not_here")


def test_package_validation_includes_the_optional_extensions(tmp_path):
    root = _sdp(tmp_path)
    assert validate_salmon_datapackage(str(root), require_iris=False)["package"]

    data = _read_data(root)
    data.loc[data.index[1], "total_spawners"] = "999"
    _write_data(root, data)
    with pytest.raises(SdpExtensionError, match="not invariant"):
        validate_salmon_datapackage(str(root), require_iris=False)


def test_a_package_without_the_extensions_keeps_the_historic_path(tmp_path):
    root = _sdp(tmp_path)
    shutil.rmtree(root / "metadata" / "structure")
    (root / "metadata" / "methods.csv").unlink()
    from metasalmonpy.metadata import read_sdp_csv

    dictionary_path = root / "metadata" / "column_dictionary.csv"
    dictionary = read_sdp_csv(dictionary_path)
    dictionary["method_iri"] = ""
    dictionary.to_csv(dictionary_path, index=False, na_rep="")
    # No extension present: validation must behave exactly as it did before
    # 0.1.8, not merely "not crash".
    assert validate_salmon_datapackage(str(root), require_iris=False)["package"]


def test_a_failed_multi_file_write_rolls_the_whole_set_back(tmp_path):
    root = _sdp(tmp_path)
    metadata = read_sdp_observation_structures(root)
    before = {
        name: (root / name).read_bytes()
        for name in (
            "metadata/structure/observation_structures.csv",
            "metadata/structure/observation_components.csv",
        )
    }
    descriptor = root / "datapackage.json"
    descriptor.write_text("{ malformed descriptor", encoding="utf-8")
    before_descriptor = descriptor.read_bytes()

    structures = metadata["structures"].copy()
    structures.loc[structures.index[0], "structure_label"] = "Changed label"
    with pytest.raises(SdpExtensionError, match="Could not parse"):
        write_sdp_observation_structures(
            root, structures, metadata["components"], overwrite=True
        )

    for name, payload in before.items():
        assert (root / name).read_bytes() == payload, name
    assert descriptor.read_bytes() == before_descriptor


def test_a_symlinked_structure_file_is_refused(tmp_path):
    root = _sdp(tmp_path)
    outside = tmp_path / "outside.csv"
    outside.write_text("outside\n", encoding="utf-8")
    target = root / "metadata" / "structure" / "observation_components.csv"
    target.unlink()
    try:
        target.symlink_to(outside)
    except OSError:  # pragma: no cover - filesystem without symlinks
        pytest.skip("Filesystem does not permit symlink creation")
    with pytest.raises(SdpExtensionError, match="symlink"):
        read_sdp_observation_structures(root)
    assert outside.read_text(encoding="utf-8") == "outside\n"
