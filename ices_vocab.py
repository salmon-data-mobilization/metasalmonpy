from __future__ import annotations

import urllib.parse

try:
    import pandas as pd
except ImportError as exc:  # pragma: no cover - import guard
    raise ImportError("metasalmonpy requires pandas; install via `pip install pandas`.") from exc

from .term_search import _safe_json

ICES_BASE_URL = "https://vocab.ices.dk/services/api"


def ices_code_types(code_type: str = "", code_type_id: int = 0, modified: str = "") -> pd.DataFrame:
    """
    List ICES controlled vocabulary code types.

    Note: these are code lists (categorical value tables), not OWL ontologies.
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
    data = _safe_json(url, headers={"Accept": "application/json"})
    if not data:
        return pd.DataFrame()
    return pd.DataFrame(data)


def ices_codes(code_type: str, code: str = "", modified: str = "") -> pd.DataFrame:
    """
    List ICES codes for a given code type.

    Adds columns `code_type` and `url` (pointing at the CodeDetail API endpoint).
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
    data = _safe_json(url, headers={"Accept": "application/json"})
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
