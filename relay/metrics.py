"""The numbers on the dashboard (and in `python -m relay report`), computed from the store.

Operational sections (delivery, latency, coverage, match quality) use a rolling window on when the
relay received each event. The attribution comparison uses whole UTC days on when each conversion
happened, because that is how the platform reports are dated.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from relay import quality
from relay.attribution import CHANNELS
from relay.config import Settings
from relay.store import Store

DAY = 86400
TAG_JOIN = {"meta_pixel": "event_id", "google_tag": "transaction_id"}


def pct(part: float, whole: float) -> float | None:
    return round(100 * part / whole, 1) if whole else None


def percentile(values: list[float], q: float) -> float | None:
    """Linear-interpolated percentile, q in [0, 100]."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q / 100
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (position - low), 1)


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()


def _days(now: float, days: int) -> list[str]:
    last = datetime.fromtimestamp(now, timezone.utc).date()
    return [(last - timedelta(days=offset)).isoformat() for offset in range(days - 1, -1, -1)]


def collect(store: Store, settings: Settings, destinations: list[str], now: float, days: int) -> dict:
    start = now - days * DAY
    return {
        "generated_at": now,
        "days": days,
        "mode": settings.mode,
        "destinations": destinations,
        "totals": totals(store, start),
        "delivery": delivery(store, start, destinations),
        "latency": latency(store, start, now, days, destinations),
        "coverage": coverage(store, start, now, days),
        "quality": match_quality(store, start, now, days),
        "platform_quality": platform_quality(store, start),
        "attribution": attribution(store, settings.conversion_event, now, days),
        "monitor": [dict(row) for row in store.last_monitor_runs()],
    }


def totals(store: Store, start: float) -> dict:
    row = store.one(
        "SELECT COUNT(*) AS events, COALESCE(SUM(duplicate_hits), 0) AS duplicates FROM events WHERE received_at >= ?",
        (start,),
    )
    return {
        "events": row["events"],
        "duplicates_blocked": row["duplicates"],
        "pending_now": store.pending_count(),
    }


def delivery(store: Store, start: float, destinations: list[str]) -> dict:
    """Per destination: how many events ended in each status, and why the unsent ones were not sent."""
    out: dict[str, dict] = {
        name: {"total": 0, "statuses": Counter(), "reasons": Counter(), "retried": 0} for name in destinations
    }
    for row in store.all(
        "SELECT d.destination, d.status, d.reason, COUNT(*) AS n, SUM(d.attempts > 1) AS retried FROM deliveries d"
        " JOIN events e ON e.id = d.event_pk WHERE e.received_at >= ? GROUP BY 1, 2, 3",
        (start,),
    ):
        entry = out.setdefault(
            row["destination"], {"total": 0, "statuses": Counter(), "reasons": Counter(), "retried": 0}
        )
        entry["total"] += row["n"]
        entry["statuses"][row["status"]] += row["n"]
        entry["retried"] += row["retried"] or 0
        if row["status"] in ("failed", "rejected", "skipped", "pending") and row["reason"]:
            entry["reasons"][f"{row['status']}: {row['reason']}"] += row["n"]
    for entry in out.values():
        statuses = entry["statuses"]
        delivered = statuses["sent"] + statuses["validated"]
        attempted = entry["total"] - statuses["skipped"]
        entry["delivered"] = delivered
        entry["delivery_rate"] = pct(delivered, attempted)
        entry["coverage"] = pct(delivered, entry["total"])
        entry["statuses"] = dict(statuses)
        entry["reasons"] = entry["reasons"].most_common(6)
    return out


def latency(store: Store, start: float, now: float, days: int, destinations: list[str]) -> dict:
    """Received-to-accepted time per delivery (queueing, retries and the API call), the API call alone,
    and the event age at delivery (what Meta's data freshness and event delay measure)."""
    rows = store.all(
        "SELECT d.destination, (d.sent_at - e.received_at) * 1000 AS ms, d.sent_at - e.event_time AS age,"
        " d.sent_at FROM deliveries d JOIN events e ON e.id = d.event_pk"
        " WHERE d.status IN ('sent', 'validated', 'rejected') AND d.sent_at >= ?",
        (start,),
    )
    requests = store.all("SELECT destination, duration_ms, ok FROM requests WHERE started_at >= ?", (start,))
    day_list = _days(now, days)
    out = {}
    for name in destinations:
        mine = [r for r in rows if r["destination"] == name]
        calls = [r for r in requests if r["destination"] == name]
        ms = [r["ms"] for r in mine]
        ages = [r["age"] for r in mine]
        by_day: dict[str, list[float]] = defaultdict(list)
        for r in mine:
            by_day[_day(r["sent_at"])].append(r["ms"])
        out[name] = {
            "delivered": len(mine),
            "p50_ms": percentile(ms, 50),
            "p95_ms": percentile(ms, 95),
            "p99_ms": percentile(ms, 99),
            "max_ms": round(max(ms), 1) if ms else None,
            "request_p50_ms": percentile([c["duration_ms"] for c in calls], 50),
            "request_p95_ms": percentile([c["duration_ms"] for c in calls], 95),
            "requests": len(calls),
            "request_error_rate": pct(sum(1 for c in calls if not c["ok"]), len(calls)),
            "over_2h": pct(sum(1 for a in ages if a > 2 * 3600), len(ages)),
            "over_24h": pct(sum(1 for a in ages if a > DAY), len(ages)),
            "daily": [
                {"day": day, "p50_ms": percentile(by_day[day], 50), "p95_ms": percentile(by_day[day], 95)}
                for day in day_list
            ],
        }
    return out


def coverage(store: Store, start: float, now: float, days: int) -> dict:
    """Browser tags against server events, from the beacons the page sends when its tags fire.

    both: the tag fired and the relay got the server event with the same dedup key (the platform
    deduplicates them). server_only: no tag, so the server event is a conversion the browser lost.
    browser_only: a tag whose dedup key matches no server event, so the platform counts it apart from
    the server event and the order is counted twice (or the server never sent it)."""
    out = {}
    day_list = _days(now, days)
    for tag, column in TAG_JOIN.items():
        beacons = store.all(
            f"SELECT b.received_at, b.event_name, e.id AS event_pk FROM beacons b LEFT JOIN events e"
            f" ON e.event_name = b.event_name AND e.{column} = b.event_id WHERE b.tag = ? AND b.received_at >= ?",
            (tag, start),
        )
        if not beacons:
            out[tag] = None
            continue
        names = sorted({b["event_name"] for b in beacons})
        marks = ",".join("?" for _ in names)
        server = store.scalar(
            f"SELECT COUNT(*) FROM events WHERE received_at >= ? AND event_name IN ({marks})", (start, *names)
        )
        both = len({b["event_pk"] for b in beacons if b["event_pk"] is not None})
        matched_beacons = sum(1 for b in beacons if b["event_pk"] is not None)
        per_day: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for b in beacons:
            entry = per_day[_day(b["received_at"])]
            entry[0] += 1
            entry[1] += b["event_pk"] is not None
        out[tag] = {
            "browser": len(beacons),
            "server": server,
            "both": both,
            "server_only": server - both,
            "browser_only": len(beacons) - matched_beacons,
            "key_match_rate": pct(matched_beacons, len(beacons)),
            "server_recovered": pct(server - both, server),
            "daily_key_match": [
                {"day": day, "rate": pct(per_day[day][1], per_day[day][0]) if day in per_day else None}
                for day in day_list
            ],
        }
    return out


def match_quality(store: Store, start: float, now: float, days: int) -> dict:
    """The relay's view: which match keys each destination got, and the estimated Meta score."""
    meta_rows = store.all(
        "SELECT e.meta_keys, e.meta_score, e.received_at FROM events e JOIN deliveries d ON d.event_pk = e.id"
        " AND d.destination = 'meta' AND d.status != 'skipped' WHERE e.received_at >= ?",
        (start,),
    )
    google_rows = store.all(
        "SELECT e.google_keys FROM events e JOIN deliveries d ON d.event_pk = e.id"
        " AND d.destination = 'google' AND d.status != 'skipped' WHERE e.received_at >= ?",
        (start,),
    )
    meta_keys = Counter(k for r in meta_rows for k in r["meta_keys"].split(",") if k)
    google_keys = [set(filter(None, r["google_keys"].split(","))) for r in google_rows]
    google_counts = Counter(k for keys in google_keys for k in keys)
    scores = [r["meta_score"] for r in meta_rows]
    by_day: dict[str, list[float]] = defaultdict(list)
    for r in meta_rows:
        by_day[_day(r["received_at"])].append(r["meta_score"])
    bands = Counter(quality.meta_rating(s) for s in scores)
    return {
        "meta": {
            "events": len(meta_rows),
            "score": round(sum(scores) / len(scores), 1) if scores else None,
            "rating": quality.meta_rating(sum(scores) / len(scores)) if scores else None,
            "bands": {band: bands[band] for band in ("Great", "Good", "OK", "Poor")},
            "keys": [
                {"key": key, "label": label, "coverage": pct(meta_keys[key], len(meta_rows))}
                for key, label in quality.META_KEY_LABELS.items()
            ],
            "daily": [
                {"day": day, "score": round(sum(by_day[day]) / len(by_day[day]), 2) if by_day[day] else None}
                for day in _days(now, days)
            ],
        },
        "google": {
            "events": len(google_rows),
            "user_data": pct(sum(1 for keys in google_keys if quality.google_has_user_data(keys)), len(google_rows)),
            "click_id": pct(sum(1 for keys in google_keys if keys & {"gclid", "gbraid", "wbraid"}), len(google_rows)),
            "keys": [
                {"key": key, "label": label, "coverage": pct(google_counts[key], len(google_rows))}
                for key, label in quality.GOOGLE_KEY_LABELS.items()
            ],
        },
    }


def platform_quality(store: Store, start: float) -> dict:
    """What the platforms say: Meta's latest Dataset Quality snapshot; Google's processing results."""
    meta = None
    if snapshot := store.latest_snapshot("meta"):
        taken_at, data = snapshot
        meta = {"taken_at": taken_at, "events": [_meta_event(row) for row in data.get("events", {}).values()]}
    statuses = Counter()
    errors: Counter = Counter()
    warnings: Counter = Counter()
    records = 0
    for row in store.all(
        "SELECT processing_status, diagnostics, event_count FROM requests"
        " WHERE destination = 'google' AND ok = 1 AND started_at >= ?",
        (start,),
    ):
        statuses[row["processing_status"] or "VALIDATE_ONLY"] += 1
        records += row["event_count"]
        diagnostics = json.loads(row["diagnostics"]) if row["diagnostics"] else {}
        for e in diagnostics.get("errors", []):
            errors[e["reason"]] += e["count"]
        for w in diagnostics.get("warnings", []):
            warnings[w["reason"]] += w["count"]
    rejected = sum(errors.values())
    google = {
        "requests": dict(statuses),
        "records": records,
        "rejected": rejected,
        "accepted_rate": pct(records - rejected, records),
        "errors": errors.most_common(),
        "warnings": warnings.most_common(),
    }
    return {"meta": meta, "google": google}


def _meta_event(row: dict) -> dict:
    emq = row.get("event_match_quality") or {}
    score = emq.get("composite_score")
    delay = row.get("event_delay") or {}
    return {
        "event_name": row.get("event_name"),
        "score": score,
        "rating": quality.meta_rating(score) if score is not None else None,
        "keys": {
            k.get("identifier"): (k.get("coverage") or {}).get("percentage")
            for k in emq.get("match_key_feedback", [])
        },
        "diagnostics": emq.get("diagnostics", []),
        "coverage": (row.get("event_coverage") or {}).get("percentage"),
        "coverage_goal": (row.get("event_coverage") or {}).get("goal_percentage"),
        "dedup": [
            {
                "key": d.get("dedupe_key"),
                "browser": (d.get("browser_events_with_dedupe_key") or {}).get("percentage"),
                "server": (d.get("server_events_with_dedupe_key") or {}).get("percentage"),
                "deduplicated": (d.get("overall_browser_coverage_from_dedupe_key") or {}).get("percentage"),
            }
            for d in row.get("dedupe_key_feedback", [])
        ],
        "freshness": (row.get("data_freshness") or {}).get("upload_frequency"),
        "delayed_2h": delay.get("pct_delayed_over_2h"),
        "delayed_24h": delay.get("pct_delayed_over_24h"),
        "acr": (row.get("acr") or {}).get("percentage"),
    }


def attribution(store: Store, event_name: str, now: float, days: int) -> dict:
    """Orders by the relay's last-click channel against what Meta and Google each report."""
    day_list = _days(now, days)
    first, last = day_list[0], day_list[-1]
    start = datetime.fromisoformat(first).replace(tzinfo=timezone.utc).timestamp()
    internal: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0, 0.0])
    campaigns_internal: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0, 0.0])
    for row in store.all(
        "SELECT event_time, channel, campaign, value FROM events WHERE event_name = ? AND event_time >= ?",
        (event_name, start),
    ):
        day = _day(row["event_time"])
        if day > last:
            continue
        for key in ((day, row["channel"]), (day, "all")):
            internal[key][0] += 1
            internal[key][1] += row["value"] or 0.0
        if row["channel"] in ("meta", "google") and row["campaign"]:
            entry = campaigns_internal[(row["channel"], row["campaign"])]
            entry[0] += 1
            entry[1] += row["value"] or 0.0
    reported: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0.0])
    campaigns_reported: dict[tuple[str, str], list] = {}
    for row in store.all(
        "SELECT platform, day, campaign_id, campaign_name, conversions, value FROM platform_reports"
        " WHERE day BETWEEN ? AND ?",
        (first, last),
    ):
        reported[(row["day"], row["platform"])][0] += row["conversions"]
        reported[(row["day"], row["platform"])][1] += row["value"]
        entry = campaigns_reported.setdefault((row["platform"], row["campaign_id"]), [row["campaign_name"], 0.0, 0.0])
        entry[1] += row["conversions"]
        entry[2] += row["value"]
    daily = [
        {
            "day": day,
            "orders": internal[(day, "all")][0],
            **{f"internal_{c}": internal[(day, c)][0] for c in CHANNELS},
            "meta_reported": reported[(day, "meta")][0],
            "google_reported": reported[(day, "google")][0],
        }
        for day in day_list
    ]
    orders = sum(d["orders"] for d in daily)
    revenue = sum(internal[(day, "all")][1] for day in day_list)
    platforms = {}
    for platform in ("meta", "google"):
        conversions = sum(internal[(day, platform)][0] for day in day_list)
        value = sum(internal[(day, platform)][1] for day in day_list)
        claimed = sum(reported[(day, platform)][0] for day in day_list)
        claimed_value = sum(reported[(day, platform)][1] for day in day_list)
        platforms[platform] = {
            "internal": conversions,
            "internal_value": round(value, 2),
            "reported": round(claimed, 2),
            "reported_value": round(claimed_value, 2),
            "ratio": round(claimed / conversions, 2) if conversions else None,
            "share_of_orders": pct(claimed, orders),
        }
    claims = platforms["meta"]["reported"] + platforms["google"]["reported"]
    # What Meta and Google claim together, against what the last click gives the two of them.
    paid = platforms["meta"]["internal"] + platforms["google"]["internal"]
    fetched = store.scalar("SELECT MAX(fetched_at) FROM platform_reports")
    return {
        "event_name": event_name,
        "first_day": first,
        "last_day": last,
        "orders": orders,
        "revenue": round(revenue, 2),
        "by_channel": {c: sum(internal[(day, c)][0] for day in day_list) for c in CHANNELS},
        "platforms": platforms,
        "claims": round(claims, 2),
        "claims_vs_orders": round(claims / orders, 2) if orders else None,
        "internal_paid": paid,
        "claims_vs_internal": round(claims / paid, 2) if paid else None,
        "daily": daily,
        "campaigns": _campaigns(campaigns_internal, campaigns_reported),
        "reports_fetched_at": fetched,
    }


def _campaigns(internal: dict, reported: dict) -> list[dict]:
    """Join on campaign id (the site's utm_id), falling back to the campaign name (utm_campaign).
    internal: (channel, campaign) -> [conversions, value]; reported: (platform, id) -> [name, conversions, value]."""
    by_name = {(platform, (entry[0] or "").lower()): (platform, cid) for (platform, cid), entry in reported.items()}
    rows = []
    joined = set()
    for (platform, campaign), (count, _value) in internal.items():
        key = (platform, campaign) if (platform, campaign) in reported else by_name.get((platform, campaign.lower()))
        name, claimed = (reported[key][0], reported[key][1]) if key else (campaign, 0.0)
        if key:
            joined.add(key)
        rows.append({"platform": platform, "campaign": name or campaign, "internal": count,
                     "reported": round(claimed, 2), "ratio": round(claimed / count, 2)})
    for key, (name, claimed, _value) in reported.items():
        if key not in joined:
            rows.append({"platform": key[0], "campaign": name or key[1], "internal": 0, "reported": round(claimed, 2),
                         "ratio": None})
    return sorted(rows, key=lambda r: (r["platform"], -max(r["internal"], r["reported"])))
