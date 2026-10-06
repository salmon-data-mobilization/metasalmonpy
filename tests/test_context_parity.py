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

Library-specific extraction (PDF, DOCX, spreadsheets and remaining HTML parser
details) is outside the text-only pin: PARITY.md row 62. The HTML body and
script/style selection port has separate tests below (hub B-386).

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


def test_html_context_uses_body_text_outside_script_style_and_head(tmp_path):
    # R's xml2 reader produces these three lines from the same document.
    # Exercise the file-to-chunk path that packet preparation consumes, while
    # keeping the existing source label and chunk identifier in view.
    page = tmp_path / "field-guide.html"
    page.write_text(
        "<HTML><HEAD><TITLE>hidden title</TITLE>"
        "<STYLE>.hide{display:none}</STYLE></HEAD>"
        "<BODY><DIV>Visible start <SPAN>visible middle</SPAN>"
        "<SCRIPT>window.secret = 1; <STYLE>nested tag</STYLE></SCRIPT>"
        "<STYLE>.body{display:none}</STYLE> visible end</DIV></BODY></HTML>",
        encoding="utf-8",
    )

    chunks = load_context_chunks([page])

    assert _records(chunks) == [
        {
            "source": "field-guide.html",
            "chunk_id": "field-guide.html#1",
            "text": "Visible start\nvisible middle\nvisible end",
        }
    ]


@pytest.mark.parametrize(
    "markup,expected",
    [
        (
            "<p>Fragment one</p><script>hidden fragment</script>"
            "<p>Fragment two</p>",
            "Fragment one\nFragment two",
        ),
        (
            "<html><head><title>Fallback title</title>"
            "<style>hidden style</style></head></html>",
            "Fallback title",
        ),
        (
            "<html><head><title>Hidden title</title></head>"
            "<p>Implicit body</p></html>",
            "Implicit body",
        ),
        (
            "<title>Hidden title</title><p>Visible body</p>",
            "Visible body",
        ),
    ],
    ids=["fragment", "head-only-fallback", "implicit-body", "implicit-head"],
)
def test_html_context_keeps_rs_fragment_and_no_body_fallback(tmp_path, markup, expected):
    # xml2 synthesizes a body for the fragment and loose paragraph, treating
    # a preceding loose title as head text. A head-only document has no body,
    # so R falls back to its whole document.
    page = tmp_path / "fragment.htm"
    page.write_text(markup, encoding="utf-8")

    assert load_context_chunks([page])["text"].tolist() == [expected]


def test_html_context_skips_an_empty_body_despite_head_text(tmp_path):
    page = tmp_path / "empty-body.html"
    page.write_text(
        "<html><head><title>Hidden title</title></head><body></body></html>",
        encoding="utf-8",
    )

    with pytest.warns(UserWarning, match="empty context file"):
        assert load_context_chunks([page]).empty


@pytest.mark.parametrize(
    "body_markup",
    ["<p><img></p>", "<p> \n\t </p>", "<br>"],
    ids=["empty-element", "whitespace-only", "void-element"],
)
def test_html_context_skips_an_implicit_empty_body_despite_head_text(tmp_path, body_markup):
    # B435: native xml2 synthesizes an empty body for each of these documents
    # despite the omitted body tag. The title must not become evidence merely
    # because that body has no visible text. A genuinely head-only document
    # still uses the distinct fallback pinned above; no parser unification is
    # claimed by this HTML body-scope regression.
    page = tmp_path / "implicit-empty-body.html"
    page.write_text(
        "<html><head><title>Hidden</title></head>"
        + body_markup
        + "</html>",
        encoding="utf-8",
    )

    with pytest.warns(UserWarning, match="empty context file"):
        chunks = load_context_chunks([page])
    assert chunks.empty
    assert list(chunks.columns) == ["source", "chunk_id", "text"]


def test_html_context_skips_an_empty_body_when_optional_head_end_is_omitted(tmp_path):
    # The body paragraph implicitly closes head in the unchanged native reader.
    # Even with the optional head end tag omitted, no title becomes evidence.
    page = tmp_path / "optional-head-close-empty.html"
    page.write_text(
        "<html><head><title>Hidden title</title><p></p></html>",
        encoding="utf-8",
    )

    with pytest.warns(UserWarning, match="empty context file"):
        chunks = load_context_chunks([page])
    assert chunks.empty
    assert list(chunks.columns) == ["source", "chunk_id", "text"]


def test_html_context_keeps_the_no_body_fallback_for_a_frameset(tmp_path):
    # Frameset/frame markup is not an implicit body in the unchanged native
    # reader. The original merged Python reader also kept this title fallback.
    page = tmp_path / "frameset.html"
    page.write_text(
        "<html><head><title>Hidden title</title></head>"
        '<frameset><frame src="about:blank"></frameset></html>',
        encoding="utf-8",
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        chunks = load_context_chunks([page])
    assert _records(chunks) == [
        {"source": "frameset.html", "chunk_id": "frameset.html#1", "text": "Hidden title"}
    ]
    assert not caught


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


# B435 fourth actual review: keep contextual head and no-body fallback.
# These fixtures were checked against the unchanged native reader before
# selecting expected behavior. Only the last noframes-text assertion pins
# existing Python compatibility rather than asserting parser equivalence.
@pytest.mark.parametrize(
    "markup",
    [
        '<html><head><title>Fallback title</title><noscript><link rel="stylesheet" href="test.css"></noscript></head></html>',
        '<html><head><title>Fallback title</title><object data="test.svg"></object></head></html>',
        '<html><head><title>Fallback title</title><object><param name="x" value="y"></object></head></html>',
        '<html><head><title>Fallback title</title><template></template></head></html>',
        '<html><head><title>Fallback title</title></head><frameset><frame src="about:blank"><noframes></noframes></frameset></html>',
        '<html><head><title>Fallback title</title><param name="x" value="y"></head></html>',
        '<html><head><title>Fallback title</title><object><p></p></object></head></html>',
        '<html><head><title>Fallback title</title><template><p></p></template></head></html>',
        '<html><head><title>Fallback title</title><noscript><p></p></noscript></head></html>',
        '<html><head><title>Fallback title</title></head><noframes></noframes></html>',
        '<html><head><title>Fallback title</title><custom-empty></custom-empty></head></html>',
    ],
    ids=["head-noscript-link", "head-object", "head-object-param", "head-template",
         "frameset-noframes", "head-param", "head-object-child", "head-template-child",
         "head-noscript-child", "outside-head-noframes", "head-custom-empty"],
)
def test_html_context_fourth_review_preserves_native_no_body_fallback(tmp_path, markup):
    page = tmp_path / "no-body-context.html"
    original = markup.encode("utf-8")
    page.write_bytes(original)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        pool = load_context_chunks([page])
    assert _records(pool) == [{"source": page.name, "chunk_id": page.name + "#1",
                               "text": "Fallback title"}]
    assert not caught
    assert page.read_bytes() == original


@pytest.mark.parametrize(
    "markup",
    [
        '<html><head><title>Hidden title</title></head><object></object></html>',
        '<html><head><title>Hidden title</title></head><template></template></html>',
        '<html><head><title>Hidden title</title></head><noscript></noscript></html>',
        '<html><head><title>Hidden title</title></head><param name="x" value="y"></html>',
        '<html><head><title>Hidden title</title><object></object><p></p></html>',
        '<html><head><title>Hidden title</title><template></template><p></p></html>',
        '<html><head><title>Hidden title</title><noscript></noscript><p></p></html>',
        '<html><head><title>Hidden title</title></head><custom-empty></custom-empty></html>',
    ],
    ids=["outside-object", "outside-template", "outside-noscript", "outside-param",
         "object-then-implicit-body", "template-then-implicit-body", "noscript-then-implicit-body",
         "outside-custom-empty"],
)
def test_html_context_fourth_review_retains_real_empty_body_selection(tmp_path, markup):
    page = tmp_path / "real-empty-body.html"
    original = markup.encode("utf-8")
    page.write_bytes(original)
    with pytest.warns(UserWarning, match="empty context file"):
        pool = load_context_chunks([page])
    assert pool.empty
    assert list(pool.columns) == ["source", "chunk_id", "text"]
    assert page.read_bytes() == original


@pytest.mark.parametrize("tag", ["object", "template", "noscript"])
@pytest.mark.parametrize("explicit_body", [False, True], ids=["implicit-body", "explicit-body"])
def test_html_context_fourth_review_retains_visible_body_text(tmp_path, tag, explicit_body):
    if explicit_body:
        markup = ('<html><head><title>Hidden title</title></head><body><'
                  + tag + '>Visible body</' + tag + '></body></html>')
    else:
        markup = ('<html><head><title>Hidden title</title><' + tag + '></' + tag
                  + '><p>Visible body</p></html>')
    page = tmp_path / "visible-body.html"
    original = markup.encode("utf-8")
    page.write_bytes(original)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        pool = load_context_chunks([page])
    assert _records(pool) == [{"source": page.name, "chunk_id": page.name + "#1",
                               "text": "Visible body"}]
    assert not caught
    assert page.read_bytes() == original


def test_html_context_fourth_review_preserves_existing_visible_noframes_text(tmp_path):
    # Native xml2 includes the title and treats some noframes markup as text;
    # that existing library distinction is not changed by the no-body repair.
    page = tmp_path / "visible-noframes.html"
    markup = ('<html><head><title>Fallback title</title></head><frameset>'
              '<frame src="about:blank"><noframes>No frame text</noframes></frameset></html>')
    original = markup.encode("utf-8")
    page.write_bytes(original)
    pool = load_context_chunks([page])
    assert _records(pool) == [{"source": page.name, "chunk_id": page.name + "#1",
                               "text": "No frame text"}]
    assert page.read_bytes() == original


# B435 fifth actual review: native source-bound head tokens do not all create
# an implicit body. These exact cases pin the observed scope, not HTML validity.
@pytest.mark.parametrize(
    "markup, expected_text",
    [
        ('<html><head><title>Fallback</title><article></article></head></html>', 'Fallback'),
        ('<html><head><title>Fallback</title></head><article></article></html>', None),
        ('<html><head><title>Fallback</title><input></head></html>', 'Fallback'),
        ('<html><head><title>Fallback</title></head><input></html>', None),
        ('<html><head><title>Fallback</title><basefont></head></html>', 'Fallback'),
        ('<html><head><title>Fallback</title></head><basefont></html>', None),
        ('<html><head><title>Fallback</title><p></p></head></html>', None),
        ('<html><head><title>Fallback</title><p></p></html>', None),
    ],
    ids=['article-head', 'article-outside-head', 'input-head', 'input-outside-head', 'basefont-head', 'basefont-outside-head', 'p-head', 'p-omitted-head-close'],
)
def test_html_context_fifth_review_preserves_head_token_body_scope(tmp_path, markup, expected_text):
    page = tmp_path / "head-token-context.html"
    original = markup.encode("utf-8")
    page.write_bytes(original)
    if expected_text is None:
        with pytest.warns(UserWarning, match="empty context file"):
            pool = load_context_chunks([page])
        assert pool.empty
        assert list(pool.columns) == ["source", "chunk_id", "text"]
    else:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            pool = load_context_chunks([page])
        assert _records(pool) == [{"source": page.name, "chunk_id": page.name + "#1",
                                   "text": expected_text}]
        assert not caught
    assert page.read_bytes() == original


@pytest.mark.parametrize(
    "case",
    json.loads((FIXTURE_DIR / "native-head-body-scope.json").read_text(encoding="utf-8"))["cases"],
    ids=lambda case: case["name"],
)
def test_html_context_fifth_review_matches_observed_native_head_scope(tmp_path, case):
    # Expected public output comes from real pinned native documents, not the
    # Python tag set. One plaintext control preserves existing Python behavior.
    page = tmp_path / "native-head-scope.html"
    original = case["markup"].encode("utf-8")
    page.write_bytes(original)
    if not case["expected_text"]:
        with pytest.warns(UserWarning, match="empty context file"):
            pool = load_context_chunks([page])
        assert pool.empty
        assert list(pool.columns) == ["source", "chunk_id", "text"]
    else:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            pool = load_context_chunks([page])
        assert _records(pool) == [{"source": page.name, "chunk_id": page.name + "#1",
                                   "text": case["expected_text"]}]
        assert not caught
    assert page.read_bytes() == original
