import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import pandas as pd

from salmonpy import edh_build_iso19139_xml


NS = {
    "gmd": "http://www.isotc211.org/2005/gmd",
    "gco": "http://www.isotc211.org/2005/gco",
    "gml": "http://www.opengis.net/gml/3.2",
}


class EdhXmlTests(unittest.TestCase):
    def test_legacy_iso_export_available(self):
        meta = pd.DataFrame(
            {
                "dataset_id": ["fraser-coho-2024"],
                "title": ["Fraser River Coho Escapement Data"],
                "description": ["Sample escapement monitoring data for coho salmon in PFMA 29"],
                "creator": ["DFO Pacific Science"],
                "license": ["Open Government Licence - Canada"],
                "temporal_start": ["2001"],
                "temporal_end": ["2024"],
                "spatial_extent": ["PFMA 29, Fraser River watershed"],
                "topic_categories": ["biota;inlandWaters"],
                "keywords": ["coho;escapement;Fraser River"],
                "update_frequency": ["annually"],
                "security_classification": ["unclassified"],
            }
        )
        out = Path(tempfile.mktemp(suffix=".xml"))
        result = edh_build_iso19139_xml(meta, output_path=str(out), date_stamp="2026-03-03", profile="iso19139")
        self.assertTrue(out.exists())
        self.assertIn("xml", result)
        root = ET.parse(out).getroot()
        self.assertEqual(root.findtext(".//gmd:fileIdentifier/gco:CharacterString", namespaces=NS), "fraser-coho-2024")
        self.assertEqual(root.findtext(".//gmd:CI_Citation/gmd:title/gco:CharacterString", namespaces=NS), "Fraser River Coho Escapement Data")
        self.assertEqual(root.findtext(".//gmd:hierarchyLevel/*", namespaces=NS), "dataset")
        self.assertEqual(len(root.findall(".//gmd:topicCategory", namespaces=NS)), 2)
        self.assertEqual(len(root.findall(".//gmd:EX_GeographicDescription", namespaces=NS)), 1)
        self.assertEqual(len(root.findall(".//gml:TimePeriod", namespaces=NS)), 1)

    def test_hnap_default_structure(self):
        meta = pd.DataFrame(
            {
                "dataset_id": ["pacific-marine-habitat-classes"],
                "title": ["Pacific Marine Habitat Classes"],
                "title_fr": ["Categories d'habitat marin du Pacifique"],
                "description": ["Marine habitat class polygons for Pacific waters."],
                "description_fr": ["Polygones des categories d'habitat marin pour les eaux du Pacifique."],
                "creator": ["Government of Canada; Fisheries and Oceans Canada; Pacific Science; Marine Spatial Planning"],
                "modified": ["2022-11-10T12:34:56"],
                "bbox_west": [-137.4],
                "bbox_east": [-122.1],
                "bbox_south": [48.1],
                "bbox_north": [54.9],
                "topic_categories": ["oceans;biota;oceans"],
                "keywords": ["habitat;marine;Pacific;marine"],
                "update_frequency": ["annual"],
                "security_classification": ["public"],
                "provenance_note": ["Compiled from workflows."],
                "reference_system": ["EPSG:3005"],
                "distribution_url": ["https://example.org/download"],
                "status": ["completed"],
                "spatial_extent": ["Pacific Region marine waters"],
            }
        )
        out = Path(tempfile.mktemp(suffix=".xml"))
        edh_build_iso19139_xml(meta, output_path=str(out))
        root = ET.parse(out).getroot()
        file_identifier = root.findtext(".//gmd:fileIdentifier/gco:CharacterString", namespaces=NS)
        self.assertRegex(file_identifier, r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
        self.assertEqual(root.findtext(".//gmd:dataSetURI/gco:CharacterString", namespaces=NS), "pacific-marine-habitat-classes")
        self.assertEqual(root.findtext(".//gmd:metadataStandardVersion/gco:CharacterString", namespaces=NS), "CAN/CGSB-171.100-2009")
        self.assertEqual(root.findtext(".//gmd:hierarchyLevel/*", namespaces=NS), "nonGeographicDataset")
        self.assertEqual(len(root.findall(".//gmd:locale/gmd:PT_Locale", namespaces=NS)), 1)
        self.assertGreaterEqual(len(root.findall(".//gmd:PT_FreeText", namespaces=NS)), 3)
        self.assertEqual(root.findtext(".//gmd:status/*", namespaces=NS), "completed")
        self.assertEqual(root.findtext(".//gmd:maintenanceAndUpdateFrequency/*", namespaces=NS), "annually")
        self.assertEqual(root.findtext(".//gmd:classification/*", namespaces=NS), "unclassified")
        self.assertEqual(len(root.findall(".//gmd:EX_GeographicBoundingBox", namespaces=NS)), 1)
        self.assertEqual(len(root.findall(".//gmd:referenceSystemInfo", namespaces=NS)), 1)
        self.assertEqual(len(root.findall(".//gmd:distributionInfo//gmd:CI_OnlineResource", namespaces=NS)), 1)
        self.assertEqual(len(root.findall(".//gmd:citedResponsibleParty", namespaces=NS)), 1)

    def test_required_columns(self):
        with self.assertRaisesRegex(ValueError, "missing required"):
            edh_build_iso19139_xml(pd.DataFrame({"dataset_id": ["x"], "title": ["Missing description"]}))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
