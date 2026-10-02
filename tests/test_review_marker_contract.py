"""Q-63's raw ASCII marker boundary, including document publication guards."""

from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

import pandas as pd
import pytest

from metasalmonpy import (
    suggest_semantics,
    validate_dictionary,
    write_eml_from_sdp,
    publish_sdp_to_knb,
)
from metasalmonpy import eml, knb_publication, package_io
from metasalmonpy.metadata import read_sdp_csv
from metasalmonpy.review_console import _is_review_iri, _strip_review_iri


def test_raw_ascii_marker_contract_under_c_and_utf8_ctype():
    # An independent process owns LC_CTYPE, so this never changes another
    # test's locale. UTF-8 must actually be available; an unrun leg is a failure.
    script = r'''
import locale
from pathlib import Path
import sys
from metasalmonpy import metadata
assert Path(metadata.__file__).resolve() == Path(sys.argv[1]).resolve()
utf8 = None
for candidate in ("C.UTF-8", "en_US.UTF-8", "UTF-8"):
    try:
        locale.setlocale(locale.LC_CTYPE, candidate)
        utf8 = candidate
        break
    except locale.Error:
        pass
assert utf8 is not None, "No UTF-8 LC_CTYPE is available; marker matrix did not run"
for name in ("C", utf8):
    active = locale.setlocale(locale.LC_CTYPE, name)
    for value in ("REVIEW:x", " review :\tx", "\tReViEw\t: x"):
        assert metadata._is_review_iri(value), (active, value)
        assert metadata._strip_review_iri(value) == "x", (active, value)
    for value in ("\nREVIEW:x", "\u00a0REVIEW:x", "REVIEW\n:x", "REV\u0131EW:x"):
        assert not metadata._is_review_iri(value), (active, value)
        assert metadata._strip_review_iri(value) == value, (active, value)
    for suffix in ("\u00a0x", "\nx", "\f", "x\t "):
        assert metadata._strip_review_iri("REVIEW : \t" + suffix) == suffix
    print("LC_CTYPE=" + active + ": marker matrix passed")
'''
    result = subprocess.run([sys.executable, "-c", script, str(Path(__file__).parent.parent / "metadata.py")],
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
    print(result.stdout, end="")


def test_shared_marker_helpers_read_a_series_by_position():
    from metasalmonpy.metadata import _strip_review_iri as strip_raw
    values = pd.Series([" review :\turn:example:x"], index=[7])
    assert _is_review_iri(values)
    assert strip_raw(values) == "urn:example:x"


@pytest.mark.parametrize("prefix", ["REVIEW:", " review :\t", "\tReViEw\t:\t "])
def test_ascii_marker_spellings_share_one_detector_and_strip(prefix):
    value = prefix + "urn:example:term"
    assert _is_review_iri(value)
    assert _strip_review_iri(value) == "urn:example:term"


@pytest.mark.parametrize("value", [
    "\nREVIEW:x", "\rREVIEW:x", "\fREVIEW:x", "\u00a0REVIEW:x",
    "REVIEW\n:x", "REVIEW\u00a0:x", "REV\u0131EW:x",
])
def test_excluded_prefixes_remain_raw_and_are_not_markers(value):
    assert not _is_review_iri(value)
    assert _strip_review_iri(value) == value


@pytest.mark.parametrize("suffix", ["\u00a0urn:example:x", "\f", "\nx", "x\t "])
def test_strip_removes_only_spaces_and_tabs_next_to_the_colon(suffix):
    assert _strip_review_iri("REVIEW : \t" + suffix) == suffix


@pytest.mark.parametrize("value", ["REV\u0131EW:urn:example:x", "REVIEW\n:urn:example:x", "\u00a0REVIEW:urn:example:x"])
def test_dictionary_excluded_marker_spellings_fail_the_existing_iri_shape(value):
    dictionary = read_sdp_csv(Path(__file__).parent.parent / "data/column_dictionary.csv")
    dictionary.loc[0, "term_iri"] = value
    with pytest.raises(ValueError, match="absolute IRIs") as caught:
        validate_dictionary(dictionary, require_iris=True)
    assert "REVIEW-prefixed" not in str(caught.value)


@pytest.mark.parametrize("value, expected", [
    (" review :urn:x", True), ("\nREVIEW:urn:x", False),
    ("REV\u0131EW:urn:x", False), ("\u00a0REVIEW:urn:x", False),
])
def test_package_prefill_uses_the_raw_marker_before_unrelated_normalization(value, expected):
    assert package_io._is_review_value(value) is expected
    assert package_io._mark_review_iri(value) == (value if expected else "REVIEW:" + value)


def test_semantic_query_fallback_recognizes_ascii_spaces_before_the_colon():
    dictionary = pd.DataFrame([{
        "dataset_id": "demo", "table_id": "t", "column_name": "catch_count",
        "column_label": "Catch count", "column_description": "review :draft",
        "column_role": "measurement", "term_iri": pd.NA,
    }])
    queries = []
    def search(query, **kwargs):
        queries.append(query)
        return pd.DataFrame()
    suggest_semantics(pd.DataFrame({"catch_count": [1]}), dictionary, search_fn=search)
    assert queries
    assert all("review" not in str(query).lower() for query in queries)


def _fixture_sdp(tmp_path, family, name):
    target = tmp_path / "sdp"
    shutil.copytree(Path(__file__).parent / "data" / family / name, target)
    return target


@pytest.mark.parametrize("carrier", ["text", "attribute"])
def test_eml_public_export_refuses_a_marker_anywhere_in_the_document(tmp_path, monkeypatch, carrier):
    pytest.importorskip("yaml")
    pytest.importorskip("lxml")
    target = _fixture_sdp(tmp_path, "eml", "sdp-default")
    build = eml._build_document
    def marked_document(*args, **kwargs):
        built = build(*args, **kwargs)
        # The final guard scans the whole generated document, even a value
        # outside the semantic annotation slots checked earlier.
        title = built["document"].find("dataset/title")
        if carrier == "text":
            title.text = " review :draft"
        else:
            # ElementTree writes an attribute tab as &#09;. The guard must
            # still see the raw value admitted by the cell predicate.
            title.set("{http://www.w3.org/XML/1998/namespace}lang", "review\t:draft")
        return built
    monkeypatch.setattr(eml, "_build_document", marked_document)
    with pytest.raises(ValueError, match="unresolved.*REVIEW"):
        write_eml_from_sdp(target, overwrite=True)


@pytest.mark.parametrize("carrier", ["text", "attribute"])
def test_knb_public_dry_run_refuses_a_marker_anywhere_in_ore(tmp_path, monkeypatch, carrier):
    pytest.importorskip("yaml")
    pytest.importorskip("lxml")
    target = _fixture_sdp(tmp_path, "knb", "sdp-public")
    build = knb_publication._build_ore
    def marked_document(*args, **kwargs):
        document = build(*args, **kwargs)
        note = ET.SubElement(document, "note")
        if carrier == "text":
            note.text = " review :draft"
        else:
            note.set("marker", "review\t:draft")
        return document
    monkeypatch.setattr(knb_publication, "_build_ore", marked_document)
    with pytest.raises(ValueError, match="local/review marker"):
        publish_sdp_to_knb(target, public=True, dry_run=True, knb_environment="production")


@pytest.mark.parametrize("narrative", ["Peer review: ordinary narrative", "preview: ordinary narrative"])
def test_eml_public_export_keeps_ordinary_review_narrative(tmp_path, narrative):
    pytest.importorskip("yaml")
    pytest.importorskip("lxml")
    target = _fixture_sdp(tmp_path, "eml", "sdp-default")
    dataset_path = target / "metadata" / "dataset.csv"
    dataset = read_sdp_csv(dataset_path)
    dataset.loc[0, "title"] = narrative
    dataset.to_csv(dataset_path, index=False)
    write_eml_from_sdp(target, overwrite=True)
    assert ET.parse(target / "metadata" / "eml.xml").find("dataset/title").text == narrative


@pytest.mark.parametrize("narrative", ["Peer review: ordinary narrative", "preview: ordinary narrative"])
def test_knb_public_dry_run_keeps_ordinary_review_narrative(tmp_path, monkeypatch, narrative):
    pytest.importorskip("yaml")
    pytest.importorskip("lxml")
    target = _fixture_sdp(tmp_path, "knb", "sdp-public")
    build = knb_publication._build_ore
    def narrated_document(*args, **kwargs):
        document = build(*args, **kwargs)
        ET.SubElement(document, "note").text = narrative
        return document
    monkeypatch.setattr(knb_publication, "_build_ore", narrated_document)
    result = publish_sdp_to_knb(target, public=True, dry_run=True, knb_environment="production")
    assert result["status"] == "dry_run"


def test_eml_public_export_keeps_the_inherited_uppercase_literal_guard(tmp_path):
    pytest.importorskip("yaml")
    pytest.importorskip("lxml")
    target = _fixture_sdp(tmp_path, "eml", "sdp-default")
    dataset_path = target / "metadata" / "dataset.csv"
    dataset = read_sdp_csv(dataset_path)
    dataset.loc[0, "title"] = "Peer REVIEW: inherited conservative refusal"
    dataset.to_csv(dataset_path, index=False)
    with pytest.raises(ValueError, match="unresolved.*REVIEW"):
        write_eml_from_sdp(target, overwrite=True)


def test_knb_public_dry_run_keeps_the_inherited_uppercase_literal_guard(tmp_path, monkeypatch):
    pytest.importorskip("yaml")
    pytest.importorskip("lxml")
    target = _fixture_sdp(tmp_path, "knb", "sdp-public")
    build = knb_publication._build_ore
    def narrated_document(*args, **kwargs):
        document = build(*args, **kwargs)
        ET.SubElement(document, "note").text = "Peer REVIEW: inherited conservative refusal"
        return document
    monkeypatch.setattr(knb_publication, "_build_ore", narrated_document)
    with pytest.raises(ValueError, match="local/review marker"):
        publish_sdp_to_knb(target, public=True, dry_run=True, knb_environment="production")
