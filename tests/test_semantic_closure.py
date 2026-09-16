"""Tests for ``write_sdp_semantic_closure()`` (metasalmon backlog #116, hub B-165).

The load-bearing assertion in most of these is not "a file appeared" but "the
validator that previously had no producer accepts what the producer wrote":
``eml._read_vocabulary()`` and ``eml._read_semantic_review()`` are the two gates
that made this gap visible, so they are the oracle here.

Every test is offline. ``search_fn`` is injected, so no test reaches w3id.org.

Mirrors ``tests/testthat/test-semantic-closure.R``, test for test, against the
same package shape: the bundled ``tests/data/eml/sdp-default`` fixture has the
four vocabulary rows and five review targets R's ``make_eml_test_sdp()`` builds,
including the one row that makes the two canonical sets differ.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import warnings
from pathlib import Path

import pandas as pd
import pytest

from metasalmonpy import (
    read_salmon_datapackage,
    render_ontology_term_request,
    write_sdp_semantic_closure,
)
from metasalmonpy import eml as eml_module
from metasalmonpy import semantic_closure as closure_module
from metasalmonpy.sdp_methods import SdpExtensionError
from metasalmonpy.term_requests import GAP_COLUMNS
from metasalmonpy.term_search import _search_failed_sources

_DATA_DIR = Path(__file__).parent / "data" / "eml"

OBSERVED_IRI = "https://w3id.org/smn/ObservedRateOrAbundance"
STOCK_IRI = "https://w3id.org/smn/Stock"
OBSERVATION_IRI = "https://w3id.org/smn/Observation"
INVENTED_IRI = "https://w3id.org/smn/NoSuchTermHere"
QUANTITY_KIND_IRI = "http://qudt.org/vocab/quantitykind/Count"
UNIT_IRI = "http://qudt.org/vocab/unit/COUNT"


def _yaml_available() -> bool:
    try:
        import yaml  # noqa: F401
    except ImportError:
        return False
    return True


# The fixture package carries a reviewed EML sidecar, and the producer reads the
# closure paths out of it rather than guessing them, so every test built on that
# fixture needs PyYAML. The two tests at the end of this file are the ones that
# must run on BOTH dependency legs: a package with no sidecar needs no extra at
# all, and a package with one gets an error naming the extra rather than a
# silent fallback to the default paths.
_REQUIRES_YAML = pytest.mark.skipif(
    not _yaml_available(),
    reason="the reviewed EML sidecar needs PyYAML (the metasalmonpy[eml] extra)",
)


def _sdp(tmp_path: Path, measurement_term_iri: str = OBSERVED_IRI) -> str:
    """A copy of the bundled fixture, with the closure the fixture ships removed.

    Clearing is deliberate: a test must not be able to pass by reading what the
    fixture already put there.
    """
    target = tmp_path / "sdp"
    shutil.copytree(_DATA_DIR / "sdp-default", target)
    os.unlink(target / "metadata" / "semantic_vocabulary.csv")
    os.unlink(target / "reviewed_semantic_selections.csv")
    if measurement_term_iri != OBSERVED_IRI:
        dictionary_path = target / "metadata" / "column_dictionary.csv"
        dictionary_path.write_text(
            dictionary_path.read_text(encoding="utf-8").replace(
                OBSERVED_IRI, measurement_term_iri
            ),
            encoding="utf-8",
        )
    return str(target)


def _search_index() -> pd.DataFrame:
    """A deterministic stand-in index for ``find_terms()``, keyed by query text.

    Keyed by the query so that the IRI-to-query derivation is exercised rather
    than bypassed.
    """
    return pd.DataFrame(
        [
            {
                "query": "observed rate or abundance",
                "iri": OBSERVED_IRI,
                "label": "Observed rate or abundance",
                "definition": "An empirically observed compound measurement variable.",
                "source": "smn",
                "ontology": "smn",
                "resource_kind": "Class",
                "type_iris": "http://www.w3.org/2002/07/owl#Class",
            },
            {
                "query": "stock",
                "iri": STOCK_IRI,
                "label": "Stock",
                "definition": "An operationally defined grouping of salmon.",
                "source": "smn",
                "ontology": "smn",
                "resource_kind": "Class",
                "type_iris": "http://www.w3.org/2002/07/owl#Class",
            },
            {
                "query": "observation",
                "iri": OBSERVATION_IRI,
                "label": "Observation",
                "definition": "An act of observing a property of a feature of interest.",
                "source": "smn",
                "ontology": "smn",
                "resource_kind": "Class",
                "type_iris": "http://www.w3.org/2002/07/owl#Class",
            },
        ]
    )


def _search_stub(index: pd.DataFrame = None, calls: list = None):
    frame = _search_index() if index is None else index

    def search_fn(query, role=None, sources=None):
        if calls is not None:
            calls.append(query)
        hits = frame[frame["query"] == query].drop(columns=["query"]).copy()
        hits["score"] = [0.9] * len(hits)
        return hits.reset_index(drop=True)

    return search_fn


def _degraded_stub(failed: str = "gcdfo"):
    """An empty result carrying the failed-source diagnostics ``find_terms()`` attaches.

    This is the harder half: the call returns normally, and only the attribute
    says the answer is unknown.
    """

    def search_fn(query, role=None, sources=None):
        hits = pd.DataFrame(
            columns=[
                "label",
                "iri",
                "definition",
                "source",
                "ontology",
                "resource_kind",
                "type_iris",
                "score",
            ]
        )
        hits.attrs["diagnostics"] = pd.DataFrame(
            [
                {
                    "source": failed,
                    "query": query,
                    "status": "http_error",
                    "count": 0,
                    "elapsed_secs": 0.0,
                    "error": "HTTP 503",
                }
            ]
        )
        return hits

    return search_fn


def _qudt_evidence() -> pd.DataFrame:
    """The two QUDT rows the fixture's dictionary uses.

    QUDT is not a searchable source, so this is what a hand-authored ``evidence``
    row looks like.
    """
    return pd.DataFrame(
        [
            {
                "iri": QUANTITY_KIND_IRI,
                "label": "Count",
                "definition": "A quantity kind for counts.",
                "source": "qudt",
                "ontology": "qudt",
                "resource_kind": "QuantityKind",
                "type_iris": "http://qudt.org/schema/qudt/QuantityKind",
                "native_type": "qudt:QuantityKind",
                "source_url": "https://qudt.org/3.1.1/vocab/quantitykind/",
                "confidence": "high",
                "review_rationale": "The column contains abundance counts.",
            },
            {
                "iri": UNIT_IRI,
                "label": "Count",
                "definition": "A counting unit.",
                "source": "qudt",
                "ontology": "qudt",
                "resource_kind": "Unit",
                "type_iris": "http://qudt.org/schema/qudt/Unit",
                "native_type": "qudt:Unit",
                "source_url": "https://qudt.org/3.1.1/vocab/unit/",
                "confidence": "high",
                "review_rationale": "The values use the reviewed counting unit.",
            },
        ]
    )


def _judgements(rows) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"iri": iri, "confidence": confidence, "review_rationale": rationale}
            for iri, confidence, rationale in rows
        ]
    )


def _reviewed_evidence() -> pd.DataFrame:
    """What a reviewer actually supplies.

    The QUDT rows in full, plus their own judgement on every other target. Term
    evidence for the smn IRIs is left to the search, which is the division of
    labour the function is built around.
    """
    return pd.concat(
        [
            _qudt_evidence(),
            _judgements(
                [
                    (
                        OBSERVED_IRI,
                        "medium",
                        "The reviewed measurement type is intentionally broad.",
                    ),
                    (STOCK_IRI, "high", "The counts describe a salmon stock."),
                    (OBSERVATION_IRI, "high", "The row grain is an observation."),
                ]
            ),
        ],
        ignore_index=True,
    )


def _unresolvable_evidence() -> pd.DataFrame:
    """Every rationale supplied, so the only warning a run can raise is the gap."""
    return pd.concat(
        [
            _reviewed_evidence(),
            _judgements(
                [(INVENTED_IRI, "low", "Placeholder selection pending a minted term.")]
            ),
        ],
        ignore_index=True,
    )


def _full_evidence() -> pd.DataFrame:
    smn = []
    for iri, label, definition, confidence, rationale in (
        (
            OBSERVED_IRI,
            "Observed rate or abundance",
            "An empirically observed compound measurement variable.",
            "medium",
            "The reviewed measurement type is intentionally broad.",
        ),
        (
            STOCK_IRI,
            "Stock",
            "An operationally defined grouping of salmon.",
            "high",
            "The counts describe a salmon stock.",
        ),
        (
            OBSERVATION_IRI,
            "Observation",
            "An act of observing a property of a feature of interest.",
            "high",
            "The row grain is an observation.",
        ),
    ):
        smn.append(
            {
                "iri": iri,
                "label": label,
                "definition": definition,
                "source": "smn",
                "ontology": "smn",
                "resource_kind": "Class",
                "type_iris": "http://www.w3.org/2002/07/owl#Class",
                "native_type": "owl:Class",
                "source_url": "https://w3id.org/smn/",
                "confidence": confidence,
                "review_rationale": rationale,
            }
        )
    return pd.concat([_qudt_evidence(), pd.DataFrame(smn)], ignore_index=True)


def _file_sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_the_vocabulary_field_order_matches_the_digest_verifier_exactly():
    # The digest is over these ten values joined by "\r". If the producer's field
    # list and the verifier's ever diverged, every hash the producer writes would
    # be wrong and ``_read_vocabulary()`` would reject a file it should accept,
    # with a message that does not say why.
    #
    # Python can import the verifier's own tuple, so the producer derives its
    # list rather than restating it and the two cannot diverge at all. R keeps a
    # second copy and pins it by evaluating the verifier's body, because R has no
    # way to reach a constant inside a function. This asserts the derivation, so
    # that replacing it with a literal fails here.
    assert closure_module._VOCABULARY_FIELDS == tuple(
        eml_module._VOCABULARY_SNAPSHOT_FIELDS
    )
    assert closure_module._VOCABULARY_FIELDS[0] == "iri"
    assert len(closure_module._VOCABULARY_FIELDS) == 10


@_REQUIRES_YAML
def test_the_two_canonical_sets_differ_and_the_producer_derives_both(tmp_path):
    path = _sdp(tmp_path)

    closure = write_sdp_semantic_closure(
        path,
        evidence=_reviewed_evidence(),
        search_fn=_search_stub(),
        quiet=True,
    )

    # The bundled-fixture difference, which is the same shape the shipped Fraser
    # coho example has: the table-level observation unit is a review target and
    # not a measurement vocabulary term.
    assert set(closure["review_targets"]["iri"]) - set(
        closure["measurement_iris"]
    ) == {OBSERVATION_IRI}
    assert set(closure["measurement_iris"]) - set(
        closure["review_targets"]["iri"]
    ) == set()
    assert len(closure["vocabulary"]) == 4
    assert len(closure["review"]) == 5
    assert len(closure["gaps"]) == 0


@_REQUIRES_YAML
def test_both_written_files_satisfy_the_validators_that_had_no_producer(tmp_path):
    import yaml

    path = _sdp(tmp_path)

    # No warning: nothing is unresolved and every rationale was supplied.
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        closure = write_sdp_semantic_closure(
            path,
            evidence=_reviewed_evidence(),
            search_fn=_search_stub(),
            quiet=True,
        )

    assert Path(closure["files"]["vocabulary"]).is_file()
    assert Path(closure["files"]["review"]).is_file()

    pkg = read_salmon_datapackage(path)
    with open(Path(path) / "metadata" / "eml-mapping.yml", encoding="utf-8") as handle:
        mapping = yaml.safe_load(handle)

    # These two calls are the point of the item: before it, nothing in the
    # package could produce a file either of them accepts.
    vocabulary = eml_module._read_vocabulary(Path(path), pkg, mapping)
    assert len(vocabulary) == 4
    review = eml_module._read_semantic_review(Path(path), pkg, mapping)
    assert len(review) == 5
    assert list(review["decision"]) == ["accepted"] * 5


@_REQUIRES_YAML
def test_the_sidecar_digests_are_rewritten_so_no_user_hand_writes_one(tmp_path):
    import yaml

    path = _sdp(tmp_path)

    closure = write_sdp_semantic_closure(
        path,
        evidence=_reviewed_evidence(),
        search_fn=_search_stub(),
        quiet=True,
    )

    with open(Path(path) / "metadata" / "eml-mapping.yml", encoding="utf-8") as handle:
        mapping = yaml.safe_load(handle)
    assert mapping["semantic_vocabulary"]["sha256"] == _file_sha256(
        closure["files"]["vocabulary"]
    )
    assert mapping["semantic_review"]["sha256"] == _file_sha256(
        closure["files"]["review"]
    )
    # Every row's own snapshot digest, recomputed from the row as written.
    written = eml_module._read_character_csv(closure["files"]["vocabulary"])
    expected = [
        eml_module._vocabulary_snapshot_sha256(written.iloc[index].to_dict())
        for index in range(len(written))
    ]
    assert list(written["reviewed_snapshot_sha256"]) == expected


@_REQUIRES_YAML
def test_an_unresolvable_iri_becomes_a_gap_and_both_files_are_still_written(tmp_path):
    import yaml

    path = _sdp(tmp_path, measurement_term_iri=INVENTED_IRI)

    # The stub knows nothing about the invented IRI, so its evidence cannot be
    # resolved and no ``evidence`` row supplies it. A rationale IS supplied for
    # it, so the gap warning is the only warning this call can raise.
    with pytest.warns(RuntimeWarning, match="could not be resolved"):
        closure = write_sdp_semantic_closure(
            path,
            evidence=_unresolvable_evidence(),
            search_fn=_search_stub(),
            quiet=True,
        )

    # GAP, NOT ABORT: the files exist.
    assert Path(closure["files"]["vocabulary"]).is_file()
    assert Path(closure["files"]["review"]).is_file()
    assert len(closure["review"]) == 5

    # And the gap is in the shape the term-request pipeline consumes.
    gaps = closure["gaps"]
    assert len(gaps) == 1
    assert list(gaps["unresolved_iri"]) == [INVENTED_IRI]
    assert list(gaps["gap_detection_basis"]) == ["no_candidates"]
    assert list(gaps["target_sdp_field"]) == ["term_iri"]
    assert list(gaps["dictionary_role"]) == ["variable"]
    assert list(gaps["candidate_count"]) == [0]
    # The IRI's own namespace says where the term would have to be minted.
    assert list(gaps["placement_recommendation"]) == ["smn"]
    assert set(GAP_COLUMNS) <= set(gaps.columns)

    # The unresolved row is omitted rather than invented, so the vocabulary is
    # short by exactly one and the EML gate says so in the same terms.
    assert len(closure["vocabulary"]) == 3
    assert INVENTED_IRI not in set(closure["vocabulary"]["iri"])
    pkg = read_salmon_datapackage(path)
    with open(Path(path) / "metadata" / "eml-mapping.yml", encoding="utf-8") as handle:
        mapping = yaml.safe_load(handle)
    with pytest.raises(ValueError, match="canonical measurement IRI"):
        eml_module._read_vocabulary(Path(path), pkg, mapping)


@_REQUIRES_YAML
def test_the_gap_table_is_accepted_by_render_ontology_term_request(tmp_path):
    path = _sdp(tmp_path, measurement_term_iri=INVENTED_IRI)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        closure = write_sdp_semantic_closure(
            path,
            evidence=_unresolvable_evidence(),
            search_fn=_search_stub(),
            quiet=True,
        )
    requests = render_ontology_term_request(closure["gaps"], ask=False)
    assert len(requests) >= 1
    assert "request_scope" in requests.columns


@_REQUIRES_YAML
def test_no_llm_request_is_constructed_on_the_closure_path(tmp_path, monkeypatch):
    path = _sdp(tmp_path)

    # A raising binding on the one function that performs the provider call is
    # the sentinel: if anything on this path reached the LLM, this test fails.
    from metasalmonpy import llm_review

    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("LLM review must never run on the closure path.")

    monkeypatch.setattr(llm_review, "request_json", explode)
    monkeypatch.setattr(llm_review, "_request_json_with_retries", explode)

    closure = write_sdp_semantic_closure(
        path,
        evidence=_reviewed_evidence(),
        search_fn=_search_stub(),
        quiet=True,
    )
    assert len(closure["vocabulary"]) == 4


@_REQUIRES_YAML
def test_fully_hand_supplied_evidence_runs_no_search_at_all(tmp_path):
    path = _sdp(tmp_path)

    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("search_fn must not be called.")

    closure = write_sdp_semantic_closure(
        path,
        evidence=_full_evidence(),
        search_fn=explode,
        quiet=True,
    )
    assert len(closure["vocabulary"]) == 4
    assert len(closure["gaps"]) == 0
    assert len(closure["placeholders"]) == 0


@_REQUIRES_YAML
def test_the_iri_local_name_is_the_query_when_no_review_trail_exists(tmp_path):
    path = _sdp(tmp_path)
    calls: list = []

    write_sdp_semantic_closure(
        path,
        evidence=_reviewed_evidence(),
        search_fn=_search_stub(calls=calls),
        quiet=True,
    )
    # ``ObservedRateOrAbundance`` -> "observed rate or abundance", ``Stock`` ->
    # "stock". Recovering the reviewer's query from the IRI they accepted is what
    # makes evidence re-resolvable without asking the user to restate it.
    assert "observed rate or abundance" in calls
    assert "stock" in calls
    # QUDT rows are fully supplied, so they are never searched.
    assert "count" not in calls


@_REQUIRES_YAML
def test_a_recorded_decision_reason_becomes_the_review_rationale(tmp_path):
    path = _sdp(tmp_path)
    pd.DataFrame(
        [
            {
                "dataset_id": "demo-salmon-2026",
                "table_id": "counts",
                "column_name": "count",
                "target_sdp_field": "term_iri",
                "iri": OBSERVED_IRI,
                "search_query": "observed rate or abundance",
                "decision": "accepted",
                "decision_reason": "Chosen during review because the grain is a rate.",
            }
        ]
    ).to_csv(Path(path) / "semantic_suggestions.csv", index=False)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        closure = write_sdp_semantic_closure(
            path,
            evidence=_qudt_evidence(),
            search_fn=_search_stub(),
            quiet=True,
        )
    review = closure["review"]
    row = review[review["target_sdp_field"] == "term_iri"]
    assert list(row["review_rationale"]) == [
        "Chosen during review because the grain is a rate."
    ]


@_REQUIRES_YAML
def test_a_target_with_no_recorded_rationale_gets_a_review_required_marker(tmp_path):
    path = _sdp(tmp_path)
    evidence = _qudt_evidence().drop(columns=["confidence", "review_rationale"])

    with pytest.warns(RuntimeWarning, match="REVIEW REQUIRED"):
        closure = write_sdp_semantic_closure(
            path,
            # Vocabulary evidence only: no judgement columns anywhere.
            evidence=evidence,
            search_fn=_search_stub(),
            quiet=True,
        )
    assert len(closure["placeholders"]) == 5
    assert all(
        value.startswith("REVIEW REQUIRED:")
        for value in closure["placeholders"]["review_rationale"]
    )
    assert list(closure["review"]["confidence"]) == ["unassessed"] * 5


@_REQUIRES_YAML
def test_supplied_evidence_overlays_a_searched_term_field_by_field(tmp_path):
    """A row may correct ONE field and leave the rest to the search.

    Verified against R rather than only asserted: both producers, same package,
    same injected search, same evidence -- identical label, identical definition,
    and byte-identical files.
    """
    path = _sdp(tmp_path)
    evidence = _reviewed_evidence()
    evidence.loc[evidence["iri"] == STOCK_IRI, "label"] = "Stock (reviewer label)"

    closure = write_sdp_semantic_closure(
        path, evidence=evidence, search_fn=_search_stub(), quiet=True
    )

    stock = closure["vocabulary"][closure["vocabulary"]["iri"] == STOCK_IRI]
    # The supplied field wins.
    assert list(stock["label"]) == ["Stock (reviewer label)"]
    # And every field it did NOT supply still comes from the search.
    assert list(stock["definition"]) == [
        "An operationally defined grouping of salmon."
    ]
    assert list(stock["source"]) == ["smn"]
    # `native_type` and `source_url` are derived from the resolved source, which
    # is the pair no search fills.
    assert list(stock["native_type"]) == ["owl:Class"]
    assert list(stock["source_url"]) == ["https://w3id.org/smn/"]


@_REQUIRES_YAML
def test_a_target_sdp_field_row_narrows_evidence_to_one_slot(tmp_path):
    """An IRI selected in more than one slot can carry per-slot judgement.

    The scoped row wins for its own slot; the IRI-wide row still serves the rest.
    """
    path = _sdp(tmp_path)
    evidence = pd.concat(
        [
            pd.DataFrame(
                [
                    {
                        "iri": OBSERVATION_IRI,
                        "target_sdp_field": "observation_unit_iri",
                        "confidence": "scoped-high",
                    }
                ]
            ),
            _reviewed_evidence(),
        ],
        ignore_index=True,
    )

    closure = write_sdp_semantic_closure(
        path, evidence=evidence, search_fn=_search_stub(), quiet=True
    )

    review = closure["review"]
    scoped = review[review["target_sdp_field"] == "observation_unit_iri"]
    assert list(scoped["confidence"]) == ["scoped-high"]
    # Every other slot keeps the value its IRI-wide row supplied.
    assert list(review[review["target_sdp_field"] == "term_iri"]["confidence"]) == [
        "medium"
    ]
    assert list(review[review["target_sdp_field"] == "unit_iri"]["confidence"]) == [
        "high"
    ]


@_REQUIRES_YAML
def test_two_evidence_rows_for_one_iri_and_slot_are_refused(tmp_path):
    # The same guard R applies, in the same place: a second row for one IRI and
    # target field would make the evidence say two things about one slot.
    path = _sdp(tmp_path)
    evidence = pd.concat(
        [
            _reviewed_evidence(),
            pd.DataFrame([{"iri": STOCK_IRI, "label": "A second general row"}]),
        ],
        ignore_index=True,
    )
    with pytest.raises(ValueError, match="at most one row per IRI"):
        write_sdp_semantic_closure(
            path, evidence=evidence, search_fn=_search_stub(), quiet=True
        )


@_REQUIRES_YAML
def test_both_files_are_written_in_c_collation_order(tmp_path):
    path = _sdp(tmp_path)

    closure = write_sdp_semantic_closure(
        path,
        evidence=_reviewed_evidence(),
        search_fn=_search_stub(),
        quiet=True,
    )
    iris = list(closure["vocabulary"]["iri"])
    assert iris == sorted(iris)
    key = [
        "\r".join(
            str(closure["review"][name].iloc[index])
            for name in (
                "dataset_id",
                "table_id",
                "column_name",
                "target_scope",
                "target_sdp_field",
                "dictionary_role",
                "iri",
            )
        )
        for index in range(len(closure["review"]))
    ]
    assert key == sorted(key)
    # What was sorted is what was written: the file bytes carry the same order.
    written = eml_module._read_character_csv(closure["files"]["vocabulary"])
    assert list(written["iri"]) == iris


@_REQUIRES_YAML
def test_the_producer_is_byte_reproducible_across_two_runs(tmp_path):
    path = _sdp(tmp_path)

    first = write_sdp_semantic_closure(
        path,
        evidence=_reviewed_evidence(),
        search_fn=_search_stub(),
        quiet=True,
    )
    first_digests = [
        _file_sha256(first["files"][name]) for name in ("vocabulary", "review")
    ]
    second = write_sdp_semantic_closure(
        path,
        evidence=_reviewed_evidence(),
        search_fn=_search_stub(),
        quiet=True,
    )
    second_digests = [
        _file_sha256(second["files"][name]) for name in ("vocabulary", "review")
    ]
    assert first_digests == second_digests


@_REQUIRES_YAML
def test_evidence_must_be_rows_not_a_parsed_object(tmp_path):
    path = _sdp(tmp_path)

    with pytest.raises(ValueError, match="must be a data frame"):
        write_sdp_semantic_closure(path, evidence="metadata/vocab.csv", quiet=True)
    with pytest.raises(ValueError, match="must have an 'iri' column"):
        write_sdp_semantic_closure(
            path, evidence=pd.DataFrame([{"label": "x"}]), quiet=True
        )
    with pytest.raises(ValueError, match="cannot use"):
        write_sdp_semantic_closure(
            path,
            evidence=pd.DataFrame([{"iri": "x", "not_a_field": "y"}]),
            quiet=True,
        )
    with pytest.raises(ValueError, match="at most one row per IRI"):
        write_sdp_semantic_closure(
            path,
            evidence=pd.DataFrame(
                [{"iri": "x", "label": "a"}, {"iri": "x", "label": "b"}]
            ),
            quiet=True,
        )


@_REQUIRES_YAML
def test_an_inline_sidecar_key_is_reported_rather_than_rewritten(tmp_path):
    import yaml

    path = _sdp(tmp_path)
    mapping_path = Path(path) / "metadata" / "eml-mapping.yml"
    lines = mapping_path.read_text(encoding="utf-8").splitlines()

    # Flow style. Pinning the digest here would mean rewriting the document, so
    # the producer says so instead of appending a second key and making the YAML
    # say two things. This path is otherwise unreachable, which is why it has a
    # test.
    start = next(
        index for index, line in enumerate(lines) if line.startswith("semantic_review:")
    )
    lines = (
        lines[:start]
        + ["semantic_review: {path: reviewed_semantic_selections.csv, sha256: 'x'}"]
        + lines[start + 3 :]
    )
    mapping_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.warns(RuntimeWarning, match="inline"):
        closure = write_sdp_semantic_closure(
            path,
            evidence=_reviewed_evidence(),
            search_fn=_search_stub(),
            quiet=True,
        )
    # The files are still written, and the vocabulary digest, which IS a block
    # mapping, is still pinned.
    assert Path(closure["files"]["review"]).is_file()
    with open(mapping_path, encoding="utf-8") as handle:
        mapping = yaml.safe_load(handle)
    assert mapping["semantic_vocabulary"]["sha256"] == _file_sha256(
        closure["files"]["vocabulary"]
    )
    assert mapping["semantic_review"]["sha256"] == "x"


def test_a_missing_package_directory_is_refused_before_anything_is_read(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        write_sdp_semantic_closure(str(tmp_path / "absent"), quiet=True)


# ---------------------------------------------------------------------------
# A LOOKUP THAT DID NOT ANSWER IS NOT AN ONTOLOGY GAP.
#
# These three are the same defect arriving by the routes a search has for
# failing to answer, and the reason they are one subject: in R each once produced
# a ``no_candidates`` gap row, an omitted vocabulary row, and a written closure
# -- which turns B-116's ruled gap-not-abort shape into a request that an
# ontology mint a term nobody established was missing. ``find_terms()`` already
# warns, in its own words, that such a result is unknown rather than an ontology
# gap.
# ---------------------------------------------------------------------------


@_REQUIRES_YAML
def test_a_search_that_raises_aborts_rather_than_manufacturing_a_gap(tmp_path):
    path = _sdp(tmp_path)

    def explode(*args, **kwargs):
        raise ConnectionError("connection reset by peer")

    with pytest.raises(RuntimeError, match="did not answer"):
        write_sdp_semantic_closure(
            path,
            evidence=_reviewed_evidence(),
            search_fn=explode,
            quiet=True,
        )

    # NOTHING WAS WRITTEN. The remedy is to re-run, so a half-derived closure on
    # disk would make the retry start from worse state than the first attempt did.
    assert not (Path(path) / "metadata" / "semantic_vocabulary.csv").exists()
    assert not (Path(path) / "reviewed_semantic_selections.csv").exists()


@_REQUIRES_YAML
def test_a_raised_search_error_is_redacted_where_it_is_captured(tmp_path):
    """External text is redacted at CAPTURE, not at display.

    Python's half of the cli-safety contract (PARITY.md row 37): there is no
    escaping layer, because an exception message is a finished string, but the
    redaction half applies exactly as it does in R. The message reaches a user,
    so a provider error carrying a credential must not carry it here.
    """
    path = _sdp(tmp_path)

    def explode(*args, **kwargs):
        raise ConnectionError("refused: api_key=sk-not-a-real-secret-value")

    with pytest.raises(RuntimeError) as caught:
        write_sdp_semantic_closure(
            path,
            evidence=_reviewed_evidence(),
            search_fn=explode,
            quiet=True,
        )
    assert "sk-not-a-real-secret-value" not in str(caught.value)
    assert "did not answer" in str(caught.value)


@_REQUIRES_YAML
def test_an_empty_result_with_failed_source_diagnostics_is_not_a_gap(tmp_path):
    path = _sdp(tmp_path)

    with pytest.raises(RuntimeError, match="did not answer"):
        write_sdp_semantic_closure(
            path,
            evidence=_reviewed_evidence(),
            search_fn=_degraded_stub(),
            quiet=True,
        )
    assert not (Path(path) / "metadata" / "semantic_vocabulary.csv").exists()
    assert not (Path(path) / "reviewed_semantic_selections.csv").exists()


def test_the_degraded_status_test_is_the_one_find_terms_applies():
    # Read rather than restated, so the producer's abort and ``find_terms()``'s
    # own warning cannot drift apart about what "did not answer" means.
    assert _search_failed_sources(
        pd.DataFrame(
            {"source": ["smn", "gcdfo"], "status": ["success", "http_error"]}
        )
    ) == ["gcdfo"]
    assert _search_failed_sources(
        pd.DataFrame({"source": ["smn"], "status": ["success"]})
    ) == []
    # Whatever ``attrs`` yields, including nothing at all.
    assert _search_failed_sources(None) == []
    assert _search_failed_sources(pd.DataFrame({"source": ["smn"]})) == []


@_REQUIRES_YAML
def test_a_term_found_with_a_blank_required_field_is_incomplete_not_a_gap(tmp_path):
    path = _sdp(tmp_path)

    # An ontology class with no definition. The IRI matches exactly, so the term
    # was FOUND; what is missing is evidence about it.
    index = _search_index()
    index.loc[index["iri"] == OBSERVED_IRI, "definition"] = ""

    with pytest.warns(RuntimeWarning, match="not an ontology gap"):
        closure = write_sdp_semantic_closure(
            path,
            evidence=_reviewed_evidence(),
            search_fn=_search_stub(index=index),
            quiet=True,
        )

    # NOT a gap: nothing asks the term-request pipeline to mint what was found.
    assert len(closure["gaps"]) == 0
    incomplete = closure["incomplete"]
    assert len(incomplete) == 1
    assert list(incomplete["iri"]) == [OBSERVED_IRI]
    assert list(incomplete["missing_fields"]) == ["definition"]
    assert list(incomplete["resolved_source"]) == ["smn"]
    assert list(incomplete["target_sdp_field"]) == ["term_iri"]
    assert list(incomplete["dictionary_role"]) == ["variable"]
    # The row is still omitted, because the validator refuses a blank definition,
    # and the other three are still written.
    assert len(closure["vocabulary"]) == 3
    assert Path(closure["files"]["vocabulary"]).is_file()


@_REQUIRES_YAML
@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_a_linked_closure_output_is_refused_and_its_target_is_left_alone(tmp_path):
    path = _sdp(tmp_path)

    # The threat model: an SDP that arrived from a collaborator, whose own
    # content names a file the process can write.
    outside = tmp_path / "outside"
    outside.mkdir()
    external = outside / "keep-me.yml"
    # A YAML mapping, so the sidecar iteration exercises the write rather than
    # tripping over an unparseable link target on its way there.
    keep = "keep_me: true\nother: 1\n"

    for relative in (
        "metadata/semantic_vocabulary.csv",
        "reviewed_semantic_selections.csv",
        "metadata/eml-mapping.yml",
    ):
        # Rewritten each pass: the point of the loop is that each of the three
        # names is refused on its own, so each starts from an untouched target.
        external.write_text(keep, encoding="utf-8")
        target = Path(path) / relative
        original = target.read_bytes() if target.exists() else None
        if target.exists():
            target.unlink()
        target.symlink_to(external)

        with pytest.raises(SdpExtensionError, match="refuses to write"):
            write_sdp_semantic_closure(
                path,
                evidence=_reviewed_evidence(),
                search_fn=_search_stub(),
                quiet=True,
            )
        assert external.read_text(encoding="utf-8") == keep

        target.unlink()
        if original is not None:
            target.write_bytes(original)


@_REQUIRES_YAML
def test_a_failure_in_the_third_write_leaves_the_first_two_unchanged(
    tmp_path, monkeypatch
):
    path = _sdp(tmp_path)

    # A valid closure whose sidecar digests match its files.
    first = write_sdp_semantic_closure(
        path,
        evidence=_reviewed_evidence(),
        search_fn=_search_stub(),
        quiet=True,
    )
    mapping_path = Path(path) / "metadata" / "eml-mapping.yml"
    before_vocabulary = Path(first["files"]["vocabulary"]).read_bytes()
    before_mapping = mapping_path.read_bytes()

    # A second run that WOULD write different bytes, so a surviving old file is
    # distinguishable from a rewritten identical one. THE LAST ASSERTION IN THIS
    # TEST IS WHAT MAKES THAT TRUE RATHER THAN ASSUMED: the first draft of this
    # test supplied the changed definition on a `target_sdp_field`-scoped row,
    # which the vocabulary overlay correctly ignores, so both halves compared
    # identical bytes against identical bytes and the test passed against a
    # deliberately non-atomic writer.
    changed = _reviewed_evidence()
    changed.loc[
        changed["iri"] == STOCK_IRI, "definition"
    ] = "A deliberately different definition."

    # Half one: the sidecar render fails. Before the three writes became one set,
    # both CSVs had already replaced their valid versions by the time this ran,
    # and the sidecar kept its old digests -- a package that fails its own digest
    # check even though the call raised.
    def explode(*args, **kwargs):
        raise RuntimeError("sidecar digest render failed")

    monkeypatch.setattr(closure_module, "_set_mapping_digest", explode)
    with pytest.raises(RuntimeError, match="sidecar digest render failed"):
        write_sdp_semantic_closure(
            path, evidence=changed, search_fn=_search_stub(), quiet=True
        )
    monkeypatch.undo()
    assert Path(first["files"]["vocabulary"]).read_bytes() == before_vocabulary
    assert mapping_path.read_bytes() == before_mapping

    # Half two, with no mock in it: a stray directory where the ledger belongs
    # fails the second install. The first must not already be installed.
    review_path = Path(first["files"]["review"])
    review_path.unlink()
    review_path.mkdir()
    with pytest.raises(SdpExtensionError, match="directory"):
        write_sdp_semantic_closure(
            path, evidence=changed, search_fn=_search_stub(), quiet=True
        )
    assert Path(first["files"]["vocabulary"]).read_bytes() == before_vocabulary
    assert mapping_path.read_bytes() == before_mapping

    # AND THE TWO ASSERTIONS ABOVE ARE NOT VACUOUS. Clear the obstruction and let
    # the same call succeed: if `changed` did not really change the vocabulary
    # bytes, "unchanged" would have been true whatever the writer did.
    review_path.rmdir()
    write_sdp_semantic_closure(
        path, evidence=changed, search_fn=_search_stub(), quiet=True
    )
    assert Path(first["files"]["vocabulary"]).read_bytes() != before_vocabulary
    assert mapping_path.read_bytes() != before_mapping


@_REQUIRES_YAML
def test_an_empty_closure_writes_header_only_files(tmp_path):
    """A package with nothing annotated still gets both files, with headers only.

    The branch where no vocabulary row survives is the one where the per-row
    digest column has no rows to compute over, and it is reachable from an
    ordinary package: every measurement column left undecided.
    """
    path = _sdp(tmp_path)
    dictionary_path = Path(path) / "metadata" / "column_dictionary.csv"
    dictionary_path.write_text(
        dictionary_path.read_text(encoding="utf-8")
        .replace(",measurement,", ",attribute,")
        .replace(OBSERVED_IRI, "")
        .replace(STOCK_IRI, "")
        .replace(QUANTITY_KIND_IRI, "")
        .replace(UNIT_IRI, ""),
        encoding="utf-8",
    )
    tables_path = Path(path) / "metadata" / "tables.csv"
    tables_path.write_text(
        tables_path.read_text(encoding="utf-8").replace(OBSERVATION_IRI, ""),
        encoding="utf-8",
    )

    closure = write_sdp_semantic_closure(path, search_fn=_search_stub(), quiet=True)

    assert len(closure["vocabulary"]) == 0
    assert len(closure["review"]) == 0
    assert len(closure["gaps"]) == 0
    assert len(closure["placeholders"]) == 0
    # The digest column exists even with no rows to compute it over, so the file
    # carries the header ``_read_vocabulary()`` requires.
    assert "reviewed_snapshot_sha256" in closure["vocabulary"].columns
    vocabulary_text = Path(closure["files"]["vocabulary"]).read_text(encoding="utf-8")
    assert vocabulary_text.splitlines() == [
        ",".join(list(closure_module._VOCABULARY_FIELDS) + ["reviewed_snapshot_sha256"])
    ]
    review_text = Path(closure["files"]["review"]).read_text(encoding="utf-8")
    assert review_text.splitlines() == [",".join(closure_module._LEDGER_FIELDS)]


# ---------------------------------------------------------------------------
# THE TWO TESTS BELOW RUN ON BOTH DEPENDENCY LEGS, and that is their point.
#
# The sidecar is full YAML, which lives in the optional ``metasalmonpy[eml]``
# extra, and the producer reads the declared closure paths out of it. Falling
# back to the default paths when PyYAML is absent was the alternative and is
# wrong: those declared paths are the only thing stopping this writing a file the
# sidecar does not point at. So the gate is an error naming the extra, and a
# package with no sidecar needs no extra at all.
# ---------------------------------------------------------------------------


def test_a_package_with_no_sidecar_needs_no_extra(tmp_path):
    path = _sdp(tmp_path)
    os.unlink(Path(path) / "metadata" / "eml-mapping.yml")

    closure = write_sdp_semantic_closure(
        path,
        evidence=_reviewed_evidence(),
        search_fn=_search_stub(),
        quiet=True,
    )

    assert closure["files"]["mapping"] is None
    assert closure["files"]["vocabulary"].endswith("metadata/semantic_vocabulary.csv")
    assert closure["files"]["review"].endswith("reviewed_semantic_selections.csv")
    assert len(closure["vocabulary"]) == 4
    assert len(closure["review"]) == 5


def test_a_sidecar_with_no_pyyaml_raises_an_error_naming_the_extra(tmp_path):
    import builtins

    path = _sdp(tmp_path)
    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("blocked yaml")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = blocked
    try:
        with pytest.raises(ImportError) as caught:
            write_sdp_semantic_closure(
                path,
                evidence=_reviewed_evidence(),
                search_fn=_search_stub(),
                quiet=True,
            )
    finally:
        builtins.__import__ = real_import
    message = str(caught.value)
    assert "metasalmonpy[eml]" in message
    assert "eml-mapping.yml" in message
    # AND NOTHING WAS WRITTEN TO THE DEFAULT PATHS. A fallback would have looked
    # like success here.
    assert not (Path(path) / "metadata" / "semantic_vocabulary.csv").exists()
    assert not (Path(path) / "reviewed_semantic_selections.csv").exists()
