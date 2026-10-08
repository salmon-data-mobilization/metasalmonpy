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


@pytest.mark.parametrize("carrier", [
    "propertyURI", "valueURI", "url", "userId", "directory",
    "DataONE", "packageId", "schemaLocation",
])
def test_eml_iri_inventory_reaches_real_emitted_fixture_fields(carrier):
    from metasalmonpy.metadata import _contains_review_iri
    document = ET.parse(Path(__file__).parent / "data/eml/eml-supplementary.xml").getroot()
    marker = "review\t:draft"
    if carrier in ("propertyURI", "valueURI", "url", "userId"):
        document.find(".//" + carrier).text = marker
    elif carrier == "directory":
        document.find(".//userId").set("directory", marker)
    elif carrier == "DataONE":
        document.find(".//otherEntity/alternateIdentifier[@system='DataONE']").text = marker
    elif carrier == "packageId":
        document.set("packageId", marker)
    else:
        document.set("{http://www.w3.org/2001/XMLSchema-instance}schemaLocation",
                     "https://eml.ecoinformatics.org/eml-2.2.0 " + marker)
    assert _contains_review_iri(ET.tostring(document, encoding="unicode"), eml._eml_iri_values(document))


def test_eml_code_vocabulary_source_uses_the_actual_emitter():
    from metasalmonpy.metadata import _contains_review_iri
    document = ET.Element("eml")
    codes = pd.DataFrame([{
        "table_id": "counts", "column_name": "sex", "code_value": "F",
        "code_label": "Female", "code_description": "Female fish",
        "vocabulary_iri": "review\t:draft",
    }])
    eml._add_non_numeric_domain(document, {"measurement_scale": "nominal"},
                                {"table_id": "counts", "column_name": "sex"}, {"codes": codes})
    assert document.find(".//codeDefinition/source").text == "review\t:draft"
    assert _contains_review_iri(ET.tostring(document, encoding="unicode"), eml._eml_iri_values(document))


@pytest.mark.parametrize("prefix,expected", [("\t", True), ("\n", False), ("\u00a0", False)])
@pytest.mark.parametrize("leading", ["", " ", "\t"])
@pytest.mark.parametrize("position", ["first", "second"])
def test_eml_schema_pair_keeps_both_uris_raw(prefix, expected, leading, position):
    from metasalmonpy.metadata import _contains_review_iri
    marker = prefix + "review :draft"
    pair = marker + " https://example.org/eml.xsd" if position == "first" else "https://eml.ecoinformatics.org/eml-2.2.0 " + marker
    document = ET.Element("eml", {
        "{http://www.w3.org/2001/XMLSchema-instance}schemaLocation":
            leading + pair,
    })
    assert _contains_review_iri(ET.tostring(document, encoding="unicode"), eml._eml_iri_values(document)) is expected


@pytest.mark.parametrize("carrier", ["about", "resource", "datatype"])
def test_ore_iri_inventory_reaches_real_namespace_qualified_fixture_fields(carrier):
    from metasalmonpy.metadata import _contains_review_iri
    document = ET.parse(Path(__file__).parent / "data/knb/r/public/resource-map.rdf").getroot()
    attribute = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}" + carrier
    element = next(element for element in document.iter() if attribute in element.attrib)
    element.set(attribute, "review\t:draft")
    xml = ET.tostring(document, encoding="unicode")
    assert "review&#09;:draft" in xml
    assert _contains_review_iri(xml, knb_publication._ore_iri_values(document))


@pytest.mark.parametrize("carrier", ["identifier", "modified", "atLocation"])
def test_ore_string_date_and_local_path_text_are_not_decoded_iri_slots(carrier):
    from metasalmonpy.metadata import _contains_review_iri
    document = ET.parse(Path(__file__).parent / "data/knb/r/public/resource-map.rdf").getroot()
    element = next(element for element in document.iter() if knb_publication._local_name(element.tag) == carrier)
    element.text = "Review: ordinary value"
    assert not _contains_review_iri(ET.tostring(document, encoding="unicode"), knb_publication._ore_iri_values(document))


@pytest.mark.parametrize("carrier", ["valueURI", "directory"])
def test_eml_public_export_refuses_markers_on_emitted_iri_fields(tmp_path, monkeypatch, carrier):
    pytest.importorskip("yaml")
    pytest.importorskip("lxml")
    target = _fixture_sdp(tmp_path, "eml", "sdp-default")
    build = eml._build_document
    def marked_document(*args, **kwargs):
        built = build(*args, **kwargs)
        document = built["document"]
        if carrier == "valueURI":
            document.find(".//annotation/valueURI").text = " review :draft"
        else:
            # The builder emits this actual URI attribute for an ORCID party.
            # Serialization escapes its tab, so the original value must reach
            # the marker guard. xml:lang/title are not IRI carriers.
            document.find(".//userId").set("directory", "review\t:draft")
            assert "review&#09;:draft" in ET.tostring(document, encoding="unicode")
        return built
    monkeypatch.setattr(eml, "_build_document", marked_document)
    with pytest.raises(ValueError, match="unresolved.*REVIEW"):
        write_eml_from_sdp(target, overwrite=True)


@pytest.mark.parametrize("carrier", ["resource", "about", "datatype"])
def test_knb_public_dry_run_refuses_markers_on_emitted_rdf_iri_fields(tmp_path, monkeypatch, carrier):
    pytest.importorskip("yaml")
    pytest.importorskip("lxml")
    target = _fixture_sdp(tmp_path, "knb", "sdp-public")
    build = knb_publication._build_ore
    resolve = knb_publication._resolve_url
    def marked_document(*args, **kwargs):
        if carrier == "about":
            # Change an actual emitted member URL in both builder and expected
            # plan relationships. A membership/identifier mismatch would fail
            # before the marker guard and would not establish its reach.
            member_pid = next(str(member["pid"]) for member in args[3] if member["role"] == "data")
            def marked_member_url(pid, config):
                value = resolve(pid, config)
                return "review\t:" + value if str(pid) == member_pid else value
            monkeypatch.setattr(knb_publication, "_resolve_url", marked_member_url)
        document = build(*args, **kwargs)
        if carrier == "resource":
            next(element for element in document.iter() if element.tag == "dcterms:creator").set("rdf:resource", "review\t:draft")
        elif carrier == "about":
            assert any(element.get("rdf:about", "").startswith("review\t:") for element in document)
        else:
            next(element for element in document.iter() if "rdf:datatype" in element.attrib).set("rdf:datatype", "review\t:draft")
        assert "review&#09;:" in ET.tostring(document, encoding="unicode")
        return document
    monkeypatch.setattr(knb_publication, "_build_ore", marked_document)
    with pytest.raises(ValueError, match="local/review marker"):
        publish_sdp_to_knb(target, public=True, dry_run=True, knb_environment="production")


@pytest.mark.parametrize("narrative", ["Review: counts were independently checked.", "Peer review: ordinary narrative", "preview: ordinary narrative"])
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


@pytest.mark.parametrize("family,name,writer", [
    ("eml", "sdp-default", "eml"), ("knb", "sdp-public", "knb"),
])
def test_public_export_keeps_an_abstract_beginning_review(tmp_path, family, name, writer):
    pytest.importorskip("yaml")
    pytest.importorskip("lxml")
    target = _fixture_sdp(tmp_path, family, name)
    dataset_path = target / "metadata" / "dataset.csv"
    dataset = read_sdp_csv(dataset_path)
    narrative = "Review: counts were independently checked."
    dataset.loc[0, "description"] = narrative
    dataset.to_csv(dataset_path, index=False)
    if writer == "eml":
        write_eml_from_sdp(target, overwrite=True)
    else:
        result = publish_sdp_to_knb(target, public=True, dry_run=True, knb_environment="production")
        assert result["status"] == "dry_run"
    assert ET.parse(target / "metadata" / "eml.xml").find("dataset/abstract/para").text == narrative


@pytest.mark.parametrize("carrier", ["label", "alternateIdentifier", "system"])
def test_eml_public_export_keeps_review_in_non_iri_values(tmp_path, monkeypatch, carrier):
    pytest.importorskip("yaml")
    pytest.importorskip("lxml")
    target = _fixture_sdp(tmp_path, "eml", "sdp-default")
    build = eml._build_document
    def narrated_document(*args, **kwargs):
        built = build(*args, **kwargs)
        document = built["document"]
        if carrier == "label":
            document.find(".//annotation/valueURI").set("label", "Review: counted terms")
        elif carrier == "alternateIdentifier":
            document.find("dataset/alternateIdentifier").text = "Review: ordinary identifier"
        else:
            document.set("system", "Review: repository label")
        return built
    monkeypatch.setattr(eml, "_build_document", narrated_document)
    write_eml_from_sdp(target, overwrite=True)


@pytest.mark.parametrize("narrative", ["Review: counts were independently checked.", "Peer review: ordinary narrative", "preview: ordinary narrative"])
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
