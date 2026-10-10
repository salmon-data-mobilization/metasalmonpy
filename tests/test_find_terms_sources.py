"""Which sources ``find_terms()`` searches, and how it reads the names it is given.

Twin of metasalmon's ``tests/testthat/test-find-terms-sources.R``. Two rules this
package already had, and metasalmon now shares:

* hub **B-420** (Q70, ruled by Brett on 2026-09-26: R moves). A call that names
  a role and no sources searches that role's ``sources_for_role()`` list.
* hub **B-421**. A named source list is normalised: each name trimmed as
  ``str.strip()`` trims it and lower-cased, and a repeat dropped after its first
  appearance, in the caller's order.

One rule is new on both sides: **a missing entry names no source.** ``None`` and
NaN used to become the source names ``"none"`` and ``"nan"``, searched as
nothing and reported as searches that found nothing; metasalmon drops its ``NA``
the same way. Those tests were demonstrated failing before the change.
"""

import pandas as pd
import pytest

from metasalmonpy import term_search
from metasalmonpy.llm_review import make_source_policy, policy_sources
from metasalmonpy.term_search import find_terms, sources_for_role

ALL_SOURCES = ("smn", "gcdfo", "ols", "nvs", "zooma", "bioportal", "qudt", "gbif", "worms")


@pytest.fixture
def searched(monkeypatch):
    """Calls ``find_terms()`` with every ``_search_*()`` stubbed. Returns the
    sources the call searched, in order, and the sources its diagnostics report.
    Each stub returns no rows."""

    def run(**kwargs):
        log = []
        for src in ALL_SOURCES:
            def stub(query, role, _src=src):
                log.append(_src)
                return term_search._empty_terms(role)

            monkeypatch.setattr(term_search, f"_search_{src}", stub)
        monkeypatch.setenv("METASALMON_CACHE", "")
        result = find_terms("spawner count", expand_query=False, **kwargs)
        diagnostics = result.attrs.get("diagnostics")
        reported = [] if diagnostics is None or diagnostics.empty else list(diagnostics["source"])
        return log, reported

    return run


@pytest.mark.parametrize(
    "role", ["variable", "property", "entity", "unit", "constraint", "statistical_modifier", "method"]
)
def test_a_role_with_no_sources_searches_that_roles_sources(searched, role):
    log, reported = searched(role=role)
    assert log == sources_for_role(role)
    assert reported == sources_for_role(role)


def test_a_call_with_no_role_searches_the_four_generic_sources(searched):
    assert searched()[0] == ["smn", "gcdfo", "ols", "nvs"]
    assert searched(role=None)[0] == ["smn", "gcdfo", "ols", "nvs"]
    assert searched(role="")[0] == ["smn", "gcdfo", "ols", "nvs"]


def test_named_sources_stay_a_strict_allowlist_whatever_the_role(searched):
    assert searched(role="unit", sources=["ols"])[0] == ["ols"]
    assert searched(role="entity", sources=["gbif", "worms"])[0] == ["gbif", "worms"]
    assert searched(role="unit", sources=[])[0] == []


def test_named_sources_are_trimmed_lower_cased_and_de_duplicated(searched):
    assert searched(sources="SMN")[0] == ["smn"]
    assert searched(sources=["Qudt"])[0] == ["qudt"]
    assert searched(sources=[" smn "])[0] == ["smn"]
    assert searched(sources=["OLS", "ols", " ols"])[1] == ["ols"]
    assert searched(sources=["\u00a0NVS\t"])[0] == ["nvs"]


@pytest.mark.parametrize("missing", [None, float("nan"), pd.NA])
def test_a_missing_source_names_no_source(searched, missing):
    assert searched(sources=["ols", missing, "", "  "])[1] == ["ols"]
    assert searched(sources=[missing])[1] == []


def test_an_unknown_source_name_is_kept_searched_as_nothing_and_reported(searched):
    log, reported = searched(sources=["Unknown", "ols"])
    assert log == ["ols"]
    assert reported == ["unknown", "ols"]


def test_the_normaliser_strips_exactly_what_metasalmon_strips():
    normalise = term_search._normalize_explicit_sources
    assert normalise(["OLS", "smn", "ols", "Smn", "nvs"]) == ("ols", "smn", "nvs")
    assert normalise([None, "", " \t\r\n", float("nan")]) == ()
    # The 29 code points metasalmon's `.ms_python_whitespace_code_points` lists.
    python_whitespace = (
        list(range(0x09, 0x0E)) + list(range(0x1C, 0x21)) + [0x85, 0xA0, 0x1680]
        + list(range(0x2000, 0x200B)) + [0x2028, 0x2029, 0x202F, 0x205F, 0x3000]
    )
    assert len(python_whitespace) == 29
    assert [point for point in range(0x110000) if chr(point).isspace()] == python_whitespace
    for point in python_whitespace:
        assert normalise([f"{chr(point)}SMN{chr(point)}"]) == ("smn",), f"U+{point:04X}"
    assert normalise(["\u180esmn"]) == ("\u180esmn",)
    assert normalise(["s mn"]) == ("s mn",)


def test_an_explicit_policy_is_normalised_before_any_search_function_sees_it():
    policy = make_source_policy(["SMN", " smn", "Gcdfo", None])
    assert policy == {"explicit": True, "sources": ("smn", "gcdfo")}
    assert policy_sources(policy, "unit") == ("smn", "gcdfo")
    assert make_source_policy(None) == {"explicit": False, "sources": None}


def test_the_review_packet_records_the_normalised_list():
    # The packet's source_policy.explicit_allowlist is the list the searches
    # read, as metasalmon records it since its B-421 (pull request 211); it
    # used to be the list as given, which PARITY.md row 65 (g) recorded while
    # metasalmon did the same.
    from metasalmonpy import semantic_review_packet

    payload = semantic_review_packet.source_policy_payload(
        semantic_review_packet._source_policy(["SMN", " smn", "Gcdfo", None])
    )
    assert payload["mode"] == "explicit"
    assert payload["explicit_allowlist"] == ["smn", "gcdfo"]
    assert all(sources == ["smn", "gcdfo"] for sources in payload["effective_sources_by_role"].values())
    payload = semantic_review_packet.source_policy_payload(semantic_review_packet._source_policy(None))
    assert payload["mode"] == "role_defaults"
    assert payload["explicit_allowlist"] == []
    assert payload["effective_sources_by_role"]["unit"] == list(sources_for_role("unit"))
