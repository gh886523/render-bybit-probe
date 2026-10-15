"""Token-protected, fixed-target Bybit Spot candle pages; no trading or storage."""
from decimal import Decimal
import os
import re
import secrets
import time
from urllib.parse import urlencode

from flask import jsonify, request

INTERVAL_MS = 14_400_000
MAX_RANGE_CANDLES = 2000
MAX_PAGE_CANDLES = 1000
MAX_RESPONSE_BYTES = 524288
ORIGIN = "https://api.bybit.com"
TOKEN_PATTERN = r"[A-Za-z0-9_-]{32,256}"


class BridgeError(Exception):
    def __init__(self, code, status=502):
        self.code, self.status = code, status


def authorized():
    expected = os.environ.get("MARKET_BRIDGE_TOKEN", "")
    header = request.headers.get("Authorization", "")
    supplied = re.fullmatch(r"Bearer (" + TOKEN_PATTERN + r")", header, flags=re.IGNORECASE | re.ASCII)
    if not re.fullmatch(TOKEN_PATTERN, expected) or supplied is None:
        return False
    return secrets.compare_digest(expected.encode("ascii"), supplied.group(1).encode("ascii"))


def parameters():
    if set(request.args) - {"start_ms", "end_ms", "limit"}:
        raise BridgeError("invalid_parameters", 400)
    if any(len(request.args.getlist(key)) != 1 for key in request.args):
        raise BridgeError("duplicate_parameters", 400)
    values = []
    for key in ("start_ms", "end_ms", "limit"):
        text = request.args.get(key, "1000" if key == "limit" else "")
        if not re.fullmatch(r"0|[1-9][0-9]{0,12}", text):
            raise BridgeError("invalid_parameters", 400)
        values.append(int(text))
    start, end, limit = values
    if start % INTERVAL_MS or end % INTERVAL_MS or start > end:
        raise BridgeError("invalid_time_range", 400)
    if (end - start) // INTERVAL_MS + 1 > MAX_RANGE_CANDLES:
        raise BridgeError("time_range_too_large", 400)
    if not 1 <= limit <= MAX_PAGE_CANDLES:
        raise BridgeError("invalid_limit", 400)
    return start, end, limit


def upstream(fetch, path, params=None):
    url = ORIGIN + path + ("?" + urlencode(params) if params else "")
    status, data, outcome = fetch(url, max_bytes=MAX_RESPONSE_BYTES)
    errors = {"dns_failure", "tls_error", "timeout", "connection_error", "http_403_forbidden",
              "http_failure", "malformed_json", "invalid_json_structure", "response_too_large"}
    if outcome != "ok":
        raise BridgeError("upstream_" + outcome if outcome in errors else "upstream_failure")
    if status != 200:
        raise BridgeError("upstream_http_failure")
    if not isinstance(data, dict) or type(data.get("retCode")) is not int:
        raise BridgeError("upstream_invalid_envelope")
    if data["retCode"] != 0:
        raise BridgeError("upstream_api_rejected")
    if not isinstance(data.get("result"), dict):
        raise BridgeError("upstream_invalid_result")
    return data["result"]


def completed_boundary(fetch):
    clock = upstream(fetch, "/v5/market/time")
    seconds, nanos = clock.get("timeSecond"), clock.get("timeNano")
    if (not isinstance(seconds, str) or not re.fullmatch(r"[1-9][0-9]{0,12}", seconds)
            or not isinstance(nanos, str) or not re.fullmatch(r"[1-9][0-9]{0,21}", nanos)):
        raise BridgeError("upstream_invalid_clock")
    if int(nanos) // 1_000_000_000 != int(seconds):
        raise BridgeError("upstream_invalid_clock")
    server_ms = int(nanos) // 1_000_000
    safe_ms = min(server_ms, time.time_ns() // 1_000_000)
    return server_ms, safe_ms // INTERVAL_MS * INTERVAL_MS


def validate_rows(market, start, end, limit):
    if market.get("symbol") != "STRKUSDT" or market.get("category") != "spot":
        raise BridgeError("upstream_market_mismatch")
    rows = market.get("list")
    if not isinstance(rows, list) or not 1 <= len(rows) <= limit:
        raise BridgeError("upstream_invalid_candles")
    previous = end + INTERVAL_MS
    for row in rows:
        if not isinstance(row, list) or len(row) != 7 or any(not isinstance(value, str) for value in row):
            raise BridgeError("upstream_invalid_candle_fields")
        stamp = row[0]
        if not re.fullmatch(r"0|[1-9][0-9]{0,12}", stamp):
            raise BridgeError("upstream_invalid_timestamp")
        opened = int(stamp)
        if opened % INTERVAL_MS or not start <= opened <= end:
            raise BridgeError("upstream_invalid_timestamp")
        # Require newest-first, no duplicates/gaps, and the exact requested end.
        if opened != previous - INTERVAL_MS:
            raise BridgeError("upstream_candle_gap_or_order")
        previous = opened
        if any(len(value) > 128 or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value) for value in row[1:]):
            raise BridgeError("upstream_invalid_numeric_field")
        # Decimal comparisons validate exact text values; output is never converted.
        opened_price, high, low, close, volume, turnover = map(Decimal, row[1:])
        if min(opened_price, high, low, close) <= 0 or not low <= min(opened_price, close) <= max(opened_price, close) <= high:
            raise BridgeError("upstream_invalid_ohlc")
    return rows


def register_bridge(app, fetch, lock):
    @app.route("/market/candles", methods=["GET"], provide_automatic_options=False)
    def market_candles():
        # Flask implicitly permits HEAD for GET routes; do not perform work for it.
        if request.method != "GET":
            return jsonify({"error": "method_not_allowed"}), 405
        if not authorized():
            return jsonify({"error": "unauthorized"}), 401, {"WWW-Authenticate": "Bearer"}
        try:
            start, end, limit = parameters()
        except BridgeError as error:
            return jsonify({"error": error.code}), error.status
        if not lock.acquire(blocking=False):
            return jsonify({"error": "busy"}), 429, {"Retry-After": "2"}
        try:
            server_ms, completed_ms = completed_boundary(fetch)
            if end + INTERVAL_MS > completed_ms:
                raise BridgeError("range_includes_incomplete_or_future_candles", 400)
            market = upstream(fetch, "/v5/market/kline", dict(category="spot", symbol="STRKUSDT",
                              interval="240", start=start, end=end, limit=limit))
            rows = validate_rows(market, start, end, limit)
            more = int(rows[-1][0]) > start
            return jsonify({"schema_version": 1,
                "source": {"provider": "Bybit", "api": "V5", "origin": ORIGIN,
                           "category": "spot", "symbol": "STRKUSDT", "interval": "240"},
                "request": {"start_ms": start, "end_ms": end, "limit": limit},
                "server_time_ms": server_ms, "completed_through_ms": completed_ms,
                "order": "newest_first", "fields": ["startTime", "open", "high", "low", "close", "volume", "turnover"],
                "candles": rows, "has_more": more,
                "next_end_ms": int(rows[-1][0]) - INTERVAL_MS if more else None})
        except BridgeError as error:
            return jsonify({"error": error.code}), error.status
        except Exception:
            # No exceptions, upstream retMsg, headers or environment are logged.
            return jsonify({"error": "bridge_internal_error"}), 500
        finally:
            lock.release()
