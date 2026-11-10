"""Monitor: pulls what the platforms report back, on a timer and on demand.

- Meta event match quality, event coverage, dedup keys, freshness and delay (Dataset Quality API).
- Google processing status for each ingest request (Data Manager API diagnostics); records Google
  rejected after accepting them become `rejected` deliveries.
- Conversions each platform attributes per campaign and day (Meta Insights, Google Ads reporting),
  for the internal-versus-platform attribution comparison.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from relay.clock import Clock
from relay.config import Settings
from relay.google import TERMINAL_STATUSES, GoogleDestination
from relay.meta import MetaDestination
from relay.store import Store
from relay.transport import PlatformError

log = logging.getLogger("relay.monitor")

# Stop asking about a request Google has not finished processing after this long.
STATUS_GIVE_UP_SECONDS = 14 * 86400


class Monitor:
    def __init__(self, store: Store, destinations: dict, clock: Clock, settings: Settings,
                 status_delay: timedelta) -> None:
        self.store = store
        self.destinations = destinations
        self.clock = clock
        self.settings = settings
        self.status_delay = status_delay
        self._stop = threading.Event()
        self._run_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def tasks(self) -> list[tuple[str, Callable[[], str]]]:
        tasks: list[tuple[str, Callable[[], str]]] = []
        meta = self.destinations.get("meta")
        google = self.destinations.get("google")
        if isinstance(meta, MetaDestination):
            tasks.append(("meta_quality", lambda: self._meta_quality(meta)))
            if meta.settings.ad_account_id:
                tasks.append(("meta_report", lambda: self._report("meta", meta)))
        if isinstance(google, GoogleDestination):
            tasks.append(("google_status", lambda: self._google_status(google)))
            if google.settings.developer_token:
                tasks.append(("google_report", lambda: self._report("google", google)))
        return tasks

    def refresh(self) -> list[tuple[str, bool, str]]:
        """Run every task once. A failing task is recorded and does not stop the others."""
        results = []
        with self._run_lock:
            for name, task in self.tasks():
                try:
                    detail, ok = task(), True
                except PlatformError as exc:
                    detail, ok = str(exc), False
                except Exception as exc:  # noqa: BLE001 - one broken task must not hide the others
                    log.exception("monitor task %s crashed", name)
                    detail, ok = f"{type(exc).__name__}: {exc}", False
                self.store.record_monitor_run(name, ok, detail, self.clock.now())
                results.append((name, ok, detail))
        return results

    def _meta_quality(self, meta: MetaDestination) -> str:
        data = meta.fetch_quality()
        self.store.add_snapshot("meta", data, self.clock.now())
        return f"{len(data['events'])} event name(s)"

    def _report(self, platform: str, destination) -> str:
        until = datetime.fromtimestamp(self.clock.now(), timezone.utc).date()
        since = until - timedelta(days=self.settings.report_days - 1)
        rows = destination.fetch_report(since, until)
        self.store.replace_reports(platform, since.isoformat(), until.isoformat(), rows, self.clock.now())
        total = sum(r["conversions"] for r in rows)
        return f"{total:g} conversions, {since} to {until}"

    def _google_status(self, google: GoogleDestination) -> str:
        now = self.clock.now()
        checked = finished = rejected = 0
        last_id = 0
        while page := self.store.requests_to_check(
            "google", now - self.status_delay.total_seconds(), now - STATUS_GIVE_UP_SECONDS, last_id
        ):
            for request in page:
                last_id = request["id"]
                status, diagnostics = google.fetch_status(request["platform_ref"])
                checked += 1
                if status not in TERMINAL_STATUSES:
                    continue
                finished += 1
                self.store.update_request_status(request["id"], status, diagnostics, self.clock.now())
                for error in diagnostics["errors"]:
                    indices = error["indices"]
                    if not indices and status == "FAILED":
                        indices = list(range(request["event_count"]))
                    rejected += self.store.reject_deliveries(request["id"], indices, error["reason"])
        return f"{checked} checked, {finished} finished, {rejected} record(s) rejected"

    def start(self) -> None:
        if self._thread or not self.tasks():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="relay-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=60)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.refresh()
            self._stop.wait(self.settings.monitor_interval)
