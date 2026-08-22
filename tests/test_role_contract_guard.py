"""Static guard for the semantic-role contract — SIX of the seven surfaces.

metasalmon's AGENTS.md counts SEVEN surfaces a semantic role has to reach:
the target/role maps, the bundle roles and slot fields, the role-hint
vocabulary, the retrieval filters, the deterministic validators, the ranking
preferences — and ``role_boost`` inside R's ``.ranking_profile_defaults()``.
This guard covers the first six, which are the six that have Python
counterparts.

**The seventh surface is deliberately out of this guard's reach, and saying
so is part of the contract.** Python has no ranking-profile system:
``role_boost`` survives only as an inlined dict literal inside
``term_search._score_and_rank_terms``, with no ``ranking_profile`` parameter,
no defaults function to enumerate, and no override path (hub backlog **#87**,
PARITY.md row 32). R can assert "every ranked role has a boost entry" because
``.ranking_profile_defaults()`` is an enumerable table
(``tests/testthat/test-smn-outranks-gcdfo.R``); there is nothing equivalent
to enumerate here, so a guard claiming to check it would be asserting against
a string literal it cannot prove is the one ranking actually uses. A guard
whose claimed scope exceeds its real scope is worse than a missing guard,
because green reads as "all seven verified" — that is how R shipped
``statistical_modifier`` broken through CI and PR review at sdp-0.3.0.

*Retires / extends when:* #87 lands a Python ``ranking_profile_defaults()``
and this guard grows the seventh check — or metasalmon consolidates its
seventh check into ``test-role-contract-guard.R`` and this file mirrors the
consolidated shape. ``test_the_seventh_surface_is_still_unreachable`` below
is the tripwire that turns either event into a failure here instead of a
silently narrower guard.

Two documented differences from R's guard, both structural rather than
behavioural:

* R checks two salmon-index hint emitters (Turtle and RDF/XML) and a separate
  ``.gcdfo_filter_for_role()``; here the RDF/XML parser serves both indexes
  and the role filtering lives in ``_filter_local_index``'s role-column
  allowlist, so those are the surfaces inspected.
* Several R maps are function-local and only readable via ``deparse``; the
  Python equivalents are read with ``inspect.getsource``, with the same
  stated limitation — a body check proves the role *string* is present, not
  that the branch producing it is reachable.
"""

from __future__ import annotations

import inspect
import re

import pytest

pd = pytest.importorskip("pandas")

from metasalmonpy import llm_review, semantics, term_search, term_search_smn
from metasalmonpy.llm_review import BUNDLE_ROLES, BUNDLE_SLOT_FIELDS
from metasalmonpy.term_search import _load_role_preferences, sources_for_role


def slot_roles() -> list[str]:
    """BUNDLE_SLOT_FIELDS is the authority: the dictionary slots, in order.

    Every other layer is checked against it rather than against a second
    hand-maintained list (mirrors .ms_semantic_bundle_slot_fields()).
    """
    return list(BUNDLE_SLOT_FIELDS)


# Roles the salmon-ontology hint layer can emit. Two documented differences
# from the dictionary slots:
#   * `unit` is absent — units resolve from QUDT/NVS, never from smn/gcdfo.
#   * `method` is present although it is not a dictionary slot — it survives
#     for codes.csv code values.
HINT_ROLES = (
    "variable",
    "property",
    "entity",
    "constraint",
    "method",
    "statistical_modifier",
)

# source_hint values name a retrieval backend; `local` covers both salmon
# ontologies, which sources_for_role() names individually.
HINT_TO_SOURCES = {"local": ("smn", "gcdfo")}


def _system_prompt() -> str:
    return llm_review._bundle_messages({})[0]["content"]


def test_the_bundle_review_prompt_judges_exactly_the_dictionary_slots():
    # The prompt's opening instruction is a role-contract surface: R's
    # sdp-0.3.0 left it naming the removed `method` slot and omitting the one
    # that replaced it, so the prompt contradicted its own later "A method is
    # never a dictionary slot" line.
    prompt = _system_prompt()
    judge = re.findall(r"Judge [^.]*\.", prompt)

    assert len(judge) == 1
    for role in slot_roles():
        assert role in judge[0]
    # A method is never a dictionary slot, so it is never judged as one.
    assert "method" not in judge[0]


def test_the_bundle_prompt_describes_the_roles_it_asks_the_model_to_judge():
    prompt = _system_prompt()
    for role in slot_roles():
        assert role in prompt


def test_every_dictionary_slot_role_has_ontology_ranking_preferences():
    prefs = _load_role_preferences()
    assert len(prefs) > 0
    assert set(slot_roles()) - set(prefs["role"]) == set()


def test_method_keeps_ranking_preferences_for_code_values():
    # The dictionary slot is gone but the role is not: code-value targets
    # still search shared-vocabulary procedures, so removing these rows would
    # be wrong.
    prefs = _load_role_preferences()
    method_prefs = prefs[prefs["role"] == "method"]

    assert len(method_prefs) > 0
    assert "method" in BUNDLE_ROLES
    assert "method" not in slot_roles()


def test_statistical_modifier_prefers_the_reviewed_salmon_vocabulary():
    prefs = _load_role_preferences()
    modifier_prefs = prefs[prefs["role"] == "statistical_modifier"]

    assert len(modifier_prefs) > 0
    smn_pref = modifier_prefs[modifier_prefs["ontology"] == "smn"]
    # Checked before indexing so a missing row fails here rather than erroring.
    assert len(smn_pref) == 1
    assert int(smn_pref["priority"].iloc[0]) == 1
    # I-ADOPT is where the component is defined; STATO is the general fallback.
    assert "iadopt" in set(modifier_prefs["ontology"])
    assert not modifier_prefs["alignment_only"].any()


def test_every_preference_row_names_a_role_the_retrieval_layer_can_serve():
    # Catches both halves of the drift: a preference row for a role
    # sources_for_role() does not know, and a role whose preferred ontology
    # sits behind a backend that role never queries.
    prefs = _load_role_preferences()
    known_roles = set(BUNDLE_ROLES) | {"wikidata"}
    assert set(prefs["role"].unique()) - known_roles == set()

    findings = []
    for _, pref in prefs.iterrows():
        role = pref["role"]
        # `wikidata` is the alignment-only pseudo-role, not a retrievable role.
        if role == "wikidata":
            continue
        hint = pref["source_hint"]
        expected = HINT_TO_SOURCES.get(hint, (hint,))
        if not set(expected) & set(sources_for_role(role)):
            findings.append(f"{role}/{pref['ontology']} needs source {hint}")

    assert not findings, (
        "An ontology-preferences.csv row prefers a source that "
        "sources_for_role() never queries for that role, so the preference "
        "can never apply. See metasalmon AGENTS.md on the role contract.\n"
        + "\n".join(findings)
    )


def test_the_role_hint_layer_emits_every_role_it_can_flag():
    # This is the layer metasalmon's AGENTS.md says gets forgotten. A flag
    # with no emitter never reaches `role_hints`, and the role-type validator
    # then downgrades every correct accept for that role.
    flags = term_search_smn._smn_role_flags(
        label="Mean",
        definition="The arithmetic mean of the observed values.",
        resource_kind="Concept",
        module_name="07-controlled-vocabularies",
        in_scheme="https://w3id.org/smn/StatisticalModifierScheme",
        parent_iris="",
        type_iris="http://w3id.org/iadopt/ont/StatisticalModifier",
        iri="https://w3id.org/smn/MeanStatisticalModifier",
    )
    assert set(flags) == {f"is_{role}" for role in HINT_ROLES}

    # The hint string emitter must translate every flag it can receive; a
    # flag with no (flag, hint) pair is silently dropped from role_hints.
    hints_source = inspect.getsource(term_search_smn._smn_role_hints)
    # The RDF/XML parser serves both salmon indexes (smn fallback and gcdfo),
    # so its flag dict is the second emitter body to check.
    rdf_source = inspect.getsource(term_search._parse_salmon_rdfxml)
    for role in HINT_ROLES:
        assert f'"is_{role}"' in hints_source
        assert f'"is_{role}"' in rdf_source


def test_the_local_index_role_filter_has_a_column_for_every_role():
    # Without a role column in the allowlist the role falls through to "keep
    # everything", so a query like "mean" matches any variable whose
    # definition merely contains the word and only ranking keeps the real
    # modifier on top. (_filter_local_index serves both smn and gcdfo — the
    # Python counterpart of R's .gcdfo_filter_for_role.)
    filter_source = inspect.getsource(term_search._filter_local_index)
    for role in set(slot_roles()) - {"unit"} | set(HINT_ROLES):
        assert f'"{role}"' in filter_source, (
            f"_filter_local_index has no role column case for {role!r}"
        )


def test_sources_for_role_serves_every_bundle_role():
    # A role that falls through to the generic default has no retrieval
    # identity of its own — the shape of failure that let R's
    # statistical_modifier reach ranking with no source preferences.
    generic_default = sources_for_role("")
    for role in BUNDLE_ROLES:
        sources = sources_for_role(role)
        assert sources, f"sources_for_role({role!r}) returned nothing"
        assert sources != generic_default, (
            f"sources_for_role({role!r}) fell through to the default"
        )


def test_the_deterministic_validators_cover_the_evidence_gated_roles():
    # The SEM_* family: method (surviving, code-level), statistical_modifier
    # (its dictionary replacement), constraint, and the role-type veto.
    validators_source = inspect.getsource(llm_review._apply_validators)
    for code in (
        "SEM_METHOD_EVIDENCE_REQUIRED",
        "SEM_MODIFIER_EVIDENCE_REQUIRED",
        "SEM_CONSTRAINT_EVIDENCE_REQUIRED",
        "SEM_ROLE_TYPE_MISMATCH",
    ):
        assert code in validators_source


def test_the_role_field_maps_agree_with_the_dictionary_slot_fields():
    # apply_semantic_suggestions() maps role -> field (function-local, so the
    # body is the only place to read it); target discovery maps field -> role
    # through the module-level ROLE_MAP. Both must know every slot.
    apply_source = inspect.getsource(semantics.apply_semantic_suggestions)
    for role, field in BUNDLE_SLOT_FIELDS.items():
        assert f'"{role}": "{field}"' in apply_source
        assert semantics.ROLE_MAP.get(field) == role
    assert set(semantics.ROLE_MAP) == set(BUNDLE_SLOT_FIELDS.values())


def test_the_seventh_surface_is_still_unreachable():
    # The tripwire behind this guard's stated scope. The seventh role-contract
    # surface — role_boost — lives in R's .ranking_profile_defaults(); Python
    # has no profile system (hub backlog #87, PARITY.md row 32), so this guard
    # covers six surfaces and SAYS so. The moment a profile system appears,
    # this test fails and the fix is to EXTEND this guard with the seventh
    # check (every ranked role has a role_boost entry), not to delete the
    # test.
    assert not hasattr(term_search, "ranking_profile_defaults"), (
        "term_search has grown a ranking_profile_defaults(): backlog #87 has "
        "landed. Extend this guard to the seventh surface (every ranked role "
        "has a role_boost entry, as tests/testthat/test-smn-outranks-gcdfo.R "
        "asserts in R) and update this file's docstring before removing this "
        "tripwire."
    )
    rank_params = inspect.signature(
        term_search._score_and_rank_terms
    ).parameters
    assert "ranking_profile" not in rank_params, (
        "_score_and_rank_terms has grown a ranking_profile parameter: extend "
        "this guard to the seventh surface (see docstring) instead of "
        "leaving it silently six-of-seven."
    )
