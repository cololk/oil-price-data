import io
import json
from pathlib import Path
import runpy
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from scripts import fetch_brent as b

NOW = b.epoch("2026-10-02T16:00:00Z")
QUOTE_TIME = NOW - 60
LEGACY = {"price": 93.19, "prev": 95.87, "updated": "2026-10-01T15:18:15Z",
          "date": "2026-10-01", "source": "github-actions"}


def payload(**meta_changes):
    meta = {"symbol": "BZ=F", "currency": "USD", "regularMarketPrice": 102.345,
            "regularMarketTime": QUOTE_TIME, "gmtoffset": -14400,
            "chartPreviousClose": 88.00}
    meta.update(meta_changes)
    return {"chart": {"error": None, "result": [{"meta": meta,
            "timestamp": [QUOTE_TIME - 2 * 86400, QUOTE_TIME - 86400, QUOTE_TIME],
            "indicators": {"quote": [{"close": [99.1, 101.23, 102.345]}]}}]}}


class Response(io.BytesIO):
    def __init__(self, data, status=200):
        super().__init__(data if isinstance(data, bytes) else json.dumps(data).encode())
        self.status = status

    def getcode(self):
        return self.status


class ParsingTests(unittest.TestCase):
    def test_actual_brent_and_previous_session(self):
        quote = b.parse_quote(payload(), NOW)
        self.assertEqual(quote, {"price": 102.34, "prev": 101.23,
                                 "quote_time": b.timestamp(QUOTE_TIME)})

    def test_explicit_previous_close_over_range_previous_close(self):
        self.assertEqual(b.parse_quote(payload(previousClose=101.5), NOW)["prev"], 101.5)

    def test_missing_previous_bar_does_not_use_older_close(self):
        data = payload()
        data["chart"]["result"][0]["indicators"]["quote"][0]["close"][1] = None
        self.assertIsNone(b.parse_quote(data, NOW)["prev"])

    def test_missing_current_close_does_not_shift_previous_session(self):
        data = payload()
        data["chart"]["result"][0]["indicators"]["quote"][0]["close"][-1] = None
        self.assertEqual(b.parse_quote(data, NOW)["prev"], 101.23)

    def test_reject_wti_and_non_usd(self):
        for changed in ({"symbol": "CL=F"}, {"currency": "EUR"}):
            with self.subTest(changed=changed), self.assertRaisesRegex(b.QuoteError, "wrong_symbol"):
                b.parse_quote(payload(**changed), NOW)

    def test_reject_invalid_prices(self):
        for value in (None, "102.3", True, 0, -1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(b.QuoteError):
                b.parse_quote(payload(regularMarketPrice=value), NOW)

    def test_quote_timestamp_required(self):
        for value in (None, "123", True, 0, float("nan")):
            with self.subTest(value=value), self.assertRaises(b.QuoteError):
                b.parse_quote(payload(regularMarketTime=value), NOW)

    def test_stale_and_future_quotes_rejected(self):
        for value, message in ((NOW - b.MAX_AGE - 1, "stale_quote"), (NOW + 301, "future_quote")):
            with self.subTest(value=value), self.assertRaisesRegex(b.QuoteError, message):
                b.parse_quote(payload(regularMarketTime=value), NOW)

    def test_weekend_quote_is_accepted_with_original_time(self):
        result = b.parse_quote(payload(), NOW + 2 * 86400)
        self.assertEqual(result["quote_time"], b.timestamp(QUOTE_TIME))

    def test_malformed_chart_is_controlled_error(self):
        for value in (None, [], {}, {"chart": None}, {"chart": {"result": []}},
                      {"chart": {"error": {"description": "secret body"}}}):
            with self.subTest(value=value), self.assertRaises(b.QuoteError) as ctx:
                b.parse_quote(value, NOW)
            self.assertNotIn("secret body", str(ctx.exception))

    def test_proxy_requires_successful_upstream(self):
        for status in (429, 503, None):
            with self.subTest(status=status), self.assertRaisesRegex(b.QuoteError, "proxy_upstream"):
                b.parse_quote({"status": {"http_code": status}, "contents": json.dumps(payload())}, NOW, True)


class FetchTests(unittest.TestCase):
    def run_fetch(self, responses, **kwargs):
        self.opener = Mock(side_effect=responses)
        self.sleep = Mock()
        self.events = []
        return b.fetch_quote(NOW, opener=self.opener, sleep=self.sleep,
                             emit=lambda **event: self.events.append(event), **kwargs)

    def test_direct_success_does_not_contact_proxy(self):
        result = self.run_fetch([Response(payload())])
        self.assertEqual(result["route"], "yahoo-query1")
        self.assertEqual(self.opener.call_count, 1)
        self.assertEqual(self.opener.call_args[1]["timeout"], 20)
        self.assertIn("BZ%3DF", self.opener.call_args[0][0].full_url)
        self.sleep.assert_not_called()

    def test_timeout_retry_then_success(self):
        self.run_fetch([socket.timeout("secret=https://token@example.test"), Response(payload())])
        self.sleep.assert_called_once_with(2)
        first = self.events[0]
        self.assertEqual(first["event"], "fetch_failed")
        self.assertTrue(first["will_retry"])
        self.assertIn("elapsed_ms", first)
        self.assertNotIn("secret", json.dumps(self.events))

    def test_rate_limit_retries_then_uses_alternate_route(self):
        errors = [HTTPError("https://private.test/?token=secret", 429, "secret", {}, None)
                  for _ in range(2)]
        result = self.run_fetch(errors + [Response(payload())])
        self.assertEqual(result["route"], "yahoo-query2")
        self.assertEqual(self.events[0]["http_status"], 429)
        self.assertNotIn("secret", json.dumps(self.events))

    def test_nonretryable_http_error_falls_back_immediately(self):
        result = self.run_fetch([HTTPError("https://example.test", 403, "denied", {}, None), Response(payload())])
        self.assertEqual(result["route"], "yahoo-query2")
        self.sleep.assert_not_called()

    def test_proxy_is_last_route_and_still_requires_actual_brent(self):
        proxy = {"status": {"http_code": 200}, "contents": json.dumps(payload())}
        result = self.run_fetch([Response(payload(symbol="CL=F")), Response(payload(currency="EUR")), Response(proxy)])
        self.assertEqual(result["route"], "yahoo-via-allorigins")
        self.assertEqual(result["price"], 102.34)

    def test_proxy_upstream_status_logged_and_retried(self):
        route = (b.ROUTES[-1],)
        proxy = {"status": {"http_code": 503}, "contents": "not JSON"}
        good = {"status": {"http_code": 200}, "contents": json.dumps(payload())}
        self.run_fetch([Response(proxy), Response(good)], routes=route)
        self.assertEqual(self.events[0]["upstream_http_status"], 503)
        self.sleep.assert_called_once_with(2)

    def test_html_response_retried_without_logging_body(self):
        self.run_fetch([Response(b"<html>secret</html>"), Response(payload())])
        self.assertEqual(self.events[0]["reason"], "invalid_json")
        self.assertEqual(self.events[0]["http_status"], 200)
        self.assertNotIn("secret", json.dumps(self.events))

    def test_all_network_failures_are_bounded_and_raise(self):
        with self.assertRaisesRegex(b.QuoteError, "all_routes_failed"):
            self.run_fetch([URLError(socket.gaierror("private hostname")) for _ in range(6)])
        self.assertEqual(self.opener.call_count, 6)
        self.assertEqual(self.sleep.call_count, 3)
        self.assertEqual(len(self.events), 6)
        self.assertNotIn("private hostname", json.dumps(self.events))

    def test_old_response_cannot_regress_previous_quote(self):
        proxy = {"status": {"http_code": 200}, "contents": json.dumps(payload())}
        with self.assertRaisesRegex(b.QuoteError, "all_routes_failed"):
            self.run_fetch([Response(payload()), Response(payload()), Response(proxy)],
                           minimum_time=QUOTE_TIME + 1)
        self.assertTrue(all(e["reason"] == "quote_regression" for e in self.events))

    def test_oversized_response_is_rejected(self):
        result = self.run_fetch([Response(b"x" * (b.MAX_BYTES + 1)), Response(payload())])
        self.assertEqual(result["route"], "yahoo-query2")
        self.assertEqual(self.events[0]["reason"], "response_too_large")


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.data = Path(self.directory.name) / "brent.json"
        self.status = Path(self.directory.name) / "brent-status.json"
        self.data.write_text(json.dumps(LEGACY), encoding="utf-8")
        self.success = Mock(return_value=dict(b.parse_quote(payload(), NOW), route="yahoo-query1"))
        self.failure = Mock(side_effect=b.QuoteError("all_routes_failed"))
        self.logger = patch.object(b, "log")
        self.logger.start()
        self.addCleanup(self.logger.stop)

    def read(self):
        return json.loads(self.data.read_text())

    def refresh(self, fetcher, now=NOW):
        return b.update(self.data, self.status, now, fetcher)

    def test_success_keeps_legacy_fields_and_adds_verified_provenance(self):
        self.assertEqual(self.refresh(self.success), 0)
        result = self.read()
        self.assertTrue(set(LEGACY).issubset(result))
        self.assertEqual(result["source"], "github-actions")
        self.assertEqual(result["symbol"], "BZ=F")
        self.assertEqual(result["quote_time"], b.timestamp(QUOTE_TIME))
        self.assertFalse(result["stale"])
        self.assertEqual(json.loads(self.status.read_text())["status"], "ok")

    def test_failure_keeps_prices_timestamps_and_provenance(self):
        self.assertEqual(self.refresh(self.failure), 1)
        result = self.read()
        for key, value in LEGACY.items():
            self.assertEqual(result[key], value)
        self.assertTrue(result["stale"])
        self.assertEqual(result["checked_at"], b.timestamp(NOW))
        self.assertNotIn("symbol", result)  # Never relabel old WTI estimates as Brent.
        status = json.loads(self.status.read_text())
        self.assertEqual(status["status"], "error")
        self.assertTrue(status["has_cached_quote"])

    def test_repeat_quote_does_not_refresh_updated_or_date(self):
        self.refresh(self.success)
        original = self.read()
        self.refresh(self.success, now=NOW + 86400)
        result = self.read()
        self.assertEqual(result["updated"], original["updated"])
        self.assertEqual(result["date"], original["date"])
        self.assertEqual(result["quote_time"], original["quote_time"])
        self.assertNotEqual(result["checked_at"], original["checked_at"])
        self.assertNotEqual(result["fetched_at"], original["fetched_at"])

    def test_new_quote_recovers_from_failed_refresh(self):
        self.refresh(self.failure)
        self.assertEqual(self.refresh(self.success), 0)
        self.assertFalse(self.read()["stale"])
        self.assertNotIn("error", self.read())

    def test_failure_after_success_preserves_quote_time_and_fetched_at(self):
        self.refresh(self.success)
        original = self.read()
        self.refresh(self.failure, NOW + 86400)
        result = self.read()
        for key in ("price", "prev", "updated", "date", "quote_time", "fetched_at", "provider", "symbol"):
            self.assertEqual(result[key], original[key])
        self.assertTrue(result["stale"])

    def test_missing_cache_writes_status_without_inventing_quote(self):
        self.data.unlink()
        self.assertEqual(self.refresh(self.failure), 1)
        self.assertFalse(self.data.exists())
        self.assertFalse(json.loads(self.status.read_text())["has_cached_quote"])

    def test_corrupt_cache_preserved_and_marked_unusable_in_status(self):
        for invalid in ("{broken", '{"price":NaN}', '[]'):
            with self.subTest(invalid=invalid):
                self.data.write_text(invalid)
                self.assertEqual(self.refresh(self.failure), 1)
                self.assertEqual(self.data.read_text(), invalid)
                self.assertFalse(json.loads(self.status.read_text())["has_cached_quote"])

    def test_regression_guard_uses_previous_verified_quote_time(self):
        self.refresh(self.success)
        self.refresh(self.success, NOW + 60)
        self.assertEqual(self.success.call_args[1]["minimum_time"], QUOTE_TIME)

    def test_atomic_write_failure_preserves_original_file(self):
        original = self.data.read_bytes()
        with patch.object(b.os, "replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                b.atomic_write(self.data, {"price": 102.3})
        self.assertEqual(self.data.read_bytes(), original)
        self.assertEqual([p.name for p in self.data.parent.iterdir()], ["brent.json"])

    def test_cli_failure_exits_one_and_publishes_stale_status(self):
        argv = [b.__file__, "--output", str(self.data), "--status-output", str(self.status)]
        with patch("sys.argv", argv), patch("sys.stdout", new_callable=io.StringIO), \
                patch("urllib.request.urlopen", side_effect=socket.timeout()), \
                patch("time.sleep"), self.assertRaises(SystemExit) as result:
            runpy.run_path(b.__file__, run_name="__main__")
        self.assertEqual(result.exception.code, 1)
        self.assertTrue(self.read()["stale"])
        self.assertEqual(self.read()["updated"], LEGACY["updated"])
        self.assertEqual(json.loads(self.status.read_text())["status"], "error")

    def test_cli_success_exits_zero_with_real_brent_symbol(self):
        argv = [b.__file__, "--output", str(self.data), "--status-output", str(self.status)]
        with patch("sys.argv", argv), patch("sys.stdout", new_callable=io.StringIO), \
                patch("urllib.request.urlopen", return_value=Response(payload())), \
                patch("time.time", return_value=NOW), self.assertRaises(SystemExit) as result:
            runpy.run_path(b.__file__, run_name="__main__")
        self.assertEqual(result.exception.code, 0)
        self.assertEqual(self.read()["symbol"], "BZ=F")
        self.assertFalse(self.read()["stale"])


if __name__ == "__main__":
    unittest.main()
