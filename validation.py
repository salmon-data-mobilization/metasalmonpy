"""
Graceful semantic validation with gap reporting.

This module provides user-friendly validation that reports semantic gaps without
aborting the entire validation run.
"""

from typing import Dict, List, Optional
import re

import pandas as pd


def re_match(pattern: str, value: str) -> bool:
    return re.search(pattern, value, flags=re.IGNORECASE) is not None


def validate_semantics(
    dictionary: pd.DataFrame,
    require_iris: bool = False,
    entity_defaults: Optional[pd.DataFrame] = None,
    vocab_priority: Optional[List[str]] = None
) -> Dict[str, pd.DataFrame]:
    """
    Validate semantics with graceful gap reporting.

    Ensures structural requirements, adds a `required` column if missing,
    runs validate_dictionary(), and reports missing `term_iri` for measurement
    columns. In default mode validate_dictionary() warns (instead of raising)
    when semantic fields are missing, so the caller can continue.

    Parameters
    ----------
    dictionary : pd.DataFrame
        Dictionary tibble/data frame
    require_iris : bool, default=False
        If True, require IRIs in all semantic fields
    entity_defaults : pd.DataFrame, optional
        Data frame with `table_prefix` and `entity_iri`
        (not applied automatically here but reserved for future use)
    vocab_priority : List[str], optional
        Character vector of vocab sources (reserved for future use)

    Returns
    -------
    Dict[str, pd.DataFrame]
        Dictionary with elements:
        - dict: normalized dictionary with `required` column
        - issues: DataFrame of structural issues (empty if none)
        - missing_terms: DataFrame of measurement rows missing `term_iri`
        - missing_semantics: DataFrame of measurement rows missing any semantic field
          (core + optional)

    Examples
    --------
    >>> import pandas as pd
    >>> from metasalmonpy import validate_semantics
    >>> dict_df = pd.read_csv("column_dictionary.csv")
    >>> result = validate_semantics(dict_df, require_iris=False)
    >>> print(result['issues'])  # Structural problems
    >>> print(result['missing_terms'])  # Measurements needing term_iri
    >>> # Suggest terms for missing measurements
    >>> if not result['missing_terms'].empty:
    ...     print("Proposed terms:")
    ...     print(result['missing_terms'][['term_label', 'term_definition']])
    """
    try:
        from .dictionary import validate_dictionary, CORE_SEMANTIC_FIELDS, OPTIONAL_SEMANTIC_FIELDS
    except ImportError:  # pragma: no cover - direct module import compatibility
        from metasalmonpy.dictionary import validate_dictionary, CORE_SEMANTIC_FIELDS, OPTIONAL_SEMANTIC_FIELDS

    dict_df = dictionary.copy()

    if "required" not in dict_df.columns:
        dict_df["required"] = False
    for col in ["term_iri", "property_iri", "entity_iri", "unit_iri", "constraint_iri", "statistical_modifier_iri"]:
        if col not in dict_df.columns:
            dict_df[col] = pd.NA
    for col in ["table_id", "column_name", "column_role", "column_description"]:
        if col not in dict_df.columns:
            dict_df[col] = pd.NA

    issues = pd.DataFrame()
    missing_terms = pd.DataFrame()
    missing_semantics = pd.DataFrame(columns=["field", "table_id", "column_name"])

    try:
        dict_df = validate_dictionary(dict_df, require_iris=require_iris)
    except Exception as e:
        issues = pd.DataFrame({"message": [str(e)]})

    semantic_rows = dict_df["column_role"] == "measurement"
    if semantic_rows.any():
        all_semantic_fields = CORE_SEMANTIC_FIELDS + OPTIONAL_SEMANTIC_FIELDS

        for field in all_semantic_fields:
            if field not in dict_df.columns:
                continue
            missing_mask = semantic_rows & (dict_df[field].isna() | (dict_df[field] == ""))
            if missing_mask.any():
                rows = dict_df.loc[missing_mask, ["table_id", "column_name"]].copy()
                rows["field"] = field
                rows = rows[["field", "table_id", "column_name"]]
                missing_semantics = pd.concat([missing_semantics, rows], ignore_index=True)

    semantic_cols = [
        "term_iri",
        "property_iri",
        "entity_iri",
        "unit_iri",
        "constraint_iri",
        "statistical_modifier_iri",
    ]
    iri_issue_rows = []
    for col in [c for c in semantic_cols if c in dict_df.columns]:
        for idx, iri in dict_df[col].dropna().items():
            iri = str(iri).strip()
            if not iri:
                continue
            issue = None
            if re_match(r"^salmon:[^\s]+$", iri):
                issue = f"Row {idx + 1} field {col} uses legacy SMN CURIE form ({iri}); use https://w3id.org/smn/<Term>."
            elif re_match(r"^smn:[^\s]+$", iri):
                issue = f"Row {idx + 1} field {col} uses non-canonical SMN CURIE form ({iri}); use https://w3id.org/smn/<Term>."
            elif re_match(r"^gcdfo:[^\s]+$", iri):
                issue = f"Row {idx + 1} field {col} uses non-canonical GCDFO CURIE form ({iri}); use https://w3id.org/gcdfo/salmon#<Term>."
            elif re_match(r"^https?://w3id\.org/salmon/", iri):
                issue = f"Row {idx + 1} field {col} uses legacy SMN namespace ({iri}); use https://w3id.org/smn/<Term>."
            elif re_match(r"^http://w3id\.org/smn/", iri):
                issue = f"Row {idx + 1} field {col} uses non-canonical SMN HTTP IRI ({iri}); use https://w3id.org/smn/<Term>."
            elif re_match(r"^https?://w3id\.org/smn#", iri):
                issue = f"Row {idx + 1} field {col} uses non-canonical SMN IRI form ({iri}); use https://w3id.org/smn/<Term>."
            elif re_match(r"^http://w3id\.org/gcdfo/salmon#", iri):
                issue = f"Row {idx + 1} field {col} uses non-canonical GCDFO HTTP IRI ({iri}); use https://w3id.org/gcdfo/salmon#<Term>."
            elif re_match(r"^https?://w3id\.org/gcdfo/salmon/", iri):
                issue = f"Row {idx + 1} field {col} uses non-canonical GCDFO IRI form ({iri}); use https://w3id.org/gcdfo/salmon#<Term>."
            if issue:
                iri_issue_rows.append({"message": issue})
    if iri_issue_rows:
        issues = pd.concat([issues, pd.DataFrame(iri_issue_rows)], ignore_index=True)

    missing_mask = (
        (dict_df["column_role"] == "measurement") &
        (dict_df["term_iri"].isna() | (dict_df["term_iri"] == ""))
    )

    if missing_mask.any():
        missing_df = dict_df[missing_mask].copy()

        missing_df["term_label"] = missing_df["column_name"].fillna("").str.replace("_", " ").str.title()
        missing_df["term_definition"] = missing_df["column_description"].fillna("")
        missing_df["term_type"] = "skos_concept"
        missing_df["suggested_parent_iri"] = "https://w3id.org/smn/TargetOrLimitRateOrAbundance"
        missing_df["notes"] = (
            "Derived from " + missing_df["column_name"].fillna("") +
            " in " + missing_df["table_id"].fillna("") +
            " (constraints: " + missing_df["constraint_iri"].fillna("") + ")"
        )

        missing_terms = missing_df[[
            "term_label", "term_definition", "term_type",
            "suggested_parent_iri", "notes"
        ]]

    return {
        "dict": dict_df,
        "issues": issues,
        "missing_terms": missing_terms,
        "missing_semantics": missing_semantics,
    }


__all__ = ["validate_semantics"]
