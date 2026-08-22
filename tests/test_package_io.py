import tempfile
import unittest
from pathlib import Path

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None

if pd is None:
    raise unittest.SkipTest("pandas not installed")

from metasalmonpy import (
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

        tmpdir = tempfile.mkdtemp(prefix="metasalmonpy-io-")
        create_salmon_datapackage({"observations": df}, dataset_meta, table_meta, dict_df, path=tmpdir, overwrite=True)
        pkg = read_salmon_datapackage(tmpdir)

        self.assertTrue(Path(tmpdir, "metadata", "dataset.csv").exists())
        self.assertTrue(Path(tmpdir, "metadata", "tables.csv").exists())
        self.assertTrue(
            Path(tmpdir, "metadata", "column_dictionary.csv").exists()
        )
        self.assertTrue(Path(tmpdir, "data", "observations.csv").exists())
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
        tmpdir = tempfile.mkdtemp(prefix="metasalmonpy-csv-")
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

        tmpdir = tempfile.mkdtemp(prefix="metasalmonpy-from-data-")
        legacy_xml = Path(tmpdir, "legacy-iso19139.xml")
        pkg_path = create_salmon_datapackage_from_data(
            resources,
            path=str(Path(tmpdir, "pkg")),
            dataset_id="demo",
            seed_semantics=False,
            overwrite=True,
            include_edh_xml=True,
            edh_profile="iso19139",
            edh_xml_path=str(legacy_xml),
        )
        self.assertTrue((pkg_path / "metadata" / "dataset.csv").exists())
        self.assertTrue((pkg_path / "metadata" / "codes.csv").exists())
        self.assertTrue((pkg_path / "data" / "catches.csv").exists())
        self.assertTrue(legacy_xml.exists())


class EraInferenceTests(unittest.TestCase):
    """metasalmon 0.1.7's SDP-inference corrections, verified against v0.1.7."""

    def test_primary_key_must_be_complete_and_unique(self):
        # v0.1.7 stopped naming the first ID-shaped column and started
        # requiring it to be usable. Era R over these frames returns
        # "fish_id", NA and NA respectively.
        from metasalmonpy.metadata import infer_table_metadata_from_resources

        cases = (
            (pd.DataFrame({"sample_id": ["a", None, "c"], "fish_id": ["a", "b", "c"]}),
             "fish_id"),
            (pd.DataFrame({"dup_id": ["x", "x", "y"]}), None),
            (pd.DataFrame({"sample_id": ["a", " ", "c"]}), None),
        )
        for frame, expected in cases:
            with self.subTest(columns=list(frame.columns)):
                meta = infer_table_metadata_from_resources({"t": frame})
                value = meta["primary_key"].iloc[0]
                if expected is None:
                    self.assertTrue(pd.isna(value))
                else:
                    self.assertEqual(value, expected)

    def test_blank_spec_version_follows_the_profile_rules(self):
        # v0.1.7 replaced the frozen "sdp-0.1.0" literal with the vendored
        # profile version; metasalmon main's .ms_sdp_profile_version()
        # returns "sdp-0.3.0", from the same bundle both mirrors now vendor.
        from metasalmonpy.metadata import (
            SDP_PROFILE_VERSION,
            infer_dataset_metadata_from_resources,
        )
        from metasalmonpy.package_io import _fill_review_placeholders

        self.assertEqual(SDP_PROFILE_VERSION, "sdp-0.3.0")
        inferred = infer_dataset_metadata_from_resources(
            {"t": pd.DataFrame({"count": [1, 2]})}
        )
        self.assertEqual(inferred["spec_version"].iloc[0], SDP_PROFILE_VERSION)

        blank = pd.DataFrame([{"dataset_id": "d", "spec_version": "  "}])
        filled, _tables, _dictionary = _fill_review_placeholders(
            blank, pd.DataFrame(), pd.DataFrame()
        )
        self.assertEqual(filled["spec_version"].iloc[0], SDP_PROFILE_VERSION)

    def test_licence_descriptors_match_era_r(self):
        # Era R's .ms_license_descriptor() over these 21 values -- 13 accepted
        # and 8 rejected; the final branch (a canonical HTTP(S) rights URL
        # stays a URL descriptor) is the 0.1.7 addition. Keep the count in
        # PARITY.md row 26 in step with this list.
        from metasalmonpy.package_io import _license_descriptor

        accepted = {
            "Open Government Licence - Canada": {
                "name": "OGL-Canada-2.0",
                "title": "Open Government Licence - Canada",
                "path": "https://open.canada.ca/en/open-government-licence-canada",
            },
            "CC-BY-4.0": {
                "name": "CC-BY-4.0",
                "title": "Creative Commons Attribution 4.0 International",
                "path": "https://creativecommons.org/licenses/by/4.0/",
            },
            "MIT": {
                "name": "MIT",
                "title": "MIT License",
                "path": "https://opensource.org/license/mit",
            },
            "https://example.org/licence": {"path": "https://example.org/licence"},
            "http://example.org/licence": {"path": "http://example.org/licence"},
            "https://example.org/licence?a=1": {
                "path": "https://example.org/licence?a=1"
            },
            "https://example.org/licence#frag": {
                "path": "https://example.org/licence#frag"
            },
            "https://example.org/": {"path": "https://example.org/"},
            "https://Example.ORG/licence": {"path": "https://Example.ORG/licence"},
            "https://user:pw@example.org/licence": {
                "path": "https://user:pw@example.org/licence"
            },
            "https://example.org:8443/licence": {
                "path": "https://example.org:8443/licence"
            },
            "  https://example.org/licence  ": {"path": "https://example.org/licence"},
            "https://example.org/licence/": {"path": "https://example.org/licence/"},
        }
        for value, descriptor in accepted.items():
            with self.subTest(license=value):
                self.assertEqual(_license_descriptor(value), descriptor)

        rejected = (
            "https://example.org",  # curl adds the "/" path, so it never round-trips
            "https://example.org/a b",
            "ftp://example.org/x",
            "https:///licence",
            "HTTPS://example.org/licence",  # curl lowercases the scheme
            "https://example.org/./dot",  # curl resolves dot segments away
            "not a url",
            "",
        )
        for value in rejected:
            with self.subTest(license=value):
                with self.assertRaises(ValueError):
                    _license_descriptor(value)

        self.assertEqual(len(accepted) + len(rejected), 21)

    def test_percent_encoded_rights_urls_are_a_documented_divergence(self):
        """The one place these conditions do not reproduce curl's verdict.

        Measured against metasalmon v0.1.8 (R 4.5.2), not assumed: curl decodes
        an encoded separator and uppercases hex digits before comparing, so R
        rejects two of these four while ``_is_canonical_rights_url`` accepts
        all four. The register (PARITY.md row 26) confines the residual risk to
        exactly this; the assertions below are what stop that claim from
        drifting out of date in either direction.

        **Retirement condition:** delete this test when the conditions gain a
        percent-encoding normalization step that reproduces curl's, and replace
        it with equality assertions against freshly measured R verdicts.
        """
        from metasalmonpy.package_io import _is_canonical_rights_url

        agrees_with_r = (
            "https://example.org/licence%20terms",
            "https://example.org/lic%C3%A9nce",
        )
        diverges_from_r = (
            # R rejects: curl decodes %2F to "/" so the value does not round-trip.
            "https://example.org/licence%2Fterms",
            # R rejects: curl uppercases the hex digits.
            "https://example.org/lic%c3%a9nce",
        )
        for value in agrees_with_r + diverges_from_r:
            with self.subTest(license=value):
                self.assertTrue(_is_canonical_rights_url(value))

    def test_a_custom_rights_url_reaches_datapackage_json(self):
        import json

        from metasalmonpy.package_io import write_salmon_datapackage

        with tempfile.TemporaryDirectory() as tmpdir:
            target = write_salmon_datapackage(
                {"obs": pd.DataFrame({"note": ["a"]})},
                pd.DataFrame(
                    [
                        {
                            "dataset_id": "demo",
                            "title": "Demo",
                            "license": "https://example.org/rights/policy",
                        }
                    ]
                ),
                pd.DataFrame(
                    [
                        {
                            "dataset_id": "demo",
                            "table_id": "obs",
                            "file_name": "obs.csv",
                            "table_label": "Obs",
                        }
                    ]
                ),
                pd.DataFrame(
                    [
                        {
                            "dataset_id": "demo",
                            "table_id": "obs",
                            "column_name": "note",
                            "column_label": "Note",
                            "column_description": "A note.",
                            "column_role": "attribute",
                            "value_type": "string",
                            "required": False,
                        }
                    ]
                ),
                path=tmpdir,
                overwrite=True,
            )
            descriptor = json.loads(
                (Path(target) / "datapackage.json").read_text(encoding="utf-8")
            )
        self.assertEqual(
            descriptor["licenses"], [{"path": "https://example.org/rights/policy"}]
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
