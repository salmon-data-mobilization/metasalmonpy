"""One owner for the writer provenance an SDP manifest may declare.

Mirrors metasalmon's ``R/provenance.R`` (metasalmon 0.4.0, hub backlog #88).

Every manifest this package writes carries a ``provenance.generated_by``
naming the function that wrote it, plus that implementation's version.
metasalmon writes the same artifacts and names *itself*, so a validator that
accepts only one implementation's writer rejects a byte-identical manifest
written by the other for no data reason. The ruling is that every validator
accepts either implementation (PARITY.md rows 11, 12 and 29).

That ruling was applied one artifact at a time, and each application re-typed
the same pair of strings -- SSSOM, then measurement decompositions, then the
reproducibility manifest. Three hand-maintained string tables is exactly how a
ruling gets applied twice out of three times: on the R side it *was*, and the
reproducibility validator sat rejecting Python-written manifests until 0.4.0.
So the accepted set lives here now, and the next manifest type inherits dual
acceptance instead of re-deriving it.

The two writer names are deliberately not the same shape: R's is a ``::``
namespace call and this package's a ``.`` module attribute, each written the
way its own users would call it. Both are derived from the bare function name,
so a validator names its writer once.

metasalmon keeps the same table under the same rule, in ``R/provenance.R``.
Adding a writer here means adding it there.
"""

from __future__ import annotations

from typing import Optional

#: The version field each implementation's manifests carry.
METASALMON_VERSION_FIELD = "metasalmon_version"
METASALMONPY_VERSION_FIELD = "metasalmonpy_version"


def accepted_writers(writer: str) -> dict:
    """The ``generated_by`` values a validator accepts for one manifest type.

    ``writer`` is the bare function name, e.g. ``"write_sdp_sssom"``.
    """
    return {
        "metasalmon::" + writer: METASALMON_VERSION_FIELD,
        "metasalmonpy." + writer: METASALMONPY_VERSION_FIELD,
    }


def version_field(provenance: object, writer: str) -> Optional[str]:
    """Mirror ``.ms_manifest_provenance_version_field``.

    Return the provenance field that must carry a version for this manifest's
    declared writer, or ``None`` when ``generated_by`` is absent, malformed, or
    names neither mirror implementation. A non-dict ``provenance`` is not an
    error here; it is simply not an accepted provenance block.
    """
    if not isinstance(provenance, dict):
        return None
    generated_by = provenance.get("generated_by")
    if not isinstance(generated_by, str):
        return None
    return accepted_writers(writer).get(generated_by)


def version_ok(value: object) -> bool:
    """Mirror ``.ms_manifest_provenance_version_ok``: one non-blank string.

    Deliberately NOT called by the SSSOM validator, which asks only that the
    field be present. That is not an oversight: metasalmon's
    ``.ms_sssom_validate_manifest()`` asks exactly the same weaker question,
    and the two readers of the same artifact must accept the same manifests.

    *Retires when:* both SSSOM validators tighten to the non-blank shape their
    four sibling validators already use -- in the same stream, so the two sides
    never disagree about which manifests are valid.
    """
    return isinstance(value, str) and bool(value.strip())


__all__ = [
    "METASALMONPY_VERSION_FIELD",
    "METASALMON_VERSION_FIELD",
    "accepted_writers",
    "version_field",
    "version_ok",
]
