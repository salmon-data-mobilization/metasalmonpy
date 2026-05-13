from __future__ import annotations

import re
from typing import Optional, Sequence

import pandas as pd
import requests


TERM_REQUEST_DEFAULT_TEMPLATE = "https://github.com/dfo-pacific-science/dfo-salmon-ontology/blob/main/.github/ISSUE_TEMPLATE/new-term-request.md"

GAP_COLUMNS = [
    "dataset_id",
    "table_id",
    "column_name",
    "code_value",
    "target_scope",
    "target_sdp_file",
    "target_sdp_field",
    "target_row_key",
    "dictionary_role",
    "search_query",
    "column_label",
    "column_description",
    "top_non_smn_source",
    "top_non_smn_label",
    "top_non_smn_iri",
    "top_non_smn_ontology",
    "top_non_smn_match_type",
    "top_non_smn_score",
    "candidate_count",
    "non_smn_sources",
    "placement_recommendation",
    "placement_confidence",
    "placement_rationale",
]


def _empty_term_gap_result() -> pd.DataFrame:
    return pd.DataFrame(columns=GAP_COLUMNS)


def _trim_empties(values: Sequence) -> list[str]:
    out = []
    for value in values:
        if pd.isna(value):
            continue
        text = str(value).strip()
        if text:
            out.append(text)
    return out


def _first_non_empty(*values, default=""):
    for value in values:
        if isinstance(value, (list, tuple, pd.Series)):
            nested = _first_non_empty(*list(value), default="")
            if nested:
                return nested
        elif not pd.isna(value) and str(value).strip():
            return str(value).strip()
    return default


def _has_local_term_signals(query, dictionary_role, sources) -> bool:
    q = str(query or "").lower()
    if not q:
        return False
    local_patterns = [
        "id",
        "ids",
        "code",
        "codes",
        "flag",
        "status",
        "project",
        "program",
        "site",
        "station",
        "trip",
        "haul",
        "vessel",
        "fleet",
        "qc",
        "qaqc",
        "sample",
        "event",
        "group",
        "run",
        "permit",
        "operator",
        "file",
    ]
    if any(re.search(rf"\b{re.escape(pattern)}\b", q) for pattern in local_patterns):
        return True
    return str(dictionary_role).lower() in {"unit", "constraint", "method"} and bool(sources)


def _recommend_term_placement(search_query, dictionary_role, sources, local_hint=False) -> dict:
    sources = [str(src).lower() for src in _trim_empties(sources)]
    score_smn = 0.0
    score_profile = 0.0
    if any(src in {"smn", "gcdfo"} for src in sources):
        score_smn += 2.0
    if any(src in {"ols", "nvs", "qudt"} for src in sources):
        score_smn += 0.7
    if any(src in {"gbif", "worms", "bioportal", "zooma"} for src in sources):
        score_profile += 0.6
    if local_hint:
        score_profile += 1.0
    if dictionary_role in {"variable", "property", "entity", "constraint"} and sources:
        score_smn += 0.4
    if score_profile >= score_smn + 0.8:
        placement = "profile"
    elif score_smn >= score_profile + 0.8:
        placement = "smn"
    else:
        placement = "uncertain"
    gap = max(abs(score_smn - score_profile), 0.0)
    confidence = min(0.95, 0.35 + (gap / 4.0))
    return {
        "placement": placement,
        "confidence": confidence,
        "rationale": f"Signals: sources={{{','.join(sources)}}}, local_pattern={bool(local_hint)}, role={dictionary_role} -> suggest '{placement}'",
    }


def detect_semantic_term_gaps(
    dict_df: Optional[pd.DataFrame] = None,
    suggestions: Optional[pd.DataFrame] = None,
    include_target_scopes: Sequence[str] = ("column", "code", "table", "dataset"),
    include_dictionary_roles: Optional[Sequence[str]] = None,
    min_score: Optional[float] = None,
) -> pd.DataFrame:
    if suggestions is None:
        if dict_df is None:
            raise ValueError("Provide either dict_df with semantic_suggestions or suggestions.")
        suggestions = dict_df.attrs.get("semantic_suggestions")
    if suggestions is None or len(suggestions) == 0:
        return _empty_term_gap_result()
    df = pd.DataFrame(suggestions).copy()
    required = [
        "dataset_id",
        "table_id",
        "column_name",
        "code_value",
        "dictionary_role",
        "target_scope",
        "target_sdp_file",
        "target_sdp_field",
        "target_row_key",
        "search_query",
        "column_label",
        "column_description",
        "source",
        "label",
        "iri",
        "ontology",
        "match_type",
        "definition",
    ]
    missing = [col for col in required if col not in df.columns]
    if missing:
        raise ValueError(f"Missing required suggestion columns: {missing}")
    df["target_scope"] = df["target_scope"].astype(str).str.lower().str.strip()
    df = df[df["target_scope"].isin([str(scope).lower() for scope in include_target_scopes])]
    if include_dictionary_roles is not None:
        df = df[df["dictionary_role"].isin(include_dictionary_roles)]
    if df.empty:
        return _empty_term_gap_result()
    df["source"] = df["source"].fillna("").astype(str).str.lower().str.strip()
    df["score"] = pd.to_numeric(df.get("score", pd.NA), errors="coerce")
    if min_score is not None:
        df = df[df["score"].isna() | (df["score"] >= min_score)]
    if df.empty:
        return _empty_term_gap_result()
    def _is_smn_row(row) -> bool:
        iri = _first_non_empty(row.get("iri"))
        return bool(iri) and (
            row.get("source") == "smn"
            or re.search(r"^https?://w3id\.org/smn/", iri, flags=re.I) is not None
        )

    df["is_smn"] = df.apply(_is_smn_row, axis=1)
    base_cols = [
        "dataset_id",
        "table_id",
        "column_name",
        "code_value",
        "target_scope",
        "target_sdp_file",
        "target_sdp_field",
        "target_row_key",
        "dictionary_role",
    ]
    rows = []
    for _, group in df.groupby(base_cols, dropna=False):
        if group["is_smn"].any():
            continue
        non_smn = group[~group["is_smn"]]
        if non_smn.empty:
            continue
        top = non_smn.sort_values(["score", "source", "label"], ascending=[False, True, True], na_position="last").iloc[0]
        sources = sorted(set(_trim_empties(non_smn["source"])))
        local_hint = _has_local_term_signals(top.get("search_query"), top.get("dictionary_role"), sources)
        recommendation = _recommend_term_placement(top.get("search_query"), top.get("dictionary_role"), sources, local_hint)
        key = group.iloc[0]
        rows.append(
            {
                "dataset_id": key.get("dataset_id"),
                "table_id": key.get("table_id"),
                "column_name": key.get("column_name"),
                "code_value": key.get("code_value"),
                "target_scope": key.get("target_scope"),
                "target_sdp_file": key.get("target_sdp_file"),
                "target_sdp_field": key.get("target_sdp_field"),
                "target_row_key": key.get("target_row_key"),
                "dictionary_role": key.get("dictionary_role"),
                "search_query": _first_non_empty(key.get("search_query")),
                "column_label": _first_non_empty(key.get("column_label")),
                "column_description": _first_non_empty(key.get("column_description")),
                "top_non_smn_source": top.get("source"),
                "top_non_smn_label": top.get("label"),
                "top_non_smn_iri": _first_non_empty(top.get("iri")),
                "top_non_smn_ontology": _first_non_empty(top.get("ontology")),
                "top_non_smn_match_type": _first_non_empty(top.get("match_type")),
                "top_non_smn_score": top.get("score"),
                "candidate_count": len(non_smn),
                "non_smn_sources": ", ".join(sources),
                "placement_recommendation": recommendation["placement"],
                "placement_confidence": recommendation["confidence"],
                "placement_rationale": recommendation["rationale"],
            }
        )
    if not rows:
        return _empty_term_gap_result()
    return pd.DataFrame(rows)[GAP_COLUMNS].sort_values("placement_confidence", ascending=False).reset_index(drop=True)


def render_ontology_term_request(
    gaps: pd.DataFrame,
    scope: str = "auto",
    ask: bool = False,
    profile_name: Optional[str] = None,
    scope_overrides=None,
    issue_labels=None,
    term_request_template: str = TERM_REQUEST_DEFAULT_TEMPLATE,
    ontology_repo: str = "dfo-pacific-science/dfo-salmon-ontology",
) -> pd.DataFrame:
    df = pd.DataFrame(gaps).copy()
    if df.empty:
        return pd.DataFrame()
    required = [
        "dataset_id",
        "table_id",
        "column_name",
        "target_scope",
        "target_sdp_file",
        "target_sdp_field",
        "target_row_key",
        "dictionary_role",
        "search_query",
        "column_label",
        "column_description",
        "top_non_smn_source",
        "top_non_smn_label",
        "top_non_smn_iri",
        "top_non_smn_ontology",
        "placement_recommendation",
    ]
    missing = [col for col in required if col not in df.columns]
    if missing:
        raise ValueError(f"Missing required gap columns: {missing}")
    if scope not in {"auto", "smn", "profile"}:
        raise ValueError("scope must be one of 'auto', 'smn', or 'profile'.")
    df["request_scope"] = df["placement_recommendation"].str.lower().str.strip() if scope == "auto" else scope
    if scope_overrides is not None:
        if isinstance(scope_overrides, str):
            df["request_scope"] = scope_overrides
        else:
            if len(scope_overrides) != len(df):
                raise ValueError("scope_overrides must be length 1 or len(gaps).")
            df["request_scope"] = list(scope_overrides)
    if profile_name is None:
        if scope == "profile" and not ask:
            raise ValueError("profile_name is required when scope='profile' and ask=False.")
        profile_name = "project-profile"
    if ask:
        for idx, row in df.iterrows():
            if row["request_scope"] not in {"auto", "uncertain", ""}:
                continue
            term_label = _first_non_empty(row.get("top_non_smn_label"), row.get("search_query"), row.get("column_name"), default="Unnamed term")
            print(f"Term gap review: {term_label}")
            print("1. Request in shared SMN")
            print("2. Request in local/program/organization profile")
            print("3. Skip for now")
            choice = input("Route this term request [1/2/3]: ").strip()
            df.at[idx, "request_scope"] = {"1": "smn", "2": "profile", "3": "skip"}.get(choice, "skip")
            if df.at[idx, "request_scope"] == "profile" and profile_name == "project-profile":
                entered = input("Profile name [project-profile]: ").strip()
                if entered:
                    profile_name = entered
    df["request_scope"] = df["request_scope"].where(df["request_scope"].isin(["smn", "profile"]), "skip")
    df["profile_name"] = df["request_scope"].apply(lambda s: profile_name if s == "profile" else pd.NA)

    titles = []
    bodies = []
    for _, row in df.iterrows():
        term_label = _first_non_empty(row.get("top_non_smn_label"), row.get("search_query"), row.get("column_name"), default="Unnamed term")
        if row["request_scope"] == "smn":
            titles.append(f"Request new shared SMN term: {term_label}")
            scope_block = "Shared vocabulary candidate for `smn` (reusable across salmon programs and organizations)"
        elif row["request_scope"] == "profile":
            titles.append(f"Request new {profile_name} profile term: {term_label}")
            scope_block = f"Profile: `{profile_name}` (default location for this domain term)"
        else:
            titles.append(f"Skip term request: {term_label}")
            scope_block = "Skipped at this stage"
        query_text = _first_non_empty(row.get("search_query"), row.get("column_name"), term_label)
        source = _first_non_empty(row.get("top_non_smn_source"), default="unknown")
        iri = _first_non_empty(row.get("top_non_smn_iri"), default="Not found")
        ontology = _first_non_empty(row.get("top_non_smn_ontology"), default="unknown")
        description = _first_non_empty(row.get("column_description"), default="No additional description captured.")
        rationale = _first_non_empty(row.get("placement_rationale"), default="No rationale computed yet.")
        target = f"dataset {row.get('dataset_id')} table {row.get('table_id')} role {row.get('dictionary_role')}"
        bodies.append(
            "\n".join(
                [
                    "## Proposed ontology term request",
                    "",
                    f"**Target term (dataset query):** `{query_text}`",
                    "",
                    "## Context",
                    f"- Dataset: `{row.get('dataset_id')}`",
                    f"- Table: `{row.get('table_id')}`",
                    f"- Target role: `{row.get('dictionary_role')}`",
                    f"- Target field: `{row.get('target_sdp_field')}` in `{row.get('target_sdp_file')}`",
                    f"- Column/table context: `{target}`",
                    "",
                    "## Why this is currently missing from SMN",
                    rationale,
                    "",
                    "## Best matching candidate outside SMN",
                    f"- Label: `{term_label}`",
                    f"- IRI (if any): `{iri}`",
                    f"- Source: `{source}`",
                    f"- Ontology: `{ontology}`",
                    "",
                    "## Suggested definition",
                    description,
                    "",
                    "## Placement for governance",
                    scope_block,
                    "",
                    "## Helpful links",
                    f"- New term template: {term_request_template}",
                    "- Ontology repo: https://github.com/dfo-pacific-science/dfo-salmon-ontology",
                    "- Shared domain conventions: https://github.com/dfo-pacific-science/dfo-salmon-ontology/blob/main/README.md",
                ]
            )
        )
    df["request_title"] = titles
    df["request_body"] = bodies
    df["ontology_repo"] = ontology_repo
    df["issue_labels"] = [issue_labels for _ in range(len(df))]
    return df


def submit_term_request_issues(
    requests_df: pd.DataFrame,
    repo: str = "dfo-pacific-science/dfo-salmon-ontology",
    token: Optional[str] = None,
    dry_run: bool = True,
    confirm: bool = False,
) -> pd.DataFrame:
    df = pd.DataFrame(requests_df).copy()
    if df.empty:
        return pd.DataFrame()
    required = {"request_title", "request_body", "request_scope", "ontology_repo"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing required request columns: {sorted(missing)}")
    pending = df[df["request_scope"].isin(["smn", "profile"])]
    if pending.empty:
        return pd.DataFrame()
    if dry_run:
        return pd.DataFrame(
            {
                "request_title": pending["request_title"].tolist(),
                "request_body": pending["request_body"].tolist(),
                "request_scope": pending["request_scope"].tolist(),
                "issue_number": [pd.NA] * len(pending),
                "issue_url": [pd.NA] * len(pending),
                "status": ["dry_run"] * len(pending),
            }
        )
    if token is None:
        from .github_io import _github_token

        token = _github_token()
    if not token:
        raise ValueError("No GitHub token available.")
    rows = []
    target_repo = pending["ontology_repo"].iloc[0] if "ontology_repo" in pending else repo
    for _, row in pending.iterrows():
        if confirm:
            answer = input(f"Submit {row['request_scope']} request: {row['request_title']}? [y/N] ").strip().lower()
            if answer not in {"y", "yes"}:
                rows.append(
                    {
                        "request_title": row["request_title"],
                        "status": "skipped",
                        "issue_number": pd.NA,
                        "issue_url": pd.NA,
                    }
                )
                continue
        response = requests.post(
            f"https://api.github.com/repos/{target_repo}/issues",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            json={"title": row["request_title"], "body": row["request_body"], "labels": row.get("issue_labels") or []},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        rows.append(
            {
                "request_title": row["request_title"],
                "status": "submitted",
                "issue_number": payload.get("number"),
                "issue_url": payload.get("html_url"),
            }
        )
    return pd.DataFrame(rows)


__all__ = [
    "detect_semantic_term_gaps",
    "render_ontology_term_request",
    "submit_term_request_issues",
]
