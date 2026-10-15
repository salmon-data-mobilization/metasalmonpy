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
import xml.etree.ElementTree as ET
from typing import Dict, List, Mapping, Optional, Tuple, Union

try:
    import pandas as pd
except ImportError as exc:  # pragma: no cover - import guard
    raise ImportError("metasalmonpy requires pandas; install via `pip install pandas`.") from exc


_SMN_MODULE_BASE = "https://w3id.org/smn/modules"

# The namespaces the RDF/XML readers name, by URI. A document's own prefixes
# are the serializer's choice (the smn 0.0.3 release binds the OBO namespace to
# `ns1`), so the readers match a child's namespace, never its prefix. Mirrors
# metasalmon's `.ms_rdfxml_ns()`.
_RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
_RDFS_NS = "http://www.w3.org/2000/01/rdf-schema#"
_OWL_NS = "http://www.w3.org/2002/07/owl#"
_SKOS_NS = "http://www.w3.org/2004/02/skos/core#"
_OBO_NS = "http://purl.obolibrary.org/obo/"
_DCTERMS_NS = "http://purl.org/dc/terms/"

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
        # A module may legitimately contribute zero rows -- the RDA
        # profile-bridge modules (08, 09) hold foreign-subject statements
        # only, per smn CONVENTIONS 5b, and R indexes them to zero rows too.
        # But every real Turtle module declares prefixes, so a non-empty body
        # without a single @prefix is not Turtle (an error page served with
        # 200, or a format change) and silently skipping it would drop every
        # term it carries while the aggregate still looks healthy.
        if not re.search(r"^\s*@prefix\s", text, flags=re.MULTILINE):
            raise RuntimeError(
                f"SMN module '{module_name}' returned non-Turtle content; "
                "refusing to build a silently partial term index."
            )
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

            rows.append(
                _smn_index_row(
                    iri=iri,
                    type_iris=type_iris,
                    labels=labels,
                    alt_labels=alt_labels,
                    definition=definition,
                    in_scheme=in_scheme,
                    parents=parents,
                    module_name=module_name,
                    search_module=module_name,
                )
            )

    if not rows:
        return _smn_index_empty()

    frame = pd.DataFrame(rows, columns=list(SMN_INDEX_COLUMNS))
    return frame.drop_duplicates(subset=["iri"], keep="first").reset_index(drop=True)


def _smn_index_row(
    *,
    iri: str,
    type_iris: List[str],
    labels: List[str],
    alt_labels: List[str],
    definition: List[str],
    in_scheme: List[str],
    parents: List[str],
    module_name: str,
    search_module: Optional[str] = None,
) -> dict:
    """One row of the smn term index, and the one place its role hints are emitted.

    Both smn readers build their rows here -- the module reader for the latest
    ontology and the release reader for a pinned one -- so a term reaches
    ranking and the role-type validator with the same hints whichever way smn
    was read. ``module_name`` is the evidence ``_smn_role_flags()`` reads;
    ``search_module`` is the module name the module reader also folds into
    ``search_text`` (the release reader has none to fold). Mirrors metasalmon's
    ``.smn_index_row()``.
    """
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
    search_parts = [
        label,
        " ".join(alt_labels),
        definition_text,
        " ".join(in_scheme),
        " ".join(parents),
        _smn_subject_local_name(iri),
    ]
    if search_module is not None:
        search_parts.append(search_module)

    return {
        "iri": iri,
        "label": label,
        "alt_labels": " | ".join(alt_labels),
        "definition": definition_text,
        "resource_kind": resource_kind if resource_kind is not None else "Resource",
        "in_scheme": " | ".join(in_scheme),
        "parent_iris": " | ".join(parents),
        "type_iris": " | ".join(type_iris),
        "search_text": " ".join(search_parts).lower(),
        "is_variable": bool(role_flags["is_variable"]),
        "is_property": bool(role_flags["is_property"]),
        "is_entity": bool(role_flags["is_entity"]),
        "is_constraint": bool(role_flags["is_constraint"]),
        "is_method": bool(role_flags["is_method"]),
        "is_statistical_modifier": bool(role_flags["is_statistical_modifier"]),
        "role_hints": _smn_role_hints(role_flags),
    }


# The type every individual in a release carries because the serializer
# declares it, and no smn module asserts it. Kept as type evidence, its word
# "individual" gave every SKOS concept an entity hint.
_SERIALIZER_TYPES = (f"{_OWL_NS}NamedIndividual",)

# What `_smn_release_index()` gathers for a subject: the element that names its
# value, and whether the value is a resource (``rdf:resource``) or a literal.
_RELEASE_PREDICATES: Tuple[Tuple[str, str, bool], ...] = (
    ("type", f"{{{_RDF_NS}}}type", True),
    ("label", f"{{{_RDFS_NS}}}label", False),
    ("pref_label", f"{{{_SKOS_NS}}}prefLabel", False),
    ("alt_label", f"{{{_SKOS_NS}}}altLabel", False),
    ("iao_definition", f"{{{_OBO_NS}}}IAO_0000115", False),
    ("skos_definition", f"{{{_SKOS_NS}}}definition", False),
    ("comment", f"{{{_RDFS_NS}}}comment", False),
    ("in_scheme", f"{{{_SKOS_NS}}}inScheme", True),
    ("sub_class_of", f"{{{_RDFS_NS}}}subClassOf", True),
    ("broader", f"{{{_SKOS_NS}}}broader", True),
    ("equivalent_class", f"{{{_OWL_NS}}}equivalentClass", True),
    ("sub_property_of", f"{{{_RDFS_NS}}}subPropertyOf", True),
)


def _element_iri(tag: str) -> str:
    """The IRI an element's ElementTree tag names: its namespace and local name."""
    if tag.startswith("{"):
        namespace, _, local = tag[1:].partition("}")
        return namespace + local
    return tag


def _release_literal(element: "ET.Element") -> str:
    """An element's text, its whitespace runs collapsed as metasalmon collapses them.

    R's ``gsub("\\s+", " ", perl = TRUE)`` matches ASCII whitespace only, and
    ``trimws()`` then trims the spaces left at either end.
    """
    return re.sub(r"[ \t\n\r\f\v]+", " ", "".join(element.itertext())).strip(" ")


def _smn_release_index(xml_text: Union[str, bytes]) -> pd.DataFrame:
    """The smn term index of one release snapshot, read from its RDF/XML.

    A release is one merged graph, ``docs/releases/<version>/smn.owl`` in the
    salmon-domain-ontology repository, so it has no modules for
    ``parse_smn_ttl_modules()`` to read, and its Turtle is a serializer's
    output, which that reader's line-based parse does not read (it found no
    terms in the 0.0.3 release). The RDF/XML is read with an XML parser and
    every row is built by ``_smn_index_row()``, so the terms carry the hints
    the module reader gives them. Two things differ between a release and the
    modules, and each is read so that the hints still agree:

    * The serializer declares every individual ``owl:NamedIndividual``, which no
      smn module asserts. The declaration is left out of the type evidence.
    * A release says nothing about which module a term came from. smn keeps its
      shared SKOS schemes and concepts, and only those, in
      ``07-controlled-vocabularies`` (its ``ontology/modules/README.md``, and
      Layer B of its CONVENTIONS.md), so a term typed ``skos:Concept`` or
      ``skos:ConceptScheme`` is read as that module's. The only other module
      the hints read is ``01-entity-systematics``, whose entity hint rests on
      the module alone, so a release cannot reproduce it.

    Measured on the smn main branch at 0e42037 (its modules against its own
    merged build): 126 of 129 shared terms carry the same hints. The other
    three are GeographicFeature, which loses the hint ``01-entity-systematics``
    gave it, and YearBasis and AgeNotation, whose label and definition sit in a
    module the module reader drops as a duplicate block. Mirrors metasalmon's
    ``.smn_release_index()``, and the two are held to one fixture.
    """
    root = ET.fromstring(xml_text)
    if root.tag != f"{{{_RDF_NS}}}RDF":
        return _smn_index_empty()

    about_attr = f"{{{_RDF_NS}}}about"
    resource_attr = f"{{{_RDF_NS}}}resource"
    description_tag = f"{{{_RDF_NS}}}Description"
    fields: Dict[str, Dict[str, List[str]]] = {}

    for node in root:
        iri = node.attrib.get(about_attr)
        if not iri or not re.search(r"^https?://w3id\.org/smn/", iri):
            continue
        # One subject can be spread over several nodes; its values are gathered
        # predicate by predicate, in the order the module reader reads them.
        gathered = fields.setdefault(iri, {name: [] for name, _, _ in _RELEASE_PREDICATES})
        # A typed node element (`<owl:Class rdf:about=...>`) states its type;
        # `rdf:Description` states none.
        if node.tag != description_tag:
            gathered["type"].append(_element_iri(node.tag))
        for name, tag, is_resource in _RELEASE_PREDICATES:
            for child in node:
                if child.tag != tag:
                    continue
                if is_resource:
                    value = child.attrib.get(resource_attr)
                else:
                    value = _release_literal(child)
                if value:
                    gathered[name].append(value)

    rows: List[dict] = []
    for iri, gathered in fields.items():
        type_iris = [
            value
            for value in _unique_preserving_order(gathered["type"])
            if value not in _SERIALIZER_TYPES
        ]
        if f"{_OWL_NS}Ontology" in type_iris:
            continue
        resource_kind = _smn_resource_kind(type_iris)
        module_name = (
            "07-controlled-vocabularies"
            if resource_kind in ("Concept", "ConceptScheme")
            else ""
        )
        rows.append(
            _smn_index_row(
                iri=iri,
                type_iris=type_iris,
                labels=_unique_preserving_order(gathered["label"] + gathered["pref_label"]),
                alt_labels=_unique_preserving_order(gathered["alt_label"]),
                definition=_unique_preserving_order(
                    gathered["iao_definition"] + gathered["skos_definition"] + gathered["comment"]
                ),
                in_scheme=_unique_preserving_order(gathered["in_scheme"]),
                parents=_unique_preserving_order(
                    gathered["sub_class_of"]
                    + gathered["broader"]
                    + gathered["equivalent_class"]
                    + gathered["sub_property_of"]
                ),
                module_name=module_name,
            )
        )

    if not rows:
        return _smn_index_empty()
    return pd.DataFrame(rows, columns=list(SMN_INDEX_COLUMNS))


__all__ = ["SMN_INDEX_COLUMNS", "parse_smn_ttl_modules"]
