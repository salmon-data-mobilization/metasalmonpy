"""The vendored SDP schema bundle and the contract identifiers it carries.

The bundle under ``data/schema`` and ``data/profiles`` is a verbatim copy of
the ``sdp-0.2.0`` git tag of ``salmon-data-mobilization/smn-data-pkg``, which
is also what metasalmon vendors at its ``v0.1.8`` tag (verified byte-for-byte
when the bundle landed). It is deliberately **not** taken from that
repository's ``main``, which is ``sdp-0.3.0``-shaped and no longer carries
``methods.schema.json`` at all.

These tests are the guard against the two ways a vendored bundle rots: the
files silently disappearing from the wheel, and the code drifting away from
the contract the files describe.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from metasalmonpy import sdp_schema
from metasalmonpy.metadata import SDP_PROFILE_VERSION

REPO_ROOT = Path(__file__).resolve().parent.parent


def _copy_vendored_bundle(destination: Path) -> Path:
    """A writable copy of the vendored bundle, for tampering tests."""

    for relative in list(sdp_schema.SDP_METADATA_SCHEMA_PATHS.values()) + [
        sdp_schema.SDP_PROFILE_PATH,
        sdp_schema.SDP_RULES_PATH,
    ]:
        source = sdp_schema.vendored_path(relative)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return destination


def test_the_profile_version_is_read_from_the_vendored_bundle():
    # Not a constant in Python source: a profile bump is a bundle swap. The
    # 0.1.7 register row recorded exactly this as its retirement condition.
    assert sdp_schema.sdp_profile_version() == "sdp-0.2.0"
    assert SDP_PROFILE_VERSION == sdp_schema.sdp_profile_version()

    rules = sdp_schema.vendored_path(sdp_schema.SDP_RULES_PATH)
    assert rules.read_text(encoding="utf-8").splitlines()[0] == "version: sdp-0.2.0"


def test_a_bundle_with_no_version_key_raises_rather_than_guessing(tmp_path, monkeypatch):
    # A silently stale version would be stamped into every published package.
    # Before the 0.2.0 rung the narrow line scan raised this itself; the check
    # now lives in ``_validate_sdp_schema()``, which is where the remote bundle
    # is checked too, so one rule covers both sources.
    _copy_vendored_bundle(tmp_path)
    rules = tmp_path / sdp_schema.SDP_RULES_PATH
    rules.write_text(
        "\n".join(
            line
            for line in rules.read_text(encoding="utf-8").splitlines()
            if not line.startswith("version:")
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sdp_schema, "_DATA_DIR", tmp_path)
    sdp_schema.reset_schema_cache()
    try:
        with pytest.raises(
            sdp_schema.SdpSchemaError, match="sdp:version and rules version"
        ):
            sdp_schema.sdp_profile_version()
    finally:
        sdp_schema.reset_schema_cache()


def test_the_contract_identifiers_name_the_canonical_host():
    # metasalmon corrected these from the retired dfo-pacific-science
    # organization at v0.1.8. They are stamped into datapackage.json; nothing
    # fetches them, so a stale host is silent until somebody clicks it.
    for url in (
        sdp_schema.SDP_PROFILE_URL,
        sdp_schema.SDP_PUBLIC_SCHEMA_BASE,
        sdp_schema.SDP_RULES_URL,
    ):
        assert url.startswith("https://salmon-data-mobilization.github.io/smn-data-pkg/")
        assert "dfo-pacific-science" not in url


def test_no_file_still_points_smn_data_pkg_at_the_retired_organization():
    """Nothing may still resolve *smn-data-pkg* under ``dfo-pacific-science``.

    Deliberately scoped to the spec repository. Two other old-organization
    references survive on purpose and are logged as out of scope in the S10
    execplan: ``ontology_fetch.py``'s default ontology URL (R is stale in the
    same place and the two paths diverge, so it is a cross-repo coordination
    task) and ``term_requests.GCDFO_REPO`` (it matches current R and stays in
    lockstep until either repo moves first). A blanket ban on the string would
    have to exempt both, and an exemption list with no expiry is how a guard
    outlives its cause -- so the guard names the one host it actually owns.

    Retirement condition: widen this to the whole organization name once those
    two references are resolved in their own streams.
    """
    stale = []
    needle = "dfo-pacific-science.github.io/smn-data-pkg"
    candidates = list(REPO_ROOT.glob("*.py"))
    candidates += [
        path
        for path in (REPO_ROOT / "data").rglob("*")
        if path.is_file() and path.suffix in (".json", ".yaml", ".csv", ".md")
    ]
    for path in candidates:
        if needle in path.read_text(encoding="utf-8"):
            stale.append(str(path.relative_to(REPO_ROOT)))
    assert stale == []


def test_the_vendored_bundle_declares_the_canonical_urls():
    profile = json.loads(
        sdp_schema.vendored_path(sdp_schema.SDP_PROFILE_PATH).read_text(
            encoding="utf-8"
        )
    )
    assert profile["$id"] == sdp_schema.SDP_PROFILE_URL
    assert profile["properties"]["profile"]["const"] == sdp_schema.SDP_PROFILE_URL
    assert profile["sdp:rules"] == sdp_schema.SDP_RULES_URL


@pytest.mark.parametrize("table", sorted(sdp_schema.SDP_METADATA_SCHEMA_PATHS))
def test_every_declared_metadata_schema_ships(table):
    path = sdp_schema.vendored_path(sdp_schema.SDP_METADATA_SCHEMA_PATHS[table])
    assert path.is_file(), path
    assert sdp_schema.sdp_schema_field_names(table)


def test_the_bundle_carries_the_0_2_0_extension_schemas():
    # These three are exactly what taking the bundle from ``main`` would have
    # lost: sdp-0.3.0 removes the methods registry.
    for table in ("methods", "observation_structures", "observation_components"):
        assert table in sdp_schema.SDP_METADATA_SCHEMA_PATHS


def test_the_profile_declares_the_optional_extension_resources():
    profile = json.loads(
        sdp_schema.vendored_path(sdp_schema.SDP_PROFILE_PATH).read_text(
            encoding="utf-8"
        )
    )
    resources = {
        resource["name"]: resource for resource in profile["sdp:metadataResources"]
    }
    for name, path in (
        ("sdp_methods", "metadata/methods.csv"),
        (
            "sdp_observation_structures",
            "metadata/structure/observation_structures.csv",
        ),
        (
            "sdp_observation_components",
            "metadata/structure/observation_components.csv",
        ),
    ):
        assert resources[name]["path"] == path
        assert resources[name]["sdp:requirement"] == "optional"


def test_the_bundle_is_declared_as_package_data():
    # Globs in pyproject are the only reason these files reach a wheel, and a
    # missing glob fails at install time, not here -- so assert the glob.
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for glob in (
        "schema/frictionless/metadata/*.json",
        "schema/*.yaml",
        "profiles/salmon-data-package/v0.2/*.json",
    ):
        assert glob in text, glob


def test_bundled_demos_separate_abundance_semantics_from_the_counting_unit():
    """The bundled demo dictionary must not confuse a unit with a property.

    metasalmon 0.1.8 corrected this row: the values are expressed in QUDT
    ``Individual`` while ``property_iri`` names the released Salmon Domain
    Ontology ``smn:Abundance`` characteristic. The former ``property_iri``,
    QUDT ``NumberOfOrganisms``, **does not exist** -- and a counting unit is
    not a substitute for the ecological property being measured. This demo is
    copied by users and fed to LLMs as context, so a wrong IRI here propagates.
    """
    import csv

    path = REPO_ROOT / "data" / "column_dictionary.csv"
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    spawners = [
        row for row in rows if row["column_name"] == "NATURAL_SPAWNERS_TOTAL"
    ]
    assert len(spawners) == 1
    assert spawners[0]["unit_iri"] == "https://qudt.org/vocab/unit/INDIV"
    assert spawners[0]["unit_label"] == "Individual"
    assert spawners[0]["property_iri"] == "https://w3id.org/smn/Abundance"
    assert not [row for row in rows if "NumberOfOrganisms" in row["property_iri"]]
    assert not [row for row in rows if row["unit_iri"].endswith("/Each")]


def test_the_reviewed_unit_crosswalk_covers_both_iri_schemes():
    """QUDT IRIs appear with both ``http`` and ``https`` in real dictionaries.

    Covering only one scheme made an otherwise-reviewed unit unresolvable at
    EML export, which is how the bundled demo could not be exported at all.
    """
    import csv

    path = REPO_ROOT / "data" / "eml-unit-crosswalk.csv"
    with path.open(encoding="utf-8", newline="") as handle:
        rows = {row["unit_iri"]: row for row in csv.DictReader(handle)}

    for iri in (
        "http://qudt.org/vocab/unit/COUNT",
        "https://qudt.org/vocab/unit/COUNT",
        "http://qudt.org/vocab/unit/INDIV",
        "https://qudt.org/vocab/unit/INDIV",
    ):
        assert rows[iri]["eml_standard_unit"] == "number"
        assert rows[iri]["review_status"] == "reviewed"
        assert rows[iri]["profile_version"] == "2"


# --- the remote loader (metasalmon 0.2.0/0.2.1) ----------------------------


def _vendored_bundle_documents():
    """The bundle as ``_fetch_remote_sdp_schema`` would return it."""
    return sdp_schema._load_vendored_sdp_schema()


def test_the_remote_base_url_is_pinned_to_the_spec_tag_not_to_main():
    """metasalmon defaults to ``main``; this package cannot.

    ``main`` was ``sdp-0.2.0``-shaped when metasalmon 0.2.1 shipped and is
    ``sdp-0.3.0``-shaped now, so following the default literally would fetch a
    bundle from a spec era this package does not implement, fail validation,
    and fall back to the vendored copy on every call. Retirement condition: the
    pin moves to ``sdp-0.3.0`` at the 0.3.0 rung, with the vendored bundle.
    """
    assert sdp_schema.SDP_SPEC_TAG == "sdp-0.2.0"
    assert sdp_schema.DEFAULT_SDP_SCHEMA_BASE_URL.endswith("/" + sdp_schema.SDP_SPEC_TAG)
    assert sdp_schema.sdp_profile_version() == sdp_schema.SDP_SPEC_TAG


def test_the_base_url_and_source_are_read_at_call_time(monkeypatch):
    """An import-time read cannot be changed by the caller who imports it."""
    sdp_schema.set_sdp_schema_source(None)
    monkeypatch.setenv("METASALMONPY_SDP_SCHEMA_BASE_URL", "https://example.org/bundle")
    assert sdp_schema.default_sdp_schema_base_url() == "https://example.org/bundle"
    monkeypatch.delenv("METASALMONPY_SDP_SCHEMA_BASE_URL")
    assert sdp_schema.default_sdp_schema_base_url().endswith("/sdp-0.2.0")
    monkeypatch.setenv("METASALMONPY_SDP_SCHEMA_SOURCE", "vendored")
    assert sdp_schema.default_sdp_schema_source() == "vendored"


def test_a_successful_remote_fetch_is_used_and_cached():
    """The gap metasalmon 0.2.0's NEWS records: nothing exercised a success."""
    calls = []

    def fetcher(base_url, timeout):
        calls.append((base_url, timeout))
        return _vendored_bundle_documents()

    sdp_schema.set_sdp_schema_source("auto")
    try:
        schema = sdp_schema.load_sdp_schema(fetch_fn=fetcher)
        assert schema["source"] == "remote"
        assert schema["version"] == "sdp-0.2.0"
        assert calls and calls[0][0].endswith("/sdp-0.2.0")
        # Cached per process: a second call does not fetch again.
        sdp_schema.load_sdp_schema(fetch_fn=fetcher)
        assert len(calls) == 1
    finally:
        sdp_schema.set_sdp_schema_source("vendored")


def test_auto_falls_back_to_the_vendored_bundle_with_one_warning():
    def failing(base_url, timeout):
        raise RuntimeError("network down; Authorization: Bearer abcdefghijklmnop")

    sdp_schema.set_sdp_schema_source("auto")
    try:
        with pytest.warns(RuntimeWarning) as recorded:
            schema = sdp_schema.load_sdp_schema(fetch_fn=failing)
        assert schema["source"] == "vendored"
        # Captured external text is redacted before it reaches the warning.
        assert "abcdefghijklmnop" not in str(recorded[0].message)
        assert "[REDACTED]" in str(recorded[0].message)
    finally:
        sdp_schema.set_sdp_schema_source("vendored")


def test_source_remote_aborts_rather_than_silently_using_a_stale_bundle():
    def failing(base_url, timeout):
        raise RuntimeError("404 Not Found")

    with pytest.raises(sdp_schema.SdpSchemaError, match="Unable to load remote"):
        sdp_schema.load_sdp_schema(source="remote", fetch_fn=failing)


def test_identity_is_derived_from_the_bundle_not_asserted_against_a_constant():
    """An upstream ``$id`` migration must be followable, not fatal.

    This is the defect metasalmon 0.2.0 fixed: upstream migrated every profile
    ``$id``, metasalmon compared it against its own constant, and
    ``source="remote"`` aborted while ``"auto"`` silently used a stale bundle.
    """
    moved = _vendored_bundle_documents()
    new_id = "https://example.org/smn-data-pkg/profiles/v0.2/profile.json"
    profile = json.loads(json.dumps(moved["profile"]))
    profile["$id"] = new_id
    profile["properties"]["profile"]["const"] = new_id
    rules = dict(moved["rules"])
    rules["profile"] = new_id
    followed = sdp_schema._validate_sdp_schema(
        {
            "metadata_schemas": moved["metadata_schemas"],
            "profile": profile,
            "rules": rules,
        }
    )
    assert followed["profile_uri"] == new_id


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda p, r: p.__setitem__("$id", "not a URI"), "profile \\$id"),
        (lambda p, r: p.__setitem__("$id", "https://?query"), "profile \\$id"),
        (lambda p, r: r.__setitem__("profile", "https://other.example/p"), "rules profile"),
        (lambda p, r: r.pop("version"), "sdp:version and rules version"),
        (lambda p, r: p.__setitem__("sdp:rules", "not a URI"), "sdp:rules"),
    ],
)
def test_a_self_inconsistent_bundle_is_rejected(mutate, message):
    bundle = _vendored_bundle_documents()
    profile = json.loads(json.dumps(bundle["profile"]))
    rules = dict(bundle["rules"])
    mutate(profile, rules)
    with pytest.raises(sdp_schema.SdpSchemaError, match=message):
        sdp_schema._validate_sdp_schema(
            {
                "metadata_schemas": bundle["metadata_schemas"],
                "profile": profile,
                "rules": rules,
            }
        )


def test_a_consistently_padded_identifier_is_normalised_not_emitted():
    """Comparing the raw value while testing the trimmed one was the bug."""
    bundle = _vendored_bundle_documents()
    padded = "  " + bundle["profile"]["$id"] + "  "
    profile = json.loads(json.dumps(bundle["profile"]))
    profile["$id"] = padded
    profile["properties"]["profile"]["const"] = padded
    rules = dict(bundle["rules"])
    rules["profile"] = padded
    validated = sdp_schema._validate_sdp_schema(
        {
            "metadata_schemas": bundle["metadata_schemas"],
            "profile": profile,
            "rules": rules,
        }
    )
    assert validated["profile_uri"] == padded.strip()


def test_the_rules_scanner_reads_top_level_scalars_without_pyyaml():
    """Core dependencies stay pandas + requests (PARITY.md rows 30 and 34)."""
    scalars = sdp_schema._rules_scalars(
        sdp_schema.vendored_path(sdp_schema.SDP_RULES_PATH).read_text(encoding="utf-8")
    )
    assert scalars["version"] == "sdp-0.2.0"
    assert scalars["profile"] == sdp_schema.SDP_PROFILE_URL
    # Nested keys are not top-level scalars and must not be picked up.
    assert "id" not in scalars
    assert "severity" not in scalars




def test_the_metadata_resources_follow_a_bundle_that_moves_its_schema_urls(monkeypatch):
    """The derivation is load-bearing, not decorative.

    Composing the URL from ``SDP_PUBLIC_SCHEMA_BASE`` gives the same answer as
    reading it out of the vendored bundle, so a test against the vendored
    bundle alone cannot tell the two apart. This moves the bundle's own URLs
    and asserts the core metadata resources follow it.
    """
    from metasalmonpy import package_io

    moved = sdp_schema._load_vendored_sdp_schema()
    profile = json.loads(json.dumps(moved["profile"]))
    for resource in profile["sdp:metadataResources"]:
        resource["schema"] = "https://elsewhere.example/" + resource["name"] + ".json"
    relocated = dict(moved)
    relocated["profile"] = profile
    monkeypatch.setattr(sdp_schema, "load_sdp_schema", lambda **kwargs: relocated)

    entries = package_io._metadata_resource_entries(include_codes=True)
    assert [entry["schema"] for entry in entries] == [
        "https://elsewhere.example/sdp_dataset.json",
        "https://elsewhere.example/sdp_tables.json",
        "https://elsewhere.example/sdp_column_dictionary.json",
        "https://elsewhere.example/sdp_codes.json",
    ]


def test_a_dataset_declaring_a_different_spec_version_warns_and_keeps_both(tmp_path):
    """metasalmon warns rather than silently rewriting the declared version."""

    from metasalmonpy import read_salmon_datapackage, write_salmon_datapackage

    source = Path(__file__).resolve().parent / "data" / "resource_types" / "r-package"
    package = read_salmon_datapackage(str(source))
    package["dataset"]["spec_version"] = "sdp-0.1.0"
    destination = tmp_path / "declared"
    with pytest.warns(UserWarning, match="declares 'sdp-0.1.0'"):
        write_salmon_datapackage(
            resources=package["resources"],
            dataset_meta=package["dataset"],
            table_meta=package["tables"],
            dict_df=package["dictionary"],
            codes=package["codes"],
            path=str(destination),
        )
    descriptor = json.loads((destination / "datapackage.json").read_text(encoding="utf-8"))
    assert descriptor["sdp"]["specVersion"] == "sdp-0.2.0"
    dataset = (destination / "metadata" / "dataset.csv").read_text(encoding="utf-8")
    assert "sdp-0.1.0" in dataset
