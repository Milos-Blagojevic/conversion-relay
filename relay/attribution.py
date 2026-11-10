"""Internal attribution: last-click from the click context the site passes with each event.

The site keeps the last ad click (click ids and UTMs) in a first-party cookie and sends it as
`click`. The relay credits the conversion to that click's channel when the click is inside the
lookback window, and to `direct` otherwise. It is the business's own view, one channel per order,
which is what the dashboard sets against what Meta and Google each claim.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from relay.models import Click

CHANNELS = ("meta", "google", "other", "direct")

META_SOURCES = frozenset({"facebook", "fb", "instagram", "ig", "meta", "messenger", "audience_network"})
GOOGLE_SOURCES = frozenset({"google", "youtube", "adwords", "googleads"})
PAID_MEDIUMS = frozenset({"cpc", "ppc", "paid", "paidsearch", "paid_search", "paid_social", "paidsocial", "display", "video"})


def classify(click: Click, event_time: datetime, lookback_days: int) -> tuple[str, str | None]:
    """(channel, campaign) for one conversion."""
    has_click = any((click.gclid, click.gbraid, click.wbraid, click.fbclid, click.utm_source))
    if not has_click:
        return "direct", None
    if click.clicked_at and event_time - click.clicked_at > timedelta(days=lookback_days):
        return "direct", None
    campaign = click.utm_id or click.utm_campaign
    source = (click.utm_source or "").strip().lower()
    medium = (click.utm_medium or "").strip().lower()
    google_click = bool(click.gclid or click.gbraid or click.wbraid)
    # Both click ids at once means the cookie kept an older one; the UTM source says which was last.
    if source in META_SOURCES and (click.fbclid or medium in PAID_MEDIUMS):
        return "meta", campaign
    if source in GOOGLE_SOURCES and (google_click or medium in PAID_MEDIUMS):
        return "google", campaign
    if google_click:
        return "google", campaign
    if click.fbclid:
        return "meta", campaign
    return "other", campaign
