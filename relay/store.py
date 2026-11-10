"""SQLite storage: events, per-destination deliveries, API requests, browser beacons, platform data.

One connection shared by the request threads, the dispatcher and the monitor, behind a lock; WAL mode.
A database belongs to one mode: a sandbox database (simulated platforms) never opens in live mode and
the other way round, so demo traffic can not end up next to real conversions.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL,
    event_name TEXT NOT NULL,
    event_time REAL NOT NULL,
    received_at REAL NOT NULL,
    value REAL,
    currency TEXT,
    transaction_id TEXT NOT NULL,
    meta_keys TEXT NOT NULL,
    google_keys TEXT NOT NULL,
    meta_score REAL NOT NULL,
    channel TEXT NOT NULL,
    campaign TEXT,
    consent_ad_user_data INTEGER,
    duplicate_hits INTEGER NOT NULL DEFAULT 0,
    UNIQUE (event_name, event_id)
);
CREATE INDEX IF NOT EXISTS events_time ON events (event_time);
CREATE INDEX IF NOT EXISTS events_received ON events (received_at);
-- Google tag beacons join on the transaction id.
CREATE INDEX IF NOT EXISTS events_tx ON events (event_name, transaction_id);

CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY,
    destination TEXT NOT NULL,
    started_at REAL NOT NULL,
    duration_ms REAL NOT NULL,
    http_status INTEGER,
    event_count INTEGER NOT NULL,
    ok INTEGER NOT NULL,
    platform_ref TEXT,
    error TEXT,
    processing_status TEXT,
    diagnostics TEXT,
    checked_at REAL
);
CREATE INDEX IF NOT EXISTS requests_status ON requests (destination, processing_status);

CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY,
    event_pk INTEGER NOT NULL REFERENCES events (id),
    destination TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    payload TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    solo INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL,
    sent_at REAL,
    request_pk INTEGER REFERENCES requests (id),
    request_index INTEGER,
    UNIQUE (event_pk, destination)
);
CREATE INDEX IF NOT EXISTS deliveries_due ON deliveries (destination, status, next_attempt_at);
CREATE INDEX IF NOT EXISTS deliveries_request ON deliveries (request_pk);

CREATE TABLE IF NOT EXISTS beacons (
    id INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL,
    event_name TEXT NOT NULL,
    tag TEXT NOT NULL,
    received_at REAL NOT NULL,
    UNIQUE (event_name, event_id, tag)
);
CREATE INDEX IF NOT EXISTS beacons_time ON beacons (received_at);

CREATE TABLE IF NOT EXISTS quality_snapshots (
    id INTEGER PRIMARY KEY,
    destination TEXT NOT NULL,
    taken_at REAL NOT NULL,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS platform_reports (
    platform TEXT NOT NULL,
    day TEXT NOT NULL,
    campaign_id TEXT NOT NULL,
    campaign_name TEXT,
    conversions REAL NOT NULL,
    value REAL NOT NULL,
    fetched_at REAL NOT NULL,
    PRIMARY KEY (platform, day, campaign_id)
);

CREATE TABLE IF NOT EXISTS monitor_runs (
    id INTEGER PRIMARY KEY,
    task TEXT NOT NULL,
    ran_at REAL NOT NULL,
    ok INTEGER NOT NULL,
    detail TEXT
);
"""


class ModeMismatch(RuntimeError):
    pass


class Duplicate(Exception):
    def __init__(self, event_pk: int) -> None:
        super().__init__(event_pk)
        self.event_pk = event_pk


class Store:
    def __init__(self, path: str, mode: str) -> None:
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        try:
            self._claim_mode(mode)
        except ModeMismatch:
            self.conn.close()
            raise

    def _claim_mode(self, mode: str) -> None:
        row = self.conn.execute("SELECT value FROM settings WHERE key = 'mode'").fetchone()
        if row is None:
            self.conn.execute("INSERT INTO settings (key, value) VALUES ('mode', ?)", (mode,))
        elif row["value"] != mode:
            raise ModeMismatch(f"{self.path} is a {row['value']} database; it can not be opened in {mode} mode")

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    def all(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, tuple(params)).fetchone()

    def scalar(self, sql: str, params: Iterable[Any] = ()) -> Any:
        row = self.one(sql, params)
        return row[0] if row else None

    # Events and deliveries

    def insert_event(self, event: dict, deliveries: list[dict]) -> int:
        """Insert an event with its deliveries in one transaction. Raises Duplicate for a known event."""
        columns = ", ".join(event)
        marks = ", ".join("?" for _ in event)
        try:
            with self.tx() as conn:
                pk = conn.execute(f"INSERT INTO events ({columns}) VALUES ({marks})", tuple(event.values())).lastrowid
                for delivery in deliveries:
                    conn.execute(
                        "INSERT INTO deliveries (event_pk, destination, status, reason, payload, next_attempt_at)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            pk,
                            delivery["destination"],
                            delivery["status"],
                            delivery.get("reason"),
                            json.dumps(delivery["payload"]) if delivery.get("payload") is not None else None,
                            delivery["next_attempt_at"],
                        ),
                    )
        except sqlite3.IntegrityError:
            existing = self.one(
                "SELECT id FROM events WHERE event_name = ? AND event_id = ?", (event["event_name"], event["event_id"])
            )
            if existing is None:
                raise
            self.bump_duplicate(existing["id"])
            raise Duplicate(existing["id"]) from None
        return pk

    def find_event(self, event_name: str, event_id: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM events WHERE event_name = ? AND event_id = ?", (event_name, event_id))

    def bump_duplicate(self, event_pk: int) -> None:
        with self.tx() as conn:
            conn.execute("UPDATE events SET duplicate_hits = duplicate_hits + 1 WHERE id = ?", (event_pk,))

    def deliveries_for(self, event_pk: int) -> list[sqlite3.Row]:
        return self.all("SELECT * FROM deliveries WHERE event_pk = ? ORDER BY destination", (event_pk,))

    def claim_due(self, destination: str, now: float, limit: int) -> list[sqlite3.Row]:
        """The next batch to send: one isolated (`solo`) delivery, or up to `limit` regular ones."""
        rows = self.all(
            "SELECT d.id, d.payload, d.attempts, d.solo, e.event_time FROM deliveries d"
            " JOIN events e ON e.id = d.event_pk"
            " WHERE d.destination = ? AND d.status = 'pending' AND d.next_attempt_at <= ?"
            " ORDER BY d.solo DESC, d.next_attempt_at, d.id LIMIT ?",
            (destination, now, limit),
        )
        if rows and rows[0]["solo"]:
            return rows[:1]
        return [row for row in rows if not row["solo"]]

    def record_request(
        self,
        destination: str,
        started_at: float,
        duration_ms: float,
        http_status: int | None,
        event_count: int,
        ok: bool,
        platform_ref: str | None,
        error: str | None,
        processing_status: str | None = None,
    ) -> int:
        with self.tx() as conn:
            return conn.execute(
                "INSERT INTO requests (destination, started_at, duration_ms, http_status, event_count, ok,"
                " platform_ref, error, processing_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (destination, started_at, duration_ms, http_status, event_count, int(ok), platform_ref, error,
                 processing_status),
            ).lastrowid

    def mark_sent(self, accepted: list[tuple[int, int, str]], request_pk: int, sent_at: float) -> None:
        """(delivery id, index in the request, status) rows. Accepted deliveries drop their payload:
        the hashed identifiers, IP address and user agent are not kept once the platform has them."""
        with self.tx() as conn:
            conn.executemany(
                "UPDATE deliveries SET status = ?, sent_at = ?, request_pk = ?, request_index = ?, payload = NULL,"
                " attempts = attempts + 1, reason = NULL WHERE id = ?",
                [(status, sent_at, request_pk, index, delivery_id) for delivery_id, index, status in accepted],
            )

    def mark_retry(self, delivery_id: int, next_attempt_at: float, reason: str, solo: bool = False,
                   count_attempt: bool = True) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE deliveries SET attempts = attempts + ?, next_attempt_at = ?, reason = ?,"
                " solo = MAX(solo, ?) WHERE id = ?",
                (int(count_attempt), next_attempt_at, reason[:500], int(solo), delivery_id),
            )

    def mark_failed(self, delivery_id: int, reason: str, request_pk: int | None = None) -> None:
        """Give up on a delivery. Its payload goes too: nothing resends it, so the customer data has no use."""
        with self.tx() as conn:
            conn.execute(
                "UPDATE deliveries SET status = 'failed', reason = ?, attempts = attempts + 1, payload = NULL,"
                " request_pk = COALESCE(?, request_pk) WHERE id = ?",
                (reason[:500], request_pk, delivery_id),
            )

    def pending_count(self) -> int:
        return self.scalar("SELECT COUNT(*) FROM deliveries WHERE status = 'pending'") or 0

    def next_due(self) -> float | None:
        return self.scalar("SELECT MIN(next_attempt_at) FROM deliveries WHERE status = 'pending'")

    # Google request status (asynchronous processing diagnostics)

    def requests_to_check(self, destination: str, started_before: float, started_after: float, after_id: int = 0,
                          limit: int = 200) -> list[sqlite3.Row]:
        """Accepted requests still processing, in id order from `after_id`; `started_after` drops stale ones."""
        return self.all(
            "SELECT * FROM requests WHERE destination = ? AND ok = 1 AND platform_ref IS NOT NULL"
            " AND processing_status = 'PROCESSING' AND started_at BETWEEN ? AND ? AND id > ? ORDER BY id LIMIT ?",
            (destination, started_after, started_before, after_id, limit),
        )

    def update_request_status(self, request_pk: int, status: str, diagnostics: dict, checked_at: float) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE requests SET processing_status = ?, diagnostics = ?, checked_at = ? WHERE id = ?",
                (status, json.dumps(diagnostics), checked_at, request_pk),
            )

    def reject_deliveries(self, request_pk: int, indices: list[int], reason: str) -> int:
        """Deliveries the platform accepted but then failed in processing."""
        with self.tx() as conn:
            cursor = conn.executemany(
                "UPDATE deliveries SET status = 'rejected', reason = ? WHERE request_pk = ? AND request_index = ?",
                [(reason, request_pk, index) for index in indices],
            )
            return cursor.rowcount

    # Browser beacons

    def add_beacon(self, event_id: str, event_name: str, tag: str, received_at: float) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO beacons (event_id, event_name, tag, received_at) VALUES (?, ?, ?, ?)",
                (event_id, event_name, tag, received_at),
            )

    # Platform data

    def add_snapshot(self, destination: str, data: dict, taken_at: float) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO quality_snapshots (destination, taken_at, data) VALUES (?, ?, ?)",
                (destination, taken_at, json.dumps(data)),
            )

    def latest_snapshot(self, destination: str) -> tuple[float, dict] | None:
        row = self.one(
            "SELECT taken_at, data FROM quality_snapshots WHERE destination = ? ORDER BY taken_at DESC, id DESC LIMIT 1",
            (destination,),
        )
        return (row["taken_at"], json.loads(row["data"])) if row else None

    def replace_reports(self, platform: str, first_day: str, last_day: str, rows: list[dict], fetched_at: float) -> None:
        """Platforms restate recent days as late conversions arrive, so a fetch replaces its whole range."""
        with self.tx() as conn:
            conn.execute(
                "DELETE FROM platform_reports WHERE platform = ? AND day BETWEEN ? AND ?", (platform, first_day, last_day)
            )
            conn.executemany(
                "INSERT INTO platform_reports (platform, day, campaign_id, campaign_name, conversions, value, fetched_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (platform, day, campaign_id) DO UPDATE SET"
                " conversions = conversions + excluded.conversions, value = value + excluded.value",
                [
                    (platform, r["day"], r["campaign_id"], r.get("campaign_name"), r["conversions"], r["value"], fetched_at)
                    for r in rows
                ],
            )

    def record_monitor_run(self, task: str, ok: bool, detail: str | None, ran_at: float) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO monitor_runs (task, ran_at, ok, detail) VALUES (?, ?, ?, ?)",
                (task, ran_at, int(ok), (detail or "")[:1000] or None),
            )

    def last_monitor_runs(self) -> list[sqlite3.Row]:
        return self.all(
            "SELECT m.* FROM monitor_runs m JOIN (SELECT task, MAX(id) AS id FROM monitor_runs GROUP BY task) last"
            " ON last.id = m.id ORDER BY m.task"
        )
