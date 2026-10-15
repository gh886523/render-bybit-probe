# Bybit Network Probe and Read-only Market Bridge

A standalone, read-only connectivity diagnostic and token-protected candle bridge.
The diagnostic routes need no credentials. The bridge requires its own bearer
token, but never a Bybit API key. Diagnostics measure outbound IPv4 through ipify,
looks up country/ASN with ipwho.is, and checks two fixed Bybit V5 public endpoints.

- `/`: readable HTML results.
- `/json`: structured JSON results.
- `/market/candles`: protected, validated STRKUSDT Spot 4H candle pages.

Each visit runs fresh checks and returns `Cache-Control: no-store`. It does not
cache results. Geolocation failure does not stop the Bybit checks. Each outbound
request uses an 8-second socket timeout, a 64 KiB response limit, no retries and
no redirects. A run makes up to four requests. API responses are summarized, not
dumped. The candle check requests STRKUSDT Spot 4H with `limit=2`; this may include
a forming candle. There is no trading, strategy, account access, or state storage.

## Try locally (Python 3.11+)

From a **separate copy of this folder**:

```sh
python -m venv .venv
```

Activate it with `.venv\Scripts\Activate.ps1` in Windows PowerShell, or
`source .venv/bin/activate` on Linux/macOS. Then:

```sh
pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:5000/` or `http://127.0.0.1:5000/json`.
The local development server needs no credentials. Use Gunicorn on Render/Linux.

## Publish only this standalone application

1. Copy `app.py`, `market_bridge.py`, `test_market_bridge.py`, `requirements.txt`,
   `README.md`, and `.gitignore` into a new,
   otherwise empty directory outside any existing project.
2. On GitHub, create a **new public repository** for this diagnostic only.
3. Upload those six files at the repository root. Do not upload a parent folder,
   environment files, virtual environment, credentials, or unrelated project files.
   Creating the repository and uploading are manual steps; this app does neither.

## Create the temporary Render service

1. In the Render dashboard, select **New → Web Service**.
2. Choose **Public Git Repository** and paste the new repository's public HTTPS
   URL. No private repository integration or GitHub token is required by this app.
3. Select your branch, choose **Python 3**, and leave Root Directory empty when
   these six files are at the repository root.
4. Select **Frankfurt, Germany** as the region.
5. Select **Free**, if available for your account. If it is unavailable, stop
   rather than selecting a paid instance solely for this test.
6. Use Python **3.11 or newer**. If setting `PYTHON_VERSION`, use a supported full
   Python version as described in Render's Python version instructions below.
7. Set **Build Command** to:

   ```sh
   pip install -r requirements.txt
   ```

8. Set **Start Command** to:

   ```sh
   gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 2 --timeout 60 app:app
   ```

   Render supplies `PORT`. Keep **one worker**: the global execution lock is
   shared by its two threads, not across multiple worker processes. While a run
   is in progress, another visit gets HTTP 429 `busy` without extra outbound
   requests or cached results. Do not enable Flask debug mode.
9. Create the service and wait for deployment. Open its public URL and `/json`.
   Initial startup may take time on a Free service. Each reload starts a new run;
   wait for the previous run to complete.
10. Inspect `outbound_ipv4`, `country_code`, `asn`, and both Bybit HTTP statuses,
    `ret_code` and `candle_count`. `bybit_accessible=true` requires both checks
    to succeed, including the candle response's identity and basic data shape.

**Frankfurt does not guarantee a German outbound IP or Bybit access.** This test
measures actual responses. IP geolocation is an estimate; destination-dependent
egress routing and changes over time are possible. `unavailable` means the IP or
geolocation service could not supply a validated value. HTTP 403 indicates refusal
from this runner; it does not alone prove why access was refused. DNS, TLS,
connection, timeout, other HTTP errors, malformed JSON, and API-level failures
are reported separately. Raw error messages and internal server data are withheld.

## Protected candle endpoint (Phase 1 only)

`GET /market/candles?start_ms=<open-ms>&end_ms=<open-ms>&limit=1000`

Send `Authorization: Bearer <token>` over HTTPS. In Render's **Environment** settings,
add **MARKET_BRIDGE_TOKEN** as a secret value. Generate a high-entropy token using
a password manager: 43 random URL-safe characters is a suitable length. The
accepted format is 32–256 ASCII letters, digits, `_` or `-`. Keep the same value
in your password manager for testing. Do not put it in source, a URL, shell history,
screenshots, or logs. If the variable is missing, empty or malformed, all bridge
requests return **401**. Diagnostic routes continue to work without it.

Only the following query parameters are accepted, each at most once:

| Parameter | Meaning |
| --- | --- |
| `start_ms` | Required inclusive UTC candle-open timestamp in integer milliseconds. |
| `end_ms` | Required inclusive UTC candle-open timestamp; must be >= start. |
| `limit` | Optional page size, integer 1–1000; default 1000. |

Timestamp inputs must be canonical nonnegative decimal integers, at most 13 digits,
and multiples of **14,400,000** (UTC 4H boundaries). A request may span at most
**2,000 candles** inclusive (about 333 days). Split larger ranges into separate
bounded requests. No symbols, categories, intervals, URLs, hosts, cutoffs or tokens
are accepted as query parameters. Only GET performs bridge work; HEAD and other
methods are rejected.

The bridge obtains Bybit server time first. The last requested candle must be
completed according to **both** Bybit and the service's local UTC clock. It then
makes one fixed Bybit V5 kline request with `category=spot`, `symbol=STRKUSDT`, and
`interval=240`. Requests use an 8-second socket timeout, at most 512 KiB per
upstream response, no redirects, and **zero automatic retries**. A bridge call
uses at most two upstream GETs and never queries ipify/geolocation.

HTTP 200 success has this shape (values below are illustrative):

```json
{
  "schema_version": 1,
  "source": {
    "provider": "Bybit", "api": "V5", "origin": "https://api.bybit.com",
    "category": "spot", "symbol": "STRKUSDT", "interval": "240"
  },
  "request": {"start_ms": 1727942400000, "end_ms": 1727985600000, "limit": 2},
  "server_time_ms": 1728000000000,
  "completed_through_ms": 1728000000000,
  "order": "newest_first",
  "fields": ["startTime", "open", "high", "low", "close", "volume", "turnover"],
  "candles": [
    ["1727985600000", "0.036400", "0.040000", "0.030000", "0.036500", "123456.00", "4500.123456789"],
    ["1727971200000", "0.036400", "0.040000", "0.030000", "0.036500", "123456.00", "4500.123456789"]
  ],
  "has_more": true,
  "next_end_ms": 1727956800000
}
```

All **seven candle fields remain the original Bybit strings**, including trailing
zeroes and arbitrary decimal precision. `Decimal` is used only for validation;
prices are never calculated or converted to floats. No missing field is invented.
Rows must have the correct market identity and seven fields, finite positive OHLC,
valid OHLC bounds, nonnegative volume/turnover, aligned timestamps inside the
requested range, and strict contiguous **newest-first** ordering beginning at
`end_ms`. Duplicates, gaps, empty results, missing newest candles, unexpected HTTP
statuses, invalid JSON and unsuccessful Bybit `retCode` values fail the whole page.

For pagination, keep `start_ms` and `limit` unchanged and replace `end_ms` with
`next_end_ms` while `has_more` is true. Short pages can still have a continuation.
The cursor decreases by at least one 4H interval and cannot go below `start_ms`.
Stop only when `has_more=false` and `next_end_ms=null`. Clients must still validate
complete cross-page coverage and continuity against their own stored history.
Do not silently skip a failed page or substitute stale cached data.

Error bodies contain a fixed code, for example `{"error":"upstream_http_403_forbidden"}`:

| HTTP status | Meaning |
| --- | --- |
| 400 | Invalid/duplicate/excessive parameters or incomplete/future candles. |
| 401 | Missing/invalid bearer token or absent/malformed server secret. |
| 405 | Unsupported HTTP method. |
| 429 | Another diagnostic/bridge run holds the shared lock; Retry-After: 2. |
| 502 | Upstream HTTP/API/network/JSON/data validation failure; no candles returned. |
| 500 | Unexpected failure; raw details withheld and lock released. |

Network failures distinguish `upstream_dns_failure`, `upstream_tls_error`,
`upstream_timeout` and `upstream_connection_error`. Validation codes include
`upstream_market_mismatch`, `upstream_candle_gap_or_order` and
`upstream_invalid_candle_fields`. No upstream `retMsg`, headers, stack trace,
environment variables, token, or internal server paths are returned.

## Manual deployment and safe testing

Nothing is deployed by running the local tests. For a later manual deployment:

1. In a separate copy of **this directory**, install requirements and run:
   `python -B -m unittest -v test_market_bridge`. All outbound HTTP is mocked;
   tests use a public dummy token and never read a real credential.
2. After review, manually upload/update only the six files listed above in the
   independent public repository. Do not upload any parent project contents.
3. Set `MARKET_BRIDGE_TOKEN` in the Render service's Environment settings. Save
   and redeploy only when you choose to do so. For a Public Git Repository service,
   use **Manual Deploy → Deploy latest commit** when needed. Keep the existing
   Frankfurt/Free settings and Python 3.11+ runtime.
4. Build command: `pip install -r requirements.txt`.
5. Start command:
   `gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 2 --timeout 60 app:app`.
   Keep one worker and one instance for the shared lock. Keep debug and header
   logging disabled; do not add Authorization to access-log formats.
6. Open `/` or `/json` to check the preserved diagnostics. Then test the protected
   endpoint using the hidden token prompt below. No token belongs in the browser URL.

**PowerShell 7+**, with the real service hostname substituted (the token is entered
at a hidden prompt, never as a command argument or URL parameter):

```powershell
$bridgeToken = Read-Host 'Bridge token (hidden)' -AsSecureString
$interval = 14400000L
$now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
$end = $now - ($now % $interval) - $interval
$start = $end - $interval
$uri = "https://YOUR-SERVICE.onrender.com/market/candles?start_ms=$start&end_ms=$end&limit=2"
try {
    Invoke-RestMethod -Uri $uri -Authentication Bearer -Token $bridgeToken -MaximumRedirection 0 -TimeoutSec 90 |
        ConvertTo-Json -Depth 8
} finally {
    Remove-Variable bridgeToken
}
```

This prints market data only. Do not use verbose/debug HTTP tracing or PowerShell
transcripts during credential testing. HTTPS redirects are disabled to avoid
forwarding Authorization elsewhere. Repeat without `-Authentication` and `-Token`
to verify HTTP 401. Never disable server authentication to get a successful test.
To test locally, set the token in the local process environment using your secure
secret-handling method, then run `python app.py`; do not store it in a tracked file.

Render Free may sleep, restart, or delay startup. The client's 90-second timeout
allows for cold start and is distinct from each upstream 8-second socket timeout.
If startup or a transient 429/502 occurs, retry the **same bounded request** after
a delay (for example, 5 then 15 seconds, at most two retries), without exposing the
token. Wait for Bybit publication if requesting a just-closed candle. Do not retry
401 or invalid-input errors automatically, and do not accept partial data as success.

## Security and compatibility limits

- A shared bearer token grants read access, not per-user identity, token expiry,
  replay prevention, quotas, or full denial-of-service protection. Rotate it in
  Render and in authorized clients if exposed. It must never be a Bybit API key.
- The public diagnostic routes remain unauthenticated. Their shared lock can be
  occupied by public requests, temporarily returning 429 to authorized clients.
  This simple gate limits concurrent work, not sustained request volume.
- One worker is required; multiple processes/instances do not share this lock.
  Platform/server request logging is outside this app's control: keep credentials
  in HTTPS Authorization headers, never in query strings, paths, or debug output.
- Source identification describes the validated fixed Bybit response; this service
  does not sign data or provide immutable history. It has no cache, database,
  account sizing, exchange orders, private API calls, or persistent state.
- Successful diagnostic access **does not guarantee uninterrupted future
  availability**, stable country/IP, or future Bybit acceptance. Free instances
  sleep and have usage limits. A failed dependency must remain an explicit failure.
- Before any integration, agree on the client's adapter for this versioned wrapper,
  newest-first pagination, 2,000-candle range splitting, clock/closed-candle cutoff,
  strict stored-history comparison, token storage, cold-start timeout and retry
  policy. This app does not modify or connect any existing market synchronizer.

## Remove or pause when unused

In Render, open this service's **Settings**, find **Delete Web Service**, and
confirm deletion after recording the results. This avoids leaving a public
diagnostic running unnecessarily. No paid third-party service is needed.

Official references: [Flask deployment](https://render.com/docs/deploy-flask),
[public repositories](https://render.com/docs/web-services#deploy-your-own-code),
[regions](https://render.com/docs/regions), and
[Python versions](https://render.com/docs/python-version),
[Free service limits](https://render.com/docs/free), and
[Bybit candle contract](https://bybit-exchange.github.io/docs/v5/market/kline).
