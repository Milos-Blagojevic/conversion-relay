"""Sandbox platforms: in-process stand-ins for Meta and Google that answer the relay's real requests.

`SandboxTransport` replaces HTTP in sandbox mode and routes each request the platform clients make
(Conversions API, Dataset Quality API, Insights, OAuth, Data Manager ingest and status, Google Ads
search) to `Simulator`, which keeps its state in `sim_*` tables of the sandbox database. The demo's
"browser" side calls `meta_pixel` and `google_tag` directly, standing in for the tags on the page.

It models the documented behaviour the demo is about:
- Meta keeps the first of a browser event and a server event with the same event_name and event_id
  received within 48 hours and drops the other. Failing that, a server event that follows a browser
  event with the same event_name and fbp or external_id is dropped. Anything else counts again.
  https://developers.facebook.com/docs/marketing-api/conversions-api/deduplicate-pixel-and-server-events
- Google Ads keeps one conversion per transactionId in a conversion action. A server event whose
  transactionId matches a tag conversion merges into it (and fills in customer data the tag lacked);
  a tag conversion without a transaction ID can not be matched, so the server event counts again.
  https://developers.google.com/data-manager/api/devguides/events/google-ads/online/send-events
- Each platform credits a matched conversion to its own last ad interaction inside its window
  (Meta: 7-day click or 1-day view; Google: 30-day click), whatever the other platform did. That is
  why the two platforms together claim more conversions than there were orders.

Match rates, latency and error rates are invented, but deterministic for a given seed.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
import threading
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlparse

from relay.clock import Clock
from relay.config import Settings
from relay.quality import META_KEY_IDENTIFIERS
from relay.store import Store

SCHEMA = """
CREATE TABLE IF NOT EXISTS sim_journeys (order_ref TEXT PRIMARY KEY, touches TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS sim_meta (
    id INTEGER PRIMARY KEY,
    pixel_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    event_name TEXT NOT NULL,
    event_id TEXT,
    order_ref TEXT,
    received_at REAL NOT NULL,
    event_time REAL NOT NULL,
    fbp TEXT,
    external_id TEXT,
    keys TEXT NOT NULL,
    value REAL,
    counted INTEGER NOT NULL,
    dedup TEXT,
    paired INTEGER NOT NULL DEFAULT 0,
    matched INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sim_meta_id ON sim_meta (pixel_id, event_name, event_id);
CREATE INDEX IF NOT EXISTS sim_meta_order ON sim_meta (order_ref);

CREATE TABLE IF NOT EXISTS sim_google (
    id INTEGER PRIMARY KEY,
    action_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    transaction_id TEXT,
    order_ref TEXT,
    received_at REAL NOT NULL,
    event_time REAL NOT NULL,
    value REAL,
    gclid TEXT,
    user_data INTEGER NOT NULL,
    counted INTEGER NOT NULL,
    merged INTEGER NOT NULL DEFAULT 0,
    matched INTEGER NOT NULL,
    request_id TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS sim_google_tid ON sim_google (action_id, transaction_id);
CREATE INDEX IF NOT EXISTS sim_google_order ON sim_google (order_ref);

CREATE TABLE IF NOT EXISTS sim_requests (
    request_id TEXT PRIMARY KEY,
    received_at REAL NOT NULL,
    record_count INTEGER NOT NULL,
    errors TEXT NOT NULL
);
"""

DEDUP_WINDOW = 48 * 3600
META_MAX_AGE = 7 * 86400
META_CLICK_WINDOW = 7 * 86400
META_VIEW_WINDOW = 86400
GOOGLE_CLICK_WINDOW = 30 * 86400
GOOGLE_MAX_AGE = 90 * 86400
GOOGLE_PROCESSING_SECONDS = 20
META_ACTION_TYPES = ("offsite_conversion.fb_pixel_purchase", "purchase")

# Chance each Meta match key alone finds the person; keys combine as independent tries.
META_MATCH_WEIGHTS = {
    "em": 0.55, "ph": 0.40, "fbc": 0.60, "external_id": 0.20, "fbp": 0.30,
    "fn": 0.05, "ln": 0.05, "zp": 0.05, "ct": 0.03, "st": 0.01, "country": 0.01,
}
META_IP_AND_UA = 0.15
GOOGLE_USER_DATA_MATCH = 0.55
GOOGLE_IP_ONLY_MATCH = 0.10
GOOGLE_TAG_NO_CLICK_MATCH = 0.30


def uniform(*parts: object) -> float:
    """A stable number in [0, 1) for the given parts: the same event always gets the same draw."""
    digest = hashlib.sha256("|".join(map(str, parts)).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def meta_match_probability(keys: set[str]) -> float:
    miss = 1.0
    for key, weight in META_MATCH_WEIGHTS.items():
        if key in keys:
            miss *= 1 - weight
    if {"client_ip_address", "client_user_agent"} <= keys:
        miss *= 1 - META_IP_AND_UA
    return 1 - miss


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()


def _parse_time(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _meta_error(message: str, code: int, subcode: int | None = None, user_msg: str | None = None) -> dict:
    error = {"message": message, "type": "OAuthException", "code": code, "fbtrace_id": f"Sbx{uuid.uuid4().hex[:12]}"}
    if subcode:
        error["error_subcode"] = subcode
    if user_msg:
        error["error_user_msg"] = user_msg
    return {"error": error}


def _google_error(code: int, status: str, message: str) -> dict:
    return {"error": {"code": code, "message": message, "status": status}}


class Simulator:
    def __init__(self, store: Store, clock: Clock, settings: Settings) -> None:
        assert settings.meta and settings.google, "sandbox settings carry both platforms"
        self.store = store
        self.clock = clock
        self.meta = settings.meta
        self.google = settings.google
        self._tokens: set[str] = set()
        self._lock = threading.Lock()
        with store.lock:
            store.conn.executescript(SCHEMA)

    # Ground truth the demo writes: each order's ad interactions.

    def add_journey(self, order_ref: str, touches: list[dict]) -> None:
        with self.store.tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sim_journeys (order_ref, touches) VALUES (?, ?)", (order_ref, json.dumps(touches))
            )

    def _journeys(self) -> dict[str, list[dict]]:
        return {row["order_ref"]: json.loads(row["touches"]) for row in self.store.all("SELECT * FROM sim_journeys")}

    # Meta

    def meta_pixel(self, *, event_name: str, event_id: str | None, order_ref: str, fbp: str | None,
                   fbc: str | None, value: float | None, event_time: float) -> dict:
        """A Meta Pixel hit from the page. The pixel always has the browser's IP, user agent and cookies."""
        keys = {"client_ip_address", "client_user_agent"} | {k for k, v in (("fbp", fbp), ("fbc", fbc)) if v}
        return self._meta_record("browser", event_name, event_id, order_ref, fbp, None, keys, value, event_time)

    def _meta_record(self, channel: str, event_name: str, event_id: str | None, order_ref: str | None,
                     fbp: str | None, external_id: str | None, keys: set[str], value: float | None,
                     event_time: float) -> dict:
        now = self.clock.now()
        pixel = self.meta.pixel_id
        counted, dedup, twin = 1, None, None
        with self._lock:
            if event_id:
                twin = self.store.one(
                    "SELECT id FROM sim_meta WHERE pixel_id = ? AND event_name = ? AND event_id = ? AND channel != ?"
                    " AND counted = 1 AND received_at >= ? LIMIT 1",
                    (pixel, event_name, event_id, channel, now - DEDUP_WINDOW),
                )
                if twin:
                    counted, dedup = 0, "event_id"
            if counted and channel == "server":
                for column, value_ in (("fbp", fbp), ("external_id", external_id)):
                    if not value_:
                        continue
                    twin = self.store.one(
                        f"SELECT id FROM sim_meta WHERE pixel_id = ? AND event_name = ? AND channel = 'browser'"
                        f" AND {column} = ? AND counted = 1 AND paired = 0 AND received_at >= ? ORDER BY id LIMIT 1",
                        (pixel, event_name, value_, now - DEDUP_WINDOW),
                    )
                    if twin:
                        counted, dedup = 0, column
                        break
            matched = int(uniform("meta", channel, event_name, event_id, order_ref) < meta_match_probability(keys))
            with self.store.tx() as conn:
                if twin:
                    conn.execute("UPDATE sim_meta SET paired = 1 WHERE id = ?", (twin["id"],))
                conn.execute(
                    "INSERT INTO sim_meta (pixel_id, channel, event_name, event_id, order_ref, received_at, event_time,"
                    " fbp, external_id, keys, value, counted, dedup, paired, matched)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (pixel, channel, event_name, event_id, order_ref, now, event_time, fbp, external_id,
                     ",".join(sorted(keys)), value, counted, dedup, int(bool(twin)), matched),
                )
        return {"counted": bool(counted), "dedup": dedup}

    def meta_events(self, pixel_id: str, request: dict) -> tuple[int, dict]:
        """POST /{version}/{pixel_id}/events"""
        if request.get("access_token") != self.meta.access_token:
            return 400, _meta_error("Invalid OAuth access token - Cannot parse access token", 190)
        if pixel_id != self.meta.pixel_id:
            return 400, _meta_error(f"Unsupported post request. Object with ID '{pixel_id}' does not exist", 100, 33)
        events = request.get("data")
        if not isinstance(events, list) or not 1 <= len(events) <= 1000:
            return 400, _meta_error("Invalid parameter: data must hold 1 to 1,000 events", 100)
        now = self.clock.now()
        for event in events:
            missing = [k for k in ("event_name", "event_time", "action_source", "user_data") if not event.get(k)]
            if missing:
                return 400, _meta_error(f"Invalid parameter: missing {', '.join(missing)}", 100)
            if now - event["event_time"] > META_MAX_AGE:
                return 400, _meta_error(
                    "Invalid parameter", 100, 2804003,
                    "The timestamp for this event is too far in the past. Events need to be sent within 7 days.",
                )
            if event["action_source"] == "website" and not event["user_data"].get("client_user_agent"):
                return 400, _meta_error(
                    "Invalid parameter", 100, 2804050, "client_user_agent is required for website events."
                )
        for event in events:
            user = event["user_data"]
            custom = event.get("custom_data") or {}
            self._meta_record(
                "server",
                event["event_name"],
                event.get("event_id"),
                custom.get("order_id") or event.get("event_id"),
                user.get("fbp"),
                (user.get("external_id") or [None])[0],
                set(user),
                custom.get("value"),
                float(event["event_time"]),
            )
        return 200, {"events_received": len(events), "messages": [], "fbtrace_id": f"Sbx{uuid.uuid4().hex[:12]}"}

    def meta_quality(self, params: dict) -> tuple[int, dict]:
        """GET /{version}/dataset_quality: the last 7 days of server events, per event name."""
        if params.get("access_token") != self.meta.access_token:
            return 400, _meta_error("Invalid OAuth access token - Cannot parse access token", 190)
        if params.get("dataset_id") != self.meta.pixel_id:
            return 400, _meta_error("Invalid parameter: dataset_id", 100)
        since = self.clock.now() - 7 * 86400
        rows = self.store.all("SELECT * FROM sim_meta WHERE pixel_id = ? AND received_at >= ?", (self.meta.pixel_id, since))
        web = []
        for name in sorted({r["event_name"] for r in rows}):
            server = [r for r in rows if r["event_name"] == name and r["channel"] == "server"]
            browser = [r for r in rows if r["event_name"] == name and r["channel"] == "browser"]
            if server:
                web.append(self._meta_event_quality(name, server, browser, rows))
        return 200, {"web": web}

    def _meta_event_quality(self, name: str, server: list, browser: list, rows: list) -> dict:
        def pct(part: int, whole: int) -> float:
            return round(100 * part / whole, 1) if whole else 0.0

        keysets = [set(r["keys"].split(",")) for r in server]
        score = sum(meta_match_probability(k) for k in keysets) / len(keysets) * 10
        feedback = [
            {"identifier": identifier, "coverage": {"percentage": pct(sum(key in k for k in keysets), len(keysets))}}
            for key, identifier in META_KEY_IDENTIFIERS.items()
            if any(key in k for k in keysets)
        ]
        diagnostics = []
        fbc_share = pct(sum("fbc" in k for k in keysets), len(keysets))
        if fbc_share < 50:
            diagnostics.append(
                {
                    "name": "Send the fbc click ID with more events",
                    "description": "The fbc click identifier is present on too small a share of events.",
                    "percentage": round(100 - fbc_share, 1),
                    "affected_event_count": sum("fbc" not in k for k in keysets),
                    "total_event_count": len(keysets),
                }
            )
        server_ids = {r["event_id"] for r in server if r["event_id"]}
        browser_paired_by_id = sum(1 for r in browser if r["event_id"] in server_ids)
        server_dedup = {}
        for key in ("fbp", "external_id"):
            server_dedup[key] = sum(1 for r in server if r["dedup"] == key)
        covered = browser_paired_by_id + sum(server_dedup.values())
        delays = sorted(r["received_at"] - r["event_time"] for r in server)
        median = delays[len(delays) // 2]
        frequency = "real_time" if median < 3600 else "hourly" if median < 86400 else "daily"
        browser_counted = sum(1 for r in browser if r["counted"] and r["matched"])
        server_only = sum(1 for r in server if r["counted"] and r["matched"])
        over_2h = sum(1 for d in delays if d > 7200)
        over_24h = sum(1 for d in delays if d > 86400)
        return {
            "event_name": name,
            "event_match_quality": {
                "composite_score": round(score, 1),
                "match_key_feedback": feedback,
                "diagnostics": diagnostics,
            },
            "event_coverage": {"percentage": pct(min(covered, len(browser)), len(browser)), "goal_percentage": 75},
            "dedupe_key_feedback": [
                {
                    "dedupe_key": "event_id",
                    "browser_events_with_dedupe_key": {"percentage": pct(sum(1 for r in browser if r["event_id"]), len(browser))},
                    "server_events_with_dedupe_key": {"percentage": pct(sum(1 for r in server if r["event_id"]), len(server))},
                    "overall_browser_coverage_from_dedupe_key": {"percentage": pct(browser_paired_by_id, len(browser))},
                },
                *(
                    {
                        "dedupe_key": key,
                        "browser_events_with_dedupe_key": {"percentage": pct(sum(1 for r in browser if r[key]), len(browser))},
                        "server_events_with_dedupe_key": {"percentage": pct(sum(1 for r in server if r[key]), len(server))},
                        "overall_browser_coverage_from_dedupe_key": {"percentage": pct(server_dedup[key], len(browser))},
                    }
                    for key in ("fbp", "external_id")
                ),
            ],
            "data_freshness": {"upload_frequency": frequency},
            "event_delay": {
                "events_delayed_over_2h": over_2h,
                "events_delayed_over_24h": over_24h,
                "total_events": len(delays),
                "pct_delayed_over_2h": pct(over_2h, len(delays)),
                "pct_delayed_over_24h": pct(over_24h, len(delays)),
            },
            "acr": {
                "percentage": pct(server_only, browser_counted),
                "description": "Sandbox: conversions reported only through the Conversions API, as a share of"
                " those reported through the Pixel, in the last 7 days.",
            },
        }

    def meta_insights(self, account_id: str, params: dict, base_url: str) -> tuple[int, dict]:
        """GET /{version}/act_{id}/insights at campaign level, one row per day (conversion date)."""
        if params.get("access_token") != self.meta.access_token:
            return 400, _meta_error("Invalid OAuth access token - Cannot parse access token", 190)
        if account_id != self.meta.ad_account_id:
            return 400, _meta_error(f"Unsupported get request. Object with ID 'act_{account_id}' does not exist", 100, 33)
        try:
            window = json.loads(params["time_range"])
            since, until = window["since"], window["until"]
        except (KeyError, ValueError):
            return 400, _meta_error("Invalid parameter: time_range", 100)
        journeys = self._journeys()
        totals: dict[tuple[str, str, str], list[float]] = {}
        for row in self.store.all(
            "SELECT * FROM sim_meta WHERE pixel_id = ? AND event_name = 'Purchase' AND counted = 1 AND matched = 1",
            (self.meta.pixel_id,),
        ):
            day = _day(row["event_time"])
            if not since <= day <= until:
                continue
            touch = _last_touch(journeys.get(row["order_ref"], []), "meta", row["event_time"],
                                {"click": META_CLICK_WINDOW, "view": META_VIEW_WINDOW})
            if touch:
                entry = totals.setdefault((day, touch["campaign_id"], touch["campaign_name"]), [0, 0.0])
                entry[0] += 1
                entry[1] += row["value"] or 0.0
        data = [
            {
                "campaign_id": campaign_id,
                "campaign_name": campaign_name,
                "actions": [{"action_type": t, "value": str(count)} for t in META_ACTION_TYPES],
                "action_values": [{"action_type": t, "value": f"{value:.2f}"} for t in META_ACTION_TYPES],
                "date_start": day,
                "date_stop": day,
            }
            for (day, campaign_id, campaign_name), (count, value) in sorted(totals.items())
        ]
        limit = int(params.get("limit", 25))
        offset = int(params.get("after", 0))
        page = {"data": data[offset:offset + limit], "paging": {}}
        if offset + limit < len(data):
            query = {k: v for k, v in params.items() if k != "after"} | {"after": str(offset + limit)}
            page["paging"]["next"] = f"{base_url}/act_{account_id}/insights?{urlencode(query)}"
        return 200, page

    # Google

    def google_tag(self, *, order_ref: str, transaction_id: str | None, value: float | None, gclid: str | None,
                   event_time: float) -> dict:
        """A Google tag conversion from the page; `gclid` is the one in the _gcl_aw cookie, if any."""
        now = self.clock.now()
        action = self.google.conversion_action_id
        matched = 1 if gclid else int(uniform("gtag", order_ref) < GOOGLE_TAG_NO_CLICK_MATCH)
        with self._lock:
            twin = None
            if transaction_id:
                twin = self.store.one(
                    "SELECT id FROM sim_google WHERE action_id = ? AND transaction_id = ? AND counted = 1 LIMIT 1",
                    (action, transaction_id),
                )
            with self.store.tx() as conn:
                conn.execute(
                    "INSERT INTO sim_google (action_id, channel, transaction_id, order_ref, received_at, event_time,"
                    " value, gclid, user_data, counted, merged, matched) VALUES (?, 'tag', ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
                    (action, transaction_id, order_ref, now, event_time, value, gclid, int(not twin), int(bool(twin)),
                     matched),
                )
        return {"counted": not twin}

    def google_token(self, form: dict | None) -> tuple[int, dict]:
        form = form or {}
        if form.get("grant_type") != "refresh_token" or form.get("refresh_token") != self.google.refresh_token:
            return 400, {"error": "invalid_grant", "error_description": "Bad Request"}
        token = f"sandbox-access-{uuid.uuid4().hex[:16]}"
        self._tokens.add(token)
        return 200, {"access_token": token, "expires_in": 3599, "token_type": "Bearer"}

    def _authorized(self, headers: dict | None) -> bool:
        auth = (headers or {}).get("Authorization", "")
        return auth.startswith("Bearer ") and auth[7:] in self._tokens

    def google_ingest(self, request: dict, headers: dict | None) -> tuple[int, dict]:
        """POST /v1/events:ingest"""
        if not self._authorized(headers):
            return 401, _google_error(401, "UNAUTHENTICATED", "Request had invalid authentication credentials.")
        destinations = request.get("destinations") or []
        if len(destinations) != 1:
            return 400, _google_error(400, "INVALID_ARGUMENT", "The sandbox takes exactly one destination.")
        destination = destinations[0]
        if (destination.get("operatingAccount", {}).get("accountId") != self.google.customer_id
                or destination.get("productDestinationId") != self.google.conversion_action_id):
            return 403, _google_error(403, "PERMISSION_DENIED", "The caller does not have permission")
        events = request.get("events") or []
        if not 1 <= len(events) <= 2000:
            return 400, _google_error(400, "INVALID_ARGUMENT", "events must hold 1 to 2,000 events.")
        for index, event in enumerate(events):
            problem = _google_invalid(event, request.get("encoding"))
            if problem:
                return 400, _google_error(400, "INVALID_ARGUMENT", f"events[{index}]: {problem}")
        if request.get("validateOnly"):
            return 200, {}
        request_id = str(uuid.uuid4())
        errors: dict[str, list[int]] = {}
        for index, event in enumerate(events):
            reason = self._google_record(event, request_id)
            if reason:
                errors.setdefault(reason, []).append(index)
        with self.store.tx() as conn:
            conn.execute(
                "INSERT INTO sim_requests (request_id, received_at, record_count, errors) VALUES (?, ?, ?, ?)",
                (request_id, self.clock.now(), len(events), json.dumps(errors)),
            )
        return 200, {"requestId": request_id}

    def _google_record(self, event: dict, request_id: str) -> str | None:
        """Process one ingested event; returns the processing error reason, if it fails."""
        now = self.clock.now()
        action = self.google.conversion_action_id
        event_time = _parse_time(event["eventTimestamp"])
        transaction_id = event["transactionId"]
        gclid = event.get("adIdentifiers", {}).get("gclid")
        user_data = bool(event.get("userData", {}).get("userIdentifiers"))
        if now - event_time > GOOGLE_MAX_AGE:
            return "PROCESSING_ERROR_REASON_EVENT_TOO_OLD"
        if event.get("consent", {}).get("adUserData") == "CONSENT_DENIED":
            return "PROCESSING_ERROR_REASON_DENIED_CONSENT"
        if gclid and len(gclid) < 20:
            return "PROCESSING_ERROR_REASON_INVALID_GCLID"
        if gclid:
            matched = 1
        elif user_data:
            matched = int(uniform("ec", transaction_id) < GOOGLE_USER_DATA_MATCH)
        else:
            matched = int(uniform("ip", transaction_id) < GOOGLE_IP_ONLY_MATCH)
        with self._lock:
            twin = self.store.one(
                "SELECT * FROM sim_google WHERE action_id = ? AND transaction_id = ? AND counted = 1 LIMIT 1",
                (action, transaction_id),
            )
            if twin and twin["channel"] == "api":
                return "PROCESSING_ERROR_REASON_DUPLICATE_TRANSACTION_ID"
            with self.store.tx() as conn:
                if twin:
                    # Merged into the tag conversion: customer data fills in what the tag lacked.
                    conn.execute(
                        "UPDATE sim_google SET user_data = MAX(user_data, ?), matched = MAX(matched, ?) WHERE id = ?",
                        (int(user_data), matched if not twin["user_data"] else 0, twin["id"]),
                    )
                conn.execute(
                    "INSERT INTO sim_google (action_id, channel, transaction_id, order_ref, received_at, event_time,"
                    " value, gclid, user_data, counted, merged, matched, request_id)"
                    " VALUES (?, 'api', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (action, transaction_id, transaction_id, now, event_time, event.get("conversionValue"), gclid,
                     int(user_data), int(not twin), int(bool(twin)), matched, request_id),
                )
        return None

    def google_status(self, params: dict, headers: dict | None) -> tuple[int, dict]:
        """GET /v1/requestStatus:retrieve"""
        if not self._authorized(headers):
            return 401, _google_error(401, "UNAUTHENTICATED", "Request had invalid authentication credentials.")
        row = self.store.one("SELECT * FROM sim_requests WHERE request_id = ?", (params.get("requestId"),))
        if row is None:
            return 404, _google_error(404, "NOT_FOUND", "Request not found.")
        destination = {
            "operatingAccount": {"accountType": "GOOGLE_ADS", "accountId": self.google.customer_id},
            "productDestinationId": self.google.conversion_action_id,
        }
        if self.clock.now() - row["received_at"] < GOOGLE_PROCESSING_SECONDS:
            return 200, {"requestStatusPerDestination": [{"destination": destination, "requestStatus": "PROCESSING"}]}
        errors = json.loads(row["errors"])
        failed = sum(len(indices) for indices in errors.values())
        status = "SUCCESS" if not failed else "FAILED" if failed == row["record_count"] else "PARTIAL_SUCCESS"
        entry = {
            "destination": destination,
            "requestStatus": status,
            "eventsIngestionStatus": {"recordCount": str(row["record_count"])},
            "warningInfo": {"warningCounts": []},
        }
        if errors:
            entry["errorInfo"] = {
                "errorCounts": [
                    {"recordCount": str(len(indices)), "reason": reason, "recordIndices": indices}
                    for reason, indices in errors.items()
                ]
            }
        return 200, {"requestStatusPerDestination": [entry]}

    def google_search(self, customer_id: str, request: dict, headers: dict | None) -> tuple[int, dict]:
        """POST /{version}/customers/{id}/googleAds:search, for the relay's attribution query only."""
        if not self._authorized(headers) or (headers or {}).get("developer-token") != self.google.developer_token:
            return 401, _google_error(401, "UNAUTHENTICATED", "Request had invalid authentication credentials.")
        if customer_id != self.google.customer_id:
            return 403, _google_error(403, "PERMISSION_DENIED", "User doesn't have permission to access customer.")
        query = request.get("query", "")
        dates = re.search(r"segments\.date BETWEEN '([\d-]+)' AND '([\d-]+)'", query)
        action = re.search(r"conversionActions/(\d+)'", query)
        if not dates or not action:
            return 400, _google_error(400, "INVALID_ARGUMENT", "The sandbox answers only the relay's report query.")
        since, until = dates.groups()
        journeys = self._journeys()
        totals: dict[tuple[str, str, str], list[float]] = {}
        for row in self.store.all(
            "SELECT * FROM sim_google WHERE action_id = ? AND counted = 1 AND matched = 1", (action.group(1),)
        ):
            day = _day(row["event_time"])
            if not since <= day <= until:
                continue
            touch = _last_touch(journeys.get(row["order_ref"], []), "google", row["event_time"],
                                {"click": GOOGLE_CLICK_WINDOW})
            if touch:
                entry = totals.setdefault((day, touch["campaign_id"], touch["campaign_name"]), [0, 0.0])
                entry[0] += 1
                entry[1] += row["value"] or 0.0
        resource = f"customers/{customer_id}/conversionActions/{action.group(1)}"
        results = [
            {
                "campaign": {"resourceName": f"customers/{customer_id}/campaigns/{cid}", "id": cid, "name": name},
                "metrics": {"allConversionsByConversionDate": count, "allConversionsValueByConversionDate": value},
                "segments": {"date": day, "conversionAction": resource},
            }
            for (day, cid, name), (count, value) in sorted(totals.items())
        ]
        return 200, {"results": results, "fieldMask": "segments.date,segments.conversionAction,campaign.id,"
                      "campaign.name,metrics.allConversionsByConversionDate,metrics.allConversionsValueByConversionDate"}

    # What the demo page shows per order

    def order_outcomes(self, order_refs: list[str]) -> dict[str, dict]:
        if not order_refs:
            return {}
        marks = ",".join("?" for _ in order_refs)
        out: dict[str, dict] = {ref: {"meta": [], "google": []} for ref in order_refs}
        for row in self.store.all(f"SELECT * FROM sim_meta WHERE order_ref IN ({marks}) ORDER BY id", order_refs):
            out[row["order_ref"]]["meta"].append(
                {"channel": row["channel"], "counted": bool(row["counted"]), "dedup": row["dedup"]}
            )
        for row in self.store.all(f"SELECT * FROM sim_google WHERE order_ref IN ({marks}) ORDER BY id", order_refs):
            out[row["order_ref"]]["google"].append(
                {"channel": row["channel"], "counted": bool(row["counted"]), "merged": bool(row["merged"])}
            )
        return out


def _last_touch(touches: list[dict], channel: str, event_time: float, windows: dict[str, float]) -> dict | None:
    """The platform's own credit: its last click in the click window, else its last view in the view window."""
    for kind in ("click", "view"):
        if kind not in windows:
            continue
        eligible = [
            t for t in touches
            if t["channel"] == channel and t["kind"] == kind and 0 <= event_time - t["at"] <= windows[kind]
        ]
        if eligible:
            return max(eligible, key=lambda t: t["at"])
    return None


def _google_invalid(event: dict, encoding: str | None) -> str | None:
    if not event.get("transactionId"):
        return "transaction_id is required for multi-source conversions"
    try:
        _parse_time(event.get("eventTimestamp", ""))
    except ValueError:
        return "event_timestamp is not an RFC 3339 timestamp"
    ad = event.get("adIdentifiers", {})
    has_id = (
        any(ad.get(k) for k in ("gclid", "gbraid", "wbraid", "sessionAttributes"))
        or ad.get("landingPageDeviceInfo", {}).get("ipAddress")
        or event.get("userData", {}).get("userIdentifiers")
        or event.get("eventDeviceInfo", {}).get("ipAddress")
    )
    if not has_id:
        return "at least one of ad_identifiers, user_data or event_device_info.ip_address is required"
    if event.get("userData") and encoding not in ("HEX", "BASE64"):
        return "encoding is required for user data"
    return None


class SimResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


class SandboxTransport:
    """Routes the platform clients' HTTP calls to the simulator, with made-up latency and failures."""

    # (median milliseconds, lognormal spread) per platform
    LATENCY = {"meta": (110.0, 0.45), "google": (240.0, 0.5)}
    FAILURE_RATE = 0.02

    def __init__(self, simulator: Simulator, clock: Clock, seed: int = 11, failure_rate: float = FAILURE_RATE) -> None:
        self.sim = simulator
        self.clock = clock
        self.rng = random.Random(seed)
        self.failure_rate = failure_rate

    def _wait(self, platform: str, can_fail: bool) -> SimResponse | None:
        median, spread = self.LATENCY[platform]
        self.clock.sleep(median * math.exp(self.rng.gauss(0, spread)) / 1000)
        if can_fail and self.rng.random() < self.failure_rate:
            if platform == "meta":
                error = _meta_error("An unexpected error has occurred. Please retry your request later.", 2)
                error["error"]["is_transient"] = True
                return SimResponse(500, error)
            return SimResponse(503, _google_error(503, "UNAVAILABLE", "The service is currently unavailable."))
        return None

    def request(self, method, url, *, params=None, json=None, data=None, headers=None):
        parts = urlparse(url)
        query = dict(parse_qsl(parts.query)) | {k: str(v) for k, v in (params or {}).items()}
        host, path = parts.netloc, parts.path
        if host == "graph.facebook.com":
            segments = path.strip("/").split("/")
            if method == "POST" and len(segments) == 3 and segments[2] == "events":
                return self._wait("meta", True) or SimResponse(*self.sim.meta_events(segments[1], json or {}))
            if method == "GET" and segments[1:] == ["dataset_quality"]:
                return self._wait("meta", False) or SimResponse(*self.sim.meta_quality(query))
            if method == "GET" and len(segments) == 3 and segments[1].startswith("act_") and segments[2] == "insights":
                base = f"{parts.scheme}://{host}/{segments[0]}"
                return self._wait("meta", False) or SimResponse(*self.sim.meta_insights(segments[1][4:], query, base))
        elif host == "oauth2.googleapis.com" and method == "POST" and path == "/token":
            return SimResponse(*self.sim.google_token(data))
        elif host == "datamanager.googleapis.com":
            if method == "POST" and path == "/v1/events:ingest":
                return self._wait("google", True) or SimResponse(*self.sim.google_ingest(json or {}, headers))
            if method == "GET" and path == "/v1/requestStatus:retrieve":
                return self._wait("google", False) or SimResponse(*self.sim.google_status(query, headers))
        elif host == "googleads.googleapis.com":
            match = re.fullmatch(r"/v\d+/customers/(\d+)/googleAds:search", path)
            if method == "POST" and match:
                return self._wait("google", False) or SimResponse(*self.sim.google_search(match[1], json or {}, headers))
        return SimResponse(404, {"error": {"message": f"the sandbox has no route for {method} {url}"}})
