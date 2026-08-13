import os
import shutil
import subprocess
import tempfile
import unittest

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None

from metasalmonpy import (
    read_salmon_datapackage,
    validate_dictionary,
    write_salmon_datapackage,
)


R_LIB_PATH = "/tmp/metasalmon-lib"
HAVE_R = shutil.which("Rscript") is not None and os.path.isdir(R_LIB_PATH)


@unittest.skipUnless(pd is not None, "pandas not installed")
@unittest.skipUnless(HAVE_R, "Rscript or metasalmon library not available")
class RoundTripTests(unittest.TestCase):
    def test_r_to_python_roundtrip(self):
        pkg_dir = tempfile.mkdtemp(prefix="r-to-py-")
        env = os.environ.copy()
        env["R_LIBS"] = R_LIB_PATH
        env["PKG_DIR"] = pkg_dir

        r_script = r"""
        suppressMessages(library(metasalmon))
        dir <- Sys.getenv("PKG_DIR")
        df <- data.frame(species = c("Coho","Chinook"), count = c(1L,2L))
        dataset_meta <- data.frame(dataset_id="r-demo", title="R Demo", description="desc")
        table_meta <- data.frame(dataset_id="r-demo", table_id="observations", file_name="observations.csv", table_label="Observations")
        dict <- infer_dictionary(df, dataset_id="r-demo", table_id="observations")
        dict$column_role[dict$column_name == "count"] <- "attribute"
        write_salmon_datapackage(resources=list(observations=df), dataset_meta=dataset_meta, table_meta=table_meta, dict=dict, path=dir, overwrite=TRUE)
        """
        subprocess.run(["Rscript", "-e", r_script], env=env, check=True, text=True)

        pkg = read_salmon_datapackage(pkg_dir)
        self.assertEqual(pkg["dataset"]["dataset_id"].iloc[0], "r-demo")
        self.assertIn("observations", pkg["resources"])
        self.assertFalse(pkg["dictionary"].empty)

    def test_python_to_r_roundtrip(self):
        pkg_dir = tempfile.mkdtemp(prefix="py-to-r-")
        df = pd.DataFrame({"species": ["Coho"], "count": [5]})
        dataset_meta = pd.DataFrame({"dataset_id": ["py-demo"], "title": ["Py Demo"], "description": ["desc"]})
        table_meta = pd.DataFrame(
            {"dataset_id": ["py-demo"], "table_id": ["observations"], "file_name": ["observations.csv"], "table_label": ["Observations"]}
        )
        dict_df = pd.DataFrame(
            {
                "dataset_id": ["py-demo", "py-demo"],
                "table_id": ["observations", "observations"],
                "column_name": ["species", "count"],
                "column_label": ["species", "count"],
                "column_description": ["", ""],
                "column_role": ["attribute", "attribute"],
                "value_type": ["string", "integer"],
                "required": [False, False],
            }
        )
        dict_df = validate_dictionary(dict_df)

        write_salmon_datapackage(
            {"observations": df},
            dataset_meta,
            table_meta,
            dict_df,
            path=pkg_dir,
            overwrite=True,
        )

        env = os.environ.copy()
        env["R_LIBS"] = R_LIB_PATH
        env["PKG_DIR"] = pkg_dir
        env["OUT_DATASET"] = os.path.join(pkg_dir, "r_dataset.csv")

        r_script = r"""
        suppressMessages(library(metasalmon))
        dir <- Sys.getenv("PKG_DIR")
        out <- Sys.getenv("OUT_DATASET")
        pkg <- read_salmon_datapackage(dir)
        write.csv(pkg$dataset, out, row.names = FALSE)
        """
        subprocess.run(["Rscript", "-e", r_script], env=env, check=True, text=True)

        r_dataset = pd.read_csv(env["OUT_DATASET"])
        self.assertEqual(r_dataset["dataset_id"].iloc[0], "py-demo")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
