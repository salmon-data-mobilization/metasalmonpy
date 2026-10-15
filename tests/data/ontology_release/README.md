# Release-snapshot fixtures

Small stand-ins for the release snapshots smn and gcdfo publish as
`docs/releases/<version>/`, used by metasalmon's `test-ontology-release.R` and
metasalmonpy's `tests/test_ontology_release.py`. metasalmonpy vendors this
directory unchanged as `tests/data/ontology_release/`, and its test fails when
the two copies differ wherever a metasalmon checkout is present.

- `smn-0.0.3/smn.owl` is written the way the smn release serializer writes
  RDF/XML: every individual is also declared `owl:NamedIndividual`, the OBO
  namespace is bound to `ns1`, and one subject is spread over two nodes.
- `smn-modules/` holds the same smn terms written as the smn modules write
  them, under the module names the latest reader gives them. The release
  reader must give every shared term the hints the module reader gives it.
- `gcdfo-0.0.9/gcdfo.owl` is a few gcdfo terms with a version IRI.

No fixture carries a `MANIFEST.sha256`: the tests write one into a temporary
copy, from the bytes as checked out, so a checkout that rewrites line endings
cannot make a correct manifest look wrong.
