"""The vendored SDP schema bundle and the contract identifiers it carries.

The bundle under ``data/schema`` and ``data/profiles`` is a byte-for-byte copy
of the ``sdp-0.3.2`` git tag of ``salmon-data-mobilization/smn-data-pkg``,
which is also what metasalmon vendors since its hub item B-198: the SHA-256 of
every file in ``data/sdp-bundle-manifest.json`` equals the one in metasalmon's
``inst/extdata/sdp-bundle-manifest.json`` (hub item B-199). It is deliberately
taken from the release tag, not from that repository's ``main``: the pin and
the bundle must hold the same bytes, and sdp-0.3.0 itself removed
``methods.schema.json`` from the specification.

These tests are the guard against the three ways a vendored bundle rots: the
files silently disappearing from the wheel, the code drifting away from the
contract the files describe, and the files drifting away from the tag they were
copied from.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import warnings
from pathlib import Path

import pandas as pd
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
    assert sdp_schema.sdp_profile_version() == "sdp-0.3.2"
    assert SDP_PROFILE_VERSION == sdp_schema.sdp_profile_version()

    rules = sdp_schema.vendored_path(sdp_schema.SDP_RULES_PATH)
    assert rules.read_text(encoding="utf-8").splitlines()[0] == "version: sdp-0.3.2"


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
    references survived on purpose and were logged as out of scope in the S10
    execplan: ``ontology_fetch.py``'s default ontology URL (R was stale in the
    same place and the two paths diverged, so it was a cross-repo coordination
    task) and ``term_requests.GCDFO_REPO`` (it matches current R and stays in
    lockstep until either repo moves first). The first was resolved on
    2026-09-26 by hub B-423, which moved the default to smn's w3id.org root as
    metasalmon has it. A blanket ban on the string would still have to exempt
    the second, and an exemption list with no expiry is how a guard outlives
    its cause -- so the guard names the one host it actually owns.

    Retirement condition: widen this to the whole organization name once
    ``term_requests.GCDFO_REPO`` is resolved in its own stream.
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


def test_the_bundle_carries_the_0_3_0_extension_schemas():
    # sdp-0.3.0 removed the methods registry from the specification, so the
    # bundle must not define it: the legacy reader's frozen column tuple in
    # ``sdp_methods`` is the only surviving record of that schema.
    for table in ("observation_structures", "observation_components"):
        assert table in sdp_schema.SDP_METADATA_SCHEMA_PATHS
    assert "methods" not in sdp_schema.SDP_METADATA_SCHEMA_PATHS
    assert not sdp_schema.vendored_path(
        "schema/frictionless/metadata/methods.schema.json"
    ).exists()


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
    # The registry left the specification at sdp-0.3.0.
    assert "sdp_methods" not in resources


def test_the_bundle_is_declared_as_package_data():
    # Globs in pyproject are the only reason these files reach a wheel, and a
    # missing glob fails at install time, not here -- so assert the glob.
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for glob in (
        "schema/frictionless/metadata/*.json",
        "schema/*.yaml",
        "profiles/salmon-data-package/v0.3/*.json",
        # The manifest ships with the bundle it describes, as metasalmon's
        # does from inst/extdata (hub item B-199).
        '"sdp-bundle-manifest.json"',
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
    """Both mirrors pin the spec release tag, never ``main``.

    Tracking ``main`` meant every upstream spec release broke networked
    schema loads (sdp-0.3.0 deleted ``methods.schema.json`` and the remote
    fetch 404ed). The pin, the vendored bundle, and the profile version must
    all name the same spec era — PARITY.md rows 27 and 38. ``sdp-0.3.2`` is
    the first release carrying the Q-51 ruling, and Brett ruled on 2026-09-23
    that the pin names a tag rather than a commit (hub items B-198 and B-199).
    """
    assert sdp_schema.SDP_SPEC_TAG == "sdp-0.3.2"
    assert sdp_schema.DEFAULT_SDP_SCHEMA_BASE_URL.endswith("/" + sdp_schema.SDP_SPEC_TAG)
    assert sdp_schema.sdp_profile_version() == sdp_schema.SDP_SPEC_TAG


def test_the_base_url_and_source_are_read_at_call_time(monkeypatch):
    """An import-time read cannot be changed by the caller who imports it."""
    sdp_schema.set_sdp_schema_source(None)
    monkeypatch.setenv("METASALMONPY_SDP_SCHEMA_BASE_URL", "https://example.org/bundle")
    assert sdp_schema.default_sdp_schema_base_url() == "https://example.org/bundle"
    monkeypatch.delenv("METASALMONPY_SDP_SCHEMA_BASE_URL")
    assert sdp_schema.default_sdp_schema_base_url().endswith("/sdp-0.3.2")
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
        assert schema["version"] == "sdp-0.3.2"
        assert calls and calls[0][0].endswith("/sdp-0.3.2")
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
    assert scalars["version"] == "sdp-0.3.2"
    assert scalars["profile"] == sdp_schema.SDP_PROFILE_URL
    # Nested keys are not top-level scalars and must not be picked up.
    assert "id" not in scalars
    assert "severity" not in scalars


# --- the vendored bundle, its manifest and the pin (hub item B-199) --------
#
# The twins of metasalmon's two B-198 tests in
# ``tests/testthat/test-schema-helpers.R``. Under the default ``"auto"`` source
# ``load_sdp_schema()`` loads the REMOTE bundle at the pinned tag first and the
# vendored copy only when that fetch fails, and under the default settings
# ``review_metadata()`` and the ``set_sdp_*()`` setters read the vendored copy
# in the tag's place (hub item B-215). So an offline session and an online one
# validate against different schemas unless the two hold the same bytes. Until
# B-199 they did not: ``sdp.rules.yaml`` came from a later commit than the pin
# (hub item B-166), and after metasalmon moved to ``sdp-0.3.2`` this package
# went on pinning ``sdp-0.3.0``.

_MANIFEST = "sdp-bundle-manifest.json"


def _loader_paths() -> list:
    """Every file ``_fetch_remote_sdp_schema()`` fetches, in its order.

    Read from the constants that function and ``_load_vendored_sdp_schema()``
    read, so a file added to the loader is covered here without editing this
    list.
    """
    return list(sdp_schema.SDP_METADATA_SCHEMA_PATHS.values()) + [
        sdp_schema.SDP_PROFILE_PATH,
        sdp_schema.SDP_RULES_PATH,
    ]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _bundle_manifest_findings() -> list:
    """Every way the vendored bundle, its manifest and the pin disagree.

    Empty means they agree. A list rather than a first failing assert, so one
    run names every file a partial re-vendor missed.
    """
    manifest_path = sdp_schema.vendored_path(_MANIFEST)
    if not manifest_path.is_file():
        return [f"there is no manifest at data/{_MANIFEST}"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    listed = manifest.get("files") or {}
    paths = _loader_paths()
    findings = []

    unlisted = [path for path in paths if path not in listed]
    if unlisted:
        findings.append(f"the loader reads {unlisted} and the manifest does not list them")
    unread = [path for path in listed if path not in paths]
    if unread:
        findings.append(f"the manifest lists {unread} and the loader does not read them")
    for path in paths:
        vendored = sdp_schema.vendored_path(path)
        if not vendored.is_file():
            findings.append(f"there is no vendored copy of {path}")
        elif path in listed and _sha256(vendored.read_bytes()) != listed[path]:
            findings.append(f"the vendored {path} does not have the SHA-256 the manifest names")

    # The pin names the manifest's tag, in the repository the manifest names.
    if sdp_schema.SDP_SPEC_TAG != manifest.get("tag"):
        findings.append(
            f"SDP_SPEC_TAG is {sdp_schema.SDP_SPEC_TAG!r} and the manifest's tag is "
            f"{manifest.get('tag')!r}"
        )
    pinned = (
        re.sub(r"^https://github\.com/", "https://raw.githubusercontent.com/", str(manifest.get("source")))
        + "/"
        + str(manifest.get("tag"))
    )
    if sdp_schema.DEFAULT_SDP_SCHEMA_BASE_URL != pinned:
        findings.append(
            f"DEFAULT_SDP_SCHEMA_BASE_URL is {sdp_schema.DEFAULT_SDP_SCHEMA_BASE_URL!r}, "
            f"not the manifest's {pinned!r}"
        )
    if not re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("commit"))):
        findings.append(f"the manifest's commit {manifest.get('commit')!r} is not a full SHA-1")

    # The bundle declares the version its tag names. The spec's release
    # workflow refuses a tag whose files say another version, and this is the
    # value every package written with a blank ``spec_version`` declares.
    try:
        version = sdp_schema._load_vendored_sdp_schema()["version"]
    except sdp_schema.SdpSchemaError as error:
        findings.append(f"the vendored bundle does not load: {error}")
    else:
        if version != manifest.get("tag"):
            findings.append(
                f"the vendored bundle declares {version!r} and the manifest's tag is "
                f"{manifest.get('tag')!r}"
            )
    return findings


def test_the_vendored_sdp_bundle_matches_its_manifest_with_no_network():
    """The bundle, ``data/sdp-bundle-manifest.json`` and the pin, held together.

    The byte-for-byte test below can check the pinned tag only online, and
    CI's suite runs offline, so a re-vendor made offline, or checked only by an
    offline suite, would otherwise go unchecked. The manifest names the tag the
    bundle was copied from and the SHA-256 of each file, and this holds the
    three to one another with no network:

    - a partial re-vendor, or a later hand edit, fails that file's hash;
    - a file the loader reads with no manifest entry fails the path set, so a
      resource added to the loader has to be vendored and hashed with it;
    - a pin moved without a re-vendor, or a re-vendor without the pin, fails
      the tag.

    *Retires when:* ``data/`` stops vendoring a copy of an upstream spec
    release, because the hashes are what make "a copy" checkable. The pin
    clauses alone retire sooner, if the loader stops fetching a remote bundle.
    """
    assert _bundle_manifest_findings() == []


def _tamper_hand_edit(root: Path, monkeypatch) -> None:
    # The hand edit the Q-51 ruling makes tempting: the vendored dataset schema
    # put back to the pre-ruling temporal pattern, which is also what a partial
    # re-vendor that skipped this file leaves behind.
    path = root / "schema/frictionless/metadata/dataset.schema.json"
    text = path.read_text(encoding="utf-8")
    edited = text.replace(r"|\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}Z", "")
    assert edited != text, "the tamper did not change the file"
    path.write_text(edited, encoding="utf-8")


def _tamper_unlisted_file(root: Path, monkeypatch) -> None:
    manifest_path = root / _MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["files"][sdp_schema.SDP_RULES_PATH]
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def _tamper_missing_file(root: Path, monkeypatch) -> None:
    (root / sdp_schema.SDP_METADATA_SCHEMA_PATHS["codes"]).unlink()


def _tamper_moved_pin(root: Path, monkeypatch) -> None:
    # The pin moved on its own, without a re-vendor.
    moved = "sdp-99.0.0"
    monkeypatch.setattr(sdp_schema, "SDP_SPEC_TAG", moved)
    monkeypatch.setattr(
        sdp_schema,
        "DEFAULT_SDP_SCHEMA_BASE_URL",
        "https://raw.githubusercontent.com/salmon-data-mobilization/smn-data-pkg/" + moved,
    )


@pytest.mark.parametrize(
    "tamper, expected",
    [
        (_tamper_hand_edit, "the vendored schema/frictionless/metadata/dataset.schema.json does not have"),
        (_tamper_unlisted_file, "the manifest does not list them"),
        (_tamper_missing_file, "there is no vendored copy of schema/frictionless/metadata/codes.schema.json"),
        (_tamper_moved_pin, "SDP_SPEC_TAG is 'sdp-99.0.0'"),
    ],
    ids=["hand-edit", "unlisted-file", "missing-file", "moved-pin"],
)
def test_the_manifest_check_can_fail(tmp_path, monkeypatch, tamper, expected):
    """Without this the test above could pass vacuously.

    A check that compared the manifest with itself, or read the wrong
    directory, would pass there whatever the bundle held. Each case changes a
    copy of the bundle, or the pin, in one of the ways the test above exists to
    catch, and requires a finding that names it.
    """
    _copy_vendored_bundle(tmp_path)
    shutil.copyfile(sdp_schema.vendored_path(_MANIFEST), tmp_path / _MANIFEST)
    monkeypatch.setattr(sdp_schema, "_DATA_DIR", tmp_path)
    # The unchanged copy passes, so the finding below is the tamper's.
    assert _bundle_manifest_findings() == []

    tamper(tmp_path, monkeypatch)
    findings = _bundle_manifest_findings()
    assert any(expected in finding for finding in findings), findings


def test_the_pinned_tag_serves_the_vendored_bundle_byte_for_byte():
    """Every file the remote loader fetches, at the pin, against the vendored copy.

    Opt-in, as this suite's other network test is: CI's suite runs offline,
    so set ``METASALMONPY_RUN_SDP_PIN_TEST=1`` to run it. The offline manifest
    test above is what CI checks. Measured before B-199, on ``main``
    ``e81cacd`` with this test enabled: the ``sdp-0.3.0`` pin served a
    ``sdp.rules.yaml`` that differed from the vendored one, which B-166 had
    copied from a later commit. The loader answers ``"remote"`` whenever the
    fetch succeeds, so every online session read those rules and every offline
    one the vendored copy.

    It takes a generous timeout of its own, where the loader's is two seconds,
    and it skips only when the network or the service fails: a transport
    error, a 429 or a 5xx. Any other status fails, because a 404 means the pin
    names a ref or a path that does not exist, and so does any byte difference.

    *Retires when:* the loader stops fetching a remote bundle, or stops falling
    back to the vendored one. Either way there is no longer a second copy for
    the first to disagree with. The opt-in gate retires sooner, when a CI job
    with network access sets the variable, so the check runs on every pull
    request rather than on request.
    """
    if not os.getenv("METASALMONPY_RUN_SDP_PIN_TEST", ""):
        pytest.skip(
            "SDP pin network test disabled. Set METASALMONPY_RUN_SDP_PIN_TEST=1 to enable."
        )
    import requests

    differing = []
    for path in _loader_paths():
        url = sdp_schema.DEFAULT_SDP_SCHEMA_BASE_URL + "/" + path
        try:
            response = requests.get(
                url, timeout=30, headers={"User-Agent": "metasalmonpy tests"}
            )
        except requests.RequestException as error:
            pytest.skip(f"Could not fetch {path}: {error}")
        if response.status_code == 429 or response.status_code >= 500:
            pytest.skip(f"raw.githubusercontent.com answered {response.status_code} for {path}")
        assert response.status_code == 200, (
            f"The pinned ref answered HTTP {response.status_code} for {path}. A 404 "
            "means the pin names a ref or a path that does not exist."
        )
        if _sha256(response.content) != _sha256(sdp_schema.vendored_path(path).read_bytes()):
            differing.append(path)
    assert differing == [], (
        f"{sdp_schema.DEFAULT_SDP_SCHEMA_BASE_URL} serves different bytes for {differing}"
    )


def test_both_writers_follow_a_bundle_that_moves_its_schema_urls(monkeypatch):
    """The derivation is load-bearing, not decorative.

    Composing the URL from ``SDP_PUBLIC_SCHEMA_BASE`` gives the same answer as
    reading it out of the vendored bundle, so a test against the vendored
    bundle alone cannot tell the two apart. This moves the bundle's own URLs
    and asserts both the core metadata resources (``package_io``) and the
    extension resources (``observation_structures``, metasalmon 0.2.1) follow
    it. ``sdp_methods`` left the profile at sdp-0.3.0, so its legacy resource
    entry must keep composing the public fallback URL instead.
    """
    from metasalmonpy import observation_structures, package_io, sdp_methods

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
    structures_resource = observation_structures._observation_resources()[0]
    assert (
        structures_resource["schema"]
        == "https://elsewhere.example/sdp_observation_structures.json"
    )
    # The registry left the profile at sdp-0.3.0: its legacy descriptor entry
    # falls back to the composed public URL, which is exactly what legacy
    # descriptors declare.
    legacy = sdp_methods._extension_resource(
        "sdp_methods", "metadata/methods.csv", "t", "d", "methods.schema.json"
    )
    assert legacy["schema"] == sdp_schema.sdp_schema_url("methods.schema.json")


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
    assert descriptor["sdp"]["specVersion"] == "sdp-0.3.2"
    dataset = (destination / "metadata" / "dataset.csv").read_text(encoding="utf-8")
    assert "sdp-0.1.0" in dataset


def _write_declaring(spec_version: str, path: Path) -> Path:
    """A complete package, so the only thing that can warn is ``spec_version``."""
    from metasalmonpy import write_salmon_datapackage

    return Path(
        write_salmon_datapackage(
            {"obs": pd.DataFrame({"site_id": ["s1", "s2"]})},
            pd.DataFrame(
                [
                    {
                        "dataset_id": "sv-1",
                        "title": "T",
                        "description": "D",
                        "creator": "C",
                        "contact_name": "N",
                        "contact_email": "n@example.org",
                        "license": "CC-BY-4.0",
                        "temporal_start": "1996",
                        "temporal_end": "2024",
                        "spec_version": spec_version,
                    }
                ]
            ),
            pd.DataFrame(
                [
                    {
                        "dataset_id": "sv-1",
                        "table_id": "obs",
                        "file_name": "data/obs.csv",
                        "table_label": "Obs",
                        "description": "D",
                    }
                ]
            ),
            pd.DataFrame(
                [
                    {
                        "dataset_id": "sv-1",
                        "table_id": "obs",
                        "column_name": "site_id",
                        "column_label": "Site",
                        "column_description": "Site",
                        "column_role": "identifier",
                        "value_type": "string",
                        "required": False,
                    }
                ]
            ),
            path=str(path),
            overwrite=True,
        )
    )


def test_a_patch_level_spec_version_difference_is_a_message_and_a_minor_one_a_warning(
    tmp_path, capsys
):
    """A patch release keeps the profile, so a patch-only difference is a note.

    The twin of metasalmon's test of the same name (Brett, 2026-09-27). Moving
    the pin to ``sdp-0.3.2`` made every package written by 0.5.0, stamped
    ``sdp-0.3.0``, warn when re-written, although both versions name the same
    ``v0.3`` profile. R now informs with ``cli::cli_inform()`` there; this
    package prints the note to standard output, as the setters and
    ``migrate_sdp_methods()`` print R's informational messages, so it is seen
    by default as R's is and raises no warning. The declared versions are
    derived from the loaded one, so this survives the next re-vendor.
    """
    loaded = sdp_schema.sdp_profile_version()
    parts = loaded[len("sdp-") :].split(".")
    assert loaded.startswith("sdp-") and len(parts) == 3, loaded
    major, minor, patch = (int(part) for part in parts)
    other_patch = f"sdp-{major}.{minor}.{patch + 1}"
    other_minor = f"sdp-{major}.{minor + 1}.0"

    # Same major and minor: a note, no warning, and both values are kept.
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        package = _write_declaring(other_patch, tmp_path / "patch")
    assert [str(warning.message) for warning in recorded] == []
    printed = capsys.readouterr().out
    assert "a patch release of the same profile" in printed
    assert repr(other_patch) in printed and repr(loaded) in printed
    dataset = pd.read_csv(
        package / "metadata" / "dataset.csv", dtype=str, keep_default_na=False
    )
    assert dataset["spec_version"].tolist() == [other_patch]
    descriptor = json.loads((package / "datapackage.json").read_text(encoding="utf-8"))
    assert descriptor["sdp"]["specVersion"] == loaded

    # A different minor is a different profile: still a warning, and no note.
    with pytest.warns(UserWarning, match="but the loaded SDP schema is"):
        _write_declaring(other_minor, tmp_path / "minor")
    assert "patch release" not in capsys.readouterr().out

    # The loaded version itself: neither.
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        _write_declaring(loaded, tmp_path / "same")
    assert [str(warning.message) for warning in recorded] == []
    assert "SDP schema is" not in capsys.readouterr().out


def test_sdp_versions_that_differ_only_in_the_patch_number_name_one_profile():
    same = sdp_schema._sdp_same_minor_version

    assert same("sdp-0.3.0", "sdp-0.3.2")
    assert same("sdp-0.3.10", "sdp-0.3.2")
    assert same("sdp-0.3.2", "sdp-0.3.2")

    # A minor or major difference is a different profile, and minor 30 is not 3.
    assert not same("sdp-0.2.0", "sdp-0.3.2")
    assert not same("sdp-1.3.2", "sdp-0.3.2")
    assert not same("sdp-0.30.0", "sdp-0.3.0")

    # Anything that is not sdp-<major>.<minor>.<patch> counts as a real
    # difference, including digits outside ASCII, which ``\d`` would admit.
    for label in (
        "0.3.0",
        "sdp-0.3",
        "sdp-0.3.0-rc1",
        "sdp-0.3.0\n",
        "sdp-0.٣.0",
        None,
        ["sdp-0.3.0"],
    ):
        assert not same(label, "sdp-0.3.2"), label
        assert not same("sdp-0.3.2", label), label


def test_per_resource_schema_urls_come_from_the_bundle():
    """metasalmon 0.2.1: the last hardcoded contract value in a descriptor."""
    for name, expected_file in (
        ("sdp_dataset", "dataset.schema.json"),
        ("sdp_methods", "methods.schema.json"),
        ("sdp_observation_structures", "observation_structures.schema.json"),
    ):
        assert sdp_schema.sdp_metadata_resource_schema(name, expected_file).endswith(
            "/" + expected_file
        )
    # The fallback is not dead code: a bundle published before the v0.2
    # extension resources existed has no ``sdp_methods`` entry, and composing
    # the public base with the caller's filename is the URL that shipped
    # before this was derived.
    assert sdp_schema.sdp_metadata_resource_schema(
        "sdp_not_in_this_bundle", "future.schema.json"
    ) == sdp_schema.sdp_schema_url("future.schema.json")
