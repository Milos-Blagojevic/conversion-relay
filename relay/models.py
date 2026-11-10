"""The event the relay accepts. Raw customer information comes in; only normalised, hashed values go out."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ACTION_SOURCES = (
    "website",
    "app",
    "email",
    "phone_call",
    "chat",
    "physical_store",
    "system_generated",
    "business_messaging",
    "other",
)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class User(_Model):
    email: str | None = Field(None, max_length=320)
    phone: str | None = Field(None, max_length=64)
    first_name: str | None = Field(None, max_length=200)
    last_name: str | None = Field(None, max_length=200)
    city: str | None = Field(None, max_length=200)
    state: str | None = Field(None, max_length=100)
    zip: str | None = Field(None, max_length=20)
    country: str | None = Field(None, max_length=2, description="ISO 3166-1 alpha-2")
    external_id: str | None = Field(None, max_length=200)
    client_ip: str | None = Field(None, max_length=64)
    user_agent: str | None = Field(None, max_length=1024)
    fbp: str | None = Field(None, max_length=256, description="The _fbp cookie")
    fbc: str | None = Field(None, max_length=512, description="The _fbc cookie")


def _utc(value: datetime) -> datetime:
    """Naive timestamps are UTC."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class Click(_Model):
    """The last ad click the site saw for this visitor (from its first-party cookie or landing URL)."""

    gclid: str | None = Field(None, max_length=512)
    gbraid: str | None = Field(None, max_length=512)
    wbraid: str | None = Field(None, max_length=512)
    fbclid: str | None = Field(None, max_length=512)
    utm_source: str | None = Field(None, max_length=200)
    utm_medium: str | None = Field(None, max_length=200)
    utm_campaign: str | None = Field(None, max_length=200)
    utm_id: str | None = Field(None, max_length=200, description="Campaign id, used to join platform reports")
    clicked_at: datetime | None = None

    @field_validator("clicked_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return _utc(value) if value else None


class Consent(_Model):
    """None means the site did not say; False means the visitor refused and nothing is shared."""

    ad_user_data: bool | None = None
    ad_personalization: bool | None = None


class EventIn(_Model):
    event_id: str = Field(min_length=1, max_length=128, description="Dedup key, shared with the browser tag")
    event_name: str = Field("Purchase", min_length=1, max_length=64)
    event_time: datetime
    action_source: Literal[ACTION_SOURCES] = "website"  # type: ignore[valid-type]
    event_source_url: str | None = Field(None, max_length=2048)
    value: float | None = Field(None, ge=0)
    currency: str | None = Field(None, min_length=3, max_length=3)
    order_id: str | None = Field(None, max_length=128, description="Google transactionId; defaults to event_id")
    user: User = Field(default_factory=User)
    click: Click = Field(default_factory=Click)
    consent: Consent = Field(default_factory=Consent)

    @field_validator("event_time")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.isalpha():
            raise ValueError("currency must be a 3-letter ISO 4217 code")
        return value.upper()

    @property
    def transaction_id(self) -> str:
        return self.order_id or self.event_id


class Batch(_Model):
    events: list[EventIn] = Field(min_length=1, max_length=500)


class Beacon(_Model):
    """A browser tag fired for this event. Sent by the page, so it carries no customer information."""

    event_id: str = Field(min_length=1, max_length=128)
    event_name: str = Field("Purchase", min_length=1, max_length=64)
    tag: Literal["meta_pixel", "google_tag"]
