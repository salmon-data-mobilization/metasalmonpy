"""The suite tests the checkout it lives in, whatever that directory is called.

Hub queue B-191. ``tests/conftest.py`` describes the route and why it replaced
the old one; this file is the part CI can see. CI checks the repository out
into a directory named ``metasalmonpy``, the one name under which the old route
happened to work, so a CI run of the suite on its own cannot notice the route
breaking. The parametrized test below copies the checkout under two directory
names that broke it and runs pytest inside the copy, which gives CI the view a
hub worktree has.

The two names cover different failures, and each of the conftest's three
measures is pinned by at least one of them. A name that is not a Python
identifier catches the checkout root being set up as a package. A name that is
an identifier catches the root being imported a second time under that name,
which is also what a ``tests/__init__.py`` brings back. Both catch the
conftest's binding going missing, since the copy must import *itself*: in CI
that means winning against the editable install of the real checkout, and with
nothing installed it means importing at all.

*Retires when:* the conftest's measures do, which is when the package moves
into its own ``src/metasalmonpy/`` directory. ``metasalmonpy.__file__`` then
stops being the checkout root's ``__init__.py``, so this file has to be
rewritten then rather than deleted. The property it pins, that the suite tests
the tree it lives in, still holds after the move.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import metasalmonpy

CHECKOUT_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_INIT = CHECKOUT_ROOT / "__init__.py"

INNER_TEST = (
    "tests/test_import_route_guard.py"
    "::test_the_suite_imports_this_checkout_once_as_metasalmonpy"
)


def _file_of(module):
    origin = getattr(module, "__file__", None)
    return Path(origin).resolve() if isinstance(origin, str) else None


def test_the_suite_imports_this_checkout_once_as_metasalmonpy():
    assert _file_of(metasalmonpy) == PACKAGE_INIT, (
        f"metasalmonpy was imported from {metasalmonpy.__file__}, not from the "
        f"checkout these tests live in, {CHECKOUT_ROOT}"
    )
    names = sorted(
        name
        for name, module in list(sys.modules.items())
        if _file_of(module) == PACKAGE_INIT
    )
    assert names == ["metasalmonpy"], (
        f"the checkout root is imported as {names}; any name besides "
        f"metasalmonpy is one pytest derived from the checkout directory"
    )


@pytest.mark.parametrize(
    "directory_name",
    [
        # The hub worktree key's shape. It is not a Python identifier, so
        # pytest 8 and later imported the root as a bare ``__init__`` module
        # and every test errored at setup.
        "salmon-data-mobilization-metasalmonpy-B-191",
        # An identifier that is not the package's name. pytest imported the
        # root a second time under it, and ``import metasalmonpy`` found
        # whatever was installed, so the run went green against another tree.
        "checkout",
    ],
)
def test_a_copy_under_another_directory_name_tests_itself(tmp_path, directory_name):
    copy = tmp_path / directory_name
    shutil.copytree(
        CHECKOUT_ROOT,
        copy,
        ignore=shutil.ignore_patterns(
            ".*", "__pycache__", "*.egg-info", "build", "dist", "_site", "venv"
        ),
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", INNER_TEST],
        cwd=copy,
        capture_output=True,
        text=True,
        timeout=300,
    )
    report = f"exit {result.returncode}\n{result.stdout}\n{result.stderr}"
    assert result.returncode == 0, (
        f"pytest in a copy of this checkout named {directory_name!r}: {report}"
    )
    # A zero exit alone would also accept a run that skipped the one test.
    assert "1 passed" in result.stdout, report
