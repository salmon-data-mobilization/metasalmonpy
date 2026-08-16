# EML 2.2.0 XML Schema set (vendored)

Provenance: copied verbatim (27 `.xsd` files, byte-identical) from the
`xsd/eml-2.2.0/` directory of the installed **emld 0.5.3** R package
(<https://github.com/ropensci/emld>) on 2026-08-15. This is the exact schema
set metasalmon's `write_eml_from_sdp()` validates against via
`emld::eml_validate(..., schema = system.file("xsd", "eml-2.2.0", "eml.xsd",
package = "emld"))`, so validating against these files with lxml (libxml2 —
the same engine emld uses) gives accept/reject parity by construction.

Upstream: the Ecological Metadata Language (EML) 2.2.0 specification,
copyright 1997-2002 Regents of the University of California, University of
New Mexico, and Arizona State University; developed by the Knowledge Network
for Biocomplexity (KNB) / NCEAS (<https://eml.ecoinformatics.org/>). Each
`.xsd` file carries its own copyright and GPL notice header. The schemas are
distributed under the GNU General Public License, version 2 or later — see
`LICENSE` in this directory (the verbatim GPL-2 text shipped with the EML
schema distribution inside emld).

Entry point: `eml.xsd`. All imports/includes are relative within this
directory; do not rename or remove individual files.
