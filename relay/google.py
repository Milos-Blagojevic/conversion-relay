"""Google: enhanced conversions through the Data Manager API, its processing diagnostics, and the
conversions Google Ads reports per campaign.

Since June 2026 Google takes server-side conversion data through the Data Manager API, not the
Google Ads API upload services. The relay sends each conversion as a multi-source event: the same
`transactionId` as the Google tag's `transaction_id`, so Google deduplicates the two and the
server event adds hashed customer data (enhanced conversions) or recovers a conversion the tag missed.

Ingest: https://developers.google.com/data-manager/api/devguides/events/google-ads/online/send-events
Diagnostics: https://developers.google.com/data-manager/api/devguides/diagnostics
Reporting (Google Ads API): https://developers.google.com/google-ads/api/docs/conversions/reporting
"""

from __future__ import annotations

import threading
from datetime import date, datetime, timezone

import requests

from relay.clock import Clock
from relay.config import GoogleSettings
from relay.dispatcher import Outcome, SendResult, Skip, refused
from relay.models import EventIn
from relay.normalize import Prepared
from relay.transport import PlatformError, Transport, body

NAME = "google"
EVENT_SOURCES = {"website": "WEB", "app": "APP"}
TERMINAL_STATUSES = frozenset({"SUCCESS", "PARTIAL_SUCCESS", "FAILED"})
# Processing errors that mean Google already holds the conversion (a resend after a timeout).
ALREADY_RECORDED = frozenset({"PROCESSING_ERROR_REASON_DUPLICATE_TRANSACTION_ID"})


class TokenSource:
    """OAuth access tokens from a refresh token, cached until a minute before they expire."""

    def __init__(self, settings: GoogleSettings, transport: Transport, clock: Clock) -> None:
        self.settings = settings
        self.transport = transport
        self.clock = clock
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def token(self) -> str:
        with self._lock:
            if self._token and self.clock.now() < self._expires_at - 60:
                return self._token
            try:
                response = self.transport.request(
                    "POST",
                    self.settings.token_url,
                    data={
                        "client_id": self.settings.client_id,
                        "client_secret": self.settings.client_secret,
                        "refresh_token": self.settings.refresh_token,
                        "grant_type": "refresh_token",
                    },
                )
            except requests.RequestException as exc:
                raise PlatformError(f"Google OAuth: {exc}") from exc
            data = body(response)
            if response.status_code != 200 or "access_token" not in data:
                detail = data.get("error_description") or data.get("error") or response.text[:200]
                raise PlatformError(f"Google OAuth {response.status_code}: {detail}")
            self._token = data["access_token"]
            self._expires_at = self.clock.now() + float(data.get("expires_in", 3600))
            return self._token

    def invalidate(self) -> None:
        with self._lock:
            self._token = None


def _timestamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _error_message(data) -> str:
    error = data.get("error", {}) if isinstance(data, dict) else {}
    return error.get("message") or str(data)[:300]


class GoogleDestination:
    name = NAME
    batch_size = 500  # the API takes up to 2,000 events per request

    def __init__(self, settings: GoogleSettings, transport: Transport, clock: Clock, tokens: TokenSource) -> None:
        self.settings = settings
        self.transport = transport
        self.clock = clock
        self.tokens = tokens

    @property
    def destination(self) -> dict:
        destination: dict = {
            "operatingAccount": {"accountType": "GOOGLE_ADS", "accountId": self.settings.customer_id},
            "productDestinationId": self.settings.conversion_action_id,
        }
        if self.settings.login_customer_id:
            destination["loginAccount"] = {"accountType": "GOOGLE_ADS", "accountId": self.settings.login_customer_id}
        return destination

    def build(self, event: EventIn, prepared: Prepared) -> dict | Skip | None:
        if event.event_name not in self.settings.events:
            return None
        if event.consent.ad_user_data is False:
            return Skip("consent")
        if not (prepared.google_user_identifiers or prepared.google_ad_identifiers or prepared.client_ip):
            return Skip("no identifiers")
        payload: dict = {
            "transactionId": event.transaction_id,
            "eventTimestamp": _timestamp(event.event_time),
        }
        if source := EVENT_SOURCES.get(event.action_source):
            payload["eventSource"] = source
        if event.value is not None:
            payload["conversionValue"] = event.value
        if event.currency:
            payload["currency"] = event.currency
        if prepared.google_user_identifiers:
            payload["userData"] = {"userIdentifiers": prepared.google_user_identifiers}
        if prepared.google_ad_identifiers:
            payload["adIdentifiers"] = dict(prepared.google_ad_identifiers)
        device = {k: v for k, v in (("userAgent", prepared.user_agent), ("ipAddress", prepared.client_ip)) if v}
        if device:
            payload["eventDeviceInfo"] = device
        consent = {
            field: "CONSENT_GRANTED" if granted else "CONSENT_DENIED"
            for field, granted in (
                ("adUserData", event.consent.ad_user_data),
                ("adPersonalization", event.consent.ad_personalization),
            )
            if granted is not None
        }
        if consent:
            payload["consent"] = consent
        return payload

    def _headers(self, ads_api: bool = False) -> dict:
        headers = {"Authorization": f"Bearer {self.tokens.token()}"}
        if ads_api:
            headers["developer-token"] = self.settings.developer_token or ""
            if self.settings.login_customer_id:
                headers["login-customer-id"] = self.settings.login_customer_id
        return headers

    def send(self, payloads: list[dict]) -> SendResult:
        count = len(payloads)
        try:
            headers = self._headers()
        except PlatformError as exc:
            return SendResult(None, [Outcome("retry", str(exc))] * count, error=str(exc))
        request = {
            "destinations": [self.destination],
            "events": payloads,
            "encoding": "HEX",
            "validateOnly": self.settings.validate_only,
        }
        try:
            response = self.transport.request(
                "POST", f"{self.settings.datamanager_url}/v1/events:ingest", json=request, headers=headers
            )
        except requests.exceptions.InvalidJSONError as exc:
            # Something in an event can not be sent as JSON: find it, do not retry the batch forever.
            return SendResult(None, [refused(count, f"not JSON-serialisable: {exc}")] * count, error=str(exc))
        except requests.RequestException as exc:
            return SendResult(None, [Outcome("retry", f"network: {exc}")] * count, error=str(exc))
        data = body(response)
        status = response.status_code
        if status == 200:
            if self.settings.validate_only:
                return SendResult(200, [Outcome("validated")] * count)
            return SendResult(200, [Outcome("sent")] * count, platform_ref=data.get("requestId"),
                              processing_status="PROCESSING")
        message = _error_message(data)
        if status == 401:
            self.tokens.invalidate()
            outcome = Outcome("retry", f"google 401: {message}")
        elif status in (403, 429) or status >= 500:
            outcome = Outcome("retry", f"google {status}: {message}")
        else:
            outcome = refused(count, f"google {status}: {message}")
        return SendResult(status, [outcome] * count, error=message)

    # Monitoring

    def fetch_status(self, request_id: str) -> tuple[str, dict]:
        """(request status, diagnostics) for one ingest request. Processing is asynchronous."""
        try:
            response = self.transport.request(
                "GET",
                f"{self.settings.datamanager_url}/v1/requestStatus:retrieve",
                params={"requestId": request_id},
                headers=self._headers(),
            )
        except requests.RequestException as exc:
            raise PlatformError(f"Google: {exc}") from exc
        data = body(response)
        if response.status_code != 200:
            raise PlatformError(f"Google {response.status_code}: {_error_message(data)}")
        statuses = data.get("requestStatusPerDestination") or [{}]
        entry = statuses[0]
        diagnostics = {
            "record_count": int(entry.get("eventsIngestionStatus", {}).get("recordCount", 0) or 0),
            "errors": [
                {
                    "reason": e.get("reason", "PROCESSING_ERROR_REASON_UNSPECIFIED"),
                    "count": int(e.get("recordCount", 0) or 0),
                    "indices": e.get("recordIndices", []),
                }
                for e in entry.get("errorInfo", {}).get("errorCounts", [])
            ],
            "warnings": [
                {
                    "reason": w.get("reason", "PROCESSING_WARNING_REASON_UNSPECIFIED"),
                    "count": int(w.get("recordCount", 0) or 0),
                }
                for w in entry.get("warningInfo", {}).get("warningCounts", [])
            ],
        }
        return entry.get("requestStatus", "REQUEST_STATUS_UNKNOWN"), diagnostics

    def fetch_report(self, since: date, until: date) -> list[dict]:
        """Conversions of the relay's conversion action that Google Ads credits to each campaign per day,
        dated by conversion time (the `*_by_conversion_date` metrics), so days line up with the relay's."""
        if not self.settings.developer_token:
            raise PlatformError("GOOGLE_DEVELOPER_TOKEN is not set")
        action = f"customers/{self.settings.customer_id}/conversionActions/{self.settings.conversion_action_id}"
        query = (
            "SELECT segments.date, segments.conversion_action, campaign.id, campaign.name,"
            " metrics.all_conversions_by_conversion_date, metrics.all_conversions_value_by_conversion_date"
            " FROM campaign"
            f" WHERE segments.date BETWEEN '{since.isoformat()}' AND '{until.isoformat()}'"
            f" AND segments.conversion_action = '{action}'"
        )
        url = (
            f"{self.settings.ads_url}/{self.settings.ads_api_version}/customers/{self.settings.customer_id}"
            "/googleAds:search"
        )
        rows: list[dict] = []
        page_token: str | None = None
        while True:
            request: dict = {"query": query}
            if page_token:
                request["pageToken"] = page_token
            try:
                response = self.transport.request("POST", url, json=request, headers=self._headers(ads_api=True))
            except requests.RequestException as exc:
                raise PlatformError(f"Google Ads: {exc}") from exc
            data = body(response)
            if response.status_code != 200:
                raise PlatformError(f"Google Ads {response.status_code}: {_error_message(data)}")
            for result in data.get("results", []):
                metrics = result.get("metrics", {})
                campaign = result.get("campaign", {})
                rows.append(
                    {
                        "day": result.get("segments", {}).get("date"),
                        "campaign_id": str(campaign.get("id", "")),
                        "campaign_name": campaign.get("name"),
                        "conversions": float(metrics.get("allConversionsByConversionDate", 0) or 0),
                        "value": float(metrics.get("allConversionsValueByConversionDate", 0) or 0),
                    }
                )
            page_token = data.get("nextPageToken")
            if not page_token:
                return rows
