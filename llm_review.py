from __future__ import annotations

import hashlib
import html
import json
import os
import random
import re
import time
import warnings
import zipfile
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Optional, Sequence

import pandas as pd
import requests

from .term_search import _normalize_explicit_sources, sources_for_role
from .text_safety import redact_secrets


LLM_ASSESSMENT_COLUMNS = [
    "dataset_id",
    "table_id",
    "column_name",
    "code_value",
    "dictionary_role",
    "target_scope",
    "target_sdp_file",
    "target_sdp_field",
    "search_query",
    "llm_provider",
    "llm_model",
    "llm_decision",
    "llm_confidence",
    "llm_selected_candidate_index",
    "llm_selected_iri",
    "llm_selected_label",
    "llm_rationale",
    "llm_missing_context",
    "llm_bundle_summary",
    "llm_retry_query",
    "llm_new_term_label",
    "llm_new_term_definition",
    "llm_new_term_namespace",
    "llm_context_sources",
    "llm_exploration_used",
    "llm_exploration_queries",
    "llm_exploration_candidate_gain",
    "llm_error",
    "llm_escalated_from",
    "llm_retry_query_rejection_reason",
]

TARGET_JOIN_COLUMNS = [
    "dataset_id",
    "table_id",
    "column_name",
    "code_value",
    "dictionary_role",
    "target_scope",
    "target_sdp_file",
    "target_sdp_field",
    "search_query",
]

ALLOWED_DECISIONS = {
    "accept",
    "review",
    "retry_search",
    "request_new_term",
    "reject_shortlist",
}
AUTO_APPLY_ROLES = {"variable", "property", "entity", "unit"}
# The dictionary slots, in order — the authority every other role surface is
# checked against (mirrors .ms_semantic_bundle_slot_fields). sdp-0.3.0
# replaced the dictionary method slot with statistical_modifier_iri.
BUNDLE_SLOT_FIELDS = {
    "variable": "term_iri",
    "property": "property_iri",
    "entity": "entity_iri",
    "unit": "unit_iri",
    "constraint": "constraint_iri",
    "statistical_modifier": "statistical_modifier_iri",
}
# `method` stays a bundle role with NO slot field: codes-scope targets still
# search shared-vocabulary procedures for codes.csv term_iri, so the bundle
# payload names the role (always already_filled_or_not_requested for column
# bundles) without offering a dictionary field to write
# (mirrors .ms_semantic_bundle_roles).
BUNDLE_ROLES = tuple(BUNDLE_SLOT_FIELDS) + ("method",)


def _missing(value) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return isinstance(value, str) and not value.strip()


def _text(value, default=None):
    return default if _missing(value) else str(value).strip()


def _target_key(row) -> tuple:
    return tuple(_text(row.get(column), "") for column in TARGET_JOIN_COLUMNS)


_LOGICAL_TRUE_TOKENS = frozenset({"true", "t", "1"})
_LOGICAL_FALSE_TOKENS = frozenset({"false", "f", "0"})


def _cast_logical_cell(value, column: str) -> bool:
    """One cell of a logical assessment column, read as metasalmon reads it.

    Mirrors ``.ms_llm_cast_assessment_column()`` (``R/llm-review-adapter.R``):
    a boolean stays itself, text is trimmed and lowercased and must name a
    boolean -- ``true``/``t``/``1`` or ``false``/``f``/``0`` -- and anything
    else is refused rather than guessed. A missing cell reads as ``False``,
    which has always been this normalizer's default for the column. Before
    hub item B-362 the column went through ``.astype(bool)``, so the string
    ``"FALSE"`` -- what a persisted assessment CSV carries -- read as ``True``.
    """
    if _missing(value):
        return False
    if pd.api.types.is_bool(value):
        return bool(value)
    if pd.api.types.is_number(value):
        # as.character(1) is "1" and as.character(0) is "0"; every other
        # number renders to text that names no boolean.
        if value == 1:
            return True
        if value == 0:
            return False
    else:
        text = str(value).strip().lower()
        if text in _LOGICAL_TRUE_TOKENS:
            return True
        if text in _LOGICAL_FALSE_TOKENS:
            return False
    raise ValueError(
        f"Assessment column {column!r} contains values that cannot be "
        f"normalized to the required type: {value!r}."
    )


def normalize_assessment_rows(rows=None) -> pd.DataFrame:
    """Return assessment rows with the stable 30-column public schema."""
    frame = pd.DataFrame(rows).copy() if rows is not None else pd.DataFrame()
    defaults = {
        "llm_confidence": pd.NA,
        "llm_selected_candidate_index": pd.NA,
        "llm_exploration_used": False,
        "llm_exploration_candidate_gain": 0,
    }
    for column in LLM_ASSESSMENT_COLUMNS:
        if column not in frame:
            frame[column] = defaults.get(column, pd.NA)
    if not frame.empty:
        frame["llm_confidence"] = pd.to_numeric(
            frame["llm_confidence"], errors="coerce"
        )
        frame["llm_selected_candidate_index"] = pd.to_numeric(
            frame["llm_selected_candidate_index"], errors="coerce"
        ).astype("Int64")
        frame["llm_exploration_used"] = (
            frame["llm_exploration_used"]
            .map(lambda value: _cast_logical_cell(value, "llm_exploration_used"))
            .astype(bool)
        )
        frame["llm_exploration_candidate_gain"] = pd.to_numeric(
            frame["llm_exploration_candidate_gain"], errors="coerce"
        ).fillna(0).astype("Int64")
    return frame[LLM_ASSESSMENT_COLUMNS]


def make_source_policy(sources: Optional[Sequence[str]]) -> dict:
    if sources is None:
        return {"explicit": False, "sources": None}
    normalized = _normalize_explicit_sources(sources)
    return {"explicit": True, "sources": normalized}


def policy_sources(policy: dict, role: str) -> tuple[str, ...]:
    if policy["explicit"]:
        return tuple(policy["sources"])
    return tuple(sources_for_role(role))


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        text = data.strip()
        if text:
            self.parts.append(text)


# --- Context documents -------------------------------------------------------
#
# Everything from here to `_relevant_context()` mirrors metasalmon's
# `R/llm-semantic-helpers.R` step for step (hub queue B-364, ruled 2026-09-25
# with the S16 execplan, decision 4 and section 2.7): the same extension list,
# the same text extraction for text formats, the same 2200/200 chunking, the
# same source labels and chunk ids, the same token-overlap scoring and the same
# tie order. The two packages are about to share one review-packet file
# (B-326 / B-327), so a context document has to become the same excerpts on
# both sides. What is deliberately NOT shared is the library-specific
# extraction for PDF, DOCX, spreadsheets and HTML: PARITY.md row 62.
#
# R facts this code leans on, each measured on 2026-09-25 under R 4.5.2:
#   * `readLines()` accepts LF, CRLF and a bare CR as a line end, discards a
#     UTF-8 byte-order mark in a UTF-8 locale, and reports no trailing empty
#     line for a final newline.
#   * `trimws()` strips only space, tab, CR and LF -- never NBSP, form feed or
#     vertical tab -- so `str.strip()` is the wrong tool here.
#   * `iconv(..., sub = "")` drops the five bytes Windows-1252 leaves
#     undefined (0x81, 0x8D, 0x8F, 0x90, 0x9D) instead of failing, so the
#     latin-1 step is reached only when nothing survived cp1252.
#   * `[^a-z0-9]` in R's default regex engine is a code-point range, so every
#     non-ASCII character is a token separator, and `tolower()` applies the
#     simple case mapping.

# `.ms_supported_context_extensions()`, in its order.
SUPPORTED_CONTEXT_EXTENSIONS = (
    "md", "txt", "csv", "tsv", "json", "yaml", "yml", "rst", "r", "rmd", "qmd",
    "pdf", "htm", "html", "docx", "xls", "xlsx", "xlsm",
)
# `.ms_chunk_context_text()` defaults and `.ms_llm_context_chunk_limit()`'s
# ordinary limit.
CONTEXT_CHUNK_CHARS = 2200
CONTEXT_OVERLAP_CHARS = 200
CONTEXT_EXCERPT_LIMIT = 4
# R's `trimws()` default: `[ \t\r\n]`.
_R_WHITESPACE = " \t\r\n"


def _r_trimws(value) -> str:
    """R's ``trimws()``: strip space, tab, CR and LF only, never other whitespace."""
    return str(value).strip(_R_WHITESPACE)


def _r_file_ext(name: str) -> str:
    """``tools::file_ext()``: the alphanumeric run after the last dot, else ``""``."""
    match = re.search(r"\.([^\W_]+)$", name)
    return match.group(1) if match else ""


def _decode_context_bytes(raw: bytes) -> str:
    """``.ms_read_text_utf8()``'s decoding: UTF-8, then Windows-1252, then latin-1.

    A leading byte-order mark goes first, as ``readLines()`` discards it. The
    cp1252 step drops the five undefined bytes the way ``iconv(sub = "")``
    does, so latin-1 is reached only when cp1252 produced nothing at all.
    """
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    converted = raw.decode("cp1252", errors="ignore")
    if not converted:
        converted = raw.decode("latin-1")
    return converted


def _r_read_lines(path: Path) -> list[str]:
    """``readLines(path, warn = FALSE, encoding = "UTF-8")`` with R's fallback decoding."""
    text = _decode_context_bytes(path.read_bytes())
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _read_text_file(path: Path) -> str:
    """``.ms_read_text_utf8()``: the file's lines joined with LF, nothing collapsed."""
    return "\n".join(_r_read_lines(path))


def _read_rmarkdown(path: Path) -> str:
    """``.ms_context_text_from_rmarkdown()``: drop leading YAML front matter and
    every fence line. The fenced content itself stays."""
    lines = _r_read_lines(path)
    if not lines:
        return ""
    if _r_trimws(lines[0]) == "---":
        closing = [
            index
            for index, line in enumerate(lines[1:], start=1)
            if _r_trimws(line) == "---"
        ]
        if closing:
            lines = lines[closing[0] + 1 :]
    return "\n".join(
        line for line in lines if not _r_trimws(line).startswith("```")
    )


def _read_docx(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml").decode("utf-8", errors="replace")
    return " ".join(
        html.unescape(text)
        for text in re.findall(r"<w:t[^>]*>(.*?)</w:t>", xml, flags=re.S)
    )


def _read_context_file(path: Path) -> Optional[str]:
    """``.ms_context_text_from_file()``: the document's text, or ``None`` when it is skipped.

    Text formats go through R's own extraction above. PDF, DOCX, spreadsheet
    and HTML text is library-specific on each side (PARITY.md row 62) and only
    the shared steps -- the extension gate, the trim and the empty-file skip --
    are mirrored for them.
    """
    extension = _r_file_ext(path.name).lower()
    if extension not in SUPPORTED_CONTEXT_EXTENSIONS:
        warnings.warn(
            f"Skipping unsupported context file {path}. Supported extensions: "
            + ", ".join(SUPPORTED_CONTEXT_EXTENSIONS),
            stacklevel=2,
        )
        return None
    if extension in {"xls", "xlsx", "xlsm"}:
        workbook = pd.read_excel(path, sheet_name=None)
        text = "\n\n".join(
            f"Sheet: {name}\n{frame.to_csv(index=False)}"
            for name, frame in workbook.items()
        )
    elif extension == "pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise ImportError(
                "PDF context requires the optional pypdf dependency."
            ) from exc
        text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    elif extension in {"htm", "html"}:
        parser = _TextExtractor()
        parser.feed(_decode_context_bytes(path.read_bytes()))
        text = "\n".join(parser.parts)
    elif extension in {"rmd", "qmd"}:
        text = _read_rmarkdown(path)
    elif extension == "docx":
        text = _read_docx(path)
    else:
        text = _read_text_file(path)
    text = _r_trimws(text)
    if not text:
        warnings.warn(f"Skipping empty context file {path}.", stacklevel=2)
        return None
    return text


def _normalize_context_files(context_files) -> list[Path]:
    if context_files is None:
        return []
    if isinstance(context_files, (str, os.PathLike)):
        values = [context_files]
    elif isinstance(context_files, Sequence) and not isinstance(
        context_files, (pd.DataFrame, pd.Series)
    ):
        values = list(context_files)
    else:
        raise TypeError(
            "llm_context_files must contain local file paths, not parsed "
            "data frames, XML objects, or other in-memory objects."
        )
    paths = []
    for value in values:
        if not isinstance(value, (str, os.PathLike)):
            raise TypeError(
                "llm_context_files must contain only local file paths."
            )
        path = Path(value).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"LLM context file does not exist: {path}")
        paths.append(path)
    return paths


def validate_context_files(context_files) -> list[Path]:
    """Validate that context inputs are existing local paths."""
    return _normalize_context_files(context_files)


def _make_unique(names: list[str], sep: str) -> list[str]:
    """``base::make.unique(names, sep)``: a repeated name gets ``sep`` and the
    smallest count that is not already a name, counting on per name."""
    taken = set(names)
    seen: set[str] = set()
    counters: dict[str, int] = {}
    out = []
    for name in names:
        if name not in seen:
            seen.add(name)
            out.append(name)
            continue
        count = counters.get(name, 0)
        while True:
            count += 1
            candidate = f"{name}{sep}{count}"
            if candidate not in taken:
                break
        counters[name] = count
        taken.add(candidate)
        out.append(candidate)
    return out


def _unique_context_sources(documents: list[dict]) -> list[dict]:
    """``.ms_unique_context_sources()``: keep a unique basename as it is; give a
    colliding one its parent directory, then a ``" #n"`` suffix as the last
    resort, so two ``README.md`` files never share a chunk id."""
    if len(documents) < 2:
        return documents
    sources = [document["source"] for document in documents]
    duplicated = {source for source in sources if sources.count(source) > 1}
    if not duplicated:
        return documents
    for document in documents:
        if document["source"] not in duplicated:
            continue
        parent = os.path.basename(os.path.dirname(document["path"]))
        if parent and parent not in {".", "/"}:
            document["source"] = f"{parent}/{document['source']}"
    final = [document["source"] for document in documents]
    if len(set(final)) != len(final):
        for document, label in zip(documents, _make_unique(final, sep=" #")):
            document["source"] = label
    return documents


def _chunk_context_text(
    text,
    source: str,
    chunk_chars: int = CONTEXT_CHUNK_CHARS,
    overlap_chars: int = CONTEXT_OVERLAP_CHARS,
) -> list[dict]:
    """``.ms_chunk_context_text()``: fixed-width character windows with overlap.

    Windows start every ``chunk_chars - overlap_chars`` characters for as long
    as a start lies inside the text, so a document of exactly 2200 characters
    yields two chunks, the second being its last 200 characters. Each window
    is trimmed; an all-whitespace window is dropped but keeps its number, so
    the ids of the survivors are what R numbers them.
    """
    text = "" if text is None else str(text)
    if not _r_trimws(text):
        return []
    chunk_chars = max(400, int(chunk_chars))
    overlap_chars = max(0, min(int(overlap_chars), chunk_chars // 2))
    step = max(1, chunk_chars - overlap_chars)
    chunks = []
    for index, start in enumerate(range(0, len(text), step), start=1):
        chunk_text = _r_trimws(text[start : start + chunk_chars])
        if chunk_text:
            chunks.append(
                {
                    "source": source,
                    "chunk_id": f"{source}#{index}",
                    "text": chunk_text,
                }
            )
    return chunks


def _flatten_text(values) -> list[str]:
    """The pieces ``paste(unlist(list(...)))`` would join. A missing value is
    skipped: R renders it as ``NA``, which never survives the token filter."""
    flat = []
    for value in values:
        if isinstance(value, str):
            flat.append(value)
        elif isinstance(value, (list, tuple, pd.Series, pd.Index)):
            flat.extend(_flatten_text(value))
        elif not _missing(value):
            flat.append(str(value))
    return flat


def _context_tokens(*values) -> list[str]:
    """``.ms_context_tokens()``: lowercase ASCII runs of three or more characters.

    R lowercases before its camelCase split, so that split never fires and is
    not reproduced. ``tolower()`` applies the simple case mapping where
    ``str.lower()`` applies the full one; the only unconditional difference is
    U+0130, which ``str.lower()`` turns into ``i`` plus a combining dot -- a
    non-ASCII separator that would cut the token R keeps whole.
    """
    text = " ".join(_flatten_text(values))
    text = text.replace("İ", "i").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return [token for token in text.split(" ") if len(token) >= 3]


def _context_chunk_limit(config: dict) -> int:
    """``.ms_llm_context_chunk_limit()``: two excerpts on OpenRouter's free
    tier, four otherwise."""
    if _uses_openrouter_free(config.get("provider"), config.get("model")):
        return 2
    return CONTEXT_EXCERPT_LIMIT


def _score_context_chunks(
    chunks: pd.DataFrame,
    target,
    candidates: Optional[pd.DataFrame] = None,
    max_chunks: int = CONTEXT_EXCERPT_LIMIT,
) -> pd.DataFrame:
    """``.ms_score_context_chunks()``: the pool ranked for one target.

    The score is how many distinct query tokens -- from the target's search
    query, labels and descriptions and the candidates' labels and definitions
    -- occur in the chunk. Ties break on the shorter chunk, then the source
    label in C collation (the order metasalmon's scorer takes under hub item
    B-326), then pool order. A scored result carries ``context_score``; with
    nothing to score by, R returns the head of the pool unscored, and so does
    this.
    """
    if chunks is None or chunks.empty:
        return chunks
    labels = definitions = []
    if candidates is not None and len(candidates) > 0:
        if "label" in candidates:
            labels = candidates["label"].tolist()
        if "definition" in candidates:
            definitions = candidates["definition"].tolist()
    query_tokens = list(
        dict.fromkeys(
            _context_tokens(
                target.get("search_query"),
                target.get("target_label"),
                target.get("target_description"),
                target.get("column_label"),
                target.get("column_description"),
                labels,
                definitions,
            )
        )
    )
    if not query_tokens:
        return chunks.head(max_chunks).reset_index(drop=True)
    query_set = set(query_tokens)
    texts = [str(text) for text in chunks["text"]]
    sources = [str(source) for source in chunks["source"]]
    scores = [len(query_set.intersection(_context_tokens(text))) for text in texts]
    order = sorted(
        range(len(texts)),
        key=lambda index: (-scores[index], len(texts[index]), sources[index]),
    )
    ranked = chunks.iloc[order].copy()
    ranked["context_score"] = [scores[index] for index in order]
    return ranked.head(max(1, int(max_chunks))).reset_index(drop=True)


def load_context_chunks(
    context_files=None,
    context_text=None,
    chunk_size: int = CONTEXT_CHUNK_CHARS,
    overlap: int = CONTEXT_OVERLAP_CHARS,
) -> pd.DataFrame:
    """``.ms_collect_context_chunks()``: every context document and inline
    snippet as a pool of chunks, one row each, with metasalmon's source labels
    and chunk ids (``<source>#<n>`` for a file, ``inline_context[<i>]#<n>``
    for the i-th non-empty snippet)."""
    paths = _normalize_context_files(context_files)
    documents = []
    for path in paths:
        text = _read_context_file(path)
        if text is None:
            continue
        resolved = path.resolve()
        documents.append(
            {"path": str(resolved), "source": resolved.name, "text": text}
        )
    inline = []
    if context_text is not None:
        values = (
            [context_text]
            if isinstance(context_text, str)
            else list(context_text)
        )
        for value in values:
            if not isinstance(value, str):
                raise TypeError("llm_context_text must contain only strings.")
            value = _r_trimws(value)
            if value:
                inline.append(value)
    if not documents and not inline:
        return pd.DataFrame(columns=["source", "chunk_id", "text"])

    chunks = []
    for document in _unique_context_sources(documents):
        chunks.extend(
            _chunk_context_text(
                document["text"], document["source"], chunk_size, overlap
            )
        )
    for index, value in enumerate(inline, start=1):
        snippet_chunks = _chunk_context_text(
            value, "inline_context", chunk_size, overlap
        )
        for position, chunk in enumerate(snippet_chunks, start=1):
            chunk["chunk_id"] = f"inline_context[{index}]#{position}"
        chunks.extend(snippet_chunks)
    return pd.DataFrame(chunks, columns=["source", "chunk_id", "text"])


def _relevant_context(
    chunks: pd.DataFrame,
    targets: pd.DataFrame,
    suggestions: Optional[pd.DataFrame] = None,
    max_chunks: int = CONTEXT_EXCERPT_LIMIT,
) -> pd.DataFrame:
    """The excerpts one review unit sees, at most ``max_chunks`` of them.

    Each target is scored on its own with its own candidates
    (``.ms_prepare_context_chunks()``); a bundle's per-role picks are then
    joined in role order, deduplicated on source and chunk id, and cut to the
    limit (``.ms_semantic_bundle_context_chunks()``). A single target is the
    one-row case of the same rule.
    """
    if chunks.empty:
        return chunks
    picks = []
    for _, target in targets.iterrows():
        candidates = (
            _candidates_for_target(suggestions, target)
            if suggestions is not None
            else None
        )
        picks.append(_score_context_chunks(chunks, target, candidates, max_chunks))
    if not picks:
        return chunks.head(0).reset_index(drop=True)
    ranked = pd.concat(picks, ignore_index=True)
    ranked = ranked.drop_duplicates(subset=["source", "chunk_id"], keep="first")
    return ranked.head(max_chunks)[["source", "chunk_id", "text"]].reset_index(
        drop=True
    )


def resolve_llm_config(
    provider: str,
    model: Optional[str],
    api_key: Optional[str],
    base_url: Optional[str],
    reasoning_effort: Optional[str],
    timeout_seconds: int,
    request_fn,
) -> dict:
    provider = str(provider).lower()
    presets = {
        "openai": {
            "model": "gpt-5-mini",
            "base_url": "https://api.openai.com/v1",
            "key_env": "OPENAI_API_KEY",
        },
        "openrouter": {
            "model": "openrouter/free",
            "base_url": "https://openrouter.ai/api/v1",
            "key_env": "OPENROUTER_API_KEY",
        },
        "openai_compatible": {
            "model": None,
            "base_url": None,
            "key_env": "METASALMON_LLM_API_KEY",
        },
        "chapi": {
            "model": "ollama2.mistral:7b",
            "base_url": "https://chapi-dev.intra.azure.cloud.dfo-mpo.gc.ca/api",
            "key_env": "CHAPI_API_KEY",
        },
    }
    if provider not in presets:
        raise ValueError(
            "llm_provider must be openai, openrouter, openai_compatible, or chapi."
        )
    preset = presets[provider]
    resolved = {
        "provider": provider,
        "model": model
        or os.getenv(f"{provider.upper()}_MODEL")
        or preset["model"],
        "base_url": base_url
        or os.getenv(f"{provider.upper()}_BASE_URL")
        or preset["base_url"],
        "api_key": api_key or os.getenv(preset["key_env"]),
        "reasoning_effort": reasoning_effort,
        "timeout_seconds": timeout_seconds,
        "request_fn": request_fn,
    }
    if not resolved["model"]:
        raise ValueError("llm_model is required for this provider.")
    if request_fn is None and not resolved["api_key"]:
        raise ValueError(
            f"No API key is configured for llm_provider={provider!r}."
        )
    if request_fn is None and not resolved["base_url"]:
        raise ValueError(
            "llm_base_url is required for an OpenAI-compatible provider."
        )
    return resolved


def _clean_json_text(value: str) -> str:
    text = str(value).strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _response_data(result):
    if isinstance(result, requests.Response):
        result.raise_for_status()
        result = result.json()
    if isinstance(result, dict) and result.get("data") is not None:
        return result["data"]
    if isinstance(result, dict) and "choices" in result:
        content = (
            result.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )
        return json.loads(_clean_json_text(content))
    if isinstance(result, dict) and "content" in result and len(result) <= 3:
        content = result.get("content")
        if isinstance(content, str):
            return json.loads(_clean_json_text(content))
    return result


def request_json(messages: list[dict], config: dict):
    request_fn = config["request_fn"]
    if request_fn is not None:
        return _response_data(request_fn(messages, config))
    body = {
        "model": config["model"],
        "messages": messages,
        "response_format": {"type": "json_object"},
    }
    if config.get("reasoning_effort"):
        body["reasoning_effort"] = config["reasoning_effort"]
    response = requests.post(
        f"{str(config['base_url']).rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {config['api_key']}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=config["timeout_seconds"],
    )
    return _response_data(response)


# --- provider retry (metasalmon 0.2.3) --------------------------------------------------

# Patchable seam for tests; mirrors how R tests stub Sys.sleep.
_sleep = time.sleep


def _uses_openrouter_free(provider: object, model: object) -> bool:
    """Mirror ``.ms_llm_uses_openrouter_free``."""
    if provider != "openrouter" or model is None:
        return False
    model = str(model)
    return model == "openrouter/free" or model.endswith(":free")


def _uses_chapi_gpt_oss(provider: object, model: object) -> bool:
    """Mirror ``.ms_llm_uses_chapi_gpt_oss``."""
    if provider != "chapi" or model is None:
        return False
    return re.match(r"^gpt-oss(:|$)", str(model)) is not None


def _retry_limit(config: dict) -> int:
    """Total attempts, not retries — mirror ``.ms_llm_retry_limit``.

    The default was 1, which meant ``attempt >= attempts`` was true on the
    first pass and the retryable-error classifier below was never consulted
    for the default providers — a 429 or a 503 failed the whole review on the
    first try, after the user had already paid for every preceding request.
    """
    provider = config.get("provider")
    model = config.get("model")
    if _uses_openrouter_free(provider, model):
        return 4
    if _uses_chapi_gpt_oss(provider, model):
        return 4
    return 3


_HTTP_DATE = re.compile(
    r"^[A-Za-z]{3},\s+([0-9]{2})\s+([A-Za-z]{3})\s+([0-9]{4})"
    r"\s+([0-9]{2}):([0-9]{2}):([0-9]{2})\s+GMT$"
)
_HTTP_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


def _parse_http_date(value: str) -> Optional[datetime]:
    """An HTTP date, parsed without consulting the process locale.

    Mirrors ``.ms_parse_http_date``: only IMF-fixdate is handled, which is the
    form RFC 7231 requires senders to use; the two obsolete formats return
    ``None`` and fall back to bounded backoff. The month table is hardcoded
    English on purpose — an HTTP date always carries the English names, and a
    locale-aware parser under a non-English locale is exactly how R's original
    implementation turned a valid ``Retry-After`` into a sub-second retry.
    """
    parts = _HTTP_DATE.match(str(value))
    if parts is None:
        return None
    month = _HTTP_MONTHS.get(parts.group(2))
    if month is None:
        return None
    try:
        return datetime(
            int(parts.group(3)), month, int(parts.group(1)),
            int(parts.group(4)), int(parts.group(5)), int(parts.group(6)),
            tzinfo=timezone.utc,
        )
    except ValueError:
        return None


def _retry_after_seconds(error: BaseException) -> Optional[float]:
    """How long the server asked us to wait — mirror ``.ms_llm_retry_after_seconds``.

    The header is either delta-seconds or an HTTP-date, and both forms appear
    in the wild. Returns ``None`` when the error carries no response or no
    usable header.
    """
    response = getattr(error, "response", None)
    if response is None:
        return None
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        raw = headers.get("Retry-After")
    except Exception:
        return None
    if raw is None or not str(raw).strip():
        return None
    raw = str(raw).strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    at = _parse_http_date(raw)
    if at is None:
        return None
    return max(0.0, at.timestamp() - time.time())


def _retry_wait_seconds(error: BaseException, attempt: int, max_wait: float = 60.0) -> float:
    """Mirror ``.ms_llm_retry_wait_seconds``.

    A server that says ``Retry-After`` is telling you the rate-limit window;
    ignoring it and retrying on a fixed backoff is how a 429 becomes a ban.
    Capped: a provider asking for a multi-minute wait should fail the call so
    the caller can decide, rather than silently blocking a review for that
    long. Without the requested wait, exponential backoff with jitter — a
    batch of requests that hit the same rate limit must not retry in lockstep.
    """
    requested = _retry_after_seconds(error)
    if requested is not None:
        return min(requested, max_wait)
    backoff = min(max_wait, 0.5 * (2 ** (attempt - 1)))
    return backoff + random.uniform(0.0, backoff / 2.0)


_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})

# Mirror of `.ms_llm_is_retryable_error`'s pattern list, applied to the
# message of any error an injected request_fn raises.
_RETRYABLE_MESSAGE_PATTERNS = (
    "timeout was reached",
    "timed out",
    "http 408",
    "http 429",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "temporarily unavailable",
    "connection reset",
    "empty reply",
    "failed to perform http request",
)


def _is_retryable_error(error: BaseException) -> bool:
    """Mirror ``.ms_llm_is_retryable_error`` — the same retryable set.

    R classifies by message substring because httr2 spells the status into the
    condition message ("HTTP 429 Too Many Requests"); requests spells it
    differently ("429 Client Error: ..."), so the structured checks on the
    attached response and the transport exception types express the identical
    rule over this library's error shapes. The message patterns remain for
    errors raised by injected ``request_fn`` hooks, where R and Python see the
    same author-written text.
    """
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None) if response is not None else None
    if isinstance(status, int) and status in _RETRYABLE_STATUS:
        return True
    if isinstance(error, (requests.Timeout, requests.ConnectionError)):
        return True
    message = str(error).lower()
    return any(pattern in message for pattern in _RETRYABLE_MESSAGE_PATTERNS)


def _request_json_with_retries(messages: list[dict], config: dict):
    """Mirror ``.ms_llm_request_with_retries``.

    A non-retryable error (including an injected sentinel ``request_fn`` that
    always raises) is re-raised from the first attempt, so "the LLM was not
    called" test hooks still prove exactly one call.
    """
    attempts = _retry_limit(config)
    last_error: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        try:
            return request_json(messages, config)
        except Exception as exc:  # noqa: BLE001 - classifier decides below
            last_error = exc
            if attempt >= attempts or not _is_retryable_error(exc):
                raise
            wait = _retry_wait_seconds(exc, attempt)
            if wait > 0:
                _sleep(wait)
    raise last_error  # pragma: no cover - loop always returns or raises


def _candidate_id(row, role: str, position: int) -> str:
    source = _text(row.get("source"), "unknown")
    iri = _text(row.get("iri"))
    if iri:
        return f"{source}::{iri}"
    evidence = "|".join(
        [
            role,
            source,
            _text(row.get("ontology"), ""),
            _text(row.get("label"), ""),
            _text(row.get("definition"), ""),
        ]
    )
    return f"{source}::blank::{hashlib.sha256(evidence.encode()).hexdigest()[:16]}"


def _candidate_payload(candidates: pd.DataFrame, role: str) -> list[dict]:
    rows = []
    for position, (_, row) in enumerate(candidates.iterrows(), start=1):
        rows.append(
            {
                "candidate_id": _candidate_id(row, role, position),
                "index": position,
                "label": _text(row.get("label")),
                "iri": _text(row.get("iri")),
                "source": _text(row.get("source")),
                "ontology": _text(row.get("ontology")),
                "ontology_type": _text(
                    row.get("ontology_type"),
                    _text(row.get("term_type"), _text(row.get("role"))),
                ),
                "definition": _text(row.get("definition")),
                "role_hints": _text(row.get("role_hints")),
                "score": (
                    float(row.get("score"))
                    if not _missing(row.get("score"))
                    else None
                ),
            }
        )
    return rows


def _candidates_for_target(
    suggestions: pd.DataFrame,
    target,
) -> pd.DataFrame:
    if suggestions.empty:
        return suggestions.copy()
    key = _target_key(target)
    mask = suggestions.apply(lambda row: _target_key(row) == key, axis=1)
    return suggestions.loc[mask].reset_index(drop=True)


def _base_assessment(target, config: dict, context: pd.DataFrame) -> dict:
    return {
        **{
            column: target.get(column, pd.NA)
            for column in TARGET_JOIN_COLUMNS
            if column != "search_query"
        },
        "search_query": target.get("search_query", pd.NA),
        "llm_provider": config["provider"],
        "llm_model": config["model"],
        "llm_decision": pd.NA,
        "llm_confidence": pd.NA,
        "llm_selected_candidate_index": pd.NA,
        "llm_selected_iri": pd.NA,
        "llm_selected_label": pd.NA,
        "llm_rationale": pd.NA,
        "llm_missing_context": pd.NA,
        "llm_bundle_summary": pd.NA,
        "llm_retry_query": pd.NA,
        "llm_new_term_label": pd.NA,
        "llm_new_term_definition": pd.NA,
        "llm_new_term_namespace": pd.NA,
        "llm_context_sources": (
            "; ".join(dict.fromkeys(context["source"]))
            if not context.empty
            else pd.NA
        ),
        "llm_exploration_used": False,
        "llm_exploration_queries": pd.NA,
        "llm_exploration_candidate_gain": 0,
        "llm_error": pd.NA,
        "llm_escalated_from": pd.NA,
        "llm_retry_query_rejection_reason": pd.NA,
    }


def _validate_item(item, candidates: pd.DataFrame, role: str) -> dict:
    if not isinstance(item, dict):
        raise ValueError("Assessment item must be a JSON object.")
    decision = str(item.get("decision") or "").strip().lower()
    if decision == "propose_new_term":
        decision = "request_new_term"
    if decision not in ALLOWED_DECISIONS:
        raise ValueError(f"Unsupported LLM decision for {role}: {decision!r}.")

    payload = _candidate_payload(candidates, role)
    selected_index = item.get("selected_candidate_index")
    selected_id = _text(item.get("selected_candidate_id"))
    if selected_id:
        matching = [
            candidate["index"]
            for candidate in payload
            if candidate["candidate_id"] == selected_id
        ]
        if len(matching) != 1:
            raise ValueError(
                f"Unknown selected_candidate_id for role {role}: {selected_id}"
            )
        selected_index = matching[0]
    if selected_index is not None and not _missing(selected_index):
        try:
            selected_index = int(selected_index)
        except (TypeError, ValueError) as exc:
            raise ValueError("selected_candidate_index must be an integer.") from exc
        if selected_index < 1 or selected_index > len(candidates):
            decision = "review"
            selected_index = None
    else:
        selected_index = None
    if decision == "accept" and selected_index is None:
        decision = "review"

    confidence = item.get("confidence")
    try:
        confidence = float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        confidence = None
    if confidence is not None:
        confidence = max(0.0, min(1.0, confidence))
    return {
        "decision": decision,
        "confidence": confidence,
        "selected_index": selected_index,
        "rationale": _text(item.get("rationale")),
        "missing_context": _text(item.get("missing_context")),
        "retry_query": _text(item.get("retry_query")),
        "new_term_label": _text(
            item.get("suggested_label"), _text(item.get("new_term_label"))
        ),
        "new_term_definition": _text(
            item.get("suggested_definition"),
            _text(item.get("new_term_definition")),
        ),
        "new_term_namespace": _text(
            item.get("suggested_namespace"),
            _text(item.get("new_term_namespace")),
        ),
    }


def _assessment_from_item(
    target,
    candidates: pd.DataFrame,
    item,
    config: dict,
    context: pd.DataFrame,
    bundle_summary=None,
) -> dict:
    validated = _validate_item(item, candidates, str(target["dictionary_role"]))
    row = _base_assessment(target, config, context)
    selected = validated["selected_index"]
    row.update(
        {
            "llm_decision": validated["decision"],
            "llm_confidence": validated["confidence"],
            "llm_selected_candidate_index": selected,
            "llm_selected_iri": (
                candidates.iloc[selected - 1].get("iri")
                if selected is not None
                else pd.NA
            ),
            "llm_selected_label": (
                candidates.iloc[selected - 1].get("label")
                if selected is not None
                else pd.NA
            ),
            "llm_rationale": validated["rationale"],
            "llm_missing_context": validated["missing_context"],
            "llm_bundle_summary": bundle_summary,
            "llm_retry_query": validated["retry_query"],
            "llm_new_term_label": validated["new_term_label"],
            "llm_new_term_definition": validated["new_term_definition"],
            "llm_new_term_namespace": validated["new_term_namespace"],
        }
    )
    return row


def _error_assessment(target, error, config, context) -> dict:
    row = _base_assessment(target, config, context)
    # Redacted where the provider's text is CAPTURED, not where it is shown.
    # This row is returned on the exported ``semantic_llm_assessments``
    # attribute and written to CSV, so display-time redaction would be too
    # late — metasalmon 0.2.0 made the same correction on its bundle-review
    # path for the same reason.
    row["llm_error"] = redact_secrets(error)
    return row


def _target_payload(target, candidates, context) -> dict:
    role = str(target["dictionary_role"])
    return {
        "target": {
            column: None if _missing(target.get(column)) else target.get(column)
            for column in target.index
        },
        "candidates": _candidate_payload(candidates, role),
        "context": context.to_dict("records"),
    }


def _generic_messages(target, candidates, context) -> list[dict]:
    payload = _target_payload(target, candidates, context)
    return [
        {
            "role": "system",
            "content": (
                "Judge only the supplied ontology candidates. Return JSON with "
                "decision, confidence, selected_candidate_index, rationale, "
                "missing_context, retry_query, suggested_label, "
                "suggested_definition, and suggested_namespace. decision must "
                "be accept, review, retry_search, request_new_term, or "
                "reject_shortlist. Never invent an IRI."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(payload, default=str, ensure_ascii=True),
        },
    ]


def _bundle_payload(
    targets: pd.DataFrame,
    suggestions: pd.DataFrame,
    context: pd.DataFrame,
    source_policy: dict,
    dictionary: pd.DataFrame,
) -> dict:
    first = targets.iloc[0]
    dictionary_row = _dictionary_row(first, dictionary)
    target_by_role = {
        str(target["dictionary_role"]): target
        for _, target in targets.iterrows()
    }
    current_slots = {
        role: (
            None
            if _missing(dictionary_row.get(field))
            else dictionary_row.get(field)
        )
        for role, field in BUNDLE_SLOT_FIELDS.items()
    }
    slots = []
    for role in BUNDLE_ROLES:
        field = BUNDLE_SLOT_FIELDS.get(role)
        target = target_by_role.get(role)
        if target is None:
            slots.append(
                {
                    "role": role,
                    # `method` has no dictionary slot field (sdp-0.3.0 dropped
                    # method_iri; the role survives only for codes-scope
                    # searches), so an unrequested role may have no field.
                    "target_sdp_field": field,
                    "status": "already_filled_or_not_requested",
                    "current_value": current_slots.get(role),
                    "candidates": [],
                }
            )
            continue
        candidates = _candidates_for_target(suggestions, target)
        slots.append(
            {
                "role": role,
                "status": "review",
                "current_value": current_slots.get(role),
                "search_query": target.get("search_query"),
                "target_sdp_field": target.get("target_sdp_field"),
                "candidates": _candidate_payload(candidates, role),
            }
        )
    dictionary_context = {
        column: (
            None
            if _missing(dictionary_row.get(column))
            else dictionary_row.get(column)
        )
        for column in (
            "dataset_id",
            "table_id",
            "column_name",
            "column_label",
            "column_description",
            "column_role",
            "value_type",
            "unit_label",
            "term_type",
        )
    }
    return {
        "bundle": {
            "dataset_id": first.get("dataset_id"),
            "table_id": first.get("table_id"),
            "column_name": first.get("column_name"),
            "column_label": first.get("column_label"),
            "column_description": first.get("column_description"),
            "unit_label": first.get("unit_label"),
        },
        "dictionary_context": dictionary_context,
        "current_slots": current_slots,
        "source_policy": {
            "mode": "strict_allowlist"
            if source_policy["explicit"]
            else "role_aware_defaults",
            "sources": list(source_policy["sources"] or ()),
        },
        "slots": slots,
        "context": context.to_dict("records"),
    }


def _bundle_messages(payload) -> list[dict]:
    # The opening "Judge ..." instruction is a role-contract surface: it must
    # name exactly the dictionary slots (mirrors
    # .ms_semantic_bundle_system_prompt, which sdp-0.3.0 shipped still naming
    # the removed `method` slot — the defect the role-contract guard pins).
    return [
        {
            "role": "system",
            "content": (
                "Review this measurement as one I-ADOPT semantic bundle. "
                "Judge variable, property, entity, unit, constraint, and "
                "statistical_modifier together before finalizing any slot. "
                "A method is never a dictionary slot: procedures are "
                "usedProcedure-style context recorded on tables.csv or "
                "resolved through codes.csv, so method targets appear only "
                "for code values. "
                "statistical_modifier is part of variable identity (I-ADOPT "
                "StatisticalModifier); accept one only when the column is an "
                "aggregation or summary such as a mean, maximum, total, or "
                "peak. "
                "Return JSON with bundle_summary and exactly one item in slots "
                "for each supplied slot whose status is review. Do not assess "
                "slots already marked as filled. Each item uses role, decision, "
                "confidence, selected_candidate_id, rationale, missing_context, "
                "retry_query, suggested_label, suggested_definition, and "
                "suggested_namespace. Candidate ontology types are native and "
                "must not be rewritten. Never invent an IRI."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(payload, default=str, ensure_ascii=True),
        },
    ]


def _assess_generic(target, candidates, context, config) -> dict:
    try:
        result = _request_json_with_retries(
            _generic_messages(target, candidates, context),
            config,
        )
        return _assessment_from_item(
            target,
            candidates,
            result,
            config,
            context,
        )
    except Exception as exc:
        return _error_assessment(target, exc, config, context)


def _assess_bundle(
    targets,
    suggestions,
    context,
    config,
    source_policy,
    dictionary,
) -> list[dict]:
    try:
        result = _request_json_with_retries(
            _bundle_messages(
                _bundle_payload(
                    targets,
                    suggestions,
                    context,
                    source_policy,
                    dictionary,
                )
            ),
            config,
        )
    except Exception as exc:
        return [
            _error_assessment(target, exc, config, context)
            for _, target in targets.iterrows()
        ]
    if not isinstance(result, dict) or not isinstance(
        result.get("slots"), list
    ):
        return [
            _assess_generic(
                target,
                _candidates_for_target(suggestions, target),
                context,
                config,
            )
            for _, target in targets.iterrows()
        ]

    items_by_role = {}
    duplicated = set()
    for item in result["slots"]:
        role = _text(item.get("role")) if isinstance(item, dict) else None
        if role in items_by_role:
            duplicated.add(role)
        elif role:
            items_by_role[role] = item

    rows = []
    for _, target in targets.iterrows():
        role = str(target["dictionary_role"])
        candidates = _candidates_for_target(suggestions, target)
        item = items_by_role.get(role)
        if item is None or role in duplicated:
            rows.append(_assess_generic(target, candidates, context, config))
            continue
        try:
            rows.append(
                _assessment_from_item(
                    target,
                    candidates,
                    item,
                    config,
                    context,
                    bundle_summary=_text(result.get("bundle_summary")),
                )
            )
        except Exception:
            rows.append(_assess_generic(target, candidates, context, config))
    return rows


# --- the retry-query classifier ---------------------------------------------
#
# Ported from metasalmon's ``.ms_llm_normalize_query_text()``,
# ``.ms_llm_query_looks_like_identifier()`` and
# ``.ms_llm_classify_retry_query()`` (``R/llm-semantic-helpers.R``) for hub
# item B-362, so that a retry query written into the shared review record gets
# one verdict in both packages. The verdicts are pinned against R's own in
# ``tests/test_retry_query_classifier.py``. The one place this departs from
# *current* R on purpose is the case fold, which R moves to under B-361 (5).

# ``trimws()``'s default class: space, tab, CR and LF, and nothing else.
_R_TRIMWS_CHARS = " \t\r\n"

# What R's ``\s`` collapses, measured character by character under R 4.5.2 in
# a UTF-8 locale (metasalmon main @ 98cb9e6): the ASCII whitespace plus the
# Unicode spaces ``iswspace()`` accepts. Python's ``\s`` is wider -- it also
# swallows U+001C-U+001F, NEL (U+0085) and the no-break spaces U+00A0, U+2007
# and U+202F -- so the class is written out rather than borrowed.
_R_WHITESPACE_RUN = re.compile(
    "[\\t\\n\\x0b\\x0c\\r \\u1680\\u2000-\\u2006\\u2008-\\u200a"
    "\\u2028\\u2029\\u205f\\u3000]+"
)

# R's ``tolower()`` folds non-ASCII letters by locale, so the same pair of
# queries can be a duplicate in one locale and not in another. The ruled
# duplicate check folds ASCII letters only, the same everywhere (S16 execplan,
# decisions 11 and 12); metasalmon adopts it under B-361 point (5).
_ASCII_LOWER = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"
)

_IDENTIFIER_SCHEME_PREFIX = re.compile(r"(?:https?://|urn:|doi:)", re.IGNORECASE)
# R's second alternative is ``^[A-Za-z][A-Za-z0-9._+-]*:[^\\s]+$``, compiled
# by TRE, whose bracket expressions have no escapes: ``[^\s]`` there means
# "neither a backslash nor the letter s", not "non-whitespace". So after the
# colon R wants one or more characters other than ``\`` and ``s``; a space is
# allowed and ``smn:species`` is not identifier-like. Measured, not read
# (``abc:d e`` is TRUE, ``abc:s`` is FALSE, ``abc:S`` is TRUE), and reproduced
# here on purpose, because the review record needs one verdict in both
# packages. *Retires when* metasalmon rewrites that class as ``[^[:space:]]``
# or compiles it with ``perl = TRUE``: this class then becomes ``\S`` in the
# same stream, and the R-computed fixture flips with it.
_IDENTIFIER_CURIE = re.compile(r"[A-Za-z][A-Za-z0-9._+-]*:[^\\s]+")


def _first_scalar(value):
    if isinstance(value, pd.Series):
        return _first_scalar(value.iloc[0]) if len(value) else None
    if isinstance(value, (list, tuple)):
        return _first_scalar(value[0]) if value else None
    return value


def _non_empty_string(value) -> Optional[str]:
    """``.ms_llm_non_empty_string()``: the first scalar, trimmed as R trims."""
    value = _first_scalar(value)
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    text = str(value).strip(_R_TRIMWS_CHARS)
    return text if text else None


def _normalize_query_text(value) -> Optional[str]:
    """Collapse whitespace runs to one space and trim, as R does."""
    text = _non_empty_string(value)
    if text is None:
        return None
    return _R_WHITESPACE_RUN.sub(" ", text).strip(_R_TRIMWS_CHARS)


def _query_looks_like_identifier(value) -> bool:
    text = _normalize_query_text(value)
    if text is None:
        return False
    return bool(
        _IDENTIFIER_SCHEME_PREFIX.match(text) or _IDENTIFIER_CURIE.fullmatch(text)
    )


def _classify_retry_query(retry_query, original_query) -> dict:
    """Classify a model's retry query the way metasalmon does.

    Returns the four members of ``.ms_llm_classify_retry_query()``'s list:
    ``query`` and ``original_query`` (both normalized; ``None`` when empty),
    ``disposition`` -- ``invalid``, ``duplicate_original_query``,
    ``identifier_like`` or ``use_query`` -- and ``rejection_reason``, set only
    for a duplicate.
    """
    retry = _normalize_query_text(retry_query)
    original = _normalize_query_text(original_query)
    if retry is None:
        disposition = "invalid"
    elif original is not None and retry.translate(_ASCII_LOWER) == original.translate(
        _ASCII_LOWER
    ):
        disposition = "duplicate_original_query"
    elif _query_looks_like_identifier(retry):
        disposition = "identifier_like"
    else:
        disposition = "use_query"
    return {
        "query": retry,
        "original_query": original,
        "disposition": disposition,
        "rejection_reason": (
            "duplicate_original_query"
            if disposition == "duplicate_original_query"
            else None
        ),
    }


def _generated_retry_query(target, row, config) -> Optional[str]:
    messages = [
        {
            "role": "system",
            "content": (
                "Return JSON with alternate_queries containing one short "
                "plain-language ontology search phrase. Do not return an IRI, "
                "CURIE, DOI, or the original query."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "dictionary_role": target.get("dictionary_role"),
                    "target_label": target.get("target_label"),
                    "target_description": target.get("target_description"),
                    "original_query": target.get("search_query"),
                    "rejected_retry_query": row.get("llm_retry_query"),
                    "rationale": row.get("llm_rationale"),
                },
                default=str,
            ),
        },
    ]
    try:
        result = _request_json_with_retries(messages, config)
    except Exception:
        return None
    if not isinstance(result, dict):
        return None
    values = (
        result.get("alternate_queries")
        or result.get("queries")
        or result.get("suggested_queries")
        or []
    )
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return None
    for value in values:
        classification = _classify_retry_query(value, target.get("search_query"))
        if classification["disposition"] == "use_query" and classification["query"]:
            return classification["query"]
    return None


def _retry_candidates(
    target,
    query: str,
    search_fn: Callable,
    source_policy: dict,
    max_per_role: int,
) -> pd.DataFrame:
    role = str(target["dictionary_role"])
    result = search_fn(
        query,
        role=role,
        sources=policy_sources(source_policy, role),
    )
    if result is None or result.empty:
        return pd.DataFrame()
    result = result.copy()
    result["retrieval_query"] = query
    result["retrieval_pass"] = 2
    for column, value in target.items():
        result[column] = value
    result["search_query"] = target["search_query"]
    dedupe = [column for column in ("source", "iri", "label") if column in result]
    if dedupe:
        result = result.drop_duplicates(dedupe)
    if "score" in result:
        result["score"] = pd.to_numeric(result["score"], errors="coerce")
        result = result.sort_values("score", ascending=False, na_position="last")
    return result.head(max_per_role)


def _merge_retry_candidates(
    suggestions,
    retry_rows,
    target,
    max_per_role,
) -> tuple[pd.DataFrame, int]:
    before = _candidates_for_target(suggestions, target)
    before_ids = {
        _candidate_id(row, str(target["dictionary_role"]), position)
        for position, (_, row) in enumerate(before.iterrows(), start=1)
    }
    combined = pd.concat([suggestions, retry_rows], ignore_index=True, sort=False)
    key_mask = combined.apply(lambda row: _target_key(row) == _target_key(target), axis=1)
    target_rows = combined.loc[key_mask].copy()
    other_rows = combined.loc[~key_mask].copy()
    dedupe = [
        column for column in ("source", "iri", "label") if column in target_rows
    ]
    if dedupe:
        target_rows = target_rows.drop_duplicates(dedupe, keep="first")
    if "score" in target_rows:
        target_rows["score"] = pd.to_numeric(
            target_rows["score"], errors="coerce"
        )
        target_rows = target_rows.sort_values(
            "score", ascending=False, na_position="last"
        )
    target_rows = target_rows.head(max_per_role)
    after_ids = {
        _candidate_id(row, str(target["dictionary_role"]), position)
        for position, (_, row) in enumerate(target_rows.iterrows(), start=1)
    }
    gain = len(after_ids - before_ids)
    return (
        pd.concat([other_rows, target_rows], ignore_index=True, sort=False),
        gain,
    )


def _restore_target_candidates(current, original, target) -> pd.DataFrame:
    current_mask = current.apply(
        lambda row: _target_key(row) == _target_key(target),
        axis=1,
    )
    original_rows = _candidates_for_target(original, target)
    return pd.concat(
        [current.loc[~current_mask], original_rows],
        ignore_index=True,
        sort=False,
    )


def _reject_duplicate_retry(row) -> None:
    row["llm_retry_query_rejection_reason"] = "duplicate_original_query"
    note = (
        "Retry query matched the original query after case and whitespace "
        "normalization; the retry was not issued."
    )
    rationale = _text(row.get("llm_rationale"), "")
    row["llm_rationale"] = f"{rationale} {note}".strip()


def _escalate_reject_shortlist(row, initial=None) -> None:
    if _text(row.get("llm_decision")) != "reject_shortlist":
        return
    before = _text((initial or {}).get("llm_rationale"), "")
    after = _text(row.get("llm_rationale"), "")
    parts = []
    if before:
        parts.append(f"Initial shortlist rejection: {before}")
    if after and after != before:
        parts.append(f"Post-retry shortlist rejection: {after}")
    if not parts and after:
        parts.append(f"Shortlist rejection: {after}")
    parts.append(
        "Shortlist rejected and exploration found no acceptable candidate; "
        "escalated to request_new_term so the likely ontology gap is surfaced."
    )
    row["llm_decision"] = "request_new_term"
    row["llm_escalated_from"] = "reject_shortlist"
    row["llm_rationale"] = " ".join(parts)


def _apply_bundle_retry(
    targets,
    suggestions,
    rows,
    context,
    config,
    source_policy,
    dictionary,
    search_fn,
    max_per_role,
) -> tuple[pd.DataFrame, list[dict]]:
    original_suggestions = suggestions.copy()
    valid_retry = False
    gains = {}
    retry_queries = {}
    initial_rows = {
        str(row["dictionary_role"]): row for row in rows
    }
    for row in rows:
        if _text(row.get("llm_decision")) != "retry_search":
            continue
        target = targets.loc[
            targets.apply(lambda value: _target_key(value) == _target_key(row), axis=1)
        ].iloc[0]
        classification = _classify_retry_query(
            row.get("llm_retry_query"),
            target.get("search_query"),
        )
        disposition = classification["disposition"]
        if disposition == "duplicate_original_query":
            _reject_duplicate_retry(row)
            continue
        if disposition == "identifier_like":
            query = _generated_retry_query(target, row, config)
            if query is None:
                continue
        elif disposition != "use_query":
            continue
        else:
            query = classification["query"]
        role = str(target["dictionary_role"])
        gains[role] = 0
        retry_queries[role] = query
        row["llm_exploration_used"] = True
        row["llm_exploration_queries"] = query
        row["llm_exploration_candidate_gain"] = 0
        retry = _retry_candidates(
            target,
            query,
            search_fn,
            source_policy,
            max_per_role,
        )
        if retry.empty:
            continue
        merged_suggestions, gain = _merge_retry_candidates(
            suggestions,
            retry,
            target,
            max_per_role,
        )
        gains[role] = gain
        row["llm_exploration_candidate_gain"] = gain
        if gain > 0:
            suggestions = merged_suggestions
        valid_retry = valid_retry or gain > 0

    if valid_retry:
        reassessed = _assess_bundle(
            targets,
            suggestions,
            context,
            config,
            source_policy,
            dictionary,
        )
        reassessed_by_role = {
            str(row["dictionary_role"]): row for row in reassessed
        }
        rows = []
        for role, original in initial_rows.items():
            replacement = reassessed_by_role.get(role)
            if (
                role in gains
                and gains[role] > 0
                and replacement is not None
                and _missing(replacement.get("llm_error"))
            ):
                replacement["llm_exploration_used"] = True
                replacement["llm_exploration_queries"] = retry_queries[role]
                replacement["llm_exploration_candidate_gain"] = gains[role]
                rows.append(replacement)
            else:
                rows.append(original)
                if role in gains and gains[role] > 0:
                    target = targets.loc[
                        targets["dictionary_role"].astype(str) == role
                    ].iloc[0]
                    suggestions = _restore_target_candidates(
                        suggestions,
                        original_suggestions,
                        target,
                    )

    for row in rows:
        role = str(row["dictionary_role"])
        initial = initial_rows.get(role, {})
        _escalate_reject_shortlist(row, initial)
    return suggestions, rows


def _apply_generic_retry(
    target,
    suggestions,
    row,
    context,
    config,
    source_policy,
    search_fn,
    max_per_role,
) -> tuple[pd.DataFrame, dict]:
    if _text(row.get("llm_decision")) != "retry_search":
        return suggestions, row
    classification = _classify_retry_query(
        row.get("llm_retry_query"),
        target.get("search_query"),
    )
    disposition = classification["disposition"]
    if disposition == "duplicate_original_query":
        _reject_duplicate_retry(row)
        return suggestions, row
    if disposition == "identifier_like":
        query = _generated_retry_query(target, row, config)
        if query is None:
            return suggestions, row
    elif disposition != "use_query":
        return suggestions, row
    else:
        query = classification["query"]

    extra = _retry_candidates(
        target,
        query,
        search_fn,
        source_policy,
        max_per_role,
    )
    row["llm_exploration_used"] = True
    row["llm_exploration_queries"] = query
    if extra.empty:
        row["llm_exploration_candidate_gain"] = 0
        return suggestions, row

    merged, gain = _merge_retry_candidates(
        suggestions,
        extra,
        target,
        max_per_role,
    )
    row["llm_exploration_candidate_gain"] = gain
    if gain <= 0:
        return suggestions, row

    candidates = _candidates_for_target(merged, target)
    reassessed = _assess_generic(target, candidates, context, config)
    if not _missing(reassessed.get("llm_error")):
        return suggestions, row
    reassessed["llm_exploration_used"] = True
    reassessed["llm_exploration_queries"] = query
    reassessed["llm_exploration_candidate_gain"] = gain
    return merged, reassessed


VALIDATOR_FINDING_COLUMNS = [
    "dataset_id",
    "table_id",
    "column_name",
    "code",
    "severity",
    "role",
    "before_decision",
    "after_decision",
    "message",
]


def _dictionary_row(target, dictionary) -> dict:
    matches = dictionary[
        (dictionary["dataset_id"].astype(str) == str(target.get("dataset_id")))
        & (dictionary["table_id"].astype(str) == str(target.get("table_id")))
        & (
            dictionary["column_name"].astype(str)
            == str(target.get("column_name"))
        )
    ]
    return matches.iloc[0].to_dict() if not matches.empty else {}


# --- The bundle validators ---------------------------------------------------
#
# Everything from here to `_apply_validators()` gives the verdicts metasalmon's
# `R/semantic-bundle-validators.R` gives on the same input (hub queue B-360,
# ruled 2026-09-25 with the S16 execplan, decision 9: converge on R's
# validators, this package moving). The validators are surface 5 of the role
# contract, and the shared review-packet fixtures (B-326 / B-327) fail until the
# two sides agree, so this is convergence rather than a PARITY.md row.
# tests/test_validator_parity.py holds the block to cases metasalmon itself
# scored.
#
# Regex flags, because they decide word boundaries. R's `perl = TRUE` patterns
# run PCRE without Unicode properties -- measured 2026-09-25 on R 4.5.2: `\b`,
# `\s`, `\w` and `[[:punct:]]` are ASCII-only -- so their mirrors here carry
# re.ASCII. R's default engine, which `_role_type_message()`'s explicit
# native-type tests use, has a locale-aware `\b`; Python's default Unicode `\b`
# is the closest match for that.

_VALIDATOR_WEAK_SINGLETONS = frozenset(
    {
        "age", "code", "count", "length", "method", "number", "phase", "rate",
        "sex", "total", "unit", "value", "weight",
    }
)


def _validator_trim(value) -> str:
    """R's ``trimws()`` default: space, tab, CR and LF only. Hub B-364 adds a
    general ``_r_trimws()`` to the context block; fold this into it once both
    have merged."""
    return str(value).strip(" \t\r\n")


def _validator_scalar(value, default=None):
    """``.ms_semantic_trim_string()``: the value trimmed as R trims it, else the default."""
    if _missing(value):
        return default
    text = _validator_trim(value)
    return text if text else default


def _validator_values(values) -> list[str]:
    """The pieces ``unlist(list(...))`` would yield, as text; missing ones dropped."""
    flat = []
    for value in values:
        if isinstance(value, str):
            flat.append(value)
        elif isinstance(value, (list, tuple, pd.Series, pd.Index)):
            flat.extend(_validator_values(value))
        elif not _missing(value):
            flat.append(str(value))
    return flat


def _validator_text(*values) -> str:
    """``.ms_semantic_validator_text()``: the non-blank values, untrimmed, joined
    by one space and lowercased. A missing or blank value is dropped, not
    rendered, so no double space stands where one was."""
    kept = [value for value in _validator_values(values) if _validator_trim(value)]
    return " ".join(kept).lower()


def _validator_tokens(text: str) -> list[str]:
    """``.ms_context_tokens()`` as the anchor builder uses it: lowercase ASCII
    runs of three or more characters. B-364 adds the general
    ``_context_tokens()``; fold this into it once both have merged."""
    text = re.sub(r"[^a-z0-9]+", " ", str(text).lower())
    return [token for token in text.split(" ") if len(token) >= 3]


def _field_anchors(target, dictionary_row: dict) -> list[str]:
    """``.ms_semantic_validator_field_anchors()``: what a context chunk must
    carry to count as evidence for this column.

    The column names (the dictionary's, then the target's) are the candidates;
    only when there is no column name at all do the column labels stand in. A
    candidate makes an ``identifier:`` anchor when it is a plain identifier and
    a ``phrase_start:`` anchor always -- unless it is one weak or short word,
    which anchors nothing.
    """
    field_names = [
        str(name)
        for name in (dictionary_row.get("column_name"), target.get("column_name"))
        if not _missing(name) and _validator_trim(name)
    ]
    labels = [dictionary_row.get("column_label"), target.get("column_label")]
    candidates = field_names if field_names else labels
    anchors = []
    for candidate in dict.fromkeys(
        str(candidate) for candidate in candidates if not _missing(candidate)
    ):
        identifier = _validator_trim(candidate).lower()
        phrase = _validator_trim(re.sub(r"[^a-z0-9]+", " ", identifier))
        tokens = _validator_tokens(phrase)
        if not phrase or (
            len(tokens) < 2
            and (len(phrase) < 6 or phrase in _VALIDATOR_WEAK_SINGLETONS)
        ):
            continue
        if field_names and re.fullmatch(r"[a-z0-9_]+", identifier):
            anchors.append(f"identifier:{identifier}")
        anchors.append(f"phrase_start:{phrase}")
    return list(dict.fromkeys(anchors))


def _chunk_has_anchor(text, anchor: str) -> bool:
    """``.ms_semantic_validator_chunk_has_anchor()``.

    An identifier anchor must be a whole ``[a-z0-9_]`` token of the chunk. A
    phrase anchor must start the chunk once leading markup (ASCII punctuation
    and digits) is stripped, and the chunk's leading token may not carry ``_``
    or ``-``, so ``CATCH_COUNT_ESTIMATE ...`` does not vouch for
    ``catch_count``. Both regexes are R's, quirks included: the leading-token
    pattern spans to the end of the string, so on a multi-line chunk it does
    not match, the whole chunk stands in as the leading token, and any ``_`` or
    ``-`` in it fails the phrase anchor.
    """
    raw_text = "" if _missing(text) else str(text)
    lowered = raw_text.lower()
    anchor = _validator_trim(anchor).lower()
    if not lowered or not anchor:
        return False
    if anchor.startswith("identifier:"):
        identifier = anchor[len("identifier:"):]
        return identifier in re.split(r"[^a-z0-9_]+", lowered)
    phrase = re.sub(r"^phrase_start:", "", anchor)
    unmarked = re.sub(
        r"^\s*(?:[!-/:-@\[-`{-~0-9]+\s*)+", "", raw_text, flags=re.ASCII
    )
    leading_token = re.sub(
        r"^\s*([a-zA-Z0-9][a-zA-Z0-9_-]*).*$", r"\1", unmarked, flags=re.ASCII
    )
    if re.search(r"[_-]", leading_token):
        return False
    normalized = _validator_trim(re.sub(r"[^a-z0-9]+", " ", unmarked.lower()))
    return normalized == phrase or normalized.startswith(f"{phrase} ")


def _anchored_context_text(target, dictionary_row: dict, context) -> list[str]:
    """The context chunks that carry one of the column's anchors, in pool order."""
    if context is None or context.empty or "text" not in context:
        return []
    anchors = _field_anchors(target, dictionary_row)
    selected = []
    for text in context["text"]:
        if _missing(text):
            continue
        if any(_chunk_has_anchor(text, anchor) for anchor in anchors):
            selected.append(str(text))
    return selected


def _evidence_text(target, dictionary, context=None) -> str:
    """The bundle validator evidence for a target whose dictionary row is
    looked up in ``dictionary``."""
    return _bundle_validator_evidence(
        target, _dictionary_row(target, dictionary), context
    )


def _bundle_validator_evidence(target, dictionary_row: dict, context=None) -> str:
    """``.ms_semantic_bundle_validator_evidence()``: the target's query context
    and labels, the dictionary row's name, labels and unit, and the anchored
    context chunks, as one lowercase string."""
    return _validator_text(
        target.get("target_query_context"),
        target.get("column_label"),
        target.get("column_description"),
        dictionary_row.get("column_name"),
        dictionary_row.get("column_label"),
        dictionary_row.get("column_description"),
        dictionary_row.get("unit_label"),
        _anchored_context_text(target, dictionary_row, context),
    )


_VALIDATOR_NEGATION = (
    r"no|not|without|unknown|unspecified|missing|does not|did not|is not|was not"
)
_METHOD_TERMS = (
    r"protocol|gear|instrument|assay|technique|field method|lab method|"
    r"laboratory method|survey method|measurement method|estimation method|"
    r"field procedure|lab procedure|laboratory procedure|"
    r"measurement procedure|operational procedure"
)
_CONSTRAINT_TERMS = (
    r"origin|life[ -]?cycle|life[ -]?stage|stage|run|season|age|sex|"
    r"maturity|phase|terminal|ocean|freshwater|wild|hatchery|population|"
    r"stock|species group|reporting unit|benchmark"
)


def _strip_negated_evidence(text: str, evidence_pattern: str) -> str:
    """``.ms_semantic_validator_strip_negated_evidence()``: blank every
    evidence term that a negation reaches within sixty characters."""
    return re.sub(
        rf"\b({_VALIDATOR_NEGATION})\b.{{0,60}}\b({evidence_pattern})\b",
        " ",
        text,
        flags=re.ASCII,
    )


def _has_method_evidence(text) -> bool:
    """``.ms_semantic_validator_has_method_evidence()``: six alternatives -- a
    method term, a measurement verb followed by ``using``/``with``/``via`` or
    by ``by ... <method|protocol|procedure|observer|technician|instrument|gear>``,
    ``using``/``with`` followed by an instrument, and an estimation verb
    followed by ``using``/``with``/``from`` or by ``by ... <model|algorithm|
    estimator|method|procedure>``."""
    text = _validator_text(text)
    if not text:
        return False
    positive = _strip_negated_evidence(text, _METHOD_TERMS)
    return bool(
        re.search(
            rf"\b({_METHOD_TERMS})\b"
            r"|\b(measured|sampled|surveyed|enumerated|counted|weighed)\b"
            r".{0,80}\b(using|with|via)\b"
            r"|\b(measured|sampled|surveyed|enumerated|counted|weighed)\b"
            r".{0,80}\bby\b.{0,40}"
            r"\b(method|protocol|procedure|observer|technician|instrument|gear)\b"
            r"|\b(using|with)\b.{0,80}"
            r"\b(board|scale|net|sonar|weir|camera|caliper|ruler|sensor|model)\b"
            r"|\b(estimated|calculated|derived|modelled|modeled)\s+(using|with|from)\b"
            r"|\b(estimated|calculated|derived|modelled|modeled)\b.{0,80}\bby\b"
            r".{0,40}\b(model|algorithm|estimator|method|procedure)\b",
            positive,
            flags=re.ASCII,
        )
    )


def _has_modifier_evidence(text) -> bool:
    """``.ms_semantic_validator_has_modifier_evidence()``. Underscores and dots
    are not word boundaries, so ``mean_weight`` is split first."""
    text = _validator_text(text)
    if not text:
        return False
    return bool(
        re.search(
            r"\b(mean|average|median|max|maximum|min|minimum|total|"
            r"cumulative|sum|peak|aggregate|aggregated)\b",
            re.sub(r"[_.]", " ", text),
            flags=re.ASCII,
        )
    )


def _has_constraint_evidence(text) -> bool:
    """``.ms_semantic_validator_has_constraint_evidence()``."""
    text = _validator_text(text)
    if not text:
        return False
    return bool(
        re.search(
            rf"\b({_CONSTRAINT_TERMS})\b",
            _strip_negated_evidence(text, _CONSTRAINT_TERMS),
            flags=re.ASCII,
        )
    )


def _candidate_type(candidate: dict) -> str:
    """``.ms_semantic_validator_candidate_type()``: the candidate's type fields
    present, in this order, as validator text."""
    return _validator_text(
        *(
            candidate.get(field)
            for field in ("term_type", "native_type", "resource_kind", "type_iris")
        )
    )


def _split_role_hints(value) -> list[str]:
    """``.ms_semantic_split_role_hints()``: split on ``|`` only, each hint
    trimmed, empties dropped, order and case kept. A comma or semicolon is
    part of a hint, not a separator."""
    if _missing(value):
        return []
    return [
        hint
        for hint in (_validator_trim(part) for part in str(value).split("|"))
        if hint
    ]


def _role_type_message(role: str, candidate: dict) -> Optional[str]:
    """``.ms_validate_semantic_role_type()``'s message, or ``None`` when the
    candidate may fill the role."""
    hints = _split_role_hints(candidate.get("role_hints"))
    if hints and role not in hints:
        return (
            f"Candidate role hints are incompatible with the {role} slot: "
            f"{', '.join(hints)}."
        )
    iri = _validator_scalar(candidate.get("iri"), "").lower()
    native_type = _candidate_type(candidate)
    if re.search(
        r"object\s*property|datatype\s*property|annotation\s*property|"
        r"rdf\s*property|owl#objectproperty|owl#datatypeproperty|"
        r"owl#annotationproperty",
        native_type,
        flags=re.ASCII,
    ):
        return (
            "Candidate is an ontology relation predicate, not a value that "
            f"can populate the {role} semantic slot."
        )
    # R runs these four in its default engine, not PCRE: locale-aware \b.
    combined = f"{iri} {native_type}"
    explicit_role = None
    if re.search(r"/vocab/unit/|\b(unit|unit of measure)\b", combined):
        explicit_role = "unit"
    elif re.search(r"/vocab/quantitykind/|\b(quantity kind|quantitykind)\b", combined):
        explicit_role = "property"
    elif re.search(r"\b(method|procedure)\b", native_type):
        explicit_role = "method"
    elif re.search(r"\bconstraint\b", native_type):
        explicit_role = "constraint"
    if explicit_role is not None and explicit_role != role:
        return (
            f"Candidate native type is explicitly {explicit_role}-like and "
            f"is incompatible with the {role} slot."
        )
    return None


_TIME_UNIT = r"(s|sec|second|min|minute|h|hr|hour|d|day|wk|week|mo|month|yr|year|season)"
_DENOMINATOR_PATTERN = (
    rf"(\bper\s+{_TIME_UNIT}\b"
    rf"|/\s*{_TIME_UNIT}\b"
    rf"|[-_]per[-_]{_TIME_UNIT}\b"
    rf"|\b{_TIME_UNIT}\s*\^?\s*-\s*1\b)"
)
_POWERED_DENOMINATOR_PATTERN = (
    rf"((\bper\s+|/\s*|[-_]per[-_]){_TIME_UNIT}\s*(\^?\s*[2-9]|squared|cubed)\b"
    rf"|\b{_TIME_UNIT}\s*\^?\s*-\s*[2-9]\b)"
)
# A value that IS a compound unit, whole, names its dimension outright.
_COMPOUND_DIMENSION_RULES = (
    ("flow", r"^(cubic met(er|re)s? per second|m3/s|cumecs?|cms)$"),
    ("speed", r"^(kilomet(er|re)s? per hour|met(er|re)s? per second|km/h|m/s|kph)$"),
)
_DIMENSION_RULES = (
    ("flow", r"\b(flow|discharge)\b"),
    ("speed", r"\b(speed|velocity)\b"),
    ("temperature", r"\b(temperature|celsius|fahrenheit|kelvin|deg c)\b"),
    ("area", r"\b(area|square[ -](milli|centi|kilo)?met(er|re)s?|m2|hectare)\b"),
    (
        "volume",
        r"\b(volume|lit(er|re)s?|cubic[ -](milli|centi|kilo)?met(er|re)s?|m3)\b",
    ),
    (
        "mass",
        r"\b(mass|weight|kilograms?|grams?|tonnes?|pounds?|lbs?|kg|kilogm|gm)\b",
    ),
    (
        "length",
        r"\b(length|width|depth|height|fork length|millimet(er|re)s?|"
        r"centimet(er|re)s?|met(er|re)s?|mm|cm|millim|centim)\b",
    ),
    ("count", r"\b(count|abundance|number|numerosity|individuals?|num)\b"),
    (
        "dimensionless",
        r"\b(dimensionless|unitless|percentage|percent|proportion|ratio|"
        r"fraction|decimal|dimensionlessratio)\b"
        r"|\b(survival|exploitation|harvest|mortality) rate\b"
        r"|/vocab/unit/(percent|one)\b",
    ),
    (
        "rate",
        r"\b(frequency|occurrences? per|individuals? per|fish per|"
        r"events? per|per capita per)\b",
    ),
)
_STRONG_PHYSICAL_DIMENSIONS = (
    "flow", "speed", "temperature", "area", "volume", "mass", "length",
)


def _dimension(*values) -> Optional[str]:
    """``.ms_semantic_validator_dimension()``: the one dimension the values
    name, or ``None`` when they name none or more than one.

    Each value is lowercased, trimmed, its Unicode minus, superscripts and
    middle dot normalised, and its whitespace collapsed. A value that is a
    whole compound unit (``m3/s``, ``km/h``) decides on its own. A time
    denominator (``per year``, ``/s``, ``yr^-1``) counted more than once, or
    raised to a power, means a derived quantity this classifier does not name.
    One time denominator adds ``rate``. Then: exactly one strong physical
    dimension wins unless a rate is also present; more than one is ambiguous;
    ``rate`` beats the weak classes; and a weak class stands only alone.
    """
    normalized = []
    for value in _validator_values(values):
        if not _validator_trim(value):
            continue
        value = _validator_trim(value).lower()
        value = re.sub(r"\u2212|\u207b", "-", value)
        value = (
            value.replace("\u00b9", "1")
            .replace("\u00b2", "2")
            .replace("\u00b3", "3")
            .replace("\u00b7", " ")
        )
        normalized.append(re.sub(r"\s+", " ", value, flags=re.ASCII))
    if not normalized:
        return None
    text = " ".join(normalized)

    compound = [
        name
        for name, pattern in _COMPOUND_DIMENSION_RULES
        if any(re.search(pattern, value, flags=re.ASCII) for value in normalized)
    ]
    if len(compound) == 1:
        return compound[0]
    if len(compound) > 1:
        return None

    denominator_count = max(
        len(re.findall(_DENOMINATOR_PATTERN, value, flags=re.ASCII))
        for value in normalized
    )
    powered_denominator = any(
        re.search(_POWERED_DENOMINATOR_PATTERN, value, flags=re.ASCII)
        for value in normalized
    )
    if denominator_count > 1 or powered_denominator:
        return None

    matched = [
        name
        for name, pattern in _DIMENSION_RULES
        if re.search(pattern, text, flags=re.ASCII)
    ]
    if denominator_count == 1 and "rate" not in matched:
        matched.append("rate")
    strong_physical = [name for name in matched if name in _STRONG_PHYSICAL_DIMENSIONS]
    if len(strong_physical) == 1:
        return None if "rate" in matched else strong_physical[0]
    if len(strong_physical) > 1:
        return None
    if "rate" in matched:
        return "rate"
    return matched[0] if len(matched) == 1 else None


def _selected_candidate(row, target, suggestions) -> Optional[dict]:
    raw_index = row.get("llm_selected_candidate_index")
    if _missing(raw_index):
        return None
    candidates = _candidates_for_target(suggestions, target)
    try:
        index = int(raw_index)
    except (TypeError, ValueError):
        return None
    if not 1 <= index <= len(candidates):
        return None
    return candidates.iloc[index - 1].to_dict()


def _finding(row, code: str, message: str) -> dict:
    return {
        "dataset_id": row.get("dataset_id"),
        "table_id": row.get("table_id"),
        "column_name": row.get("column_name"),
        "code": code,
        "severity": "warning",
        "role": row.get("dictionary_role"),
        "before_decision": "accept",
        "after_decision": "review",
        "message": message,
    }


def _candidate_dimension(candidate: dict) -> Optional[str]:
    """``.ms_semantic_validator_candidate_dimension()``."""
    return _dimension(
        candidate.get("label"),
        candidate.get("definition"),
        candidate.get("iri"),
    )


def _current_selected_iris(rows, dictionary_row: dict) -> dict:
    """``.ms_semantic_bundle_current_selected_iris()``: the IRI each slot holds
    once the accepts are applied -- the dictionary's own filled slots (a
    ``REVIEW:``-marked value does not count) overridden by every accepted
    row's selected IRI. The paired-redundancy rule reads this, so a variable
    the dictionary already carries pairs with a newly accepted constraint."""
    selected = {role: None for role in BUNDLE_SLOT_FIELDS}
    for role, field in BUNDLE_SLOT_FIELDS.items():
        value = _validator_scalar(dictionary_row.get(field), "")
        if value and not re.match(r"REVIEW:", value, flags=re.IGNORECASE):
            selected[role] = value
    for row in rows:
        iri = _validator_scalar(row.get("llm_selected_iri"), "")
        if _validator_scalar(row.get("llm_decision"), "") == "accept" and iri:
            selected[str(row.get("dictionary_role"))] = iri
    return selected


def _downgrade(row, findings: list[dict]) -> None:
    """Turn the accept into a review, clear the selection, keep the confidence
    and append ``[CODE] message`` for each finding to the rationale."""
    row["llm_decision"] = "review"
    row["llm_selected_candidate_index"] = pd.NA
    row["llm_selected_iri"] = pd.NA
    row["llm_selected_label"] = pd.NA
    notes = " ".join(
        f"[{finding['code']}] {finding['message']}" for finding in findings
    )
    rationale = _validator_scalar(row.get("llm_rationale"), None)
    row["llm_rationale"] = notes if rationale is None else f"{rationale} {notes}"


def _apply_validators(
    rows,
    targets,
    dictionary,
    suggestions,
    context,
) -> tuple[list[dict], list[dict]]:
    """``.ms_semantic_apply_bundle_validators()``: run the seven deterministic
    validators over every accepted row of one bundle, in R's order, and
    downgrade the rows that fail.

    The selected candidates and the current slot IRIs are read once, before
    any row is downgraded, so a pair or a redundancy is judged on what the
    model accepted rather than on what an earlier validator left standing.
    """
    target_by_role = {
        str(target["dictionary_role"]): target
        for _, target in targets.iterrows()
    }
    bundle_dictionary_row = (
        _dictionary_row(targets.iloc[0], dictionary) if len(targets) else {}
    )
    selected = {}
    for row in rows:
        role = str(row.get("dictionary_role"))
        if (
            _validator_scalar(row.get("llm_decision"), "") != "accept"
            or role not in target_by_role
        ):
            continue
        candidate = _selected_candidate(row, target_by_role[role], suggestions)
        if candidate is not None:
            selected[role] = candidate
    selected_iris = _current_selected_iris(rows, bundle_dictionary_row)

    all_findings = []
    for row in rows:
        if _validator_scalar(row.get("llm_decision"), "") != "accept":
            continue
        role = str(row.get("dictionary_role"))
        target = target_by_role.get(role)
        if target is None:
            continue
        candidate = _selected_candidate(row, target, suggestions)
        if candidate is None:
            continue
        dictionary_row = _dictionary_row(target, dictionary)
        evidence = _bundle_validator_evidence(target, dictionary_row, context)
        row_findings = []

        if role == "method" and not _has_method_evidence(evidence):
            row_findings.append(
                _finding(
                    row,
                    "SEM_METHOD_EVIDENCE_REQUIRED",
                    "The accepted method candidate lacks explicit field, "
                    "protocol, gear, instrument, or estimation-procedure evidence.",
                )
            )
        if role == "constraint" and not _has_constraint_evidence(evidence):
            row_findings.append(
                _finding(
                    row,
                    "SEM_CONSTRAINT_EVIDENCE_REQUIRED",
                    "The accepted constraint candidate lacks an explicit "
                    "qualifier such as origin, life stage, phase, season, age, "
                    "sex, stock, or reporting unit.",
                )
            )
        if role == "statistical_modifier" and not _has_modifier_evidence(
            evidence
        ):
            # The dictionary's statistical modifier is part of variable
            # identity, so an accept needs the column itself to name an
            # aggregation. Without this, an unsupported modifier silently
            # changes what the variable means. Sits BESIDE the surviving
            # method validator, not in place of it: the code-level method
            # role outlives sdp-0.3.0 (mirrors
            # .ms_validate_semantic_modifier_evidence).
            row_findings.append(
                _finding(
                    row,
                    "SEM_MODIFIER_EVIDENCE_REQUIRED",
                    "The accepted statistical-modifier candidate lacks "
                    "explicit aggregation evidence (mean, median, maximum, "
                    "minimum, total, or peak).",
                )
            )
        role_message = _role_type_message(role, candidate)
        if role_message:
            row_findings.append(
                _finding(row, "SEM_ROLE_TYPE_MISMATCH", role_message)
            )

        if role in {"property", "unit"}:
            expected = _dimension(
                dictionary_row.get("unit_label"),
                dictionary_row.get("unit_iri"),
            )
            actual = _candidate_dimension(candidate) if expected else None
            if expected and actual and expected != actual:
                row_findings.append(
                    _finding(
                        row,
                        "SEM_DIMENSION_MISMATCH",
                        f"Candidate appears {actual}-dimensional, but the "
                        f"dictionary unit is {expected}-dimensional.",
                    )
                )

        property_candidate = selected.get("property")
        unit_candidate = selected.get("unit")
        if (
            role in {"property", "unit"}
            and property_candidate is not None
            and unit_candidate is not None
        ):
            property_dimension = _candidate_dimension(property_candidate)
            unit_dimension = _candidate_dimension(unit_candidate)
            if (
                property_dimension
                and unit_dimension
                and property_dimension != unit_dimension
            ):
                row_findings.append(
                    _finding(
                        row,
                        "SEM_PROPERTY_UNIT_DIMENSION_MISMATCH",
                        f"The accepted property is {property_dimension}-"
                        f"dimensional, while the accepted unit is "
                        f"{unit_dimension}-dimensional; both require review as "
                        "a pair.",
                    )
                )

        if (
            role == "constraint"
            and _validator_scalar(candidate.get("iri"), "")
            == "https://w3id.org/smn/CatchContext"
            and _validator_scalar(selected_iris.get("variable"), "")
            == "https://w3id.org/smn/CatchAbundance"
            and not _has_constraint_evidence(evidence)
        ):
            row_findings.append(
                _finding(
                    row,
                    "SEM_REDUNDANT_CATCH_CONTEXT",
                    "CatchContext duplicates the accepted CatchAbundance "
                    "framing when no additional constraint evidence is present.",
                )
            )

        if row_findings:
            _downgrade(row, row_findings)
            all_findings.extend(row_findings)
    return rows, all_findings


def _merge_assessments_into_suggestions(
    suggestions: pd.DataFrame,
    assessments: pd.DataFrame,
) -> pd.DataFrame:
    out = suggestions.copy()
    if out.empty:
        for column in [
            "llm_decision",
            "llm_confidence",
            "llm_selected",
            "llm_candidate_rank",
        ]:
            out[column] = pd.Series(dtype="object")
        return out
    assessment_map = {
        _target_key(row): row for _, row in assessments.iterrows()
    }
    out["llm_candidate_rank"] = (
        out.groupby(TARGET_JOIN_COLUMNS, dropna=False).cumcount() + 1
    )
    review_columns = [
        column
        for column in LLM_ASSESSMENT_COLUMNS
        if column.startswith("llm_")
    ]
    for column in review_columns:
        out[column] = out.apply(
            lambda row: assessment_map.get(_target_key(row), {}).get(
                column, pd.NA
            ),
            axis=1,
        )
    out["llm_selected"] = out.apply(
        lambda row: bool(
            not _missing(row.get("llm_selected_candidate_index"))
            and int(row["llm_selected_candidate_index"])
            == int(row["llm_candidate_rank"])
            and _text(row.get("llm_decision")) == "accept"
        ),
        axis=1,
    )
    return out


def assess_semantic_suggestions(
    targets: pd.DataFrame,
    suggestions: pd.DataFrame,
    dictionary: pd.DataFrame,
    *,
    source_policy: dict,
    search_fn: Callable,
    max_per_role: int,
    provider: str,
    model: Optional[str],
    api_key: Optional[str],
    base_url: Optional[str],
    reasoning_effort: Optional[str],
    context_files,
    context_text,
    timeout_seconds: int,
    request_fn,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Assess deterministic candidates while preserving them on any failure."""
    config = resolve_llm_config(
        provider,
        model,
        api_key,
        base_url,
        reasoning_effort,
        timeout_seconds,
        request_fn,
    )
    chunks = load_context_chunks(context_files, context_text)
    excerpt_limit = _context_chunk_limit(config)
    rows = []
    handled = set()
    validator_findings = []

    column_targets = targets[
        (targets["target_scope"] == "column")
        & targets["column_name"].notna()
    ]
    bundle_keys = [
        "dataset_id",
        "table_id",
        "column_name",
    ]
    for key, group in column_targets.groupby(bundle_keys, dropna=False):
        roles = set(group["dictionary_role"].astype(str))
        if not roles.intersection(BUNDLE_SLOT_FIELDS):
            continue
        dictionary_row = _dictionary_row(group.iloc[0], dictionary)
        if _text(dictionary_row.get("column_role"), "").lower() != "measurement":
            continue
        context = _relevant_context(chunks, group, suggestions, excerpt_limit)
        bundle_rows = _assess_bundle(
            group,
            suggestions,
            context,
            config,
            source_policy,
            dictionary,
        )
        suggestions, bundle_rows = _apply_bundle_retry(
            group,
            suggestions,
            bundle_rows,
            context,
            config,
            source_policy,
            dictionary,
            search_fn,
            max_per_role,
        )
        bundle_rows, bundle_findings = _apply_validators(
            bundle_rows,
            group,
            dictionary,
            suggestions,
            context,
        )
        validator_findings.extend(bundle_findings)
        rows.extend(bundle_rows)
        handled.update(_target_key(target) for _, target in group.iterrows())

    for _, target in targets.iterrows():
        if _target_key(target) in handled:
            continue
        candidates = _candidates_for_target(suggestions, target)
        context = _relevant_context(
            chunks, pd.DataFrame([target]), suggestions, excerpt_limit
        )
        row = _assess_generic(target, candidates, context, config)
        initial_row = row.copy()
        suggestions, row = _apply_generic_retry(
            target,
            suggestions,
            row,
            context,
            config,
            source_policy,
            search_fn,
            max_per_role,
        )
        _escalate_reject_shortlist(row, initial_row)
        rows.append(row)

    assessments = normalize_assessment_rows(rows)
    assessments.attrs["semantic_validator_findings"] = pd.DataFrame(
        validator_findings,
        columns=VALIDATOR_FINDING_COLUMNS,
    )
    return (
        _merge_assessments_into_suggestions(suggestions, assessments),
        assessments,
    )


__all__ = [
    "AUTO_APPLY_ROLES",
    "LLM_ASSESSMENT_COLUMNS",
    "assess_semantic_suggestions",
    "load_context_chunks",
    "make_source_policy",
    "normalize_assessment_rows",
    "policy_sources",
    "validate_context_files",
]
