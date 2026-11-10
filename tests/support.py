"""Shared test data: platform settings, endpoint URLs and an event factory."""

from __future__ import annotations

from datetime import datetime, timezone

import responses

from relay import config
from relay.models import EventIn

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc).timestamp()

META = config.MetaSettings(pixel_id="111", access_token="meta-token", test_event_code="TEST123", ad_account_id="222")
GOOGLE = config.GoogleSettings(
    customer_id="1234567890",
    conversion_action_id="555",
    client_id="cid",
    client_secret="secret",
    refresh_token="refresh",
    login_customer_id="9998887777",
    developer_token="dev-token",
)
META_EVENTS = "https://graph.facebook.com/v27.0/111/events"
GOOGLE_INGEST = "https://datamanager.googleapis.com/v1/events:ingest"
GOOGLE_STATUS = "https://datamanager.googleapis.com/v1/requestStatus:retrieve"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"


def token(http: responses.RequestsMock, value: str = "access-1") -> None:
    http.add(responses.POST, GOOGLE_TOKEN, json={"access_token": value, "expires_in": 3600})


def event(**overrides) -> EventIn:
    """A purchase one minute before NOW with full customer data; nested dicts in overrides merge."""
    data = {
        "event_id": "order-1",
        "event_name": "Purchase",
        "event_time": NOW - 60,
        "event_source_url": "https://shop.example/thanks",
        "value": 49.0,
        "currency": "usd",
        "user": {
            "email": " John.Smith@Example.com ",
            "phone": "+1 (800) 555-0199",
            "first_name": "John",
            "last_name": "Smith-Jones",
            "zip": "94045",
            "country": "US",
            "client_ip": "203.0.113.7",
            "user_agent": "Mozilla/5.0",
            "fbp": "fb.1.1700000000000.123",
        },
        "click": {"gclid": "Cj0KCQiA" + "x" * 40, "utm_source": "google", "utm_medium": "cpc", "utm_id": "c-9"},
        "consent": {"ad_user_data": True, "ad_personalization": True},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key] = {**data[key], **value}
        else:
            data[key] = value
    return EventIn.model_validate(data)
