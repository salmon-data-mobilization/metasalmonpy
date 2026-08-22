"""HTTP-error diagnostics and network robustness (S10 chunk E).

metasalmon 0.2.2: a failed vocabulary lookup is never indistinguishable from a
successful empty one — failures are recorded per source in the diagnostics
frame as ``status="http_error"``, surfaced as a warning, and a degraded lookup
is never written to the result cache. metasalmon 0.2.3: the BioPortal API key
travels in an ``Authorization`` header, and request URLs are redacted at
capture. Expected shapes were verified by driving metasalmon ``main`` (see the
chunk E differential in the PR) over the same scripted failures.
"""

import io
import os
import unittest
import urllib.error
import warnings
from unittest import mock

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None

if pd is None:
    raise unittest.SkipTest("pandas not installed")

import metasalmonpy.term_search as ts
from metasalmonpy import find_terms

_DIAGNOSTIC_COLUMNS = ["source", "query", "status", "count", "elapsed_secs", "error"]


def _rows(role="variable", label="Var A", iri="iri:a"):
    return pd.DataFrame(
        {
            "label": [label],
            "iri": [iri],
            "source": ["ols"],
            "ontology": ["o1"],
            "role": [role],
            "match_type": [""],
            "definition": [""],
        }
    )


class SafeJsonFailureSignallingTests(unittest.TestCase):
    def _collect(self, fn):
        failures = []
        ts._search_failure_sinks.append(failures)
        try:
            result = fn()
        finally:
            ts._search_failure_sinks.pop()
        return result, failures

    def test_http_error_status_is_signalled_not_swallowed(self):
        error = urllib.error.HTTPError(
            "https://example.org/search", 503, "Service Unavailable", {}, io.BytesIO(b"")
        )
        with mock.patch.object(ts.urllib.request, "urlopen", side_effect=error):
            result, failures = self._collect(
                lambda: ts._safe_json("https://example.org/search")
            )
        self.assertIsNone(result)
        self.assertEqual(
            failures,
            ["Vocabulary API request failed: HTTP 503"],
        )

    def test_connection_failure_without_curl_is_signalled(self):
        with mock.patch.object(
            ts.urllib.request, "urlopen", side_effect=OSError("connection refused")
        ), mock.patch.object(ts.shutil, "which", return_value=None):
            result, failures = self._collect(
                lambda: ts._safe_json("https://example.org/search")
            )
        self.assertIsNone(result)
        self.assertEqual(len(failures), 1)
        self.assertIn("connection refused", failures[0])

    def test_the_recorded_url_is_redacted_at_capture(self):
        # metasalmon 0.2.3: this is the one place every source's URL is
        # recorded, so a user-supplied endpoint carrying a key must never
        # reach the failure record raw.
        error = urllib.error.HTTPError(
            "https://example.org/search", 500, "boom", {}, io.BytesIO(b"")
        )
        with mock.patch.object(ts.urllib.request, "urlopen", side_effect=error):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                failures = []
                ts._search_failure_sinks.append(failures)
                try:
                    ts._safe_json("https://example.org/search?apikey=SECRETVALUE")
                finally:
                    ts._search_failure_sinks.pop()
        del caught
        # The HTTP failure detail carries the status; had the URL been quoted
        # it would have been the redacted form. Exercise the redaction
        # directly on the timeout path, which quotes the URL:
        error_408 = urllib.error.HTTPError(
            "https://example.org/search", 408, "timeout", {}, io.BytesIO(b"")
        )
        with mock.patch.object(ts.urllib.request, "urlopen", side_effect=error_408):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                ts._safe_json("https://example.org/search?apikey=SECRETVALUE")
        messages = [str(w.message) for w in caught]
        self.assertTrue(any("timed out" in message for message in messages))
        self.assertFalse(any("SECRETVALUE" in message for message in messages))
        self.assertTrue(any("apikey=[REDACTED]" in message for message in messages))

    def test_outside_find_terms_a_failure_stays_silent(self):
        # Mirror of R's classed signal with no handler installed: quiet.
        error = urllib.error.HTTPError(
            "https://example.org/x", 502, "bad", {}, io.BytesIO(b"")
        )
        with mock.patch.object(ts.urllib.request, "urlopen", side_effect=error):
            self.assertIsNone(ts._safe_json("https://example.org/x"))


class FindTermsDegradedLookupTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("METASALMONPY_CACHE", None)
        self.addCleanup(os.environ.pop, "METASALMONPY_CACHE", None)
        ts._term_cache.clear()
        self.addCleanup(ts._term_cache.clear)

    def test_diagnostics_frame_has_the_r_columns(self):
        with mock.patch.object(ts, "_search_ols", return_value=_rows()):
            res = find_terms("count", role="variable", sources=("ols",))
        self.assertEqual(
            list(res.attrs["diagnostics"].columns), _DIAGNOSTIC_COLUMNS
        )
        row = res.attrs["diagnostics"].iloc[0]
        self.assertEqual(row["status"], "success")
        self.assertIsNone(row["error"])

    def test_failed_side_request_yields_http_error_but_keeps_rows(self):
        # A source that returned rows despite a failed side request is still
        # a partial answer, not a clean success.
        def partial(query, role):
            ts._signal_search_failure("https://example.org/enrich", "HTTP 503")
            return _rows(role)

        with mock.patch.object(ts, "_search_ols", side_effect=partial):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                res = find_terms("count", role="variable", sources=("ols",))
        self.assertEqual(len(res), 1)
        diagnostics = res.attrs["diagnostics"]
        self.assertEqual(diagnostics.iloc[0]["status"], "http_error")
        self.assertEqual(diagnostics.iloc[0]["count"], 1)
        self.assertIn("HTTP 503", diagnostics.iloc[0]["error"])
        messages = [str(w.message) for w in caught]
        self.assertTrue(
            any("Vocabulary lookup was incomplete" in m for m in messages)
        )
        self.assertTrue(any("ontology gap" in m for m in messages))

    def test_multiple_failures_are_joined_with_semicolons(self):
        def doubly_partial(query, role):
            ts._signal_search_failure("https://example.org/a", "HTTP 500")
            ts._signal_search_failure("https://example.org/b", "HTTP 429")
            return ts._empty_terms(role)

        with mock.patch.object(ts, "_search_ols", side_effect=doubly_partial):
            with warnings.catch_warnings(record=True):
                warnings.simplefilter("always")
                res = find_terms("count", role="variable", sources=("ols",))
        error = res.attrs["diagnostics"].iloc[0]["error"]
        self.assertEqual(
            error,
            "Vocabulary API request failed: HTTP 500; "
            "Vocabulary API request failed: HTTP 429",
        )

    def test_a_degraded_lookup_is_never_cached(self):
        os.environ["METASALMONPY_CACHE"] = "1"
        calls = {"n": 0}

        def flaky(query, role):
            calls["n"] += 1
            if calls["n"] == 1:
                ts._signal_search_failure("https://example.org/x", "HTTP 503")
                return ts._empty_terms(role)
            return _rows(role)

        with mock.patch.object(ts, "_search_ols", side_effect=flaky):
            with warnings.catch_warnings(record=True):
                warnings.simplefilter("always")
                first = find_terms("count", role="variable", sources=("ols",))
                second = find_terms("count", role="variable", sources=("ols",))
                third = find_terms("count", role="variable", sources=("ols",))
        # The outage's empty result was not frozen: the second call re-ran
        # the source and its clean answer is what got cached for the third.
        self.assertTrue(first.empty)
        self.assertEqual(len(second), 1)
        self.assertEqual(len(third), 1)
        self.assertEqual(calls["n"], 2)

    def test_a_clean_lookup_is_cached(self):
        os.environ["METASALMONPY_CACHE"] = "1"
        calls = {"n": 0}

        def clean(query, role):
            calls["n"] += 1
            return _rows(role)

        with mock.patch.object(ts, "_search_ols", side_effect=clean):
            find_terms("count", role="variable", sources=("ols",))
            find_terms("count", role="variable", sources=("ols",))
        self.assertEqual(calls["n"], 1)

    def test_source_exception_error_text_is_redacted(self):
        def leaky(query, role):
            raise RuntimeError("upstream said Authorization: Bearer supersecret")

        with mock.patch.object(ts, "_search_ols", side_effect=leaky):
            with warnings.catch_warnings(record=True):
                warnings.simplefilter("always")
                res = find_terms("count", role="variable", sources=("ols",))
        error = res.attrs["diagnostics"].iloc[0]["error"]
        self.assertNotIn("supersecret", error)
        self.assertIn("[REDACTED]", error)

    def test_degraded_warning_names_each_failed_source_once_sorted(self):
        def fail_a(query, role):
            ts._signal_search_failure("https://example.org/a", "HTTP 500")
            return ts._empty_terms(role)

        def fail_b(query, role):
            raise RuntimeError("boom")

        with mock.patch.object(ts, "_search_ols", side_effect=fail_a), \
                mock.patch.object(ts, "_search_nvs", side_effect=fail_b):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                find_terms("count", role="variable", sources=("nvs", "ols"))
        incomplete = [
            str(w.message)
            for w in caught
            if "Vocabulary lookup was incomplete" in str(w.message)
        ]
        self.assertEqual(len(incomplete), 1)
        self.assertIn("'nvs', 'ols'", incomplete[0])


class BioportalHeaderAuthTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(os.environ.pop, "BIOPORTAL_APIKEY", None)
        os.environ["BIOPORTAL_APIKEY"] = "TESTKEY123"

    def test_the_key_travels_in_a_header_not_the_query_string(self):
        captured = {}

        def capture(url, headers=None, timeout=30):
            captured["url"] = url
            captured["headers"] = headers
            return None

        with mock.patch.object(ts, "_safe_json", side_effect=capture):
            ts._search_bioportal("salmon", "entity")
        self.assertEqual(
            captured["url"], "https://data.bioontology.org/search?q=salmon"
        )
        self.assertNotIn("TESTKEY123", captured["url"])
        self.assertEqual(
            captured["headers"], {"Authorization": "apikey token=TESTKEY123"}
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
