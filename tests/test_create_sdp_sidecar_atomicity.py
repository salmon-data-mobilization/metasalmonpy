"""An abort while ``create_sdp()`` renders one of its own sidecars must leave the
previous copy of that file byte-identical.

The mirror half of hub queue B-111 (metasalmon backlog #111), queued as B-179,
and modelled on metasalmon's ``tests/testthat/test-create-sdp-sidecar-atomicity.R``.

``create_sdp()`` writes three files of its own AFTER ``write_salmon_datapackage()``
has installed the package: ``README-review.txt``, ``semantic_suggestions.csv``
and ``metadata/metadata-edh-hnap.xml``. Each used to go through
``_replace_create_output()``, which unlinked the existing file and left the
caller to render a replacement straight into the final name, so an abort
anywhere in that render destroyed the previous copy. A re-run regenerates all
three, so the loss only matters for a copy the user has changed, and those are
exactly the copies a re-run cannot reproduce: an annotated checklist, a
suggestions file carrying review decisions, and the EDH XML of a package whose
metadata has since moved on. Each test below therefore puts recognisable bytes
in place first and asserts those exact bytes survive. It checks bytes rather
than parseability, because a half-written CSV can still parse.

**The injection point is what these tests are about.** Each hook sits at a
sidecar's RENDER step, which is the one point the old code and the new code
share. So each test fails on the pre-fix code, where the file is already gone
when the render runs, and passes on the fixed code, which renders to bytes and
then installs them with ``atomic_io.atomic_write()``. An abort injected at the
install instead proves nothing: a staged-sibling rename leaves the prior bytes
in place by construction, so that test passes on the unfixed code too.

**Every hook is keyed on CONTENT, never on the destination path.** The fixed
README and EDH renders go through the same writer as before, but into a scratch
file, so a hook keyed on the sidecar's own name would stop firing on the fixed
code and the test would then be testing nothing. Keying on content keeps the
hook on the render wherever the bytes go. Each test also asserts that its own
injected error is the one raised, so a hook that stops firing fails loudly
instead of passing quietly.

The EDH sidecar gets a fourth injection that R's file has no counterpart for.
This package builds the XML from a full ``read_salmon_datapackage()`` of the
package on disk, where R builds from the in-memory artifacts, and before the
fix that read ran after the unlink. So every read and parse failure of the
whole package sat inside the destroyed-file window as well. The fix keeps the
read and moves it, with the render, ahead of anything that touches the file.
"""

from __future__ import annotations

import ast
import datetime
import functools
import inspect
import os
import textwrap
import warnings
from pathlib import Path

import pandas as pd
import pytest

from metasalmonpy import atomic_io, create_sdp, edh_xml, package_io
from metasalmonpy import semantics as sem

# The generated checklist's first line. The README hook keys on it, so if the
# heading changes, that test's ``pytest.raises`` fails rather than going green
# over a hook that no longer fires.
README_HEADING = "SALMON DATA PACKAGE REVIEW\n"

SIDECARS = (
    "README-review.txt",
    "semantic_suggestions.csv",
    "metadata/metadata-edh-hnap.xml",
)


class _Injected(RuntimeError):
    """Distinct from every exception ``create_sdp()`` raises on its own."""


def _stub_search(query, role=None, sources=None, **kwargs):
    hits = {
        "variable": ("Spawner Abundance", "https://w3id.org/smn/SpawnerAbundance"),
        "property": ("Abundance", "https://w3id.org/smn/Abundance"),
        "entity": ("Spawner", "https://w3id.org/smn/Spawner"),
        "unit": ("Count", "http://qudt.org/vocab/unit/NUM"),
    }
    if role not in hits:
        return pd.DataFrame()
    label, iri = hits[role]
    return pd.DataFrame(
        {
            "label": [label],
            "iri": [iri],
            "source": ["smn"],
            "ontology": ["smn"],
            "role": [role],
            "match_type": ["label_exact"],
            "definition": [f"{label}, a stub definition."],
            "score": [4.9],
        }
    )


@pytest.fixture(autouse=True)
def _stub_retrieval(monkeypatch):
    """Retrieval stubbed the way ``test_review_console.seeded_package`` does it,
    so the create path seeds a non-empty ``semantic_suggestions.csv`` offline."""
    monkeypatch.setattr(
        sem,
        "suggest_semantics",
        functools.partial(sem.suggest_semantics, search_fn=_stub_search),
    )


def _create(path: Path, *, include_edh_xml: bool) -> Path:
    # Exactly the two warnings a freshly seeded package always raises are
    # silenced, and nothing else. Seeding writes ``REVIEW:``-prefixed IRIs, and
    # an EDH XML built from unreviewed metadata is a draft. Both are
    # ``create_sdp()``'s own documented behaviour and are not under test here.
    # Anything else, such as a ``ResourceWarning`` from a leaked scratch file,
    # still reaches pytest's warning summary.
    # *Retires when:* ``create_sdp()`` stops warning about a fresh package's
    # review values, or these tests seed a package with none.
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", message="REVIEW-prefixed IRI values were found", category=UserWarning
        )
        warnings.filterwarnings(
            "ignore", message="Created EDH XML is a draft", category=UserWarning
        )
        return create_sdp(
            {
                "spawners": pd.DataFrame(
                    {
                        "stream_name": ["Bear Creek", "Elk River"],
                        "spawner_count": [120, 340],
                    }
                )
            },
            path=str(path),
            dataset_id="sidecar-demo",
            seed_semantics=True,
            semantic_max_per_role=2,
            seed_verbose=False,
            check_updates=False,
            overwrite=True,
            include_edh_xml=include_edh_xml,
        )


def _bytes(path: Path):
    """``None`` rather than an error when the file is gone, so a destroyed
    sidecar fails the byte comparison with a legible message."""
    return path.read_bytes() if path.is_file() else None


def test_an_abort_rendering_readme_review_leaves_the_prior_file_byte_identical(
    tmp_path, monkeypatch
):
    package = _create(tmp_path / "readme-abort", include_edh_xml=False)
    readme = package / "README-review.txt"
    assert readme.is_file()
    # Stands in for the copy a reviewer annotated in place. Re-running
    # ``create_sdp()`` cannot bring these lines back.
    readme.write_bytes(
        b"SALMON DATA PACKAGE REVIEW -- ANNOTATED\n[x] 1. decided on 2026-09-24\n"
    )
    before = _bytes(readme)

    # The README's render is ``Path.write_text``: it was the direct write into
    # the sidecar before the fix, and it renders into a scratch file after it.
    real_write_text = Path.write_text

    def write_text(self, data, *args, **kwargs):
        if str(data).startswith(README_HEADING):
            raise _Injected("injected abort: README-review.txt render")
        return real_write_text(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write_text)
    with pytest.raises(_Injected, match="README-review.txt render"):
        _create(package, include_edh_xml=False)
    monkeypatch.undo()

    assert _bytes(readme) == before


def test_an_abort_rendering_semantic_suggestions_leaves_the_prior_file_byte_identical(
    tmp_path, monkeypatch
):
    package = _create(tmp_path / "suggestions-abort", include_edh_xml=False)
    suggestions = package / "semantic_suggestions.csv"
    assert suggestions.is_file()
    # Stands in for the evidence trail after review. The ``decision`` and
    # ``decision_reason`` columns ``apply_sdp_semantics()`` writes cannot be
    # reconstructed by a re-run.
    suggestions.write_bytes(
        b"column_name,dictionary_role,iri,decision,decision_reason\n"
        b"spawner_count,variable,https://w3id.org/smn/SpawnerAbundance,"
        b"accepted,reviewed by hand\n"
    )
    before = _bytes(suggestions)

    # Keyed on the semantic target row's ``target_sdp_field`` column rather than
    # on ``to_csv``'s path argument. The fixed render calls ``to_csv`` with no
    # path at all, so a path-keyed hook would never fire there. No metadata CSV
    # the package writer renders earlier in the call carries this column, and
    # the RED run against the pre-fix code is what shows the hook lands after
    # the old unlink rather than before it.
    real_to_csv = pd.DataFrame.to_csv

    def to_csv(self, *args, **kwargs):
        if "target_sdp_field" in self.columns:
            raise _Injected("injected abort: semantic_suggestions.csv render")
        return real_to_csv(self, *args, **kwargs)

    monkeypatch.setattr(pd.DataFrame, "to_csv", to_csv)
    with pytest.raises(_Injected, match="semantic_suggestions.csv render"):
        _create(package, include_edh_xml=False)
    monkeypatch.undo()

    assert _bytes(suggestions) == before


def _plant_shipped_edh_xml(package: Path) -> bytes:
    edh = package / "metadata" / "metadata-edh-hnap.xml"
    assert edh.is_file()
    # This XML is rendered from the package's metadata at write time, so once
    # that metadata has moved on, the copy that was shipped cannot be rebuilt.
    edh.write_bytes(
        b'<?xml version="1.0"?>\n'
        b"<gmd:MD_Metadata><!-- shipped to EDH 2026-09-01 --></gmd:MD_Metadata>\n"
    )
    return _bytes(edh)


def test_an_abort_rendering_the_edh_xml_leaves_the_prior_file_byte_identical(
    tmp_path, monkeypatch
):
    package = _create(tmp_path / "edh-abort", include_edh_xml=True)
    edh = package / "metadata" / "metadata-edh-hnap.xml"
    before = _plant_shipped_edh_xml(package)

    # ``edh_build_hnap_xml`` IS the render, so it needs no content key: it runs
    # once in ``create_sdp()``, at this step, before the fix and after it.
    def boom(*args, **kwargs):
        raise _Injected("injected abort: EDH XML render")

    monkeypatch.setattr(edh_xml, "edh_build_hnap_xml", boom)
    with pytest.raises(_Injected, match="EDH XML render"):
        _create(package, include_edh_xml=True)
    monkeypatch.undo()

    assert _bytes(edh) == before


def test_an_abort_reading_the_package_for_the_edh_xml_leaves_the_prior_file_byte_identical(
    tmp_path, monkeypatch
):
    # The difference from R, pinned on its own. ``read_salmon_datapackage()``
    # is called once in ``create_sdp()``, by the EDH step only, so a raising
    # stand-in for it is a read or parse failure at exactly that point. Before
    # the fix it ran after the unlink.
    package = _create(tmp_path / "edh-read-abort", include_edh_xml=True)
    edh = package / "metadata" / "metadata-edh-hnap.xml"
    before = _plant_shipped_edh_xml(package)

    def boom(*args, **kwargs):
        raise _Injected("injected abort: package read for the EDH XML")

    monkeypatch.setattr(package_io, "read_salmon_datapackage", boom)
    with pytest.raises(_Injected, match="package read for the EDH XML"):
        _create(package, include_edh_xml=True)
    monkeypatch.undo()

    assert _bytes(edh) == before


def test_the_three_create_owned_sidecars_still_round_trip_on_the_happy_path(
    tmp_path, monkeypatch
):
    # Every test above aborts, so none of them would notice the rewrite
    # landing different bytes from the ones it used to land. This one pins
    # that the rewrite still happens and still installs the generated content.
    #
    # The EDH XML stamps today's date, so the date is frozen. Otherwise a run
    # that crossed midnight between the two creates would fail for a reason
    # that has nothing to do with atomicity.
    class _FrozenDate(datetime.date):
        @classmethod
        def today(cls):
            return cls(2026, 9, 24)

    monkeypatch.setattr(edh_xml, "date", _FrozenDate)

    package = _create(tmp_path / "happy-path", include_edh_xml=True)
    paths = [package / name for name in SIDECARS]
    assert all(path.is_file() for path in paths)
    first = [path.read_bytes() for path in paths]
    # What the first run landed has to be the generated content. The re-run
    # comparison below is only against the first run, so without this a
    # writer that was wrong in the same way both times would pass. That
    # happened: an install that wrote empty bytes went green here before these
    # three lines were added.
    readme_bytes, suggestions_bytes, edh_bytes = first
    assert readme_bytes.startswith(README_HEADING.encode("utf-8"))
    assert b"target_sdp_field" in suggestions_bytes.splitlines()[0]
    assert b"https://w3id.org/smn/SpawnerAbundance" in suggestions_bytes
    assert b"MD_Metadata" in edh_bytes and b"sidecar-demo" in edh_bytes

    for path in paths:
        path.write_bytes(b"clobbered\n")
    _create(package, include_edh_xml=True)

    assert [path.read_bytes() for path in paths] == first

    # The install stages a dot-prefixed sibling and renames it into place. The
    # stage must never outlive a successful call, whichever writer made it.
    stray = sorted(
        entry.name
        for directory in (package, package / "metadata")
        for entry in directory.iterdir()
        if any(entry.name.startswith(f".{path.name}-") for path in paths)
    )
    assert stray == []

    # And the installed file has the mode a plain write gives it rather than
    # ``mkstemp``'s 0600, which is the trap ``atomic_io`` exists to close
    # (PARITY.md row 24). The pre-fix ``write_text`` got this for free.
    expected_mode = atomic_io.default_file_mode()
    assert [path.stat().st_mode & 0o777 for path in paths] == [expected_mode] * 3


def test_a_hard_linked_sidecar_is_replaced_not_written_through(tmp_path):
    """The protection the deleted ``_replace_create_output()`` existed for,
    pinned now that nothing unlinks first.

    Mirrors R's ``a hard-linked create-owned output is replaced, not written
    through`` in ``tests/testthat/test-package-helpers.R``. The containment
    check sees symbolic links and cannot see a hard link, so a writer that
    opened a sidecar in place would truncate an inode it shares with a file
    outside the package. The staged-sibling install never opens the
    destination, so the outside file keeps its content. This test passes both
    before the fix and after it. It is here so that deleting the helper rests
    on a checked claim rather than an argued one.
    """
    package = _create(tmp_path / "pkg", include_edh_xml=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    precious = b"PRECIOUS EXTERNAL CONTENT\n"
    for index, name in enumerate(SIDECARS):
        external = outside / f"precious-{index}.txt"
        external.write_bytes(precious)
        sidecar = package / name
        sidecar.unlink()
        try:
            os.link(external, sidecar)
        except (AttributeError, NotImplementedError, OSError):
            pytest.skip("hard links are not supported here")

    _create(package, include_edh_xml=True)

    for index, name in enumerate(SIDECARS):
        assert (outside / f"precious-{index}.txt").read_bytes() == precious
        assert (package / name).read_bytes() != precious


def test_suggestions_csv_bytes_match_a_direct_to_csv_write(tmp_path):
    """Rendering to bytes must not change what the bytes are.

    The twin of ``test_metadata_csv_bytes_match_a_direct_to_csv_write``. The fix
    changes when ``semantic_suggestions.csv`` is installed and must not change a
    byte of it. The former call wrote ``to_csv(path, ...)``, and the render
    needs ``to_csv(None, ...)``. pandas documents the line terminator of a path
    write as defaulting to ``os.linesep``, so this asserts the two are equal
    rather than trusting it, on whatever platform the suite runs. The logical
    column is there to catch the renderer being swapped for
    ``_metadata_csv_bytes()``, which renders ``TRUE``/``FALSE`` where this file
    has always had ``True``/``False``.

    *Retires when:* ``semantic_suggestions.csv`` stops being rendered through
    ``DataFrame.to_csv``, at which point there is no second rendering path to
    compare against.
    """
    frame = pd.DataFrame(
        {
            "column_name": ["spawner_count", "stream_name", "note"],
            "iri": ["REVIEW:https://w3id.org/smn/SpawnerAbundance", None, 'q"m'],
            "definition": ["a,b", "line\nbreak", "Fraser sockeye — Chilko"],
            "score": [4.9, None, 3.0],
            "flag": [True, False, True],
        }
    )
    direct = tmp_path / "direct.csv"
    frame.to_csv(direct, index=False, na_rep=package_io.csv_na_token())

    assert package_io._suggestions_csv_bytes(frame) == direct.read_bytes()


def _code_of(function) -> str:
    """The function's code with its docstring and comments removed.

    This is the analogue of R's ``deparse(body(...))``. A comment that
    describes what the code used to do is not a filesystem call and should not
    trip the guard, and a string literal mentioning one would still be flagged,
    which fails closed.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    definition = tree.body[0]
    first = definition.body[0] if definition.body else None
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        definition.body = definition.body[1:]
    return ast.unparse(definition)


def test_no_create_owned_sidecar_is_written_by_a_direct_filesystem_call():
    """The structural half, modelled on R's guard of the same name and on
    ``test_write_datapackage_abort_safety``'s guard for the package writer.

    Each abort injection above proves that one rewrite is safe. This test
    proves that no FOURTH direct write has been added beside them, which is the
    regression the injections cannot see.

    Its scope is exactly the two functions that write the three sidecars and
    nothing else, stated because a guard that claims more than it checks is
    worse than no guard. ``create_sdp()`` holds the suggestions and EDH writes,
    and the README write lives in ``_write_review_readme()``. A sidecar write
    moved into a third function escapes this guard, so add that function here
    when one appears. The render helpers are deliberately outside it, because
    they write into a scratch directory of their own and never into the package.
    The token list is the package writer guard's list plus the deleted
    helper's name. It is a deliberately literal scan, so a mutation reached
    through an alias the list does not name is not caught.

    Two tokens are exempted, each for a reason:

    * ``.unlink(``: ``create_sdp()`` DELETES ``semantic_suggestions.csv`` when
      there is no shortlist to write. Deleting a file is already atomic, and
      the writer has no delete to route it through.
    * ``.mkdir(``: the EDH XML's ``metadata/`` directory. The builder used to
      create it as a side effect of writing there, and the install needs it to
      exist. Creating a directory destroys nothing.

    *Retires when:* the three sidecars are rendered into one write set that
    owns the only filesystem handle, which makes a stray direct write
    unrepresentable, or when ``create_sdp()`` stops writing files of its own.
    """
    exempt = (".unlink(", ".mkdir(")
    mutating_tokens = tuple(
        token
        for token in (
            ".to_csv(",
            ".write_text(",
            ".write_bytes(",
            ".mkdir(",
            ".unlink(",
            ".rmdir(",
            "json.dump(",
            "shutil.rmtree(",
            "shutil.copy",
            "shutil.move(",
            "os.replace(",
            "os.remove(",
            "os.rename(",
            "os.makedirs(",
            ".open(\"w",
            ".open('w",
            "_replace_create_output(",
        )
        if token not in exempt
    )

    for writer in (package_io.create_sdp, package_io._write_review_readme):
        code = _code_of(writer)
        found = [token for token in mutating_tokens if token in code]
        assert found == [], (
            f"{writer.__name__}() contains direct filesystem calls: "
            + ", ".join(found)
        )

    # The exemptions are asserted to be REACHED, not merely permitted. An
    # exemption for a call that is no longer there is a hole nobody can see.
    create_code = _code_of(package_io.create_sdp)
    for token in exempt:
        assert token in create_code, f"create_sdp() no longer needs the {token} exemption"
