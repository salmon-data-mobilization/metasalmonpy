"""One owner for accepted manifest-writer provenance.

Mirrors metasalmon's ``tests/testthat/test-provenance.R`` at v0.4.0.

The honest-provenance ruling — every validator accepts a manifest written by
either mirror implementation — was applied one artifact at a time, and each
application re-typed the same pair of writer literals. On the R side that is
how it got applied twice out of three times: the reproducibility validator
kept rejecting Python-written manifests until 0.4.0 (hub backlog #88), and
because ``publish_sdp_to_knb()`` validates the manifest while planning, that
also blocked KNB publication of any Python-written SDP.

The structural guard at the bottom is the part that stops a fourth manifest
type repeating it.
"""

import ast
import inspect
import textwrap
import unittest

from metasalmonpy import (
    measurement_decompositions,
    provenance,
    reproducibility,
    sssom,
)


class AcceptedWriterTests(unittest.TestCase):
    def test_both_implementations_are_accepted_for_every_writer(self):
        for writer in (
            "write_sdp_sssom",
            "write_sdp_measurement_decompositions",
            "write_sdp_reproducibility_manifest",
        ):
            accepted = provenance.accepted_writers(writer)
            self.assertEqual(
                accepted,
                {
                    "metasalmon::" + writer: "metasalmon_version",
                    "metasalmonpy." + writer: "metasalmonpy_version",
                },
            )

    def test_the_version_field_follows_the_declared_writer(self):
        self.assertEqual(
            provenance.version_field(
                {"generated_by": "metasalmon::write_sdp_sssom"},
                "write_sdp_sssom",
            ),
            "metasalmon_version",
        )
        self.assertEqual(
            provenance.version_field(
                {"generated_by": "metasalmonpy.write_sdp_sssom"},
                "write_sdp_sssom",
            ),
            "metasalmonpy_version",
        )

    def test_an_absent_or_foreign_writer_has_no_version_field(self):
        for block in (
            None,
            "metasalmon::write_sdp_sssom",
            [],
            {},
            {"generated_by": None},
            {"generated_by": 7},
            {"generated_by": ["metasalmon::write_sdp_sssom"]},
            {"generated_by": "someoneelse.write_sdp_sssom"},
            # The right shape for a *different* manifest type is still wrong
            # for this one.
            {"generated_by": "metasalmon::write_sdp_reproducibility_manifest"},
        ):
            self.assertIsNone(
                provenance.version_field(block, "write_sdp_sssom"), msg=repr(block)
            )

    def test_an_accepted_version_value_is_one_non_blank_string(self):
        self.assertTrue(provenance.version_ok("0.4.0"))
        self.assertTrue(provenance.version_ok("development"))
        for value in (
            None,
            "",
            "   ",
            "\t\n",
            1.8,
            True,
            0,
            ["0.4.0"],
            {"version": "0.4.0"},
        ):
            self.assertFalse(provenance.version_ok(value), msg=repr(value))


class StructuralOwnershipGuard(unittest.TestCase):
    """No manifest validator may re-type the accepted writer strings.

    *Retires when:* nothing — it is a permanent structural check. Add the new
    validator to ``VALIDATORS`` below when a manifest type is added; exempt one
    only by recording here why that manifest cannot be written by the mirror.
    """

    VALIDATORS = {
        "sssom._validate_manifest": sssom._validate_manifest,
        "measurement_decompositions._validate_manifest": (
            measurement_decompositions._validate_manifest
        ),
    }

    def setUp(self):
        # Discovered rather than named, so a rename shows up here as a failure
        # instead of as a silently skipped module. ``reproducibility`` is the
        # validator #88 was about, so its absence must not be quiet.
        self.reproducibility_validators = {
            name: getattr(reproducibility, name)
            for name in dir(reproducibility)
            if name.startswith("_validate") and "manifest" in name
        }
        self.assertTrue(
            self.reproducibility_validators,
            "reproducibility.py has no manifest validator to guard",
        )

    @staticmethod
    def _body(function):
        """The function body without its docstring.

        R's guard reads ``deparse(body(f))``, which has no docstring in it.
        The Python equivalent has to drop the docstring explicitly, or a
        validator that merely *documents* which writers it accepts fails a
        guard that is about where the strings are **resolved**.
        """
        source = inspect.getsource(function)
        tree = ast.parse(textwrap.dedent(source)).body[0]
        body = tree.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body = body[1:]
        return "\n".join(ast.unparse(node) for node in body)

    def _check(self, name, function):
        source = self._body(function)
        self.assertIn(
            "_provenance.version_field",
            source,
            msg=f"{name} must resolve its accepted writers via provenance.py",
        )
        for literal in ("metasalmonpy.write_sdp", "metasalmon::write_sdp"):
            self.assertNotIn(
                literal,
                source,
                msg=f"{name} re-types a writer literal instead of sharing it",
            )

    def test_no_manifest_validator_re_types_the_accepted_writer_strings(self):
        for name, function in self.VALIDATORS.items():
            if function is None:
                continue
            self._check(name, function)
        checked = 0
        for name, function in self.reproducibility_validators.items():
            if "provenance" not in self._body(function):
                # A validator that never reads provenance has nothing to share.
                continue
            self._check("reproducibility." + name, function)
            checked += 1
        self.assertEqual(
            checked,
            1,
            "exactly one reproducibility validator should read provenance; "
            "backlog #88 was the one that did not",
        )

    def test_the_module_tables_are_gone(self):
        # The three ``_ACCEPTED_PROVENANCE`` dicts this consolidation replaced.
        # A module that grows one back has a second copy of the ruling.
        for module in (sssom, measurement_decompositions, reproducibility):
            self.assertFalse(
                hasattr(module, "_ACCEPTED_PROVENANCE"),
                msg=f"{module.__name__} re-grew a local accepted-writer table",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
