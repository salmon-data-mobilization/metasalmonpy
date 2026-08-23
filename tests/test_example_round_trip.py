"""Round-trip the shipped example through the package's own validator.

The Python port of metasalmon's ``tests/testthat/test-example-round-trip.R``
(hub backlog #98/#100): the 30-row sample and its bundled metadata shipped
while no test ever pointed the validator at the artifacts the docs hand a new
user — and one would have failed until S10 chunk A replaced the corrupt
bundled dictionary. These tests are that missing gate, verified against
metasalmon ``main`` @ ``9d8f12596157dbf1c0b49f55d6e07cbece8abd70``, whose R
counterparts pass the identical assertions over byte-identical shipped files.

metasalmon also ships a fuller 173-row example whose strict validation is
pinned to exactly one known failure; this package does not carry that example
(PARITY.md row 46, open), so the strict pin here is the tiny example's:
**zero** issues.
"""

from __future__ import annotations

import csv
import warnings
from pathlib import Path

import pandas as pd

import metasalmonpy
from metasalmonpy import validate_salmon_datapackage, write_salmon_datapackage
from metasalmonpy.metadata import read_sdp_csv

DATA = Path(metasalmonpy.__file__).parent / "data"

SHIPPED_METADATA = [
    "dataset.csv",
    "tables.csv",
    "column_dictionary.csv",
    "codes.csv",
    "nuseds-fraser-coho-sample.csv",
]


def _build_tiny_example_package(path) -> Path:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        write_salmon_datapackage(
            resources={
                "nuseds_fraser_coho": read_sdp_csv(
                    DATA / "nuseds-fraser-coho-sample.csv"
                )
            },
            dataset_meta=read_sdp_csv(DATA / "dataset.csv"),
            table_meta=read_sdp_csv(DATA / "tables.csv"),
            dict_df=read_sdp_csv(DATA / "column_dictionary.csv"),
            codes=read_sdp_csv(DATA / "codes.csv"),
            path=str(path),
            overwrite=True,
        )
    return Path(path)


def test_the_30_row_example_passes_lenient_validation(tmp_path):
    package_path = _build_tiny_example_package(tmp_path / "example")
    # Backlog #98: this aborted with 2 structural issues in both mirrors —
    # START_DTT/END_DTT declared ``value_type: date`` over Oracle DD-MON-YY
    # bytes — until the shipped sample was fixed. It must also be silent:
    # no placeholder, tidy-shape, or semantic warning fires on the shipped
    # artifacts (R emits none either).
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        result = validate_salmon_datapackage(str(package_path), require_iris=False)
    assert isinstance(result, dict)
    assert len(result["issues"]) == 0


def test_the_30_row_example_passes_strict_validation_with_zero_issues(tmp_path):
    package_path = _build_tiny_example_package(tmp_path / "example")
    # The tiny example is the walkthrough artifact, so it must clear the
    # package's final gate completely. If this test starts reporting issues,
    # the shipped artifacts drifted; fix them, do not relax this assertion.
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        result = validate_salmon_datapackage(str(package_path), require_iris=True)
    assert len(result["issues"]) == 0
    semantic_issues = result["semantic_validation"]["issues"]
    assert not isinstance(semantic_issues, pd.DataFrame) or len(semantic_issues) == 0


def test_the_shipped_example_metadata_csvs_are_well_formed():
    # metasalmon's codes.csv once declared 9 header columns while every data
    # row had 8 fields, so each read emitted 26 parsing problems every caller
    # had to suppress. Any shipped metadata file must parse clean: every row
    # carries exactly the header's field count.
    for name in SHIPPED_METADATA:
        with (DATA / name).open(newline="", encoding="utf-8") as handle:
            rows = list(csv.reader(handle))
        header_width = len(rows[0])
        for line_number, row in enumerate(rows[1:], start=2):
            assert len(row) == header_width, f"{name} line {line_number}"
        # And the package reader agrees on the shape.
        frame = read_sdp_csv(DATA / name)
        assert len(frame.columns) == header_width, name
        assert len(frame) == len(rows) - 1, name
