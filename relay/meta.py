"""Meta: Conversions API delivery, Dataset Quality API (event match quality) and Insights (reported conversions).

Conversions API: https://developers.facebook.com/docs/marketing-api/conversions-api/using-the-api
Dataset Quality API: https://developers.facebook.com/docs/marketing-api/conversions-api/dataset-quality-api
"""

from __future__ import annotations

import json
import re
from datetime import date

import requests

from relay.clock import Clock
from relay.config import MetaSettings
from relay.dispatcher import Outcome, SendResult, Skip, refused
from relay.models import EventIn
from relay.normalize import Prepared
from relay.transport import PlatformError, Transport, body

NAME = "meta"
# Meta rejects the whole request when any event is older than 7 days; leave a margin for the trip.
MAX_EVENT_AGE_SECONDS = 7 * 86400 - 600
# Graph API error codes that mean "try again later": unknown, service, rate limits, temporarily blocked.
TRANSIENT_CODES = frozenset({1, 2, 4, 17, 32, 341, 368, 613, 80004})
# Token or permission problems: fixing the configuration should let the queued events through.
AUTH_CODES = frozenset({10, 102, 190, *range(200, 300)})
# Code 100 subcode 33: "object does not exist, cannot be loaded due to missing permissions", which is
# what a token not assigned to the pixel gets.
AUTH_SUBCODES = frozenset({33})
_TOKEN = re.compile(r"(access_token=)[^&\s'\"]+")


def redact(text: str) -> str:
    """Monitoring URLs carry the access token; keep it out of error text, the database and the page."""
    return _TOKEN.sub(r"\1[redacted]", text)


QUALITY_FIELDS = (
    "web{event_name,"
    "event_match_quality{composite_score,match_key_feedback{identifier,coverage{percentage}},diagnostics},"
    "event_coverage{percentage,goal_percentage},"
    "dedupe_key_feedback{dedupe_key,browser_events_with_dedupe_key{percentage},"
    "server_events_with_dedupe_key{percentage},overall_browser_coverage_from_dedupe_key{percentage}},"
    "data_freshness{upload_frequency},"
    "event_delay{events_delayed_over_2h,events_delayed_over_24h,total_events,pct_delayed_over_2h,pct_delayed_over_24h},"
    "acr{percentage,description}}"
)


class MetaDestination:
    name = NAME
    batch_size = 500  # the API takes up to 1,000 events per request

    def __init__(self, settings: MetaSettings, transport: Transport, clock: Clock) -> None:
        self.settings = settings
        self.transport = transport
        self.clock = clock

    def _url(self, path: str) -> str:
        return f"{self.settings.graph_url}/{self.settings.api_version}/{path}"

    def build(self, event: EventIn, prepared: Prepared) -> dict | Skip | None:
        if event.consent.ad_user_data is False:
            return Skip("consent")
        if not prepared.meta_user_data:
            return Skip("no identifiers")
        if event.action_source == "website" and not prepared.user_agent:
            # Meta rejects website events without client_user_agent (and with it the whole batch).
            return Skip("website event without user agent")
        if event.action_source == "website" and not event.event_source_url:
            return Skip("website event without event_source_url")
        if event.action_source == "app":
            # App events need app_data (device and app details), which this relay does not collect.
            return Skip("app events are not supported")
        payload: dict = {
            "event_name": event.event_name,
            "event_time": int(event.event_time.timestamp()),
            "event_id": event.event_id,
            "action_source": event.action_source,
            "user_data": prepared.meta_user_data,
        }
        if event.event_source_url:
            payload["event_source_url"] = event.event_source_url
        custom: dict = {}
        if event.value is not None:
            custom["value"] = event.value
        if event.currency:
            custom["currency"] = event.currency
        if event.order_id:
            custom["order_id"] = event.order_id
        if custom:
            payload["custom_data"] = custom
        return payload

    def send(self, payloads: list[dict]) -> SendResult:
        now = self.clock.now()
        fresh = [i for i, p in enumerate(payloads) if now - p["event_time"] <= MAX_EVENT_AGE_SECONDS]
        outcomes = [Outcome("failed", "event_time older than 7 days")] * len(payloads)
        if not fresh:
            return SendResult(None, outcomes, error="all events older than 7 days")
        request = {"data": [payloads[i] for i in fresh], "access_token": self.settings.access_token}
        if self.settings.test_event_code:
            request["test_event_code"] = self.settings.test_event_code
        try:
            response = self.transport.request("POST", self._url(f"{self.settings.pixel_id}/events"), json=request)
        except requests.exceptions.InvalidJSONError as exc:
            # Something in an event can not be sent as JSON: find it, do not retry the batch forever.
            return _fill(outcomes, fresh, refused(len(fresh), f"not JSON-serialisable: {exc}"), None, str(exc))
        except requests.RequestException as exc:
            return _fill(outcomes, fresh, Outcome("retry", f"network: {redact(str(exc))}"), None, redact(str(exc)))
        data = body(response)
        if response.status_code == 200:
            result = _fill(outcomes, fresh, Outcome("sent"), 200, None)
            result.platform_ref = data.get("fbtrace_id")
            return result
        error = data.get("error", {}) if isinstance(data, dict) else {}
        message = error.get("error_user_msg") or error.get("message") or response.text[:300]
        code = error.get("code")
        if response.status_code >= 500 or response.status_code == 429 or code in TRANSIENT_CODES or error.get(
            "is_transient"
        ):
            outcome = Outcome("retry", f"meta {response.status_code}: {message}")
        elif code in AUTH_CODES or error.get("error_subcode") in AUTH_SUBCODES:
            outcome = Outcome("retry", f"meta auth/permission ({code}): {message}")
        else:
            outcome = refused(len(fresh), f"meta {response.status_code}: {message}")
        result = _fill(outcomes, fresh, outcome, response.status_code, message)
        result.platform_ref = error.get("fbtrace_id")
        return result

    # Monitoring

    def _get(self, url: str, params: dict | None) -> dict:
        try:
            response = self.transport.request("GET", url, params=params)
        except requests.RequestException as exc:
            raise PlatformError(f"Meta: {redact(str(exc))}") from exc
        data = body(response)
        if response.status_code != 200:
            error = data.get("error", {}) if isinstance(data, dict) else {}
            detail = error.get("message") or response.text[:300]
            raise PlatformError(f"Meta {response.status_code}: {redact(detail)}")
        return data

    def fetch_quality(self) -> dict:
        """Event match quality, coverage, dedup keys, freshness and delay per event name (last 7 days)."""
        data = self._get(
            self._url("dataset_quality"),
            {"dataset_id": self.settings.pixel_id, "access_token": self.settings.access_token, "fields": QUALITY_FIELDS},
        )
        return {"events": {row.get("event_name", "?"): row for row in data.get("web", [])}}

    def fetch_report(self, since: date, until: date) -> list[dict]:
        """Conversions Meta attributes to each campaign per day, dated by conversion time."""
        if not self.settings.ad_account_id:
            raise PlatformError("META_AD_ACCOUNT_ID is not set")
        params: dict | None = {
            "level": "campaign",
            "time_increment": 1,
            "time_range": json.dumps({"since": since.isoformat(), "until": until.isoformat()}),
            "fields": "campaign_id,campaign_name,actions,action_values",
            "action_report_time": "conversion",
            "use_unified_attribution_setting": "true",
            "limit": 500,
            "access_token": self.settings.access_token,
        }
        url = self._url(f"act_{self.settings.ad_account_id}/insights")
        rows: list[dict] = []
        while url:
            data = self._get(url, params)
            for row in data.get("data", []):
                conversions = _action(row.get("actions"), self.settings.action_type)
                value = _action(row.get("action_values"), self.settings.action_type)
                if conversions or value:
                    rows.append(
                        {
                            "day": row["date_start"],
                            "campaign_id": str(row.get("campaign_id", "")),
                            "campaign_name": row.get("campaign_name"),
                            "conversions": conversions,
                            "value": value,
                        }
                    )
            # `next` is a complete URL that already carries the token and the cursor.
            url, params = data.get("paging", {}).get("next"), None
        return rows


def _action(actions: list[dict] | None, action_type: str) -> float:
    return sum(float(a.get("value", 0)) for a in actions or [] if a.get("action_type") == action_type)


def _fill(outcomes: list[Outcome], indices: list[int], outcome: Outcome, status: int | None,
          error: str | None) -> SendResult:
    filled = list(outcomes)
    for i in indices:
        filled[i] = outcome
    return SendResult(status, filled, error=error)
