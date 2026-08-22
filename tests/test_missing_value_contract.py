"""The canonical missing-value contract: one token, defined once.

metasalmon 0.2.4 pinned the contract after readr's own defaults were caught
disagreeing with each other: it *writes* a missing value as the two characters
``NA`` and *reads* ``c("", "NA")`` as missing, so a value that is literally
the string ``"NA"`` — a real fisheries gear code — was written
indistinguishably from a missing value and destroyed at write time, where no
reader could recover it. Both sides now use the empty field, defined once in
``csv_na_token()`` (R: ``.ms_csv_na_token()``) because the contract is only
sound if the two sides agree and they live in different files.

The byte expectations in this file were produced by driving metasalmon
``main`` @ ``39818ce836f8a5cab30e31265e3801e5e5115458`` (2026-08-22) over the
same frame through ``write_salmon_datapackage()`` and diffing the written
package against this package's output: ``data/obs.csv`` and
``metadata/column_dictionary.csv`` came out byte-identical. A byte-level
assertion is the only one that would have failed under the era behaviour,
where the loss happened at write time and every parsed-result assertion still
passed.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None

if pd is None:
    raise unittest.SkipTest("pandas not installed")

from metasalmonpy.metadata import csv_na_token, read_sdp_csv
from metasalmonpy.package_io import read_salmon_datapackage, write_salmon_datapackage

REPO_ROOT = Path(__file__).resolve().parent.parent

# ``to_csv`` call sites permitted NOT to pass ``na_rep=csv_na_token()``, each
# with the condition that retires the entry. Add one only for text that never
# becomes a canonical SDP artifact on disk.
# Format: "<module>:<function>" -> reason.
ALLOWED_TO_CSV: dict[str, str] = {
    "llm_review.py:_read_context_file": (
        "renders a spreadsheet as inline LLM prompt text; never written to a "
        "package. Retires if this rendering is ever persisted as an artifact."
    ),
}


# --- the 0.2.4 regression gate, on the bytes ---------------------------------

_ADVERSARIAL = {
    # Every token pandas' default NA vocabulary would destroy, plus a genuine
    # missing value and a genuine empty string, which share the empty field.
    "gear_code": ["null", "N/A", "nan", "NA", "None", "", None],
    # Embedded comma, quote, newline, non-ASCII, and padding for trim_ws.
    "note": ["has, comma", 'has "quote"', "line\nbreak", "näägel", "ok", " padded ", "x"],
}

# The bytes metasalmon main @ 39818ce writes for the same frame (measured, not
# reasoned): a literal "NA" is data, a missing value is the empty field.
_EXPECTED_RESOURCE_BYTES = (
    "gear_code,note\n"
    'null,"has, comma"\n'
    'N/A,"has ""quote"""\n'
    'nan,"line\nbreak"\n'
    "NA,näägel\n"
    "None,ok\n"
    ", padded \n"
    ",x\n"
)


def _write_package(tmp_path: Path) -> Path:
    import warnings

    dataset_meta = pd.DataFrame([{"dataset_id": "demo", "title": "Demo"}])
    table_meta = pd.DataFrame(
        [
            {
                "dataset_id": "demo",
                "table_id": "obs",
                "file_name": "obs.csv",
                "table_label": "Observations",
            }
        ]
    )
    dictionary = pd.DataFrame(
        [
            {
                "dataset_id": "demo",
                "table_id": "obs",
                "column_name": name,
                "column_label": name,
                "column_description": f"The {name}.",
                "column_role": "attribute",
                "value_type": "string",
                "required": False,
            }
            for name in ("gear_code", "note")
        ]
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return Path(
            write_salmon_datapackage(
                {"obs": pd.DataFrame(_ADVERSARIAL)},
                dataset_meta,
                table_meta,
                dictionary,
                path=str(tmp_path / "sdp"),
                overwrite=True,
            )
        )


def test_literal_na_and_adversarial_tokens_survive_the_round_trip(tmp_path):
    target = _write_package(tmp_path)

    # The bytes themselves must distinguish a literal "NA" from absence --
    # asserting only on the parsed result would pass even if the reader were
    # guessing. This is the exact content R main @ 39818ce writes.
    written = (target / "data" / "obs.csv").read_text(encoding="utf-8", newline="")
    assert written == _EXPECTED_RESOURCE_BYTES

    back = read_salmon_datapackage(str(target))["resources"]["obs"]
    # The literal tokens are values, not missingness.
    assert back["gear_code"].tolist()[:5] == ["null", "N/A", "nan", "NA", "None"]
    # A genuinely missing value and a genuine empty string both come back as
    # the empty string here (R: NA_character_) -- the representation half of
    # PARITY.md row 21, unchanged by this contract.
    assert back["gear_code"].tolist()[5:] == ["", ""]
    # Quoting, embedded newlines, non-ASCII survive; padding trims on read.
    assert back["note"].tolist()[:5] == _ADVERSARIAL["note"][:5]
    assert back["note"].iloc[5] == "padded"


def test_the_round_trip_reproduces_its_own_bytes(tmp_path):
    # Write -> read -> write must be a fixed point: if the reader guessed at
    # missingness the second write would bake the guess into bytes.
    first = _write_package(tmp_path / "first")
    back = read_salmon_datapackage(str(first))

    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        second = Path(
            write_salmon_datapackage(
                {"obs": back["resources"]["obs"]},
                back["dataset"],
                back["tables"],
                back["dictionary"],
                path=str(tmp_path / "second"),
                overwrite=True,
            )
        )
    first_bytes = (first / "data" / "obs.csv").read_bytes()
    second_bytes = (second / "data" / "obs.csv").read_bytes()
    # The one read/write asymmetry is readr's trim_ws, shared with R: the
    # padded cell comes back trimmed, so normalize it before comparing.
    assert second_bytes == first_bytes.replace(b", padded \n", b",padded\n")


# --- the single token authority ----------------------------------------------


def test_the_token_is_the_empty_field():
    # Pinned rather than assumed: every writer and the EML audit route through
    # this value, and R's .ms_csv_na_token() returns the same "".
    assert csv_na_token() == ""


def test_the_reader_keeps_the_token_as_the_empty_string(tmp_path):
    # read_sdp_csv deliberately maps NOTHING to pandas NaN: the token survives
    # as the empty string (PARITY.md row 21's representation note), and a
    # whitespace-only cell trims to it before the token is matched.
    path = tmp_path / "cells.csv"
    path.write_text('a,b,c\nNA,   ,\n', encoding="utf-8", newline="")
    frame = read_sdp_csv(path)
    assert frame.iloc[0].tolist() == ["NA", "", ""]
    assert not frame.isna().any().any()


def _to_csv_call_sites(path: Path) -> list[tuple[str, ast.Call]]:
    """``(module:function, call)`` for every ``.to_csv(...)`` call."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    enclosing: dict[ast.AST, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                enclosing.setdefault(child, node.name)
    found = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "to_csv"
        ):
            found.append((f"{path.name}:{enclosing.get(node, '<module>')}", node))
    return found


def _package_sources() -> list[Path]:
    sources = sorted(REPO_ROOT.glob("*.py")) + sorted(REPO_ROOT.glob("scripts/*.py"))
    return [path for path in sources if "tests" not in path.parts]


def _routes_through_token(call: ast.Call) -> bool:
    for keyword in call.keywords:
        if keyword.arg == "na_rep":
            value = keyword.value
            return (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "csv_na_token"
            )
    return False


def test_every_csv_writer_routes_through_the_token_authority():
    """Every ``to_csv`` call passes ``na_rep=csv_na_token()``, or says why not.

    pandas' default ``na_rep`` happens to be the same empty string, which is
    exactly why this guard exists: an implicit agreement holds only
    incidentally, and a writer added with ``na_rep="NA"`` — or relying on the
    default while the default is what someone changes — would reintroduce the
    write-time loss 0.2.4 fixed without failing any parsed-result test.
    Retires if the canonical writers stop going through pandas ``to_csv``.
    """
    offenders = []
    for path in _package_sources():
        for site, call in _to_csv_call_sites(path):
            if site in ALLOWED_TO_CSV:
                continue
            if not _routes_through_token(call):
                offenders.append(site)
    assert not offenders, (
        "to_csv call sites not routing na_rep through csv_na_token(): "
        f"{sorted(set(offenders))}. Pass na_rep=csv_na_token(), or add the "
        "site to ALLOWED_TO_CSV with the condition that retires the entry."
    )


def test_every_to_csv_allowlist_entry_still_has_a_call_site():
    """An allowance that outlived its cause hides the next failure."""
    live = {
        site for path in _package_sources() for site, _ in _to_csv_call_sites(path)
    }
    stale = sorted(set(ALLOWED_TO_CSV) - live)
    assert not stale, f"ALLOWED_TO_CSV entries with no matching call site: {stale}"


def test_no_reader_reintroduces_an_na_token_vocabulary():
    """``na_values``/``keep_default_na`` belong to ``read_sdp_csv`` alone.

    The era defect was a reader-side vocabulary (``na = c("", "NA")`` in R,
    pandas' 19-token default here) disagreeing with the writer's token. Every
    SDP read goes through ``read_sdp_csv``, so any other module spelling
    ``na_values=`` or ``keep_default_na=`` is a second reader contract in the
    making. Retires if pandas ever changes ``read_csv`` to default to no NA
    vocabulary, at which point the shared reader's kwargs become redundant
    rather than load-bearing.
    """
    offenders = []
    for path in _package_sources():
        if path.name == "metadata.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg in ("na_values", "keep_default_na"):
                        offenders.append(f"{path.name}:{keyword.arg}")
    assert not offenders, (
        "reader NA vocabulary spelled outside metadata.read_sdp_csv: "
        f"{sorted(set(offenders))}"
    )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
