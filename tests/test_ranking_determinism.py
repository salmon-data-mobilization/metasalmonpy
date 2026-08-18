"""Candidate order does not depend on input order (metasalmon 0.2.1).

metasalmon 0.2.1 made semantic ranking reproducible across locales *and*
across input orders: score alone is not a total order, and with
``seed_semantics = TRUE`` the top-1 pick becomes a written IRI in
``column_dictionary.csv``, so the same input seeded differently on macOS and in
a C-locale container. Every ordering site now carries the full tie-break key
set ``(-score, source, ontology, label, iri)``.

Python's ``sorted``/``sort_values`` are codepoint-ordinal, so the *locale* half
of that fix is inapplicable here (PARITY.md row 3) and only the tie-break half
is portable. This file pins the portable half by permuting the input.

**Measured against metasalmon 0.2.1, not assumed.** Driving
``.score_and_rank_terms()`` over the same six candidates under four input
permutations returns one fixed order on both sides. The orders are not the
*same* order — R ranks ``gcdfo`` above ``smn`` where this package does the
reverse — which is PARITY.md row 32's pre-existing ranking-profile gap,
confirmed live rather than inferred. Row 32 is not in this rung's scope; this
test asserts the property 0.2.1 added, and deliberately does not assert an
order this package is already registered as not sharing.
"""

from __future__ import annotations

import pandas as pd

from metasalmonpy.semantics import apply_semantic_suggestions
from metasalmonpy.term_search import _score_and_rank_terms

# Tie-heavy on purpose: the same score, the same label from several ontologies,
# which is exactly the shape where score-plus-label is still not a total order.
CANDIDATES = pd.DataFrame(
    {
        "iri": [
            "https://x/1",
            "https://x/2",
            "https://x/3",
            "https://y/1",
            "https://y/2",
            "https://z/1",
        ],
        "label": ["length", "length", "Length", "length", "weight", "length"],
        "definition": ["d"] * 6,
        "source": ["ols", "ols", "nvs", "ols", "smn", "gcdfo"],
        "ontology": ["envo", "obi", "P01", "envo", "smn", "gcdfo"],
        "match_type": ["exact"] * 6,
    }
)

PERMUTATIONS = (
    [0, 1, 2, 3, 4, 5],
    [5, 4, 3, 2, 1, 0],
    [2, 0, 4, 1, 5, 3],
    [1, 3, 5, 0, 2, 4],
)


def test_candidate_order_is_independent_of_input_order():
    vocab = pd.DataFrame({"role": [], "ontology": [], "weight": []})
    orders = {
        tuple(
            _score_and_rank_terms(
                CANDIDATES.iloc[list(perm)].reset_index(drop=True),
                "variable",
                vocab,
                "length",
            )["iri"]
        )
        for perm in PERMUTATIONS
    }
    assert len(orders) == 1, orders


def test_apply_semantic_suggestions_applies_in_first_occurrence_order():
    """``_row_id`` ordering, mirroring R's ``order(.row_id, method="radix")``."""
    dictionary = pd.DataFrame(
        {
            "dataset_id": ["d", "d"],
            "table_id": ["t", "t"],
            "column_name": ["length", "weight"],
            "column_label": ["length", "weight"],
            "column_description": ["a", "b"],
            "value_type": ["number", "number"],
            "column_role": ["measurement", "measurement"],
            "term_iri": ["", ""],
            "property_iri": ["", ""],
            "entity_iri": ["", ""],
            "constraint_iri": ["", ""],
            "method_iri": ["", ""],
            "unit_iri": ["", ""],
            "unit_label": ["", ""],
            "term_type": ["", ""],
            "required": [False, False],
        }
    )
    suggestions = pd.DataFrame(
        {
            "dataset_id": ["d", "d"],
            "table_id": ["t", "t"],
            "column_name": ["length", "length"],
            "dictionary_role": ["variable", "variable"],
            "iri": ["https://first", "https://second"],
            "label": ["first", "second"],
            "score": [1.0, 1.0],
        }
    )
    applied = apply_semantic_suggestions(dictionary, suggestions, strategy="top")
    first = applied.loc[applied["column_name"] == "length", "term_iri"].iloc[0]
    assert first == "https://first"

    # The same frame with the rows swapped picks the other candidate: the rule
    # is first-occurrence, so it is the frame's order that decides, not a
    # non-deterministic tie-break.
    swapped = apply_semantic_suggestions(
        dictionary, suggestions.iloc[::-1].reset_index(drop=True), strategy="top"
    )
    assert swapped.loc[swapped["column_name"] == "length", "term_iri"].iloc[0] == (
        "https://second"
    )
