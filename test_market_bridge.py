"""Run locally with unittest; every outbound HTTP response is mocked."""
from copy import deepcopy
import io
import json
import os
import socket
import ssl
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

import app as probe
import market_bridge as bridge

TOKEN = "fixture_only_" + "a" * 40
PRIVATE = "DO_NOT_LEAK_RESPONSE_HEADERS_TOKEN_OR_EXCEPTION"
CLOCK = 1_728_000_000_000
I = bridge.INTERVAL_MS
START, END = CLOCK - 4 * I, CLOCK - I


def candle(stamp):
    return [str(stamp), "0.03640000000000000001", "0.040000", "0.030000",
            "0.036500", "123456.00000000000000001", "4500.1234567890123456789"]


class Response(io.BytesIO):
    status = 200


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.client = probe.app.test_client()
        self.calls = []
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"MARKET_BRIDGE_TOKEN": TOKEN}).start()
        patch.object(bridge.time, "time_ns", return_value=CLOCK * 1_000_000).start()
        self.market_change = lambda result: result
        self.clock_change = lambda result: result
        self.failure = None
        self.opener = patch.object(probe, "build_opener").start().return_value
        self.opener.open.side_effect = self.open

    def open(self, request, timeout):
        self.assertEqual(timeout, 8)
        self.assertEqual(request.get_method(), "GET")
        self.assertFalse(request.has_header("Authorization"))
        parsed = urlparse(request.full_url)
        self.calls.append((parsed.hostname, parsed.path, parse_qs(parsed.query)))
        if parsed.hostname == "api.ipify.org":
            return Response(b'{"ip":"192.0.2.1"}')
        if parsed.hostname == "ipwho.is":
            return Response(b'{"success":true,"country_code":"DE","connection":{"asn":64500}}')
        self.assertEqual(parsed.hostname, "api.bybit.com")
        if self.failure:
            return self.failure(parsed)
        if parsed.path == "/v5/market/time":
            result = self.clock_change({"timeSecond": str(CLOCK // 1000), "timeNano": str(CLOCK * 1_000_000)})
        else:
            self.assertEqual(parsed.path, "/v5/market/kline")
            query = parse_qs(parsed.query)
            self.assertEqual(query["category"], ["spot"])
            self.assertEqual(query["symbol"], ["STRKUSDT"])
            self.assertEqual(query["interval"], ["240"])
            start, end, limit = (int(query[name][0]) for name in ("start", "end", "limit"))
            rows = [candle(stamp) for stamp in range(end, start-I, -I)][:limit]
            result = self.market_change({"category": "spot", "symbol": "STRKUSDT", "list": rows})
        return Response(json.dumps({"retCode": 0, "retMsg": PRIVATE, "result": result}).encode())

    def get(self, query=None, token=TOKEN, method="GET"):
        headers = {"Authorization": "Bearer " + token} if token is not None else {}
        return self.client.open("/market/candles", method=method,
            query_string=query if query is not None else {"start_ms": START, "end_ms": END, "limit": 2}, headers=headers)

    def test_authentication_fail_closed_and_no_network(self):
        for token in (None, "wrong", "b" * 44, "İ" * 40):
            self.assertEqual(self.get(token=token).status_code, 401)
        with patch.dict(os.environ, {"MARKET_BRIDGE_TOKEN": ""}):
            self.assertEqual(self.get().status_code, 401)
        with patch.dict(os.environ, {"MARKET_BRIDGE_TOKEN": "too_short"}):
            self.assertEqual(self.get().status_code, 401)
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.get().status_code, 401)
        self.assertEqual(self.calls, [])

    def test_exact_strings_contract_and_pagination(self):
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        data = response.get_json()
        self.assertEqual(data["source"]["category"], "spot")
        self.assertEqual(data["candles"], [candle(END), candle(END-I)])
        self.assertEqual(data["completed_through_ms"], CLOCK)
        self.assertTrue(data["has_more"])
        following = self.get({"start_ms": START, "end_ms": data["next_end_ms"], "limit": 2}).get_json()
        self.assertEqual(following["candles"], [candle(START+I), candle(START)])
        self.assertFalse(following["has_more"])
        self.assertIsNone(following["next_end_ms"])
        self.assertNotIn(TOKEN, response.get_data(as_text=True))
        self.assertNotIn(PRIVATE, response.get_data(as_text=True))

    def test_short_page_still_provides_safe_continuation(self):
        self.market_change = lambda market: dict(market, list=market["list"][:1])
        data = self.get().get_json()
        self.assertEqual(data["candles"], [candle(END)])
        self.assertEqual(data["next_end_ms"], END-I)

    def test_maximum_page_and_range_are_supported(self):
        start = CLOCK-2000*I
        response = self.get({"start_ms": start, "end_ms": END})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.get_json()["candles"]), 1000)

    def test_invalid_excessive_duplicate_and_proxy_inputs_rejected_before_network(self):
        queries = [{}, {"start_ms": START}, {"start_ms": END, "end_ms": START},
            {"start_ms": START+1, "end_ms": END}, {"start_ms": "1e12", "end_ms": END},
            {"start_ms": "0"*100, "end_ms": END},
            {"start_ms": CLOCK-2001*I, "end_ms": END},
            {"start_ms": START, "end_ms": END, "limit": 0},
            {"start_ms": START, "end_ms": END, "limit": 1001},
            [("start_ms", START), ("start_ms", START), ("end_ms", END)]]
        for key in ("url", "host", "symbol", "category", "interval", "token", "cutoff_ms"):
            queries.append({"start_ms": START, "end_ms": END, key: "unaccepted"})
        for query in queries:
            with self.subTest(query=query):
                self.assertEqual(self.get(query).status_code, 400)
        self.assertEqual(self.calls, [])

    def test_forming_or_future_candle_rejected_before_kline(self):
        response = self.get({"start_ms": CLOCK, "end_ms": CLOCK})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(self.calls), 1)

    def test_local_clock_cannot_be_overridden_by_a_future_server_clock(self):
        def future(clock):
            return {"timeSecond": str((CLOCK+I)//1000), "timeNano": str((CLOCK+I)*1_000_000)}
        self.clock_change = future
        self.assertEqual(self.get({"start_ms": CLOCK, "end_ms": CLOCK}).status_code, 400)

    def test_invalid_clock_rejected(self):
        for change in (lambda c: {}, lambda c: dict(c, timeNano="1"), lambda c: dict(c, timeSecond=True)):
            self.clock_change = change
            response = self.get()
            self.assertEqual(response.status_code, 502)
            self.assertEqual(response.get_json()["error"], "upstream_invalid_clock")

    def test_market_identity_and_missing_rows_rejected(self):
        for change in (lambda m: dict(m, symbol="BTCUSDT"), lambda m: dict(m, category="linear"),
                       lambda m: dict(m, list=[]), lambda m: dict(m, list=None)):
            self.market_change = change
            self.assertEqual(self.get().status_code, 502)

    def test_invalid_rows_order_gaps_and_missing_fields_rejected(self):
        valid = [candle(END), candle(END-I)]
        cases = [list(reversed(valid)), [valid[0], valid[0]], [valid[0], candle(START)],
                 [candle(END-I)], [valid[0][:-1]], [candle(END+I)], [candle(END+1)]]
        for index, value in ((0, END), (0, "NaN"), (1, 0.0364), (1, "NaN"), (1, "Infinity"),
                             (1, "-1"), (1, "0"), (1, "0.06"), (2, "0.01"),
                             (5, "-1"), (6, "1e8"), (6, "1"*129)):
            row = candle(END)
            row[index] = value
            cases.append([row])
        for rows in cases:
            self.market_change = lambda m: dict(m, list=deepcopy(rows))
            with self.subTest(rows=rows):
                self.assertEqual(self.get().status_code, 502)

    def test_http_api_network_json_and_size_failures_are_sanitized_without_retries(self):
        cases = [
            (lambda: HTTPError("https://example.invalid/"+PRIVATE, 403, PRIVATE, {}, None), "upstream_http_403_forbidden"),
            (lambda: HTTPError("https://example.invalid", 429, PRIVATE, {}, None), "upstream_http_failure"),
            (lambda: URLError(socket.gaierror(PRIVATE)), "upstream_dns_failure"),
            (lambda: URLError(ssl.SSLError(PRIVATE)), "upstream_tls_error"),
            (lambda: TimeoutError(PRIVATE), "upstream_timeout"),
            (lambda: ConnectionRefusedError(PRIVATE), "upstream_connection_error"),
        ]
        for error, expected in cases:
            def fail(parsed): raise error()
            self.failure = fail
            before = len(self.calls)
            response = self.get()
            self.assertEqual(response.status_code, 502)
            self.assertEqual(response.get_json(), {"error": expected})
            self.assertEqual(len(self.calls), before+1)
            self.assertNotIn(PRIVATE, response.get_data(as_text=True))
        for payload, expected in ((b'not JSON', 'upstream_malformed_json'),
                (b'x'*(bridge.MAX_RESPONSE_BYTES+1), 'upstream_response_too_large'),
                (b'{"retCode":true,"result":{}}', 'upstream_invalid_envelope'),
                (b'{"retCode":10006,"retMsg":"'+PRIVATE.encode()+b'"}', 'upstream_api_rejected'),
                (b'{"retCode":0,"retCode":1}', 'upstream_malformed_json'),
                (b'{"retCode":0,"result":{"x":NaN}}', 'upstream_malformed_json')):
            self.failure = lambda parsed: Response(payload)
            response = self.get()
            self.assertEqual(response.status_code, 502)
            self.assertEqual(response.get_json(), {"error": expected})
            self.assertNotIn(PRIVATE, response.get_data(as_text=True))

    def test_busy_method_restrictions_and_shared_diagnostic_lock(self):
        with probe.RUN_LOCK:
            response = self.get()
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.headers["Retry-After"], "2")
            self.assertEqual(self.client.get("/json").status_code, 429)
        for method in ("POST", "PUT", "DELETE", "HEAD", "OPTIONS"):
            self.assertEqual(self.get(method=method).status_code, 405)
        self.assertEqual(self.calls, [])

    def test_unexpected_failure_is_sanitized_and_releases_lock(self):
        def fail(parsed): raise RuntimeError(PRIVATE)
        self.failure = fail
        response = self.get()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json(), {"error": "bridge_internal_error"})
        self.assertNotIn(PRIVATE, response.get_data(as_text=True))
        self.assertFalse(probe.RUN_LOCK.locked())

    def test_kline_failure_returns_no_partial_candles_or_raw_response(self):
        original = self.opener.open.side_effect
        def rejected(request, timeout):
            if urlparse(request.full_url).path == "/v5/market/kline":
                self.calls.append(("api.bybit.com", "/v5/market/kline", {}))
                return Response(json.dumps({"retCode": 10006, "retMsg": PRIVATE}).encode())
            return original(request, timeout)
        self.opener.open.side_effect = rejected
        response = self.get()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json(), {"error": "upstream_api_rejected"})
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn(PRIVATE, response.get_data(as_text=True))

    def test_redirects_remain_disabled_and_response_reads_are_bounded(self):
        self.assertIsNone(probe.NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere.invalid"))
        reads = []
        class TrackedResponse(Response):
            def read(self, size=-1):
                reads.append(size)
                return super().read(size)
        self.opener.open.side_effect = lambda req, timeout: TrackedResponse(b'x'*(bridge.MAX_RESPONSE_BYTES+1))
        self.assertEqual(self.get().status_code, 502)
        self.assertEqual(reads, [bridge.MAX_RESPONSE_BYTES+1])

    def test_existing_diagnostics_still_run_fresh_and_do_not_expose_token(self):
        # Diagnostics request no start/end; supply a matching two-candle fixture.
        original = self.opener.open.side_effect
        def diagnostic_open(request, timeout):
            if urlparse(request.full_url).path == "/v5/market/kline":
                self.calls.append(("api.bybit.com", "/v5/market/kline", {}))
                return Response(json.dumps({"retCode": 0, "result": {
                    "category": "spot", "symbol": "STRKUSDT", "list": [candle(END), candle(END-I)]}}).encode())
            return original(request, timeout)
        self.opener.open.side_effect = diagnostic_open
        for route in ("/", "/json"):
            response = self.client.get(route)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(TOKEN, response.get_data(as_text=True))
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(len(self.calls), 8)


if __name__ == "__main__":
    unittest.main()
