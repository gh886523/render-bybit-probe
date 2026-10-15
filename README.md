# Bybit Network Probe

A standalone, public, read-only connectivity diagnostic. No API keys or other
credentials are required. It measures the service's outbound IPv4 through ipify,
looks up country/ASN with ipwho.is, and checks two fixed Bybit V5 public endpoints.

- `/`: readable HTML results.
- `/json`: structured JSON results.

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

## Publish only these four files

1. Copy `app.py`, `requirements.txt`, `README.md`, and `.gitignore` into a new,
   otherwise empty directory outside any existing project.
2. On GitHub, create a **new public repository** for this diagnostic only.
3. Upload those four files at the repository root. Do not upload a parent folder,
   environment files, virtual environment, credentials, or unrelated project files.
   Creating the repository and uploading are manual steps; this app does neither.

## Create the temporary Render service

1. In the Render dashboard, select **New → Web Service**.
2. Choose **Public Git Repository** and paste the new repository's public HTTPS
   URL. No private repository integration or GitHub token is required by this app.
3. Select your branch, choose **Python 3**, and leave Root Directory empty when
   these four files are at the repository root.
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

## Remove after testing

In Render, open this service's **Settings**, find **Delete Web Service**, and
confirm deletion after recording the results. This avoids leaving a public
diagnostic running unnecessarily. No paid third-party service is needed.

Official references: [Flask deployment](https://render.com/docs/deploy-flask),
[public repositories](https://render.com/docs/web-services#deploy-your-own-code),
[regions](https://render.com/docs/regions), and
[Python versions](https://render.com/docs/python-version).
