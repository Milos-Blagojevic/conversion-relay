"""Delivery: send pending events to each destination in batches, retry what failed for a passing reason.

At-least-once. A delivery is marked sent only after the platform accepted it, so a crash between the
two resends it; both platforms deduplicate a resend (Meta on event_name + event_id, Google on
transactionId within the conversion action).
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Literal, Protocol

from relay.clock import Clock
from relay.models import EventIn
from relay.normalize import Prepared
from relay.store import Store

log = logging.getLogger("relay.dispatcher")

# Keep retrying (backoff doubling from 10 s, capped at 1 h) until this long after the event happened.
# Meta refuses events older than 7 days anyway, and it bounds how long customer data waits in the queue.
RETRY_WINDOW_SECONDS = 7 * 86400
BACKOFF_BASE_SECONDS = 10
BACKOFF_MAX_SECONDS = 3600


@dataclass(frozen=True)
class Skip:
    """The event is not sent to this destination, for a reason the dashboard counts."""

    reason: str


@dataclass(frozen=True)
class Outcome:
    # sent: accepted. validated: checked, not ingested (Google validate-only). retry: try again later.
    # isolate: the batch was rejected as a whole; send this one alone to find the bad event. failed: give up.
    status: Literal["sent", "validated", "retry", "isolate", "failed"]
    reason: str | None = None


@dataclass
class SendResult:
    http_status: int | None
    outcomes: list[Outcome]
    platform_ref: str | None = None
    error: str | None = None
    processing_status: str | None = None

    @property
    def ok(self) -> bool:
        return any(o.status in ("sent", "validated") for o in self.outcomes)


class Destination(Protocol):
    name: str
    batch_size: int

    def build(self, event: EventIn, prepared: Prepared) -> dict | Skip | None:
        """The destination's event payload; Skip to record a skipped delivery; None if not applicable."""

    def send(self, payloads: list[dict]) -> SendResult: ...


def backoff(attempts: int) -> float:
    return float(min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * 2 ** max(0, attempts - 1)))


def refused(count: int, reason: str) -> Outcome:
    """A request the platform refused as a whole: split a batch to find the bad event; fail a lone one."""
    return Outcome("isolate", f"batch rejected: {reason}") if count > 1 else Outcome("failed", reason)



@dataclass
class Dispatcher:
    """One thread per destination, so a slow or failing platform never delays the other."""

    store: Store
    destinations: dict[str, Destination]
    clock: Clock
    idle_seconds: float = 1.0
    _wakes: dict[str, threading.Event] = field(default_factory=dict)
    _stop: threading.Event = field(default_factory=threading.Event)
    _threads: list[threading.Thread] = field(default_factory=list)

    def wake(self) -> None:
        for wake in self._wakes.values():
            wake.set()

    def run_due(self) -> int:
        """Send everything due now, destination by destination. Returns the number of requests made."""
        return sum(self.drain(destination) for destination in self.destinations.values())

    def drain(self, destination: Destination) -> int:
        """Send everything due now to one destination."""
        made = 0
        while not self._stop.is_set():
            rows = self.store.claim_due(destination.name, self.clock.now(), destination.batch_size)
            if not rows:
                break
            self._send(destination, rows)
            made += 1
        return made

    def _send(self, destination: Destination, rows: list) -> None:
        payloads = [json.loads(row["payload"]) for row in rows]
        started = self.clock.now()
        try:
            result = destination.send(payloads)
        except Exception as exc:  # noqa: BLE001 - a client bug must not stop delivery for everyone else
            log.exception("%s send crashed", destination.name)
            result = SendResult(None, [Outcome("retry", f"client error: {exc}")] * len(rows), error=str(exc))
        finished = self.clock.now()
        duration_ms = (finished - started) * 1000
        request_pk = self.store.record_request(
            destination.name,
            started,
            duration_ms,
            result.http_status,
            len(rows),
            result.ok,
            result.platform_ref,
            result.error,
            result.processing_status,
        )
        accepted: list[tuple[int, int, str]] = []
        for index, (row, outcome) in enumerate(zip(rows, result.outcomes, strict=True)):
            if outcome.status in ("sent", "validated"):
                # The index within the request maps Google's per-record diagnostics back to deliveries.
                accepted.append((row["id"], index, outcome.status))
            elif outcome.status == "isolate":
                self.store.mark_retry(row["id"], finished, outcome.reason or "batch rejected", solo=True,
                                      count_attempt=False)
            elif outcome.status == "retry":
                reason = outcome.reason or "retry"
                if finished - row["event_time"] > RETRY_WINDOW_SECONDS:
                    self.store.mark_failed(row["id"], f"still failing 7 days after the event: {reason}", request_pk)
                else:
                    self.store.mark_retry(row["id"], finished + backoff(row["attempts"] + 1), reason)
            else:
                self.store.mark_failed(row["id"], outcome.reason or "failed", request_pk)
        if accepted:
            self.store.mark_sent(accepted, request_pk, finished)

    def start(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        for name, destination in self.destinations.items():
            self._wakes[name] = threading.Event()
            thread = threading.Thread(target=self._loop, args=(destination, self._wakes[name]),
                                      name=f"relay-{name}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        self.wake()
        for thread in self._threads:
            thread.join(timeout=30)
        self._threads.clear()
        self._wakes.clear()

    def _loop(self, destination: Destination, wake: threading.Event) -> None:
        while not self._stop.is_set():
            # Clear before draining: an event queued during the drain sets it again and is not missed.
            wake.clear()
            try:
                self.drain(destination)
            except Exception:  # noqa: BLE001 - keep the loop alive; the next pass retries
                log.exception("%s dispatch pass failed", destination.name)
            wake.wait(self.idle_seconds)
