import tempfile
import unittest
from pathlib import Path

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None

if pd is None:
    raise unittest.SkipTest("pandas not installed")

from salmonpy import (
    create_salmon_datapackage,
    create_salmon_datapackage_from_data,
    infer_salmon_datapackage_artifacts,
    read_salmon_datapackage,
    validate_dictionary,
)


class PackageIOTests(unittest.TestCase):
    def test_create_and_read_package_roundtrip(self):
        df = pd.DataFrame({"species": ["Coho"], "count": [5]})
        dataset_meta = pd.DataFrame({"dataset_id": ["demo"], "title": ["Demo"], "description": ["desc"]})
        table_meta = pd.DataFrame(
            {"dataset_id": ["demo"], "table_id": ["observations"], "file_name": ["observations.csv"], "table_label": ["Observations"]}
        )
        dict_df = pd.DataFrame(
            {
                "dataset_id": ["demo", "demo"],
                "table_id": ["observations", "observations"],
                "column_name": ["species", "count"],
                "column_label": ["species", "count"],
                "column_description": ["", ""],
                "column_role": ["attribute", "measurement"],
                "value_type": ["string", "integer"],
                "required": [False, False],
            }
        )
        dict_df = validate_dictionary(dict_df)

        tmpdir = tempfile.mkdtemp(prefix="salmonpy-io-")
        create_salmon_datapackage({"observations": df}, dataset_meta, table_meta, dict_df, path=tmpdir, overwrite=True)
        pkg = read_salmon_datapackage(tmpdir)

        self.assertTrue(Path(tmpdir, "dataset.csv").exists())
        self.assertTrue(Path(tmpdir, "tables.csv").exists())
        self.assertTrue(Path(tmpdir, "column_dictionary.csv").exists())
        self.assertIn("observations", pkg["resources"])
        self.assertFalse(pkg["dictionary"].empty)
        self.assertEqual(pkg["dataset"]["dataset_id"].iloc[0], "demo")

    def test_read_prefers_canonical_csv_without_datapackage_json(self):
        df = pd.DataFrame({"species": ["Coho"], "count": [5]})
        dataset_meta = pd.DataFrame({"dataset_id": ["demo"], "title": ["Demo"], "description": ["desc"]})
        table_meta = pd.DataFrame(
            {"dataset_id": ["demo"], "table_id": ["observations"], "file_name": ["observations.csv"], "table_label": ["Observations"]}
        )
        dict_df = validate_dictionary(
            pd.DataFrame(
                {
                    "dataset_id": ["demo", "demo"],
                    "table_id": ["observations", "observations"],
                    "column_name": ["species", "count"],
                    "column_label": ["species", "count"],
                    "column_description": ["", ""],
                    "column_role": ["attribute", "measurement"],
                    "value_type": ["string", "integer"],
                    "required": [False, False],
                }
            )
        )
        tmpdir = tempfile.mkdtemp(prefix="salmonpy-csv-")
        create_salmon_datapackage({"observations": df}, dataset_meta, table_meta, dict_df, path=tmpdir, overwrite=True)
        Path(tmpdir, "datapackage.json").unlink()
        pkg = read_salmon_datapackage(tmpdir)
        self.assertEqual(pkg["tables"]["table_id"].iloc[0], "observations")
        self.assertEqual(pkg["dictionary"]["column_name"].tolist(), ["species", "count"])

    def test_infer_and_create_from_data(self):
        resources = {
            "catches": pd.DataFrame(
                {
                    "station_id": ["A", "B"],
                    "species": ["Coho", "Chinook"],
                    "count": [10, 20],
                    "sample_date": pd.to_datetime(["2024-01-01", "2024-01-02"]),
                }
            ),
            "stations": pd.DataFrame({"station_id": ["A", "B"], "lat": [50.1, 50.2], "lon": [-125.1, -125.2]}),
        }
        artifacts = infer_salmon_datapackage_artifacts(resources, dataset_id="demo", seed_semantics=False)
        self.assertEqual(set(artifacts["resources"].keys()), {"catches", "stations"})
        self.assertIn("keywords", artifacts["dataset_meta"].columns)
        self.assertFalse(artifacts["codes"].empty)

        tmpdir = tempfile.mkdtemp(prefix="salmonpy-from-data-")
        pkg_path = create_salmon_datapackage_from_data(
            resources,
            path=str(Path(tmpdir, "pkg")),
            dataset_id="demo",
            seed_semantics=False,
            overwrite=True,
        )
        self.assertTrue((pkg_path / "dataset.csv").exists())
        self.assertTrue((pkg_path / "codes.csv").exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
