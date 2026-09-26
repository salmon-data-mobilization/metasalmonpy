"""No canonical string may be built by the platform C library.

``datetime.strftime`` delegates ``%Y`` to libc, and the platforms disagree
below year 1000: glibc renders ``date(1, 1, 1)`` as ``1-01-01`` where the
macOS/BSD implementation pads it to ``0001-01-01``. Canonical value keys,
canonical CSV bytes and normalized dimension values are all compared
byte-for-byte -- against R, against a package's own ``codes.csv``, and across
machines -- so none of them may vary with the libc that built the interpreter.

This guard exists because that divergence is **invisible to a macOS
developer**: the whole suite was green locally while
``test_canonical_keys_match_era_r[date]`` failed on every Linux CI run, and
reading the call site tells you nothing, exactly as reading an import
statement cannot tell you whether a path is core-deps safe. It is the same
class of decay the core-dependency tests were written for.

The guard retires when Python stops handing year formatting to libc. An
individual entry in ``ALLOWED`` retires when its call site goes away.
"""

from __future__ import annotations

import ast
import datetime as _dt
import json
import re
import shutil
import tempfile
import warnings
import xml.etree.ElementTree as ET
from pathlib import Path

import pandas as pd
import pytest

from metasalmonpy import eml
from metasalmonpy import observation_structures as obs
from metasalmonpy import package_io
from metasalmonpy import resource_types as rt
from metasalmonpy import write_eml_from_sdp

REPO_ROOT = Path(__file__).resolve().parent.parent

# Call sites permitted to use ``strftime`` anyway, each with the condition that
# retires the entry. Add one only for text a human reads that no machine
# re-checks -- never for a comparison key, an identifier, or written bytes.
# Format: "<module>:<function>" -> reason.
ALLOWED: dict[str, str] = {}


def _package_sources() -> list[Path]:
    """Every module shipped as ``metasalmonpy``, tests excluded.

    ``pyproject.toml`` maps the repo root to the package, so the modules are
    the root ``*.py`` files plus ``scripts/``.
    """
    sources = sorted(path for path in REPO_ROOT.glob("*.py"))
    sources += sorted(REPO_ROOT.glob("scripts/*.py"))
    return [path for path in sources if "tests" not in path.parts]


def _strftime_call_sites(path: Path) -> list[str]:
    """``module:function`` for every ``.strftime(...)`` call in one module."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    enclosing: dict[ast.AST, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                enclosing.setdefault(child, node.name)

    found = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "strftime"
        ):
            found.append(f"{path.name}:{enclosing.get(node, '<module>')}")
    return found


def test_no_package_module_formats_a_calendar_through_libc():
    offenders = []
    for path in _package_sources():
        for site in _strftime_call_sites(path):
            if site not in ALLOWED:
                offenders.append(site)

    assert not offenders, (
        "strftime() renders %Y through the platform C library, which does not "
        "zero-pad a year below 1000 on glibc. Use resource_types._iso_date / "
        "._iso_seconds, or date.isoformat(), which are pure Python. If the "
        "string is only ever read by a human, add the call site to ALLOWED "
        "with the reason that would retire the entry. Offenders: "
        f"{sorted(set(offenders))}"
    )


def test_every_allowlist_entry_still_has_a_call_site():
    """An allowance that outlived its cause hides the next failure."""
    live = {site for path in _package_sources() for site in _strftime_call_sites(path)}
    stale = sorted(set(ALLOWED) - live)
    assert not stale, f"ALLOWED entries with no matching call site: {stale}"


def test_a_pre_1000_year_keys_with_a_padded_year():
    """The measured R verdict for ``0001-01-01``, asserted directly.

    Redundant with ``test_canonical_keys_match_era_r[date]`` on purpose: that
    test compares a whole corpus and reports the first difference, while this
    one names the boundary that actually broke.
    """
    assert rt.canonical_value_tokens(["0001-01-01"], "date") == ["0001-01-01"]
    assert rt.canonical_value_tokens([_dt.date(1, 1, 1)], "date") == ["0001-01-01"]


def test_a_pre_1000_datetime_keys_with_a_padded_year():
    """The same boundary on the datetime key, which has its own renderer."""
    key = rt.format_datetime_token(_dt.datetime(42, 7, 9, 1, 2, 3))
    assert key is not None and key.startswith("0042-07-09T01:02:03")


# --- one value, one rendering ----------------------------------------------
#
# Everything below is hub item **B-145**, the metasalmonpy half of **B-115**.
# It lives in this file because this is the guard the defect slipped past: the
# two tests above pin the padded year in a canonical *key*, and nothing pinned
# it -- or the separator, or the zone marker -- in the bytes a package is
# actually written with.
#
# The spelling is **ruled, not chosen** (Brett, 2026-09-14, once for both
# implementations): readr's ISO instant form, the ``T`` separator and the ``Z``
# zone marker.

# The item's own fixture, and metasalmon B-115's: a pre-1000 instant, so the
# year is exercised, and an all-midnight instant, because pandas' column-wise
# ``to_csv`` formatter drops the time from a column whose values are all
# midnight -- which is how ``temporal_end`` became the bare ``2024-12-31``.
INSTANT_START = _dt.datetime(999, 6, 5, 13, 45, 30)
INSTANT_END = _dt.datetime(2024, 12, 31, 0, 0, 0)

RULED_INSTANT = re.compile(r"^[0-9]+-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")

# ``_iso_seconds`` renders a calendar without committing to a zone marker, so a
# caller that appends its own is a second rendering of an instant waiting to
# drift from the first. **Four** such call sites existed before B-145, in two
# modules, and two of them wrote a different string from the other two for the
# same tz-aware instant. Each entry says what would retire it.
ISO_SECONDS_CALLERS: dict[str, str] = {
    "resource_types.py:iso_instant_text": (
        "The one rendering of an instant as written bytes. Retires when "
        "nothing -- this is the function every other caller was folded into."
    ),
    "resource_types.py:format_datetime_token": (
        "A canonical comparison KEY, not written bytes: microsecond "
        "precision, and an exact-epoch suffix past it, so it cannot share "
        "iso_instant_text()'s second-resolution output. Retires when the key "
        "and the byte renderer are ruled to be the same string, which would "
        "mean writing microseconds into every package."
    ),
}


def _iso_seconds_call_sites(path: Path) -> list[str]:
    """``module:function`` for every ``_iso_seconds(...)`` call in one module."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    enclosing: dict[ast.AST, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                enclosing.setdefault(child, node.name)

    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and (
            (isinstance(node.func, ast.Name) and node.func.id == "_iso_seconds")
            or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "_iso_seconds"
            )
        ):
            found.append(f"{path.name}:{enclosing.get(node, '<module>')}")
    return found


def test_only_the_named_callers_render_an_instant_themselves():
    """An instant has one rendering, and this is what keeps it to one.

    ``AGENTS.md``'s "one value, one rendering -- and the renderer is chosen by
    type" contract: two renderings of one value looks like ordinary code from
    every angle except the one where you compare the outputs. It is found by
    reading, so this test is the reading, done mechanically.

    What it would have caught, measured on the pre-fix tree rather than
    asserted. ``_iso_seconds(x) + "Z"`` appeared at **four** call sites across
    two modules: ``render_resource_frame`` twice,
    ``observation_structures._typed_character`` and ``._normalize_typed_values``.
    Only ``_typed_character`` folded a tz-aware value to UTC first;
    ``_normalize_typed_values`` was handed a value ``_parse_datetime`` had
    already folded; and neither branch of ``render_resource_frame`` folded at
    all. So for ``datetime(2024, 12, 31, tzinfo=UTC-08:00)`` the package wrote
    **two different strings for one instant** --
    ``render_resource_frame`` gave ``2024-12-31T00:00:00Z``, a local wall clock
    wearing a ``Z``, where ``_typed_character`` gave the correct
    ``2024-12-31T08:00:00Z``, which is also what ``readr::write_csv()`` writes.
    Every suite stayed green throughout, because each test built its
    expectation with the same call it was testing.
    """
    offenders = []
    for path in _package_sources():
        for site in _iso_seconds_call_sites(path):
            if site not in ISO_SECONDS_CALLERS:
                offenders.append(site)

    assert not offenders, (
        "_iso_seconds() renders a calendar without a zone marker, so appending "
        "one is a second rendering of an instant. Call "
        "resource_types.iso_instant_text() instead, which is the single "
        "renderer every written instant goes through. If this really is a "
        "different string -- a comparison key rather than written bytes -- add "
        "the call site to ISO_SECONDS_CALLERS with the condition that would "
        f"retire the entry. Offenders: {sorted(set(offenders))}"
    )


def test_every_iso_seconds_caller_entry_still_has_a_call_site():
    """An allowance that outlived its cause hides the next failure."""
    live = {
        site for path in _package_sources() for site in _iso_seconds_call_sites(path)
    }
    stale = sorted(set(ISO_SECONDS_CALLERS) - live)
    assert not stale, f"ISO_SECONDS_CALLERS entries with no matching call site: {stale}"


def test_every_instant_emitter_agrees_on_one_value():
    """The descriptor, both CSV writers and the dimension normalizer agree.

    Not "they call the same helper" -- that is the implementation. This asserts
    the observable: four independent paths, one instant, one string.
    """
    rendered = rt.iso_instant_text(INSTANT_START)

    assert package_io._descriptor_temporal_text(INSTANT_START) == rendered
    assert package_io._descriptor_temporal_text(pd.Timestamp(INSTANT_START)) == rendered
    assert obs._typed_character(INSTANT_START) == rendered

    frame = pd.DataFrame({"when": [INSTANT_START]})
    assert rt.render_resource_frame(frame)["when"].iloc[0] == rendered
    assert package_io._metadata_csv_bytes(frame).decode("utf-8").splitlines()[1] == (
        rendered
    )


def test_a_tz_aware_instant_is_folded_to_utc_before_the_z_is_added():
    """``Z`` is a claim about the instant, not decoration.

    ``readr::write_csv()`` folds to UTC -- measured 2026-09-16, R 4.3.3 / readr
    2.2.0, which writes ``2024-12-31T08:00:00Z`` for the value below -- so a
    renderer that stamps ``Z`` on a local wall clock moves the instant and
    diverges from metasalmon silently. Both branches of
    ``render_resource_frame`` did exactly that before B-145; they are asserted
    here too, because the helper agreeing with itself proves nothing about the
    callers.
    """
    aware = INSTANT_END.replace(tzinfo=_dt.timezone(_dt.timedelta(hours=-8)))
    assert rt.iso_instant_text(aware) == "2024-12-31T08:00:00Z"
    assert obs._typed_character(aware) == "2024-12-31T08:00:00Z"
    object_column = pd.DataFrame({"w": pd.Series([aware], dtype="object")})
    assert rt.render_resource_frame(object_column)["w"].iloc[0] == (
        "2024-12-31T08:00:00Z"
    )
    assert rt.render_resource_frame(pd.DataFrame({"w": [aware]}))["w"].iloc[0] == (
        "2024-12-31T08:00:00Z"
    )


def test_a_missing_instant_is_a_missing_field_and_not_a_crash():
    """``pd.NaT`` subclasses ``datetime``, which is the trap this pins.

    ``isinstance(pd.NaT, datetime.datetime)`` is **True**, so a bare isinstance
    test accepts a missing value and the renderer then raises
    ``ValueError: cannot convert float NaN to integer``. Measured on the
    pre-fix tree: ``render_resource_frame`` raised for an object column holding
    one instant and one ``NaT``, while its own ``datetime64`` branch two lines
    above guarded with ``pd.isna`` -- one function, two branches, two answers.
    metasalmon completes that write rather than aborting:
    ``readr::write_csv(na = .ms_csv_na_token())`` renders an ``NA`` POSIXct as
    the empty field (measured, R 4.3.3 / readr 2.2.0; readr's own default
    ``na`` is the two characters ``NA``, which is why the token is named). So
    raising was a divergence from metasalmon and not only an internal
    inconsistency.

    Both dtypes and both writers are asserted, because the defect was that one
    branch had the guard and the other did not.
    """
    assert isinstance(pd.NaT, _dt.datetime)  # the premise, pinned
    assert not rt.is_instant(pd.NaT)
    assert rt.is_instant(INSTANT_END)

    for frame in (
        pd.DataFrame({"w": pd.Series([INSTANT_END, pd.NaT], dtype="object")}),
        pd.DataFrame({"w": [INSTANT_END, None]}),
    ):
        rendered = list(rt.render_resource_frame(frame)["w"])
        assert rendered[0] == "2024-12-31T00:00:00Z"
        assert rendered[1] is None or pd.isna(rendered[1])

        rows = package_io._metadata_csv_bytes(frame).decode("utf-8").splitlines()
        assert rows[1] == "2024-12-31T00:00:00Z"
        assert rows[2] in ('""', "")


def _package_with_typed_instants(tmpdir: str) -> Path:
    """The item's fixture, written through the real writer."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        target = package_io.write_salmon_datapackage(
            {"obs": pd.DataFrame({"note": ["a"]})},
            pd.DataFrame(
                [
                    {
                        "dataset_id": "demo",
                        "title": "Demo",
                        "temporal_start": INSTANT_START,
                        "temporal_end": INSTANT_END,
                    }
                ]
            ),
            pd.DataFrame(
                [{"dataset_id": "demo", "table_id": "obs", "file_name": "obs.csv"}]
            ),
            pd.DataFrame(
                [
                    {
                        "dataset_id": "demo",
                        "table_id": "obs",
                        "column_name": "note",
                        "column_label": "Note",
                        "column_description": "A note.",
                        "column_role": "attribute",
                        "value_type": "string",
                        "required": False,
                    }
                ]
            ),
            path=tmpdir,
            overwrite=True,
        )
    return Path(target)


def _written_instants(target: Path) -> tuple[dict, dict]:
    """``temporal_start``/``temporal_end`` as each written file spells them."""
    descriptor = json.loads((target / "datapackage.json").read_text(encoding="utf-8"))
    row = pd.read_csv(
        target / "metadata" / "dataset.csv", dtype=str, keep_default_na=False
    ).iloc[0]
    return (
        {
            "start": descriptor.get("temporal", {}).get("start"),
            "end": descriptor.get("temporal", {}).get("end"),
        },
        {"start": row["temporal_start"], "end": row["temporal_end"]},
    )


def test_datapackage_json_and_dataset_csv_spell_an_instant_identically():
    """The twin of metasalmon's B-115 assertion, and B-145's whole point.

    Agreement is asserted rather than a literal, so it survives any later
    ruling that moves the spelling: whichever writer moves next, the two files
    still have to move together. Measured before the fix, 2026-09-16 on pandas
    3.0.5 -- ``datapackage.json`` held ``0999-06-05T13:45:30`` and
    ``2024-12-31T00:00:00`` while ``metadata/dataset.csv`` held
    ``999-06-05 13:45:30`` and ``2024-12-31``, so all four cells differed.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        descriptor, csv = _written_instants(_package_with_typed_instants(tmpdir))

    assert descriptor["start"] == csv["start"]
    assert descriptor["end"] == csv["end"]


def test_a_written_instant_takes_the_ruled_iso_form():
    """The shape Brett ruled: ``T`` separator, ``Z`` zone marker, seconds.

    A shape rather than a literal, so a ruling on the *year* -- hub item
    **B-161** -- can change that digit group without rewriting this test.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        descriptor, csv = _written_instants(_package_with_typed_instants(tmpdir))

    for spelled in (descriptor["start"], descriptor["end"], csv["start"], csv["end"]):
        assert RULED_INSTANT.match(spelled), spelled


def test_a_written_pre_1000_instant_keeps_its_padded_year():
    """The residual hub item **B-161** names, pinned rather than described.

    Deliberately a **literal**, where the two tests above are deliberately not.
    In metasalmon the year is platform-dependent -- ``readr::write_csv()``
    wrote ``999-06-05T13:45:30Z`` on Linux and ``0999-06-05T13:45:30Z`` on
    macOS -- which is why its own test asserts a shape. Here it is padded **by
    construction**: ``resource_types._iso_date`` builds the year with ``%04d``
    and never reaches ``strftime``, which is what the rest of this file exists
    to keep true. So a literal costs nothing in portability and buys the one
    thing a shape cannot: if this path is ever routed back through ``to_csv``
    or ``strftime``, the padding goes and only this assertion notices.

    It is therefore also the measurement B-161 is about, in executable form:
    **Python writes the padded year on every platform; metasalmon writes
    whichever year readr writes.** Those agree on macOS and differ on Linux,
    which is where CI runs. This test asserts what this implementation does and
    rules nothing about what it should do -- B-161 is where that is settled,
    for both implementations at once.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        descriptor, csv = _written_instants(_package_with_typed_instants(tmpdir))

    assert descriptor["start"] == "0999-06-05T13:45:30Z"
    assert csv["start"] == "0999-06-05T13:45:30Z"


def test_an_all_midnight_instant_column_keeps_its_time():
    """pandas' CSV formatter is vector-wise; the ruled spelling is not.

    ``to_csv`` renders a ``datetime64`` column whose every value is midnight as
    a bare date, so ``temporal_end`` was written ``2024-12-31`` while the
    descriptor wrote ``2024-12-31T00:00:00``. One cell's bytes depending on the
    other rows in its column is the ``.ms_sssom_canonical_bytes()`` defect
    ``AGENTS.md`` describes, in pandas.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        descriptor, csv = _written_instants(_package_with_typed_instants(tmpdir))

    assert descriptor["end"] == "2024-12-31T00:00:00Z"
    assert csv["end"] == "2024-12-31T00:00:00Z"


def test_a_date_is_not_an_instant_and_keeps_its_own_spelling():
    """The narrowness B-115 chose, mirrored: only the instant branch moved.

    ``datetime.datetime`` subclasses ``date``, so the dispatch has to be
    written in the direction that leaves a plain date alone. A ``date`` already
    agrees between the two files -- ``date.isoformat()`` and ``str(date)`` are
    the same pure-Python padded string -- and coercing it would change bytes
    the ruling never touched.
    """
    day = _dt.date(999, 6, 5)
    assert package_io._descriptor_temporal_text(day) == "0999-06-05"
    assert (
        package_io._metadata_csv_bytes(pd.DataFrame({"when": [day]}))
        .decode("utf-8")
        .splitlines()[1]
        == "0999-06-05"
    )


# --- the third copy: the EML calendarDate -----------------------------------
#
# Hub item **B-245**, the metasalmonpy half of **B-162**. metasalmon keeps
# B-162's two EML tests beside B-115's writer tests, in
# ``tests/testthat/test-canonical-date-render.R``. The twins of B-115's tests
# are the B-145 section above, so the twins of B-162's follow them here.
#
# ``eml._add_coverage()`` writes ``temporal_start`` and ``temporal_end`` into
# ``calendarDate`` through ``_as_character()``, which reads like a third
# renderer beside the two writers. It is not one, and these two tests check
# that it stays that way. The builder has a single entry,
# ``write_eml_from_sdp(path)``, and it reads the package back from disk as
# text: ``metadata/dataset.csv`` through ``metadata.read_sdp_csv()``, every
# column ``str``, or ``datapackage.json`` when there is no canonical metadata.
# So the value is rendered once, by a writer, and ``_as_character()`` of that
# text is the identity. THE BASELINE IS THE FILE THE EML IS READ FROM.
# ``_written_instants()`` above reads that file as text whatever the value's
# type, so it serves here too.
#
# WHAT THESE DELIBERATELY DO NOT PIN: a typed instant. Its three copies agree
# today (``2024-01-01T00:00:00Z`` in both files and in the ``calendarDate``,
# measured 2026-09-25), but EML 2.2.0's ``calendarDate`` is
# ``xs:gYear | xs:date``, so ``write_eml_from_sdp()`` refuses that document at
# its schema check. An EML that carries an instant has to differ from the CSV
# there, so a byte-agreement pin would make that fix look like a regression.
#
# *Retires when:* the EML stops being built from the package on disk. A builder
# that takes a typed frame is a writer, and has to render through the CSV's
# baseline; the pin then moves to that renderer.

EML_FIXTURE = Path(__file__).resolve().parent / "data" / "eml" / "sdp-default"


def _eml_extra_available() -> bool:
    try:
        import lxml.etree  # noqa: F401
        import yaml  # noqa: F401
    except ImportError:
        return False
    return True


def _eml_package_with_temporal(tmpdir: str, start: object, end: object) -> Path:
    """The EML export fixture, rewritten through the real writer.

    Only ``temporal_start`` and ``temporal_end`` change. The mapping sidecar and
    the two files whose digests it records are not package-managed, so the
    rewrite leaves them as they were and the package still exports.
    """
    target = Path(tmpdir) / "sdp"
    shutil.copytree(EML_FIXTURE, target)
    package = package_io.read_salmon_datapackage(str(target))
    package_io.write_salmon_datapackage(
        resources=package["resources"],
        dataset_meta=package["dataset"].assign(
            temporal_start=[start], temporal_end=[end]
        ),
        table_meta=package["tables"],
        dict_df=package["dictionary"],
        codes=package["codes"],
        path=str(target),
        overwrite=True,
    )
    return target


def _calendar_dates(document: ET.Element) -> list:
    """The begin and end ``calendarDate`` of a coverage or a whole document."""
    return [
        document.find(".//rangeOfDates/beginDate/calendarDate").text,
        document.find(".//rangeOfDates/endDate/calendarDate").text,
    ]


# The skip retires when lxml and PyYAML move from the ``[eml]`` extra into the
# core dependencies, or when ``write_eml_from_sdp()`` stops needing them.
@pytest.mark.skipif(
    not _eml_extra_available(), reason="requires the metasalmonpy[eml] extra"
)
def test_eml_calendar_date_is_the_dataset_csv_spelling_of_a_typed_date():
    """Through the exported builder and its schema check.

    EML can carry a date, so here all three copies agree byte for byte. The
    pre-1000 year is the one whose spelling depends on which renderer ran:
    ``date.isoformat()`` pads it, and glibc's ``strftime`` does not.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        target = _eml_package_with_temporal(
            tmpdir, _dt.date(999, 1, 1), _dt.date(2024, 12, 31)
        )
        result = write_eml_from_sdp(str(target))
        calendar = _calendar_dates(ET.parse(result["path"]).getroot())
        descriptor, csv = _written_instants(target)

    assert result["validation"] is True
    assert calendar == [csv["start"], csv["end"]]
    assert calendar == [descriptor["start"], descriptor["end"]]
    assert calendar == ["0999-01-01", "2024-12-31"]


def test_eml_calendar_date_keeps_the_dataset_csv_spelling_of_short_year_text():
    """Text that a re-rendering EML would change: an unpadded year.

    The exported call stops at its schema check, since ``999-06-05`` is not an
    ``xs:date``, so the coverage is built from the frame
    ``validate_salmon_datapackage()`` returns as ``package``, which is the frame
    ``write_eml_from_sdp()`` hands the builder. That also keeps this half free
    of the ``[eml]`` extra. As in metasalmon, the descriptor is not asserted:
    how a writer spells off-profile text is not this test's question.

    Because ``999-06-05`` is off-profile, a validator that enforces the
    schema's temporal pattern (the subject of hub item **B-205**) would refuse
    it before returning the frame. If that happens, take the frame from
    ``read_salmon_datapackage()``, as metasalmon's twin does: validation
    returns that frame unchanged.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        target = _eml_package_with_temporal(tmpdir, "999-06-05", "2024-12-31")
        package = package_io.validate_salmon_datapackage(
            str(target), require_iris=True
        )["package"]
        coverage = ET.Element("dataset")
        eml._add_coverage(coverage, package["dataset"], {})
        _, csv = _written_instants(target)

    assert _calendar_dates(coverage) == [csv["start"], csv["end"]]
    assert _calendar_dates(coverage)[0] == "999-06-05"
