"""Provider retry with Retry-After (metasalmon 0.2.3, S10 chunk E).

R's ``.ms_llm_retry_limit()`` returned 1 attempt for everything except two
special-cased models, so ``attempt >= attempts`` was true on the first pass
and the retryable-error classifier was never consulted — a 429 or a 503
failed the whole review on the first try, after the user had already paid for
every preceding request. This package had no retry at all, which is the same
observable behaviour. The expected values below (limits, the retryable set,
the HTTP-date epoch, the cap, the backoff bounds) were verified against
metasalmon ``main`` — see the chunk E differential in the PR.
"""

import types
import unittest
from unittest import mock

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None

if pd is None:
    raise unittest.SkipTest("pandas not installed")

import requests

import metasalmonpy.llm_review as lr


def _config(provider="openai", model="gpt-5-mini", request_fn=None):
    return {
        "provider": provider,
        "model": model,
        "base_url": "https://example.invalid/v1",
        "api_key": "test",
        "reasoning_effort": None,
        "timeout_seconds": 5,
        "request_fn": request_fn,
    }


def _http_error(status, retry_after=None):
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    response = types.SimpleNamespace(status_code=status, headers=headers)
    error = requests.HTTPError(f"{status} error")
    error.response = response
    return error


class RetryLimitTests(unittest.TestCase):
    def test_total_attempts_mirror_r(self):
        # `.ms_llm_retry_limit()` — total attempts, not retries.
        self.assertEqual(lr._retry_limit(_config("openai", "gpt-5-mini")), 3)
        self.assertEqual(lr._retry_limit(_config("chapi", "ollama2.mistral:7b")), 3)
        self.assertEqual(
            lr._retry_limit(_config("openrouter", "openrouter/free")), 4
        )
        self.assertEqual(
            lr._retry_limit(_config("openrouter", "meta-llama/llama-3:free")), 4
        )
        self.assertEqual(lr._retry_limit(_config("openrouter", "gpt-4o")), 3)
        self.assertEqual(lr._retry_limit(_config("chapi", "gpt-oss:120b")), 4)
        self.assertEqual(lr._retry_limit(_config("chapi", "gpt-oss")), 4)
        self.assertEqual(lr._retry_limit(_config("chapi", "gpt-oss-x")), 3)


class RetryableClassifierTests(unittest.TestCase):
    def test_message_patterns_mirror_r(self):
        # The same strings `.ms_llm_is_retryable_error()` classifies, with
        # the same verdicts.
        retryable = [
            "Timeout was reached: connection",
            "operation timed out after 5s",
            "HTTP 408 Request Timeout",
            "HTTP 429 Too Many Requests",
            "HTTP 500 Internal Server Error",
            "HTTP 502 Bad Gateway",
            "HTTP 503 Service Unavailable",
            "HTTP 504 Gateway Timeout",
            "model temporarily unavailable",
            "Connection reset by peer",
            "Empty reply from server",
            "Failed to perform HTTP request",
        ]
        not_retryable = [
            "HTTP 401 Unauthorized",
            "HTTP 400 Bad Request",
            "LLM response was not a JSON object.",
            "provider unavailable",
            "the LLM must not be called",
        ]
        for message in retryable:
            self.assertTrue(
                lr._is_retryable_error(RuntimeError(message)), message
            )
        for message in not_retryable:
            self.assertFalse(
                lr._is_retryable_error(RuntimeError(message)), message
            )

    def test_requests_native_errors_classify_by_status_and_type(self):
        # requests spells a 429 "429 Client Error: ...", which the message
        # patterns alone would miss; the attached response carries the truth.
        for status in (408, 429, 500, 502, 503, 504):
            self.assertTrue(lr._is_retryable_error(_http_error(status)), status)
        for status in (400, 401, 403, 404, 422):
            self.assertFalse(lr._is_retryable_error(_http_error(status)), status)
        self.assertTrue(lr._is_retryable_error(requests.ConnectTimeout("t")))
        self.assertTrue(lr._is_retryable_error(requests.ConnectionError("c")))


class HttpDateTests(unittest.TestCase):
    def test_imf_fixdate_parses_to_the_exact_epoch(self):
        parsed = lr._parse_http_date("Wed, 21 Oct 2015 07:28:00 GMT")
        self.assertIsNotNone(parsed)
        # Epoch value pinned against R's ISOdatetime(..., tz = "GMT").
        self.assertEqual(parsed.timestamp(), 1445412480.0)

    def test_obsolete_formats_fall_back_to_backoff(self):
        # Only IMF-fixdate is handled — RFC 7231's required sender form; the
        # two obsolete forms return None and the caller uses bounded backoff.
        self.assertIsNone(lr._parse_http_date("Wednesday, 21-Oct-15 07:28:00 GMT"))
        self.assertIsNone(lr._parse_http_date("Wed Oct 21 07:28:00 2015"))
        self.assertIsNone(lr._parse_http_date("21 Oct 2015 07:28:00 GMT"))
        self.assertIsNone(lr._parse_http_date("Wed, 21 Foo 2015 07:28:00 GMT"))
        self.assertIsNone(lr._parse_http_date(""))


class RetryWaitTests(unittest.TestCase):
    def test_delta_seconds_is_honoured_and_capped(self):
        self.assertEqual(
            lr._retry_wait_seconds(_http_error(429, retry_after="2"), attempt=1),
            2.0,
        )
        # Capped at 60: a provider asking for a multi-minute wait should fail
        # the call so the caller can decide.
        self.assertEqual(
            lr._retry_wait_seconds(_http_error(429, retry_after="300"), attempt=1),
            60.0,
        )

    def test_http_date_form_is_honoured(self):
        # The stamp is built by hand (not strftime) so the test itself cannot
        # go locale-dependent — the exact hazard the parser exists to avoid.
        moment = lr.datetime.fromtimestamp(
            lr.datetime.now(lr.timezone.utc).timestamp() + 30, lr.timezone.utc
        )
        weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        months = [
            "Jan", "Feb", "Mar", "Apr", "May", "Jun",
            "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
        ]
        stamp = (
            f"{weekdays[moment.weekday()]}, {moment.day:02d} "
            f"{months[moment.month - 1]} {moment.year:04d} "
            f"{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d} GMT"
        )
        wait = lr._retry_wait_seconds(_http_error(429, retry_after=stamp), attempt=1)
        self.assertGreater(wait, 20)
        self.assertLessEqual(wait, 30.5)

    def test_a_past_http_date_waits_zero_not_negative(self):
        wait = lr._retry_wait_seconds(
            _http_error(429, retry_after="Wed, 21 Oct 2015 07:28:00 GMT"),
            attempt=1,
        )
        self.assertEqual(wait, 0.0)

    def test_backoff_is_exponential_with_jitter(self):
        # Without Retry-After: min(60, 0.5 * 2^(attempt-1)) plus jitter in
        # [0, backoff/2). Jitter matters — a batch that hits one rate limit
        # must not retry in lockstep.
        error = RuntimeError("HTTP 503")
        for attempt, base in ((1, 0.5), (2, 1.0), (3, 2.0), (8, 60.0)):
            for _ in range(5):
                wait = lr._retry_wait_seconds(error, attempt=attempt)
                self.assertGreaterEqual(wait, base)
                self.assertLessEqual(wait, base * 1.5)


class RequestWithRetriesTests(unittest.TestCase):
    def test_success_needs_one_attempt_and_no_sleep(self):
        calls = []

        def ok(messages, config):
            calls.append(1)
            return {"decision": "review"}

        with mock.patch.object(lr, "_sleep") as slept:
            result = lr._request_json_with_retries(
                [{"role": "user", "content": "x"}], _config(request_fn=ok)
            )
        self.assertEqual(result, {"decision": "review"})
        self.assertEqual(len(calls), 1)
        slept.assert_not_called()

    def test_retryable_failure_retries_then_succeeds(self):
        calls = []

        def flaky(messages, config):
            calls.append(1)
            if len(calls) < 3:
                raise _http_error(429, retry_after="1")
            return {"decision": "accept"}

        with mock.patch.object(lr, "_sleep") as slept:
            result = lr._request_json_with_retries(
                [{"role": "user", "content": "x"}], _config(request_fn=flaky)
            )
        self.assertEqual(result, {"decision": "accept"})
        self.assertEqual(len(calls), 3)
        self.assertEqual(
            [call.args[0] for call in slept.call_args_list], [1.0, 1.0]
        )

    def test_a_sentinel_request_fn_is_called_exactly_once(self):
        # The repo-wide test hook: a raising request_fn proves the LLM was
        # not called more than asked. A non-retryable error must never be
        # retried into a second (paid) provider call.
        calls = []

        def sentinel(messages, config):
            calls.append(1)
            raise RuntimeError("the LLM must not be called")

        with mock.patch.object(lr, "_sleep") as slept:
            with self.assertRaisesRegex(RuntimeError, "must not be called"):
                lr._request_json_with_retries(
                    [{"role": "user", "content": "x"}],
                    _config(request_fn=sentinel),
                )
        self.assertEqual(len(calls), 1)
        slept.assert_not_called()

    def test_exhaustion_reraises_the_last_error(self):
        calls = []

        def always_503(messages, config):
            calls.append(1)
            raise RuntimeError("HTTP 503 Service Unavailable")

        with mock.patch.object(lr, "_sleep") as slept:
            with self.assertRaisesRegex(RuntimeError, "HTTP 503"):
                lr._request_json_with_retries(
                    [{"role": "user", "content": "x"}],
                    _config(request_fn=always_503),
                )
        # Default providers: 3 total attempts, so 2 sleeps.
        self.assertEqual(len(calls), 3)
        self.assertEqual(slept.call_count, 2)

    def test_openrouter_free_gets_four_attempts(self):
        calls = []

        def always_429(messages, config):
            calls.append(1)
            raise _http_error(429, retry_after="0")

        with mock.patch.object(lr, "_sleep"):
            with self.assertRaises(requests.HTTPError):
                lr._request_json_with_retries(
                    [{"role": "user", "content": "x"}],
                    _config(
                        provider="openrouter",
                        model="openrouter/free",
                        request_fn=always_429,
                    ),
                )
        self.assertEqual(len(calls), 4)

    def test_the_review_paths_route_through_the_retry_wrapper(self):
        # The three provider call sites (generic, bundle, exploration) must
        # not call request_json directly any more; a 503 on the first attempt
        # must not surface as an assessment error.
        with open(lr.__file__, "r", encoding="utf-8") as handle:
            source = handle.read()
        direct_calls = [
            line
            for line in source.splitlines()
            if "request_json(" in line
            and "_request_json_with_retries" not in line
            and "def request_json" not in line
            and "request_fn(" not in line
        ]
        self.assertEqual(
            direct_calls,
            ["            return request_json(messages, config)"],
            "only the retry wrapper itself may call request_json directly",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
