"""Normalise and hash customer information the way each platform matches it.

Meta: https://developers.facebook.com/docs/marketing-api/conversions-api/parameters/customer-information-parameters
Google (Data Manager API): https://developers.google.com/data-manager/api/devguides/concepts/formatting

The two disagree in places (Google drops dots and +tags from Gmail addresses, wants E.164 phones with
the plus and keeps hyphens in names; Meta wants bare digits and no punctuation), so each platform gets
its own hashes. Raw values never leave this module: `prepare` returns only what is sent.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field

import phonenumbers

from relay.models import EventIn

_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_GMAIL = {"gmail.com", "googlemail.com"}


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def email(raw: str | None) -> str | None:
    if not raw:
        return None
    value = re.sub(r"\s+", "", raw).lower()
    return value if _EMAIL.fullmatch(value) else None


def google_email(value: str) -> str:
    """Google's extra step on an already normalised address: Gmail ignores dots and +tags."""
    local, domain = value.rsplit("@", 1)
    if domain in _GMAIL:
        local = local.split("+", 1)[0].replace(".", "")
    return f"{local}@{domain}"


def phone_e164(raw: str | None, country: str | None) -> str | None:
    """E.164 (`+18005550199`), or None when the number is not valid for its country."""
    if not raw:
        return None
    try:
        parsed = phonenumbers.parse(raw, country.upper() if country else None)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def country(raw: str | None) -> str | None:
    if not raw:
        return None
    value = raw.strip()
    return value.lower() if len(value) == 2 and value.isascii() and value.isalpha() else None


def _letters(raw: str) -> str:
    """Lowercase, without whitespace, punctuation or symbols (Meta's rule for names and cities)."""
    value = unicodedata.normalize("NFC", raw).lower()
    return "".join(ch for ch in value if unicodedata.category(ch)[0] in "LN")


def meta_text(raw: str | None) -> str | None:
    if not raw:
        return None
    return _letters(raw) or None


def meta_zip(raw: str | None, country_code: str | None) -> str | None:
    if not raw:
        return None
    value = re.sub(r"[\s-]", "", raw).lower()
    if country_code == "us":
        value = value[:5]
    return value or None


def google_name(raw: str | None) -> str | None:
    if not raw:
        return None
    value = unicodedata.normalize("NFC", raw).strip().lower()
    return value or None


def google_postal(raw: str | None, country_code: str | None) -> str | None:
    if not raw:
        return None
    value = raw.strip()
    if country_code == "us":
        value = re.sub(r"\D", "", value)[:5]
    return value or None


def fbc_from_click(fbclid: str, clicked_at_ms: int) -> str:
    """The `_fbc` value Meta's pixel would have set for this click: fb.<subdomain index>.<ms>.<fbclid>."""
    return f"fb.1.{clicked_at_ms}.{fbclid}"


@dataclass
class Prepared:
    """What the destinations send, plus which match keys each one got."""

    meta_user_data: dict = field(default_factory=dict)
    google_user_identifiers: list[dict] = field(default_factory=list)
    google_ad_identifiers: dict = field(default_factory=dict)
    client_ip: str | None = None
    user_agent: str | None = None

    @property
    def meta_keys(self) -> list[str]:
        return sorted(self.meta_user_data)

    @property
    def google_keys(self) -> list[str]:
        keys = {next(iter(identifier)) for identifier in self.google_user_identifiers}
        keys |= set(self.google_ad_identifiers)
        if self.client_ip:
            keys.add("ip")
        names = {"emailAddress": "email", "phoneNumber": "phone", "address": "address"}
        return sorted(names.get(key, key) for key in keys)


def prepare(event: EventIn) -> Prepared:
    user, click = event.user, event.click
    cc = country(user.country)
    out = Prepared(client_ip=user.client_ip or None, user_agent=user.user_agent or None)

    normalized_email = email(user.email)
    phone = phone_e164(user.phone, cc)
    first_meta, last_meta = meta_text(user.first_name), meta_text(user.last_name)

    meta: dict = {}
    if normalized_email:
        meta["em"] = [sha256_hex(normalized_email)]
    if phone:
        meta["ph"] = [sha256_hex(phone.lstrip("+"))]
    if first_meta:
        meta["fn"] = [sha256_hex(first_meta)]
    if last_meta:
        meta["ln"] = [sha256_hex(last_meta)]
    if city := meta_text(user.city):
        meta["ct"] = [sha256_hex(city)]
    if state := meta_text(user.state):
        meta["st"] = [sha256_hex(state)]
    if zp := meta_zip(user.zip, cc):
        meta["zp"] = [sha256_hex(zp)]
    if cc:
        meta["country"] = [sha256_hex(cc)]
    if user.external_id:
        meta["external_id"] = [sha256_hex(user.external_id)]
    if user.client_ip:
        meta["client_ip_address"] = user.client_ip
    if user.user_agent:
        meta["client_user_agent"] = user.user_agent
    if user.fbp:
        meta["fbp"] = user.fbp
    if user.fbc:
        meta["fbc"] = user.fbc
    elif click.fbclid:
        clicked = click.clicked_at or event.event_time
        meta["fbc"] = fbc_from_click(click.fbclid, int(clicked.timestamp() * 1000))
    out.meta_user_data = meta

    identifiers: list[dict] = []
    if normalized_email:
        identifiers.append({"emailAddress": sha256_hex(google_email(normalized_email))})
    if phone:
        identifiers.append({"phoneNumber": sha256_hex(phone)})
    given, family = google_name(user.first_name), google_name(user.last_name)
    postal = google_postal(user.zip, cc)
    # Google matches an address only as a whole: all four parts or none.
    if given and family and cc and postal:
        identifiers.append(
            {
                "address": {
                    "givenName": sha256_hex(given),
                    "familyName": sha256_hex(family),
                    "regionCode": cc.upper(),
                    "postalCode": postal,
                }
            }
        )
    out.google_user_identifiers = identifiers
    out.google_ad_identifiers = {
        key: value for key in ("gclid", "gbraid", "wbraid") if (value := getattr(click, key))
    }
    return out
