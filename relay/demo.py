"""Demo traffic for sandbox mode: a shop's orders, each with an ad journey, browser tags and a server event.

The server event goes through the relay exactly as a real one would (`Relay.ingest`, the dispatcher,
the platform clients); only the platforms at the other end are simulated. Two set-ups:

- `shared`: the browser tags and the server event share the dedup keys (Meta eventID = event_id,
  Google tag transaction_id = transactionId) and the server forwards the _fbp/_fbc cookies.
- `broken`: a common first attempt at server-side tracking. The pixel makes up its own event ID, the
  Google tag sends no transaction ID and the server forwards no browser cookies. Both platforms then
  count most orders twice.

`seed` replays N days of history (by default the first half broken, then fixed) on a manual clock.
"""

from __future__ import annotations

import random
import string
import threading
import uuid
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from relay.clock import ManualClock
from relay.models import EventIn
from relay.runtime import Relay

SCENARIOS = ("shared", "broken")

META_CAMPAIGNS = [
    ("120210000000001", "Prospecting - Advantage+ shopping"),
    ("120210000000002", "Retargeting - dynamic catalog"),
]
GOOGLE_CAMPAIGNS = [
    ("21000000001", "Search - brand"),
    ("21000000002", "Performance Max - all products"),
]
FIRST_NAMES = ["Ana", "Marko", "Jelena", "Luka", "Sofia", "Noah", "Emma", "Liam", "Mia", "Lucas", "Olivia", "Elias"]
LAST_NAMES = ["Jovanovic", "Smith", "Muller", "Rossi", "Garcia", "Novak", "Dubois", "Kowalski", "Jensen", "Silva"]
DOMAINS = ["gmail.com", "outlook.com", "yahoo.com", "icloud.com", "proton.me"]
# (city, state, zip, country, phone prefix, phone digits)
PLACES = [
    ("Austin", "TX", "78701", "US", "+1512", 7),
    ("Brooklyn", "NY", "11201", "US", "+1718", 7),
    ("Seattle", "WA", "98101", "US", "+1206", 7),
    ("London", "", "SW1A 1AA", "GB", "+447700", 6),
    ("Berlin", "", "10115", "DE", "+4915", 9),
]
USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5"
    " Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0"
    " Safari/537.36",
    "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Mobile"
    " Safari/537.36",
]
SOURCES = ("meta_click", "google_click", "both", "meta_view", "direct")


def _click_id(rng: random.Random, prefix: str, length: int) -> str:
    return prefix + "".join(rng.choices(string.ascii_letters + string.digits + "_-", k=length))


@dataclass
class Order:
    order_id: str
    at: float
    value: float
    journey: list[dict]
    click: dict
    user: dict
    fbp: str
    fbc: str | None
    gclid_cookie: str | None
    consent: bool
    blocked: bool
    server_delay: float
    scenario: str
    # What the page's tags send: the order id when the keys are shared, a made-up one when broken.
    browser_event_id: str
    browser_transaction_id: str | None


def _touch(channel: str, kind: str, at: float, campaign: tuple[str, str]) -> dict:
    return {"channel": channel, "kind": kind, "at": at, "campaign_id": campaign[0], "campaign_name": campaign[1]}


def journey_for(rng: random.Random, at: float, source: str | None = None) -> list[dict]:
    """Ad interactions before the order. With `source` the demo page picks one; otherwise random."""
    hours = 3600.0
    touches: list[dict] = []
    if source is None:
        if rng.random() < 0.35:
            touches.append(_touch("meta", "view", at - rng.uniform(0.2, 48) * hours, rng.choice(META_CAMPAIGNS)))
        if rng.random() < 0.32:
            touches.append(_touch("meta", "click", at - rng.uniform(0.1, 6 * 24) * hours, rng.choice(META_CAMPAIGNS)))
        if rng.random() < 0.38:
            touches.append(_touch("google", "click", at - rng.uniform(0.05, 10 * 24) * hours,
                                  rng.choice(GOOGLE_CAMPAIGNS)))
        if rng.random() < 0.12:
            touches.append(_touch("email", "click", at - rng.uniform(0.1, 48) * hours, ("newsletter", "Newsletter")))
        return touches
    if source in ("meta_click", "both"):
        touches.append(_touch("meta", "click", at - (30 if source == "meta_click" else 26) * hours, META_CAMPAIGNS[0]))
    if source in ("google_click", "both"):
        touches.append(_touch("google", "click", at - 0.5 * hours, GOOGLE_CAMPAIGNS[0]))
    if source == "meta_view":
        touches.append(_touch("meta", "view", at - 3 * hours, META_CAMPAIGNS[1]))
    return touches


def click_context(rng: random.Random, journey: list[dict]) -> tuple[dict, str | None, str | None]:
    """What the site's first-party cookie holds: the last click, plus the platforms' click ids
    (_fbc from the last Meta click, _gcl_aw from the last Google click)."""
    clicks = sorted((t for t in journey if t["kind"] == "click"), key=lambda t: t["at"])
    fbclid = gclid = None
    for touch in clicks:
        if touch["channel"] == "meta":
            fbclid = (touch, _click_id(rng, "IwAR", 60))
        elif touch["channel"] == "google":
            gclid = (touch, _click_id(rng, "Cj0KCQiA", 80))
    if not clicks:
        return {}, None, None
    last = clicks[-1]
    clicked_at = datetime.fromtimestamp(last["at"], timezone.utc).isoformat()
    if last["channel"] == "meta":
        click = {"fbclid": fbclid[1], "utm_source": "facebook", "utm_medium": "paid_social",
                 "utm_campaign": last["campaign_name"], "utm_id": last["campaign_id"], "clicked_at": clicked_at}
    elif last["channel"] == "google":
        click = {"gclid": gclid[1], "utm_source": "google", "utm_medium": "cpc",
                 "utm_campaign": last["campaign_name"], "utm_id": last["campaign_id"], "clicked_at": clicked_at}
    else:
        click = {"utm_source": "newsletter", "utm_medium": "email", "utm_campaign": last["campaign_name"],
                 "clicked_at": clicked_at}
    fbc = f"fb.1.{int(fbclid[0]['at'] * 1000)}.{fbclid[1]}" if fbclid else None
    return click, fbc, gclid[1] if gclid else None


def make_order(rng: random.Random, at: float, scenario: str, *, source: str | None = None,
               blocked: bool | None = None, consent: bool | None = None, email: bool | None = None,
               phone: bool | None = None) -> Order:
    journey = journey_for(rng, at, source)
    click, fbc, gclid = click_context(rng, journey)
    if click.get("gclid") and source is None and rng.random() < 0.015:
        # A landing-page parser that cuts the gclid short: Google accepts the request, then rejects the record.
        click["gclid"] = click["gclid"][:12]
    first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
    city, state, zip_code, country, prefix, digits = rng.choice(PLACES)
    user: dict = {
        "client_ip": f"{rng.randint(23, 223)}.{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}",
        "user_agent": rng.choice(USER_AGENTS),
        "country": country,
    }
    if email if email is not None else rng.random() < 0.93:
        user["email"] = f"{first}.{last}{rng.randint(1, 999)}@{rng.choice(DOMAINS)}"
    if phone if phone is not None else rng.random() < 0.58:
        user["phone"] = prefix + "".join(rng.choices("23456789", k=digits))
    if rng.random() < 0.75:
        user.update(first_name=first, last_name=last, city=city, zip=zip_code)
        if state:
            user["state"] = state
    if rng.random() < 0.45:
        user["external_id"] = f"cust-{rng.randint(10_000, 99_999)}"
    delay = rng.random()
    server_delay = (
        rng.uniform(0.3, 3) if delay < 0.92 else rng.uniform(300, 5400) if delay < 0.98 else rng.uniform(3, 30) * 3600
    )
    order_id = f"ord-{uuid.UUID(int=rng.getrandbits(128)).hex[:12]}"
    shared = scenario == "shared"
    return Order(
        order_id=order_id,
        at=at,
        value=round(rng.choice([29.0, 49.0, 64.5, 89.0, 120.0, 189.0]) * rng.choice([1, 1, 1, 2]), 2),
        journey=journey,
        click=click,
        user=user,
        fbp=f"fb.1.{int((at - rng.uniform(0, 30 * 86400)) * 1000)}.{rng.randint(10**9, 10**10 - 1)}",
        fbc=fbc,
        gclid_cookie=gclid,
        consent=consent if consent is not None else rng.random() > 0.06,
        blocked=blocked if blocked is not None else rng.random() < 0.24,
        server_delay=server_delay,
        scenario=scenario,
        browser_event_id=order_id if shared else f"px-{rng.getrandbits(48):012x}",
        browser_transaction_id=order_id if shared else None,
    )


def server_event(order: Order) -> EventIn:
    """What the shop's backend sends the relay when the order is paid."""
    user = dict(order.user)
    if order.scenario == "shared":
        user["fbp"] = order.fbp
        if order.fbc:
            user["fbc"] = order.fbc
    return EventIn.model_validate(
        {
            "event_id": order.order_id,
            "event_name": "Purchase",
            "event_time": datetime.fromtimestamp(order.at, timezone.utc),
            "action_source": "website",
            "event_source_url": "https://shop.example/checkout/thank-you",
            "value": order.value,
            "currency": "USD",
            "order_id": order.order_id,
            "user": user,
            "click": order.click,
            "consent": {"ad_user_data": order.consent, "ad_personalization": order.consent},
        }
    )


def beacon_ids(order: Order) -> dict[str, str]:
    """The dedup key each tag's beacon reports. Without a transaction ID the Google tag has nothing to
    match on, so its beacon carries a tag-only id."""
    return {
        "meta_pixel": order.browser_event_id,
        "google_tag": order.browser_transaction_id or f"gtag-{order.order_id}",
    }


def fire_platform_tags(relay: Relay, order: Order) -> bool:
    """The page's Meta Pixel and Google tag hits at checkout, as the sandbox platforms receive them.
    Returns False when nothing fires (ad blocker or refused consent)."""
    if order.blocked or not order.consent:
        return False
    relay.simulator.meta_pixel(event_name="Purchase", event_id=order.browser_event_id, order_ref=order.order_id,
                               fbp=order.fbp, fbc=order.fbc, value=order.value, event_time=order.at)
    relay.simulator.google_tag(order_ref=order.order_id, transaction_id=order.browser_transaction_id,
                               value=order.value, gclid=order.gclid_cookie, event_time=order.at)
    return True


def fire_tags(relay: Relay, order: Order) -> None:
    """Platform tags plus the beacons the page sends the relay (`POST /v1/beacons`) when they fire."""
    if fire_platform_tags(relay, order):
        now = relay.clock.now()
        for tag, event_id in beacon_ids(order).items():
            relay.store.add_beacon(event_id, "Purchase", tag, now)


def seed(relay: Relay, *, days: int = 14, orders_per_day: int = 60, broken_days: int = 7, seed: int = 7,
         end: float | None = None) -> dict:
    """Replay `days` of orders ending now: the first `broken_days` with the broken set-up, then fixed."""
    if not relay.settings.sandbox or relay.simulator is None:
        raise RuntimeError("seed runs only in sandbox mode")
    if not isinstance(relay.clock, ManualClock):
        raise RuntimeError("seed needs a ManualClock")
    rng = random.Random(seed)
    clock = relay.clock
    end = end if end is not None else clock.now()
    start_day = datetime.fromtimestamp(end, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=days - 1
    )
    timeline: list[tuple[float, int, str, Order]] = []
    orders = 0
    for day in range(days):
        midnight = (start_day + timedelta(days=day)).timestamp()
        scenario = "broken" if day < broken_days else "shared"
        for _ in range(max(1, round(rng.gauss(orders_per_day, orders_per_day * 0.15)))):
            at = midnight + rng.uniform(7, 23.5) * 3600
            if at >= end:
                continue
            order = make_order(rng, at, scenario)
            relay.simulator.add_journey(order.order_id, order.journey)
            timeline.append((at, 0, "tags", order))
            timeline.append((at + order.server_delay, 1, "server", order))
            orders += 1
    timeline.sort(key=lambda item: (item[0], item[1]))
    # Replay from the first day. Time only moves forward: the simulated API latency advances the clock too.
    clock.set(start_day.timestamp())
    for at, _, kind, order in timeline:
        if at > end:
            break
        _advance(relay, clock, at)
        if kind == "tags":
            fire_tags(relay, order)
        else:
            relay.ingest(server_event(order))
            relay.dispatcher.run_due()
    _advance(relay, clock, end)
    monitor = relay.monitor.refresh()
    return {"orders": orders, "events": relay.store.scalar("SELECT COUNT(*) FROM events"), "monitor": monitor}


def _advance(relay: Relay, clock: ManualClock, until: float) -> None:
    """Move the clock to `until`, sending each retry when it falls due, as the dispatcher thread would."""
    while (due := relay.store.next_due()) is not None and due <= until:
        clock.set(max(due, clock.now()))
        relay.dispatcher.run_due()
    clock.set(max(until, clock.now()))


class LiveDemo:
    """Orders placed from the demo page (`/demo`), kept in memory for its status table."""

    KEEP = 100

    def __init__(self, relay: Relay) -> None:
        self.relay = relay
        self.rng = random.Random()
        self.orders: OrderedDict[str, Order] = OrderedDict()
        self.lock = threading.Lock()

    def place(self, *, scenario: str, source: str, blocked: bool, consent: bool, email: bool, phone: bool) -> Order:
        order = make_order(self.rng, self.relay.clock.now(), scenario, source=source, blocked=blocked,
                           consent=consent, email=email, phone=phone)
        self.relay.simulator.add_journey(order.order_id, order.journey)
        with self.lock:
            self.orders[order.order_id] = order
            while len(self.orders) > self.KEEP:
                self.orders.popitem(last=False)
        return order

    def get(self, order_id: str) -> Order | None:
        with self.lock:
            return self.orders.get(order_id)

    def state(self, limit: int = 12) -> dict:
        with self.lock:
            orders = list(self.orders.values())
        outcomes = self.relay.simulator.order_outcomes([o.order_id for o in orders])
        deliveries: dict[str, dict] = defaultdict(dict)
        if orders:
            marks = ",".join("?" for _ in orders)
            for row in self.relay.store.all(
                "SELECT e.event_id, d.destination, d.status, d.reason, (d.sent_at - e.received_at) * 1000 AS ms"
                f" FROM events e JOIN deliveries d ON d.event_pk = e.id WHERE e.event_id IN ({marks})",
                [o.order_id for o in orders],
            ):
                deliveries[row["event_id"]][row["destination"]] = {
                    "status": row["status"],
                    "reason": row["reason"],
                    "latency_ms": round(row["ms"]) if row["ms"] is not None else None,
                }
        rows = []
        totals = {"orders": len(orders), "meta_counted": 0, "meta_hits": 0, "google_counted": 0, "google_hits": 0}
        for order in orders:
            outcome = outcomes.get(order.order_id, {"meta": [], "google": []})
            for platform in ("meta", "google"):
                totals[f"{platform}_counted"] += sum(1 for hit in outcome[platform] if hit["counted"])
                totals[f"{platform}_hits"] += len(outcome[platform])
            rows.append(
                {
                    "order_id": order.order_id,
                    "value": order.value,
                    "scenario": order.scenario,
                    "tags_fired": not order.blocked and order.consent,
                    "consent": order.consent,
                    "relay": deliveries.get(order.order_id, {}),
                    "meta": outcome["meta"],
                    "google": outcome["google"],
                }
            )
        return {"totals": totals, "orders": rows[::-1][:limit]}
