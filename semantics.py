from __future__ import annotations

from typing import Callable, Optional, Sequence

try:
    import pandas as pd
except ImportError as exc:  # pragma: no cover - import guard
    raise ImportError("salmonpy requires pandas; install via `pip install pandas`.") from exc

import re

from .metadata import normalize_codes, normalize_dataset_meta, normalize_dictionary, normalize_table_meta
from .term_search import find_terms
from .dwc_dp import suggest_dwc_mappings

ROLE_MAP = {
    "term_iri": "variable",
    "property_iri": "property",
    "entity_iri": "entity",
    "unit_iri": "unit",
    "constraint_iri": "constraint",
    "method_iri": "method",
}


def _is_missing(value) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except Exception:
        pass
    return isinstance(value, str) and value.strip() == ""


def _first_non_empty(*values) -> str:
    for value in values:
        if isinstance(value, (list, tuple)):
            nested = _first_non_empty(*value)
            if nested:
                return nested
        elif not _is_missing(value):
            return str(value)
    return ""


def _clean_query(value: str) -> str:
    value = re.sub(r"[._]+", " ", str(value))
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _split_role_hints(value) -> list[str]:
    if _is_missing(value):
        return []
    return [part.strip() for part in re.split(r"\|", str(value)) if part.strip()]


def _role_hint_status(role: str, role_hints) -> str:
    hints = _split_role_hints(role_hints)
    if not hints:
        return "unknown"
    if role in hints:
        return "match"
    if role == "variable" and "property" in hints:
        return "mismatch_property"
    if role == "property" and "variable" in hints:
        return "mismatch_variable"
    return "unknown"


def _role_hint_bonus(status: str) -> float:
    return {"match": 0.35, "mismatch_property": -0.35, "mismatch_variable": -0.35}.get(status, 0.0)


def _role_hint_explanation(status: str, role: str):
    if status == "match":
        return f"Candidate carries a {role} role hint."
    if status == "mismatch_property":
        return "Candidate carries a property hint; kept lower for variable destination."
    if status == "mismatch_variable":
        return "Candidate carries a variable hint; kept lower for property destination."
    return pd.NA


def suggest_semantics(
    df: pd.DataFrame,
    dict_df: pd.DataFrame,
    sources: Sequence[str] = ("smn", "gcdfo", "ols", "nvs"),
    include_dwc: bool = False,
    max_per_role: int = 3,
    search_fn: Callable = find_terms,
    codes: Optional[pd.DataFrame] = None,
    table_meta: Optional[pd.DataFrame] = None,
    dataset_meta: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """
    Suggest semantic annotations for dictionary, code, table, and dataset targets.
    """
    dictionary = normalize_dictionary(pd.DataFrame(dict_df))
    codes_df = normalize_codes(codes)
    table_df = normalize_table_meta(table_meta) if table_meta is not None else pd.DataFrame()
    dataset_df = normalize_dataset_meta(dataset_meta) if dataset_meta is not None else pd.DataFrame()

    if dictionary.empty and (codes_df is None or codes_df.empty) and table_df.empty and dataset_df.empty:
        dictionary.attrs["semantic_suggestions"] = pd.DataFrame()
        if include_dwc:
            dictionary.attrs["dwc_mappings"] = pd.DataFrame()
        return dictionary

    targets = []

    for _, row in dictionary.iterrows():
        if row.get("column_role") != "measurement":
            continue

        query = _clean_query(_first_non_empty(row.get("column_description"), row.get("column_label"), row.get("column_name")))
        for col_name, role_name in ROLE_MAP.items():
            if col_name not in dictionary.columns:
                continue
            if not _is_missing(row[col_name]):
                continue
            role_query = query
            if role_name == "unit":
                role_query = _clean_query(_first_non_empty(row.get("unit_label"), query))
            if not role_query:
                continue
            targets.append(
                {
                    "dataset_id": row.get("dataset_id"),
                    "table_id": row.get("table_id"),
                    "column_name": row.get("column_name"),
                    "code_value": pd.NA,
                    "dictionary_role": role_name,
                    "target_scope": "column",
                    "target_sdp_file": "column_dictionary.csv",
                    "target_sdp_field": col_name,
                    "target_row_key": f"{row.get('dataset_id')}/{row.get('table_id')}/{row.get('column_name')}",
                    "target_label": row.get("column_label"),
                    "target_description": row.get("column_description"),
                    "search_query": role_query,
                    "column_label": row.get("column_label"),
                    "column_description": row.get("column_description"),
                    "code_label": pd.NA,
                    "code_description": pd.NA,
                }
            )

    if codes_df is not None and not codes_df.empty:
        for _, row in codes_df.iterrows():
            if not _is_missing(row.get("term_iri")):
                continue
            dataset_id = row.get("dataset_id")
            table_id = row.get("table_id")
            column_name = row.get("column_name")
            parent = dictionary[
                (dictionary["dataset_id"] == dataset_id)
                & (dictionary["table_id"] == table_id)
                & (dictionary["column_name"] == column_name)
            ]
            parent_row = parent.iloc[0] if not parent.empty else {}
            parent_role = parent_row.get("column_role", pd.NA) if hasattr(parent_row, "get") else pd.NA
            role_set = ["constraint", "entity", "method"] if parent_role == "measurement" else ["entity"]
            query = _clean_query(
                _first_non_empty(
                    row.get("code_description"),
                    row.get("code_label"),
                    row.get("code_value"),
                    parent_row.get("column_description", pd.NA) if hasattr(parent_row, "get") else pd.NA,
                    parent_row.get("column_label", pd.NA) if hasattr(parent_row, "get") else pd.NA,
                    column_name,
                )
            )
            if not query:
                continue
            for role_name in role_set:
                targets.append(
                    {
                        "dataset_id": dataset_id,
                        "table_id": table_id,
                        "column_name": column_name,
                        "code_value": row.get("code_value"),
                        "dictionary_role": role_name,
                        "target_scope": "code",
                        "target_sdp_file": "codes.csv",
                        "target_sdp_field": "term_iri",
                        "target_row_key": f"{dataset_id}/{table_id}/{column_name}/{row.get('code_value')}",
                        "target_label": _first_non_empty(row.get("code_label"), row.get("code_value")),
                        "target_description": row.get("code_description"),
                        "search_query": query,
                        "column_label": parent_row.get("column_label", column_name) if hasattr(parent_row, "get") else column_name,
                        "column_description": parent_row.get("column_description", pd.NA) if hasattr(parent_row, "get") else pd.NA,
                        "code_label": row.get("code_label"),
                        "code_description": row.get("code_description"),
                    }
                )

    if not table_df.empty:
        for _, row in table_df.iterrows():
            if not _is_missing(row.get("observation_unit_iri")):
                continue
            query = _clean_query(_first_non_empty(row.get("observation_unit"), row.get("description"), row.get("table_label"), row.get("table_id")))
            if not query:
                continue
            targets.append(
                {
                    "dataset_id": row.get("dataset_id"),
                    "table_id": row.get("table_id"),
                    "column_name": pd.NA,
                    "code_value": pd.NA,
                    "dictionary_role": "entity",
                    "target_scope": "table",
                    "target_sdp_file": "tables.csv",
                    "target_sdp_field": "observation_unit_iri",
                    "target_row_key": f"{row.get('dataset_id')}/{row.get('table_id')}",
                    "target_label": row.get("table_label"),
                    "target_description": row.get("description"),
                    "search_query": query,
                    "column_label": pd.NA,
                    "column_description": pd.NA,
                    "code_label": pd.NA,
                    "code_description": pd.NA,
                }
            )

    if not dataset_df.empty:
        for _, row in dataset_df.iterrows():
            if not _is_missing(row.get("keywords")):
                continue
            query = _clean_query(_first_non_empty(row.get("description"), row.get("title"), row.get("dataset_id")))
            if not query:
                continue
            targets.append(
                {
                    "dataset_id": row.get("dataset_id"),
                    "table_id": pd.NA,
                    "column_name": pd.NA,
                    "code_value": pd.NA,
                    "dictionary_role": "entity",
                    "target_scope": "dataset",
                    "target_sdp_file": "dataset.csv",
                    "target_sdp_field": "keywords",
                    "target_row_key": row.get("dataset_id"),
                    "target_label": row.get("title"),
                    "target_description": row.get("description"),
                    "search_query": query,
                    "column_label": pd.NA,
                    "column_description": pd.NA,
                    "code_label": pd.NA,
                    "code_description": pd.NA,
                }
            )

    suggestion_rows = []
    leading = [
        "column_name",
        "dictionary_role",
        "table_id",
        "dataset_id",
        "target_row_key",
        "target_label",
        "target_description",
        "target_scope",
        "target_sdp_file",
        "target_sdp_field",
        "search_query",
        "column_label",
        "column_description",
        "code_value",
        "code_label",
        "code_description",
    ]

    for target in targets:
        res = search_fn(target["search_query"], role=target["dictionary_role"], sources=sources)
        if res is None or res.empty:
            continue
        res = res.copy()
        if "role_hints" not in res.columns:
            res["role_hints"] = pd.NA
        res = res.drop_duplicates(subset=[col for col in ["source", "iri"] if col in res.columns], keep="first")
        res["role_hint_status"] = res["role_hints"].apply(lambda value: _role_hint_status(str(target["dictionary_role"]), value))
        res["role_hint_bonus"] = res["role_hint_status"].apply(_role_hint_bonus)
        res["role_hint_explanation"] = res["role_hint_status"].apply(lambda status: _role_hint_explanation(status, str(target["dictionary_role"])))
        if "score" in res.columns:
            res["score"] = pd.to_numeric(res["score"], errors="coerce").fillna(0) + res["role_hint_bonus"]
            res = res.sort_values(["score", "source", "ontology", "label", "iri"], ascending=[False, True, True, True, True])
        else:
            res = res.sort_values(["role_hint_bonus", "source", "ontology", "label", "iri"], ascending=[False, True, True, True, True])
        res = res.head(max_per_role).copy()
        for key, value in target.items():
            res[key] = value
        suggestion_rows.append(res)

    if suggestion_rows:
        suggestions_df = pd.concat(suggestion_rows, ignore_index=True)
        optional = [
            col
            for col in [
                "score",
                "alignment_only",
                "agreement_sources",
                "zooma_confidence",
                "zooma_annotator",
                "role_hints",
                "role_hint_status",
                "role_hint_bonus",
                "role_hint_explanation",
            ]
            if col in suggestions_df.columns
        ]
        core = ["label", "iri", "source", "ontology", "role", "match_type", "definition"]
        ordered = [col for col in leading + core + optional if col in suggestions_df.columns]
        suggestions_df = suggestions_df[ordered + [col for col in suggestions_df.columns if col not in ordered]]
    else:
        suggestions_df = pd.DataFrame(
            columns=leading + ["label", "iri", "source", "ontology", "role", "match_type", "definition"]
        )

    if not suggestions_df.empty:
        suggestions_df["_candidate_label_norm"] = suggestions_df["label"].fillna("").astype(str).str.strip().str.lower()
        group_cols = [
            "dataset_id",
            "table_id",
            "column_name",
            "code_value",
            "target_scope",
            "target_sdp_file",
            "_candidate_label_norm",
        ]
        suggestions_df["_collision_key"] = suggestions_df[group_cols].apply(
            lambda row: "\r".join("<NA>" if _is_missing(value) else str(value) for value in row),
            axis=1,
        )
        collision_roles = suggestions_df.groupby("_collision_key")["dictionary_role"].agg(
            lambda values: "|".join(sorted(set(values.dropna().astype(str))))
        )
        suggestions_df["collision_roles"] = suggestions_df["_collision_key"].map(collision_roles)
        suggestions_df["role_collision"] = suggestions_df["collision_roles"].apply(
            lambda value: {"variable", "property"}.issubset(set(str(value).split("|")))
        )
        suggestions_df["role_collision_note"] = pd.NA
        variable_collision = suggestions_df["role_collision"] & (suggestions_df["dictionary_role"] == "variable")
        property_collision = suggestions_df["role_collision"] & (suggestions_df["dictionary_role"] == "property")
        suggestions_df.loc[variable_collision, "role_collision_note"] = (
            "Label appears for variable and property candidates; this row targets variable semantics for "
            + suggestions_df.loc[variable_collision, "target_sdp_field"].astype(str)
            + "."
        )
        suggestions_df.loc[property_collision, "role_collision_note"] = (
            "Label appears for variable and property candidates; this row targets property semantics for "
            + suggestions_df.loc[property_collision, "target_sdp_field"].astype(str)
            + "."
        )
        suggestions_df = suggestions_df.drop(columns=["_candidate_label_norm", "_collision_key"])

    dictionary.attrs["semantic_suggestions"] = suggestions_df
    if include_dwc:
        dictionary.attrs["dwc_mappings"] = suggest_dwc_mappings(dictionary).attrs.get("dwc_mappings", pd.DataFrame())
    return dictionary


def apply_semantic_suggestions(
    dict_df: pd.DataFrame,
    suggestions: Optional[pd.DataFrame] = None,
    strategy: str = "top",
    columns: Optional[Sequence[str]] = None,
    roles: Optional[Sequence[str]] = None,
    min_score: Optional[float] = None,
    overwrite: bool = False,
    verbose: bool = True,
) -> pd.DataFrame:
    if strategy != "top":
        raise ValueError("Unsupported strategy: use 'top'.")
    out = normalize_dictionary(dict_df)
    if suggestions is None:
        suggestions = dict_df.attrs.get("semantic_suggestions")
    if suggestions is None:
        raise ValueError("No semantic suggestions supplied.")
    suggestions_df = pd.DataFrame(suggestions).copy()
    if suggestions_df.empty:
        if verbose:
            print("No semantic suggestions to apply.")
        return out
    required = {"column_name", "dictionary_role", "iri"}
    missing = required - set(suggestions_df.columns)
    if missing:
        raise ValueError(f"Suggestions are missing required columns: {sorted(missing)}")
    if min_score is not None and "score" not in suggestions_df.columns:
        raise ValueError("min_score requires scored suggestions.")

    role_to_field = {
        "variable": "term_iri",
        "property": "property_iri",
        "entity": "entity_iri",
        "unit": "unit_iri",
        "constraint": "constraint_iri",
        "method": "method_iri",
    }
    if roles is not None:
        invalid = set(roles) - set(role_to_field)
        if invalid:
            raise ValueError(f"Unsupported roles: {sorted(invalid)}")

    suggestions_df["_row_id"] = range(len(suggestions_df))
    suggestions_df = suggestions_df[~suggestions_df["iri"].isna() & (suggestions_df["iri"] != "")]
    if "target_scope" in suggestions_df.columns:
        suggestions_df = suggestions_df[suggestions_df["target_scope"].isna() | (suggestions_df["target_scope"] == "column")]
    if "target_sdp_file" in suggestions_df.columns:
        suggestions_df = suggestions_df[
            suggestions_df["target_sdp_file"].isna() | (suggestions_df["target_sdp_file"] == "column_dictionary.csv")
        ]
    if columns is not None:
        suggestions_df = suggestions_df[suggestions_df["column_name"].isin(columns)]
    if roles is not None:
        suggestions_df = suggestions_df[suggestions_df["dictionary_role"].isin(roles)]
    if min_score is not None:
        suggestions_df = suggestions_df[pd.to_numeric(suggestions_df["score"], errors="coerce") >= min_score]
    suggestions_df = suggestions_df[suggestions_df["dictionary_role"].isin(role_to_field)]
    if suggestions_df.empty:
        if verbose:
            print("No semantic suggestions met the requested filters.")
        return out

    match_keys = [key for key in ["dataset_id", "table_id", "column_name", "dictionary_role"] if key in suggestions_df.columns]
    selected = suggestions_df.sort_values("_row_id").drop_duplicates(subset=match_keys, keep="first")
    applied = 0
    skipped_existing = 0
    unmatched = 0

    for _, suggestion in selected.iterrows():
        field = role_to_field[str(suggestion["dictionary_role"])]
        matches = out["column_name"] == suggestion["column_name"]
        for key in ["dataset_id", "table_id"]:
            if key in out.columns and key in suggestion and not _is_missing(suggestion[key]):
                matches = matches & (out[key] == suggestion[key])
        row_ids = out.index[matches].tolist()
        if not row_ids:
            unmatched += 1
            continue
        if overwrite:
            out.loc[row_ids, field] = suggestion["iri"]
            applied += len(row_ids)
            continue
        missing_now = out.loc[row_ids, field].apply(_is_missing)
        fill_rows = missing_now[missing_now].index
        if len(fill_rows) > 0:
            out.loc[fill_rows, field] = suggestion["iri"]
            applied += len(fill_rows)
        skipped_existing += int((~missing_now).sum())

    if verbose:
        print(f"Applied {applied} semantic suggestion fields using the {strategy} strategy.")
        if skipped_existing:
            print(f"{skipped_existing} fields were left alone because the dictionary already had an IRI.")
        if unmatched:
            print(f"{unmatched} suggestions did not match any dictionary row.")
    return out


__all__ = ["apply_semantic_suggestions", "suggest_semantics"]
