from __future__ import annotations

import urllib.parse
import warnings

try:
    import pandas as pd
except ImportError as exc:  # pragma: no cover - import guard
    raise ImportError("metasalmonpy requires pandas; install via `pip install pandas`.") from exc

from . import term_search as _term_search
from .term_search import _safe_json
from .text_safety import redact_secrets

ICES_BASE_URL = "https://vocab.ices.dk/services/api"


def _ices_request(url: str):
    """One request to the ICES vocab API: the parsed JSON, or ``None`` when the
    request failed. A failed request also warns, naming the request.

    A request that failed and an answer with no rows both reach the helpers
    below as nothing to return, and until hub item B-378 both gave the same
    empty DataFrame in silence, so an outage read as ICES saying it holds no
    such codes. ``_safe_json()`` tells the two apart only by recording the
    failure in a sink, and only when a caller has installed one, which nothing
    here did. ``find_terms()`` installs one for the same reason and warns that
    a source did not answer; this does the same for ICES, as metasalmon's
    ``.ices_request()`` does (hub item B-377).

    A warning and not an error, because the return value stays what it was:
    the empty DataFrame, which these helpers have always returned for a failed
    request, and which metasalmon's B-377 kept for its own helpers by warning
    rather than aborting.
    """
    failures: list = []
    _term_search._search_failure_sinks.append(failures)
    try:
        data = _safe_json(url, headers={"Accept": "application/json"})
    finally:
        _term_search._search_failure_sinks.pop()
    if not failures:
        return data
    # Redacted where the text is captured. _safe_json() has redacted the
    # failure already, and redacting again can only hide more, so this does
    # not depend on it.
    request = redact_secrets(url)
    detail = redact_secrets(failures[-1].removeprefix(_term_search._SEARCH_FAILURE_PREFIX))
    warnings.warn(
        "The ICES vocabulary request failed, so the result is empty.\n"
        f"Request: {request}\n"
        f"Failure: {detail}\n"
        "This empty result says nothing about what ICES holds. "
        "An answer with no rows gives no warning.",
        RuntimeWarning,
        stacklevel=3,
    )
    return None


def ices_code_types(code_type: str = "", code_type_id: int = 0, modified: str = "") -> pd.DataFrame:
    """
    List ICES controlled vocabulary code types.

    Note: these are code lists (categorical value tables), not OWL ontologies.

    The DataFrame is empty when ICES answers with no rows, and also when the
    request fails, which warns (``RuntimeWarning``), naming the request.
    """
    params = {}
    if code_type:
        params["codeType"] = code_type
    if code_type_id:
        params["codeTypeID"] = int(code_type_id)
    if modified:
        params["modified"] = modified
    url = f"{ICES_BASE_URL}/CodeType"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = _ices_request(url)
    if not data:
        return pd.DataFrame()
    return pd.DataFrame(data)


def ices_codes(code_type: str, code: str = "", modified: str = "") -> pd.DataFrame:
    """
    List ICES codes for a given code type.

    Adds columns `code_type` and `url` (pointing at the CodeDetail API endpoint).

    The DataFrame is empty when ICES answers with no rows, and also when the
    request fails, which warns (``RuntimeWarning``), naming the request.
    """
    if not code_type:
        raise ValueError("code_type must be a non-empty ICES code type key (e.g., 'Gear').")

    params = {}
    if code:
        params["code"] = code
    if modified:
        params["modified"] = modified

    url = f"{ICES_BASE_URL}/Code/{urllib.parse.quote(code_type, safe='')}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = _ices_request(url)
    if not data:
        return pd.DataFrame()

    df = pd.DataFrame(data)
    if df.empty:
        return df
    df = df.copy()
    df["code_type"] = code_type
    if "key" in df.columns:
        df["url"] = df["key"].apply(lambda k: f"{ICES_BASE_URL}/CodeDetail/{code_type}/{k}")
    else:
        df["url"] = None
    return df


def ices_find_code_types(query: str, max_results: int = 20) -> pd.DataFrame:
    """
    Find ICES code types by simple substring match on key/description.

    The DataFrame is empty when nothing matches, and also when the request to
    ICES fails, which warns (``RuntimeWarning``), naming the request.
    """
    if not query:
        return pd.DataFrame()
    q = query.lower()
    df = ices_code_types()
    if df.empty:
        return df
    for col in ["key", "description", "longDescription"]:
        if col not in df.columns:
            df[col] = ""
    mask = (
        df["key"].fillna("").astype(str).str.lower().str.contains(q, regex=False)
        | df["description"].fillna("").astype(str).str.lower().str.contains(q, regex=False)
        | df["longDescription"].fillna("").astype(str).str.lower().str.contains(q, regex=False)
    )
    return df.loc[mask].head(max_results).reset_index(drop=True)


def ices_find_codes(query: str, code_type: str, max_results: int = 50) -> pd.DataFrame:
    """
    Find ICES codes within a code type by simple substring match on key/description.

    The DataFrame is empty when nothing matches, and also when the request to
    ICES fails, which warns (``RuntimeWarning``), naming the request.
    """
    if not query:
        return pd.DataFrame()
    q = query.lower()
    df = ices_codes(code_type)
    if df.empty:
        return df
    for col in ["key", "description", "longDescription"]:
        if col not in df.columns:
            df[col] = ""
    mask = (
        df["key"].fillna("").astype(str).str.lower().str.contains(q, regex=False)
        | df["description"].fillna("").astype(str).str.lower().str.contains(q, regex=False)
        | df["longDescription"].fillna("").astype(str).str.lower().str.contains(q, regex=False)
    )
    return df.loc[mask].head(max_results).reset_index(drop=True)


__all__ = ["ices_code_types", "ices_codes", "ices_find_code_types", "ices_find_codes", "ICES_BASE_URL"]
