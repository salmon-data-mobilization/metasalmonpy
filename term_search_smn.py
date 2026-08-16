"""
Salmon Domain Ontology (SMN) module indexing helpers.

Mirrors metasalmon's ``R/term_search_smn.R``. The shared SMN root
(``https://w3id.org/smn/``) is the canonical entrypoint for the latest
ontology. For lightweight lexical search we index the canonical module IRIs
under ``https://w3id.org/smn/modules/...``, which currently remain
Turtle-first on W3ID (the W3ID redirects resolve to the
``salmon-data-mobilization/salmon-domain-ontology`` repository).

Turtle parsing is deliberately regex/line based to mirror the R
implementation exactly; neither package uses an RDF library for this.
"""

from __future__ import annotations

import re
from typing import Dict, List, Mapping, Optional, Tuple

try:
    import pandas as pd
except ImportError as exc:  # pragma: no cover - import guard
    raise ImportError("metasalmonpy requires pandas; install via `pip install pandas`.") from exc


_SMN_MODULE_BASE = "https://w3id.org/smn/modules"

# Column contract shared with the gcdfo index and consumed by
# term_search._filter_local_index. Mirrors R's parsed index tibbles, plus the
# `is_statistical_modifier` flag column (sdp-0.3.0 forward-compatibility; in R
# the TTL parse carries the flag only inside `role_hints`).
SMN_INDEX_COLUMNS: Tuple[str, ...] = (
    "iri",
    "label",
    "alt_labels",
    "definition",
    "resource_kind",
    "in_scheme",
    "parent_iris",
    "type_iris",
    "search_text",
    "is_variable",
    "is_property",
    "is_entity",
    "is_constraint",
    "is_method",
    "is_statistical_modifier",
    "role_hints",
)


def _smn_module_urls() -> List[str]:
    """Canonical SMN module URLs, in the same order as R's `.smn_module_urls()`."""
    return [
        f"{_SMN_MODULE_BASE}/01-entity-systematics",
        f"{_SMN_MODULE_BASE}/02-observation-measurement",
        f"{_SMN_MODULE_BASE}/03-assessment-benchmarks",
        f"{_SMN_MODULE_BASE}/04-management-governance",
        f"{_SMN_MODULE_BASE}/05-provenance-quality",
        f"{_SMN_MODULE_BASE}/06-data-interoperability",
        f"{_SMN_MODULE_BASE}/07-controlled-vocabularies",
        f"{_SMN_MODULE_BASE}/08-rda-case-study-profile-bridges",
        f"{_SMN_MODULE_BASE}/09-rda-neville-decomposition-profile-bridges",
        f"{_SMN_MODULE_BASE}/alignment-main",
        f"{_SMN_MODULE_BASE}/alignment-research",
    ]


def _smn_ttl_prefixes(text: str) -> Dict[str, str]:
    """Extract `@prefix name: <iri> .` declarations from a Turtle document."""
    prefixes: Dict[str, str] = {}
    for line in text.split("\n"):
        match = re.match(r"^\s*@prefix\s+([A-Za-z][A-Za-z0-9_-]*):\s*<([^>]+)>\s*\.", line)
        if match:
            prefixes[match.group(1)] = match.group(2)
    return prefixes


def _smn_expand_curie(term: str, prefixes: Mapping[str, str]) -> Optional[str]:
    """Expand a CURIE (or unwrap an IRI) using the document's prefix map."""
    term = term.strip()
    if not term:
        return None
    if term.startswith("<") and term.endswith(">"):
        return term[1:-1]
    if ":" not in term:
        return term

    prefix, _, local = term.partition(":")
    base = prefixes.get(prefix)
    if not base:
        return term
    return base + local


def _smn_literal_values(text: str) -> List[str]:
    """Extract quoted literal values (dropping any language tag)."""
    values = re.findall(r'"[^"]+"(?:@[A-Za-z-]+)?', text)
    out = []
    for value in values:
        value = re.sub(r'^"', "", value)
        value = re.sub(r'"(@[A-Za-z-]+)?$', "", value)
        out.append(value)
    return out


def _smn_term_values(text: str, prefixes: Mapping[str, str]) -> List[str]:
    """Extract IRIs and CURIEs from a predicate value chunk, expanded to IRIs."""
    values = re.findall(r"(<[^>]+>|[A-Za-z][A-Za-z0-9_-]*:[^\s,;]+)", text)
    out = []
    for value in values:
        expanded = _smn_expand_curie(value, prefixes)
        if expanded:
            out.append(expanded)
    return out


def _smn_predicate_chunks(rest: str, predicate: str) -> List[str]:
    """Return the value chunk following each occurrence of `predicate`."""
    pattern = r"(?:^|;)\s*" + re.escape(predicate) + r"\s+([^;]+)"
    return [chunk.strip() for chunk in re.findall(pattern, rest)]


def _smn_subject_local_name(iri: Optional[str]) -> str:
    if not iri:
        return ""
    return re.sub(r"^.*/", "", iri)


def _smn_resource_kind(type_iris: List[str]) -> Optional[str]:
    types = [t.lower() for t in type_iris]
    if any(t.endswith("skos/core#conceptscheme") for t in types):
        return "ConceptScheme"
    if any(t.endswith("skos/core#concept") for t in types):
        return "Concept"
    if any(t.endswith("owl#namedindividual") for t in types):
        return "NamedIndividual"
    if any(t.endswith("owl#objectproperty") for t in types):
        return "ObjectProperty"
    if any(t.endswith("owl#dataproperty") for t in types):
        return "DataProperty"
    if any(t.endswith("owl#annotationproperty") for t in types):
        return "AnnotationProperty"
    if any(t.endswith("owl#class") for t in types):
        return "Class"
    return None


def _smn_role_flags(
    label: str,
    definition: str,
    resource_kind: Optional[str],
    module_name: str,
    in_scheme: str,
    parent_iris: str,
    type_iris: str,
    iri: str,
) -> Dict[str, bool]:
    """Port of R's `.smn_role_flags` (metasalmon 0.3.0, incl. statistical modifiers)."""
    local_name = _smn_subject_local_name(iri)
    subject_text = " ".join(
        [
            label or "",
            resource_kind or "",
            in_scheme or "",
            parent_iris or "",
            type_iris or "",
            local_name,
        ]
    ).lower()
    evidence_text = f"{subject_text} {definition or ''}".lower()

    # Treat only the vocabulary container itself as a scheme. A SKOS concept's
    # `inScheme` value is evidence about the concept, not evidence that the
    # concept is a ConceptScheme.
    is_scheme = (
        (resource_kind or "").lower() == "conceptscheme"
        or bool(re.search(r"\bscheme$", (label or "").strip().lower()))
        or bool(re.search(r"scheme$", local_name.lower()))
    )

    # Role exclusions describe the term itself, so evaluate them against its
    # label/type/parents rather than incidental words in a prose definition.
    entity_exclusion = (
        bool(
            re.search(
                r"measurement|benchmark|reference point|procedure|method|characteristic|property",
                subject_text,
            )
        )
        or bool(re.search(r"\bstock assessment\b", subject_text))
        or bool(re.search(r"\b(phase|context|origin)\b", subject_text))
    )
    is_entity = (
        (
            "entity-systematics" in module_name
            or bool(
                re.search(
                    r"entity|population|stock|river|habitat|taxon|organism|individual|group|stratum|species",
                    subject_text,
                )
            )
        )
        and not is_scheme
        and not entity_exclusion
    )
    is_property = bool(
        re.search(r"property|characteristic|length|weight|size|status|confidence|phase", subject_text)
    )
    if "sosa/property" in subject_text:
        is_property = True
    is_method = bool(re.search(r"method|procedure|protocol|enumeration", subject_text))
    if "sosa/procedure" in subject_text:
        is_method = True
    is_constraint = "controlled-vocabularies" in module_name or bool(
        re.search(
            r"constraint|context|phase|origin|benchmark|reference point|target|limit|status zone",
            subject_text,
        )
    )
    # sdp-0.3.0: statistical modifiers are their own I-ADOPT component, and smn
    # 0.0.3 gives them a scheme of their own. Without this hint every real
    # modifier concept reaches review carrying only the broad
    # "controlled-vocabularies" constraint hint.
    is_statistical_modifier = bool(
        re.search(r"statisticalmodifier|statistical modifier", subject_text)
    ) or (
        # Token fallback only inside the controlled-vocabulary module, so a
        # variable named TotalRunSize does not pick up a modifier hint.
        "controlled-vocabularies" in module_name
        and bool(
            re.search(
                r"\b(mean|median|average|maximum|minimum|total|cumulative|peak)\b",
                subject_text,
            )
        )
    )
    is_variable = (
        bool(re.search(r"measurement|abundance|count|rate|escapement|recruit", subject_text))
        or (
            bool(re.search(r"measurement datum|abundance|count|rate|escapement", evidence_text))
            and bool(re.search(r"observedrateorabundance|measurement", subject_text))
        )
    ) and not bool(re.search(r"context|scheme|benchmark|reference point", subject_text))

    return {
        "is_variable": is_variable,
        "is_property": is_property,
        "is_entity": is_entity,
        "is_constraint": is_constraint,
        "is_method": is_method,
        "is_statistical_modifier": is_statistical_modifier,
    }


def _smn_role_hints(role_flags: Mapping[str, bool]) -> str:
    hints = [
        hint
        for flag, hint in (
            ("is_variable", "variable"),
            ("is_property", "property"),
            ("is_entity", "entity"),
            ("is_constraint", "constraint"),
            ("is_method", "method"),
            ("is_statistical_modifier", "statistical_modifier"),
        )
        if role_flags.get(flag)
    ]
    return "|".join(hints)


def _smn_index_empty() -> pd.DataFrame:
    return pd.DataFrame(columns=list(SMN_INDEX_COLUMNS))


def _unique_preserving_order(values: List[str]) -> List[str]:
    return list(dict.fromkeys(values))


def parse_smn_ttl_modules(texts: Mapping[str, str]) -> pd.DataFrame:
    """
    Parse SMN Turtle modules into the shared term-index frame.

    Parameters
    ----------
    texts
        Mapping of module URL (or reference) to the module's Turtle text.
        The URL basename becomes the module name used for role hints.

    Returns
    -------
    pandas.DataFrame
        One row per subject, deduplicated on ``iri`` (first occurrence wins),
        with the columns in ``SMN_INDEX_COLUMNS``. Row order follows module
        and document order, so the result is a pure, deterministic function
        of the input texts.
    """
    rows: List[dict] = []

    for module_reference, text in texts.items():
        if not text:
            continue

        module_name = re.sub(r"/+$", "", str(module_reference)).rsplit("/", 1)[-1]
        prefixes = _smn_ttl_prefixes(text)
        stripped = re.sub(r"^\s*#.*$", "", text, flags=re.MULTILINE)
        stripped = re.sub(r"^\s*@prefix\s+.*$", "", stripped, flags=re.MULTILINE)
        blocks = [block.strip() for block in re.split(r"\n\s*\n+", stripped)]
        blocks = [block for block in blocks if block]

        for block in blocks:
            collapsed = re.sub(r"\s+", " ", block.strip())
            if not collapsed:
                continue

            subject = re.sub(r"\s.*$", "", collapsed)
            if not subject.startswith("smn:"):
                continue

            iri = _smn_expand_curie(subject, prefixes)
            if not iri or not re.search(r"^https?://w3id\.org/smn/", iri):
                continue

            rest = re.sub(r"^\S+\s+", "", collapsed, count=1)
            type_iris = _unique_preserving_order(
                [
                    value
                    for chunk in _smn_predicate_chunks(rest, "a")
                    for value in _smn_term_values(chunk, prefixes)
                ]
            )
            labels = _unique_preserving_order(
                [
                    value
                    for chunk in _smn_predicate_chunks(rest, "rdfs:label")
                    for value in _smn_literal_values(chunk)
                ]
                + [
                    value
                    for chunk in _smn_predicate_chunks(rest, "skos:prefLabel")
                    for value in _smn_literal_values(chunk)
                ]
            )
            alt_labels = _unique_preserving_order(
                [
                    value
                    for chunk in _smn_predicate_chunks(rest, "skos:altLabel")
                    for value in _smn_literal_values(chunk)
                ]
            )
            definition = _unique_preserving_order(
                [
                    value
                    for predicate in ("iao:0000115", "skos:definition", "rdfs:comment")
                    for chunk in _smn_predicate_chunks(rest, predicate)
                    for value in _smn_literal_values(chunk)
                ]
            )
            in_scheme = _unique_preserving_order(
                [
                    value
                    for chunk in _smn_predicate_chunks(rest, "skos:inScheme")
                    for value in _smn_term_values(chunk, prefixes)
                ]
            )
            parents = _unique_preserving_order(
                [
                    value
                    for predicate in (
                        "rdfs:subClassOf",
                        "skos:broader",
                        "owl:equivalentClass",
                        "rdfs:subPropertyOf",
                    )
                    for chunk in _smn_predicate_chunks(rest, predicate)
                    for value in _smn_term_values(chunk, prefixes)
                ]
            )

            label = labels[0] if labels else _smn_subject_local_name(iri)
            definition_text = " | ".join(definition) if definition else ""
            resource_kind = _smn_resource_kind(type_iris)
            role_flags = _smn_role_flags(
                label=label,
                definition=definition_text,
                resource_kind=resource_kind,
                module_name=module_name,
                in_scheme=" | ".join(in_scheme),
                parent_iris=" | ".join(parents),
                type_iris=" | ".join(type_iris),
                iri=iri,
            )
            search_text = " ".join(
                [
                    label,
                    " ".join(alt_labels),
                    definition_text,
                    " ".join(in_scheme),
                    " ".join(parents),
                    _smn_subject_local_name(iri),
                    module_name,
                ]
            ).lower()

            rows.append(
                {
                    "iri": iri,
                    "label": label,
                    "alt_labels": " | ".join(alt_labels),
                    "definition": definition_text,
                    "resource_kind": resource_kind if resource_kind is not None else "Resource",
                    "in_scheme": " | ".join(in_scheme),
                    "parent_iris": " | ".join(parents),
                    "type_iris": " | ".join(type_iris),
                    "search_text": search_text,
                    "is_variable": bool(role_flags["is_variable"]),
                    "is_property": bool(role_flags["is_property"]),
                    "is_entity": bool(role_flags["is_entity"]),
                    "is_constraint": bool(role_flags["is_constraint"]),
                    "is_method": bool(role_flags["is_method"]),
                    "is_statistical_modifier": bool(role_flags["is_statistical_modifier"]),
                    "role_hints": _smn_role_hints(role_flags),
                }
            )

    if not rows:
        return _smn_index_empty()

    frame = pd.DataFrame(rows, columns=list(SMN_INDEX_COLUMNS))
    return frame.drop_duplicates(subset=["iri"], keep="first").reset_index(drop=True)


__all__ = ["SMN_INDEX_COLUMNS", "parse_smn_ttl_modules"]
