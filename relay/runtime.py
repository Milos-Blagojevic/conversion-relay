"""Wiring: one `Relay` holds the store, the destinations, the dispatcher and the monitor, and ingests events."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from relay import attribution, quality
from relay.clock import Clock, SystemClock
from relay.config import Settings
from relay.dispatcher import Destination, Dispatcher, Skip
from relay.google import GoogleDestination, TokenSource
from relay.meta import MetaDestination
from relay.models import EventIn
from relay.monitor import Monitor
from relay.normalize import prepare
from relay.store import Duplicate, Store
from relay.transport import RequestsTransport, Transport

# Events dated further ahead than this are a clock or unit bug on the sender's side.
MAX_FUTURE_SECONDS = 600
# Google recommends waiting this long before asking for an ingest request's processing status.
GOOGLE_STATUS_DELAY_SECONDS = 1800


class InvalidEvent(ValueError):
    pass


@dataclass
class IngestResult:
    event_pk: int
    duplicate: bool
    deliveries: dict[str, str] = field(default_factory=dict)


@dataclass
class Relay:
    settings: Settings
    store: Store
    clock: Clock
    destinations: dict[str, Destination]
    dispatcher: Dispatcher
    monitor: Monitor
    simulator: object | None = None

    def ingest(self, event: EventIn) -> IngestResult:
        """Store the event and queue it for every destination it applies to. Idempotent on
        (event_name, event_id): a repeat returns the first one's deliveries and sends nothing."""
        now = self.clock.now()
        if event.event_time.timestamp() > now + MAX_FUTURE_SECONDS:
            raise InvalidEvent("event_time is in the future")
        existing = self.store.find_event(event.event_name, event.event_id)
        if existing is not None:
            self.store.bump_duplicate(existing["id"])
            return self._duplicate(existing["id"])
        prepared = prepare(event)
        channel, campaign = attribution.classify(
            event.click, event.event_time, self.settings.click_lookback_days
        )
        row = {
            "event_id": event.event_id,
            "event_name": event.event_name,
            "event_time": event.event_time.timestamp(),
            "received_at": now,
            "value": event.value,
            "currency": event.currency,
            "transaction_id": event.transaction_id,
            "meta_keys": ",".join(prepared.meta_keys),
            "google_keys": ",".join(prepared.google_keys),
            "meta_score": quality.meta_score(prepared.meta_keys),
            "channel": channel,
            "campaign": campaign,
            "consent_ad_user_data": None if event.consent.ad_user_data is None else int(event.consent.ad_user_data),
        }
        deliveries = []
        for destination in self.destinations.values():
            plan = destination.build(event, prepared)
            if plan is None:
                continue
            skipped = isinstance(plan, Skip)
            deliveries.append(
                {
                    "destination": destination.name,
                    "status": "skipped" if skipped else "pending",
                    "reason": plan.reason if skipped else None,
                    "payload": None if skipped else plan,
                    "next_attempt_at": now,
                }
            )
        try:
            pk = self.store.insert_event(row, deliveries)
        except Duplicate as duplicate:
            return self._duplicate(duplicate.event_pk)
        self.dispatcher.wake()
        return IngestResult(pk, False, {d["destination"]: d["status"] for d in deliveries})

    def _duplicate(self, event_pk: int) -> IngestResult:
        statuses = {row["destination"]: row["status"] for row in self.store.deliveries_for(event_pk)}
        return IngestResult(event_pk, True, statuses)

    def start(self) -> None:
        self.dispatcher.start()
        self.monitor.start()

    def stop(self) -> None:
        self.monitor.stop()
        self.dispatcher.stop()
        self.store.close()


def build(settings: Settings, clock: Clock | None = None, transport: Transport | None = None) -> Relay:
    clock = clock or SystemClock()
    store = Store(settings.db_path, settings.mode)
    simulator = None
    if settings.sandbox:
        # Imported here: live mode never loads the simulator.
        from relay.simulator import SandboxTransport, Simulator

        simulator = Simulator(store, clock, settings)
        transport = SandboxTransport(simulator, clock)
    elif transport is None:
        transport = RequestsTransport()
    destinations: dict[str, Destination] = {}
    if settings.meta:
        destinations["meta"] = MetaDestination(settings.meta, transport, clock)
    if settings.google:
        tokens = TokenSource(settings.google, transport, clock)
        destinations["google"] = GoogleDestination(settings.google, transport, clock, tokens)
    dispatcher = Dispatcher(store, destinations, clock)
    status_delay = 0 if settings.sandbox else GOOGLE_STATUS_DELAY_SECONDS
    monitor = Monitor(store, destinations, clock, settings, timedelta(seconds=status_delay))
    return Relay(settings, store, clock, destinations, dispatcher, monitor, simulator)
