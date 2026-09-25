import io
import subprocess
import unittest
import urllib.error
import warnings
from http.client import responses
from unittest import mock

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None

if pd is None:
    raise unittest.SkipTest("pandas not installed")

import metasalmonpy.ices_vocab as iv
import metasalmonpy.term_search as ts


class IcesVocabTests(unittest.TestCase):
    def test_ices_code_types(self):
        fake = [
            {"key": "Gear", "description": "Gear Type Codes", "guid": "g1"},
            {"key": "TS_Sex", "description": "Sex Codes (Fisheries)", "guid": "g2"},
        ]
        with mock.patch.object(iv, "_safe_json", return_value=fake):
            df = iv.ices_code_types()
        self.assertFalse(df.empty)
        self.assertIn("key", df.columns)

        with mock.patch.object(iv, "_safe_json", return_value=fake):
            filtered = iv.ices_find_code_types("gear")
        self.assertEqual(filtered.iloc[0]["key"], "Gear")

    def test_ices_codes(self):
        fake = [
            {"key": "BOT", "description": "Bottom Trawl"},
            {"key": "BMT", "description": "Beam trawl"},
        ]
        with mock.patch.object(iv, "_safe_json", return_value=fake):
            df = iv.ices_codes("Gear")
        self.assertFalse(df.empty)
        self.assertEqual(df.iloc[0]["code_type"], "Gear")
        self.assertIn("/CodeDetail/Gear/BOT", df.iloc[0]["url"])

        with mock.patch.object(iv, "_safe_json", return_value=fake):
            filtered = iv.ices_find_codes("beam", "Gear")
        self.assertEqual(filtered.iloc[0]["key"], "BMT")


# A request that failed and an answer with no rows used to reach the caller as
# the same empty DataFrame, with no warning, so an outage read as ICES saying it
# holds no such codes (hub item B-378, the mirror half of metasalmon's B-377).
# These tests mock the request itself before any helper is called: urlopen,
# and the curl fallback _safe_json() takes when urlopen cannot connect. So
# _safe_json() classifies each failure as it would against the service, and
# nothing opens a connection. Each mock records the URL it was asked for, and
# every test checks that record, so a mock that stopped intercepting would fail
# here rather than go to ICES.


def _refused(url):
    """A refused connection, as urlopen reports one."""
    raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))


class _Response:
    """What urlopen returns for a 2xx answer."""

    def __init__(self, status, body):
        self.status = status
        self._body = body.encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _answer(status, body):
    """An HTTP answer with ``status`` and ``body``, in the shape urlopen gives
    it: a response for a 2xx status, and an HTTPError raised for an error."""

    def respond(url):
        if status >= 400:
            raise urllib.error.HTTPError(
                url, status, responses.get(status, ""), {}, io.BytesIO(body.encode("utf-8"))
            )
        return _Response(status, body)

    return respond


# Each helper, and the one request it makes.
_HELPERS = {
    "ices_code_types()": (
        lambda: iv.ices_code_types(),
        "https://vocab.ices.dk/services/api/CodeType",
    ),
    "ices_codes()": (
        lambda: iv.ices_codes("Gear"),
        "https://vocab.ices.dk/services/api/Code/Gear",
    ),
    "ices_find_code_types()": (
        lambda: iv.ices_find_code_types("gear"),
        "https://vocab.ices.dk/services/api/CodeType",
    ),
    "ices_find_codes()": (
        lambda: iv.ices_find_codes("beam", "Gear"),
        "https://vocab.ices.dk/services/api/Code/Gear",
    ),
}


def _run(helper, respond, curl_on_path=True):
    """Run one helper with ``respond`` standing in for the request.

    Returns what it returned, the warnings it gave, and the URLs it asked for,
    urlopen's and curl's alike. ``curl_on_path`` decides whether _safe_json()
    finds curl for its fallback. The fallback is mocked either way, and the
    mock fails to connect, as curl does against a host that refuses.
    """
    requested = []

    def urlopen(request, timeout=None):
        requested.append(request.full_url)
        return respond(request.full_url)

    def check_output(cmd, timeout=None):
        requested.append(next(arg for arg in cmd if arg.startswith(("http://", "https://"))))
        # curl's exit status 7: it failed to connect to the host.
        raise subprocess.CalledProcessError(7, cmd)

    which = "/usr/bin/curl" if curl_on_path else None
    with mock.patch.object(ts.urllib.request, "urlopen", urlopen), mock.patch.object(
        ts.shutil, "which", return_value=which
    ), mock.patch.object(ts.subprocess, "check_output", check_output), warnings.catch_warnings(
        record=True
    ) as caught:
        warnings.simplefilter("always")
        result = helper()
    return result, caught, requested


class IcesFailedRequestTests(unittest.TestCase):
    def assert_empty_frame(self, result):
        self.assertIsInstance(result, pd.DataFrame)
        self.assertEqual(len(result), 0)

    def assert_one_failure_warning(self, caught, request):
        self.assertEqual(len(caught), 1, [str(w.message) for w in caught])
        self.assertTrue(issubclass(caught[0].category, RuntimeWarning))
        message = str(caught[0].message)
        self.assertIn(request, message)
        return message

    def test_a_refused_connection_warns_naming_the_request(self):
        for name, (helper, request) in _HELPERS.items():
            with self.subTest(helper=name):
                result, caught, requested = _run(helper, _refused, curl_on_path=False)
                self.assertEqual(requested, [request])
                message = self.assert_one_failure_warning(caught, request)
                self.assertIn("Connection refused", message)
                self.assert_empty_frame(result)

    def test_a_refused_connection_warns_once_when_curl_is_refused_too(self):
        # With curl on PATH, _safe_json() asks again through curl before it
        # gives up, so the request is made twice and still warns once.
        for name, (helper, request) in _HELPERS.items():
            with self.subTest(helper=name):
                result, caught, requested = _run(helper, _refused)
                self.assertEqual(requested, [request, request])
                message = self.assert_one_failure_warning(caught, request)
                self.assertIn("Connection refused", message)
                self.assert_empty_frame(result)

    def test_an_http_503_answer_warns_naming_the_request(self):
        for name, (helper, request) in _HELPERS.items():
            with self.subTest(helper=name):
                result, caught, requested = _run(
                    helper, _answer(503, "<html>Service Unavailable</html>")
                )
                self.assertEqual(requested, [request])
                message = self.assert_one_failure_warning(caught, request)
                self.assertIn("HTTP 503", message)
                self.assert_empty_frame(result)

    def test_an_answer_of_no_rows_gives_the_empty_result_with_no_warning(self):
        for name, (helper, request) in _HELPERS.items():
            with self.subTest(helper=name):
                result, caught, requested = _run(helper, _answer(200, "[]"))
                self.assertEqual(requested, [request])
                self.assertEqual([str(w.message) for w in caught], [])
                self.assert_empty_frame(result)

    def test_a_failure_recorded_for_another_call_never_replaces_an_answer(self):
        # The failure sinks are one stack for the process, so a call on another
        # thread can record its failure in this call's sink. Simulated here by
        # recording one while this call's own request answers with rows.
        rows = [{"key": "BMT", "description": "Beam trawl"}]

        def answered_while_another_call_failed(url, headers=None, timeout=30):
            ts._signal_search_failure("https://example.org/another-call", "HTTP 503")
            return rows

        with mock.patch.object(
            iv, "_safe_json", answered_while_another_call_failed
        ), warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = iv.ices_codes("Gear")
        self.assertEqual([str(w.message) for w in caught], [])
        self.assertEqual(list(result["key"]), ["BMT"])

    def test_the_warning_names_what_metasalmon_names_in_its_order(self):
        # metasalmon's cli warning, measured on its main at 14f6d4f, is a
        # heading and three bullets: the request, the failure, and what the
        # empty result does not say. Here each is one line.
        _, caught, _ = _run(_HELPERS["ices_codes()"][0], _answer(503, "Service Unavailable"))
        self.assertEqual(
            [str(w.message) for w in caught],
            [
                "The ICES vocabulary request failed, so the result is empty.\n"
                "Request: https://vocab.ices.dk/services/api/Code/Gear\n"
                "Failure: HTTP 503\n"
                "This empty result says nothing about what ICES holds. "
                "An answer with no rows gives no warning."
            ],
        )

    def test_the_warning_redacts_a_secret_in_the_request_and_in_the_failure(self):
        # The ICES API takes no key, so the request is given one through the
        # base URL: what is pinned is that whatever the request carries is
        # redacted.
        def leaky_refusal(url):
            raise urllib.error.URLError(
                "Failed to connect to proxy.example port 3128: "
                "Proxy-Authorization: Basic dXNlcjpzM2NyM3Q="
            )

        secret_base = "https://vocab.ices.dk/services/api?api_key=s3cr3t-in-the-request"
        cases = [
            ("leaky refusal, curl absent", leaky_refusal, False),
            ("leaky refusal, curl present", leaky_refusal, True),
            ("HTTP 503", _answer(503, "Service Unavailable"), True),
        ]
        for label, respond, curl_on_path in cases:
            with self.subTest(case=label):
                with mock.patch.object(iv, "ICES_BASE_URL", secret_base):
                    result, caught, requested = _run(
                        _HELPERS["ices_codes()"][0], respond, curl_on_path=curl_on_path
                    )
                self.assertEqual(requested[0], secret_base + "/Code/Gear")
                message = self.assert_one_failure_warning(
                    caught, "https://vocab.ices.dk/services/api?api_key=[REDACTED]"
                )
                self.assertNotIn("s3cr3t-in-the-request", message)
                self.assertNotIn("dXNlcjpzM2NyM3Q=", message)
                self.assert_empty_frame(result)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

