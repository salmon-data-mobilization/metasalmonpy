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
from pathlib import Path

import pytest

from metasalmonpy import sdp_schema
from metasalmonpy.metadata import SDP_PROFILE_VERSION

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_the_profile_version_is_read_from_the_vendored_bundle():
    # Not a constant in Python source: a profile bump is a bundle swap. The
    # 0.1.7 register row recorded exactly this as its retirement condition.
    assert sdp_schema.sdp_profile_version() == "sdp-0.2.0"
    assert SDP_PROFILE_VERSION == sdp_schema.sdp_profile_version()

    rules = sdp_schema.vendored_path(sdp_schema.SDP_RULES_PATH)
    assert rules.read_text(encoding="utf-8").splitlines()[0] == "version: sdp-0.2.0"


def test_a_bundle_with_no_version_key_raises_rather_than_guessing(tmp_path, monkeypatch):
    # A silently stale version would be stamped into every published package.
    broken = tmp_path / "schema"
    broken.mkdir()
    (broken / "sdp.rules.yaml").write_text("rules: []\n", encoding="utf-8")
    monkeypatch.setattr(sdp_schema, "_DATA_DIR", tmp_path)
    sdp_schema.sdp_profile_version.cache_clear()
    try:
        with pytest.raises(ValueError, match="no top-level 'version:' key"):
            sdp_schema.sdp_profile_version()
    finally:
        sdp_schema.sdp_profile_version.cache_clear()


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
