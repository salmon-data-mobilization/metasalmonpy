"""Test-suite defaults, and the route by which the suite imports the package.

**The suite imports the checkout it lives in, whatever that directory is
called** (hub queue B-191). The repository root *is* the package -- a flat
layout, packaged as ``metasalmonpy`` by ``pyproject.toml``'s ``package-dir``
mapping -- so the directory a checkout sits in is not the package's name, and
nothing guarantees that it matches. The suite used to depend on it matching,
because pytest named the root package after that directory. Measured on
2026-09-23 against ``3f8349a``:

* a name that is not a Python identifier, which every hub worktree
  (``<owner>-<repo>-<id>``) is, made pytest 8 and later import the root as a
  bare ``__init__`` module, and **every test errored at setup**;
* a name that is an identifier but not ``metasalmonpy`` imported the package a
  second time under that name, while ``import metasalmonpy`` found whatever
  copy was *installed*: **a green run against a different tree**;
* with nothing installed, only a directory named ``metasalmonpy`` imported at
  all.

CI never saw any of this, because ``actions/checkout`` names the directory
after the repository. Three things now keep the directory's name out of the
route:

1. ``_bind_metasalmonpy_to_this_checkout()`` below binds ``metasalmonpy`` to
   this checkout by file location, before anything imports it, so an editable
   install pointing at some other checkout cannot substitute itself.
2. ``_CheckoutRootIsADirectory`` collects the checkout root as a plain
   directory, so pytest never imports its ``__init__.py`` under a name of its
   own. pytest 7 has neither the hook this needs nor the failure it prevents:
   it does not set up the root as a package for a test beneath it, and 7.4.4
   passed from a hub worktree. So the plugin is registered only where
   ``pytest.Dir`` exists.
3. ``tests/`` is deliberately **not** a package. An ``__init__.py`` here makes
   pytest name every test module after the checkout directory again.

``tests/test_import_route_guard.py`` runs a copy of this checkout under two
directory names that used to break, which is the only way a CI checkout, whose
directory name is always right, can report a regression here. *Retires when:*
the package moves into its own ``src/metasalmonpy/`` directory, so the checkout
root stops being a package and there is no directory name left for pytest to
derive a module name from.

**The schema bundle is pinned to its vendored copy.** The loader is
remote-first (``sdp_schema.load_sdp_schema``), so without the pin the suite
would make nine HTTP requests on the first write and behave differently on a
machine with no network. metasalmon's own suite pins
``metasalmon.sdp_schema_source = "vendored"`` for exactly this reason, and
0.2.0's NEWS records the gap that pin left: nothing had ever exercised a
successful remote fetch. ``tests/test_sdp_schema.py`` closes it here with an
injected fetcher, so the pin below costs no coverage.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

CHECKOUT_ROOT = Path(__file__).resolve().parent.parent
_PACKAGE_INIT = CHECKOUT_ROOT / "__init__.py"


def _bind_metasalmonpy_to_this_checkout() -> None:
    loaded = sys.modules.get("metasalmonpy")
    if loaded is not None:
        origin = getattr(loaded, "__file__", None)
        if origin is None or Path(origin).resolve() != _PACKAGE_INIT:
            raise ImportError(
                f"metasalmonpy was already imported from {origin!r} when this "
                f"suite's conftest loaded, and these tests must run against the "
                f"checkout they live in, {CHECKOUT_ROOT}"
            )
        return
    spec = importlib.util.spec_from_file_location(
        "metasalmonpy",
        str(_PACKAGE_INIT),
        submodule_search_locations=[str(CHECKOUT_ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["metasalmonpy"] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules["metasalmonpy"]
        raise


_bind_metasalmonpy_to_this_checkout()

from metasalmonpy import sdp_schema, term_search  # noqa: E402  (after the binding)


class _CheckoutRootIsADirectory:
    """Collect the checkout root as ``pytest.Dir`` rather than ``pytest.Package``.

    A ``Package`` imports its ``__init__.py`` when a test beneath it is set up,
    under a module name pytest derives from the directory. Registered as a
    plugin rather than written as a hook in this file, because a conftest's
    hooks apply only below its own directory, and the root is above it.
    """

    @pytest.hookimpl(tryfirst=True)
    def pytest_collect_directory(self, path, parent):
        if path.resolve() == CHECKOUT_ROOT:
            return pytest.Dir.from_parent(parent, path=path)
        return None


def pytest_configure(config):
    if hasattr(pytest, "Dir"):  # pytest >= 8; see point 2 of the docstring
        config.pluginmanager.register(
            _CheckoutRootIsADirectory(), "metasalmonpy-checkout-root"
        )


@pytest.fixture(autouse=True)
def _pin_vendored_schema_bundle():
    sdp_schema.set_sdp_schema_source("vendored")
    yield
    sdp_schema.set_sdp_schema_source(None)


@pytest.fixture(autouse=True)
def _reset_term_search_session_caches():
    """The 0.2.2 session caches are per-process state; tests are not sessions.

    Without this, the first test to resolve an smn/gcdfo index (usually
    through a monkeypatched fetch) would feed its fixture index to every
    later test, and a cached ``find_terms`` result would cross test
    boundaries whenever a test enables ``METASALMONPY_CACHE``.
    """
    term_search._smn_index_cache.clear()
    term_search._gcdfo_index_cache.clear()
    term_search._term_cache.clear()
    yield
    term_search._smn_index_cache.clear()
    term_search._gcdfo_index_cache.clear()
    term_search._term_cache.clear()
