"""The legacy SDP SOSA procedure registry reader, and its IRI predicate.

sdp-0.3.0 removed ``metadata/methods.csv`` from the specification and
metasalmon removed all three registry APIs with it. The reader and validator
survive **here only**, as legacy read support for R-0.2.x-written packages
(PARITY.md row 9); a current package carrying a registry is an error pointing
at ``migrate_sdp_methods()`` on every validation and publication surface,
which ``tests/test_sdp_methods_migration.py`` covers.

The ``methods-sdp`` fixture under ``tests/data/sdp-extensions/`` is genuine
metasalmon **v0.1.8** output (R 4.5.2, ``LC_COLLATE=C``) and deliberately
stays era-shaped: it is exactly the kind of legacy package the surviving
reader exists for. It was written by R's ``write_sdp_methods()`` from a
deliberately reversed row/column order, so the committed bytes prove
canonical ordering rather than input order.

The writer is not mirrored here. That is a logged decision, not an oversight —
see ``test_the_writer_is_absent_and_says_why`` and PARITY.md row 9.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

pd = pytest.importorskip("pandas")

from metasalmonpy import read_sdp_methods, validate_sdp_methods, write_sdp_methods
from metasalmonpy.sdp_methods import (
    SDP_METHODS_COLUMNS,
    SdpExtensionError,
)
from metasalmonpy.sdp_schema import sdp_schema_field_names

DATA = Path(__file__).parent / "data" / "sdp-extensions"
CHECKSUMS = json.loads((DATA / "checksums.json").read_text(encoding="utf-8"))


def _sdp(tmp_path: Path, name: str) -> Path:
    target = tmp_path / name
    shutil.copytree(DATA / name, target)
    return target


def test_the_registry_columns_are_the_frozen_legacy_contract():
    # sdp-0.3.0 removed the registry from the specification, so the vendored
    # bundle no longer defines a ``methods`` table — the tuple in
    # ``sdp_methods`` is now the frozen legacy contract, kept only to read
    # packages written before the migration. The bundle must NOT quietly grow
    # it back: a reappearing schema would mean the pin and the bundle name
    # different spec eras.
    with pytest.raises(KeyError):
        sdp_schema_field_names("methods")
    assert SDP_METHODS_COLUMNS == (
        "dataset_id",
        "method_iri",
        "method_label",
        "method_description",
        "method_version",
        "protocol_iri",
        "citation",
    )


# metasalmon's SDP-extension IRI validator resolves ``[[:space:]]`` through
# TRE (``R/iri-predicates.R`` records the engine as contractual). These are
# the verdicts metasalmon main (e02111a, R 4.5.2) gives an IRI carrying one
# exotic codepoint, reproduced by driving ``.ms_sdp_extension_is_absolute_iri``
# over the same values. Python's ``\s`` disagreed on the first five — it
# rejected every value R accepts — until ``sdp_methods`` built its patterns
# from ``metadata.R_SPACE_CLASS`` like ``eml.py`` and ``sssom.py`` (hub
# backlog #86; PARITY.md row 33's retirement condition).
_R_ABSOLUTE_IRI_VERDICTS = {
    "\x1c": True,  # file separator: a control, not TRE whitespace
    "\x1d": True,  # group separator: a control, not TRE whitespace
    "\x1e": True,  # record separator: a control, not TRE whitespace
    "\x1f": True,  # unit separator: a control, not TRE whitespace
    "\x85": True,  # NEL: a C1 control, not TRE whitespace
    "\u00a0": True,  # no-break space: NOT whitespace to TRE
    "\u2007": True,  # figure space: NOT whitespace to TRE
    "\u202f": True,  # narrow no-break space: NOT whitespace to TRE
    " ": False,  # plain space
    "\u1680": False,  # ogham space mark
    "\u2000": False,  # en quad
    "\u3000": False,  # ideographic space
}


@pytest.mark.parametrize(
    ("codepoint", "accepted"), sorted(_R_ABSOLUTE_IRI_VERDICTS.items())
)
def test_absolute_iri_shape_uses_r_tre_whitespace(codepoint, accepted):
    from metasalmonpy.sdp_methods import _is_absolute_iri

    # In an http path: the shape pattern's whitespace class decides.
    assert _is_absolute_iri(f"https://example.org/m/a{codepoint}b") is accepted
    # Non-hierarchical scheme: the shape pattern alone.
    assert _is_absolute_iri(f"urn:example:a{codepoint}b") is accepted


def test_the_committed_fixture_is_unmodified_r_output():
    name = "methods-sdp/metadata/methods.csv"
    digest = hashlib.sha256((DATA / name).read_bytes()).hexdigest()
    assert digest == CHECKSUMS["sha256"][name]


def test_reads_an_r_written_registry_with_the_exact_schema(tmp_path):
    root = _sdp(tmp_path, "methods-sdp")
    methods = read_sdp_methods(root)

    assert list(methods.columns) == list(SDP_METHODS_COLUMNS)
    assert list(methods["method_iri"]) == [
        "https://example.org/methods/expanded-count",
        "https://example.org/methods/mark-recapture",
    ]
    # R wrote this file from reversed rows AND reversed columns; the canonical
    # order is what came back.
    assert list(methods["method_label"]) == [
        "Expanded count",
        "Mark-recapture estimate",
    ]
    # ``na = ""`` on both sides: an absent optional field is the empty string,
    # never a float NaN that would poison a downstream join.
    assert list(methods["method_version"]) == ["", "2026"]
    assert list(methods["citation"]) == ["Example Program. 2026.", ""]
    assert validate_sdp_methods(root) is True


def test_normalization_imposes_canonical_order_on_any_input(tmp_path):
    """Reading an already-sorted file cannot prove the sort happens.

    The committed fixture is canonical because R wrote it that way, so a read
    round-trip stays green even with the ordering removed. Feeding an
    out-of-order frame is what pins it -- and it is the same ordering the
    (unimplemented) writer would have needed.
    """
    from metasalmonpy.sdp_methods import _normalize_methods

    rows = pd.DataFrame(
        [
            {
                "dataset_id": "methods-test",
                "method_iri": "https://example.org/methods/mark-recapture",
                "method_label": "Mark-recapture estimate",
                "method_description": "Estimates abundance.",
                "method_version": "2026",
                "protocol_iri": "",
                "citation": "",
            },
            {
                "dataset_id": "methods-test",
                "method_iri": "https://example.org/methods/expanded-count",
                "method_label": "Expanded count",
                "method_description": "Expands an observed count.",
                "method_version": "",
                "protocol_iri": "",
                "citation": "",
            },
        ]
    )

    normalized = _normalize_methods(rows)

    assert list(normalized["method_iri"]) == [
        "https://example.org/methods/expanded-count",
        "https://example.org/methods/mark-recapture",
    ]
    # Column order is part of the schema, not just row order.
    assert list(normalized.columns) == list(SDP_METHODS_COLUMNS)


def test_normalization_accepts_reversed_columns(tmp_path):
    # R wrote the committed fixture from reversed rows AND reversed columns.
    from metasalmonpy.sdp_methods import _normalize_methods

    root = _sdp(tmp_path, "methods-sdp")
    methods = read_sdp_methods(root)
    reversed_frame = methods.iloc[::-1, ::-1].reset_index(drop=True)

    normalized = _normalize_methods(reversed_frame)

    assert list(normalized.columns) == list(SDP_METHODS_COLUMNS)
    assert normalized.equals(methods)


def test_reading_without_validation_still_enforces_the_closed_schema(tmp_path):
    root = _sdp(tmp_path, "methods-sdp")
    path = root / "metadata" / "methods.csv"
    text = path.read_text(encoding="utf-8").splitlines()
    path.write_text(
        "\n".join([text[0] + ",unexpected"] + [row + ",drift" for row in text[1:]])
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(SdpExtensionError, match="exact SDP schema"):
        read_sdp_methods(root, validate=False)


def test_a_missing_static_procedure_reference_is_refused(tmp_path):
    root = _sdp(tmp_path, "methods-sdp")
    dictionary = root / "metadata" / "column_dictionary.csv"
    text = dictionary.read_text(encoding="utf-8")
    dictionary.write_text(
        text.replace(
            "measurement,number,individual",
            "measurement,number,individual",
        ).replace(
            "https://w3id.org/smn/Stock,,\n",
            "https://w3id.org/smn/Stock,,https://example.org/methods/not-registered\n",
        ),
        encoding="utf-8",
    )
    with pytest.raises(SdpExtensionError, match="Static procedure references"):
        validate_sdp_methods(root)


def test_a_foreign_dataset_id_is_refused(tmp_path):
    root = _sdp(tmp_path, "methods-sdp")
    path = root / "metadata" / "methods.csv"
    path.write_text(
        path.read_text(encoding="utf-8").replace("methods-test,", "other-dataset,"),
        encoding="utf-8",
    )
    with pytest.raises(SdpExtensionError, match="dataset.csv"):
        validate_sdp_methods(root)


def test_a_duplicate_method_iri_is_refused(tmp_path):
    root = _sdp(tmp_path, "methods-sdp")
    path = root / "metadata" / "methods.csv"
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines + [lines[1]]) + "\n", encoding="utf-8")
    with pytest.raises(SdpExtensionError, match="unique within each dataset"):
        validate_sdp_methods(root)


def test_a_relative_method_iri_is_refused(tmp_path):
    root = _sdp(tmp_path, "methods-sdp")
    path = root / "metadata" / "methods.csv"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "https://example.org/methods/expanded-count", "methods/expanded-count"
        ),
        encoding="utf-8",
    )
    with pytest.raises(SdpExtensionError, match="absolute IRI"):
        validate_sdp_methods(root)


def test_a_review_placeholder_iri_is_not_an_absolute_iri(tmp_path):
    root = _sdp(tmp_path, "methods-sdp")
    path = root / "metadata" / "methods.csv"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "https://example.org/methods/expanded-count", "REVIEW:expanded-count"
        ),
        encoding="utf-8",
    )
    with pytest.raises(SdpExtensionError, match="absolute IRI"):
        validate_sdp_methods(root)


def test_descriptor_drift_is_refused(tmp_path):
    root = _sdp(tmp_path, "methods-sdp")
    descriptor_path = root / "datapackage.json"
    descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
    del descriptor["sdp"]["metadata"]["methods"]
    descriptor_path.write_text(json.dumps(descriptor, indent=2), encoding="utf-8")
    with pytest.raises(SdpExtensionError, match="must declare metadata/methods.csv"):
        validate_sdp_methods(root)

    descriptor = json.loads((DATA / "methods-sdp" / "datapackage.json").read_text())
    for resource in descriptor["resources"]:
        if resource.get("path") == "metadata/methods.csv":
            resource["schema"] = "https://example.org/wrong.json"
    descriptor_path.write_text(json.dumps(descriptor, indent=2), encoding="utf-8")
    with pytest.raises(SdpExtensionError, match="field schema"):
        validate_sdp_methods(root)


def test_a_symlinked_registry_is_refused(tmp_path):
    root = _sdp(tmp_path, "methods-sdp")
    outside = tmp_path / "outside.csv"
    outside.write_text("outside\n", encoding="utf-8")
    target = root / "metadata" / "methods.csv"
    target.unlink()
    try:
        target.symlink_to(outside)
    except OSError:  # pragma: no cover - filesystem without symlinks
        pytest.skip("Filesystem does not permit symlink creation")
    with pytest.raises(SdpExtensionError, match="symlink"):
        read_sdp_methods(root)


def test_only_a_symlinked_package_root_is_refused(tmp_path):
    # macOS spells a temporary directory through the harmless /var ->
    # /private/var alias, so refusing every symlinked *ancestor* would make the
    # API unusable there. Only the supplied root entry is the trust boundary.
    root = _sdp(tmp_path, "methods-sdp")
    linked = tmp_path / "linked-sdp"
    try:
        linked.symlink_to(root, target_is_directory=True)
    except OSError:  # pragma: no cover - filesystem without symlinks
        pytest.skip("Filesystem does not permit directory symlink creation")
    with pytest.raises(SdpExtensionError, match="symlink"):
        read_sdp_methods(linked)
    with pytest.raises(SdpExtensionError, match="symlink"):
        read_sdp_methods(str(linked) + os.sep)
    assert validate_sdp_methods(root) is True


def test_the_writer_is_absent_and_says_why():
    """The registry writer is deliberately skipped, and must say so.

    A guard, exclusion or omission has to record the condition under which it
    stops being needed, or it outlives its cause. This one is reachable from
    the same place the R function lives, so a user who looks for
    ``write_sdp_methods`` gets the reason instead of an AttributeError.

    Retirement condition: delete this test together with the stub when the
    replay reaches 0.3.0 and ``metadata/methods.csv`` leaves the spec.
    """
    import metasalmonpy

    assert "write_sdp_methods" in metasalmonpy.__all__
    with pytest.raises(NotImplementedError) as caught:
        write_sdp_methods("/nonexistent", None)
    message = str(caught.value)
    assert "SDP 0.3.0" in message
    assert "PARITY.md row 9" in message
    # It must not look like an unimplemented feature that a caller could
    # trigger by passing better arguments.
    assert "metasalmon" in message


# --- chunk G: a 0.2.x-written package reads end-to-end post-A ---------------
#
# The methods-sdp fixture above is metasalmon v0.1.8 output and exercises the
# surviving registry reader in isolation. The era-0.2.6-sdp fixture is a
# genuine package written by the LAST 0.2.x-behaviour metasalmon tree
# (hub 5825467 = 1893cfa~1; DESCRIPTION Version: 0.2.6 — the 0.2.x releases
# are deliberately untagged, and the later "Version: 0.2.6" commits already
# carry 0.3.0 behaviour). R 4.5.2, LC_COLLATE=C; the registry was written
# from deliberately reversed row order and era-validated at generation. This
# is the package shape Python actually receives from R 0.2.x users: era
# 16-column dictionary binding method_iri, sdp-0.2.0 descriptor, 0.2.4+
# empty-string missing-value bytes, and a methods registry.


def test_the_era_026_fixture_is_unmodified_r_output():
    root = DATA / "era-0.2.6-sdp"
    for file in sorted(root.rglob("*")):
        if not file.is_file():
            continue
        name = f"era-0.2.6-sdp/{file.relative_to(root)}"
        digest = hashlib.sha256(file.read_bytes()).hexdigest()
        assert digest == CHECKSUMS["sha256"][name], name


def test_a_registry_era_package_reads_end_to_end(tmp_path):
    from metasalmonpy import read_salmon_datapackage

    root = _sdp(tmp_path, "era-0.2.6-sdp")

    package = read_salmon_datapackage(str(root))
    resource = package["resources"]["stock_recruit"]
    assert list(resource.columns) == [
        "stock_id",
        "brood_year",
        "spawner_abundance",
        "estimate_method",
    ]
    assert len(resource) == 3

    dictionary = package["dictionary"]
    # The current dictionary contract plus the era binding column: the reader
    # normalizes to the sdp-0.3.0 columns and PRESERVES the era method_iri as
    # a passthrough, so nothing a 0.2.x package recorded is dropped on read.
    assert "statistical_modifier_iri" in dictionary.columns
    assert "method_iri" in dictionary.columns
    bound = dictionary.loc[
        dictionary["column_name"] == "spawner_abundance", "method_iri"
    ].iloc[0]
    assert bound == "https://example.org/methods/expanded-count"
    assert len(package["codes"]) == 2

    # The registry itself still reads and validates through the surviving
    # legacy reader (PARITY row 9), in the writer's canonical order.
    methods = read_sdp_methods(root)
    assert list(methods.columns) == list(SDP_METHODS_COLUMNS)
    assert list(methods["method_iri"]) == [
        "https://example.org/methods/expanded-count",
        "https://example.org/methods/mark-recapture",
    ]
    assert validate_sdp_methods(root) is True


def test_a_registry_era_package_validates_toward_migration(tmp_path):
    # Reading is legacy-supported; VALIDATING a current package that still
    # carries a registry deliberately is not — every validation surface
    # points at the migration (chunk A's registry-removal contract), and the
    # migration's dry run works on the genuine era package without touching
    # its bytes.
    from metasalmonpy import migrate_sdp_methods, validate_salmon_datapackage

    root = _sdp(tmp_path, "era-0.2.6-sdp")
    before = {
        file.relative_to(root): file.read_bytes()
        for file in sorted(root.rglob("*"))
        if file.is_file()
    }

    with pytest.raises(SdpExtensionError, match="migrate_sdp_methods"):
        validate_salmon_datapackage(str(root))

    report = migrate_sdp_methods(root, dry_run=True)
    placements = report["tables"]
    assert list(placements["table_id"]) == ["stock_recruit"]
    assert list(placements["method_iri"]) == [
        "https://example.org/methods/expanded-count"
    ]

    after = {
        file.relative_to(root): file.read_bytes()
        for file in sorted(root.rglob("*"))
        if file.is_file()
    }
    assert before == after
