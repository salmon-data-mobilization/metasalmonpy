from __future__ import annotations

import re
from typing import List, Optional

try:
    import pandas as pd
except ImportError as exc:  # pragma: no cover - import guard
    raise ImportError("metasalmonpy requires pandas; install via `pip install pandas`.") from exc

from importlib import resources


def _load_dwc_dp_fields() -> pd.DataFrame:
    try:
        fields_path = resources.files("metasalmonpy").joinpath("data/dwc-dp-fields.csv")
        with resources.as_file(fields_path) as path:
            return pd.read_csv(path)
    except Exception:
        return pd.DataFrame(
            columns=[
                "table_id",
                "table_label",
                "table_iri",
                "field_name",
                "field_label",
                "field_description",
                "term_iri",
                "term_namespace",
            ]
        )


def _clean_text(value: Optional[str]) -> str:
    if value is None:
        return ""
    value = re.sub(r"[._]+", " ", str(value).lower())
    value = re.sub(r"[^a-z0-9\\s]+", " ", value)
    value = re.sub(r"\\s+", " ", value)
    return value.strip()


def _score_fields(query: str, fields: pd.DataFrame) -> pd.DataFrame:
    if not query:
        return pd.DataFrame()

    name_clean = fields["field_name"].fillna("").map(_clean_text)
    label_clean = fields["field_label"].fillna("").map(_clean_text)

    query_tokens = {tok for tok in query.split(" ") if tok}
    if not query_tokens:
        return pd.DataFrame()

    exact_name = name_clean == query
    exact_label = label_clean == query
    substring = (
        name_clean.str.contains(query, case=False, regex=False)
        | label_clean.str.contains(query, case=False, regex=False)
    )

    overlap_scores: List[float] = []
    for lbl in label_clean.tolist():
        tokens = {tok for tok in lbl.split(" ") if tok}
        if not tokens:
            overlap_scores.append(0)
            continue
        overlap_scores.append(len(tokens.intersection(query_tokens)) / len(tokens.union(query_tokens)))

    # Use pandas' built-in edit distance for small strings via python's difflib
    from difflib import SequenceMatcher  # local import

    def _ratio(a: str, b: str) -> float:
        return SequenceMatcher(None, a, b).ratio()

    ratios = [
        max(_ratio(query, n), _ratio(query, l)) if n or l else 0
        for n, l in zip(name_clean.tolist(), label_clean.tolist())
    ]
    ratio_scores = [r if r >= 0.6 else 0 for r in ratios]

    score = (
        exact_name.astype(float) * 3.0
        + exact_label.astype(float) * 2.0
        + substring.astype(float) * 1.5
        + pd.Series(overlap_scores) * 1.2
        + pd.Series(ratio_scores) * 1.0
    )

    basis = []
    for i in range(len(fields)):
        tags: List[str] = []
        if bool(exact_name.iloc[i]):
            tags.append("exact_name")
        if bool(exact_label.iloc[i]):
            tags.append("exact_label")
        if bool(substring.iloc[i]):
            tags.append("substring")
        if overlap_scores[i] > 0:
            tags.append("token_overlap")
        if ratios[i] >= 0.6:
            tags.append("fuzzy")
        basis.append("|".join(tags) if tags else "none")

    scored = fields.copy()
    scored["match_score"] = score
    scored["match_basis"] = basis
    return scored[scored["match_score"] > 0]


def suggest_dwc_mappings(dict_df: pd.DataFrame, max_per_column: int = 3) -> pd.DataFrame:
    """
    Suggest DwC-DP table/field mappings for dictionary columns.
    """
    if not isinstance(dict_df, pd.DataFrame):
        raise TypeError("dict_df must be a pandas DataFrame")
    if "column_name" not in dict_df.columns:
        raise ValueError("dict_df must include 'column_name'")

    fields = _load_dwc_dp_fields()
    if fields.empty or dict_df.empty:
        dict_df.attrs["dwc_mappings"] = pd.DataFrame()
        return dict_df

    suggestions = []

    def _first_query(*values: str) -> str:
        for val in values:
            if val is None:
                continue
            if isinstance(val, float) and pd.isna(val):
                continue
            if isinstance(val, str) and val == "":
                continue
            return str(val)
        return ""

    for _, row in dict_df.iterrows():
        query = _first_query(row.get("column_description"), row.get("column_label"), row.get("column_name"))
        query = _clean_text(query)
        if not query:
            continue

        scored = _score_fields(query, fields)
        if scored.empty:
            continue
        scored = scored.sort_values(by=["match_score", "table_id", "field_name"], ascending=[False, True, True])
        scored = scored.head(max_per_column).copy()
        scored["column_name"] = row.get("column_name")
        scored = scored[
            [
                "column_name",
                "table_id",
                "field_name",
                "field_label",
                "term_iri",
                "match_score",
                "match_basis",
            ]
        ]
        suggestions.append(scored)

    if suggestions:
        suggestions_df = pd.concat(suggestions, ignore_index=True)
    else:
        suggestions_df = pd.DataFrame(
            columns=[
                "column_name",
                "table_id",
                "field_name",
                "field_label",
                "term_iri",
                "match_score",
                "match_basis",
            ]
        )

    dict_df.attrs["dwc_mappings"] = suggestions_df
    return dict_df


__all__ = ["suggest_dwc_mappings"]
