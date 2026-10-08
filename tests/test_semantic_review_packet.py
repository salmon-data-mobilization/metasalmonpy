"""The semantic review packet contract (hub item B-327, the Python half of B-326).

Brett ruled on 2026-09-25 (hub Q67) that the model call leaves both packages:
each exports a deterministic review-packet exporter and an assessment ingester,
and model judgement runs in the user's own harness. This file holds this
package to that contract the way metasalmon's
``tests/testthat/test-semantic-review-packet.R`` holds metasalmon to it:

* **the canonical emitter** renders by one rule per type;
* **the shared fixtures** -- ``tests/data/semantic_review/v1/``, metasalmon's
  ``tests/testthat/fixtures/semantic-review/v1/`` vendored byte for byte and
  checked against its ``manifest.json`` -- build byte-identical packets here,
  producer aside, and ingest to the recorded record, findings, suggestions and
  status, compared after reading as the fixture README defines;
* **the sentinel**: no model provider is ever reached, and no socket is opened
  except through the ``search_fn`` a caller gives;
* **the package path**: the queue ``review_semantics()`` shows, the blank slots
  discovery recovers, containment, the continuation pass, and the record
  :func:`~metasalmonpy.semantic_llm_assessments` reads back.

Never edit a vendored fixture to make a test here pass: a case that cannot be
matched is a difference between the two packages, and it is found and named.
"""

from __future__ import annotations

import ast
import csv
import hashlib
import io
import json
import math
import os
import re
import shutil
import socket
import warnings
from pathlib import Path

import pandas as pd
import pytest

from metasalmonpy import (
    SemanticReviewError,
    apply_semantic_suggestions,
    create_sdp,
    detect_semantic_term_gaps,
    ingest_semantic_assessments,
    read_salmon_datapackage,
    review_semantics,
    semantic_llm_assessments,
    semantic_suggestions,
    write_salmon_datapackage,
    write_semantic_review_packet,
)
from metasalmonpy.llm_review import LLM_ASSESSMENT_COLUMNS
from metasalmonpy.resource_types import format_number_token
from metasalmonpy.semantic_review_ingest import FINDINGS_COLUMNS, _slots
from metasalmonpy.semantic_review_json import (
    read_semantic_review_json,
    semantic_review_canonical_bytes,
    semantic_review_packet_id,
)
from metasalmonpy.semantic_review_packet import (
    IDENTITY_COLUMNS,
    PACKET_VERSION,
    semantic_review_instructions,
    semantic_review_schema_path,
)

CHECKOUT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "data" / "semantic_review" / "v1"


# -----------------------------------------------------------------------------
# Helpers: the mirror of tests/testthat/helper-semantic-review.R
# -----------------------------------------------------------------------------


def _read_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _rows_to_frame(rows, numeric=(), integer=(), logical=()) -> pd.DataFrame:
    """A JSON array of row objects to a frame, every cell text (``None`` for
    null) except the columns named ``numeric``, ``integer`` and ``logical``."""
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


def _case_dictionary(case_input: dict) -> pd.DataFrame:
    """The dictionary a case's ``input.json`` describes, with ``semantic_targets``
    and ``semantic_suggestions`` attached as ``suggest_semantics()`` attaches them."""
    dictionary = _rows_to_frame(case_input["dictionary"])
    dictionary.attrs["semantic_targets"] = _rows_to_frame(case_input["targets"])
    dictionary.attrs["semantic_suggestions"] = _rows_to_frame(
        case_input["candidates"],
        numeric=("score", "role_hint_bonus"),
        integer=("retrieval_pass",),
        logical=("alignment_only", "role_collision"),
    )
    return dictionary


def _search_responses() -> dict:
    return _read_json(FIXTURES / "search-responses.json")


def _fake_search(responses: dict, calls: list = None):
    """The fixtures' fake search function: answers keyed by ``<query>|<role>``."""

    def search(query, role=None, sources=None):
        if calls is not None:
            calls.append({"query": query, "role": role, "sources": sources})
        rows = responses.get(f"{query}|{role}")
        if rows is None:
            return pd.DataFrame()
        return _rows_to_frame(rows, numeric=("score",))

    return search


def _no_search(*args, **kwargs):  # pragma: no cover - must never run
    raise AssertionError("search_fn must not be called")


def _read_csv_text(path: Path):
    """A CSV read the way the fixtures are compared: every cell text, the empty
    field ``None``. An empty file is a frame with no columns."""
    text = Path(path).read_text(encoding="utf-8")
    if text == "":
        return [], []
    parsed = list(csv.reader(io.StringIO(text)))
    return parsed[0], [[None if cell == "" else cell for cell in row] for row in parsed[1:]]


def _text(value):
    """One cell as the fixture README compares it: logicals ``TRUE``/``FALSE``,
    numbers through the number-token formatter, the empty field and null one value."""
    item = getattr(value, "item", None)
    if item is not None and type(value).__module__ == "numpy":
        value = item()
    if value is None or value is pd.NA:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return format_number_token(value)
    text = str(value)
    return text or None


def _frame_text(frame: pd.DataFrame):
    return (
        [str(column) for column in frame.columns],
        [[_text(value) for value in row] for row in frame.itertuples(index=False, name=None)],
    )


def _assert_frame_matches(frame: pd.DataFrame, path: Path, label: str) -> None:
    """The same columns in the same order and every cell equal as text.

    ``llm_error`` is compared for presence, not words: an error row's text is
    worded by each implementation (the fixture README; PARITY.md row 65).
    """
    expected_columns, expected_rows = _read_csv_text(path)
    columns, rows = _frame_text(frame)
    assert columns == expected_columns, label
    assert len(rows) == len(expected_rows), label
    for number, (row, expected) in enumerate(zip(rows, expected_rows)):
        for column, got, want in zip(expected_columns, row, expected):
            if column == "llm_error":
                assert (got is None) == (want is None), f"{label} row {number} {column}: {got!r} vs {want!r}"
            else:
                assert got == want, f"{label} row {number} {column}: {got!r} vs {want!r}"


def _strip_producer(packet: dict) -> dict:
    return {key: value for key, value in packet.items() if key != "producer"}


def _conformance_cases() -> list:
    return sorted(path.name for path in FIXTURES.iterdir() if (path / "harness-1.csv").is_file())


def _build_case(case_id: str, tmp_path: Path) -> dict:
    case_dir = FIXTURES / case_id
    case_input = _read_json(case_dir / "input.json")
    dictionary = _case_dictionary(case_input)
    review_dir = tmp_path / case_id / "review"
    built = write_semantic_review_packet(
        dictionary, context_text=case_input["context_text"], review_dir=review_dir, quiet=True
    )
    return {"case_dir": case_dir, "input": case_input, "dict": dictionary, "review_dir": review_dir, "built": built}


def _ingest_quietly(*args, **kwargs):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return ingest_semantic_assessments(*args, **kwargs)


def _harness_row(target, **values) -> dict:
    """An empty harness row for one target: the identity copied, every other cell empty."""
    row = {column: None for column in LLM_ASSESSMENT_COLUMNS}
    for column in IDENTITY_COLUMNS:
        row[column] = target.get(column)
    row["llm_provider"] = "fixture-harness"
    row["llm_model"] = "semantic-review-v1"
    for name, value in values.items():
        row[name] = None if value is None else str(value)
    return row


def _harness_frame(rows) -> pd.DataFrame:
    return pd.DataFrame(list(rows), columns=list(LLM_ASSESSMENT_COLUMNS))


def _write_harness(rows: pd.DataFrame, path: Path, packet_id=None) -> None:
    """A harness file written with a CSV library, and its ``.packet-id`` sidecar."""
    rows.to_csv(path, index=False, na_rep="")
    sidecar = Path(f"{path}.packet-id")
    if packet_id is not None:
        sidecar.write_text(packet_id + "\n", encoding="utf-8")
    elif sidecar.exists():
        sidecar.unlink()


# -----------------------------------------------------------------------------
# The canonical JSON emitter
# -----------------------------------------------------------------------------


def test_the_emitter_renders_every_type_by_one_rule_and_round_trips(tmp_path):
    value = {
        "whole": 2.0,
        "frac": 0.1 + 0.2,
        "neg": -3.5,
        "big": 9007199254740991,
        "zero": -0.0,
        "nan": float("nan"),
        "na": pd.NA,
        "flag": True,
        "off": False,
        "none": None,
        "text": "quote \" backslash \\ newline \n tab \t",
        "empty_array": [],
        "empty_object": {},
        "one": ["only"],
        "nested": {"k": [{"i": 1}, {"i": 2}]},
    }
    data = semantic_review_canonical_bytes(value)
    text = data.decode("utf-8")
    assert text.endswith("}\n")
    for fragment in (
        '"whole": 2,\n',
        '"frac": 0.30000000000000004,\n',
        '"big": 9007199254740991,\n',
        '"zero": 0,\n',
        '"nan": null,\n',
        '"na": null,\n',
        '"none": null,\n',
        '"empty_array": [],\n',
        '"empty_object": {},\n',
        '"one": [\n    "only"\n  ],\n',
        '"text": "quote \\" backslash \\\\ newline \\n tab \\t",\n',
    ):
        assert fragment in text, fragment
    path = tmp_path / "value.json"
    path.write_bytes(data)
    assert semantic_review_canonical_bytes(read_semantic_review_json(path)) == data


def test_numbers_render_by_value_not_by_type():
    # A whole number is an integer literal whatever its type; anything else is
    # the shortest decimal that round-trips, never scientific notation.
    rendered = semantic_review_canonical_bytes([1e20, 2 ** 53, 5e-7, -2.0, 7]).decode()
    assert rendered == "[\n  100000000000000000000,\n  9007199254740992,\n  0.0000005,\n  -2,\n  7\n]\n"


def test_the_emitter_refuses_a_value_it_cannot_render():
    with pytest.raises(TypeError, match="Cannot render"):
        semantic_review_canonical_bytes({"a": {1, 2}})
    with pytest.raises(TypeError, match="non-empty string name"):
        semantic_review_canonical_bytes({1: "a"})


def test_control_characters_are_escaped_the_way_the_json_module_escapes_them():
    text = semantic_review_canonical_bytes({"bell": "\x07", "del": "\x7f", "accent": "é"}).decode()
    assert '"bell": "\\u0007"' in text
    assert '"del": "\x7f"' in text
    assert '"accent": "é"' in text


def test_packet_id_excludes_packet_id_and_producer_and_nothing_else():
    packet = {"packet_version": "x", "packet_id": "old", "pass": 1, "producer": {"version": "1"}, "units": []}
    first = semantic_review_packet_id(packet)
    packet["producer"]["version"] = "2"
    packet["packet_id"] = "other"
    assert semantic_review_packet_id(packet) == first
    packet["pass"] = 2
    assert semantic_review_packet_id(packet) != first


# -----------------------------------------------------------------------------
# The shared fixtures and the vendored contract
# -----------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_every_fixture_file_matches_the_manifest_and_the_manifest_lists_every_file():
    manifest = _read_json(FIXTURES / "manifest.json")
    assert manifest["packet_version"] == PACKET_VERSION
    files = sorted(
        str(path.relative_to(FIXTURES)).replace(os.sep, "/")
        for path in FIXTURES.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    )
    assert list(manifest["files"]) == files
    for name in files:
        assert _sha256(FIXTURES / name) == manifest["files"][name], name


def test_the_vendored_instructions_and_schema_are_what_the_packets_embed():
    instructions = semantic_review_instructions()
    packet = _read_json(FIXTURES / "bundle_accept" / "packet-1.json")
    assert packet["instructions"] == instructions
    assert instructions.isascii()
    schema = _read_json(semantic_review_schema_path())
    definitions = schema["$defs"]
    assert definitions["packet"]["properties"]["packet_version"]["const"] == PACKET_VERSION
    assert definitions["assessment_row"]["required"] == list(LLM_ASSESSMENT_COLUMNS)
    assert definitions["findings_row"]["required"] == list(FINDINGS_COLUMNS)


def test_the_contract_files_are_declared_as_package_data():
    # The globs in pyproject are the only reason the schema and the
    # instructions reach a wheel; a missing glob fails at run time on an
    # installed package, not in a checkout.
    text = (CHECKOUT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"semantic-review/*.json"' in text
    assert '"semantic-review/*.txt"' in text


def _schema_errors(instance, schema, root, path="$") -> list:
    """The JSON Schema keywords the vendored packet schema uses, and no others.

    ``$ref``, ``type``, ``const``, ``enum``, ``pattern``, ``minimum``,
    ``properties``, ``required``, ``additionalProperties``, ``items``,
    ``minItems`` and ``maxItems``, with the draft 2020-12 rule that a keyword
    applies only to the instance type it constrains. No schema library is a
    dependency of this package, and the schema is small enough that its
    keyword set is pinned here instead (PARITY.md row 65).
    """
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        return _schema_errors(instance, root["$defs"][name], root, path)
    errors = []
    types = schema.get("type")
    if types is not None:
        types = [types] if isinstance(types, str) else types
        checks = {
            "object": lambda v: isinstance(v, dict),
            "array": lambda v: isinstance(v, list),
            "string": lambda v: isinstance(v, str),
            "boolean": lambda v: isinstance(v, bool),
            "null": lambda v: v is None,
            "integer": lambda v: isinstance(v, int) and not isinstance(v, bool)
            or isinstance(v, float) and v.is_integer(),
            "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        }
        if not any(checks[kind](instance) for kind in types):
            return [f"{path}: {instance!r} is not {types}"]
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: {instance!r} is not {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} is not one of {schema['enum']}")
    if isinstance(instance, str) and "pattern" in schema and not re.search(schema["pattern"], instance):
        errors.append(f"{path}: {instance!r} does not match {schema['pattern']}")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool) and "minimum" in schema:
        if instance < schema["minimum"]:
            errors.append(f"{path}: {instance!r} is below {schema['minimum']}")
    if isinstance(instance, dict):
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in instance:
                errors.append(f"{path}: missing {name}")
        for name, value in instance.items():
            if name in properties:
                errors.extend(_schema_errors(value, properties[name], root, f"{path}.{name}"))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unexpected member {name}")
            elif isinstance(schema.get("additionalProperties"), dict):
                errors.extend(_schema_errors(value, schema["additionalProperties"], root, f"{path}.{name}"))
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(f"{path}: more than {schema['maxItems']} items")
        if "items" in schema:
            for index, item in enumerate(instance):
                errors.extend(_schema_errors(item, schema["items"], root, f"{path}[{index}]"))
    return errors


def test_the_keyword_check_is_not_vacuous():
    schema = _read_json(semantic_review_schema_path())
    packet = _read_json(FIXTURES / "retry_gain" / "packet-2.json")
    assert _schema_errors(packet, schema, schema) == []
    packet["units"][0]["slots"][0]["candidates"][0]["surprise"] = 1
    packet["pass"] = 3
    errors = _schema_errors(packet, schema, schema)
    assert any("surprise" in error for error in errors)
    assert any("$.pass" in error for error in errors)


@pytest.mark.parametrize("case_id", _conformance_cases())
def test_every_golden_and_python_built_packet_satisfies_the_vendored_schema(case_id, tmp_path):
    schema = _read_json(semantic_review_schema_path())
    for name in ("packet-1.json", "packet-2.json"):
        path = FIXTURES / case_id / name
        if path.is_file():
            assert _schema_errors(_read_json(path), schema, schema) == [], name
    built = _build_case(case_id, tmp_path)["built"]
    assert _schema_errors(_read_json(built["path"]), schema, schema) == []


# -----------------------------------------------------------------------------
# Conformance: the same bytes, the same record
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("case_id", _conformance_cases())
def test_every_conformance_case_builds_the_golden_packet_byte_for_byte(case_id, tmp_path):
    case = _build_case(case_id, tmp_path)
    golden = _read_json(case["case_dir"] / "packet-1.json")
    built = _read_json(case["built"]["path"])
    assert case["built"]["packet_id"] == golden["packet_id"]
    assert semantic_review_canonical_bytes(_strip_producer(built)) == semantic_review_canonical_bytes(
        _strip_producer(golden)
    )
    assert built["producer"] == {
        "implementation": "metasalmonpy",
        "version": __import__("metasalmonpy").__version__,
        "function": "write_semantic_review_packet",
    }


def _assert_case_pass(case: dict, result: dict, pass_number: int, calls: list) -> None:
    expected_dir = case["case_dir"] / "expected"
    status = _read_json(expected_dir / f"status-{pass_number}.json")
    label = f"{case['case_dir'].name} pass {pass_number}"
    assert result["status"] == status["status"], label
    assert result["pass"] == status["pass"], label
    assert result["packet_id"] == status["packet_id"], label
    assert (result["next_packet"] is not None) == status["has_next_packet"], label
    assert result["summary"] == status["summary"], label
    assert len(calls) == status["search_calls"], label
    _assert_frame_matches(result["assessments"], expected_dir / f"record-{pass_number}.csv", f"{label} record")
    _assert_frame_matches(result["findings"], expected_dir / f"findings-{pass_number}.csv", f"{label} findings")
    _assert_frame_matches(result["suggestions"], expected_dir / f"suggestions-{pass_number}.csv", f"{label} suggestions")
    # The persisted record is the returned one.
    assert _read_csv_text(case["review_dir"] / "semantic-llm-assessments.csv") == _frame_text(result["assessments"])


@pytest.mark.parametrize("case_id", _conformance_cases())
def test_every_conformance_case_ingests_to_the_golden_record_findings_suggestions_and_status(case_id, tmp_path):
    case = _build_case(case_id, tmp_path)
    responses = _search_responses()
    calls: list = []
    result = _ingest_quietly(
        case["dict"],
        assessments=str(case["case_dir"] / "harness-1.csv"),
        review_dir=case["review_dir"],
        search_fn=_fake_search(responses, calls),
        quiet=True,
    )
    _assert_case_pass(case, result, 1, calls)
    if result["next_packet"] is not None:
        golden = _read_json(case["case_dir"] / "packet-2.json")
        built = _read_json(result["next_packet"])
        assert semantic_review_canonical_bytes(_strip_producer(built)) == semantic_review_canonical_bytes(
            _strip_producer(golden)
        )
        assert built["producer"]["function"] == "ingest_semantic_assessments"
        calls = []
        second = ingest_semantic_assessments(
            case["dict"],
            assessments=str(case["case_dir"] / "harness-2.csv"),
            review_dir=case["review_dir"],
            search_fn=_fake_search(responses, calls),
            quiet=True,
        )
        _assert_case_pass(case, second, 2, calls)
        assert second["next_packet"] is None


def _reject_variants() -> list:
    return _read_json(FIXTURES / "file_errors" / "reject.json")["variants"]


@pytest.mark.parametrize("variant", _reject_variants(), ids=lambda variant: variant["name"])
def test_the_reject_variants_raise_their_stable_codes_and_write_nothing(variant, tmp_path):
    case_dir = FIXTURES / "file_errors"
    reject = _read_json(case_dir / "reject.json")
    base = _build_case(reject["base_case"], tmp_path)
    harness = str(FIXTURES / reject["base_case"] / "harness-1.csv")
    before = sorted(path.name for path in base["review_dir"].iterdir())
    arguments = {"review_dir": base["review_dir"], "search_fn": _no_search, "quiet": True}
    if variant.get("assessments"):
        arguments["assessments"] = str(case_dir / variant["assessments"])
    if variant.get("packet"):
        arguments["packet"] = str(case_dir / variant["packet"])
        arguments.setdefault("assessments", harness)
    if variant.get("expected_packet_id"):
        arguments["packet_id"] = variant["expected_packet_id"]
        arguments.setdefault("assessments", harness)
    if variant["expected_code"] == "no_pass_2":
        pass_2_name = tmp_path / "elsewhere" / "semantic-assessments-pass-2.csv"
        pass_2_name.parent.mkdir()
        shutil.copyfile(harness, pass_2_name)
        arguments["assessments"] = str(pass_2_name)
        arguments["packet_id"] = base["built"]["packet_id"]
    with pytest.raises(SemanticReviewError) as raised:
        ingest_semantic_assessments(base["dict"], **arguments)
    assert raised.value.code == variant["expected_code"]
    assert sorted(path.name for path in base["review_dir"].iterdir()) == before


def test_a_pass_2_packet_that_does_not_descend_from_the_sessions_pass_1_packet_is_refused(tmp_path):
    case = _build_case("retry_gain", tmp_path)
    first = ingest_semantic_assessments(
        case["dict"], assessments=str(case["case_dir"] / "harness-1.csv"), review_dir=case["review_dir"],
        search_fn=_fake_search(_search_responses()), quiet=True,
    )
    assert first["status"] == "awaiting_pass_2"
    orphan = _read_json(first["next_packet"])
    orphan["parent_packet_id"] = "1" * 64
    orphan["packet_id"] = semantic_review_packet_id(orphan)
    orphan_path = tmp_path / "orphan.json"
    orphan_path.write_bytes(semantic_review_canonical_bytes(orphan))
    with pytest.raises(SemanticReviewError) as raised:
        ingest_semantic_assessments(
            case["dict"], assessments=str(case["case_dir"] / "harness-2.csv"), packet=str(orphan_path),
            review_dir=case["review_dir"], quiet=True,
        )
    assert raised.value.code == "provenance"
    # A pass-2 ingest with no continuation packet in the session is refused too.
    fresh = _build_case("bundle_accept", tmp_path / "fresh")
    with pytest.raises(SemanticReviewError) as raised:
        ingest_semantic_assessments(
            fresh["dict"], assessments=str(case["case_dir"] / "harness-2.csv"), packet=first["next_packet"],
            review_dir=fresh["review_dir"], quiet=True,
        )
    assert raised.value.code == "no_pass_2"


def test_harness_text_is_redacted_at_capture_and_no_unredacted_copy_survives(tmp_path):
    case = _build_case("bundle_accept", tmp_path)
    harness = pd.read_csv(case["case_dir"] / "harness-1.csv", dtype=str, keep_default_na=False)
    harness.loc[0, "llm_error"] = "Provider said: api_key=sk-live-9f8e7d6c5b4a was rejected."
    harness.loc[1, "llm_rationale"] = "Judged with Authorization: Bearer top-secret-token-42 in the header."

    # Written at the packet's default location: the file is replaced with its
    # redacted form after the ingest, with a warning.
    default_path = case["review_dir"] / "semantic-assessments-pass-1.csv"
    _write_harness(harness, default_path, packet_id=case["built"]["packet_id"])
    with pytest.warns(UserWarning, match="redacted"):
        result = ingest_semantic_assessments(case["dict"], review_dir=case["review_dir"], search_fn=_no_search, quiet=True)
    assert not result["assessments"]["llm_error"].astype(str).str.contains("sk-live-9f8e7d6c5b4a").any()
    assert not result["assessments"]["llm_rationale"].astype(str).str.contains("top-secret-token-42").any()
    assert result["assessments"]["llm_error"].notna().any()
    for path in case["review_dir"].iterdir():
        text = path.read_bytes()
        assert b"sk-live-9f8e7d6c5b4a" not in text, path.name
        assert b"top-secret-token-42" not in text, path.name

    # Written elsewhere: the record is redacted, the harness's own file is not
    # touched, and nothing is copied into review/.
    elsewhere = _build_case("bundle_accept", tmp_path / "elsewhere")
    outside = tmp_path / "my-answers.csv"
    _write_harness(harness, outside, packet_id=elsewhere["built"]["packet_id"])
    before = outside.read_bytes()
    with pytest.warns(UserWarning, match="redacted"):
        result = ingest_semantic_assessments(
            elsewhere["dict"], assessments=str(outside), review_dir=elsewhere["review_dir"], search_fn=_no_search, quiet=True
        )
    assert outside.read_bytes() == before
    assert not (elsewhere["review_dir"] / "semantic-assessments-pass-1.csv").exists()
    assert not result["assessments"]["llm_error"].astype(str).str.contains("sk-live-9f8e7d6c5b4a").any()


def test_a_stale_assessment_file_is_refused_after_the_packet_is_rebuilt(tmp_path):
    case = _build_case("bundle_accept", tmp_path)
    first_id = case["built"]["packet_id"]
    default_path = case["review_dir"] / "semantic-assessments-pass-1.csv"
    sidecar = Path(f"{default_path}.packet-id")
    shutil.copyfile(case["case_dir"] / "harness-1.csv", default_path)
    sidecar.write_text(first_id + "\n", encoding="utf-8")
    # The packet is rebuilt with more context, so its id changes; the old
    # answers name the old packet and are refused rather than recorded.
    rebuilt = write_semantic_review_packet(
        case["dict"], context_text="Some new context about the catch.", review_dir=case["review_dir"], overwrite=True, quiet=True
    )
    assert rebuilt["packet_id"] != first_id
    shutil.copyfile(case["case_dir"] / "harness-1.csv", default_path)
    sidecar.write_text(first_id + "\n", encoding="utf-8")
    with pytest.raises(SemanticReviewError) as raised:
        ingest_semantic_assessments(case["dict"], review_dir=case["review_dir"], search_fn=_no_search, quiet=True)
    assert raised.value.code == "packet_mismatch"
    assert not (case["review_dir"] / "semantic-llm-assessments.csv").exists()
    # The argument can name the packet instead of the sidecar, and it must agree too.
    with pytest.raises(SemanticReviewError) as raised:
        ingest_semantic_assessments(case["dict"], packet_id=first_id, review_dir=case["review_dir"], search_fn=_no_search, quiet=True)
    assert raised.value.code == "packet_mismatch"


def test_a_harness_value_in_a_package_owned_column_is_overwritten_with_one_warning(tmp_path):
    case = _build_case("row_errors", tmp_path)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = ingest_semantic_assessments(
            case["dict"], assessments=str(case["case_dir"] / "harness-1.csv"), review_dir=case["review_dir"],
            search_fn=_no_search, quiet=True,
        )
    owned = [str(warning.message) for warning in caught if "package-owned" in str(warning.message)]
    assert len(owned) == 1
    assert "llm_selected_label" in owned[0]
    row = result["assessments"][result["assessments"]["column_name"] == "FIELD_13"].iloc[0]
    assert pd.isna(row["llm_selected_label"])
    assert row["llm_context_sources"] != "should-be-overwritten"


def test_an_iri_the_packet_did_not_offer_is_never_applied(tmp_path):
    case = _build_case("row_errors", tmp_path)
    result = _ingest_quietly(
        case["dict"], assessments=str(case["case_dir"] / "harness-1.csv"), review_dir=case["review_dir"],
        search_fn=_no_search, quiet=True,
    )
    record = result["assessments"]
    not_offered = record[record["column_name"] == "FIELD_01"].iloc[0]
    assert pd.isna(not_offered["llm_decision"])
    assert pd.isna(not_offered["llm_selected_iri"])
    assert "not a candidate the packet offered" in not_offered["llm_error"]
    suggestions = result["suggestions"]
    assert "https://example.org/not-offered" not in set(suggestions["iri"])
    assert not suggestions.loc[suggestions["column_name"] == "FIELD_01", "llm_selected"].any()
    # An index that is not a whole number is refused, never truncated.
    fractional = record[record["column_name"] == "FIELD_04"].iloc[0]
    assert pd.isna(fractional["llm_selected_candidate_index"])
    assert "whole number" in fractional["llm_error"]
    # A downgrade with no rationale never starts with "NA ".
    assert not any(str(text).startswith("NA ") for text in record["llm_rationale"].dropna())


def test_an_identifier_like_retry_query_is_not_issued_and_says_why(tmp_path):
    # retry_dead_ends' README row promises an identifier-like query, but its
    # "smn:MeshSize" is not one to R: TRE reads `[^\s]` as "neither a backslash
    # nor s", and "MeshSize" has an s, so the fixture retries it as a lexical
    # query and records no reason (as this package does, llm_review.py's
    # _IDENTIFIER_CURIE). This pins decision 12 with a query both read as an
    # identifier.
    case = _build_case("retry_dead_ends", tmp_path)
    harness = pd.read_csv(case["case_dir"] / "harness-1.csv", dtype=str, keep_default_na=False)
    harness = harness.replace("", None)
    harness.loc[harness["column_name"] == "MESH_SIZE", "llm_retry_query"] = "https://w3id.org/smn/MeshSize"
    calls: list = []
    result = ingest_semantic_assessments(
        case["dict"], assessments=harness, packet_id=case["built"]["packet_id"], review_dir=case["review_dir"],
        search_fn=_fake_search(_search_responses(), calls), quiet=True,
    )
    row = result["assessments"][result["assessments"]["column_name"] == "MESH_SIZE"].iloc[0]
    assert row["llm_decision"] == "retry_search"
    assert row["llm_retry_query_rejection_reason"] == "identifier_like_query"
    assert row["llm_rationale"].endswith("Retry query looks like an identifier rather than a lexical query; the retry was not issued.")
    assert not bool(row["llm_exploration_used"])
    assert [call["query"] for call in calls] == ["fishing vessel"]
    assert result["summary"]["retries"] == 1


def test_error_downgraded_escalated_and_success_rows_carry_identical_names_and_types(tmp_path):
    case = _build_case("row_errors", tmp_path)
    result = _ingest_quietly(
        case["dict"], assessments=str(case["case_dir"] / "harness-1.csv"), review_dir=case["review_dir"],
        search_fn=_no_search, quiet=True,
    )
    escalated = _build_case("reject_escalates", tmp_path)
    second = ingest_semantic_assessments(
        escalated["dict"], assessments=str(escalated["case_dir"] / "harness-1.csv"), review_dir=escalated["review_dir"],
        search_fn=_no_search, quiet=True,
    )
    for frame in (result["assessments"], second["assessments"]):
        assert list(frame.columns) == list(LLM_ASSESSMENT_COLUMNS)
    assert dict(result["assessments"].dtypes) == dict(second["assessments"].dtypes)
    # Copies without attrs: each record carries its findings frame there, and
    # pd.concat() compares its inputs' attrs (hub B-370).
    frames = []
    for frame in (result["assessments"], second["assessments"]):
        frame = frame.copy()
        frame.attrs = {}
        frames.append(frame)
    rows = pd.concat(frames, ignore_index=True)
    assert rows["llm_error"].notna().any()
    assert ((rows["llm_decision"] == "request_new_term") & rows["llm_escalated_from"].notna()).any()
    assert (rows["llm_decision"] == "accept").any()
    assert list(result["findings"].columns) == list(FINDINGS_COLUMNS)


# -----------------------------------------------------------------------------
# A package path
# -----------------------------------------------------------------------------


def _hits(query, role=None, sources=None, **kwargs):
    return pd.DataFrame(
        {
            "label": [f"Term {i} for {role}" for i in (1, 2)],
            "iri": [f"https://example.org/candidates/{role}Term{i}" for i in (1, 2)],
            "source": "smn",
            "ontology": "smn",
            "role": role,
            "match_type": "label_exact",
            "definition": f"A {role} term.",
            "score": [4.5, 3.5],
        }
    )


def _nothing(query, role=None, sources=None, **kwargs):
    return pd.DataFrame()


def _package(path: Path, frames: dict, search, monkeypatch, table_id: str, **kwargs) -> Path:
    """``create_sdp()`` with retrieval answered by ``search`` (the review
    console's tests stub it the same way)."""
    import functools

    from metasalmonpy import semantics

    with monkeypatch.context() as patched:
        patched.setattr(semantics, "suggest_semantics", functools.partial(semantics.suggest_semantics, search_fn=search))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            create_sdp(
                frames, path=path, dataset_id="demo-1", table_id=table_id, semantic_max_per_role=1,
                seed_semantics=True, seed_verbose=False, check_updates=False, overwrite=True, **kwargs,
            )
    return path


def _spawners_package(tmp_path, monkeypatch, name="record-package") -> Path:
    frames = {"spawners": pd.DataFrame({"stream_name": ["Bear Creek", "Elk River"], "spawner_count": [120, 340]})}
    return _package(tmp_path / name, frames, _hits, monkeypatch, "spawners")


def _answer_every_slot(packet_path, first_candidate=True) -> pd.DataFrame:
    rows = []
    for slot in _slots(_read_json(packet_path)):
        if not slot["candidates"] or not first_candidate:
            rows.append(_harness_row(slot["target"], llm_decision="review", llm_confidence=0.2, llm_rationale="Nothing offered."))
        else:
            rows.append(
                _harness_row(
                    slot["target"], llm_decision="accept", llm_confidence=0.9, llm_selected_candidate_index=1,
                    llm_selected_iri=slot["candidates"][0]["iri"], llm_rationale="First.",
                )
            )
    return _harness_frame(rows)


def test_semantic_llm_assessments_path_reads_the_persisted_record_with_its_findings(tmp_path, monkeypatch):
    path = _spawners_package(tmp_path, monkeypatch)
    assert semantic_llm_assessments(str(path)) is None
    built = write_semantic_review_packet(str(path), search_fn=_hits, quiet=True)
    harness = _answer_every_slot(built["path"])
    # A data frame carries no sidecar, so the packet is named by argument;
    # without it the file is unbound.
    with pytest.raises(SemanticReviewError) as raised:
        ingest_semantic_assessments(str(path), assessments=harness, search_fn=_no_search, quiet=True)
    assert raised.value.code == "packet_unbound"
    result = ingest_semantic_assessments(str(path), assessments=harness, packet_id=built["packet_id"], search_fn=_no_search, quiet=True)
    record = semantic_llm_assessments(str(path))
    assert _frame_text(record) == _frame_text(result["assessments"])
    assert list(record.attrs["semantic_validator_findings"].columns) == list(FINDINGS_COLUMNS)
    # The suggestions were rewritten with the assessment columns, and the
    # review console reads them.
    assert "llm_decision" in semantic_suggestions(str(path)).columns
    review = review_semantics(str(path))
    assert (review["llm_decision"] == "accept").any()
    # The metadata CSVs were not touched: no candidate IRI without its marker.
    dictionary = read_salmon_datapackage(str(path))["dictionary"]
    written = dictionary["term_iri"].astype(str)
    assert not (written.str.contains("example.org/candidates") & ~written.str.startswith("REVIEW")).any()


def test_a_package_build_calls_search_fn_once_per_distinct_query_role_and_source_set(tmp_path, monkeypatch):
    path = _spawners_package(tmp_path, monkeypatch, "counting-package")
    log = []

    def counting(query, role=None, sources=None):
        log.append((query, role, tuple(sources or ())))
        return _hits(query, role, sources)

    built = write_semantic_review_packet(str(path), search_fn=counting, quiet=True)
    assert len(log) == len(set(log)) == built["targets"]
    # The same call with the same inputs writes the same bytes.
    again = write_semantic_review_packet(str(path), search_fn=counting, review_dir=tmp_path / "review-again", quiet=True)
    assert again["packet_id"] == built["packet_id"]
    assert Path(again["path"]).read_bytes() == Path(built["path"]).read_bytes()


def test_write_refuses_a_parsed_object_as_context_and_a_session_with_answers(tmp_path):
    case = _build_case("bundle_accept", tmp_path)
    with pytest.raises(TypeError, match="local file paths"):
        write_semantic_review_packet(case["dict"], context_files=pd.DataFrame({"x": [1]}), review_dir=tmp_path / "r")
    # A packet alone may be rewritten; a session with answers may not.
    write_semantic_review_packet(case["dict"], review_dir=case["review_dir"], quiet=True)
    shutil.copyfile(case["case_dir"] / "harness-1.csv", case["review_dir"] / "semantic-assessments-pass-1.csv")
    with pytest.raises(FileExistsError, match="already exists"):
        write_semantic_review_packet(case["dict"], review_dir=case["review_dir"], quiet=True)
    write_semantic_review_packet(case["dict"], review_dir=case["review_dir"], overwrite=True, quiet=True)
    assert not (case["review_dir"] / "semantic-assessments-pass-1.csv").exists()
    with pytest.raises(ValueError, match="review_dir"):
        write_semantic_review_packet(case["dict"])


def test_apply_semantic_suggestions_llm_applies_only_an_accept():
    frame = pd.DataFrame(
        {
            "dataset_id": "d1", "table_id": "t1", "column_name": ["a", "b"], "code_value": None,
            "dictionary_role": "variable", "target_scope": "column", "target_sdp_file": "column_dictionary.csv",
            "target_sdp_field": "term_iri", "target_row_key": ["d1/t1/a", "d1/t1/b"], "search_query": "q",
            "label": ["A", "B"], "iri": ["https://example.org/a", "https://example.org/b"], "source": "smn",
            "ontology": "smn", "definition": "x", "score": 1.0,
            "llm_selected": [True, True], "llm_decision": ["accept", "review"], "llm_confidence": [0.9, 0.9],
        }
    )
    dictionary = pd.DataFrame(
        {"dataset_id": "d1", "table_id": "t1", "column_name": ["a", "b"], "column_role": "measurement", "term_iri": None}
    )
    out = apply_semantic_suggestions(dictionary, suggestions=frame, strategy="llm", verbose=False)
    assert out.loc[out["column_name"] == "a", "term_iri"].iloc[0] == "https://example.org/a"
    assert pd.isna(out.loc[out["column_name"] == "b", "term_iri"].iloc[0])


# -----------------------------------------------------------------------------
# The sentinel: no model call, and no network except through search_fn
# -----------------------------------------------------------------------------

#: Every entry point that reaches a model provider, in llm_review and
#: chat_decomposition.
PROVIDER_SYMBOLS = (
    ("llm_review", "request_json"),
    ("llm_review", "_request_json_with_retries"),
    ("llm_review", "resolve_llm_config"),
    ("llm_review", "assess_semantic_suggestions"),
    ("llm_review", "_assess_semantic_suggestions"),
    ("llm_review", "_assess_generic"),
    ("llm_review", "_assess_bundle"),
    ("llm_review", "_generated_retry_query"),
    ("llm_review", "_apply_bundle_retry"),
    ("llm_review", "_apply_generic_retry"),
    ("chat_decomposition", "chat_decomposition"),
)

NEW_MODULES = (
    "semantic_review_json.py",
    "semantic_review_packet.py",
    "semantic_review_ingest.py",
    "semantic_review_deprecation.py",
)


class _Reached(BaseException):
    """Raised by a blocked entry point; a BaseException so no handler swallows it."""


def _block_every_network_path(monkeypatch):
    import importlib

    import requests

    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise _Reached("a blocked model-provider or network entry point was called")

    for module_name, symbol in PROVIDER_SYMBOLS:
        module = importlib.import_module(f"metasalmonpy.{module_name}")
        monkeypatch.setattr(module, symbol, explode)
    monkeypatch.setattr(requests.Session, "request", explode)
    monkeypatch.setattr(requests, "request", explode)
    monkeypatch.setattr(socket.socket, "connect", explode)
    monkeypatch.setattr(socket.socket, "connect_ex", explode)
    monkeypatch.setattr(socket, "create_connection", explode)
    monkeypatch.setattr(socket, "getaddrinfo", explode)


def test_the_builder_and_the_ingester_make_no_model_call_and_open_no_socket(tmp_path, monkeypatch):
    """The claim that can be proven: no model-provider call ever, and no
    network except through ``search_fn`` (execplan section 7.3).

    Blocks the provider entry points, ``requests`` and the socket API itself
    -- a guard shaped like today's HTTP client stops guarding the moment the
    client changes -- and then builds and ingests: an in-memory build, an
    ingest with no retry (which never calls ``search_fn``), and an ingest with
    a retry (which calls it once per usable query and for nothing else).
    Retires when: never, while either function documents that it makes no
    model call.
    """
    path = _spawners_package(tmp_path, monkeypatch, "sentinel-package")
    _block_every_network_path(monkeypatch)

    case = _build_case("bundle_accept", tmp_path)
    assert Path(case["built"]["path"]).is_file()
    result = ingest_semantic_assessments(
        case["dict"], assessments=str(case["case_dir"] / "harness-1.csv"), review_dir=case["review_dir"],
        search_fn=_no_search, quiet=True,
    )
    assert result["status"] == "complete"

    retry = _build_case("retry_gain", tmp_path)
    calls: list = []
    ingest_semantic_assessments(
        retry["dict"], assessments=str(retry["case_dir"] / "harness-1.csv"), review_dir=retry["review_dir"],
        search_fn=_fake_search(_search_responses(), calls), quiet=True,
    )
    assert [call["query"] for call in calls] == ["fishing gear type"]

    # A package path reaches the network only through search_fn.
    built = write_semantic_review_packet(str(path), search_fn=_hits, quiet=True)
    ingest_semantic_assessments(
        str(path), assessments=_answer_every_slot(built["path"]), packet_id=built["packet_id"], search_fn=_no_search, quiet=True
    )


def _module_imports(tree) -> set:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(("." * node.level) + (node.module or ""))
    return names


def test_the_new_modules_import_no_http_client_and_name_no_provider_code():
    blocked_imports = {"requests", "urllib.request", "http.client", "socket", "httpx", "urllib3", ".chat_decomposition"}
    provider_names = {symbol for _, symbol in PROVIDER_SYMBOLS}
    for name in NEW_MODULES:
        tree = ast.parse((CHECKOUT / name).read_text(encoding="utf-8"))
        assert not (_module_imports(tree) & blocked_imports), name
        referenced = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        referenced |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        referenced |= {
            alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names
        }
        assert not (referenced & provider_names), (name, referenced & provider_names)


def _package_functions() -> dict:
    """Every module-level function of the package, with the names it can reach.

    A static call graph: a function reaches a name it loads, an attribute it
    reads, and a name it imports inside its body; a module-level
    ``from .x import y`` is followed to ``x.y``.
    """
    index = {}
    imports = {}
    for path in sorted(CHECKOUT.glob("*.py")):
        module = path.stem
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = {}
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
                for alias in node.names:
                    aliases[alias.asname or alias.name] = (node.module, alias.name)
        imports[module] = aliases
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                index[(module, node.name)] = node
    return index, imports


def test_no_provider_symbol_is_reachable_from_the_two_exported_functions():
    """R walks ``codetools::findGlobals()`` from the two functions; this walks the
    package's static call graph the same way, and asserts no provider entry
    point is reachable. ``search_fn``'s default, ``find_terms()``, is retrieval,
    not a model call, and is reached only as that argument's value."""
    index, imports = _package_functions()
    provider = set(PROVIDER_SYMBOLS)
    frontier = [("semantic_review_packet", "write_semantic_review_packet"), ("semantic_review_ingest", "ingest_semantic_assessments")]
    reachable = set()
    while frontier:
        key = frontier.pop()
        if key in reachable or key not in index:
            continue
        reachable.add(key)
        module, _ = key
        node = index[key]
        local = {}
        for child in ast.walk(node):
            if isinstance(child, ast.ImportFrom) and child.level == 1 and child.module:
                for alias in child.names:
                    local[alias.asname or alias.name] = (child.module, alias.name)
        for child in ast.walk(node):
            name = child.id if isinstance(child, ast.Name) else None
            if name is None:
                continue
            if name in local:
                frontier.append(local[name])
            elif name in imports[module]:
                frontier.append(imports[module][name])
            elif (module, name) in index:
                frontier.append((module, name))
    assert ("semantic_review_ingest", "_validate_row") in reachable
    assert ("llm_review", "_apply_validators") in reachable
    assert not (reachable & provider), sorted(reachable & provider)
    # Retrieval is reached only through the search_fn argument, whose default
    # is find_terms().
    import inspect

    from metasalmonpy import find_terms

    for function in (write_semantic_review_packet, ingest_semantic_assessments):
        assert inspect.signature(function).parameters["search_fn"].default is find_terms


# -----------------------------------------------------------------------------
# The Theme A oracles, replayed through the ingester
# -----------------------------------------------------------------------------
#
# The six Theme A cases and three adversarial variants are conformance cases
# like any other (above); these tests add the oracle *events* after the
# downstream prefill step, which is how the Theme A oracles reach this package
# (PARITY.md row 45). ``tests/data/semantic_review/theme-a/cases-v1.json`` is
# metasalmon's ``tests/testthat/fixtures/theme-a/cases-v1.json``, vendored
# byte for byte; the event builder and the oracle evaluator below are ports of
# ``events_from_package_outputs()`` and ``evaluate_oracles()`` from metasalmon's
# ``scripts/theme-a-benchmark.R``, kept to what the tests read.

THEME_A_CASES = Path(__file__).resolve().parent / "data" / "semantic_review" / "theme-a" / "cases-v1.json"
THEME_A_CASE_IDS = {
    "catch_count": "ta_catch_count",
    "catch_weight_advisory": "ta_catch_weight",
    "fork_length_explicit_procedure": "ta_fork_length",
    "ocean_phase_explicit_lifecycle": "ta_ocean_phase",
    "synthetic_structured_gap": "ta_gap",
    "handcrafted_gcdfo_routing": "ta_gcdfo_routing",
}


def _present(value) -> bool:
    if value is None or value is pd.NA:
        return False
    if isinstance(value, float) and math.isnan(value):
        return False
    return bool(str(value).strip())


def _scope_from_namespace(namespace) -> str:
    if not _present(namespace):
        return "uncertain"
    value = str(namespace).strip().lower()
    return value if value in ("smn", "gcdfo", "profile") else "uncertain"


def _events_from_package_outputs(assessments, final_dictionary, gaps, requests) -> list:
    """``events_from_package_outputs()``: assessment, selection, prefill, gap and routing events."""
    events = []
    for _, row in assessments.iterrows():
        decision = str(row["llm_decision"]) if _present(row["llm_decision"]) else None
        events.append({"type": "assessment", "role": row["dictionary_role"], "decision": decision})
        if _present(row["llm_selected_iri"]):
            events.append({"type": "selection", "role": row["dictionary_role"], "iri": str(row["llm_selected_iri"]), "decision": decision})
    fields = {"variable": "term_iri", "property": "property_iri", "entity": "entity_iri", "unit": "unit_iri"}
    for _, row in final_dictionary.iterrows():
        for role, field in fields.items():
            if field not in final_dictionary.columns or not _present(row[field]):
                continue
            value = str(row[field])
            if re.match(r"^\s*REVIEW\s*:", value, re.IGNORECASE):
                iri = re.sub(r"^\s*REVIEW\s*:\s*", "", value, flags=re.IGNORECASE)
                events.append({"type": "prefill", "role": role, "iri": iri, "decision": "accept"})
    for _, gap in gaps.iterrows():
        if "llm_decision" in gaps.columns and _present(gap["llm_decision"]):
            decision = str(gap["llm_decision"])
        elif "detection" in gaps.columns and _present(gap["detection"]):
            decision = str(gap["detection"])
        else:
            decision = "candidate_gap"
        scope = _scope_from_namespace(gap["llm_new_term_namespace"]) if "llm_new_term_namespace" in gaps.columns else "uncertain"
        if scope == "uncertain":
            placement = str(gap["placement_recommendation"]).strip().lower() if (
                "placement_recommendation" in gaps.columns and _present(gap["placement_recommendation"])
            ) else ""
            scope = placement if placement in ("smn", "gcdfo", "profile") else "uncertain"
        events.append({"type": "gap", "role": str(gap["dictionary_role"]), "scope": scope, "decision": decision})
    if len(requests):
        for _, request in requests[requests["request_scope"].isin(["smn", "gcdfo", "profile"])].iterrows():
            events.append({"type": "routing", "scope": str(request["request_scope"]), "repository": str(request["ontology_repo"])})
    return events


def _scalar_key(value) -> str:
    if value is None:
        return "<NA>"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _event_matches_rule(event: dict, rule: dict) -> bool:
    fields = [field for field in rule if field not in ("rule_id", "note", "advisory")]
    return all(field in event and _scalar_key(event[field]) == _scalar_key(rule[field]) for field in fields)


def _evaluate_oracles(cases: dict, observed: dict) -> dict:
    """``evaluate_oracles()``, kept to its verdict: every non-advisory required
    rule matched by an event, no forbidden rule matched, per blocking case."""
    failures = []
    for case in cases["cases"]:
        events = observed[case["case_id"]]
        for rule in case["oracle"].get("required", []):
            if not rule.get("advisory") and not any(_event_matches_rule(event, rule) for event in events):
                failures.append((case["case_id"], rule["rule_id"], "required event not observed", case.get("blocking")))
        for rule in case["oracle"].get("forbidden", []):
            if any(_event_matches_rule(event, rule) for event in events):
                failures.append((case["case_id"], rule["rule_id"], "forbidden event observed", case.get("blocking")))
    blocking = [failure for failure in failures if failure[3]]
    return {"status": "pass" if not blocking else "fail", "failures": failures}


def _theme_a_run(case_id: str, tmp_path: Path) -> dict:
    """Build and ingest a Theme A case, then run this package's own prefill
    step, gap detection and term-request renderer over the result."""
    from metasalmonpy import render_ontology_term_request
    from metasalmonpy.package_io import _auto_apply_package_suggestions

    case = _build_case(case_id, tmp_path)
    result = ingest_semantic_assessments(
        case["dict"], assessments=str(case["case_dir"] / "harness-1.csv"), review_dir=case["review_dir"],
        search_fn=_no_search, quiet=True,
    )
    original = case["dict"].copy()
    original.attrs = {}
    artifacts = {"dict": original.copy(), "semantic_suggestions": result["suggestions"], "table_meta": pd.DataFrame()}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        _auto_apply_package_suggestions(artifacts, llm_assess=True)
        final = artifacts["dict"]
        gap_input = final.copy()
        gap_input.attrs = {
            "semantic_suggestions": result["suggestions"],
            "semantic_llm_assessments": result["assessments"],
            "semantic_targets": result["targets"],
        }
        gaps = detect_semantic_term_gaps(gap_input)
        requests = (
            render_ontology_term_request(gaps, scope="auto", ask=False, profile_name="theme-a-benchmark")
            if len(gaps)
            else pd.DataFrame()
        )
    return {
        "result": result,
        "events": _events_from_package_outputs(result["assessments"], final, gaps, requests),
        "gap_count": len(gaps),
        "term_request_count": len(requests),
        "expected": _read_json(case["case_dir"] / "expected" / "events.json"),
    }


def test_the_theme_a_cases_pass_their_recorded_oracles():
    cases = _read_json(THEME_A_CASES)
    assert sorted(case["case_id"] for case in cases["cases"]) == sorted(THEME_A_CASE_IDS)
    observed = {
        case_id: _read_json(FIXTURES / fixture_id / "expected" / "events.json")["events"]
        for case_id, fixture_id in THEME_A_CASE_IDS.items()
    }
    evaluation = _evaluate_oracles(cases, observed)
    assert evaluation["status"] == "pass", evaluation["failures"]


@pytest.mark.parametrize(
    "case_id",
    sorted(THEME_A_CASE_IDS.values()) + ["ta_ctx_accept", "ta_gap_reject", "ta_method_accept"],
)
def test_the_recorded_events_are_what_this_package_produces(case_id, tmp_path):
    """The ingester's result through this package's own prefill step, gap
    detection and term-request renderer gives the events metasalmon recorded,
    event for event -- every Theme A case, where metasalmon's own test
    rebuilds three."""
    run = _theme_a_run(case_id, tmp_path)
    assert run["events"] == run["expected"]["events"]
    assert run["gap_count"] == run["expected"]["gap_count"]
    assert run["term_request_count"] == run["expected"]["term_request_count"]


def test_three_adversarial_harness_answers_are_caught_by_the_deterministic_layer(tmp_path):
    cases = _read_json(THEME_A_CASES)
    by_id = {case["case_id"]: case for case in cases["cases"]}

    def passes(case_id, events):
        return _evaluate_oracles({"cases": [by_id[case_id]]}, {case_id: events})["status"] == "pass"

    # Accepting the fork-length method for catch_count fails the forbidden rule
    # through SEM_METHOD_EVIDENCE_REQUIRED: the accept becomes review.
    method = _theme_a_run("ta_method_accept", tmp_path / "method")
    assert "SEM_METHOD_EVIDENCE_REQUIRED" in set(method["result"]["findings"]["code"])
    record = method["result"]["assessments"]
    assert list(record.loc[record["dictionary_role"] == "method", "llm_decision"]) == ["review"]
    assert passes("catch_count", method["events"])
    # The evaluator bites: the accept the validators refused would have failed.
    refused = {"type": "selection", "role": "method", "iri": "https://w3id.org/smn/ForkLengthMeasurementFieldMethod", "decision": "accept"}
    assert not passes("catch_count", method["events"] + [refused])

    # Accepting CatchContext beside CatchAbundance raises SEM_REDUNDANT_CATCH_CONTEXT.
    context = _theme_a_run("ta_ctx_accept", tmp_path / "context")
    assert "SEM_REDUNDANT_CATCH_CONTEXT" in set(context["result"]["findings"]["code"])
    record = context["result"]["assessments"]
    assert list(record.loc[record["dictionary_role"] == "constraint", "llm_decision"]) == ["review"]
    assert passes("catch_count", context["events"])

    # A reject_shortlist on synthetic_structured_gap still surfaces the gap
    # through escalation.
    gap = _theme_a_run("ta_gap_reject", tmp_path / "gap")
    row = gap["result"]["assessments"].iloc[0]
    assert row["llm_decision"] == "request_new_term"
    assert row["llm_escalated_from"] == "reject_shortlist"
    assert any(event["type"] == "gap" and event["scope"] == "uncertain" for event in gap["events"])
    assert passes("synthetic_structured_gap", gap["events"])


# -----------------------------------------------------------------------------
# Blank slots with no candidates reach a package-path packet
# -----------------------------------------------------------------------------


def _zero_candidate_hits(query, role=None, sources=None, **kwargs):
    # Retrieval finds nothing for the water temperature column.
    if re.search("water|temp", str(query), re.IGNORECASE):
        return pd.DataFrame()
    slug = re.sub("[^a-z]", "-", str(query).lower())
    return pd.DataFrame(
        {
            "label": [f"Term {i} for {role}" for i in (1, 2)],
            "iri": [f"https://example.org/candidates/{role}/{slug}{i}" for i in (1, 2)],
            "source": "smn",
            "ontology": "smn",
            "role": role,
            "match_type": "label_exact",
            "definition": "A term.",
            "score": [4.5, 3.5],
        }
    )


def _zero_candidate_package(path, monkeypatch, code_scope="factor"):
    frames = {
        "catch": pd.DataFrame(
            {
                "water_temp": [8.5, 9.1, 7.4, 10.2],
                "gear_type": pd.Categorical(["GN", "SN", "GN", "TR"]),
                "catch_weight": [12.5, 8.1, 20.4, 3.3],
            }
        )
    }
    return _package(path, frames, _zero_candidate_hits, monkeypatch, "catch", semantic_code_scope=code_scope)


def test_a_blank_slot_with_no_candidates_reaches_the_packet_through_discovery(tmp_path, monkeypatch):
    path = _zero_candidate_package(tmp_path / "zero-candidate", monkeypatch)
    assert "water_temp" not in set(semantic_suggestions(str(path))["column_name"])
    built = write_semantic_review_packet(str(path), search_fn=_zero_candidate_hits, quiet=True)
    packet = _read_json(built["path"])
    slots = _slots(packet)
    water = [
        slot for slot in slots
        if slot["target"]["column_name"] == "water_temp" and slot["target"]["target_sdp_file"] == "column_dictionary.csv"
    ]
    assert water and all(not slot["candidates"] for slot in water)
    # Recovered as the bundle it is: a measurement column's slots judged together.
    bundles = [unit for unit in packet["units"] if unit["unit_key"] == "bundle:demo-1/catch/water_temp"]
    assert len(bundles) == 1 and bundles[0]["unit_kind"] == "bundle"
    assert packet["pins"]["retrieval"]["code_scope"] == "factor"
    assert len(built["not_covered"]) == 0

    # The harness answers the zero-candidate slots as the instructions say.
    rows = []
    for slot in slots:
        if not slot["candidates"]:
            rows.append(
                _harness_row(slot["target"], llm_decision="request_new_term", llm_confidence=0.6,
                             llm_rationale="No candidate was offered.", llm_new_term_label="Water temperature")
            )
        else:
            rows.append(_harness_row(slot["target"], llm_decision="review", llm_confidence=0.5, llm_rationale="Later."))
    result = ingest_semantic_assessments(
        str(path), assessments=_harness_frame(rows), packet_id=built["packet_id"], search_fn=_no_search, quiet=True
    )
    record = result["assessments"]
    water_rows = record[(record["column_name"] == "water_temp") & (record["target_sdp_file"] == "column_dictionary.csv")]
    assert len(water_rows) >= 1 and (water_rows["llm_decision"] == "request_new_term").all()
    gaps = detect_semantic_term_gaps(result["dictionary"])
    assert "water_temp" in set(gaps["column_name"])


def test_a_blank_code_level_slot_outside_the_code_scope_is_reported_as_not_covered(tmp_path, monkeypatch):
    # Created with no code-level seeding, so every gear code slot is blank and
    # has no suggestion row.
    path = _zero_candidate_package(tmp_path / "code-scope", monkeypatch, code_scope="none")
    assert "codes.csv" not in set(semantic_suggestions(str(path))["target_sdp_file"])
    narrow = write_semantic_review_packet(
        str(path), search_fn=_zero_candidate_hits, code_scope="none", review_dir=tmp_path / "narrow", quiet=True
    )
    assert len(narrow["not_covered"]) > 0
    assert (narrow["not_covered"]["target_sdp_file"] == "codes.csv").all()
    narrow_packet = _read_json(narrow["path"])
    assert narrow_packet["pins"]["retrieval"]["code_scope"] == "none"
    assert not any(slot["target"]["target_sdp_file"] == "codes.csv" for slot in _slots(narrow_packet))

    wide = write_semantic_review_packet(
        str(path), search_fn=_zero_candidate_hits, code_scope="all", review_dir=tmp_path / "wide", quiet=True
    )
    assert len(wide["not_covered"]) == 0
    code_slots = [slot for slot in _slots(_read_json(wide["path"])) if slot["target"]["target_sdp_file"] == "codes.csv"]
    assert {slot["target"]["slot_id"] for slot in code_slots} == set(narrow["not_covered"]["slot_id"])


def test_a_package_whose_every_lookup_found_nothing_still_gets_a_packet_holding_its_blank_slots(tmp_path, monkeypatch):
    frames = {"catch": pd.DataFrame({"catch_weight": [12.5, 8.1, 20.4], "water_temp": [8.5, 9.1, 7.4]})}
    path = _package(tmp_path / "all-zero", frames, _nothing, monkeypatch, "catch")
    # No shortlist was written, and the console refuses to open a queue.
    assert semantic_suggestions(str(path)) is None
    with pytest.raises(ValueError, match="No semantic suggestions"):
        review_semantics(str(path))

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        built = write_semantic_review_packet(str(path), search_fn=_nothing, quiet=True)
    packet = _read_json(built["path"])
    slots = _slots(packet)
    assert slots and all(not slot["candidates"] for slot in slots)
    kinds = {unit["unit_key"]: unit["unit_kind"] for unit in packet["units"]}
    assert {key for key, kind in kinds.items() if kind == "bundle"} == {
        "bundle:demo-1/catch/catch_weight",
        "bundle:demo-1/catch/water_temp",
    }
    assert any(key.startswith("target:tables.csv") for key in kinds)
    assert {slot["target"]["column_name"] for slot in slots if slot["target"]["column_name"]} == {"catch_weight", "water_temp"}

    # The gaps reach the term-request pipeline through the ingester.
    rows = [
        _harness_row(slot["target"], llm_decision="request_new_term", llm_confidence=0.7, llm_rationale="Nothing was offered.",
                     llm_new_term_label=f"Term for {slot['target']['dictionary_role']}")
        for slot in slots
    ]
    result = ingest_semantic_assessments(
        str(path), assessments=_harness_frame(rows), packet_id=built["packet_id"], search_fn=_no_search, quiet=True
    )
    assert result["status"] == "complete"
    assert (result["assessments"]["llm_decision"] == "request_new_term").all()
    gaps = detect_semantic_term_gaps(result["dictionary"])
    assert {"catch_weight", "water_temp"} <= set(gaps["column_name"])


def test_a_tables_csv_row_pointing_outside_the_package_is_refused_not_followed(tmp_path, monkeypatch):
    from metasalmonpy.semantic_review_packet import _contained_resource

    path = _package(tmp_path / "escape", {"catch": pd.DataFrame({"catch_weight": [12.5, 8.1]})}, _nothing, monkeypatch, "catch")
    secret_dir = tmp_path / "secret"
    secret_dir.mkdir()
    secret = secret_dir / "outside.csv"
    secret.write_text("catch_weight\n999\n", encoding="utf-8")
    tables_path = path / "metadata" / "tables.csv"
    tables = pd.read_csv(tables_path, dtype=str, keep_default_na=False)
    tables.loc[0, "file_name"] = f"../../{secret_dir.name}/outside.csv"
    tables.to_csv(tables_path, index=False)
    assert _contained_resource(path, tables.loc[0, "file_name"]) is None
    assert _contained_resource(path, "/etc/hosts") is None
    assert _contained_resource(path, "data/./spawners.csv") is None
    assert _contained_resource(path, "data/catch.csv") == path / "data" / "catch.csv"
    # A symbolic link inside the package is refused too.
    link = path / "data" / "linked.csv"
    try:
        link.symlink_to(secret)
    except OSError:  # pragma: no cover - platforms without symlinks
        pass
    else:
        assert _contained_resource(path, "data/linked.csv") is None
    with pytest.warns(UserWarning, match="not plain files inside the package"):
        built = write_semantic_review_packet(str(path), search_fn=_nothing, review_dir=tmp_path / "r", quiet=True)
    assert built["units"] > 0


# -----------------------------------------------------------------------------
# The continuation pass
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("variant", ["missing", "unusable"])
def test_a_continuation_target_left_unanswered_keeps_its_pass_1_answer_and_the_session_completes(variant, tmp_path):
    case = _build_case("retry_gain", tmp_path)
    first = ingest_semantic_assessments(
        case["dict"], assessments=str(case["case_dir"] / "harness-1.csv"), review_dir=case["review_dir"],
        search_fn=_fake_search(_search_responses()), quiet=True,
    )
    assert first["status"] == "awaiting_pass_2"
    pass_2 = _read_json(first["next_packet"])
    slot = _slots(pass_2)[0]
    assert slot["reassess"] and len(slot["candidates"]) == 4
    answer = (
        _harness_frame([])
        if variant == "missing"
        else _harness_frame([_harness_row(slot["target"], llm_error="The model timed out.")])
    )
    result = ingest_semantic_assessments(
        case["dict"], assessments=answer, packet_id=pass_2["packet_id"], review_dir=case["review_dir"], search_fn=_no_search, quiet=True
    )
    # Execplan section 4, item 3: the pass-1 row and candidates stand, the
    # session is complete, and the fallback is counted rather than silent.
    assert result["status"] == "complete"
    assert result["next_packet"] is None
    assert result["summary"]["kept_pass_1"] == 1
    assert result["summary"]["errors"] == (0 if variant == "missing" else 1)
    row = result["assessments"].iloc[0]
    assert row["llm_decision"] == "retry_search"
    assert "pass-1 answer stands" in row["llm_rationale"]
    assert bool(row["llm_exploration_used"]) is True
    assert set(result["suggestions"]["iri"]) == {"https://w3id.org/smn/Equipment", "https://w3id.org/smn/Tool"}
    assert not result["suggestions"]["llm_selected"].any()
    assert (result["suggestions"]["llm_decision"] == "retry_search").all()
    persisted = pd.read_csv(case["review_dir"] / "semantic-llm-assessments.csv", dtype=str)
    assert list(persisted["llm_decision"]) == ["retry_search"]


def _two_target_dictionary() -> pd.DataFrame:
    def target(column, query, label, description):
        return {
            "dataset_id": "fixture-1", "table_id": "catch", "column_name": column, "code_value": None,
            "dictionary_role": "variable", "search_role": "variable", "target_scope": "column",
            "target_sdp_file": "column_dictionary.csv", "target_sdp_field": "term_iri",
            "target_row_key": f"fixture-1/catch/{column}", "target_label": label, "target_description": description,
            "search_query": query, "target_query_basis": "column_description",
            "target_query_context": f"{label}: {description}", "column_label": label, "column_description": description,
            "code_label": None, "code_description": None,
        }

    targets = [
        target("GEAR_TYPE", "gear type", "Gear type", "The fishing gear used."),
        target("VESSEL", "vessel", "Vessel", "The vessel name."),
    ]

    def candidate(t, label, iri, score):
        return {
            **t, "label": label, "iri": iri, "source": "smn", "ontology": "Salmon Ontology", "role": "variable",
            "match_type": "label_partial", "definition": f"{label}.", "score": score, "term_type": "owl_class",
            "retrieval_query": t["search_query"], "retrieval_pass": 1,
        }

    candidates = [
        candidate(targets[0], "Equipment", "https://w3id.org/smn/Equipment", 0.4),
        candidate(targets[0], "Tool", "https://w3id.org/smn/Tool", 0.3),
        candidate(targets[1], "Vessel", "https://w3id.org/smn/Vessel", 0.6),
    ]
    dictionary = pd.DataFrame(
        {
            "dataset_id": "fixture-1", "table_id": "catch", "column_name": ["GEAR_TYPE", "VESSEL"],
            "column_label": ["Gear type", "Vessel"], "column_description": ["The fishing gear used.", "The vessel name."],
            "column_role": "categorical", "value_type": "string", "term_iri": None,
        }
    )
    dictionary.attrs["semantic_targets"] = pd.DataFrame(targets)
    dictionary.attrs["semantic_suggestions"] = pd.DataFrame(candidates)
    return dictionary, targets


def test_the_result_after_a_continuation_pass_describes_the_whole_session(tmp_path):
    dictionary, targets = _two_target_dictionary()
    review_dir = tmp_path / "review"
    built = write_semantic_review_packet(dictionary, review_dir=review_dir, quiet=True)
    assert built["units"] == 2
    harness_1 = _harness_frame(
        [
            _harness_row(targets[0], llm_decision="retry_search", llm_confidence=0.3, llm_rationale="Generic.", llm_retry_query="fishing gear type"),
            _harness_row(targets[1], llm_decision="accept", llm_confidence=0.9, llm_selected_candidate_index=1,
                         llm_selected_iri="https://w3id.org/smn/Vessel", llm_rationale="The vessel."),
        ]
    )
    first = ingest_semantic_assessments(
        dictionary, assessments=harness_1, packet_id=built["packet_id"], review_dir=review_dir,
        search_fn=_fake_search(_search_responses()), quiet=True,
    )
    assert first["status"] == "awaiting_pass_2"
    # At pass 1 the pending target is not merged; the accepted one is.
    assert set(first["suggestions"]["column_name"]) == {"VESSEL"}
    assert len(first["targets"]) == 2

    pass_2 = _read_json(first["next_packet"])
    assert len(pass_2["units"]) == 1
    gear = _slots(pass_2)[0]
    harness_2 = _harness_frame(
        [_harness_row(gear["target"], llm_decision="accept", llm_confidence=0.9, llm_selected_candidate_index=1,
                      llm_selected_iri="https://w3id.org/smn/FishingGear", llm_rationale="Found it.")]
    )
    result = ingest_semantic_assessments(
        dictionary, assessments=harness_2, packet_id=pass_2["packet_id"], review_dir=review_dir, search_fn=_no_search, quiet=True
    )
    assert result["status"] == "complete"
    # Every pass-1 target and every slot's candidates, with both accepts selected.
    assert set(result["targets"]["column_name"]) == {"GEAR_TYPE", "VESSEL"}
    assert len(result["assessments"]) == 2
    assert set(result["suggestions"]["column_name"]) == {"GEAR_TYPE", "VESSEL"}
    selected = result["suggestions"][result["suggestions"]["llm_selected"]]
    assert set(selected["iri"]) == {"https://w3id.org/smn/FishingGear", "https://w3id.org/smn/Vessel"}
    assert "https://w3id.org/smn/GearDeployment" in set(result["suggestions"]["iri"])
    assert semantic_suggestions(result["dictionary"]).equals(result["suggestions"])
    assert len(result["dictionary"].attrs["semantic_targets"]) == 2
    assert len(semantic_llm_assessments(result["dictionary"])) == 2


def test_a_package_that_started_with_no_shortlist_gets_one_when_a_retry_gains_candidates(tmp_path, monkeypatch):
    path = _package(
        tmp_path / "no-shortlist-retry", {"catch": pd.DataFrame({"water_temp": [8.5, 9.1, 7.4]})}, _nothing, monkeypatch,
        "catch", semantic_code_scope="none",
    )
    assert not (path / "semantic_suggestions.csv").exists()
    built = write_semantic_review_packet(str(path), search_fn=_nothing, code_scope="none", quiet=True)
    slots = _slots(_read_json(built["path"]))
    water = [slot for slot in slots if slot["target"]["column_name"] == "water_temp" and slot["target"]["dictionary_role"] == "variable"]
    assert len(water) == 1 and not water[0]["candidates"]

    def wider(query, role=None, sources=None):
        if query != "stream water temperature measured in situ":
            return pd.DataFrame()
        return pd.DataFrame(
            {"label": ["Water temperature"], "iri": ["https://w3id.org/smn/WaterTemperature"], "source": ["smn"],
             "ontology": ["Salmon Ontology"], "role": [role], "match_type": ["label_partial"],
             "definition": ["The temperature of the water."], "score": [0.92]}
        )

    rows = [
        _harness_row(slot["target"], llm_decision="retry_search", llm_confidence=0.3, llm_rationale="Search for water temperature.",
                     llm_retry_query="stream water temperature measured in situ")
        if slot["key"] == water[0]["key"]
        else _harness_row(slot["target"], llm_decision="review", llm_confidence=0.2, llm_rationale="Nothing offered.")
        for slot in slots
    ]
    first = ingest_semantic_assessments(str(path), assessments=_harness_frame(rows), packet_id=built["packet_id"], search_fn=wider, quiet=True)
    assert first["status"] == "awaiting_pass_2"
    pass_2 = _read_json(first["next_packet"])
    gear = [slot for slot in _slots(pass_2) if slot["reassess"]][0]
    assert len(gear["candidates"]) == 1
    harness_2 = _harness_frame(
        [_harness_row(gear["target"], llm_decision="accept", llm_confidence=0.9, llm_selected_candidate_index=1,
                      llm_selected_iri="https://w3id.org/smn/WaterTemperature", llm_rationale="Found it.")]
    )
    result = ingest_semantic_assessments(str(path), assessments=harness_2, packet_id=pass_2["packet_id"], search_fn=_no_search, quiet=True)
    assert result["status"] == "complete"
    # The shortlist file now exists, and the documented console path sees the accept.
    written = semantic_suggestions(str(path))
    assert written is not None and "https://w3id.org/smn/WaterTemperature" in set(written["iri"])
    assert {"decision", "decision_reason"} <= set(written.columns)
    review = review_semantics(str(path))
    assert "https://w3id.org/smn/WaterTemperature" in set(review["iri"])
    assert (review["llm_decision"] == "accept").any()


def test_a_pass_2_row_with_a_blank_provider_keeps_the_pass_1_answer_like_any_other_unusable_answer(tmp_path):
    case = _build_case("retry_gain", tmp_path)
    first = ingest_semantic_assessments(
        case["dict"], assessments=str(case["case_dir"] / "harness-1.csv"), review_dir=case["review_dir"],
        search_fn=_fake_search(_search_responses()), quiet=True,
    )
    pass_2 = _read_json(first["next_packet"])
    slot = _slots(pass_2)[0]
    blank = _harness_row(slot["target"], llm_decision="accept", llm_confidence=0.9, llm_selected_candidate_index=1,
                         llm_selected_iri="https://w3id.org/smn/FishingGear", llm_rationale="Found it.")
    blank["llm_provider"] = None
    result = ingest_semantic_assessments(
        case["dict"], assessments=_harness_frame([blank]), packet_id=pass_2["packet_id"], review_dir=case["review_dir"],
        search_fn=_no_search, quiet=True,
    )
    assert result["status"] == "complete"
    assert result["summary"]["kept_pass_1"] == 1
    assert result["summary"]["errors"] == 1
    row = result["assessments"].iloc[0]
    assert row["llm_decision"] == "retry_search"
    assert pd.isna(row["llm_error"])
    assert "llm_provider and llm_model must be non-empty" in row["llm_rationale"]
    assert set(result["suggestions"]["iri"]) == {"https://w3id.org/smn/Equipment", "https://w3id.org/smn/Tool"}


def test_a_symlinked_review_directory_or_record_is_refused_not_followed(tmp_path, monkeypatch):
    private = _spawners_package(tmp_path, monkeypatch, "private")
    built = write_semantic_review_packet(str(private), search_fn=_hits, quiet=True)
    ingest_semantic_assessments(
        str(private), assessments=_answer_every_slot(built["path"], first_candidate=False), packet_id=built["packet_id"],
        search_fn=_no_search, quiet=True,
    )
    assert semantic_llm_assessments(str(private)) is not None

    # Another package whose review/ is a link to the first one's.
    other = _spawners_package(tmp_path, monkeypatch, "other")
    try:
        (other / "review").symlink_to(private / "review", target_is_directory=True)
    except OSError:  # pragma: no cover - platforms without symlinks
        pytest.skip("symbolic links are not available here")
    with pytest.raises(ValueError, match="symbolic[- ]link"):
        semantic_llm_assessments(str(other))
    with pytest.raises(ValueError, match="symbolic[- ]link"):
        ingest_semantic_assessments(str(other), search_fn=_no_search, quiet=True)
    (other / "review").unlink()

    # And one whose record file alone is a link.
    (other / "review").mkdir()
    (other / "review" / "semantic-llm-assessments.csv").symlink_to(private / "review" / "semantic-llm-assessments.csv")
    with pytest.raises(ValueError, match="symbolic[- ]link"):
        semantic_llm_assessments(str(other))
    shutil.rmtree(other / "review")
    assert semantic_llm_assessments(str(other)) is None


def test_prune_warns_about_an_ingested_review_record(tmp_path, monkeypatch):
    path = _spawners_package(tmp_path, monkeypatch, "prune-package")
    built = write_semantic_review_packet(str(path), search_fn=_hits, quiet=True)
    ingest_semantic_assessments(
        str(path), assessments=_answer_every_slot(built["path"], first_candidate=False), packet_id=built["packet_id"],
        search_fn=_no_search, quiet=True,
    )
    package = read_salmon_datapackage(str(path))
    with pytest.warns(UserWarning, match="review/, which holds an ingested semantic review record"):
        write_salmon_datapackage(
            resources=package["resources"], dataset_meta=package["dataset"], table_meta=package["tables"],
            dict_df=package["dictionary"], codes=package["codes"], path=path, overwrite=True, prune=True,
        )


# -----------------------------------------------------------------------------
# The retriever, on the three points of R's rule B-327 added
# -----------------------------------------------------------------------------


def test_the_retriever_trims_the_query_and_skips_a_role_with_no_sources():
    from metasalmonpy.llm_review import make_source_policy
    from metasalmonpy.semantics import _retrieve_semantic_target_candidates

    target = {"dictionary_role": "variable", "search_role": "variable", "search_query": " \tspawner count\r\n", "unit_label": "count"}
    calls: list = []

    def search(query, role=None, sources=None):
        calls.append(query)
        return _hits(query, role, sources)

    rows = _retrieve_semantic_target_candidates(target, make_source_policy(None), 5, search)
    assert calls == ["spawner count"]
    assert set(rows["retrieval_query"]) == {"spawner count"}
    # Only the 19 target columns are stamped onto a candidate row.
    assert "unit_label" not in rows.columns
    # An explicit, empty allowlist names no source to search: nothing is searched.
    calls.clear()
    assert _retrieve_semantic_target_candidates(target, make_source_policy([]), 5, search).empty
    assert calls == []


def test_a_package_path_reads_an_empty_suggestion_field_as_missing(tmp_path, monkeypatch):
    # metasalmon reads the empty field as NA; this package's CSV reader keeps it
    # as "" (PARITY.md row 21). A target rebuilt from semantic_suggestions.csv
    # is built as R builds it: with its search_role column blanked, every
    # target searches, and is recorded, under its dictionary role.
    path = _spawners_package(tmp_path, monkeypatch, "blank-search-role")
    suggestions_path = path / "semantic_suggestions.csv"
    suggestions = pd.read_csv(suggestions_path, dtype=str, keep_default_na=False)
    suggestions["search_role"] = ""
    suggestions.to_csv(suggestions_path, index=False)
    roles = []

    def recording(query, role=None, sources=None):
        roles.append(role)
        return _hits(query, role, sources)

    built = write_semantic_review_packet(str(path), search_fn=recording, quiet=True)
    slots = _read_json(built["path"])["units"]
    targets = [slot["target"] for unit in slots for slot in unit["slots"]]
    assert targets and all(target["search_role"] == target["dictionary_role"] for target in targets)
    assert all(target["code_value"] is None for target in targets)
    assert None not in roles
