# AGENTS.md

Python 3.11+ FastAPI service that relays a shop's conversions server-side to the Meta Conversions API and
Google (Data Manager API multi-source conversions, i.e. enhanced conversions), and monitors event match
quality, latency, coverage and internal vs platform-reported attribution. Sandbox mode simulates both
platforms in-process for demos. `README.md` covers setup, the event format, the dashboard and the
sandbox model; read it before changing a feature.

## Layout

|Path|Role|
|---|---|
|`relay/__main__.py`|CLI: `serve`, `seed`, `report`, `refresh`|
|`relay/config.py`|The only module that reads the environment (`from_env`); sandbox placeholder accounts|
|`relay/models.py`|The accepted event (`EventIn`), batch and beacon models|
|`relay/normalize.py`|Per-platform normalisation and SHA-256 hashing (`prepare`); raw customer data stops here|
|`relay/runtime.py`|`Relay`: wiring (`build`) and idempotent ingest|
|`relay/dispatcher.py`|Batching, retries, isolation of rejected batches; `Skip`, `Outcome`, `SendResult`|
|`relay/meta.py`|Conversions API delivery, Dataset Quality API, Insights|
|`relay/google.py`|OAuth tokens, Data Manager ingest and request status, Google Ads reporting (GAQL)|
|`relay/transport.py`|The HTTP seam: `RequestsTransport` (live) or `simulator.SandboxTransport`|
|`relay/monitor.py`|Background pulls of platform data; Google per-record rejections|
|`relay/store.py`|SQLite schema and queries; a database keeps the mode it was created in|
|`relay/metrics.py`|Every dashboard number; `relay/charts.py` draws them as inline SVG|
|`relay/attribution.py`, `relay/quality.py`|Internal last-click channel; the relay's match-quality estimate and key labels|
|`relay/simulator.py`|Sandbox Meta and Google: dedup, processing, attribution, Dataset Quality and reports|
|`relay/demo.py`|Simulated orders, history seeding, the live demo page's orders|
|`relay/app.py`, `relay/templates/`, `relay/static/`|FastAPI routes, access guard, dashboard, events and demo pages|
|`tests/`|`test_<area>.py`; `support.py` holds settings, URLs and the event factory|

## Commands

```bash
pip install -r requirements-dev.txt
python -m pytest
python -m relay seed && python -m relay serve --sandbox   # free, offline demo
```

CI runs the suite on Python 3.11 and 3.13. Keep the code 3.11-compatible.

Ask before running in live mode: `serve` sends real conversions to Meta and Google, `refresh` and
`report` read the owner's ad accounts. Sandbox mode makes no network calls.

## Conventions

- All platform HTTP goes through the `Transport` the relay was built with, so sandbox mode stays
  offline and tests can mock it. `requests` only, with `transport.TIMEOUT_SECONDS`.
- Normalisation follows each platform's published rules and they differ (Gmail dots, phone format,
  punctuation in names). Change them only with the doc open; `tests/test_normalize.py` pins Google's own
  example hashes.
- Raw customer data is never stored. Events keep key names only; a delivery's payload is deleted once the
  platform accepts it or the relay gives up. Never log payloads.
- Delivery is at-least-once and idempotent on `(event_name, event_id)`. Both platforms deduplicate a resend;
  keep `event_id` (Meta) and `transactionId` (Google) stable across retries.
- A request the platform rejects as a whole goes back as `isolate`, never `failed`: one bad event must not
  fail the others. Meta events older than 7 days are failed before sending for the same reason.
- The simulator models documented platform behaviour; when changing a rule, cite the doc in its docstring
  and in README's sandbox section. Its numbers (match rates, latency, failures) are invented and must stay
  deterministic for a seed.
- Every dashboard number comes from `metrics.collect`, which `/api/metrics` and `report` share.
- Access: `/v1/events*` needs `RELAY_API_KEY` when set; `/v1/beacons` and `/healthz` are public;
  everything else needs `DASHBOARD_PASSWORD` when set, else only loopback hosts. Non-GET dashboard requests
  must be same-origin. `serve` refuses a non-loopback host without both secrets; keep it that way.
- Dependencies are pinned in `requirements.txt` and `requirements-dev.txt`.

## Tests

- No network and no keys: the autouse `http` fixture wraps every test in `responses`, so an unmocked call
  fails. Mock Meta and Google at the HTTP level with their real URLs and wire formats.
- `live` and `sandbox` fixtures build a relay on a `ManualClock`; drive delivery with
  `relay.dispatcher.run_due()` and the monitor with `relay.monitor.refresh()`, not the threads.
- Test behaviour a user would notice (what is sent, counted, retried, rejected, shown), not wiring.

## Docs

Update `README.md` in the same change when behaviour, commands, settings or the dashboard change, and
`.env.example` when a setting changes.
