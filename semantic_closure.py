"""THE REVIEWED SEMANTIC CLOSURE: A PRODUCER (metasalmon backlog #116, hub B-165).

``write_eml_from_sdp()`` and ``publish_sdp_to_knb()`` both require two files
that this package validated in three places and wrote in none:

  ``metadata/semantic_vocabulary.csv``   one evidence row per canonical
                                        MEASUREMENT IRI, each row carrying its
                                        own ``reviewed_snapshot_sha256``
  ``reviewed_semantic_selections.csv``   exactly one ``accepted`` row per
                                        canonical REVIEW TARGET

THE TWO SETS ARE DIFFERENT SETS, and that is why this producer derives both
rather than deriving one and reasoning the other from it. The measurement set
is IRIs the EML measurement and method paths emit, so it includes
code-resolved ``sosa:usedProcedure`` IRIs and excludes a table's
``observation_unit_iri``. The review-target set is slots a reviewer decided, so
it includes ``observation_unit_iri`` and excludes a code-resolved procedure,
which no reviewer ever selected as a slot. In the bundled EML fixture the
difference is exactly one row: ``smn:Observation`` is a review target and not a
vocabulary term.

GAP, NOT ABORT -- ruled by Brett 2026-09-12, for both implementations. An IRI
the searched sources answered about and do not have becomes a row in
:func:`detect_semantic_term_gaps` shape and both files are still written.
Aborting would hand the user a failure and nothing to file; a gap is what
:func:`render_ontology_term_request` and :func:`submit_term_request_issues`
already consume, so the unresolvable case leaves the pipeline rather than
dead-ending in it. An omitted vocabulary row is *not* silent: ``eml``'s
``_read_vocabulary()`` then names the same IRI when export is attempted, so the
two messages agree.

AND A GAP IS A CLAIM, so only one of the three ways a row can go unwritten is
allowed to make one:

  the sources did not answer   ABORT, before anything is written. A degraded
                               lookup is unknown -- ``find_terms()`` says so in
                               its own warning -- and the remedy is to re-run.
  the term was FOUND with a    REPORT in ``incomplete`` and warn, naming the
  required field blank         field and the slot. The term exists; what is
                               missing is evidence about it.
  every source answered and    GAP. This one, and only this one.
  none has the term

NO LLM REACHES THIS PATH. ``find_terms()`` is deterministic ontology search;
there is no ``llm_assess`` argument here and nothing below constructs an LLM
request. Pinned by ``tests/test_semantic_closure.py``, which runs the whole
producer with the provider call bound to a raising stub.

Two Python-native choices, neither of which changes what the call does. Input
problems raise ``ValueError`` (which ``SdpExtensionError`` already subclasses,
so the path refusals stay catchable both ways) and a degraded lookup raises
``RuntimeError``, because it is an environmental failure a caller retries
rather than a bad argument; R signals both through one cli condition class
because R has no such distinction to draw. Reports are Python warnings of the
``RuntimeWarning`` class ``find_terms()`` already uses.
"""

from __future__ import annotations

import hashlib
import os
import re
import warnings
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Union

import pandas as pd

from .eml import (
    _VOCABULARY_SNAPSHOT_FIELDS,
    _as_character,
    _canonical_measurement_iris,
    _canonical_review_targets,
    _is_missing,
    _vocabulary_snapshot_sha256,
)
from .metadata import read_sdp_csv
from .package_io import read_salmon_datapackage
from .sdp_methods import (
    SdpExtensionError,
    _assert_safe_directory,
    _assert_safe_file,
    _atomic_write_set,
    _csv_bytes,
    _extension_root,
)
from .term_requests import GAP_COLUMNS, _namespace_scope
from .term_search import _search_failed_sources, find_terms
from .text_safety import redact_secrets

# The vocabulary evidence fields, in the order the digest hashes them. The order
# is load-bearing: the digest is over these values joined by "\r", so a reorder
# silently changes every hash the package writes.
#
# DERIVED, NOT RESTATED. R keeps its own vector and pins it against the
# verifier's with a test that evaluates the verifier's body, because R has no
# way to import a constant out of a function. Python does, so the producer reads
# the verifier's own tuple and the two cannot diverge at all. That is a language
# difference, not a behaviour one.
_VOCABULARY_FIELDS: Tuple[str, ...] = tuple(_VOCABULARY_SNAPSHOT_FIELDS)

# The subset ``eml._read_vocabulary()`` refuses to accept empty. ``type_iris``
# and ``source_artifact_sha256`` are deliberately absent: a live w3id resolution
# is not a pinned release artifact, so it has no artifact digest.
_REQUIRED_VOCABULARY_FIELDS: Tuple[str, ...] = (
    "label",
    "definition",
    "source",
    "ontology",
    "resource_kind",
    "native_type",
    "source_url",
)

# Of those, the six ``find_terms()`` returns. ``native_type`` and ``source_url``
# describe the ontology ARTIFACT rather than the term, and nothing on the
# retrieval path records them, so they are derived below or hand-supplied.
_SEARCH_EVIDENCE_FIELDS: Tuple[str, ...] = (
    "label",
    "definition",
    "source",
    "ontology",
    "resource_kind",
    "type_iris",
)

_LEDGER_FIELDS: Tuple[str, ...] = (
    "dataset_id",
    "table_id",
    "column_name",
    "target_scope",
    "target_sdp_field",
    "dictionary_role",
    "decision",
    "confidence",
    "review_rationale",
    "iri",
)

_TARGET_FIELDS: Tuple[str, ...] = (
    "dataset_id",
    "table_id",
    "column_name",
    "target_scope",
    "target_sdp_field",
    "dictionary_role",
    "iri",
)

# Columns a caller may hand-supply through ``evidence``.
_EVIDENCE_FIELDS: Tuple[str, ...] = tuple(
    [field for field in _VOCABULARY_FIELDS if field != "iri"]
    + ["confidence", "review_rationale"]
)

# The gap table this producer returns: the :func:`detect_semantic_term_gaps`
# shape plus the IRI that could not be resolved. That column is an addition
# rather than a reuse of ``top_non_smn_iri``, which means "the best candidate
# another source returned" and is empty here precisely because nothing was
# returned. ``render_ontology_term_request()`` reads named fields and ignores
# the extra one.
_GAP_COLUMNS: Tuple[str, ...] = tuple(list(GAP_COLUMNS) + ["unresolved_iri"])

_INCOMPLETE_COLUMNS: Tuple[str, ...] = (
    "dataset_id",
    "table_id",
    "column_name",
    "target_scope",
    "target_sdp_field",
    "dictionary_role",
    "iri",
    "missing_fields",
    "resolved_source",
    "search_query",
    "searched_role",
)

_DEFAULT_SOURCES: Tuple[str, ...] = ("smn", "gcdfo")

_DEFAULT_MAPPING_PATHS: Dict[str, str] = {
    "vocabulary": "metadata/semantic_vocabulary.csv",
    "review": "reviewed_semantic_selections.csv",
}

_MAPPING_RELATIVE_PATH = "metadata/eml-mapping.yml"

# THE `REVIEW REQUIRED:` MARKER IS A GUARD, so it says what would retire it.
# It satisfies the ledger's non-empty ``review_rationale`` check without
# asserting that a human judged anything, which is the only honest thing to
# write when nothing recorded a rationale: inventing one would fabricate the one
# purely human part of the ledger, and leaving the cell empty would fail a
# validator whose complaint names the wrong problem. The marker and the warning
# are the only signals, deliberately -- judging a rationale is not a validator's
# job, so no check here can tell a real one from a plausible one.
#
# *Retires when:* every accepted slot has a recorded rationale to read, so a
# target without one is a defect rather than a row to mark. Concretely: when
# ``accept_suggestion()`` requires a ``decision_reason`` and
# ``apply_sdp_semantics()`` carries it through to ``semantic_suggestions.csv``
# for every accepted row, this branch becomes unreachable and the marker, the
# ``placeholders`` return value and the warning all go together, replaced by an
# error naming the slot.
_PLACEHOLDER_RATIONALE = (
    "REVIEW REQUIRED: record why this IRI was selected for this slot. "
    "Supply it as `review_rationale` through the `evidence` argument of "
    "write_sdp_semantic_closure(), or record a decision reason with "
    "accept_suggestion() before applying it."
)


def _require_yaml():
    """PyYAML, or an error naming the extra that carries it.

    Narrower than ``eml._require_eml_extra()`` on purpose: this producer never
    validates a document against the XSD set, so lxml is not its business. Only
    reached when the sidecar exists, so a package without one needs no extra at
    all -- which is what keeps this callable in the core-dependency CI leg.

    Falling back to the default paths when PyYAML is absent was the alternative
    and is wrong: the sidecar's declared paths are the only thing that stops
    this writing a file the sidecar does not point at, so guessing them is
    exactly the failure the declared-path read exists to prevent.
    """
    try:
        import yaml  # noqa: F401
    except ImportError:
        raise ImportError(
            "write_sdp_semantic_closure needs PyYAML to read the declared "
            "paths out of metadata/eml-mapping.yml. Install it with: "
            'pip install "metasalmonpy[eml]".'
        ) from None
    return yaml


# The artifact each searchable source resolves, which is what ``source_url``
# records. These are the two URLs the smn and gcdfo indexes fetch, so a source
# whose fetch URL changes changes here too.
#
# *Retires when:* ``find_terms()`` returns the artifact URL it resolved, at
# which point reading it back is strictly better than restating it here.
def _source_url(source: object) -> str:
    text = _closure_text(source).lower()
    if text == "smn":
        return "https://w3id.org/smn/"
    if text == "gcdfo":
        return "https://w3id.org/gcdfo/salmon"
    return ""


# ``native_type`` is the term's own RDF type in CURIE form. ``resource_kind`` is
# the RDF/XML element name the index parsed, so this is a rename rather than an
# inference.
_NATIVE_TYPES: Dict[str, str] = {
    "class": "owl:Class",
    "owlclass": "owl:Class",
    "owl_class": "owl:Class",
    "namedindividual": "owl:NamedIndividual",
    "objectproperty": "owl:ObjectProperty",
    "owl_object_property": "owl:ObjectProperty",
    "datatypeproperty": "owl:DatatypeProperty",
    "concept": "skos:Concept",
    "skosconcept": "skos:Concept",
    "skos_concept": "skos:Concept",
    "conceptscheme": "skos:ConceptScheme",
}


def _native_type(resource_kind: object) -> str:
    kind = _closure_text(resource_kind)
    if not kind:
        return ""
    return _NATIVE_TYPES.get(kind.lower(), "owl:" + kind)


def _iterable_values(value: object) -> List[object]:
    """Flatten one cell into the elements R's ``unlist()`` would see."""
    if value is None:
        return []
    if isinstance(value, pd.Series):
        return list(value)
    if isinstance(value, (list, tuple, set)):
        return list(value)
    if isinstance(value, pd.DataFrame):
        return [cell for _, column in value.items() for cell in column]
    if hasattr(value, "tolist") and not isinstance(value, (str, bytes)):
        return list(value.tolist())
    return [value]


def _closure_text(value: object, default: str = "") -> str:
    """ONE VALUE, RENDERED ONCE.

    Every cell that reaches the closure files goes through here, so the sort
    key, the digest input and the emitted byte are the same string. A
    multi-element value is joined rather than repr'd: rendering a list cell with
    ``str()`` would write Python source text into the CSV.
    """
    values = []
    for element in _iterable_values(value):
        if _is_missing(element):
            continue
        text = _as_character(element).strip()
        if text:
            values.append(text)
    if not values:
        return default
    return ";".join(values)


def _closure_column(values: object) -> List[str]:
    """Blank out missing and trim, element-wise, in one rendering."""
    return [
        "" if _is_missing(value) else _as_character(value).strip()
        for value in _iterable_values(values)
    ]


def _iri_query(iri: object) -> str:
    """The local name of an IRI, split back into words.

    ``SpawnerAbundance`` becomes "spawner abundance": the label a reviewer
    searched for is recoverable from the IRI they accepted, which is what lets
    evidence be re-resolved without asking the user to restate the query.
    """
    local = re.sub(r"^.*[#/]", "", _closure_text(iri))
    local = re.sub(r"[_-]+", " ", local)
    local = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", local)
    local = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", local)
    return re.sub(r"\s+", " ", local).strip().lower()


def _normalize_evidence(evidence: object) -> Optional[pd.DataFrame]:
    if evidence is None:
        return None
    # The same early-type contract ``llm_context_files`` carries: a caller who
    # hands over a parsed object rather than rows gets an error here, not a
    # confusing failure eight frames down.
    if not isinstance(evidence, pd.DataFrame):
        raise ValueError(
            "evidence must be a data frame of hand-supplied closure rows; got "
            f"{type(evidence).__name__}. Supply one row per IRI with an 'iri' "
            "column."
        )
    if len(evidence) == 0:
        return None
    if "iri" not in evidence.columns:
        raise ValueError("evidence must have an 'iri' column.")
    known = ["iri", "target_sdp_field"] + list(_EVIDENCE_FIELDS)
    unknown = [name for name in evidence.columns if name not in known]
    if unknown:
        raise ValueError(
            "evidence has column(s) this closure cannot use: "
            + ", ".join(unknown)
            + ". Supported columns: "
            + ", ".join(known)
            + "."
        )
    frame = pd.DataFrame(
        {name: _closure_column(evidence[name]) for name in evidence.columns},
        dtype=object,
    )
    if any(not value for value in frame["iri"]):
        raise ValueError("Every evidence row must name a non-empty 'iri'.")
    if "target_sdp_field" not in frame.columns:
        frame["target_sdp_field"] = [""] * len(frame)
    keys = [
        iri + "\r" + field
        for iri, field in zip(frame["iri"], frame["target_sdp_field"])
    ]
    duplicated = sorted({key for key in keys if keys.count(key) > 1})
    if duplicated:
        raise ValueError(
            "evidence must carry at most one row per IRI and target field: "
            + ", ".join(key.split("\r")[0] for key in duplicated)
            + "."
        )
    return frame


def _evidence_value(
    evidence: Optional[pd.DataFrame],
    iri: str,
    field: str,
    target_field: str = "",
) -> str:
    """The ``evidence`` value for one IRI and field.

    A row naming the target field wins over the IRI-wide row. Returns "" when
    nothing was supplied, which is what lets a partial row overlay a searched
    one field by field.
    """
    if evidence is None or field not in evidence.columns:
        return ""
    scoped = ""
    general = ""
    for index in range(len(evidence)):
        if evidence["iri"].iloc[index] != iri:
            continue
        value = _closure_text(evidence[field].iloc[index])
        row_field = evidence["target_sdp_field"].iloc[index]
        if row_field and row_field == target_field:
            scoped = scoped or value
        elif not row_field:
            general = general or value
    return scoped or general


def _read_decisions(path: Union[str, Path]) -> pd.DataFrame:
    """The decision trail ``apply_sdp_semantics()`` leaves in the package.

    For an accepted IRI it records the query that found it and the reviewer's
    reason. Reading it back is why a second run of this producer does not ask
    the user to restate what review already recorded. Every column access is
    guarded because the file is optional and its shape has grown across
    releases.
    """
    columns = [
        "iri",
        "dataset_id",
        "table_id",
        "column_name",
        "target_sdp_field",
        "search_query",
        "decision_reason",
    ]
    empty = pd.DataFrame({name: [] for name in columns}, dtype=object)
    suggestions_path = Path(path) / "semantic_suggestions.csv"
    if not suggestions_path.is_file():
        return empty
    try:
        suggestions = read_sdp_csv(suggestions_path)
    except Exception:
        return empty
    if len(suggestions) == 0 or "iri" not in suggestions.columns:
        return empty

    def column(name: str) -> List[str]:
        if name not in suggestions.columns:
            return [""] * len(suggestions)
        return _closure_column(suggestions[name])

    out = pd.DataFrame({name: column(name) for name in columns}, dtype=object)
    decision = [value.lower() for value in column("decision")]
    keep = [
        bool(iri) and (not state or state == "accepted")
        for iri, state in zip(out["iri"], decision)
    ]
    return out[keep].reset_index(drop=True)


def _closure_queries(
    iri: str,
    decisions: pd.DataFrame,
    targets: pd.DataFrame,
    dictionary: pd.DataFrame,
) -> List[str]:
    """Queries to try for one IRI, most specific first.

    The recorded review query comes first because it is what actually found the
    term; the IRI's own local name is the fallback that needs no prior run.
    """
    recorded = [
        value
        for value, key in zip(decisions["search_query"], decisions["iri"])
        if key == iri
    ]
    labels: List[str] = []
    has_dictionary = len(dictionary) > 0 and {
        "table_id",
        "column_name",
        "column_label",
    }.issubset(dictionary.columns)
    if has_dictionary:
        rows = targets[targets["iri"] == iri]
        for position in range(len(rows)):
            table_id = rows["table_id"].iloc[position]
            column_name = rows["column_name"].iloc[position]
            hit = dictionary[
                (dictionary["table_id"].map(_as_character) == table_id)
                & (dictionary["column_name"].map(_as_character) == column_name)
            ]
            if len(hit) > 0:
                labels.append(_closure_text(hit["column_label"].iloc[0]))
    queries: List[str] = []
    for candidate in list(recorded) + [_iri_query(iri)] + labels:
        text = _closure_text(candidate)
        if text and text not in queries:
            queries.append(text)
    return queries


def _iri_roles(iri: str, targets: pd.DataFrame, measurement_only: bool) -> List[str]:
    """Roles to search an IRI under.

    Role selects the sources and the ranking profile, so searching under the
    wrong one can hide a term that exists: a ``unit`` role, for instance, filters
    gcdfo out entirely.
    """
    roles = {
        value
        for value, key in zip(targets["dictionary_role"], targets["iri"])
        if key == iri and _closure_text(value)
    }
    if not roles:
        # A code-resolved ``sosa:usedProcedure`` is in the measurement set and is
        # never a review target, so it has no role row to read.
        roles = {"method" if measurement_only else "variable"}
    return sorted(roles)


def _no_search() -> Dict[str, object]:
    """The "no search was needed" result, so every consumer reads four fields."""
    return {"values": None, "query": "", "role": "", "errors": [], "degraded": []}


def _search_evidence(
    iri: str,
    queries: Sequence[str],
    roles: Sequence[str],
    sources: Sequence[str],
    search_fn: Callable,
) -> Dict[str, object]:
    """A LOOKUP THAT DID NOT ANSWER IS NOT AN ONTOLOGY GAP.

    Both ways a search can fail to answer reach here in the same shape -- no
    matching row -- so both are carried back to the caller rather than collapsed
    into "the term does not exist":

      a raised exception   transient network, a parser, a cache
      failed-source        ``find_terms()`` records per-source status in its
      diagnostics          ``attrs["diagnostics"]`` and returns EMPTY while
                           warning, in its own words, that such a result is
                           unknown rather than an ontology gap

    The degraded-status test is ``term_search._search_failed_sources``, read
    rather than restated, so this cannot drift from the warning it agrees with.
    The exception text is redacted where it is CAPTURED, because it ends up on a
    returned frame and in a message.
    """
    errors: List[str] = []
    degraded: List[str] = []
    for role in roles:
        for query in queries:
            hits = None
            try:
                hits = search_fn(query, role=role, sources=list(sources))
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                errors.append(redact_secrets(str(exc)))
            attrs = getattr(hits, "attrs", None)
            if isinstance(attrs, dict):
                degraded.extend(_search_failed_sources(attrs.get("diagnostics")))
            if (
                not isinstance(hits, pd.DataFrame)
                or len(hits) == 0
                or "iri" not in hits.columns
            ):
                continue
            hit = hits[hits["iri"].map(_as_character) == iri]
            if len(hit) == 0:
                continue
            values = {
                field: (
                    _closure_text(hit[field].iloc[0])
                    if field in hit.columns
                    else ""
                )
                for field in _SEARCH_EVIDENCE_FIELDS
            }
            # Resolved. Whatever else did not answer is moot here: the evidence
            # for THIS IRI is in hand, so there is no gap to misattribute to an
            # outage.
            return {
                "values": values,
                "query": query,
                "role": role,
                "errors": [],
                "degraded": [],
            }
    return {
        "values": None,
        "query": queries[0] if queries else "",
        "role": roles[0] if roles else "",
        "errors": sorted(set(errors)),
        "degraded": sorted(set(degraded)),
    }


def _placement_scope(iri: str) -> str:
    return _namespace_scope(iri) or "uncertain"


def _target_context(
    iri: str, targets: pd.DataFrame, dictionary: pd.DataFrame
) -> Dict[str, str]:
    """Where an IRI sits in the package.

    The review target that selected it, the dictionary row behind that target,
    and the defaults for an IRI that is in the measurement set only -- a
    code-resolved ``sosa:usedProcedure``, which no reviewer ever selected as a
    slot and so has no target row to read.

    Shared by the gap row and the incomplete-evidence row below, which describe
    the same IRI in the same place and must not disagree about where that is.
    """
    rows = targets[targets["iri"] == iri]
    target = rows.iloc[0] if len(rows) > 0 else None
    if target is None:
        scope, field, role = "code", "method_iri", "method"
        dataset_id = ""
        if "dataset_id" in dictionary.columns and len(dictionary) > 0:
            dataset_id = _closure_text(dictionary["dataset_id"].iloc[0])
        table_id = ""
        column_name = ""
    else:
        scope = _closure_text(target["target_scope"], "column")
        field = _closure_text(target["target_sdp_field"], "term_iri")
        role = _closure_text(target["dictionary_role"], "variable")
        dataset_id = _closure_text(target["dataset_id"])
        table_id = _closure_text(target["table_id"])
        column_name = _closure_text(target["column_name"])

    label = ""
    description = ""
    if (
        column_name
        and len(dictionary) > 0
        and {"table_id", "column_name"}.issubset(dictionary.columns)
    ):
        hit = dictionary[
            (dictionary["table_id"].map(_as_character) == table_id)
            & (dictionary["column_name"].map(_as_character) == column_name)
        ]
        if len(hit) > 0:
            if "column_label" in hit.columns:
                label = _closure_text(hit["column_label"].iloc[0])
            if "column_description" in hit.columns:
                description = _closure_text(hit["column_description"].iloc[0])
    return {
        "scope": scope,
        "field": field,
        "role": role,
        "dataset_id": dataset_id,
        "table_id": table_id,
        "column_name": column_name,
        "label": label,
        "description": description,
    }


def _gap_row(
    iri: str,
    targets: pd.DataFrame,
    dictionary: pd.DataFrame,
    query: str,
    sources: Sequence[str],
) -> Dict[str, object]:
    """One gap row for an IRI no search returned.

    ``no_candidates`` is the basis the enum already carries for "retrieval
    returned nothing at all", which is exactly this case, so no new detection
    value is introduced.

    THE PRECONDITION IS THAT NOTHING WAS FOUND. Two other outcomes look like
    this one from the vocabulary row's point of view -- the search did not
    answer, and the search found the term with a required field blank -- and
    neither reaches here: the first aborts, the second becomes an ``incomplete``
    row. A gap row asserts that a term is absent from the searched vocabularies,
    which is a claim the term-request pipeline acts on.
    """
    context = _target_context(iri, targets, dictionary)
    scope = context["scope"]
    target_file = {
        "column": "column_dictionary.csv",
        "table": "tables.csv",
    }.get(scope, "codes.csv")
    row = {name: None for name in _GAP_COLUMNS}
    row.update(
        {
            "dataset_id": context["dataset_id"],
            "table_id": context["table_id"],
            "column_name": context["column_name"],
            "code_value": None,
            "target_scope": scope,
            "target_sdp_file": target_file,
            "target_sdp_field": context["field"],
            "target_row_key": context["column_name"] or context["table_id"],
            "dictionary_role": context["role"],
            "search_query": query,
            "column_label": context["label"],
            "column_description": context["description"],
            "top_non_smn_source": "",
            "top_non_smn_label": "",
            "top_non_smn_iri": "",
            "top_non_smn_ontology": "",
            "top_non_smn_match_type": "",
            "top_non_smn_score": None,
            "candidate_count": 0,
            "non_smn_sources": "",
            "placement_recommendation": _placement_scope(iri),
            "placement_confidence": None,
            "placement_rationale": (
                "The package asserts this IRI in "
                + context["field"]
                + " and searching "
                + "/".join(sources)
                + " for it returned nothing, so either the term is absent from "
                "the searched vocabularies or the IRI is wrong. Mint the term, "
                "or supply a row through the `evidence` argument of "
                "write_sdp_semantic_closure()."
            ),
            "target_label": context["label"],
            "target_description": context["description"],
            "gap_detection_basis": "no_candidates",
            "unresolved_iri": iri,
        }
    )
    return row


def _empty_gaps() -> pd.DataFrame:
    return pd.DataFrame(columns=list(_GAP_COLUMNS))


def _incomplete_row(
    iri: str,
    targets: pd.DataFrame,
    dictionary: pd.DataFrame,
    missing_fields: Sequence[str],
    values: Dict[str, str],
    resolved: Dict[str, object],
) -> Dict[str, object]:
    """INCOMPLETE EVIDENCE IS NOT A MISSING TERM.

    An exact IRI hit can still carry a blank required field -- an ontology class
    with no ``skos:definition`` is the ordinary case -- and the vocabulary row
    cannot be written without it, because ``eml._read_vocabulary()`` refuses a
    blank. That is a different report from a gap and goes in a different table: a
    gap row hard-codes ``candidate_count = 0`` and
    ``gap_detection_basis = "no_candidates"``, so routing a found term through it
    asks ``render_ontology_term_request()`` to mint a term that already exists.

    The fix is a named field and the place it is missing from, which is what a
    reviewer needs in order to supply it through ``evidence`` or annotate it
    upstream. Reported and warned rather than raised: the rest of the closure is
    still correct and still worth having, which is the same reasoning as the
    ruled gap-not-abort shape.
    """
    context = _target_context(iri, targets, dictionary)
    return {
        "dataset_id": context["dataset_id"],
        "table_id": context["table_id"],
        "column_name": context["column_name"],
        "target_scope": context["scope"],
        "target_sdp_field": context["field"],
        "dictionary_role": context["role"],
        "iri": iri,
        "missing_fields": ";".join(sorted(set(missing_fields))),
        "resolved_source": _closure_text(values.get("source", "")),
        "search_query": _closure_text(resolved.get("query")),
        "searched_role": _closure_text(resolved.get("role")),
    }


def _empty_incomplete() -> pd.DataFrame:
    return pd.DataFrame(columns=list(_INCOMPLETE_COLUMNS))


def _resolve_write_path(path: Union[str, Path], relative: str) -> str:
    """A relative path that stays inside the package AND IS REACHED THROUGH NO LINK.

    THE THREAT MODEL IS AN SDP THAT ARRIVED FROM SOMEBODY ELSE. Both closure
    targets and the sidecar itself are package content, so all three are
    untrusted input to a public, documented workflow: the sidecar's ``path``
    values choose where a write lands, and a symlink already sitting at any of
    the three names redirects that write to whatever the process can write. So
    every one of the root, each intermediate directory component, and the final
    entry is refused when it is a link -- the package's hardened SDP layer does
    exactly this for every other metadata resource, and is called here rather
    than re-implemented.

    ``create=True`` on the directory walk replaces a recursive ``mkdir`` that
    would run BEFORE the containment check, so a declared escaping path never
    creates directories outside the package on its way to being refused.

    HARD LINKS ARE NOT DETECTED, AND ARE NOT LEFT OPEN. There is no portable
    link-count test that means anything here (``st_nlink`` says a file has other
    names, not where they are, and is meaningless on Windows), so this adds
    none. What closes it is the INSTALL path rather than a check:
    ``_atomic_write_set`` writes the bytes to a fresh sibling inode and renames
    it over the directory entry, so nothing on this path ever opens the
    destination and an external hard link keeps the inode it had, with the
    content it had. The residual is that the package's own copy stops sharing
    that inode, which is the intended outcome of replacing the file.

    *Retires when:* ``_assert_safe_file`` grows a create-if-absent mode, at which
    point this function is a single call to it.
    """
    root = _extension_root(path)
    if re.match(r"^([/\\]|[A-Za-z]:)", relative):
        raise SdpExtensionError(
            "The reviewed EML sidecar declares an absolute closure path: "
            f"{relative}."
        )
    try:
        _assert_safe_directory(root, os.path.dirname(relative) or ".", create=True)
        return str(_assert_safe_file(root, relative, must_exist=False))
    except Exception as exc:
        raise SdpExtensionError(
            "A closure output path is one this package refuses to write: "
            f"{relative}."
        ) from exc


def _mapping_file(path: Union[str, Path]) -> Optional[str]:
    """The sidecar's own path, resolved and checked ONCE, used for read and write.

    Checked for the read as well: the declared ``path`` values this producer
    obeys come out of that file, so a linked sidecar chooses where the closure
    gets written. The symlink check therefore has to happen before the file is
    read, not only before it is written.

    ``.yml`` AND ONLY ``.yml``, ON PURPOSE, and this is the measured answer to a
    question worth not re-deriving. ``eml._default_mapping_path()`` and R's
    ``.ms_eml_default_mapping_path()`` both *name* ``metadata/eml-mapping.yaml``,
    but neither returns it: they raise when both spellings exist and otherwise
    return the ``.yml`` path unconditionally. So ``.yaml`` is not a sidecar name
    this toolchain reads — measured 2026-09-16, `write_eml_from_sdp()` on a
    package carrying only ``eml-mapping.yaml`` fails with "EML mapping sidecar
    ... does not exist" naming the ``.yml`` path, in **both** implementations.
    Treating such a package as having no sidecar is therefore the same answer the
    EML writer gives, not a disagreement with it, and no digest can go stale
    because nothing ever reads the ``.yaml``.

    What *is* a real gap, and is shared rather than ours: with **both** files
    present, ``write_eml_from_sdp()`` refuses the package while this producer
    quietly uses the ``.yml``. Measured identical in R, whose closure producer
    also hard-codes the ``.yml`` path rather than routing through
    ``.ms_eml_default_mapping_path()``. Fixing it on one side only would be a
    deliberate divergence needing a register row, so it is filed for both sides
    instead of changed here.

    *Retires when:* the two sidecar spellings are resolved in one place both
    implementations call, at which point this reads that instead.
    """
    candidate = Path(path) / "metadata" / "eml-mapping.yml"
    if not candidate.exists() and not candidate.is_symlink():
        return None
    return _resolve_write_path(path, _MAPPING_RELATIVE_PATH)


def _mapping_paths(mapping_file: Optional[str]) -> Dict[str, str]:
    """The closure paths the reviewed EML sidecar declares.

    Honour them when the sidecar exists so the producer cannot write a file the
    sidecar does not point at, and rewrite the two digests afterwards so that no
    user hand-writes one.
    """
    defaults = dict(_DEFAULT_MAPPING_PATHS)
    if mapping_file is None:
        return defaults
    yaml = _require_yaml()
    try:
        with open(mapping_file, encoding="utf-8") as handle:
            mapping = yaml.safe_load(handle)
    except Exception:
        return defaults
    # A mapping, not merely "parsed": a sidecar that parses to a YAML SCALAR
    # rather than a mapping raised a subscript error that said nothing about the
    # file that caused it. The sidecar is package content, so a malformed one is
    # input and not a bug.
    if not isinstance(mapping, dict):
        return defaults

    def declared(key: str, fallback: str) -> str:
        entry = mapping.get(key)
        if not isinstance(entry, dict):
            return fallback
        value = _closure_text(entry.get("path"))
        return value or fallback

    return {
        "vocabulary": declared("semantic_vocabulary", defaults["vocabulary"]),
        "review": declared("semantic_review", defaults["review"]),
    }


def _bytes_sha256(data: bytes) -> str:
    """The digest of the bytes about to be installed, not of a file on disk.

    That is what lets the sidecar's two ``sha256`` values join the closure files
    in ONE atomic write set: hashing installed files would need the CSVs in place
    first, which is exactly the window where a later failure leaves a replaced
    file next to a stale digest. Identical output to hashing the installed file
    by construction -- both hash the same bytes.
    """
    return hashlib.sha256(data).hexdigest()


def _set_mapping_digest(
    lines: List[str], key: str, digest_value: str
) -> Tuple[List[str], bool]:
    """Replace one block-mapping ``sha256:`` value in the sidecar's TEXT.

    THE REASON IS NOT STYLE. Dumping a parsed YAML document drops every comment
    in it, and the sidecar a user starts from is a template whose first lines are
    the instructions for filling it in. Rewriting two digests must not delete the
    document's own explanation of itself, and it must not reformat a file the
    user is still editing. Only the lines this function is asked to change change.

    *Retires when:* the sidecar stops being a hand-edited file -- if a public
    writer owns it end to end, it can be emitted whole and this becomes needless.
    """
    lines = list(lines)
    entry = f"sha256: '{digest_value}'"
    head = re.compile(r"^" + re.escape(key) + r":")
    matches = [index for index, line in enumerate(lines) if head.match(line)]
    if not matches:
        return lines + [f"{key}:", f"  {entry}"], True
    start = matches[0]
    if head.sub("", lines[start]).strip():
        # A flow-style or inline value. Appending a second key would make the
        # document say two things, so say nothing and let the caller report it.
        return lines, False
    if start >= len(lines) - 1:
        return lines + [f"  {entry}"], True
    # The block ends at the next line that starts a new top-level key.
    ends = [
        index
        for index in range(start + 1, len(lines))
        if re.match(r"^[^\s#]", lines[index])
    ]
    stop_at = ends[0] - 1 if ends else len(lines) - 1
    if stop_at < start + 1:
        lines.insert(start + 1, f"  {entry}")
        return lines, True
    for index in range(start + 1, stop_at + 1):
        if re.match(r"^\s+sha256:\s*", lines[index]):
            indent = re.match(r"^(\s+)", lines[index]).group(1)
            lines[index] = indent + entry
            return lines, True
    lines.insert(start + 1, f"  {entry}")
    return lines, True


def _text_bytes(lines: Sequence[str]) -> bytes:
    """One newline-terminated line each, the way R's ``writeLines`` emits them."""
    return ("\n".join(lines) + "\n").encode("utf-8")


def _mapping_bytes(
    mapping_file: Optional[str],
    vocabulary_bytes: bytes,
    review_bytes: bytes,
) -> Dict[str, object]:
    """The sidecar's updated bytes, and nothing written.

    Rendering is split from installing so that the sidecar joins the two closure
    files in one atomic write set: the digests are computed over the bytes that
    are about to be installed, so there is never a moment when a CSV has been
    replaced and its ``sha256`` has not. The caller warns about ``refused`` only
    after the install succeeds.
    """
    if mapping_file is None:
        return {"bytes": None, "refused": []}
    with open(mapping_file, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    refused: List[str] = []
    for key, payload in (
        ("semantic_vocabulary", vocabulary_bytes),
        ("semantic_review", review_bytes),
    ):
        lines, ok = _set_mapping_digest(lines, key, _bytes_sha256(payload))
        if not ok:
            refused.append(key)
    return {"bytes": _text_bytes(lines), "refused": refused}


def _frame(rows: List[Dict[str, object]], columns: Sequence[str]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=list(columns))
    return pd.DataFrame(rows, columns=list(columns))


def write_sdp_semantic_closure(
    path: Union[str, Path],
    evidence: Optional[pd.DataFrame] = None,
    search_fn: Callable = find_terms,
    sources: Sequence[str] = _DEFAULT_SOURCES,
    quiet: bool = False,
) -> Dict[str, object]:
    """Write the reviewed semantic closure for a Salmon Data Package.

    Produces the two files :func:`write_eml_from_sdp` and
    :func:`publish_sdp_to_knb` require and neither writes:
    ``metadata/semantic_vocabulary.csv``, one evidence row per canonical
    measurement IRI, and ``reviewed_semantic_selections.csv``, exactly one
    ``accepted`` row per canonical review target. Both row sets are derived from
    the package on disk, each vocabulary row's ``reviewed_snapshot_sha256`` is
    computed, and the two file digests in ``metadata/eml-mapping.yml`` are
    rewritten when that sidecar exists -- so no digest is ever hand-written.

    **The two canonical sets are not one set.** The vocabulary describes IRIs the
    EML measurement and method paths emit: the six semantic fields of every
    ``measurement`` dictionary row, a table-level ``method_iri``, and any
    ``sosa:usedProcedure`` reached through a code value. The ledger describes
    slots a reviewer decided: the same six dictionary fields plus a table's
    ``observation_unit_iri`` and ``method_iri``. So an ``observation_unit_iri``
    is a review target and not a vocabulary term, and a code-resolved procedure
    is a vocabulary term and not a review target. Neither set can be reasoned
    from the other, which is why both are derived here.

    **Unresolvable IRIs become gaps, not errors.** Evidence is resolved by
    re-running the package's own deterministic search (:func:`find_terms`) for
    each IRI and keeping the hit whose IRI matches. An IRI every searched source
    answered about and none of them has is reported as a row of ``gaps`` -- the
    shape :func:`detect_semantic_term_gaps` returns, plus an ``unresolved_iri``
    column -- and both files are still written without it. That is deliberate: an
    IRI absent from every searched vocabulary is an ontology gap to file through
    :func:`render_ontology_term_request` and :func:`submit_term_request_issues`,
    not a reason to leave the user with no files at all. The omission is not
    silent: attempting EML export then names the same IRI as missing.

    **What is not a gap.** Two other outcomes leave a vocabulary row unwritten
    and neither is an ontology gap, because in neither case is the term known to
    be absent. Reporting them as gaps would ask
    :func:`render_ontology_term_request` to mint a term that exists.

    * *A lookup that did not answer.* If a search raises, or :func:`find_terms`
      returns empty while its ``attrs["diagnostics"]`` records a source that
      failed, nothing has been learned about that IRI. The call raises
      ``RuntimeError`` and writes nothing, naming each IRI and the sources that
      did not answer; the remedy is to re-run, so leaving a half-derived closure
      on disk would make the retry start from worse state.
    * *A term found with incomplete evidence.* An exact IRI hit can still carry a
      blank required field -- a class with no definition is the ordinary case.
      The row is reported in ``incomplete``, naming the missing field and the
      slot, with a warning; the other rows are still written. Supply the field
      through ``evidence``, or annotate the term upstream.

    **Hand-supplied evidence.** :func:`find_terms` fills ``label``,
    ``definition``, ``source``, ``ontology``, ``resource_kind`` and ``type_iris``
    for ``smn`` and ``gcdfo``. It cannot fill ``native_type`` or ``source_url``,
    which describe the ontology artifact rather than the term -- both are derived
    here from the resolved source and can be overridden -- and it cannot search
    QUDT at all, so a QUDT row is entirely hand-authored. ``confidence`` and
    ``review_rationale`` are human judgements: they are read from
    ``semantic_suggestions.csv`` where :func:`apply_sdp_semantics` recorded them,
    and otherwise written as a ``REVIEW REQUIRED:`` placeholder with a warning
    naming every target that got one. A placeholder rationale satisfies the
    ledger's non-empty check, so replace it before publication; the warning and
    the marker are the only signals, deliberately, because judging a rationale is
    not a validator's job.

    Parameters
    ----------
    path:
        Path to an existing Salmon Data Package directory.
    evidence:
        Optional data frame of hand-supplied closure rows, one per IRI, with an
        ``iri`` column and any of ``label``, ``definition``, ``source``,
        ``ontology``, ``resource_kind``, ``type_iris``, ``native_type``,
        ``source_url``, ``source_artifact_sha256``, ``confidence``,
        ``review_rationale``. Supplied non-empty values win over resolved ones
        field by field, so a row may correct one field and leave the rest to the
        search. An optional ``target_sdp_field`` column narrows a row to one
        slot, for an IRI selected in more than one. When a row supplies every
        required vocabulary field, no search runs for that IRI at all.
    search_fn:
        Function used to search terms. Defaults to :func:`find_terms`. A test
        hook; the signature is ``fn(query, role=..., sources=...)``.
    sources:
        Vocabulary sources to search. Defaults to ``("smn", "gcdfo")``, the two
        this package resolves deterministically.
    quiet:
        Suppress the progress and summary messages. Warnings about gaps and
        placeholder rationales are not suppressed.

    Returns
    -------
    dict
        ``vocabulary`` and ``review`` (the two frames as written), ``gaps``
        (IRIs absent from every searched source, in term-gap shape),
        ``incomplete`` (IRIs that were found but whose evidence is short of a
        required field, which is not a gap), ``measurement_iris`` and
        ``review_targets`` (the two canonical sets), ``placeholders`` (ledger
        rows that got a ``REVIEW REQUIRED:`` rationale), and ``files`` (the paths
        written).

    Raises
    ------
    ValueError
        For a ``path`` that is not one directory, a non-frame or malformed
        ``evidence``, or an empty ``sources``. ``SdpExtensionError`` (a
        ``ValueError``) for a closure path this package refuses to write.
    RuntimeError
        When a vocabulary lookup did not answer. Nothing is written.

    Examples
    --------
    >>> closure = write_sdp_semantic_closure("path/to/sdp")  # doctest: +SKIP

    QUDT is not a searchable source, so a unit row is hand-authored:

    >>> closure = write_sdp_semantic_closure(  # doctest: +SKIP
    ...     "path/to/sdp",
    ...     evidence=pd.DataFrame([{
    ...         "iri": "https://qudt.org/vocab/unit/INDIV",
    ...         "label": "Individual",
    ...         "definition": "A counting unit denoting one organism.",
    ...         "source": "qudt",
    ...         "ontology": "qudt",
    ...         "resource_kind": "Unit",
    ...         "type_iris": "http://qudt.org/schema/qudt/Unit",
    ...         "native_type": "qudt:Unit",
    ...         "source_url": "https://qudt.org/vocab/unit/",
    ...         "confidence": "high",
    ...         "review_rationale": "Values are whole counts of organisms.",
    ...     }]),
    ... )
    """
    if isinstance(path, (list, tuple)) or path is None or not str(path):
        raise ValueError("path must be a single package directory path.")
    if not Path(path).is_dir():
        raise ValueError(f"Directory {path} does not exist.")
    if not callable(search_fn):
        raise ValueError("search_fn must be a function.")
    source_list: List[str] = []
    for source in (sources,) if isinstance(sources, str) else sources:
        text = _as_character(source).strip()
        if text and text not in source_list:
            source_list.append(text)
    if not source_list:
        raise ValueError("sources must name at least one vocabulary source.")
    evidence = _normalize_evidence(evidence)

    pkg = read_salmon_datapackage(str(path))
    dictionary = pkg.get("dictionary")
    if not isinstance(dictionary, pd.DataFrame):
        dictionary = pd.DataFrame()
    decisions = _read_decisions(path)

    # ------------------------------------------------------------------
    # Both canonical sets, derived from the package rather than transcribed.
    # Codepoint ordering throughout: these orders become the row order of two
    # CSVs whose bytes are hashed into the reviewed sidecar.
    # ------------------------------------------------------------------
    measurement_iris = sorted(
        {
            value
            for value in _closure_column(_canonical_measurement_iris(Path(path), pkg))
            if value
        }
    )

    target_rows = [
        {name: _closure_text(target.get(name)) for name in _TARGET_FIELDS}
        for target in _canonical_review_targets(pkg)
    ]
    target_rows.sort(key=lambda row: tuple(row[name] for name in _TARGET_FIELDS))
    review_targets = _frame(target_rows, _TARGET_FIELDS)

    if not quiet:
        print(
            f"Deriving the reviewed closure for {path}.\n"
            f"  {len(measurement_iris)} canonical measurement IRI(s).\n"
            f"  {len(review_targets)} canonical review target(s)."
        )

    # ------------------------------------------------------------------
    # Vocabulary evidence, one row per measurement IRI.
    # ------------------------------------------------------------------
    target_iris = set(review_targets["iri"]) if len(review_targets) else set()
    vocabulary_rows: List[Dict[str, object]] = []
    gap_rows: List[Dict[str, object]] = []
    incomplete_rows: List[Dict[str, object]] = []
    failure_rows: List[Dict[str, str]] = []
    for iri in measurement_iris:
        values = {
            field: _evidence_value(evidence, iri, field)
            for field in _VOCABULARY_FIELDS
            if field != "iri"
        }
        if any(not values[field] for field in _REQUIRED_VOCABULARY_FIELDS):
            if not quiet:
                print(f"  Resolving {iri}.")
            resolved = _search_evidence(
                iri,
                _closure_queries(iri, decisions, review_targets, dictionary),
                _iri_roles(iri, review_targets, measurement_only=iri not in target_iris),
                source_list,
                search_fn,
            )
        else:
            resolved = _no_search()

        if resolved["values"] is not None:
            for field in _SEARCH_EVIDENCE_FIELDS:
                if not values[field]:
                    values[field] = resolved["values"][field]
        if not values["source_url"]:
            values["source_url"] = _source_url(values["source"])
        if not values["native_type"]:
            values["native_type"] = _native_type(values["resource_kind"])

        # THREE WAYS A ROW CAN BE SHORT, AND THEY ARE NOT THE SAME REPORT. Only
        # the last of them is an ontology gap, and only a gap may reach the
        # term-request pipeline; the first two once did in R, which is the defect
        # this port carries the fix for rather than reintroducing.
        missing = [
            field
            for field in _REQUIRED_VOCABULARY_FIELDS
            if not values[field]
        ]
        if missing:
            if resolved["errors"] or resolved["degraded"]:
                # 1. The lookup did not answer. Nothing has been learned about
                #    this IRI, so no report about it can be made yet. Collected
                #    and raised on below, before any file moves.
                failure_rows.append(
                    {
                        "iri": iri,
                        "failed_sources": ";".join(resolved["degraded"]),
                        "search_error": ";".join(resolved["errors"]),
                        "search_query": _closure_text(resolved["query"]),
                    }
                )
                continue
            if resolved["values"] is not None:
                # 2. The term was FOUND and its evidence is short of a required
                #    field. Not a gap: the IRI exists and asking for it to be
                #    minted would be wrong. Named field, named slot, separate
                #    table.
                incomplete_rows.append(
                    _incomplete_row(
                        iri, review_targets, dictionary, missing, values, resolved
                    )
                )
                continue
            # 3. Every searched source answered and none of them has the term.
            gap_rows.append(
                _gap_row(
                    iri,
                    review_targets,
                    dictionary,
                    _closure_text(resolved["query"]),
                    source_list,
                )
            )
            continue

        row = {"iri": iri}
        row.update(values)
        vocabulary_rows.append({field: row[field] for field in _VOCABULARY_FIELDS})

    # ------------------------------------------------------------------
    # A LOOKUP THAT DID NOT ANSWER STOPS THE RUN, BEFORE ANY FILE MOVES.
    #
    # This is the one case the gap-not-abort ruling does NOT cover, and the
    # distinction is the whole of it: that ruling is about an IRI the searched
    # vocabularies genuinely do not have, which is a term request somebody can
    # file. A source that did not answer has told us nothing, so there is nothing
    # to file and no vocabulary row to omit on purpose -- writing the closure
    # anyway would put an outage's shape into the two files a publication is
    # built from, and would hand ``render_ontology_term_request()`` a request to
    # mint a term that may already exist. ``find_terms()`` warns, in its own
    # words, that such a result is unknown rather than an ontology gap; this
    # obeys it.
    #
    # Placed here, ahead of the ledger and every write, because the remedy is to
    # re-run: leaving a half-derived closure on disk would make the retry start
    # from worse state than the first attempt did.
    # ------------------------------------------------------------------
    if failure_rows:
        failure_rows.sort(key=lambda row: row["iri"])
        detail = []
        for row in failure_rows:
            line = row["iri"]
            if row["failed_sources"]:
                line += f" (no answer from {row['failed_sources']})"
            if row["search_error"]:
                line += f" (search failed: {row['search_error']})"
            detail.append(line)
        raise RuntimeError(
            f"Vocabulary lookup did not answer for {len(failure_rows)} canonical "
            "measurement IRI(s), so no closure was written:\n  "
            + "\n  ".join(detail)
            + "\nA degraded search is unknown, not an ontology gap: none of "
            "these is a term request.\nRe-run when the sources answer, or "
            "supply the evidence through `evidence`."
        )

    # ONE VALUE, ONE RENDERING. Every cell was coerced to text exactly once, by
    # ``_closure_text``, when the row was built. The same string is the sort key
    # below, the digest input, and the CSV byte -- ``_csv_bytes`` writes a string
    # cell verbatim -- so what is sorted is what is written.
    vocabulary_rows.sort(key=lambda row: row["iri"])
    vocabulary = _frame(vocabulary_rows, _VOCABULARY_FIELDS)
    vocabulary["reviewed_snapshot_sha256"] = [
        _vocabulary_snapshot_sha256(row) for row in vocabulary_rows
    ]

    # ------------------------------------------------------------------
    # The review ledger: one accepted row per canonical target.
    # ------------------------------------------------------------------
    ledger_rows: List[Dict[str, object]] = []
    placeholder_flags: List[bool] = []
    for position in range(len(review_targets)):
        target = review_targets.iloc[position]
        iri = target["iri"]
        field = target["target_sdp_field"]
        confidence = _evidence_value(evidence, iri, "confidence", field) or "unassessed"
        rationale = _evidence_value(evidence, iri, "review_rationale", field)
        if not rationale:
            for index in range(len(decisions)):
                recorded = decisions.iloc[index]
                if recorded["iri"] != iri:
                    continue
                if recorded["table_id"] and recorded["table_id"] != target["table_id"]:
                    continue
                if (
                    recorded["column_name"]
                    and recorded["column_name"] != target["column_name"]
                ):
                    continue
                if (
                    recorded["target_sdp_field"]
                    and recorded["target_sdp_field"] != field
                ):
                    continue
                if recorded["decision_reason"]:
                    rationale = recorded["decision_reason"]
                    break
        placeholder = not rationale
        if placeholder:
            rationale = _PLACEHOLDER_RATIONALE
        placeholder_flags.append(placeholder)
        row = {name: target[name] for name in _TARGET_FIELDS}
        row["decision"] = "accepted"
        row["confidence"] = confidence
        row["review_rationale"] = rationale
        ledger_rows.append({name: row[name] for name in _LEDGER_FIELDS})
    review = _frame(ledger_rows, _LEDGER_FIELDS)

    # ------------------------------------------------------------------
    # RENDER EVERYTHING, THEN INSTALL IT AS ONE SET.
    #
    # Three coordinated files: two CSVs, and the sidecar that pins their bytes.
    # The set is only meaningful complete -- a vocabulary CSV beside its previous
    # ``sha256`` is a package that fails its own digest check -- so all three are
    # rendered to bytes first and installed by ``_atomic_write_set``, which
    # stages each as a sibling, renames them in, and rolls every one of them back
    # if any install fails. A sequence of direct writes has two windows in it: an
    # unwritable sidecar leaves a replaced CSV with a stale digest, and a failure
    # on the second CSV leaves the first replaced with no digest update at all.
    # Both raise, and both leave the package worse than it started.
    #
    # It is also what makes a linked destination harmless: nothing here opens a
    # target file, so neither a symlink nor a hard link at any of the three names
    # is written through. The path checks above refuse a symlink outright; this
    # is the layer that covers what no portable check can see.
    #
    # Reuse rather than a second mechanism: this writer is the package's own,
    # already used by the methods migration, the observation structures, and the
    # metadata writer.
    # ------------------------------------------------------------------
    mapping_file = _mapping_file(path)
    declared = _mapping_paths(mapping_file)
    vocabulary_file = _resolve_write_path(path, declared["vocabulary"])
    review_file = _resolve_write_path(path, declared["review"])

    vocabulary_bytes = _csv_bytes(
        list(_VOCABULARY_FIELDS) + ["reviewed_snapshot_sha256"], vocabulary
    )
    review_bytes = _csv_bytes(list(_LEDGER_FIELDS), review)
    mapping = _mapping_bytes(mapping_file, vocabulary_bytes, review_bytes)

    writes: Dict[str, bytes] = {
        vocabulary_file: vocabulary_bytes,
        review_file: review_bytes,
    }
    if mapping["bytes"] is not None:
        writes[mapping_file] = mapping["bytes"]
    _atomic_write_set(writes)

    # After the install, never before: if nothing was installed there is no
    # half-pinned sidecar to warn about.
    if mapping["refused"]:
        warnings.warn(
            f"The reviewed EML sidecar writes {len(mapping['refused'])} key(s) "
            "inline, so their sha256 could not be pinned without rewriting the "
            "whole document: " + ", ".join(mapping["refused"]) + ". Rewrite each "
            "as a block mapping with `path` and `sha256` on their own lines, "
            "then re-run.",
            RuntimeWarning,
            stacklevel=2,
        )

    gap_rows.sort(
        key=lambda row: (
            row["dataset_id"],
            row["table_id"],
            row["column_name"],
            row["target_sdp_field"],
            row["unresolved_iri"],
        )
    )
    gaps = _frame(gap_rows, _GAP_COLUMNS) if gap_rows else _empty_gaps()
    if len(gaps) > 0:
        warnings.warn(
            f"{len(gaps)} canonical measurement IRI(s) could not be resolved "
            f"from {', '.join(repr(source) for source in source_list)} and are "
            "absent from the reviewed vocabulary: "
            + ", ".join(
                f"{row['target_sdp_field']} = {row['unresolved_iri']}"
                for row in gap_rows
            )
            + ". Each is a row of the returned `gaps` table; pass it to "
            "render_ontology_term_request() to file a term request, or supply a "
            "row through `evidence`.",
            RuntimeWarning,
            stacklevel=2,
        )

    incomplete_rows.sort(key=lambda row: (row["iri"], row["target_sdp_field"]))
    incomplete = (
        _frame(incomplete_rows, _INCOMPLETE_COLUMNS)
        if incomplete_rows
        else _empty_incomplete()
    )
    if len(incomplete) > 0:
        warnings.warn(
            f"{len(incomplete)} canonical measurement IRI(s) resolved to a term "
            "whose evidence is short of a required field, so the row was not "
            "written: "
            + ", ".join(
                f"{row['iri']} is missing {row['missing_fields']}"
                for row in incomplete_rows
            )
            # Said explicitly because the two warnings otherwise read alike, and
            # the difference decides what a reader should do next: a gap is a
            # term to mint, this is a field to supply or annotate.
            + ". This is not an ontology gap: the term was found. Each is a row "
            "of the returned `incomplete` table. Supply the named field through "
            "`evidence`, or annotate the term in its ontology, then re-run.",
            RuntimeWarning,
            stacklevel=2,
        )

    # ``reset_index`` so every frame this function returns is positionally
    # indexed. Boolean filtering keeps the source positions, which would make
    # ``placeholders`` the one returned frame where ``.loc[0]`` can raise while
    # the other four are fine -- an inconsistency with no upside, and one R does
    # not have because a tibble has no index to carry.
    placeholders = (
        review[placeholder_flags].reset_index(drop=True) if len(review) else review
    )
    if len(placeholders) > 0:
        warnings.warn(
            f"{len(placeholders)} review target(s) got a `REVIEW REQUIRED:` "
            "rationale because no reviewer rationale was recorded: "
            + ", ".join(
                row["table_id"]
                + ("." + row["column_name"] if row["column_name"] else "")
                + "."
                + row["target_sdp_field"]
                for row, flag in zip(ledger_rows, placeholder_flags)
                if flag
            )
            + ". Supply `review_rationale` through `evidence`, or record "
            "decision reasons with accept_suggestion(), before publication.",
            RuntimeWarning,
            stacklevel=2,
        )

    if not quiet:
        written = [vocabulary_file, review_file]
        if mapping_file is not None:
            written.append(mapping_file)
        print(
            f"Wrote {len(vocabulary)} vocabulary row(s) and {len(review)} "
            "ledger row(s).\n  " + "\n  ".join(written)
        )

    return {
        "vocabulary": vocabulary,
        "review": review,
        "gaps": gaps,
        "incomplete": incomplete,
        "measurement_iris": measurement_iris,
        "review_targets": review_targets,
        "placeholders": placeholders,
        "files": {
            "vocabulary": vocabulary_file,
            "review": review_file,
            "mapping": mapping_file,
        },
    }


__all__ = ["write_sdp_semantic_closure"]
