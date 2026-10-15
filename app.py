"""Public, read-only connectivity checks against fixed HTTPS endpoints."""
from datetime import datetime, timezone
from http.client import HTTPException
import ipaddress
import json
import math
import os
import re
import socket
import ssl
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__, static_folder=None)
RUN_LOCK = Lock()
TIMEOUT = 8
MAX_BYTES = 65536
TIME_URL = "https://api.bybit.com/v5/market/time"
KLINE_URL = "https://api.bybit.com/v5/market/kline?category=spot&symbol=STRKUSDT&interval=240&limit=2"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def get_json(url, *, max_bytes=MAX_BYTES):
    """Return bounded JSON internally; expose only allowlisted summary fields."""
    status = None
    try:
        req = Request(url, headers={"User-Agent": "Public-Bybit-Network-Probe"})
        with build_opener(NoRedirect()).open(req, timeout=TIMEOUT) as response:
            status = response.status
            payload = response.read(max_bytes + 1)
        if len(payload) > max_bytes:
            return status, None, "response_too_large"
        try:
            data = json.loads(payload, object_pairs_hook=unique_object,
                              parse_constant=reject_constant)
        except (ValueError, UnicodeError):
            return status, None, "malformed_json"
        if not isinstance(data, dict):
            return status, None, "invalid_json_structure"
        return status, data, "ok"
    except HTTPError as error:
        status = error.code
        error.close()
        return status, None, "http_403_forbidden" if status == 403 else "http_failure"
    except (URLError, OSError, HTTPException) as error:
        reason = error.reason if isinstance(error, URLError) else error
        if isinstance(reason, socket.gaierror):
            kind = "dns_failure"
        elif isinstance(reason, ssl.SSLError):
            kind = "tls_error"
        elif isinstance(reason, TimeoutError):
            kind = "timeout"
        else:
            kind = "connection_error"
        return status, None, kind


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError("Nonfinite JSON value")


def bybit_check(url, *, candles=False):
    status, data, outcome = get_json(url)
    result = {"http_status": status, "status": outcome, "ret_code": None}
    if candles:
        result["candle_count"] = None
    if status != 200 or outcome != "ok":
        if outcome == "ok":
            result["status"] = "unexpected_http_status"
        return result
    code = data.get("retCode")
    if type(code) is not int:
        result["status"] = "invalid_bybit_response"
        return result
    result["ret_code"] = code
    if code != 0:
        result["status"] = "bybit_api_error"
        return result
    if candles:
        market = data.get("result")
        rows = market.get("list") if isinstance(market, dict) else None
        if isinstance(rows, list):
            result["candle_count"] = len(rows)
        valid = (isinstance(rows, list) and 1 <= len(rows) <= 2
                 and market.get("symbol") == "STRKUSDT" and market.get("category") == "spot")
        if valid:
            try:
                for row in rows:
                    if not isinstance(row, list) or len(row) != 7:
                        raise ValueError()
                    if int(row[0]) < 0 or int(row[0]) % 14_400_000:
                        raise ValueError()
                    if not all(math.isfinite(float(value)) and float(value) >= 0 for value in row[1:]):
                        raise ValueError()
            except (TypeError, ValueError, OverflowError):
                valid = False
        if not valid:
            result["status"] = "invalid_candle_data"
    return result


def diagnose():
    result = {"checked_at_utc": datetime.now(timezone.utc).isoformat(),
              "outbound_ipv4": "unavailable", "country_code": "unavailable",
              "asn": "unavailable", "ip_status": "unavailable", "geolocation_status": "unavailable"}
    _, ip_data, result["ip_status"] = get_json("https://api.ipify.org?format=json")
    if ip_data is not None:
        try:
            result["outbound_ipv4"] = str(ipaddress.IPv4Address(ip_data["ip"]))
        except (TypeError, KeyError, ValueError):
            result["ip_status"] = "invalid_ipv4_response"
    if result["outbound_ipv4"] != "unavailable":
        _, geo, result["geolocation_status"] = get_json("https://ipwho.is/" + result["outbound_ipv4"])
        if geo is not None:
            if geo.get("success") is True:
                country = geo.get("country_code")
                if isinstance(country, str) and re.fullmatch(r"[A-Z]{2}", country):
                    result["country_code"] = country
                connection = geo.get("connection")
                asn = connection.get("asn") if isinstance(connection, dict) else None
                if type(asn) is int and asn > 0:
                    result["asn"] = "AS" + str(asn)
                if result["country_code"] == "unavailable" or result["asn"] == "unavailable":
                    result["geolocation_status"] = "incomplete_geolocation"
            else:
                result["geolocation_status"] = "geolocation_unavailable"
    # IP/geolocation failure must never prevent the two independent Bybit checks.
    result["bybit_time"] = bybit_check(TIME_URL)
    result["bybit_kline"] = bybit_check(KLINE_URL, candles=True)
    result["bybit_accessible"] = result["bybit_time"]["status"] == "ok" and result["bybit_kline"]["status"] == "ok"
    return result


PAGE = """<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Bybit Network Probe</title>
<style>body{font:16px system-ui;max-width:850px;margin:3rem auto;padding:0 1rem}
pre{background:#f1f4f7;padding:1rem;white-space:pre-wrap;overflow-wrap:anywhere}</style>
<h1>Bybit Network Probe</h1>
{% if busy %}<p>A diagnostic is already running. Wait a moment and reload. No cached result is shown.</p>
{% else %}<p>Fresh checks from this service. Bybit accessible: <strong>{{ accessible }}</strong>.</p>
<pre>{{ summary }}</pre>{% endif %}
<p>Statuses distinguish DNS, TLS, connection, timeout, HTTP 403 and other HTTP errors.
HTTP 200 with malformed JSON is reported separately. No response bodies are displayed.</p>
<p>Frankfurt hosting does not guarantee a German outbound IP. Geolocation is an estimate.
The observed IP can also differ between destinations or over time.</p>
<p><a href="/">Run fresh HTML checks</a> · <a href="/json">Run fresh JSON checks</a></p>
</html>"""


@app.after_request
def no_cache(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.get("/")
@app.get("/json")
def probe():
    if not RUN_LOCK.acquire(blocking=False):
        data, status = {"status": "busy", "message": "A diagnostic is already running. Retry shortly."}, 429
    else:
        try:
            data, status = diagnose(), 200
        except Exception:
            # Do not expose a traceback, environment, URL, or private runtime data.
            data, status = {"status": "diagnostic_error", "message": "Diagnostic could not complete. Retry shortly."}, 500
        finally:
            RUN_LOCK.release()
    if request.path == "/json":
        return jsonify(data), status
    return render_template_string(PAGE, busy=status == 429,
        accessible=data.get("bybit_accessible", "unavailable"), summary=json.dumps(data, indent=2)), status


from market_bridge import register_bridge

register_bridge(app, get_json, RUN_LOCK)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)
