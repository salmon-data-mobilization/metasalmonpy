"""KNB/DataONE environments -- the closed registry of deposit targets.

Mirrors metasalmon's ``R/knb-environments.R`` (roadmap S3, metasalmon 0.4.0).

Before this module the member node, coordinating node, resolver, and Solr
endpoints were module-level constants in :mod:`metasalmonpy.knb_publication`
pinned to production, so :func:`publish_sdp_to_knb` had no state that could
vary and no way to rehearse a deposit.

Two rules give this module its shape, and both are structural rather than
advisory:

1. **An environment is switched whole or not at all.** Every derived URL is
   built here from that environment's own ``mn_base_url`` / ``cn_base_url``,
   so there is no assignment anywhere in the package that could pair a test
   node identifier with a production Solr endpoint. :func:`plan_config` then
   re-derives the whole record from the plan's fingerprinted ``node_id`` on
   every read, so a hand-edited manifest cannot smuggle a mismatched pair past
   the planner either.
2. **The registry is closed.** No custom endpoints, no partial matching, no
   fallback between environments. :func:`knb_publication.set_knb_adapter` is
   deliberately NOT covered by that rule -- it is the suite's adapter
   injection point, and the closed-registry rule governs endpoints and tokens,
   not the adapter seam.

Sources for the values below, all read from the DataONE node documents
themselves on 2026-08-22 (read-only GETs, no credentials):

* ``urn:node:mnTestKNB`` ("KNB Test Node") answers 200 at
  ``https://dev.nceas.ucsb.edu/knb/d1/mn``, and is registered ``state="up"``
  in the DataONE staging coordinating node.
* ``urn:node:cnStage`` ("cn-stage") answers 200 at
  ``https://cn-stage.test.dataone.org/cn``.
* ``urn:node:KNB`` answers 200 at ``https://knb.ecoinformatics.org/knb/d1/mn``.

This module deliberately imports nothing from the rest of the package at
module scope: both :mod:`metasalmonpy.eml` and
:mod:`metasalmonpy.knb_publication` import *it*, and a module-level import in
the other direction would be a cycle.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

# --- the registry ---------------------------------------------------------------------

#: Every environment record carries exactly these fields. The guard test reads
#: this tuple as the authority, so a field added to one environment and
#: forgotten in the other fails before it can reach a deposit.
_ENVIRONMENT_FIELDS: Tuple[str, ...] = (
    "knb_environment",
    "dataone_network",
    "node_id",
    "mn_base_url",
    "mn_endpoint",
    "object_endpoint",
    "cn_base_url",
    "cn_endpoint",
    "resolver",
    "solr_endpoint",
    "token_option",
    "token_setter",
    "pid_scope",
    "default_eml_relpath",
    "default_manifest_relpath",
    "max_replicas",
    "durable",
)


def environment_fields() -> Tuple[str, ...]:
    """Mirror ``.ms_knb_environment_fields``."""
    return _ENVIRONMENT_FIELDS


def _environment_record(
    knb_environment: str,
    dataone_network: str,
    node_id: str,
    mn_base_url: str,
    cn_base_url: str,
    token_option: str,
    token_setter: str,
    pid_scope: str,
    default_eml_relpath: str,
    default_manifest_relpath: str,
    max_replicas: int,
    durable: bool,
) -> Dict[str, object]:
    """Mirror ``.ms_knb_environment_record``.

    The four network-identity values -- node id, DataONE network, member-node
    base URL, coordinating-node base URL -- are the only URL inputs. The member
    object endpoint derives from the first, the resolver and Solr endpoint from
    the second. That is what makes "switch the environment together" a property
    of the code rather than a rule someone has to remember.
    """
    member_node = mn_base_url.rstrip("/")
    coordinating_node = cn_base_url.rstrip("/")
    return {
        "knb_environment": knb_environment,
        "dataone_network": dataone_network,
        "node_id": node_id,
        "mn_base_url": member_node,
        "mn_endpoint": member_node + "/v2",
        "object_endpoint": member_node + "/v2/object/",
        "cn_base_url": coordinating_node,
        # R resolves the Coordinating Node through ``dataone::D1Client``; the
        # raw REST adapter needs the CN v2 base as an explicit value. It is
        # derived here rather than declared, so it cannot be switched
        # independently of the resolver and Solr endpoints (PARITY.md row 16).
        "cn_endpoint": coordinating_node + "/v2",
        "resolver": coordinating_node + "/v2/resolve/",
        "solr_endpoint": coordinating_node + "/v2/query/solr/",
        "token_option": token_option,
        "token_setter": token_setter,
        "pid_scope": pid_scope,
        "default_eml_relpath": default_eml_relpath,
        "default_manifest_relpath": default_manifest_relpath,
        "max_replicas": int(max_replicas),
        "durable": bool(durable),
    }


def environment_registry() -> Dict[str, Dict[str, object]]:
    """Mirror ``.ms_knb_environment_registry``: the two supported targets."""
    return {
        "test": _environment_record(
            knb_environment="test",
            dataone_network="STAGING",
            node_id="urn:node:mnTestKNB",
            mn_base_url="https://dev.nceas.ucsb.edu/knb/d1/mn",
            cn_base_url="https://cn-stage.test.dataone.org/cn",
            # A different credential from production, deliberately. The
            # structural ``*_token`` redaction rule adopted at S10 chunk F
            # already covers this name, so it needs no further patch.
            token_option="dataone_test_token",
            token_setter="set_dataone_test_token",
            # Folded into every identifier minted for this environment so a
            # test PID can never be mistaken for -- or collide with -- a
            # production PID. The SDP archive makes this concrete: its bytes
            # are environment-independent, so without a scope the same package
            # would mint the same archive PID in both environments.
            pid_scope="knb-test",
            # A test EML document contains different resolver and object URLs,
            # so it has different bytes. Writing it to ``metadata/eml.xml``
            # would replace the reviewed production record -- and those bytes
            # are hashed into ``plan_sha256``, the deterministic archive, and
            # the reproducibility manifest, so the damage would propagate past
            # the file.
            default_eml_relpath="publication/test/eml.xml",
            default_manifest_relpath="publication/test/knb-manifest.json",
            # A rehearsal never asks peer nodes to preserve copies.
            max_replicas=0,
            durable=False,
        ),
        "production": _environment_record(
            knb_environment="production",
            dataone_network="PROD",
            node_id="urn:node:KNB",
            mn_base_url="https://knb.ecoinformatics.org/knb/d1/mn",
            cn_base_url="https://cn.dataone.org/cn",
            token_option="dataone_token",
            token_setter="set_dataone_token",
            # Empty: ``pid_preimage()`` drops an empty scope, so every
            # production identifier minted before this module existed is
            # unchanged.
            pid_scope="",
            default_eml_relpath="metadata/eml.xml",
            default_manifest_relpath="publication/knb-manifest.json",
            max_replicas=3,
            durable=True,
        ),
    }


def validate_environment_config(
    config: Dict[str, object], knb_environment: str
) -> Dict[str, object]:
    """Mirror ``.ms_knb_validate_environment_config``."""
    required = set(_ENVIRONMENT_FIELDS)
    present = set(config)
    missing = sorted(required - present)
    unexpected = sorted(present - required)
    if missing or unexpected:
        details = []
        if missing:
            details.append("Missing field(s): " + ", ".join(missing) + ".")
        if unexpected:
            details.append("Unexpected field(s): " + ", ".join(unexpected) + ".")
        raise ValueError(
            f"KNB environment {knb_environment!r} is not a complete registry "
            "record. " + " ".join(details)
        )

    # ``pid_scope`` is legitimately empty for production; every other string
    # field must be present, or an environment could be switched partway.
    string_fields = [
        field
        for field in _ENVIRONMENT_FIELDS
        if field not in ("max_replicas", "durable", "pid_scope")
    ]
    for field in string_fields:
        value = config[field]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"KNB environment {knb_environment!r} field {field!r} must be "
                "one non-empty string."
            )
    replicas = config["max_replicas"]
    if not isinstance(replicas, int) or isinstance(replicas, bool) or replicas < 0:
        raise ValueError(
            f"KNB environment {knb_environment!r} field 'max_replicas' must be "
            "one non-negative count."
        )
    if not isinstance(config["durable"], bool):
        raise ValueError(
            f"KNB environment {knb_environment!r} field 'durable' must be one "
            "boolean value."
        )
    return config


def environment_ids() -> List[str]:
    """Mirror ``.ms_knb_environment_ids``: the closed vocabulary, sorted."""
    # Plain ``sorted`` on ASCII identifiers is byte order, which is what R's
    # ``method = "radix"`` pins: the reported order of a closed vocabulary
    # never depends on the ambient locale.
    return sorted(environment_registry())


def knb_config(knb_environment: object) -> Dict[str, object]:
    """Mirror ``.ms_knb_config``: exact selection from the closed registry."""
    supported = environment_ids()
    if not isinstance(knb_environment, str):
        raise ValueError(
            "knb_environment must be exactly one of "
            + ", ".join(repr(value) for value in supported)
            + ". There is no partial matching, no custom endpoint, and no "
            "fallback between environments."
        )
    registry = environment_registry()
    # Exact match only: partial matching an environment name is how a
    # rehearsal becomes a production deposit.
    if knb_environment not in registry:
        raise ValueError(
            f"Unknown KNB environment {knb_environment!r}. Supported "
            "environment(s): " + ", ".join(repr(value) for value in supported) + "."
        )
    return validate_environment_config(registry[knb_environment], knb_environment)


def config_for_node(node_id: object) -> Dict[str, object]:
    """Mirror ``.ms_knb_config_for_node``: reverse lookup from the node id.

    This is the authoritative direction: ``node_id`` is a fingerprinted field
    of every plan and manifest, so resolving the environment from it means the
    Solr endpoint, resolver, and member-node URL a plan is read with always
    belong to the node that plan was actually built for.
    """
    registry = environment_registry()
    node = str(node_id)
    matches = [name for name, config in registry.items() if config["node_id"] == node]
    if len(matches) != 1:
        registered = [registry[name]["node_id"] for name in environment_ids()]
        raise ValueError(
            f"{node!r} is not a registered KNB member node. Registered "
            "node(s): " + ", ".join(repr(value) for value in registered) + "."
        )
    return knb_config(matches[0])


def plan_config(plan: Dict[str, object]) -> Dict[str, object]:
    """Mirror ``.ms_knb_plan_config``.

    Resolve the environment a plan or manifest belongs to, and refuse any
    record whose environment-derived values disagree with each other. A plan
    claiming the production network under a test node identifier is exactly
    the piecemeal switch this module exists to make impossible.
    """
    config = config_for_node(plan.get("node_id"))
    network = plan.get("environment")
    if not isinstance(network, str) or network != config["dataone_network"]:
        raise ValueError(
            "The publication plan mixes KNB environments. Node "
            f"{str(plan.get('node_id'))!r} belongs to the "
            f"{config['dataone_network']!r} DataONE network, but the plan "
            f"records {network!r}."
        )
    declared = plan.get("knb_environment")
    if declared is not None and str(declared) != config["knb_environment"]:
        raise ValueError(
            "The publication plan mixes KNB environments. Node "
            f"{str(plan.get('node_id'))!r} is the "
            f"{config['knb_environment']!r} environment, but the plan records "
            f"{str(declared)!r}."
        )
    return config


def resolve_environment(
    knb_environment: Optional[str], dry_run: bool
) -> Dict[str, object]:
    """Mirror ``.ms_knb_resolve_environment``.

    The default policy, which is the shape of Brett's 2026-08-22 ruling:
    develop against the test node first, then post to production once the
    package looks good there. So a dry run -- the credential-free,
    network-free rehearsal -- defaults to test, and anything live has to name
    its target. An unstated environment on a live call is an error, not a
    default.
    """
    if knb_environment is None:
        if dry_run is True:
            return knb_config("test")
        raise ValueError(
            "Live KNB publication requires an explicit knb_environment. Pass "
            'knb_environment="test" to deposit to the KNB Test Node, or '
            'knb_environment="production" to deposit to KNB. Only a dry run '
            'defaults to "test"; a live target is never inferred.'
        )
    return knb_config(knb_environment)


def pid_preimage(pid_scope: Optional[str], *parts: object) -> str:
    """Mirror ``.ms_knb_pid_preimage``.

    Fold the environment's scope into an identifier preimage. Production's
    scope is empty and is dropped, so this function is a no-op there by
    construction -- which is the property that keeps every production PID
    minted before this module byte-identical.
    """
    flat: List[str] = []
    for part in parts:
        if isinstance(part, (list, tuple)):
            flat.extend(str(item) for item in part)
        else:
            flat.append(str(part))
    scope = "" if pid_scope is None else str(pid_scope)
    if scope:
        flat.insert(0, scope)
    return ":".join(flat)


# --- process-local credentials --------------------------------------------------------

# R reads each environment's JWT from its own runtime option
# (``dataone_token`` / ``dataone_test_token``). The Pythonic form is a
# process-local store keyed by the same option names, so the registry record
# and the error messages name the same credential on both sides
# (PARITY.md row 16).
_TOKENS: Dict[str, Optional[str]] = {}


def set_token(token_option: str, token: Optional[str]) -> None:
    """Store a short-lived DataONE JWT for one environment, this process only."""
    _TOKENS[token_option] = token


def get_token(token_option: str) -> Optional[str]:
    """Read the process-local JWT for one environment, if any."""
    return _TOKENS.get(token_option)


def require_token(config: Dict[str, object]) -> str:
    """Mirror ``.ms_knb_require_token``: each environment names its own."""
    from .eml import _nonempty

    token = get_token(str(config["token_option"]))
    if not _nonempty(token):
        raise ValueError(
            "A short-lived DataONE JWT for the "
            f"{config['knb_environment']!r} environment is required in the "
            "process-local "
            f"{config['token_option']} credential; supply it with "
            f"metasalmonpy.knb_publication.{config['token_setter']}(). The "
            f"{config['knb_environment']!r} credential is a separate token "
            f"from every other environment's; {config['token_option']} is the "
            f"only credential read for {config['node_id']}."
        )
    return str(token)


__all__ = [
    "config_for_node",
    "environment_fields",
    "environment_ids",
    "environment_registry",
    "get_token",
    "knb_config",
    "pid_preimage",
    "plan_config",
    "require_token",
    "resolve_environment",
    "set_token",
    "validate_environment_config",
]
