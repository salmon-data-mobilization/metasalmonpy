"""The write → read → write round trip, against a package R wrote.

``tests/data/resource_types/r-package/`` was produced under a read-only
extraction of metasalmon **main at e02111a** (the v0.3.0 release tree plus the
post-0.3.0 fixes): the 0.2.1-era fixture was run through R's
``migrate_sdp_methods()`` and then canonicalized with R's own
read→``write_salmon_datapackage()`` round trip (verified byte-idempotent in R
before committing). Reading it here and writing it straight back must
reproduce every byte, because that is what "the dictionary is the sole type
authority" buys: a value that survives the read is a value the writer can put
back.

R adopted C collation at 0.2.0, so these byte claims carry no locale caveat.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import pandas as pd
import pytest

from metasalmonpy import (
    infer_value_type,
    read_salmon_datapackage,
    validate_salmon_datapackage,
    write_salmon_datapackage,
)

R_PACKAGE = Path(__file__).resolve().parent / "data" / "resource_types" / "r-package"
R_FILES = (
    "data/obs.csv",
    "metadata/dataset.csv",
    "metadata/tables.csv",
    "metadata/column_dictionary.csv",
    "metadata/codes.csv",
    "datapackage.json",
)


def _roundtrip(tmp_path):
    package = read_salmon_datapackage(str(R_PACKAGE))
    destination = tmp_path / "roundtrip"
    write_salmon_datapackage(
        resources=package["resources"],
        dataset_meta=package["dataset"],
        table_meta=package["tables"],
        dict_df=package["dictionary"],
        codes=package["codes"],
        path=str(destination),
    )
    return package, destination


@pytest.mark.parametrize("relative", R_FILES)
def test_the_round_trip_reproduces_era_r_bytes(tmp_path, relative):
    _, destination = _roundtrip(tmp_path)
    assert (destination / relative).read_bytes() == (R_PACKAGE / relative).read_bytes()


def test_the_reader_types_every_declared_column_from_the_dictionary(tmp_path):
    """The dtypes are the pandas counterparts of R's classes for this package.

    Measured by reading the same package with both implementations: R gave
    ``character, numeric, numeric, logical, Date, POSIXct, character``.
    """
    package, _ = _roundtrip(tmp_path)
    frame = package["resources"]["obs"]
    assert frame["brood_year"].dtype == "float64"  # declared integer
    assert frame["count"].dtype == "float64"
    assert str(frame["observed"].dtype) == "boolean"
    assert isinstance(frame["survey_date"].iloc[0], _dt.date)
    assert not isinstance(frame["survey_date"].iloc[0], _dt.datetime)
    assert pd.api.types.is_datetime64_any_dtype(frame["survey_time"].dtype)
    # Undeclared-by-type columns stay text rather than being guessed.
    assert frame["note"].iloc[0] == "a"


def test_a_literal_NA_in_a_string_column_survives_the_round_trip(tmp_path):
    """PARITY.md row 21, still true once the reader types columns.

    Era R reads this cell as missing (``na = c("", "NA")``) and writes ``NA``
    back, so the bytes agree by coincidence; metasalmon 0.2.4 onward agrees
    with this package that the token is data.
    """
    package, _ = _roundtrip(tmp_path)
    assert list(package["resources"]["obs"]["site_id"]) == ["A", "B", "C", "NA"]


def test_a_value_that_fails_its_declared_type_keeps_its_token_and_is_reported(tmp_path):
    package = read_salmon_datapackage(str(R_PACKAGE))
    broken = tmp_path / "broken"
    resources = {name: frame.copy() for name, frame in package["resources"].items()}
    # A precision the declared ``number`` cannot hold, written as raw text.
    resources["obs"]["count"] = ["1", "2", "3.14159265358979311600", "4"]
    write_salmon_datapackage(
        resources=resources,
        dataset_meta=package["dataset"],
        table_meta=package["tables"],
        dict_df=package["dictionary"],
        codes=package["codes"],
        path=str(broken),
    )
    reread = read_salmon_datapackage(str(broken))
    frame = reread["resources"]["obs"]
    # The raw token, not NA and not a rounded double.
    assert list(frame["count"]) == ["1", "2", "3.14159265358979311600", "4"]
    mismatches = frame.attrs["ms_value_type_mismatches"]
    assert mismatches[0]["column"] == "count"
    assert mismatches[0]["reason"] == "beyond exact numeric precision"

    issues = validate_salmon_datapackage(str(broken))["issues"]
    assert list(issues.columns) == [
        "issue_type",
        "table_id",
        "column_name",
        "value",
        "message",
    ]
    assert len(issues) == 1
    assert "beyond exact numeric precision" in issues["message"].iloc[0]
    assert issues["column_name"].iloc[0] == "count"


def test_the_descriptor_derives_every_uri_from_the_loaded_bundle(tmp_path):
    _, destination = _roundtrip(tmp_path)
    descriptor = json.loads((destination / "datapackage.json").read_text(encoding="utf-8"))
    metadata_resources = [
        resource
        for resource in descriptor["resources"]
        if str(resource["path"]).startswith("metadata/")
    ]
    assert metadata_resources, "metadata resources must be declared"
    for resource in metadata_resources:
        # metasalmon 0.2.1: per-resource schema URLs come from the bundle, not
        # from a constant composed in Python source. Before this rung the
        # metadata resources carried no ``schema`` key at all.
        assert resource["schema"].startswith(
            "https://salmon-data-mobilization.github.io/smn-data-pkg/"
            "schema/frictionless/metadata/"
        )
        assert resource["description"]
    assert descriptor["profile"] == descriptor["sdp"]["profile"]
    assert descriptor["sdp"]["specVersion"] == "sdp-0.3.0"


def test_infer_value_type_no_longer_collapses_midnight_to_a_date():
    """Public-API behaviour change: the class decides, not the values.

    A ``datetime64`` column whose values happen to be midnight used to infer
    ``"date"`` here. metasalmon 0.2.0 fixed the mirror-image defect, where
    ``POSIXt`` collapsed to ``"date"`` and ``"datetime"`` was never inferred at
    all. Both now answer from the class.
    """
    midnight = pd.Series(pd.to_datetime(["2024-01-31", "2024-02-01"]))
    assert infer_value_type(midnight) == "datetime"
    with_time = pd.Series(pd.to_datetime(["2024-01-31 10:00:00"]))
    assert infer_value_type(with_time) == "datetime"
    dates = pd.Series([_dt.date(2024, 1, 31), _dt.date(2024, 2, 1)], dtype="object")
    assert infer_value_type(dates) == "date"


def test_a_typed_frame_round_trips_through_infer_value_type(tmp_path):
    """The reader's dtypes and the inferrer's answers agree.

    Without this, reading a package and re-inferring its dictionary would
    rewrite ``datetime`` as ``date`` (or the reverse) on every pass.
    """
    package, _ = _roundtrip(tmp_path)
    frame = package["resources"]["obs"]
    declared = dict(
        zip(package["dictionary"]["column_name"], package["dictionary"]["value_type"])
    )
    for column in ("count", "observed", "survey_date", "survey_time", "note"):
        inferred = infer_value_type(frame[column])
        expected = declared[column]
        # ``integer`` reads as a double by the logged decision, so re-inferring
        # a declared integer answers ``number``; every other declared type is
        # recovered exactly.
        assert inferred == ("number" if expected == "integer" else expected), column
