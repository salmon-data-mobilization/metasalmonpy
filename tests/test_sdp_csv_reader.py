"""Shared SDP CSV-reader parity: readr's ``trim_ws`` and the literal-"NA" rule.

Every expectation in this file was produced by running the real thing rather
than reasoning about it:

* the ``read_sdp_csv`` cases replay ``readr::read_csv`` under R 4.5.2, the
  reader metasalmon uses for metadata, dictionaries and reviewed sidecars;
* the decomposition case replays metasalmon **v0.1.7**'s
  ``validate_sdp_measurement_decompositions()``, which returns ``TRUE`` for the
  space-padded dictionary this suite feeds it.

The two behaviours pinned here used to be split across three modules with
three different answers. ``package_io`` preserved a literal ``NA`` while the
EML and decomposition readers destroyed it, which made a dictionary cell of
``NA`` name a canonical semantic target that no ``semantic_vocabulary.csv``
row could satisfy; and nothing trimmed, so R read ``a, b`` as ``"b"`` and this
package read it as ``" b"``.
"""

import shutil
import unittest
from pathlib import Path

import pytest

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None

if pd is None:
    raise unittest.SkipTest("pandas not installed")

from metasalmonpy import eml as eml_module
from metasalmonpy import measurement_decompositions as decompositions
from metasalmonpy.metadata import read_sdp_csv
from metasalmonpy.package_io import _read_metadata_csv

DECOMPOSITION_DATA = Path(__file__).parent / "data" / "decompositions"
EML_DATA = Path(__file__).parent / "data" / "eml"

# The exact row readr was fed in the R probe, and the six values it returned
# with `trim_ws = TRUE, na = character()`.
READR_ROW = '  x  ,"  y  ",  "z"  ,   , NA ,\tt\t\n'
READR_VALUES = ["x", "y", "z", "", "NA", "t"]


def write_csv(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8", newline="")
    return path


# --- trim_ws parity -----------------------------------------------------------


def test_fields_are_trimmed_the_way_readr_trims_them(tmp_path):
    # readr trims inside quotes as well as outside, and trims before matching
    # the missing token -- so "   " becomes an empty (missing) field.
    path = write_csv(tmp_path / "row.csv", "a,b,c,d,e,f\n" + READR_ROW)
    frame = read_sdp_csv(path)
    assert [frame.iloc[0][name] for name in frame.columns] == READR_VALUES


def test_header_names_are_trimmed_the_way_readr_trims_them(tmp_path):
    path = write_csv(tmp_path / "header.csv", '  a  ,"  b  ",c\n1,2,3\n')
    assert list(read_sdp_csv(path).columns) == ["a", "b", "c"]


def test_trimming_does_not_reach_beyond_r_whitespace(tmp_path):
    # R's trimws() and readr both leave U+00A0 and U+3000 alone. Stripping
    # them would silently rewrite real label text.
    path = write_csv(tmp_path / "nbsp.csv", "a,b\n x ,　y　\n")
    frame = read_sdp_csv(path)
    assert frame.iloc[0]["a"] == " x "
    assert frame.iloc[0]["b"] == "　y　"


def test_a_space_padded_dictionary_validates_the_way_r_validates_it(tmp_path):
    # metasalmon v0.1.7 returns TRUE for exactly this SDP. Without trimming,
    # every binding field carried a leading space and no dictionary row
    # matched, so decomposition validation failed here but not in R.
    source = DECOMPOSITION_DATA / "era-sdp"
    sdp = tmp_path / "padded-sdp"
    shutil.copytree(source, sdp)
    dictionary = sdp / "metadata" / "column_dictionary.csv"
    dictionary.write_text(
        dictionary.read_text(encoding="utf-8").replace(",", ", "),
        encoding="utf-8",
    )

    assert decompositions.validate_sdp_measurement_decompositions(sdp) is True


# --- literal "NA" is data, not absence ----------------------------------------

_VOCABULARY_HEADER = (
    "iri,label,definition,source,ontology,resource_kind,type_iris,"
    "native_type,source_url,source_artifact_sha256,reviewed_snapshot_sha256\n"
)


def test_every_sdp_reader_preserves_a_literal_na(tmp_path):
    # metasalmon 0.2.4: "NA" is a real fisheries gear code. Three readers used
    # to disagree about that; they now share one implementation.
    path = write_csv(tmp_path / "cells.csv", "value,other\nNA,\n")
    for reader in (read_sdp_csv, _read_metadata_csv, eml_module._read_character_csv):
        frame = reader(path)
        assert frame.iloc[0]["value"] == "NA", reader
        assert frame.iloc[0]["other"] == "", reader


def test_a_literal_na_vocabulary_row_can_exist(tmp_path):
    # The ledger error the old asymmetry produced was unfixable by the user: a
    # dictionary constraint_iri of "NA" demanded a vocabulary row with
    # iri == "NA", and the vocabulary reader deleted that cell on the way in.
    path = write_csv(
        tmp_path / "semantic_vocabulary.csv",
        _VOCABULARY_HEADER + "NA,Not applicable,A gear code.,smn,smn,Class,,"
        "owl:Class,https://w3id.org/smn/,," + "3" * 64 + "\n",
    )
    assert list(eml_module._read_character_csv(path)["iri"]) == ["NA"]


def test_a_literal_na_constraint_iri_is_a_canonical_target():
    dictionary = pd.DataFrame(
        [
            {
                "dataset_id": "demo",
                "table_id": "counts",
                "column_name": "count",
                "column_role": "measurement",
                "term_iri": "https://w3id.org/smn/Abundance",
                "property_iri": "https://w3id.org/smn/Count",
                "entity_iri": "https://w3id.org/smn/Stock",
                "constraint_iri": "NA",
                "method_iri": "",
                "unit_iri": "",
            }
        ]
    )
    tables = pd.DataFrame(
        [{"dataset_id": "demo", "table_id": "counts", "observation_unit_iri": ""}]
    )

    targets = eml_module._canonical_review_targets(
        {"dictionary": dictionary, "tables": tables}
    )

    constraints = [
        target["iri"]
        for target in targets
        if target["target_sdp_field"] == "constraint_iri"
    ]
    assert constraints == ["NA"]


# --- the EML path still works end to end --------------------------------------


def _eml_extra_available() -> bool:
    try:
        import lxml.etree  # noqa: F401
        import yaml  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(
    not _eml_extra_available(), reason="requires the metasalmonpy[eml] extra"
)
def test_eml_export_still_matches_r_after_the_reader_change(tmp_path):
    # The reviewed crosswalk, review ledger and vocabulary all read through
    # the shared reader now; the document R produced must be unchanged.
    import xml.etree.ElementTree as ET

    from metasalmonpy import write_eml_from_sdp

    sdp = tmp_path / "sdp"
    shutil.copytree(EML_DATA / "sdp-default", sdp)
    result = write_eml_from_sdp(str(sdp), overwrite=True)

    assert ET.canonicalize(
        from_file=result["path"], strip_text=True
    ) == ET.canonicalize(from_file=str(EML_DATA / "eml-default.xml"), strip_text=True)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
