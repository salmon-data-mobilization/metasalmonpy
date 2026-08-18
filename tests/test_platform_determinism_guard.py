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
from pathlib import Path

from metasalmonpy import resource_types as rt

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
