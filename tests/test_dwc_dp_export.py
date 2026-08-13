import unittest

from metasalmonpy.dwc_dp_export import build_dwc_dp_descriptor


class TestDwcDpExport(unittest.TestCase):
    def test_build_descriptor(self):
        resources = [
            {"name": "occurrence", "path": "occurrence.csv", "schema": "occurrence"},
            {"name": "event", "path": "event.csv", "schema": "event"},
        ]
        desc = build_dwc_dp_descriptor(resources, profile_version="master")
        self.assertEqual(desc["profile"], "http://rs.tdwg.org/dwc/dwc-dp")
        self.assertEqual(len(desc["resources"]), 2)
        self.assertTrue(desc["resources"][0]["schema"].endswith("occurrence.json"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
