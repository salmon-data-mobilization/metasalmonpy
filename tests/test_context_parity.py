"""Context documents become the excerpts metasalmon builds (hub queue B-364).

Brett ruled on 2026-09-25 (S16 execplan, decision 4 and section 2.7) that the
two packages will share one review-packet file, so a context document has to
become the same excerpts on both sides. This file pins metasalmon's algorithm
on text-only fixtures:

* ``tests/data/context_parity/cases.json`` names the documents, the inline
  snippets and the target cases;
* ``tests/data/context_parity/expected.json`` is what metasalmon's
  ``.ms_collect_context_chunks()`` and ``.ms_score_context_chunks()`` produced
  for them, written by ``expected-from-r.R`` beside it (run with
  ``R_LIBS=/tmp/metasalmon-lib Rscript expected-from-r.R . expected.json``
  against metasalmon ``main`` at ``98cb9e6``, 2026-09-25);
* the offline tests below hold this package to that file, and the last test
  re-runs the R script wherever R and an installed metasalmon are available
  (the ``parity`` job of ``.github/workflows/parity.yml``) so a change on the
  R side turns that job red instead of drifting.

Library-specific extraction (PDF, DOCX, spreadsheets, HTML) is deliberately
outside this pin: PARITY.md row 62.

Tie order, because it is the one place the two sides are compared against a
rule rather than against R's current code: ``.ms_score_context_chunks()``
breaks ties on the source label with ``order()``, which follows the session
locale until hub item B-326 makes it radix. The fixture records the radix (C
collation) order, produced by running R under ``LC_COLLATE=C`` -- ``Zeta.txt``
before ``alpha.txt`` -- which is what this package sorts by and what R will
sort by everywhere once B-326 lands.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import warnings
from pathlib import Path

import pandas as pd
import pytest

from metasalmonpy.llm_review import (
    CONTEXT_EXCERPT_LIMIT,
    SUPPORTED_CONTEXT_EXTENSIONS,
    _chunk_context_text,
    _context_chunk_limit,
    _context_tokens,
    _decode_context_bytes,
    _make_unique,
    _r_trimws,
    _relevant_context,
    _score_context_chunks,
    load_context_chunks,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "data" / "context_parity"
R_LIB_PATH = "/tmp/metasalmon-lib"
HAVE_R = shutil.which("Rscript") is not None and os.path.isdir(R_LIB_PATH)


def _cases() -> dict:
    return json.loads((FIXTURE_DIR / "cases.json").read_text(encoding="utf-8"))


def _expected() -> dict:
    return json.loads((FIXTURE_DIR / "expected.json").read_text(encoding="utf-8"))


def _pool(cases: dict) -> pd.DataFrame:
    files = [FIXTURE_DIR / name for name in cases["files"]]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return load_context_chunks(files, cases["inline_text"])


def _records(frame: pd.DataFrame) -> list[dict]:
    return [
        {"source": row["source"], "chunk_id": row["chunk_id"], "text": row["text"]}
        for row in frame.to_dict("records")
    ]


def _scores(frame: pd.DataFrame) -> list[dict]:
    return [
        {
            "source": row["source"],
            "chunk_id": row["chunk_id"],
            "context_score": (
                int(row["context_score"]) if "context_score" in row else None
            ),
        }
        for row in frame.to_dict("records")
    ]


# --- the shared fixture -------------------------------------------------------


def test_the_pool_is_what_metasalmon_collects():
    # Every source label, chunk id and chunk text, in pool order: the
    # extension list, R's text extraction (cp1252 fallback, CRLF and bare CR,
    # the byte-order mark, front matter and fences for .qmd only), 2200/200
    # chunking, parent-directory labels and make.unique's " #1".
    cases = _cases()
    files = [FIXTURE_DIR / name for name in cases["files"]]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        pool = load_context_chunks(files, cases["inline_text"])
    assert _records(pool) == _expected()["pool"]
    # The unsupported and empty documents were skipped with a warning each,
    # as R skips them, rather than read as text or dropped silently.
    messages = sorted(str(warning.message) for warning in caught)
    assert [message.split(" ", 1)[0] for message in messages] == ["Skipping"] * 3
    assert any("ignored.ipynb" in message for message in messages)
    assert any("empty.txt" in message for message in messages)
    assert any("blank.md" in message for message in messages)


@pytest.mark.parametrize("name", [case["name"] for case in _cases()["targets"]])
def test_each_target_scores_the_pool_as_metasalmon_does(name):
    cases = _cases()
    case = next(case for case in cases["targets"] if case["name"] == name)
    pool = _pool(cases)
    candidates = pd.DataFrame(case["candidates"], columns=["label", "definition"])
    scored = _score_context_chunks(
        pool, case["target"], candidates, max_chunks=case["max_chunks"]
    )
    assert _scores(scored) == _expected()["targets"][name]


def test_ties_break_on_the_source_label_in_c_collation():
    # Two files hold the same sentence, so the "tie" case scores them equally
    # at equal length and the label decides: Z (0x5A) sorts before a (0x61)
    # in C collation, the reverse of what R's locale-following order() gives
    # in an en_CA or en_US session until B-326.
    expected = _expected()["targets"]["tie"]
    assert [row["chunk_id"] for row in expected[:2]] == ["Zeta.txt#1", "alpha.txt#1"]
    cases = _cases()
    case = next(case for case in cases["targets"] if case["name"] == "tie")
    scored = _score_context_chunks(_pool(cases), case["target"], None, 4)
    assert scored["chunk_id"].tolist()[:2] == ["Zeta.txt#1", "alpha.txt#1"]


# --- the pieces, each against a measured R fact ------------------------------


def test_the_extension_list_is_metasalmons():
    assert SUPPORTED_CONTEXT_EXTENSIONS == (
        "md", "txt", "csv", "tsv", "json", "yaml", "yml", "rst", "r", "rmd",
        "qmd", "pdf", "htm", "html", "docx", "xls", "xlsx", "xlsm",
    )


def test_unsupported_and_empty_files_are_skipped_with_a_warning(tmp_path):
    notebook = tmp_path / "cells.ipynb"
    notebook.write_text('{"cells": []}', encoding="utf-8")
    empty = tmp_path / "empty.md"
    empty.write_text("  \n\t\n", encoding="utf-8")
    with pytest.warns(UserWarning, match="unsupported context file"):
        assert load_context_chunks([notebook]).empty
    with pytest.warns(UserWarning, match="empty context file"):
        assert load_context_chunks([empty]).empty


def test_text_decoding_follows_read_text_utf8():
    # UTF-8 first; a byte-order mark is discarded as readLines() discards it.
    assert _decode_context_bytes(b"\xef\xbb\xbfcaf\xc3\xa9") == "café"
    # Invalid UTF-8 falls to Windows-1252, where 0x80 is the euro sign and the
    # five undefined bytes are dropped like iconv(sub = "") drops them.
    assert _decode_context_bytes(b"a\x80b\x81c") == "a€bc"
    # latin-1 is reached only when cp1252 kept nothing.
    assert _decode_context_bytes(b"\x81") == "\x81"


def test_trimws_strips_only_the_four_r_whitespace_characters():
    assert _r_trimws(" \t\r\nx\n\r\t ") == "x"
    assert _r_trimws(" x ") == " x "
    assert _r_trimws("\fx\v") == "\fx\v"


def test_chunks_overlap_and_keep_their_numbers():
    text = "a" * 2200
    chunks = _chunk_context_text(text, "doc.txt")
    # Starts every 2000 characters for as long as one lies inside the text:
    # 2200 characters give two chunks, the second the last 200 characters.
    assert [chunk["chunk_id"] for chunk in chunks] == ["doc.txt#1", "doc.txt#2"]
    assert [len(chunk["text"]) for chunk in chunks] == [2200, 200]
    assert _chunk_context_text("a" * 2000, "doc.txt")[-1]["chunk_id"] == "doc.txt#1"
    # An all-whitespace window is dropped but keeps its number.
    gappy = "a" * 400 + " " * 400 + "b" * 10
    assert [
        chunk["chunk_id"]
        for chunk in _chunk_context_text(gappy, "g.txt", chunk_chars=400, overlap_chars=0)
    ] == ["g.txt#1", "g.txt#3"]
    # Whitespace inside a chunk is never collapsed.
    assert _chunk_context_text("a  b\t\tc\n\nd", "w.txt")[0]["text"] == "a  b\t\tc\n\nd"


def test_inline_snippets_are_numbered_among_the_non_empty_ones():
    chunks = load_context_chunks(context_text=["  first  ", "   ", "second"])
    assert chunks["source"].tolist() == ["inline_context", "inline_context"]
    assert chunks["chunk_id"].tolist() == [
        "inline_context[1]#1",
        "inline_context[2]#1",
    ]
    assert chunks["text"].tolist() == ["first", "second"]


def test_make_unique_counts_the_way_base_r_does():
    # make.unique(c("a", "a", "a #1", "a"), sep = " #") in R 4.5.2.
    assert _make_unique(["a", "a", "a #1", "a"], sep=" #") == ["a", "a #2", "a #1", "a #3"]


def test_tokens_are_lowercase_ascii_runs_of_three_or_more():
    assert _context_tokens("Spawner-count, fork_length_mm; café Río 42 ab") == [
        "spawner", "count", "fork", "length", "caf",
    ]
    # No camelCase split: R lowercases first, so the split never fires.
    assert _context_tokens("SpawnerCount") == ["spawnercount"]
    # The one Unicode code point whose full and simple lowercase mappings
    # differ is folded the way R's tolower() folds it.
    assert _context_tokens("İstanbul") == ["istanbul"]
    # Missing values contribute nothing, as R's "NA" never reaches three letters.
    assert _context_tokens(None, pd.NA, ["abc", None]) == ["abc"]


def test_scoring_counts_distinct_query_tokens_present_in_the_chunk():
    pool = pd.DataFrame(
        {
            "source": ["s.txt", "s.txt", "t.txt"],
            "chunk_id": ["s.txt#1", "s.txt#2", "t.txt#1"],
            "text": [
                "spawner spawner spawner count",
                "spawner count abundance estimate",
                "nothing relevant here",
            ],
        }
    )
    target = {"search_query": "spawner count", "column_label": "Spawner abundance"}
    scored = _score_context_chunks(pool, target, None, max_chunks=4)
    assert scored["chunk_id"].tolist() == ["s.txt#2", "s.txt#1", "t.txt#1"]
    assert scored["context_score"].tolist() == [3, 2, 0]
    # With nothing to score by the head of the pool comes back unscored, and
    # max_chunks is applied as given, as in R.
    unscored = _score_context_chunks(pool, {"search_query": "ab"}, None, max_chunks=2)
    assert "context_score" not in unscored
    assert unscored["chunk_id"].tolist() == ["s.txt#1", "s.txt#2"]


def test_a_bundle_unions_its_roles_picks_in_role_order():
    # .ms_semantic_bundle_context_chunks(): each role's top picks, joined in
    # role order, deduplicated on source and chunk id, cut to the limit.
    pool = pd.DataFrame(
        {
            "source": ["d.txt"] * 3,
            "chunk_id": ["d.txt#1", "d.txt#2", "d.txt#3"],
            "text": ["water temperature", "spawner count", "fork length"],
        }
    )
    targets = pd.DataFrame(
        [
            {"dictionary_role": "variable", "search_query": "spawner count"},
            {"dictionary_role": "property", "search_query": "water temperature"},
        ]
    )
    # variable picks [#2 (2), #3 (0, the shorter filler)]; property picks
    # [#1 (2), #3 (0)]. Joined in role order and deduplicated that is
    # [#2, #3, #1], and the limit keeps the first two -- so the variable
    # role's zero-score filler outranks the property role's real hit. That is
    # R's rule, quirk included; the review-packet contract (B-327) replaces it.
    picked = _relevant_context(pool, targets, suggestions=None, max_chunks=2)
    assert picked["chunk_id"].tolist() == ["d.txt#2", "d.txt#3"]
    assert list(picked.columns) == ["source", "chunk_id", "text"]
    # With room for every pick the union is complete and in role order.
    assert _relevant_context(pool, targets, None, 4)["chunk_id"].tolist() == [
        "d.txt#2", "d.txt#3", "d.txt#1",
    ]


def test_the_excerpt_limit_is_four_except_on_openrouters_free_tier():
    assert CONTEXT_EXCERPT_LIMIT == 4
    assert _context_chunk_limit({"provider": "openai", "model": "gpt-5-mini"}) == 4
    assert _context_chunk_limit({"provider": "openrouter", "model": "openrouter/free"}) == 2


# --- the R side, wherever it can run ------------------------------------------


@pytest.mark.skipif(not HAVE_R, reason="Rscript or metasalmon library not available")
def test_expected_json_is_what_the_installed_metasalmon_computes():
    env = os.environ.copy()
    env["R_LIBS"] = R_LIB_PATH
    completed = subprocess.run(
        ["Rscript", str(FIXTURE_DIR / "expected-from-r.R"), str(FIXTURE_DIR)],
        env=env,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert json.loads(completed.stdout) == _expected()
