"""Free-text metadata review and editing (stream S5 port, metasalmon 0.5.0).

The contract this module is judged against, and the two tests that carry it:

* ``test_review_metadata_then_the_printed_calls_reach_strict_validation`` --
  every printed call is EXECUTED, with only the ``<...>`` placeholder replaced,
  and when the last gap row is gone
  ``validate_salmon_datapackage(require_iris=True)`` passes. If a program's
  output is meant to be run, the tests have to run it: a printed call that
  names a column that does not exist passes every substring assertion ever
  written.
* ``test_a_setter_patch_produces_the_descriptor_a_rebuild_would`` -- the
  surgical patch and the full rebuild share the three descriptor builders, and
  this measures that rather than trusting it. The rule that would catch
  CSV/descriptor drift is one of the dead rules in ``sdp.rules.yaml``.
"""

from __future__ import annotations

import functools
import json
import re
from pathlib import Path

import pandas as pd
import pytest

from metasalmonpy import (
    apply_sdp_semantics,
    create_sdp,
    reject_suggestion,
    review_metadata,
    review_semantics,
    set_sdp_code,
    set_sdp_column,
    set_sdp_dataset,
    set_sdp_table,
    validate_salmon_datapackage,
    write_salmon_datapackage,
)
from metasalmonpy.sdp_field_setters import (
    MetadataReview,
    settable_required_fields,
)
from metasalmonpy.sdp_schema import sdp_schema_required_field_names

SPAWNER_IRI = "https://w3id.org/smn/SpawnerAbundance"


def _stub_search(query, role=None, sources=None):
    return pd.DataFrame(
        {
            "label": ["Spawner Abundance"],
            "iri": [SPAWNER_IRI],
            "source": ["smn"],
            "ontology": ["smn"],
            "role": [role],
            "match_type": ["label"],
            "definition": ["Mature salmon returning to spawn."],
            "score": [4.9],
        }
    )


@pytest.fixture
def raw_package(tmp_path, monkeypatch):
    """A freshly created package, every free-text field still a placeholder."""
    from metasalmonpy import semantics as sem

    monkeypatch.setattr(
        sem,
        "suggest_semantics",
        functools.partial(sem.suggest_semantics, search_fn=_stub_search),
    )
    return create_sdp(
        {
            "spawners": pd.DataFrame(
                {
                    "stream_name": ["Goldstream", "Craigflower"],
                    "spawner_count": [120, 44],
                }
            )
        },
        path=tmp_path / "pkg",
        dataset_id="demo-1",
        seed_semantics=True,
        seed_verbose=False,
        check_updates=False,
    )


# ---------------------------------------------------------------------------
# The constraints.required consumer
# ---------------------------------------------------------------------------


def test_required_fields_come_from_the_schemas_constraints_required():
    """The schema is the source, not an enumeration in this package.

    ``constraints.required`` had no parser and no consumer here before the S5
    port, so a field the spec calls required and one it calls optional were
    indistinguishable.
    """
    assert sdp_schema_required_field_names("dataset") == [
        "dataset_id",
        "title",
        "description",
        "creator",
        "contact_name",
        "contact_email",
        "license",
    ]
    # An optional field is not in the set.
    assert "contact_org" not in sdp_schema_required_field_names("dataset")


def test_the_addressing_keys_are_excluded_from_what_a_setter_can_fill():
    # A blank key is a structural defect validate_salmon_datapackage() reports
    # in every mode, not incomplete metadata a setter can fill.
    settable = settable_required_fields("column_dictionary.csv")
    assert "column_label" in settable
    for key in ("dataset_id", "table_id", "column_name"):
        assert key not in settable
        assert key in sdp_schema_required_field_names("column_dictionary")


# ---------------------------------------------------------------------------
# review_metadata
# ---------------------------------------------------------------------------


def test_review_metadata_reports_a_placeholder(raw_package):
    review = review_metadata(str(raw_package))
    assert isinstance(review, MetadataReview)
    rows = review.rows
    creator = rows[(rows["file"] == "dataset.csv") & (rows["field"] == "creator")]
    assert len(creator) == 1
    assert creator["reason"].iloc[0] == "placeholder"
    assert creator["current_value"].iloc[0].startswith("MISSING METADATA:")
    # The placeholder's own hint becomes the prompt, rather than being thrown
    # away and replaced with a generic one.
    assert "creator" in creator["hint"].iloc[0]


def test_review_metadata_reports_a_blank_observation_unit_iri(raw_package):
    rows = review_metadata(str(raw_package)).rows
    hit = rows[
        (rows["file"] == "tables.csv")
        & (rows["field"] == "observation_unit_iri")
    ]
    assert len(hit) == 1
    assert hit["reason"].iloc[0] == "iri"


def test_review_metadata_reports_a_blank_measurement_iri(raw_package):
    dict_csv = raw_package / "metadata" / "column_dictionary.csv"
    frame = pd.read_csv(dict_csv)
    frame.loc[frame["column_name"] == "spawner_count", "property_iri"] = None
    frame.to_csv(dict_csv, index=False)

    rows = review_metadata(str(raw_package)).rows
    hit = rows[
        (rows["file"] == "column_dictionary.csv")
        & (rows["column_name"] == "spawner_count")
        & (rows["field"] == "property_iri")
    ]
    assert len(hit) == 1
    assert hit["reason"].iloc[0] == "iri"


def test_a_review_marked_iri_is_reported_by_both_reviews(raw_package):
    """The division of labour between the two reviews, pinned.

    An unresolved ``REVIEW:`` IRI belongs to ``review_semantics()`` -- it has a
    draft value and a shortlist, which this scan has neither of -- and it still
    blocks strict validation, so this scan has to report it too. Reporting it in
    both places is a duplicate; reporting it in neither is the defect, because
    ``review_metadata()`` is the scan that promises to list everything blocking
    ``validate_salmon_datapackage(require_iris=True)`` and a user who leaves part
    of the semantic queue undecided reaches exactly this state.

    The two predicates stay separate. ``is_review_placeholder()`` keeps naming
    only the three prose spellings, because the license gate and the
    placeholder-reporting path in ``validate_salmon_datapackage()`` are built on
    that narrowness; ``_is_unfilled_iri()`` is the IRI-field predicate and adds
    the marker.
    """
    dict_csv = raw_package / "metadata" / "column_dictionary.csv"
    marked = pd.read_csv(dict_csv)
    marked = marked[marked["column_name"] == "spawner_count"]
    assert str(marked["property_iri"].iloc[0]).startswith("REVIEW:")

    rows = review_metadata(str(raw_package)).rows
    hit = rows[
        (rows["column_name"] == "spawner_count") & (rows["field"] == "property_iri")
    ]
    assert len(hit) == 1
    assert hit["reason"].iloc[0] == "iri"
    # The draft value is shown rather than hidden, so the reader can see there
    # is something to decide rather than something to invent.
    assert hit["current_value"].iloc[0].startswith("REVIEW:")

    queued = review_semantics(str(raw_package))
    assert (
        (queued["column_name"] == "spawner_count") & (queued["role"] == "property")
    ).any()
    # And the console footer points at the review that has the candidates.
    assert "review_semantics()" in str(review_metadata(str(raw_package)))


def test_a_prose_placeholder_is_not_reclassified_as_an_iri_gap(raw_package):
    """The narrow predicate stays narrow.

    ``is_review_placeholder()`` is mirrored from R's
    ``.ms_is_review_placeholder()`` and five other callers depend on it naming
    only the three prose spellings -- above all the license gate, which must not
    treat a bare ``REVIEW:`` IRI as prose. Widening it would have been the
    smaller diff and the wrong fix, so this pins that it was not widened.
    """
    from metasalmonpy.metadata import is_review_placeholder

    assert is_review_placeholder("MISSING METADATA: add a creator")
    assert is_review_placeholder("REVIEW REQUIRED: check this")
    assert not is_review_placeholder("REVIEW:https://w3id.org/smn/Anything")

    rows = review_metadata(str(raw_package)).rows
    creator = rows[(rows["file"] == "dataset.csv") & (rows["field"] == "creator")]
    assert creator["reason"].iloc[0] == "placeholder"


def test_review_metadata_sees_a_required_column_the_file_does_not_have(
    raw_package,
):
    """A column the file does not have is blank in every row.

    That is the rule the validator applies, so the two keep agreeing about what
    blocks -- and the printed call fills it, because ``_set_sdp_metadata()``
    adds the column it is asked to write.
    """
    dataset_csv = raw_package / "metadata" / "dataset.csv"
    frame = pd.read_csv(dataset_csv)
    frame = frame.drop(columns=["creator"])
    frame.to_csv(dataset_csv, index=False)

    rows = review_metadata(str(raw_package)).rows
    hit = rows[(rows["file"] == "dataset.csv") & (rows["field"] == "creator")]
    assert len(hit) == 1
    assert hit["reason"].iloc[0] == "required"

    set_sdp_dataset(str(raw_package), creator="DFO", quiet=True)
    assert "creator" in pd.read_csv(dataset_csv).columns
    assert pd.read_csv(dataset_csv)["creator"].iloc[0] == "DFO"


def test_review_metadata_sees_a_gap_no_retrieval_ever_touched(raw_package):
    """What review_semantics() structurally cannot see.

    ``review_semantics()`` builds its queue from retrieved suggestions, so a
    field nothing was found for never appears there. A gap scan built from the
    package's own required-field rules treats an empty shortlist and a full one
    the same way.
    """
    # Drop every suggestion, so retrieval "found nothing" for everything.
    (raw_package / "semantic_suggestions.csv").unlink()
    with pytest.raises(ValueError, match="No semantic suggestions"):
        review_semantics(str(raw_package))
    assert len(review_metadata(str(raw_package))) > 0


def test_review_metadata_never_calls_retrieval(raw_package, monkeypatch):
    from metasalmonpy import term_search

    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("review_metadata() reached retrieval")

    monkeypatch.setattr(term_search, "find_terms", explode)
    assert len(review_metadata(str(raw_package))) > 0


class _NetworkReached(BaseException):
    """A sentinel that ``load_sdp_schema()`` cannot swallow.

    Deliberately NOT an ``Exception``: the loader catches ``Exception`` and falls
    back to the vendored bundle, so an ordinary raising stub would be absorbed
    and the test would pass whether or not the path is offline. This is the same
    reason the R side proves LLM opt-in with an injected function nothing
    catches.
    """


def test_review_metadata_makes_no_http_request_on_the_default_schema_source(
    raw_package, monkeypatch
):
    """The no-network contract, proved by a sentinel rather than by reading.

    ``review_metadata()`` documents that it never contacts a network, and the
    scan is built on the schema's ``constraints.required`` -- so it reads the
    schema bundle. On the shipped default source (``"auto"``) ``load_sdp_schema()``
    performs an HTTP fetch of six documents before falling back to the vendored
    copy, which makes a documented-local path network-dependent and costs a
    timeout per document when there is no network.

    ``tests/conftest.py`` pins ``sdp_schema_source="vendored"`` for the whole
    suite, which is why nothing caught this: that pin is a fact about the test
    environment, not evidence about the default. This test therefore un-pins it
    and clears the caches, so it runs in the fresh-process, default-source
    configuration a user gets.
    """
    import requests

    from metasalmonpy import sdp_schema

    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise _NetworkReached("review_metadata() made an HTTP request")

    # The package is built first, under the suite's vendored pin: create_sdp()
    # has no offline contract and is not what is under test here.
    assert raw_package.is_dir()

    monkeypatch.delenv("METASALMONPY_SDP_SCHEMA_SOURCE", raising=False)
    sdp_schema.set_sdp_schema_source(None)
    sdp_schema.reset_schema_cache()
    sdp_schema._vendored_schema_document.cache_clear()
    assert sdp_schema.default_sdp_schema_source() == "auto"
    monkeypatch.setattr(requests, "get", explode)

    try:
        assert len(review_metadata(str(raw_package))) > 0
    finally:
        sdp_schema.reset_schema_cache()


def test_review_metadata_refuses_a_path_that_is_not_a_directory(tmp_path):
    with pytest.raises(NotADirectoryError):
        review_metadata(str(tmp_path / "nope"))


def test_a_rejected_slots_reason_is_read_back_onto_the_gap(raw_package):
    """A rejection leaves the field blank, so the gap comes back looking like
    one nobody has ever considered. Reading the reason back is what makes the
    record worth keeping."""
    review = reject_suggestion(
        review_semantics(str(raw_package)),
        "spawner_count",
        "property",
        reason="none of these describe a spawner abundance property",
    )
    apply_sdp_semantics(str(raw_package), review, quiet=True)

    rows = review_metadata(str(raw_package)).rows
    hit = rows[
        (rows["column_name"] == "spawner_count") & (rows["field"] == "property_iri")
    ]
    assert len(hit) == 1
    assert "you rejected every candidate here" in hit["note"].iloc[0]
    assert "spawner abundance property" in hit["note"].iloc[0]


def test_an_empty_review_says_nothing_is_outstanding(filled_package):
    review = review_metadata(str(filled_package))
    assert review.empty
    assert "No outstanding metadata." in str(review)
    # The handoff this scan exists to provide: "nothing outstanding" and a
    # package strict validation refuses cannot both be true. They were, for a
    # measurement IRI still holding its seeded ``REVIEW:`` marker -- non-blank,
    # not one of the three prose placeholder spellings, so the scan passed over
    # it while strict validation refused the package for it.
    validate_salmon_datapackage(str(filled_package), require_iris=True)


# ---------------------------------------------------------------------------
# The printed call is the contract
# ---------------------------------------------------------------------------

_TEMPLATE = re.compile(r'"<[^"]*>"')


def _run_printed_calls(package: Path, namespace: dict) -> int:
    """Execute every call ``review_metadata()`` printed, once.

    The ``<...>`` placeholder is replaced with a real value and nothing else is
    edited, which is exactly the workflow the feature documents.
    """
    review = review_metadata(str(package))
    if review.empty:
        return 0
    lines = review.render_lines(path_expr="pkg")
    calls, current = [], None
    for line in lines:
        stripped = line.strip()
        if re.match(r"^set_sdp_\w+\(", stripped):
            current = [stripped]
        elif current is not None:
            current.append(stripped)
            if stripped == ")":
                calls.append(" ".join(current))
                current = None
    assert calls, "review_metadata() reported gaps but printed no call"
    for call in calls:
        runnable = _TEMPLATE.sub(_replacement_for(call), call)
        assert "<" not in runnable, runnable
        exec(runnable, namespace)  # noqa: S102 - the call is the contract
    return len(calls)


def _replacement_for(call: str):
    def substitute(match):
        # An IRI field needs an IRI to satisfy strict validation; everything
        # else takes ordinary prose.
        before = call[: match.start()]
        field = before.rstrip().rsplit(" ", 1)[-1].rstrip("=")
        if field.endswith("_iri"):
            return '"https://w3id.org/smn/Placeholder"'
        if field == "license":
            return '"CC-BY-4.0"'
        if field == "contact_email":
            return '"data@example.org"'
        return '"filled by the test"'

    return substitute


def test_review_metadata_then_the_printed_calls_reach_strict_validation(
    raw_package,
):
    """create_sdp() -> review_semantics() -> apply_sdp_semantics() ->
    review_metadata() -> set_sdp_*() -> strict validation, with no file opened
    in a spreadsheet at any point."""
    # First decide the semantic slots that have a shortlist.
    review = review_semantics(str(raw_package))
    for column, role in [
        ("spawner_count", "variable"),
        ("spawner_count", "property"),
        ("spawner_count", "entity"),
        ("spawner_count", "unit"),
    ]:
        review = _accept(review, column, role)
    apply_sdp_semantics(str(raw_package), review, quiet=True)

    namespace = {
        "pkg": str(raw_package),
        "set_sdp_dataset": functools.partial(set_sdp_dataset, quiet=True),
        "set_sdp_table": functools.partial(set_sdp_table, quiet=True),
        "set_sdp_column": functools.partial(set_sdp_column, quiet=True),
        "set_sdp_code": functools.partial(set_sdp_code, quiet=True),
    }
    # Each pass fills the gaps it can address; a pass that fills a required
    # column the file lacked can expose nothing new, so this converges.
    for _ in range(6):
        if _run_printed_calls(raw_package, namespace) == 0:
            break
    assert review_metadata(str(raw_package)).empty, str(
        review_metadata(str(raw_package))
    )

    # The contract's other half: when the last row is gone, strict validation
    # passes.
    validate_salmon_datapackage(str(raw_package), require_iris=True)


def _accept(review, column, role):
    from metasalmonpy import accept_suggestion

    try:
        return accept_suggestion(review, column, role, rank=1)
    except ValueError:
        return review


@pytest.fixture
def filled_package(raw_package):
    namespace = {
        "pkg": str(raw_package),
        "set_sdp_dataset": functools.partial(set_sdp_dataset, quiet=True),
        "set_sdp_table": functools.partial(set_sdp_table, quiet=True),
        "set_sdp_column": functools.partial(set_sdp_column, quiet=True),
        "set_sdp_code": functools.partial(set_sdp_code, quiet=True),
    }
    for _ in range(6):
        if _run_printed_calls(raw_package, namespace) == 0:
            break
    return raw_package


def test_the_printed_call_carries_the_table_argument(raw_package):
    text = str(review_metadata(str(raw_package)))
    assert 'set_sdp_table("' in text or "set_sdp_table(path," in text
    assert 'table="spawners"' in text


def test_a_placeholder_containing_braces_prints_literally(raw_package):
    dataset_csv = raw_package / "metadata" / "dataset.csv"
    frame = pd.read_csv(dataset_csv)
    frame.loc[0, "creator"] = "MISSING METADATA: add {creator} or %(team)s"
    frame.to_csv(dataset_csv, index=False)
    text = str(review_metadata(str(raw_package)))
    assert "{creator}" in text
    assert "%(team)s" in text


# ---------------------------------------------------------------------------
# The setters
# ---------------------------------------------------------------------------


def test_a_setter_writes_the_csv_and_the_descriptor_together(raw_package):
    set_sdp_dataset(str(raw_package), creator="Fisheries and Oceans Canada", quiet=True)
    frame = pd.read_csv(raw_package / "metadata" / "dataset.csv")
    assert frame["creator"].iloc[0] == "Fisheries and Oceans Canada"
    descriptor = json.loads((raw_package / "datapackage.json").read_text())
    creators = [
        entry
        for entry in descriptor.get("contributors", [])
        if entry.get("role") == "creator"
    ]
    assert creators and creators[0]["title"] == "Fisheries and Oceans Canada"


def test_pasting_an_unedited_call_is_refused(raw_package):
    # A package whose creator reads "<add creator, team, or originating
    # program>" would pass strict validation while saying nothing, which is
    # worse than the placeholder it replaced, because the marker is gone.
    with pytest.raises(ValueError, match="still holds the placeholder"):
        set_sdp_dataset(
            str(raw_package),
            creator="<add creator, team, or originating program>",
            quiet=True,
        )


def test_the_address_is_resolved_before_the_value_is_checked(raw_package):
    # A printed call naming a row that does not exist must fail on the address
    # rather than being masked by the placeholder guard.
    with pytest.raises(ValueError, match="No column_dictionary.csv row matches"):
        set_sdp_column(
            str(raw_package),
            "no_such_column",
            table="spawners",
            column_label="<something>",
            quiet=True,
        )


def test_a_blank_value_is_refused_as_ambiguous(raw_package):
    with pytest.raises(ValueError, match="must not be blank"):
        set_sdp_dataset(str(raw_package), creator="   ", quiet=True)


def test_pandas_na_clears_a_field_on_purpose(raw_package):
    set_sdp_dataset(str(raw_package), creator="DFO", quiet=True)
    set_sdp_dataset(str(raw_package), creator=pd.NA, quiet=True)
    frame = pd.read_csv(raw_package / "metadata" / "dataset.csv")
    assert pd.isna(frame["creator"].iloc[0])


def test_a_misspelled_field_is_an_error_rather_than_a_silent_no_op(raw_package):
    with pytest.raises(ValueError, match="has no such field"):
        set_sdp_dataset(str(raw_package), licence="CC-BY-4.0", quiet=True)


def test_an_undeclared_extra_field_is_refused(raw_package):
    with pytest.raises(ValueError, match="has no such field"):
        set_sdp_table(str(raw_package), "spawners", not_a_field="x", quiet=True)


def test_a_declared_field_reaches_the_csv_through_extras(raw_package):
    # The escape hatch: every declared field stays reachable without this
    # module re-spelling a schema that is loaded at runtime.
    set_sdp_table(
        str(raw_package), "spawners", observation_unit="one stream-year", quiet=True
    )
    frame = pd.read_csv(raw_package / "metadata" / "tables.csv")
    assert frame["observation_unit"].iloc[0] == "one stream-year"


def test_an_addressing_key_cannot_be_set(raw_package):
    with pytest.raises(ValueError, match="addresses the row and cannot be set"):
        set_sdp_column(
            str(raw_package), "spawner_count", table="spawners", column_name="x"
        )


def test_setting_nothing_is_an_error(raw_package):
    with pytest.raises(ValueError, match="Nothing to set"):
        set_sdp_dataset(str(raw_package))


def test_an_ambiguous_address_names_the_argument_that_fixes_it(tmp_path):
    package = _two_table_package(tmp_path)
    with pytest.raises(ValueError, match="Add table= to say which"):
        set_sdp_column(str(package), "spawner_count", column_label="x", quiet=True)
    set_sdp_column(
        str(package), "spawner_count", table="a", column_label="Counted", quiet=True
    )
    frame = pd.read_csv(package / "metadata" / "column_dictionary.csv")
    hit = frame[(frame["table_id"] == "a") & (frame["column_name"] == "spawner_count")]
    assert hit["column_label"].iloc[0] == "Counted"


def test_a_setter_refuses_a_path_that_is_not_a_package(tmp_path):
    with pytest.raises(NotADirectoryError):
        set_sdp_dataset(str(tmp_path / "nope"), creator="DFO")


def test_a_setter_does_not_touch_the_data_csv_bytes(raw_package):
    data = raw_package / "data" / "spawners.csv"
    before = data.read_bytes()
    set_sdp_dataset(str(raw_package), creator="DFO", quiet=True)
    assert data.read_bytes() == before


def test_set_sdp_code_addresses_one_code_row(raw_package):
    codes = pd.read_csv(raw_package / "metadata" / "codes.csv")
    row = codes.iloc[0]
    set_sdp_code(
        str(raw_package),
        str(row["column_name"]),
        str(row["code_value"]),
        table=str(row["table_id"]),
        code_description="A named watercourse.",
        quiet=True,
    )
    written = pd.read_csv(raw_package / "metadata" / "codes.csv")
    hit = written[
        (written["column_name"] == row["column_name"])
        & (written["code_value"] == row["code_value"])
    ]
    assert hit["code_description"].iloc[0] == "A named watercourse."


# ---------------------------------------------------------------------------
# The patch produces the shape a rebuild would
# ---------------------------------------------------------------------------


def _two_table_package(tmp_path) -> Path:
    resources = {
        "a": pd.DataFrame({"spawner_count": [1, 2]}),
        "b": pd.DataFrame({"spawner_count": [3, 4]}),
    }
    dataset = pd.DataFrame(
        {
            "dataset_id": ["demo"],
            "title": ["Demo"],
            "description": ["Two tables."],
            "creator": ["DFO"],
            "contact_name": ["Unit"],
            "contact_email": ["data@example.org"],
            "license": ["CC-BY-4.0"],
        }
    )
    tables = pd.DataFrame(
        {
            "dataset_id": ["demo", "demo"],
            "table_id": ["a", "b"],
            "file_name": ["data/a.csv", "data/b.csv"],
            "table_label": ["A", "B"],
            "description": ["First.", "Second."],
        }
    )
    dictionary = pd.DataFrame(
        {
            "dataset_id": ["demo", "demo"],
            "table_id": ["a", "b"],
            "column_name": ["spawner_count", "spawner_count"],
            "column_label": ["Spawners", "Spawners"],
            "column_description": ["Counted.", "Counted."],
            "column_role": ["measurement", "measurement"],
            "value_type": ["integer", "integer"],
            "required": [False, False],
        }
    )
    return write_salmon_datapackage(
        resources=resources,
        dataset_meta=dataset,
        table_meta=tables,
        dict_df=dictionary,
        path=tmp_path / "two",
    )


@pytest.mark.parametrize(
    "setter,args,kwargs",
    [
        (set_sdp_dataset, (), {"creator": "Fisheries and Oceans Canada"}),
        (set_sdp_dataset, (), {"contact_org": "Data Unit"}),
        (set_sdp_dataset, (), {"temporal_start": "2001-01-01"}),
        (set_sdp_table, ("a",), {"table_label": "Escapement"}),
        (set_sdp_table, ("a",), {"primary_key": "spawner_count"}),
        (
            set_sdp_column,
            ("spawner_count",),
            {"table": "a", "column_label": "Spawner count"},
        ),
        (
            set_sdp_column,
            ("spawner_count",),
            {"table": "a", "term_iri": SPAWNER_IRI},
        ),
    ],
)
def test_a_setter_patch_produces_the_descriptor_a_rebuild_would(
    tmp_path, setter, args, kwargs
):
    """The three descriptor builders are shared, and this measures it.

    Two producers of one JSON shape is the "one value, one rendering" defect
    class: they look correct separately and disagree in ways nothing checks,
    because ``datapackage_consistent_with_csv_metadata`` is one of the dead
    rules in ``sdp.rules.yaml``.
    """
    patched = _two_table_package(tmp_path / "patch")
    setter(str(patched), *args, quiet=True, **kwargs)

    # A full rebuild from the CSVs the patch just wrote.
    rebuilt_path = write_salmon_datapackage(
        resources={
            name: pd.read_csv(patched / "data" / f"{name}.csv")
            for name in ("a", "b")
        },
        dataset_meta=pd.read_csv(patched / "metadata" / "dataset.csv"),
        table_meta=pd.read_csv(patched / "metadata" / "tables.csv"),
        dict_df=pd.read_csv(patched / "metadata" / "column_dictionary.csv"),
        path=tmp_path / "rebuild",
    )
    assert json.loads((patched / "datapackage.json").read_text()) == json.loads(
        (rebuilt_path / "datapackage.json").read_text()
    )
