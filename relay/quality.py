"""The relay's own match-quality estimate, from the match keys each event carried.

Meta computes Event Match Quality itself and does not publish the formula; the platform score comes
from the Dataset Quality API (`meta.fetch_quality`). This estimate exists so every event gets a score
the moment it is sent, and so a drop shows up before Meta's 7-day average moves. The weights follow
the order of importance Meta documents (email and phone first, then click id, external id and
browser id, then IP and user agent, then name and address); they are a heuristic, not Meta's model.
"""

from __future__ import annotations

META_KEY_WEIGHTS: dict[str, float] = {
    "em": 3.0,
    "ph": 2.0,
    "fbc": 1.5,
    "external_id": 1.0,
    "fbp": 0.8,
    "client_ip_address": 0.5,
    "client_user_agent": 0.4,
    "fn": 0.2,
    "ln": 0.2,
    "zp": 0.2,
    "ct": 0.1,
    "st": 0.05,
    "country": 0.05,
}

META_KEY_LABELS: dict[str, str] = {
    "em": "Email",
    "ph": "Phone",
    "fbc": "Click ID (fbc)",
    "external_id": "External ID",
    "fbp": "Browser ID (fbp)",
    "client_ip_address": "IP address",
    "client_user_agent": "User agent",
    "fn": "First name",
    "ln": "Last name",
    "zp": "Zip",
    "ct": "City",
    "st": "State",
    "country": "Country",
}

# The names Meta's Dataset Quality API uses for the same keys (match_key_feedback identifiers).
META_KEY_IDENTIFIERS: dict[str, str] = {
    "em": "email",
    "ph": "phone",
    "external_id": "external_id",
    "fbc": "fbc",
    "fbp": "fbp",
    "client_ip_address": "ip_address",
    "client_user_agent": "user_agent",
    "fn": "first_name",
    "ln": "last_name",
    "zp": "zip_code",
    "ct": "city",
    "st": "state",
    "country": "country",
}

GOOGLE_KEY_LABELS: dict[str, str] = {
    "email": "Email",
    "phone": "Phone",
    "address": "Name + address",
    "gclid": "GCLID",
    "gbraid": "GBRAID",
    "wbraid": "WBRAID",
    "ip": "IP address",
}

GOOGLE_USER_DATA = frozenset({"email", "phone", "address"})


def meta_score(keys: list[str] | set[str]) -> float:
    """0-10, like Meta's scale."""
    return round(min(10.0, sum(META_KEY_WEIGHTS.get(key, 0.0) for key in keys)), 2)


def meta_rating(score: float) -> str:
    """Meta's bands in Events Manager."""
    if score >= 8:
        return "Great"
    if score >= 6:
        return "Good"
    if score >= 4:
        return "OK"
    return "Poor"


def google_has_user_data(keys: list[str] | set[str]) -> bool:
    """Enhanced conversions need at least one user-provided identifier."""
    return bool(GOOGLE_USER_DATA & set(keys))
