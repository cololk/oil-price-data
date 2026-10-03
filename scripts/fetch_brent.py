#!/usr/bin/env python3
"""Fetch actual BZ=F quotes; never substitute another oil benchmark."""

import argparse
import calendar
import json
import math
import os
from pathlib import Path
import socket
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

SYMBOL = "BZ=F"
TIMEOUT = 20
ATTEMPTS = 2
MAX_AGE = 96 * 3600  # Accommodates weekends and common three-day market holidays.
MAX_BYTES = 2 * 1024 * 1024
CHART_PATH = "/v8/finance/chart/BZ%3DF?range=5d&interval=1d"
DIRECT = "https://query1.finance.yahoo.com" + CHART_PATH
# These are alternative routes to ONE provider, not independent data sources.
ROUTES = (
    ("yahoo-query1", DIRECT, False),
    ("yahoo-query2", "https://query2.finance.yahoo.com" + CHART_PATH, False),
    ("yahoo-via-allorigins", "https://api.allorigins.win/get?url=" + quote(DIRECT, safe=""), True),
)


class QuoteError(ValueError):
    """A fixed, safe diagnostic code, never an upstream response body."""


def timestamp(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def epoch(value):
    return calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ"))


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def price(value):
    return number(value) and value > 0


def parse_quote(payload, now, proxied=False):
    try:
        if proxied:
            if payload.get("status", {}).get("http_code") != 200:
                raise QuoteError("proxy_upstream_http_error")
            payload = json.loads(payload["contents"])
        chart = payload["chart"]
        if chart.get("error"):
            raise QuoteError("yahoo_chart_error")
        result = chart["result"][0]
        meta = result["meta"]
        if meta.get("symbol") != SYMBOL or meta.get("currency") != "USD":
            raise QuoteError("wrong_symbol_or_currency")
        value = meta.get("regularMarketPrice")
        as_of = meta.get("regularMarketTime")
        if not price(value) or not number(as_of) or as_of <= 0:
            raise QuoteError("invalid_price_or_quote_time")
        if as_of > now + 300:
            raise QuoteError("future_quote")
        if now - as_of > MAX_AGE:
            raise QuoteError("stale_quote")

        previous = meta.get("previousClose")
        if not price(previous):
            # chartPreviousClose is the close before the whole requested range.
            # Instead use the last valid bar before the quote's exchange-local day.
            previous = None
            offset = meta.get("gmtoffset", 0)
            if not number(offset) or abs(offset) > 14 * 3600:
                raise QuoteError("invalid_timezone_offset")
            quote_day = time.gmtime(as_of + offset)[:3]
            times = result.get("timestamp") or []
            bars = result.get("indicators", {}).get("quote", [{}])[0].get("close") or []
            earlier = [(t, v) for t, v in zip(times, bars)
                       if number(t) and t > 0 and t < as_of
                       and time.gmtime(t + offset)[:3] < quote_day]
            if earlier:
                previous = max(earlier, key=lambda item: item[0])[1]
        return {"price": round(value, 2), "prev": round(previous, 2) if price(previous) else None,
                "quote_time": timestamp(as_of)}
    except QuoteError:
        raise
    except (KeyError, IndexError, TypeError, ValueError, OverflowError, AttributeError) as exc:
        raise QuoteError("invalid_payload_" + type(exc).__name__) from exc


def log(**fields):
    print(json.dumps(fields, sort_keys=True), flush=True)


def fetch_quote(now, minimum_time=None, opener=urlopen, sleep=time.sleep,
                monotonic=time.monotonic, emit=log, routes=ROUTES):
    for label, url, proxied in routes:
        for attempt in range(1, ATTEMPTS + 1):
            started = monotonic()
            http_status = None
            upstream_status = None
            retryable = False
            try:
                request = Request(url, headers={"User-Agent": "Mozilla/5.0 BrentPriceFetcher/1.0",
                                                "Accept": "application/json"})
                with opener(request, timeout=TIMEOUT) as response:
                    http_status = response.getcode()
                    if http_status != 200:
                        raise HTTPError(url, http_status, "unexpected status", {}, None)
                    raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise QuoteError("response_too_large")
                payload = json.loads(raw.decode("utf-8"))
                if proxied and isinstance(payload, dict):
                    candidate = payload.get("status", {})
                    if isinstance(candidate, dict) and isinstance(candidate.get("http_code"), int):
                        upstream_status = candidate["http_code"]
                result = parse_quote(payload, now, proxied)
                if minimum_time and epoch(result["quote_time"]) < minimum_time:
                    raise QuoteError("quote_regression")
                emit(event="fetch_ok", route=label, attempt=attempt,
                     http_status=http_status, upstream_http_status=upstream_status,
                     elapsed_ms=round((monotonic() - started) * 1000),
                     quote_time=result["quote_time"])
                result["route"] = label
                return result
            except HTTPError as exc:
                http_status = exc.code
                error_type, reason = "HTTPError", "http_error"
                retryable = exc.code in (408, 429) or 500 <= exc.code < 600
                if exc.fp is not None:
                    exc.close()
            except (URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
                error_type = type(exc).__name__
                detail = exc.reason if isinstance(exc, URLError) else exc
                reason = type(detail).__name__
                retryable = True
            except (UnicodeError, json.JSONDecodeError) as exc:
                error_type, reason = type(exc).__name__, "invalid_json"
                retryable = True
            except QuoteError as exc:
                error_type, reason = type(exc).__name__, str(exc)
                retryable = upstream_status in (408, 429) or (
                    upstream_status is not None and 500 <= upstream_status < 600)
            emit(event="fetch_failed", route=label, attempt=attempt,
                 http_status=http_status, upstream_http_status=upstream_status,
                 elapsed_ms=round((monotonic() - started) * 1000),
                 error_type=error_type, reason=reason,
                 will_retry=retryable and attempt < ATTEMPTS)
            if not retryable or attempt == ATTEMPTS:
                break
            sleep(2 ** attempt)  # Two attempts per route; no unbounded retry loop.
    raise QuoteError("all_routes_failed")


def read_previous(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not price(value.get("price")):
            return None
        if value.get("prev") is not None and not price(value["prev"]):
            return None
        epoch(value["updated"])
        time.strptime(value["date"], "%Y-%m-%d")
        return value
    except (OSError, ValueError, KeyError, TypeError):
        return None


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, sort_keys=True, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def update(data_path, status_path, now=None, fetcher=fetch_quote):
    now = time.time() if now is None else now
    checked = timestamp(now)
    previous = read_previous(data_path)
    minimum = None
    if previous and previous.get("provider") == "yahoo" and previous.get("symbol") == SYMBOL:
        try:
            minimum = epoch(previous["quote_time"])
        except (ValueError, TypeError, KeyError):
            pass
    try:
        result = fetcher(now, minimum_time=minimum)
    except QuoteError as exc:
        # Legacy data may have used the WTI estimate: never relabel it as Brent.
        # Keep original price, dates, provenance and last successful fetch time.
        if previous:
            previous.update(stale=True, checked_at=checked, error=str(exc))
            atomic_write(data_path, previous)
        atomic_write(status_path, {"status": "error", "stale": True,
                                   "checked_at": checked, "error": str(exc),
                                   "has_cached_quote": previous is not None})
        log(event="refresh_failed", error=str(exc), has_cached_quote=previous is not None)
        return 1

    unchanged = previous and all(previous.get(k) == result[k]
                                 for k in ("quote_time", "price", "prev"))
    updated = previous["updated"] if unchanged else checked
    output = {"price": result["price"], "prev": result["prev"],
              "updated": updated, "date": previous["date"] if unchanged else checked[:10],
              "source": "github-actions", "provider": "yahoo", "symbol": SYMBOL,
              "currency": "USD", "quote_time": result["quote_time"],
              "fetched_at": checked, "checked_at": checked,
              "route": result["route"], "stale": False}
    atomic_write(data_path, output)
    atomic_write(status_path, {"status": "ok", "stale": False, "checked_at": checked,
                               "quote_time": result["quote_time"], "route": result["route"],
                               "has_cached_quote": True})
    log(event="refresh_ok", price=result["price"], quote_time=result["quote_time"], unchanged=bool(unchanged))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("brent.json"))
    parser.add_argument("--status-output", type=Path, default=Path("brent-status.json"))
    args = parser.parse_args()
    return update(args.output, args.status_output)


if __name__ == "__main__":
    raise SystemExit(main())
