"""Call-time environment reads and the METASALMONPY_ prefix (S10 chunk E).

metasalmon 0.2.2 made ``METASALMON_CACHE`` a call-time read: as a top-level
binding it was evaluated when the namespace was built, so an installed package
captured the build machine's environment and the result cache could never be
enabled by a user. This package had the same bug class — ``SALMONPY_CACHE``
was read at import — plus the stale ``SALMONPY_`` prefix. Both are fixed
together here: the switch is a function, the prefix is ``METASALMONPY_``, and
the old spelling warns through a deprecation window (removed in the first
release after the S10 parity release).
"""

import os
import unittest
import warnings
from unittest import mock

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None

if pd is None:
    raise unittest.SkipTest("pandas not installed")

import metasalmonpy.term_search as ts


class CallTimeEnvReadTests(unittest.TestCase):
    def setUp(self):
        for name in (
            "METASALMONPY_CACHE",
            "SALMONPY_CACHE",
            "METASALMONPY_DEBUG_FETCH",
            "SALMONPY_DEBUG_FETCH",
        ):
            os.environ.pop(name, None)
            self.addCleanup(os.environ.pop, name, None)
        ts._legacy_env_warned.clear()
        self.addCleanup(ts._legacy_env_warned.clear)

    def test_cache_switch_is_read_at_call_time(self):
        # Import happened long ago; flipping the environment now must be
        # heard immediately — the exact property the R 0.2.2 fix restored.
        self.assertFalse(ts._cache_enabled())
        os.environ["METASALMONPY_CACHE"] = "1"
        self.assertTrue(ts._cache_enabled())
        os.environ["METASALMONPY_CACHE"] = "0"
        self.assertFalse(ts._cache_enabled())

    def test_truthy_value_set_matches_r(self):
        # Mirror of `.metasalmon_cache_enabled()`: tolower(value) %in%
        # c("1", "true", "yes"); unset and empty are both off. Verified
        # value-for-value against metasalmon main (see the chunk E
        # differential in the PR).
        expectations = {
            "1": True,
            "true": True,
            "TRUE": True,
            "yes": True,
            "YES": True,
            "Yes": True,
            "0": False,
            "false": False,
            "no": False,
            "on": False,
            "enabled": False,
            " 1": False,
            "": False,
        }
        for value, expected in expectations.items():
            os.environ["METASALMONPY_CACHE"] = value
            self.assertEqual(
                ts._cache_enabled(), expected, f"value {value!r}"
            )

    def test_legacy_spelling_still_works_and_warns_once(self):
        os.environ["SALMONPY_CACHE"] = "1"
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.assertTrue(ts._cache_enabled())
            self.assertTrue(ts._cache_enabled())
        deprecations = [
            w for w in caught if issubclass(w.category, DeprecationWarning)
        ]
        self.assertEqual(len(deprecations), 1)
        self.assertIn("SALMONPY_CACHE is deprecated", str(deprecations[0].message))
        self.assertIn("METASALMONPY_CACHE", str(deprecations[0].message))

    def test_current_spelling_wins_over_legacy_without_warning(self):
        os.environ["METASALMONPY_CACHE"] = "0"
        os.environ["SALMONPY_CACHE"] = "1"
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.assertFalse(ts._cache_enabled())
        self.assertEqual(
            [w for w in caught if issubclass(w.category, DeprecationWarning)],
            [],
        )

    def test_debug_fetch_reads_the_renamed_variable_at_call_time(self):
        self.assertFalse(ts._debug_fetch_enabled())
        os.environ["METASALMONPY_DEBUG_FETCH"] = "yes"
        self.assertTrue(ts._debug_fetch_enabled())

    def test_no_import_time_environment_capture_remains(self):
        # The regression this file exists for: a module-level binding named
        # `_cache_enabled` evaluated os.getenv at import. The switch must be
        # callable state, not captured state.
        self.assertTrue(callable(ts._cache_enabled))
        source_path = ts.__file__
        with open(source_path, "r", encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn('os.getenv("SALMONPY_CACHE"', source)
        self.assertNotIn('os.getenv("SALMONPY_DEBUG_FETCH"', source)


class FindTermsCacheSwitchTests(unittest.TestCase):
    def setUp(self):
        for name in ("METASALMONPY_CACHE", "SALMONPY_CACHE"):
            os.environ.pop(name, None)
            self.addCleanup(os.environ.pop, name, None)
        ts._term_cache.clear()
        self.addCleanup(ts._term_cache.clear)

    def _fake_ols(self):
        calls = {"n": 0}

        def fake(query, role):
            calls["n"] += 1
            return pd.DataFrame(
                {
                    "label": ["Var A"],
                    "iri": ["iri:ols"],
                    "source": ["ols"],
                    "ontology": ["o1"],
                    "role": [role],
                    "match_type": [""],
                    "definition": [""],
                }
            )

        return fake, calls

    def test_cache_enabled_mid_session_is_honoured(self):
        fake, calls = self._fake_ols()
        with mock.patch.object(ts, "_search_ols", side_effect=fake):
            ts.find_terms("count", role="variable", sources=("ols",))
            self.assertEqual(calls["n"], 1)
            # Enable the cache *after* import and after a first call: the
            # second call populates it, the third is served from it.
            os.environ["METASALMONPY_CACHE"] = "1"
            ts.find_terms("count", role="variable", sources=("ols",))
            self.assertEqual(calls["n"], 2)
            ts.find_terms("count", role="variable", sources=("ols",))
            self.assertEqual(calls["n"], 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
