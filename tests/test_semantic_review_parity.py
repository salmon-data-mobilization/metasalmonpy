"""A review session crosses the language boundary (hub item B-327).

The contract says a packet either package writes is one the other's ingester
accepts, and that the two record the same thing from the same answers. The
conformance tests in ``tests/test_semantic_review_packet.py`` hold this package
to metasalmon's recorded outputs; this file runs metasalmon itself, through
``tests/data/semantic_review/r-cross-language.R``, on every conformance case:

* **built in R, ingested here**: metasalmon writes the pass-1 packet, this
  package ingests the case's harness file against it and must record what the
  fixture expects -- and where the case retries, metasalmon ingests the second
  pass against the continuation packet this package wrote;
* **built here, ingested in R**: the reverse, with the second pass ingested
  here against metasalmon's continuation packet.

So each case changes hands at least once, in both directions. It runs wherever
``Rscript`` and a metasalmon with ``write_semantic_review_packet()`` are
available: the ``parity`` job of ``.github/workflows/parity.yml``, which
installs metasalmon ``main`` into ``/tmp/metasalmon-lib``, or a source tree named
by ``METASALMON_SRC``. It skips elsewhere, including where the installed
metasalmon predates B-326.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import warnings
from pathlib import Path

import pandas as pd
import pytest

from metasalmonpy import ingest_semantic_assessments, write_semantic_review_packet
from metasalmonpy.resource_types import format_number_token

DATA = Path(__file__).resolve().parent / "data" / "semantic_review"
FIXTURES = DATA / "v1"
SCRIPT = DATA / "r-cross-language.R"
R_LIB_PATH = "/tmp/metasalmon-lib"


def _r_environment() -> dict:
    env = dict(os.environ)
    if not env.get("METASALMON_SRC") and os.path.isdir(R_LIB_PATH):
        env["R_LIBS"] = R_LIB_PATH
    return env


def _metasalmon_has_the_contract() -> bool:
    if shutil.which("Rscript") is None:
        return False
    source = os.environ.get("METASALMON_SRC")
    load = (
        f"suppressPackageStartupMessages(pkgload::load_all({json.dumps(source)}, quiet = TRUE))"
        if source
        else "suppressPackageStartupMessages(library(metasalmon))"
    )
    probe = f'{load}; cat(exists("ingest_semantic_assessments", envir = asNamespace("metasalmon")))'
    try:
        completed = subprocess.run(
            ["Rscript", "-e", probe], capture_output=True, text=True, timeout=300, env=_r_environment()
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0 and completed.stdout.strip().endswith("TRUE")


@pytest.fixture(scope="module")
def metasalmon_contract():
    """Probed once, when a test here first runs, rather than at collection."""
    if not _metasalmon_has_the_contract():
        pytest.skip("Rscript with a metasalmon that has write_semantic_review_packet() (hub B-326) is not available")


def _hub_checkout():
    for candidate in (os.environ.get("METASALMON_PATH"), "/tmp/metasalmon"):
        if candidate and (Path(candidate) / "tests" / "testthat" / "fixtures" / "semantic-review" / "v1").is_dir():
            return Path(candidate)
    return None


@pytest.mark.skipif(_hub_checkout() is None, reason="no metasalmon checkout (METASALMON_PATH or the parity job's /tmp/metasalmon)")
def test_the_vendored_contract_is_the_hub_copy_byte_for_byte():
    """The schema, the instructions and every fixture file are metasalmon's.

    Runs in the ``parity`` job, which clones metasalmon to ``/tmp/metasalmon``,
    and wherever ``METASALMON_PATH`` names a checkout, so a change on either
    side that does not reach the other turns it red.
    """
    hub = _hub_checkout()
    checkout = Path(__file__).resolve().parent.parent
    for name in ("semantic-review-packet-v1.schema.json", "semantic-review-instructions-v1.txt"):
        assert (checkout / "data" / "semantic-review" / name).read_bytes() == (
            hub / "inst" / "extdata" / "semantic-review" / name
        ).read_bytes(), name
    theirs = hub / "tests" / "testthat" / "fixtures" / "semantic-review" / "v1"
    ours = sorted(str(p.relative_to(FIXTURES)) for p in FIXTURES.rglob("*") if p.is_file())
    assert ours == sorted(str(p.relative_to(theirs)) for p in theirs.rglob("*") if p.is_file())
    for name in ours:
        assert (FIXTURES / name).read_bytes() == (theirs / name).read_bytes(), name
    theme_a = hub / "tests" / "testthat" / "fixtures" / "theme-a" / "cases-v1.json"
    assert (DATA / "theme-a" / "cases-v1.json").read_bytes() == theme_a.read_bytes()


def _r(*args: str) -> str:
    completed = subprocess.run(
        ["Rscript", str(SCRIPT), *args], capture_output=True, text=True, timeout=600, env=_r_environment()
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def _read_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _cases() -> list:
    return sorted(path.name for path in FIXTURES.iterdir() if (path / "harness-1.csv").is_file()) if FIXTURES.is_dir() else []


def _rows_to_frame(rows, numeric=(), integer=(), logical=()) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    columns = list(dict.fromkeys(key for row in rows for key in row))
    data = {}
    for column in columns:
        values = [row.get(column) for row in rows]
        if column in numeric:
            data[column] = pd.Series([math.nan if v is None else float(v) for v in values], dtype="float64")
        elif column in integer:
            data[column] = pd.Series([pd.NA if v is None else int(v) for v in values], dtype="Int64")
        elif column in logical:
            data[column] = pd.Series([None if v is None else bool(v) for v in values], dtype="object")
        else:
            data[column] = pd.Series([None if v is None else str(v) for v in values], dtype="object")
    return pd.DataFrame(data)


def _case_dictionary(case_dir: Path) -> pd.DataFrame:
    case_input = _read_json(case_dir / "input.json")
    dictionary = _rows_to_frame(case_input["dictionary"])
    dictionary.attrs["semantic_targets"] = _rows_to_frame(case_input["targets"])
    dictionary.attrs["semantic_suggestions"] = _rows_to_frame(
        case_input["candidates"], numeric=("score", "role_hint_bonus"), integer=("retrieval_pass",),
        logical=("alignment_only", "role_collision"),
    )
    return dictionary


def _fake_search(calls: list):
    responses = _read_json(FIXTURES / "search-responses.json")

    def search(query, role=None, sources=None):
        calls.append(query)
        rows = responses.get(f"{query}|{role}")
        return pd.DataFrame() if rows is None else _rows_to_frame(rows, numeric=("score",))

    return search


def _csv_text(path: Path):
    import csv
    import io

    text = Path(path).read_text(encoding="utf-8")
    if text == "":
        return [], []
    parsed = list(csv.reader(io.StringIO(text)))
    return parsed[0], [[None if cell == "" else cell for cell in row] for row in parsed[1:]]


def _cell_text(value):
    item = getattr(value, "item", None)
    if item is not None and type(value).__module__ == "numpy":
        value = item()
    if value is None or value is pd.NA or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return format_number_token(value)
    return str(value) or None


def _python_frame(frame: pd.DataFrame) -> dict:
    return {
        "columns": [str(column) for column in frame.columns],
        "rows": [[_cell_text(value) for value in row] for row in frame.itertuples(index=False, name=None)],
    }


def _assert_matches_expected(label: str, outcome: dict, case_dir: Path, pass_number: int) -> None:
    """``outcome``: status, summary, search calls and the three frames as text."""
    expected_dir = case_dir / "expected"
    status = _read_json(expected_dir / f"status-{pass_number}.json")
    for key in ("status", "pass", "packet_id", "has_next_packet", "summary", "search_calls"):
        assert outcome[key] == status[key], f"{label}: {key}"
    for name in ("record", "findings", "suggestions"):
        columns, rows = _csv_text(expected_dir / f"{name}-{pass_number}.csv")
        frame = outcome[name]
        assert list(frame["columns"]) == columns, f"{label}: {name} columns"
        assert len(frame["rows"]) == len(rows), f"{label}: {name} rows"
        for got_row, want_row in zip(frame["rows"], rows):
            for column, got, want in zip(columns, got_row, want_row):
                if column == "llm_error":
                    assert (got is None) == (want is None), f"{label}: {name} {column}"
                else:
                    assert got == want, f"{label}: {name} {column}: {got!r} vs {want!r}"


def _python_ingest(case_dir: Path, review_dir: Path, harness: Path) -> dict:
    calls: list = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = ingest_semantic_assessments(
            _case_dictionary(case_dir), assessments=str(harness), review_dir=review_dir,
            search_fn=_fake_search(calls), quiet=True,
        )
    return {
        "status": result["status"],
        "pass": result["pass"],
        "packet_id": result["packet_id"],
        "has_next_packet": result["next_packet"] is not None,
        "summary": result["summary"],
        "search_calls": len(calls),
        "record": _python_frame(result["assessments"]),
        "findings": _python_frame(result["findings"]),
        "suggestions": _python_frame(result["suggestions"]),
    }


def _r_ingest(case_dir: Path, review_dir: Path, harness: Path, out: Path) -> dict:
    _r("ingest", str(case_dir), str(review_dir), str(harness), str(out))
    outcome = _read_json(out)
    for name in ("record", "findings", "suggestions"):
        outcome[name]["rows"] = [[cell for cell in row] for row in outcome[name]["rows"]]
    return outcome


def test_a_packet_built_in_r_is_ingested_here_and_its_continuation_in_r(metasalmon_contract, tmp_path):
    cases = _cases()
    assert cases
    for case_id in cases:
        case_dir = FIXTURES / case_id
        review_dir = tmp_path / case_id / "review"
        packet_id = _r("build", str(case_dir), str(review_dir)).strip()
        packet = _read_json(review_dir / "semantic-review-packet.json")
        assert packet["producer"]["implementation"] == "metasalmon", case_id
        assert packet_id == _read_json(case_dir / "packet-1.json")["packet_id"], case_id
        first = _python_ingest(case_dir, review_dir, case_dir / "harness-1.csv")
        _assert_matches_expected(f"{case_id} R-built, Python pass 1", first, case_dir, 1)
        if first["has_next_packet"]:
            continuation = _read_json(review_dir / "semantic-review-packet-pass-2.json")
            assert continuation["producer"]["implementation"] == "metasalmonpy", case_id
            second = _r_ingest(case_dir, review_dir, case_dir / "harness-2.csv", tmp_path / case_id / "r-pass-2.json")
            _assert_matches_expected(f"{case_id} Python continuation, R pass 2", second, case_dir, 2)


def test_a_packet_built_here_is_ingested_in_r_and_its_continuation_here(metasalmon_contract, tmp_path):
    cases = _cases()
    assert cases
    for case_id in cases:
        case_dir = FIXTURES / case_id
        review_dir = tmp_path / case_id / "review"
        case_input = _read_json(case_dir / "input.json")
        write_semantic_review_packet(
            _case_dictionary(case_dir), context_text=case_input["context_text"], review_dir=review_dir, quiet=True
        )
        first = _r_ingest(case_dir, review_dir, case_dir / "harness-1.csv", tmp_path / case_id / "r-pass-1.json")
        _assert_matches_expected(f"{case_id} Python-built, R pass 1", first, case_dir, 1)
        if first["has_next_packet"]:
            continuation = _read_json(review_dir / "semantic-review-packet-pass-2.json")
            assert continuation["producer"]["implementation"] == "metasalmon", case_id
            second = _python_ingest(case_dir, review_dir, case_dir / "harness-2.csv")
            _assert_matches_expected(f"{case_id} R continuation, Python pass 2", second, case_dir, 2)
