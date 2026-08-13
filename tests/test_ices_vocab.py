import unittest
from unittest import mock

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None

if pd is None:
    raise unittest.SkipTest("pandas not installed")

import metasalmonpy.ices_vocab as iv


class IcesVocabTests(unittest.TestCase):
    def test_ices_code_types(self):
        fake = [
            {"key": "Gear", "description": "Gear Type Codes", "guid": "g1"},
            {"key": "TS_Sex", "description": "Sex Codes (Fisheries)", "guid": "g2"},
        ]
        with mock.patch.object(iv, "_safe_json", return_value=fake):
            df = iv.ices_code_types()
        self.assertFalse(df.empty)
        self.assertIn("key", df.columns)

        with mock.patch.object(iv, "_safe_json", return_value=fake):
            filtered = iv.ices_find_code_types("gear")
        self.assertEqual(filtered.iloc[0]["key"], "Gear")

    def test_ices_codes(self):
        fake = [
            {"key": "BOT", "description": "Bottom Trawl"},
            {"key": "BMT", "description": "Beam trawl"},
        ]
        with mock.patch.object(iv, "_safe_json", return_value=fake):
            df = iv.ices_codes("Gear")
        self.assertFalse(df.empty)
        self.assertEqual(df.iloc[0]["code_type"], "Gear")
        self.assertIn("/CodeDetail/Gear/BOT", df.iloc[0]["url"])

        with mock.patch.object(iv, "_safe_json", return_value=fake):
            filtered = iv.ices_find_codes("beam", "Gear")
        self.assertEqual(filtered.iloc[0]["key"], "BMT")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

