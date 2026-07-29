from __future__ import annotations

import re
from typing import Optional, Sequence

import pandas as pd
import requests


TERM_REQUEST_DEFAULT_TEMPLATE = (
    "https://github.com/salmon-data-mobilization/salmon-domain-ontology/"
    "blob/main/.github/ISSUE_TEMPLATE/new-term-request.md"
)
GCDFO_TERM_REQUEST_DEFAULT_TEMPLATE = (
    "https://github.com/dfo-pacific-science/dfo-salmon-ontology/"
    "blob/main/.github/ISSUE_TEMPLATE/new-term-request.md"
)
SMN_REPO = "salmon-data-mobilization/salmon-domain-ontology"
GCDFO_REPO = "dfo-pacific-science/dfo-salmon-ontology"

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
    "target_label",
    "target_description",
    "gap_detection_basis",
    "llm_decision",
    "llm_confidence",
    "llm_rationale",
    "llm_new_term_label",
    "llm_new_term_definition",
    "llm_new_term_namespace",
    "llm_escalated_from",
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
    """
    Detect structured ontology gaps from candidate and final LLM evidence.

    When ``suggestions`` is omitted, both semantic attributes are read from
    ``dict_df``. Explicit suggestions use only LLM fields embedded in that
    table. Identical duplicate assessments are collapsed; conflicting proposed
    term fields raise an error.

    Parameters
    ----------
    dict_df
        Dictionary carrying semantic result attributes.
    suggestions
        Explicit semantic suggestion table.
    include_target_scopes
        Target scopes to retain.
    include_dictionary_roles
        Optional semantic-role filter.
    min_score
        Candidate score threshold. Final LLM ``request_new_term`` evidence is
        not removed by this threshold.

    Returns
    -------
    pandas.DataFrame
        Stable structured gap rows with candidate evidence, detection basis,
        LLM rationale, proposed-term metadata, and escalation provenance.
    """
    suggestions_explicit = suggestions is not None
    assessments = None
    if suggestions is None:
        if dict_df is None:
            raise ValueError("Provide either dict_df with semantic_suggestions or suggestions.")
        suggestions = dict_df.attrs.get("semantic_suggestions")
        assessments = dict_df.attrs.get("semantic_llm_assessments")

    df = (
        pd.DataFrame(suggestions).copy()
        if suggestions is not None
        else pd.DataFrame()
    )
    embedded = (
        df[df.get("llm_decision", pd.Series(index=df.index, dtype="object")) == "request_new_term"].copy()
        if "llm_decision" in df
        else pd.DataFrame()
    )
    if suggestions_explicit:
        assessments = embedded
    elif assessments is None or len(assessments) == 0:
        assessments = embedded
    assessments = (
        pd.DataFrame(assessments).copy()
        if assessments is not None
        else pd.DataFrame()
    )

    target_key = [
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
    target_defaults = {
        "target_row_key": pd.NA,
        "target_label": pd.NA,
        "target_description": pd.NA,
        "column_label": pd.NA,
        "column_description": pd.NA,
    }
    for frame in (df, assessments):
        for column in target_key:
            if column not in frame:
                frame[column] = pd.NA
        for column, default in target_defaults.items():
            if column not in frame:
                frame[column] = default

    allowed_scopes = {str(scope).lower() for scope in include_target_scopes}
    for frame in (df, assessments):
        frame["target_scope"] = (
            frame["target_scope"].fillna("").astype(str).str.lower().str.strip()
        )
        frame.drop(
            frame.index[~frame["target_scope"].isin(allowed_scopes)],
            inplace=True,
        )
        if include_dictionary_roles is not None:
            frame.drop(
                frame.index[
                    ~frame["dictionary_role"].isin(include_dictionary_roles)
                ],
                inplace=True,
            )

    if not assessments.empty:
        assessments = assessments[
            assessments.get("llm_decision") == "request_new_term"
        ].copy()

    required_candidate = [
        "source",
        "label",
        "iri",
        "ontology",
        "match_type",
        "definition",
    ]
    if not df.empty:
        missing = [column for column in required_candidate if column not in df]
        if missing:
            raise ValueError(f"Missing required suggestion columns: {missing}")
        df["source"] = (
            df["source"].fillna("").astype(str).str.lower().str.strip()
        )
        df["score"] = pd.to_numeric(
            df.get("score", pd.Series(index=df.index, dtype=float)),
            errors="coerce",
        )

    def key_value(row):
        return tuple(
            "" if pd.isna(row.get(column)) else str(row.get(column))
            for column in target_key
        )

    candidate_indices = {}
    for index, row in df.iterrows():
        candidate_indices.setdefault(key_value(row), []).append(index)
    candidate_groups = {
        key: df.loc[indices].copy()
        for key, indices in candidate_indices.items()
    }
    assessment_groups = {}
    if not assessments.empty:
        proposal_columns = [
            "llm_new_term_label",
            "llm_new_term_definition",
            "llm_new_term_namespace",
            "llm_escalated_from",
        ]
        for column in proposal_columns:
            if column not in assessments:
                assessments[column] = pd.NA
        assessment_indices = {}
        for index, row in assessments.iterrows():
            assessment_indices.setdefault(key_value(row), []).append(index)
        for key, indices in assessment_indices.items():
            group = assessments.loc[indices].copy()
            group = group.drop_duplicates()
            for column in proposal_columns:
                values = {
                    str(value).strip()
                    for value in group[column].dropna()
                    if str(value).strip()
                }
                if len(values) > 1:
                    raise ValueError(
                        f"Conflicting {column} values for semantic target {key}."
                    )
            assessment_groups[key] = group

    rows = []
    all_keys = set(candidate_groups) | set(assessment_groups)
    for key_values in all_keys:
        group = candidate_groups.get(key_values, pd.DataFrame())
        assessment_group = assessment_groups.get(
            key_values,
            pd.DataFrame(),
        )
        metadata = (
            group.iloc[0]
            if not group.empty
            else assessment_group.iloc[0]
        )
        candidate_evidence = group.copy()
        if min_score is not None and not candidate_evidence.empty:
            candidate_evidence = candidate_evidence[
                candidate_evidence["score"].isna()
                | (candidate_evidence["score"] >= min_score)
            ]
        if not candidate_evidence.empty:
            candidate_evidence["is_smn"] = candidate_evidence.apply(
                lambda row: bool(_first_non_empty(row.get("iri")))
                and (
                    row.get("source") == "smn"
                    or re.search(
                        r"^https?://w3id\.org/smn/",
                        _first_non_empty(row.get("iri")),
                        flags=re.I,
                    )
                    is not None
                ),
                axis=1,
            )
            non_smn = candidate_evidence[
                ~candidate_evidence["is_smn"]
            ].copy()
            candidate_gap = (
                not candidate_evidence["is_smn"].any()
                and not non_smn.empty
            )
        else:
            non_smn = pd.DataFrame()
            candidate_gap = False
        llm_gap = not assessment_group.empty
        if not candidate_gap and not llm_gap:
            continue

        top = None
        if not non_smn.empty:
            top = non_smn.sort_values(
                ["score", "source", "label"],
                ascending=[False, True, True],
                na_position="last",
            ).iloc[0]
        sources = (
            sorted(set(_trim_empties(non_smn["source"])))
            if not non_smn.empty
            else []
        )
        recommendation = _recommend_term_placement(
            metadata.get("search_query"),
            metadata.get("dictionary_role"),
            sources,
            _has_local_term_signals(
                metadata.get("search_query"),
                metadata.get("dictionary_role"),
                sources,
            ),
        )

        dict_match = pd.DataFrame()
        if isinstance(dict_df, pd.DataFrame) and not dict_df.empty:
            mask = pd.Series(True, index=dict_df.index)
            for column in ("dataset_id", "table_id", "column_name"):
                value = _first_non_empty(metadata.get(column))
                if value and column in dict_df:
                    mask &= dict_df[column].astype(str) == value
            dict_match = dict_df.loc[mask]
        dictionary_row = (
            dict_match.iloc[0] if not dict_match.empty else {}
        )

        def proposal(column):
            if assessment_group.empty or column not in assessment_group:
                return ""
            return _first_non_empty(assessment_group[column])

        column_label = _first_non_empty(
            metadata.get("column_label"),
            dictionary_row.get("column_label")
            if hasattr(dictionary_row, "get")
            else None,
            metadata.get("column_name"),
        )
        column_description = _first_non_empty(
            metadata.get("column_description"),
            dictionary_row.get("column_description")
            if hasattr(dictionary_row, "get")
            else None,
        )
        target_label = _first_non_empty(
            metadata.get("target_label"),
            column_label,
            metadata.get("search_query"),
            metadata.get("column_name"),
        )
        target_description = _first_non_empty(
            metadata.get("target_description"),
            column_description,
        )
        if candidate_gap and llm_gap:
            basis = "candidate_gap_and_llm_request_new_term"
        elif llm_gap:
            basis = "llm_request_new_term"
        else:
            basis = "candidate_gap"
        confidence = pd.NA
        if llm_gap and "llm_confidence" in assessment_group:
            values = pd.to_numeric(
                assessment_group["llm_confidence"],
                errors="coerce",
            ).dropna()
            if not values.empty:
                confidence = values.max()
        rationales = (
            list(
                dict.fromkeys(
                    _trim_empties(assessment_group.get("llm_rationale", []))
                )
            )
            if llm_gap
            else []
        )
        rows.append(
            {
                "dataset_id": metadata.get("dataset_id"),
                "table_id": metadata.get("table_id"),
                "column_name": metadata.get("column_name"),
                "code_value": metadata.get("code_value"),
                "target_scope": metadata.get("target_scope"),
                "target_sdp_file": metadata.get("target_sdp_file"),
                "target_sdp_field": metadata.get("target_sdp_field"),
                "target_row_key": _first_non_empty(
                    metadata.get("target_row_key"),
                    "/".join(
                        _first_non_empty(metadata.get(column))
                        for column in (
                            "dataset_id",
                            "table_id",
                            "column_name",
                        )
                    ),
                ),
                "dictionary_role": metadata.get("dictionary_role"),
                "search_query": _first_non_empty(
                    metadata.get("search_query")
                ),
                "column_label": column_label,
                "column_description": column_description,
                "top_non_smn_source": (
                    top.get("source") if top is not None else pd.NA
                ),
                "top_non_smn_label": (
                    top.get("label") if top is not None else pd.NA
                ),
                "top_non_smn_iri": (
                    _first_non_empty(top.get("iri"))
                    if top is not None
                    else pd.NA
                ),
                "top_non_smn_ontology": (
                    _first_non_empty(top.get("ontology"))
                    if top is not None
                    else pd.NA
                ),
                "top_non_smn_match_type": (
                    _first_non_empty(top.get("match_type"))
                    if top is not None
                    else pd.NA
                ),
                "top_non_smn_score": (
                    top.get("score") if top is not None else pd.NA
                ),
                "candidate_count": len(non_smn),
                "non_smn_sources": (
                    ", ".join(sources) if sources else pd.NA
                ),
                "placement_recommendation": recommendation["placement"],
                "placement_confidence": recommendation["confidence"],
                "placement_rationale": recommendation["rationale"],
                "target_label": target_label,
                "target_description": target_description,
                "gap_detection_basis": basis,
                "llm_decision": (
                    "request_new_term" if llm_gap else pd.NA
                ),
                "llm_confidence": confidence,
                "llm_rationale": (
                    " | ".join(rationales) if rationales else pd.NA
                ),
                "llm_new_term_label": _first_non_empty(
                    proposal("llm_new_term_label"),
                    target_label,
                ),
                "llm_new_term_definition": _first_non_empty(
                    proposal("llm_new_term_definition"),
                    target_description,
                    "Curator definition required.",
                ),
                "llm_new_term_namespace": (
                    proposal("llm_new_term_namespace") or pd.NA
                ),
                "llm_escalated_from": (
                    proposal("llm_escalated_from") or pd.NA
                ),
            }
        )
    if not rows:
        return _empty_term_gap_result()
    return (
        pd.DataFrame(rows)[GAP_COLUMNS]
        .sort_values(
            "placement_confidence",
            ascending=False,
            na_position="last",
        )
        .reset_index(drop=True)
    )


def render_ontology_term_request(
    gaps: pd.DataFrame,
    scope: str = "auto",
    ask: bool = False,
    profile_name: Optional[str] = None,
    scope_overrides=None,
    issue_labels=None,
    term_request_template: str = TERM_REQUEST_DEFAULT_TEMPLATE,
    ontology_repo: str = SMN_REPO,
    gcdfo_term_request_template: str = GCDFO_TERM_REQUEST_DEFAULT_TEMPLATE,
    gcdfo_repo: str = GCDFO_REPO,
) -> pd.DataFrame:
    """
    Render curator-reviewed ontology term request drafts.

    Routing supports SMN, GCDFO, local profiles, uncertain rows, and skipped
    rows. Automatic routing considers namespace suggestions as evidence rather
    than authority and emits repository-specific issue bodies.

    Parameters
    ----------
    gaps
        Structured rows returned by :func:`detect_semantic_term_gaps`.
    scope
        Forced scope or ``"auto"``.
    ask
        Prompt for unresolved routing when ``True``.
    profile_name
        Required for non-interactive profile requests.
    scope_overrides
        Scalar or row-aligned explicit routing overrides.

    Returns
    -------
    pandas.DataFrame
        Gap rows with request scope, repository, template, title, and body.

    Notes
    -----
    This function only renders drafts. It never submits an issue.
    """
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
    if scope not in {"auto", "smn", "gcdfo", "profile", "uncertain", "skip"}:
        raise ValueError(
            "scope must be one of 'auto', 'smn', 'gcdfo', 'profile', "
            "'uncertain', or 'skip'."
        )

    def namespace_scope(value):
        text = _first_non_empty(value).lower()
        if not text:
            return None
        if (
            text in {"gcdfo", "dfo", "dfo-salmon-ontology"}
            or "w3id.org/gcdfo" in text
            or "dfo-salmon-ontology" in text
        ):
            return "gcdfo"
        if re.search(r"(^|[^a-z])smn([^a-z]|$)", text) or "w3id.org/smn" in text:
            return "smn"
        if text in {"profile", "local", "program", "organization"}:
            return "profile"
        return None

    if scope == "auto":
        df["request_scope"] = (
            df["placement_recommendation"].fillna("uncertain").astype(str).str.lower().str.strip()
        )
        if "llm_new_term_namespace" in df:
            namespace_evidence = df["llm_new_term_namespace"].map(
                namespace_scope
            )
            use_namespace = namespace_evidence.notna()
            df.loc[use_namespace, "request_scope"] = namespace_evidence.loc[
                use_namespace
            ]
    else:
        df["request_scope"] = scope
    if scope_overrides is not None:
        if isinstance(scope_overrides, str):
            df["request_scope"] = scope_overrides
        else:
            if len(scope_overrides) != len(df):
                raise ValueError("scope_overrides must be length 1 or len(gaps).")
            df["request_scope"] = list(scope_overrides)
    if ask:
        for idx, row in df.iterrows():
            if row["request_scope"] not in {"auto", "uncertain", ""}:
                continue
            term_label = _first_non_empty(row.get("top_non_smn_label"), row.get("search_query"), row.get("column_name"), default="Unnamed term")
            print(f"Term gap review: {term_label}")
            print("1. Request in shared SMN")
            print("2. Request in DFO-specific GCDFO")
            print("3. Request in local/program/organization profile")
            print("4. Skip for now")
            choice = input("Route this term request [1/2/3/4]: ").strip()
            df.at[idx, "request_scope"] = {
                "1": "smn",
                "2": "gcdfo",
                "3": "profile",
                "4": "skip",
            }.get(choice, "skip")
            if df.at[idx, "request_scope"] == "profile" and not profile_name:
                entered = input("Profile name [project-profile]: ").strip()
                profile_name = entered or "project-profile"
    profile_rows = df["request_scope"] == "profile"
    if profile_rows.any() and not ask and not _first_non_empty(profile_name):
        raise ValueError(
            "Non-interactive profile-scoped requests require profile_name."
        )
    df["request_scope"] = df["request_scope"].where(
        df["request_scope"].isin(["smn", "gcdfo", "profile"]),
        "skip",
    )
    df["profile_name"] = df["request_scope"].apply(lambda s: profile_name if s == "profile" else pd.NA)

    titles = []
    bodies = []
    for _, row in df.iterrows():
        term_label = _first_non_empty(
            row.get("llm_new_term_label"),
            row.get("top_non_smn_label"),
            row.get("target_label"),
            row.get("search_query"),
            row.get("column_name"),
            default="Unnamed term",
        )
        if row["request_scope"] == "smn":
            titles.append(f"Request new shared SMN term: {term_label}")
            target_repo = ontology_repo
            target_template = term_request_template
        elif row["request_scope"] == "gcdfo":
            titles.append(f"Request new GCDFO term: {term_label}")
            target_repo = gcdfo_repo
            target_template = gcdfo_term_request_template
        elif row["request_scope"] == "profile":
            titles.append(f"Request new {profile_name} profile term: {term_label}")
            target_repo = ontology_repo
            target_template = term_request_template
        else:
            titles.append(f"Skip term request: {term_label}")
            target_repo = ontology_repo
            target_template = term_request_template
        query_text = _first_non_empty(row.get("search_query"), row.get("column_name"), term_label)
        source = _first_non_empty(row.get("top_non_smn_source"), default="unknown")
        iri = _first_non_empty(row.get("top_non_smn_iri"), default="Not found")
        ontology = _first_non_empty(row.get("top_non_smn_ontology"), default="unknown")
        description = _first_non_empty(
            row.get("llm_new_term_definition"),
            row.get("target_description"),
            row.get("column_description"),
            default="No additional description captured.",
        )
        rationale = _first_non_empty(
            row.get("llm_rationale"),
            row.get("placement_rationale"),
            default="No rationale captured.",
        )
        dataset = _first_non_empty(row.get("dataset_id"), default="unknown")
        table = _first_non_empty(row.get("table_id"), default="unknown")
        column = _first_non_empty(row.get("column_name"), default="unknown")
        role = _first_non_empty(
            row.get("dictionary_role"),
            default="unknown",
        )
        target_field = _first_non_empty(
            row.get("target_sdp_field"),
            default="unknown",
        )
        detection = _first_non_empty(
            row.get("gap_detection_basis"),
            default="candidate_gap",
        )
        if row["request_scope"] == "smn":
            body = "\n".join(
                [
                    "## Suggested term label (required)",
                    "",
                    term_label,
                    "",
                    "## Definition (required)",
                    "",
                    description,
                    "",
                    "## Definition source (required)",
                    "",
                    f"Dataset evidence from `{dataset}` / `{table}` / `{column}`.",
                    "",
                    "## Proposed term type (required)",
                    "",
                    "- [ ] owl_class",
                    "- [ ] owl_object_property",
                    "- [ ] owl_datatype_property",
                    "- [ ] skos_concept",
                    "",
                    "## Suggested parent term(s)",
                    "",
                    "Curator decision required.",
                    "",
                    "## Synonyms (optional)",
                    "",
                    f"RELATED: Dataset query `{query_text}`",
                    "",
                    "## Suggested relationships / cross-references (optional)",
                    "",
                    f"Nearest external candidate: `{iri}` ({source}; {ontology})",
                    "",
                    "## Dataset context (optional but helpful)",
                    "",
                    f"- Dataset id: `{dataset}`",
                    f"- Table + column(s): `{table}` / `{column}`",
                    "- Example values: Not captured",
                    "",
                    "## I-ADOPT decomposition (for measurement-like terms)",
                    "",
                    "- property_iri:",
                    "- entity_iri:",
                    "- unit_iri:",
                    "- constraint_iri:",
                    "- method_iri:",
                    "",
                    "## Additional notes",
                    "",
                    f"- Target field: `{target_field}`",
                    f"- Semantic role: `{role}`",
                    f"- Gap evidence: `{detection}`",
                    f"- Rationale: {rationale}",
                    f"- Template: {target_template}",
                    f"- Repository: https://github.com/{target_repo}",
                ]
            )
        elif row["request_scope"] == "gcdfo":
            body = "\n".join(
                [
                    "Please provide as much information as you can:",
                    "",
                    f"* **Suggested term label (required):** {term_label}",
                    "",
                    f"* **Definition (required):** {description}",
                    "",
                    "* **Definition source (required):** "
                    f"Dataset evidence from `{dataset}` / `{table}` / `{column}`.",
                    "",
                    "* **Parent term(s):** Curator decision required.",
                    "",
                    "* **Children terms** (if applicable; should any existing "
                    "terms be moved underneath this new proposed term?): None proposed.",
                    "",
                    "* **Synonyms** (please specify EXACT, BROAD, NARROW or "
                    f"RELATED): RELATED: Dataset query `{query_text}`",
                    "",
                    f"* **Cross-references:** Nearest candidate: `{iri}` "
                    f"({source}; {ontology})",
                    "",
                    "* **Any other information:** "
                    f"Target field `{target_field}`; semantic role `{role}`; "
                    f"gap evidence `{detection}`. {rationale} "
                    f"Template: {target_template}. "
                    f"Repository: https://github.com/{target_repo}",
                ]
            )
        elif row["request_scope"] == "profile":
            body = "\n".join(
                [
                    "## Proposed profile term",
                    "",
                    f"- Profile: `{profile_name}`",
                    f"- Label: {term_label}",
                    f"- Definition: {description}",
                    f"- Dataset query: `{query_text}`",
                    f"- Target: `{dataset}` / `{table}` / `{column}` / `{role}`",
                    f"- Nearest candidate: `{iri}` ({source}; {ontology})",
                    f"- Rationale: {rationale}",
                    f"- New term template: {target_template}",
                ]
            )
        else:
            body = "\n".join(
                [
                    "## Skipped ontology term request",
                    "",
                    f"- Label: {term_label}",
                    f"- Dataset query: `{query_text}`",
                    f"- Rationale: {rationale}",
                ]
            )
        bodies.append(body)
        row_index = len(bodies) - 1
        df.loc[df.index[row_index], "ontology_repo"] = target_repo
        df.loc[df.index[row_index], "term_request_template"] = target_template
    df["request_title"] = titles
    df["request_body"] = bodies
    df["issue_labels"] = [issue_labels for _ in range(len(df))]
    return df


def submit_term_request_issues(
    requests_df: pd.DataFrame,
    repo: str = SMN_REPO,
    token: Optional[str] = None,
    dry_run: bool = True,
    confirm: bool = False,
) -> pd.DataFrame:
    """
    Preview or explicitly submit rendered ontology request issues.

    Parameters
    ----------
    requests_df
        Output from :func:`render_ontology_term_request`.
    repo
        Fallback repository when a request row has no routed repository.
    token
        GitHub token. Normal token discovery runs when omitted.
    dry_run
        Return a submission preview without network mutation.
    confirm
        Required for live submission. Each request also receives its own
        interactive confirmation.

    Returns
    -------
    pandas.DataFrame
        Per-request preview or submission status and issue identifiers.
    """
    df = pd.DataFrame(requests_df).copy()
    if df.empty:
        return pd.DataFrame()
    required = {"request_title", "request_body", "request_scope", "ontology_repo"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing required request columns: {sorted(missing)}")
    pending = df[df["request_scope"].isin(["smn", "gcdfo", "profile"])]
    if pending.empty:
        return pd.DataFrame()
    if dry_run:
        return pd.DataFrame(
            {
                "request_title": pending["request_title"].tolist(),
                "request_body": pending["request_body"].tolist(),
                "request_scope": pending["request_scope"].tolist(),
                "ontology_repo": pending["ontology_repo"].tolist(),
                "issue_number": [pd.NA] * len(pending),
                "issue_url": [pd.NA] * len(pending),
                "status": ["dry_run"] * len(pending),
            }
        )
    if not confirm:
        raise ValueError(
            "Live term-request submission requires confirm=True so every "
            "request receives explicit curator confirmation."
        )
    if token is None:
        from .github_io import _github_token

        token = _github_token()
    if not token:
        raise ValueError("No GitHub token available.")
    rows = []
    for _, row in pending.iterrows():
        answer = input(
            f"Submit {row['request_scope']} request: "
            f"{row['request_title']}? [y/N] "
        ).strip().lower()
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
        target_repo = _first_non_empty(row.get("ontology_repo"), repo)
        labels = row.get("issue_labels")
        if labels is None or (
            not isinstance(labels, (list, tuple)) and pd.isna(labels)
        ):
            labels = []
        elif isinstance(labels, str):
            labels = [labels]
        response = requests.post(
            f"https://api.github.com/repos/{target_repo}/issues",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            json={
                "title": row["request_title"],
                "body": row["request_body"],
                "labels": list(labels),
            },
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
