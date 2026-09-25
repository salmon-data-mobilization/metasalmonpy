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

import contextlib
import copy
import functools
import json
import re
import warnings
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
from metasalmonpy import sdp_schema
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
    that narrowness; ``_is_unresolved_iri()`` is the IRI-field predicate and adds
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


def test_which_files_a_review_marker_actually_blocks(filled_package):
    """``_REVIEW_IRI_FILES`` is a measurement, so it is measured here.

    Three gates sweep the ``REVIEW:`` marker and they do not sweep the same
    files. That is invisible from any one call site, it is what decides which
    files this scan may report without claiming a block that does not exist, and
    it is the kind of fact that drifts silently -- so it is asserted rather than
    described. Each case marks exactly one field on an otherwise clean package
    and asks both gates.

    ``codes.csv`` is the asymmetric one: the EDH XML gate refuses a marker there
    and ``validate_salmon_datapackage(require_iris=True)`` does not. The scan
    reports it anyway, for the reason recorded on ``_REVIEW_IRI_FILES``. If this
    test starts failing on the ``codes.csv`` row because the validator now
    refuses it, that is the validator being fixed and the right change here is to
    delete the exception, not the assertion.

    Retires when the three gates sweep the same files, which is also what retires
    ``_REVIEW_IRI_FILES``.
    """
    from metasalmonpy.package_io import _collect_review_issues, read_salmon_datapackage

    mark = "REVIEW:https://w3id.org/smn/SomethingUndecided"
    # Every expectation is written out rather than read from
    # ``_REVIEW_IRI_FILES``: a test that compares the scan against the constant
    # the scan is built from passes for any value of that constant, which is no
    # test at all. Columns: file, field, does
    # ``validate_salmon_datapackage(require_iris=True)`` refuse it, does the EDH
    # XML gate refuse it, does ``review_metadata()`` report it.
    cases = [
        ("tables.csv", "observation_unit_iri", True, True, True),
        ("column_dictionary.csv", "property_iri", True, True, True),
        ("codes.csv", "term_iri", False, True, True),
        ("dataset.csv", "protocol_iri", False, False, False),
    ]

    for file_name, field, validator_refuses, edh_refuses_it, scan_reports in cases:
        csv = filled_package / "metadata" / file_name
        original = csv.read_bytes()
        frame = pd.read_csv(csv)
        if field not in frame.columns:
            frame[field] = ""
        frame[field] = frame[field].astype(object)
        frame.loc[0, field] = mark
        frame.to_csv(csv, index=False)
        try:
            with _no_warnings():
                refused = False
                try:
                    validate_salmon_datapackage(
                        str(filled_package), require_iris=True
                    )
                except ValueError:
                    refused = True
                assert refused is validator_refuses, (
                    f"{file_name}${field}: validate_salmon_datapackage refused="
                    f"{refused}, expected {validator_refuses}"
                )
                # The EDH XML gate is the other sweep, and it is wider.
                edh_refuses = bool(
                    _collect_review_issues(
                        read_salmon_datapackage(str(filled_package))
                    )
                )
                assert edh_refuses is edh_refuses_it, (
                    f"{file_name}${field}: EDH gate refused={edh_refuses}, "
                    f"expected {edh_refuses_it}"
                )
                rows = review_metadata(str(filled_package)).rows
                reported = bool(
                    len(rows[(rows["file"] == file_name) & (rows["field"] == field)])
                )
                assert reported is scan_reports, (
                    f"{file_name}${field}: review_metadata reported={reported}, "
                    f"expected {scan_reports}"
                )
        finally:
            csv.write_bytes(original)


@contextlib.contextmanager
def _no_warnings():
    """These paths warn by design; the assertions are about what they refuse."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        yield


def test_an_undeclared_iri_column_is_still_missed(filled_package):
    """A KNOWN GAP, pinned so it is visible rather than latent.

    ``_collect_review_iri_issues()`` sweeps every column of ``tables.csv`` whose
    name ends in ``_iri``, declared by the schema or not. This scan iterates the
    SCHEMA-DECLARED fields only, because every row it reports must print a
    runnable ``set_sdp_*()`` call and ``_set_sdp_metadata()`` refuses a field the
    schema does not declare. So a user who hand-adds an ``*_iri`` column to
    ``tables.csv`` and leaves a marker in it reaches the exact state the
    ``REVIEW:``-marker fix was written to remove: ``review_metadata()`` says "No
    outstanding metadata." and ``validate_salmon_datapackage(require_iris=True)``
    refuses the package.

    Not fixed here, because closing it means either printing a call that cannot
    be run or letting a setter write an undeclared field, and both are decisions
    about the printed-call contract rather than about this predicate. Narrower
    than the reported finding -- no metasalmonpy path writes such a column, so it
    is reachable only by hand-editing -- and identical in metasalmon, so it needs
    a queue item covering both.

    Retires when: the scan and ``_collect_review_iri_issues()`` agree about
    undeclared ``*_iri`` columns. Delete this test in the change that makes them
    agree; it asserts the defect, so it fails when the defect is fixed.
    """
    csv = filled_package / "metadata" / "tables.csv"
    frame = pd.read_csv(csv)
    frame["custom_thing_iri"] = ""
    frame["custom_thing_iri"] = frame["custom_thing_iri"].astype(object)
    frame.loc[0, "custom_thing_iri"] = "REVIEW:https://w3id.org/smn/HandAdded"
    frame.to_csv(csv, index=False)

    assert review_metadata(str(filled_package)).empty
    with pytest.raises(ValueError, match="unresolved review issue"):
        with _no_warnings():
            validate_salmon_datapackage(str(filled_package), require_iris=True)


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


def _use_shipped_schema_defaults(monkeypatch) -> None:
    """The schema settings a user gets, on a cold cache, for the calling test.

    Both settings and both of their environment variables are cleared, as
    metasalmon's ``local_shipped_schema_defaults()`` clears all three of its
    options. The suite pins the source to ``"vendored"`` in
    ``tests/conftest.py``, and a base URL left in the environment selects a
    schema just as a source setting does.
    """
    monkeypatch.delenv("METASALMONPY_SDP_SCHEMA_SOURCE", raising=False)
    monkeypatch.delenv("METASALMONPY_SDP_SCHEMA_BASE_URL", raising=False)
    # Each of these setters also empties the loader's cache.
    sdp_schema.set_sdp_schema_source(None)
    sdp_schema.set_sdp_schema_base_url(None)
    sdp_schema._vendored_schema_document.cache_clear()
    assert sdp_schema.default_sdp_schema_source() == "auto"
    assert (
        sdp_schema.default_sdp_schema_base_url()
        == sdp_schema.DEFAULT_SDP_SCHEMA_BASE_URL
    )


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

    _use_shipped_schema_defaults(monkeypatch)
    monkeypatch.setattr(requests, "get", explode)

    try:
        assert len(review_metadata(str(raw_package))) > 0
    finally:
        sdp_schema.reset_schema_cache()


def test_the_offline_path_opens_no_socket_at_all(raw_package, monkeypatch):
    """The same contract one layer lower, and over the setters as well.

    The test above injects ``requests.get``, which is the call the fetch makes
    TODAY. A guard shaped like the current implementation stops guarding the
    contract the moment the implementation moves: a switch to
    ``requests.Session``, ``urllib``, ``httpx`` or a subprocess would leave that
    sentinel green while the network was reached on every call. So this one
    blocks the socket API itself, which every one of those has to go through.

    It also covers the four setters and the console renderer, because under
    the shipped schema settings ``_schema_source()`` claims the bundled read
    for all of them: a call ``review_metadata()`` prints must be one
    ``_set_sdp_metadata()`` accepts, and a setter that read the remote schema
    would break that on a machine with no network rather than on this one.

    Retires when: never, while :func:`review_metadata` documents that it does not
    contact a network. An offline promise with no test that fails when a socket
    opens is a comment, not a contract.
    """
    import socket

    from metasalmonpy import sdp_schema

    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise _NetworkReached("the offline path opened a socket")

    assert raw_package.is_dir()

    _use_shipped_schema_defaults(monkeypatch)

    monkeypatch.setattr(socket.socket, "connect", explode)
    monkeypatch.setattr(socket.socket, "connect_ex", explode)
    monkeypatch.setattr(socket, "create_connection", explode)

    try:
        review = review_metadata(str(raw_package))
        assert len(review) > 0
        # Rendering is part of the same call for a user, and it reads the
        # schema again for each hint.
        assert review.render_lines(path_expr="pkg")
        set_sdp_dataset(str(raw_package), creator="Offline Guard", quiet=True)
        set_sdp_table(
            str(raw_package), table="spawners", table_label="Spawners", quiet=True
        )
        set_sdp_column(
            str(raw_package),
            table="spawners",
            column="spawner_count",
            column_description="counted spawners",
            quiet=True,
        )
    finally:
        sdp_schema.reset_schema_cache()


# ---------------------------------------------------------------------------
# A schema the settings select (hub B-215, the port of metasalmon's B-175)
# ---------------------------------------------------------------------------
#
# The offline promise above holds under the shipped schema settings. Under any
# other setting, the scan, the setters and the validator's blank-required
# collector read the schema the settings select, as the writers do, because
# that is the field contract the package was written to. These are the twins of
# metasalmon's "a schema the options select is the one the scan and the setters
# read", in tests/testthat/test-review-metadata-offline.R.

_SELECTED_BASE_URL = "https://example.invalid/smn-data-pkg/sdp-9.9.9"


#: Every way a user selects a schema other than the shipped one. Each of them
#: reaches the writers, through ``load_sdp_schema()``.
_SCHEMA_SELECTIONS = {
    "set_sdp_schema_base_url": lambda mp: sdp_schema.set_sdp_schema_base_url(
        _SELECTED_BASE_URL
    ),
    "METASALMONPY_SDP_SCHEMA_BASE_URL": lambda mp: mp.setenv(
        "METASALMONPY_SDP_SCHEMA_BASE_URL", _SELECTED_BASE_URL
    ),
    "set_sdp_schema_source": lambda mp: sdp_schema.set_sdp_schema_source("remote"),
    "METASALMONPY_SDP_SCHEMA_SOURCE": lambda mp: mp.setenv(
        "METASALMONPY_SDP_SCHEMA_SOURCE", "remote"
    ),
}


def _bundle_requiring_funding_source() -> dict:
    """The bundled schema plus one required ``dataset.csv`` field, validated.

    It stands in for a published schema that differs from the bundled copy in
    the one way that matters here: a requirement the bundle does not declare.
    """
    bundled = sdp_schema._load_vendored_sdp_schema()
    schemas = copy.deepcopy(bundled["metadata_schemas"])
    schemas["dataset"]["fields"].append(
        {
            "name": "funding_source",
            "type": "string",
            "description": "Who funded the work.",
            "constraints": {"required": True},
        }
    )
    return sdp_schema._validate_sdp_schema(
        {
            "metadata_schemas": schemas,
            "profile": bundled["profile"],
            "rules": bundled["rules"],
        }
    )


def _count_schema_fetches(monkeypatch, bundle: dict) -> list:
    """Serve ``bundle`` from the loader's remote fetch, recording each call.

    The socket API is blocked too, as in the offline test above, so a read that
    reached the network by any other route fails the test rather than passing
    unseen. The list returned holds the base URL of every fetch.
    """
    import socket

    fetched: list = []

    def fetch(base_url, timeout=2.0):
        fetched.append(base_url)
        return bundle

    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise _NetworkReached("a schema read opened a socket")

    monkeypatch.setattr(sdp_schema, "_fetch_remote_sdp_schema", fetch)
    monkeypatch.setattr(socket.socket, "connect", explode)
    monkeypatch.setattr(socket.socket, "connect_ex", explode)
    monkeypatch.setattr(socket, "create_connection", explode)
    return fetched


def _what_reads_funding_source(package: Path) -> dict:
    """Whether the scan reports ``dataset.csv``'s ``funding_source``, and
    whether the validator's blank-required collector names it.

    Both are read from the named columns of the frames returned, never by
    iterating a frame: iteration yields the column labels, and a probe built
    that way reported "not named" whatever the collector returned.
    """
    from metasalmonpy.package_io import (
        _collect_blank_required_metadata_fields,
        read_salmon_datapackage,
    )

    rows = review_metadata(str(package)).rows
    found = _collect_blank_required_metadata_fields(
        read_salmon_datapackage(str(package))
    )
    return {
        "the scan reports it": bool(
            (
                (rows["file"] == "dataset.csv")
                & (rows["field"] == "funding_source")
            ).any()
        ),
        "the collector names it": bool(
            (
                (found["file"] == "dataset.csv")
                & (found["field"] == "funding_source")
            ).any()
        ),
    }


def _set_funding_source(package: Path):
    """``True`` when ``set_sdp_dataset()`` accepts the field, else its refusal."""
    try:
        set_sdp_dataset(str(package), funding_source="A funder", quiet=True)
    except ValueError as error:
        return str(error)
    return True


@pytest.mark.parametrize("selection", sorted(_SCHEMA_SELECTIONS))
def test_a_schema_the_settings_select_is_the_one_the_scan_and_the_setters_read(
    raw_package, monkeypatch, selection
):
    """The twin of metasalmon's test of the same name (hub B-215 and B-175).

    ``load_sdp_schema()`` gives the writers the schema the settings select.
    Until B-215 the scan, the setters and the collector read the bundled copy
    under every setting. So with a selected schema that declares one more
    required ``dataset.csv`` field, the writers knew the field, the scan did not
    report it, ``set_sdp_dataset()`` refused it as undeclared, and the collector
    did not name it. Every one of the four settings was silently ignored.

    Here the writers have already loaded the selected schema, so reading it
    costs no fetch and opens no socket.
    """
    assert raw_package.is_dir()
    _use_shipped_schema_defaults(monkeypatch)
    _SCHEMA_SELECTIONS[selection](monkeypatch)
    try:
        selected = _bundle_requiring_funding_source()
        # The writers' own read, served the selected bundle: the state that a
        # package written under these settings leaves in the session cache.
        sdp_schema.load_sdp_schema(
            quiet=True, fetch_fn=lambda base_url, timeout: selected
        )
        assert "funding_source" in sdp_schema.sdp_schema_field_names("dataset")
        fetched = _count_schema_fetches(monkeypatch, selected)

        before = _what_reads_funding_source(raw_package)
        before["set_sdp_dataset() accepts it"] = _set_funding_source(raw_package)
        assert before == dict.fromkeys(before, True)
        # Once filled it blocks nothing: the scan and the validator agree.
        assert _what_reads_funding_source(raw_package) == {
            "the scan reports it": False,
            "the collector names it": False,
        }
        assert fetched == []
    finally:
        sdp_schema.set_sdp_schema_base_url(None)


@pytest.mark.parametrize("selection", sorted(_SCHEMA_SELECTIONS))
def test_a_selected_schema_nothing_has_loaded_is_loaded_once(
    raw_package, monkeypatch, selection
):
    """The other half of the same contract: no writer has loaded it yet.

    The scan asks the loader for the selected schema, as a writer would, rather
    than silently reading the bundled copy instead. Selecting a schema is the
    opt-in to reading it, and the shipped settings are the ones promised to
    stay offline. It is fetched once, from where the settings point, and every
    later read in the process, the setter's and the collector's included, is
    served from the cache.
    """
    assert raw_package.is_dir()
    _use_shipped_schema_defaults(monkeypatch)
    _SCHEMA_SELECTIONS[selection](monkeypatch)
    try:
        fetched = _count_schema_fetches(
            monkeypatch, _bundle_requiring_funding_source()
        )
        assert _what_reads_funding_source(raw_package) == {
            "the scan reports it": True,
            "the collector names it": True,
        }
        assert _set_funding_source(raw_package) is True
        assert fetched == [sdp_schema.default_sdp_schema_base_url()]
    finally:
        sdp_schema.set_sdp_schema_base_url(None)


_PINNED_BASE_URL = sdp_schema.DEFAULT_SDP_SCHEMA_BASE_URL
_SETTINGS_CASES = [
    ({}, True),
    ({"set_sdp_schema_source": "auto"}, True),
    ({"METASALMONPY_SDP_SCHEMA_SOURCE": "auto"}, True),
    ({"METASALMONPY_SDP_SCHEMA_SOURCE": ""}, True),
    ({"set_sdp_schema_base_url": _PINNED_BASE_URL}, True),
    ({"set_sdp_schema_base_url": ""}, True),
    ({"METASALMONPY_SDP_SCHEMA_BASE_URL": _PINNED_BASE_URL}, True),
    ({"METASALMONPY_SDP_SCHEMA_BASE_URL": ""}, True),
    ({"set_sdp_schema_source": "vendored"}, False),
    ({"set_sdp_schema_source": "remote"}, False),
    ({"METASALMONPY_SDP_SCHEMA_SOURCE": "vendored"}, False),
    ({"METASALMONPY_SDP_SCHEMA_SOURCE": "remote"}, False),
    ({"set_sdp_schema_base_url": "https://example.invalid/x"}, False),
    ({"METASALMONPY_SDP_SCHEMA_BASE_URL": "https://example.invalid/x"}, False),
]


@pytest.mark.parametrize(
    "settings, expected",
    _SETTINGS_CASES,
    ids=[
        ",".join(f"{name}={value!r}" for name, value in settings.items())
        or "nothing set"
        for settings, _ in _SETTINGS_CASES
    ],
)
def test_the_shipped_settings_are_told_apart_by_what_they_resolve_to(
    monkeypatch, settings, expected
):
    """The twin of metasalmon's "the default options are told apart by what
    they resolve to".

    A setting that names the default value is the default, and an empty value
    falls back to it, exactly as the loader resolves them. Only a setting that
    selects another schema moves the scan, the setters and the collector off the
    bundled read.
    """
    _use_shipped_schema_defaults(monkeypatch)
    try:
        for name, value in settings.items():
            if name.startswith("METASALMONPY_"):
                monkeypatch.setenv(name, value)
            else:
                getattr(sdp_schema, name)(value)
        assert sdp_schema._sdp_schema_options_are_default() is expected
    finally:
        sdp_schema.set_sdp_schema_base_url(None)


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
