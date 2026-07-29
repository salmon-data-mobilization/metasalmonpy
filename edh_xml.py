from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd

NS = {
    "gmd": "http://www.isotc211.org/2005/gmd",
    "gco": "http://www.isotc211.org/2005/gco",
    "gml": "http://www.opengis.net/gml/3.2",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
}

for prefix, uri in NS.items():
    ET.register_namespace(prefix, uri)


def _tag(name: str) -> str:
    prefix, local = name.split(":", 1)
    return f"{{{NS[prefix]}}}{local}"


def _child(parent, name: str, text: Optional[str] = None, **attrs):
    node = ET.SubElement(parent, _tag(name), attrs)
    if text is not None:
        node.text = str(text)
    return node


def _has_value(value) -> bool:
    return not (value is None or pd.isna(value) or str(value).strip() == "")


def _meta(row: pd.Series, name: str, default=None, aliases=()):
    for candidate in (name, *aliases):
        if candidate in row and _has_value(row[candidate]):
            return str(row[candidate])
    return default


def _split_multi(value) -> list[str]:
    if not _has_value(value):
        return []
    out = []
    seen = set()
    for part in re.split(r"[;,]", str(value)):
        token = part.strip()
        key = token.lower()
        if token and key not in seen:
            out.append(token)
            seen.add(key)
    return out


def _deterministic_uuid(value: str) -> str:
    h = list(hashlib.md5(value.encode("utf-8")).hexdigest())
    h[12] = "5"
    h[16] = "89ab"[int(h[16], 16) % 4]
    text = "".join(h)
    return f"{text[0:8]}-{text[8:12]}-{text[12:16]}-{text[16:20]}-{text[20:32]}"


def _looks_like_uuid(value: str) -> bool:
    return re.search(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", value, re.I) is not None


def _code(parent, node_name: str, child_name: str, value: str, code_list: str):
    node = _child(parent, node_name)
    _child(node, child_name, value, codeList=code_list, codeListValue=value)
    return node


def _text_node(parent, node_name: str, value, localized=None, include_locale=False):
    if not _has_value(value):
        return None
    node = _child(parent, node_name)
    _child(node, "gco:CharacterString", str(value))
    if include_locale:
        node.set(f"{{{NS['xsi']}}}type", "gmd:PT_FreeText_PropertyType")
        free = _child(node, "gmd:PT_FreeText")
        group = _child(free, "gmd:textGroup")
        loc = _child(group, "gmd:LocalisedCharacterString", str(localized) if _has_value(localized) else str(value))
        loc.set("locale", "#fra")
    return node


def _date_node(parent, node_name: str, value):
    if not _has_value(value):
        return None
    node = _child(parent, node_name)
    child = "gco:DateTime" if "T" in str(value) else "gco:Date"
    _child(node, child, str(value))
    return node


def _normalize(value, mapping, fallback):
    if not _has_value(value):
        return fallback
    token = re.sub(r"\s+", " ", re.sub(r"[_-]+", " ", str(value).strip().lower()))
    return mapping.get(token, fallback)


def edh_build_iso19139_xml(
    dataset_meta: pd.DataFrame,
    output_path: Optional[str] = None,
    file_identifier: Optional[str] = None,
    language: str = "eng",
    date_stamp=None,
    profile: str = "dfo_edh_hnap",
) -> dict:
    """
    Build DFO HNAP or generic ISO 19139 XML from dataset metadata.

    Parameters
    ----------
    dataset_meta
        Single-row normalized dataset metadata DataFrame.
    output_path
        Optional XML output path.
    file_identifier
        Optional explicit metadata file identifier.
    language
        ISO 639-2 language code.
    date_stamp
        Optional metadata date override.
    profile
        ``"dfo_edh_hnap"`` or ``"iso19139"``.

    Returns
    -------
    dict
        XML text, output path when written, and profile metadata.
    """
    if profile not in {"dfo_edh_hnap", "iso19139"}:
        raise ValueError("profile must be 'dfo_edh_hnap' or 'iso19139'.")
    if not isinstance(dataset_meta, pd.DataFrame) or len(dataset_meta) != 1:
        raise ValueError("dataset_meta must be a single-row DataFrame.")
    missing = [col for col in ["dataset_id", "title", "description"] if col not in dataset_meta.columns]
    if missing:
        raise ValueError(f"dataset_meta is missing required columns: {missing}")

    row = dataset_meta.iloc[0]
    include_locale = profile == "dfo_edh_hnap"
    dataset_id = _meta(row, "dataset_id")
    fid = file_identifier or (_deterministic_uuid(dataset_id) if include_locale and not _looks_like_uuid(dataset_id) else dataset_id)
    effective_date = _meta(row, "modified", default=str(date_stamp or date.today()))
    update_frequency = _normalize(
        _meta(row, "update_frequency", default="unknown" if include_locale else None),
        {
            "annual": "annually",
            "annually": "annually",
            "yearly": "annually",
            "daily": "daily",
            "weekly": "weekly",
            "monthly": "monthly",
            "unknown": "unknown",
            "as needed": "asNeeded",
            "asneeded": "asNeeded",
        },
        "unknown" if include_locale else None,
    )
    classification = _normalize(
        _meta(row, "security_classification", default="unclassified" if include_locale else None),
        {"public": "unclassified", "open": "unclassified", "unclassified": "unclassified", "restricted": "restricted", "confidential": "confidential"},
        "unclassified" if include_locale else None,
    )
    status = _normalize(
        _meta(row, "status", default="completed" if include_locale else None),
        {"complete": "completed", "completed": "completed", "ongoing": "onGoing", "planned": "planned"},
        "completed" if include_locale else None,
    )
    code_list_base = "http://nap.geogratis.gc.ca/metadata/register/napMetadataRegister.xml#" if include_locale else "http://www.isotc211.org/2005/resources/codeList.xml#"

    root = ET.Element(_tag("gmd:MD_Metadata"))
    _child(_child(root, "gmd:fileIdentifier"), "gco:CharacterString", fid)
    lang = _child(root, "gmd:language")
    if include_locale:
        _child(lang, "gco:CharacterString", f"{language}; CAN")
    else:
        _child(lang, "gmd:LanguageCode", language, codeList="http://www.loc.gov/standards/iso639-2/", codeListValue=language)
    _code(root, "gmd:characterSet", "gmd:MD_CharacterSetCode", "utf8", code_list_base + "MD_CharacterSetCode")
    _code(
        root,
        "gmd:hierarchyLevel",
        "gmd:MD_ScopeCode",
        _meta(row, "hierarchy_level", default="nonGeographicDataset" if include_locale else "dataset"),
        code_list_base + "MD_ScopeCode",
    )
    _date_node(root, "gmd:dateStamp", effective_date)
    if include_locale:
        _text_node(root, "gmd:metadataStandardName", "North American Profile of ISO 19115:2003 - Geographic information - Metadata", localized="Profil nord-americain de la norme ISO 19115:2003 - Information geographique - Metadonnees", include_locale=True)
        _text_node(root, "gmd:metadataStandardVersion", "CAN/CGSB-171.100-2009")
        _child(_child(root, "gmd:dataSetURI"), "gco:CharacterString", dataset_id)
        locale = _child(root, "gmd:locale")
        pt = _child(locale, "gmd:PT_Locale")
        pt.set("id", "fra")
        _child(_child(pt, "gmd:languageCode"), "gmd:LanguageCode", "French; Francais", codeList=code_list_base + "LanguageCode", codeListValue="fra")
        _child(_child(pt, "gmd:country"), "gmd:Country", "Canada; Canada", codeList=code_list_base + "Country", codeListValue="CAN")
        _code(pt, "gmd:characterEncoding", "gmd:MD_CharacterSetCode", "utf8", code_list_base + "MD_CharacterSetCode")
    else:
        _text_node(root, "gmd:metadataStandardName", "ISO 19115:2003/19139")
        _text_node(root, "gmd:metadataStandardVersion", "ISO 19139")

    if _has_value(_meta(row, "reference_system", aliases=("crs", "epsg_code"))):
        ref = _child(root, "gmd:referenceSystemInfo")
        md_ref = _child(ref, "gmd:MD_ReferenceSystem")
        rs_id = _child(_child(md_ref, "gmd:referenceSystemIdentifier"), "gmd:RS_Identifier")
        _text_node(rs_id, "gmd:code", _meta(row, "reference_system", aliases=("crs", "epsg_code")), include_locale=include_locale)

    ident = _child(root, "gmd:identificationInfo")
    data_ident = _child(ident, "gmd:MD_DataIdentification")
    citation = _child(_child(data_ident, "gmd:citation"), "gmd:CI_Citation")
    _text_node(citation, "gmd:title", _meta(row, "title"), localized=_meta(row, "title_fr"), include_locale=include_locale)
    pub_date = _child(_child(citation, "gmd:date"), "gmd:CI_Date")
    _date_node(pub_date, "gmd:date", effective_date)
    _code(pub_date, "gmd:dateType", "gmd:CI_DateTypeCode", "publication", code_list_base + "CI_DateTypeCode")
    if include_locale and fid != dataset_id:
        _text_node(_child(_child(citation, "gmd:identifier"), "gmd:MD_Identifier"), "gmd:code", dataset_id)
    if _has_value(_meta(row, "creator")):
        party = _child(citation, "gmd:citedResponsibleParty")
        rp = _child(party, "gmd:CI_ResponsibleParty")
        _text_node(rp, "gmd:organisationName", _meta(row, "creator"), include_locale=include_locale)
        _code(rp, "gmd:role", "gmd:CI_RoleCode", "principalInvestigator", code_list_base + "CI_RoleCode")

    _text_node(data_ident, "gmd:abstract", _meta(row, "description"), localized=_meta(row, "description_fr"), include_locale=include_locale)
    for keyword in _split_multi(_meta(row, "keywords")) + ([_meta(row, "dataset_type")] if _has_value(_meta(row, "dataset_type")) else []):
        if "keywords_parent" not in locals():
            keywords_parent = _child(_child(data_ident, "gmd:descriptiveKeywords"), "gmd:MD_Keywords")
        _child(_child(keywords_parent, "gmd:keyword"), "gco:CharacterString", keyword)
    for topic in _split_multi(_meta(row, "topic_categories")):
        _child(_child(data_ident, "gmd:topicCategory"), "gmd:MD_TopicCategoryCode", topic)
    if _has_value(status):
        _code(data_ident, "gmd:status", "gmd:MD_ProgressCode", status, code_list_base + "MD_ProgressCode")
    if _has_value(update_frequency):
        maint = _child(_child(data_ident, "gmd:resourceMaintenance"), "gmd:MD_MaintenanceInformation")
        _code(maint, "gmd:maintenanceAndUpdateFrequency", "gmd:MD_MaintenanceFrequencyCode", update_frequency, code_list_base + "MD_MaintenanceFrequencyCode")
    if _has_value(_meta(row, "license")) or _has_value(classification):
        legal = _child(_child(data_ident, "gmd:resourceConstraints"), "gmd:MD_LegalConstraints")
        _text_node(legal, "gmd:useLimitation", _meta(row, "license"))
        if _has_value(classification):
            _code(legal, "gmd:classification", "gmd:MD_ClassificationCode", classification, code_list_base + "MD_ClassificationCode")

    has_extent = any(_has_value(_meta(row, name)) for name in ["spatial_extent", "temporal_start", "temporal_end", "bbox_west", "bbox_east", "bbox_south", "bbox_north"])
    if has_extent:
        ex_extent = _child(_child(data_ident, "gmd:extent"), "gmd:EX_Extent")
        bbox_values = [_meta(row, name) for name in ["bbox_west", "bbox_east", "bbox_south", "bbox_north"]]
        if all(_has_value(value) for value in bbox_values):
            bbox = _child(_child(ex_extent, "gmd:geographicElement"), "gmd:EX_GeographicBoundingBox")
            for name, value in zip(["westBoundLongitude", "eastBoundLongitude", "southBoundLatitude", "northBoundLatitude"], bbox_values):
                _child(_child(bbox, f"gmd:{name}"), "gco:Decimal", value)
        elif _has_value(_meta(row, "spatial_extent")) and not include_locale:
            geo = _child(_child(ex_extent, "gmd:geographicElement"), "gmd:EX_GeographicDescription")
            _text_node(_child(_child(geo, "gmd:geographicIdentifier"), "gmd:MD_Identifier"), "gmd:code", _meta(row, "spatial_extent"))
        if _has_value(_meta(row, "temporal_start")) or _has_value(_meta(row, "temporal_end")):
            period = _child(_child(_child(_child(ex_extent, "gmd:temporalElement"), "gmd:EX_TemporalExtent"), "gmd:extent"), "gml:TimePeriod")
            period.set(f"{{{NS['gml']}}}id", "tp-" + re.sub(r"[^A-Za-z0-9]", "", fid))
            if _has_value(_meta(row, "temporal_start")):
                _child(period, "gml:beginPosition", _meta(row, "temporal_start"))
            if _has_value(_meta(row, "temporal_end")):
                _child(period, "gml:endPosition", _meta(row, "temporal_end"))

    supplemental = []
    if include_locale and _has_value(_meta(row, "spatial_extent")):
        supplemental.append(f"spatial_extent={_meta(row, 'spatial_extent')}")
    if _has_value(_meta(row, "provenance_note")):
        supplemental.append(f"provenance_note={_meta(row, 'provenance_note')}")
    if supplemental:
        _text_node(data_ident, "gmd:supplementalInformation", "; ".join(supplemental), localized="; ".join(supplemental), include_locale=include_locale)

    distribution_url = _meta(row, "distribution_url", aliases=("download_url", "data_url", "access_url"))
    if _has_value(distribution_url):
        online = _child(_child(_child(_child(_child(root, "gmd:distributionInfo"), "gmd:MD_Distribution"), "gmd:transferOptions"), "gmd:MD_DigitalTransferOptions"), "gmd:onLine")
        resource = _child(online, "gmd:CI_OnlineResource")
        _child(_child(resource, "gmd:linkage"), "gmd:URL", distribution_url)
        _text_node(resource, "gmd:name", _meta(row, "distribution_name", aliases=("download_name",)))
        _text_node(resource, "gmd:description", _meta(row, "distribution_description", aliases=("download_description",)))

    xml_text = ET.tostring(root, encoding="unicode")
    resolved_path = None
    if output_path is not None:
        resolved_path = Path(output_path)
        resolved_path.parent.mkdir(parents=True, exist_ok=True)
        resolved_path.write_text(xml_text, encoding="utf-8")
    return {"xml": xml_text, "path": resolved_path}


def edh_build_hnap_xml(
    dataset_meta: pd.DataFrame,
    output_path: Optional[str] = None,
    file_identifier: Optional[str] = None,
    language: str = "eng",
    date_stamp=None,
) -> dict:
    """Build the supported DFO EDH HNAP metadata export."""
    return edh_build_iso19139_xml(
        dataset_meta=dataset_meta,
        output_path=output_path,
        file_identifier=file_identifier,
        language=language,
        date_stamp=date_stamp,
        profile="dfo_edh_hnap",
    )


def write_edh_xml_from_sdp(
    path,
    output_path=None,
    overwrite: bool = True,
    language: str = "eng",
    file_identifier: Optional[str] = None,
    date_stamp=None,
) -> dict:
    """Rebuild HNAP XML from reviewed package metadata."""
    package_path = Path(path)
    if not package_path.is_dir():
        raise FileNotFoundError(
            f"Salmon Data Package directory does not exist: {package_path}"
        )
    from .package_io import _collect_review_issues, read_salmon_datapackage

    package = read_salmon_datapackage(package_path)
    review_issues = _collect_review_issues(package)
    if review_issues:
        raise ValueError(
            "Cannot rebuild EDH XML from unreviewed package metadata. "
            + " ".join(review_issues[:5])
        )
    dataset_meta = package["dataset"]
    if len(dataset_meta) != 1:
        raise ValueError(
            "metadata/dataset.csv must contain exactly one row."
        )
    target = (
        Path(output_path)
        if output_path is not None
        else package_path / "metadata" / "metadata-edh-hnap.xml"
    )
    if target.exists() and not overwrite:
        raise FileExistsError(
            f"EDH XML already exists at {target}. Set overwrite=True to replace."
        )
    return edh_build_hnap_xml(
        dataset_meta=dataset_meta,
        output_path=target,
        file_identifier=file_identifier,
        language=language,
        date_stamp=date_stamp,
    )


__all__ = [
    "edh_build_hnap_xml",
    "edh_build_iso19139_xml",
    "write_edh_xml_from_sdp",
]
