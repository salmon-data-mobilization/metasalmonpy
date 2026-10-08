# Commons gap export fixtures

`register-excerpt.json` is a selected excerpt of the unchanged record objects
from `scripts/okf-check.py . --gaps` at salmon-knowledge-commons commit
`1b7594e6263933e08ae196b4374373eb907c137f` (validator exit 0). The source export had 110 rows;
its UTF-8 SHA-256 was `d827b20db27f1d81ac0e3d923ed457316df5e5557e44aaa29154624a7a9b8f53`.
Each retained record has the exact source field values and JSON rendering;
only the containing array was shortened. Source row order is preserved.
The selected original zero-based indices are `0,2,3,4,5,11,16,18,34,35,36,37`.

These are draft/unverified source records, not approved term selections.
They cover open SMN/GCDFO, proposed, contested, blockers, conflicts, explicit
evidence needed, do-not-mint, PSC CV, undecided, and multiple gaps on one card.

`synthetic-controls.json` is separately labelled synthetic input for valid
rejected, new-scheme, and deprecated-card controls absent from this excerpt.
The test file creates malformed controls from these fixtures in temporary
files. No fixture is an instruction to reopen a gap or publish an issue.

The two JSON fixtures are byte-identical to metasalmon's B-278 fixtures. The
B-279 tests use them to check the Python reader and renderer against the same
source records; they do not turn the draft records into verified term choices.
Retire these fixtures only when replacing this reader contract with a newly
reviewed export schema and matching fixtures in both packages.
