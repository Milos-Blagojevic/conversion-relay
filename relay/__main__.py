"""Command line: `python -m relay serve | seed | report | refresh`."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

from relay import config, metrics
from relay.clock import ManualClock
from relay.runtime import build

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _settings(args: argparse.Namespace) -> config.Settings:
    env = dict(os.environ)
    if getattr(args, "sandbox", False):
        env["RELAY_MODE"] = "sandbox"
    return config.from_env(env)


def serve(args: argparse.Namespace) -> int:
    import uvicorn

    from relay.app import create_app

    settings = _settings(args)
    if args.host not in LOOPBACK_HOSTS and not (settings.dashboard_password and settings.api_key):
        print("Refusing to listen beyond localhost without DASHBOARD_PASSWORD and RELAY_API_KEY.", file=sys.stderr)
        return 2
    if not settings.sandbox and not (settings.meta or settings.google):
        logging.warning("No destination configured: events are stored but sent nowhere. See .env.example.")
    relay = build(settings)
    uvicorn.run(create_app(relay), host=args.host, port=args.port, log_level="info")
    return 0


def _reset_sandbox(path: str) -> None:
    """Delete a sandbox database, after checking it is one: a live database is never deleted."""
    if not Path(path).exists():
        return
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = 'mode'").fetchone()
    except sqlite3.DatabaseError:
        row = None
    finally:
        conn.close()
    if not row or row[0] != "sandbox":
        raise SystemExit(f"{path} is not a sandbox database; not deleting it")
    for suffix in ("", "-wal", "-shm"):
        Path(path + suffix).unlink(missing_ok=True)


def seed(args: argparse.Namespace) -> int:
    settings = _settings(argparse.Namespace(sandbox=True))
    if args.reset:
        _reset_sandbox(settings.db_path)
    relay = build(settings, clock=ManualClock(time.time()))
    if relay.store.scalar("SELECT COUNT(*) FROM events"):
        print(f"{settings.db_path} already holds events; use --reset to start over.", file=sys.stderr)
        return 2
    started = time.time()
    summary = demo_seed(relay, args)
    print(f"Seeded {summary['orders']} orders over {args.days} days into {settings.db_path} "
          f"in {time.time() - started:.1f} s (first {args.broken_days} days with broken dedup keys).")
    for task, ok, detail in summary["monitor"]:
        print(f"  {task}: {'ok' if ok else 'FAILED'} · {detail}")
    print("Start the dashboard: python -m relay serve --sandbox")
    return 0


def demo_seed(relay, args: argparse.Namespace) -> dict:
    from relay import demo

    return demo.seed(relay, days=args.days, orders_per_day=args.orders_per_day, broken_days=args.broken_days,
                     seed=args.seed)


def refresh(args: argparse.Namespace) -> int:
    relay = build(_settings(args))
    failed = 0
    for task, ok, detail in relay.monitor.refresh():
        print(f"{task}: {'ok' if ok else 'FAILED'} · {detail}")
        failed += not ok
    return 1 if failed else 0


def report(args: argparse.Namespace) -> int:
    settings = _settings(args)
    relay = build(settings)
    m = metrics.collect(relay.store, settings, list(relay.destinations), time.time(), args.days)
    if args.json:
        print(json.dumps(m, indent=2, default=str))
        return 0
    print(f"conversion-relay · {settings.mode} · last {args.days} days · {m['totals']['events']} events")
    for name in m["destinations"]:
        d, lat = m["delivery"][name], m["latency"][name]
        print(f"\n{name}: coverage {_pct(d['coverage'])} (delivery rate {_pct(d['delivery_rate'])}), "
              f"latency p50 {_ms(lat['p50_ms'])} p95 {_ms(lat['p95_ms'])} p99 {_ms(lat['p99_ms'])}, "
              f">2 h late {_pct(lat['over_2h'])}")
        for reason, count in d["reasons"]:
            print(f"  {count} × {reason}")
    q = m["quality"]
    print(f"\nMatch quality: relay estimate {q['meta']['score']} ({q['meta']['rating']}); "
          f"Google events with customer data {_pct(q['google']['user_data'])}")
    if m["platform_quality"]["meta"]:
        for e in m["platform_quality"]["meta"]["events"]:
            print(f"  Meta {e['event_name']}: EMQ {e['score']} ({e['rating']}), event coverage {_pct(e['coverage'])}")
    for tag, c in m["coverage"].items():
        if c:
            print(f"  {tag}: key match {_pct(c['key_match_rate'])}, server-only {c['server_only']}, "
                  f"tag-only {c['browser_only']}")
    a = m["attribution"]
    print(f"\nAttribution ({a['event_name']}, {a['first_day']} to {a['last_day']}): {a['orders']} orders")
    for platform, p in a["platforms"].items():
        ratio = f"{p['ratio']:.2f}x" if p["ratio"] is not None else "–"
        print(f"  {platform}: internal {p['internal']}, platform reports {p['reported']:g} ({ratio})")
    if a["claims_vs_internal"] is not None:
        print(f"  Meta + Google claim {a['claims']:g} conversions; last click gives them {a['internal_paid']} "
              f"({a['claims_vs_internal']:.2f}x) of {a['orders']} orders")
    return 0


def _pct(value: float | None) -> str:
    return "–" if value is None else f"{value:.1f}%"


def _ms(value: float | None) -> str:
    return "–" if value is None else f"{value:.0f} ms"


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="python -m relay", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the API, dispatcher, monitor and dashboard")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    p.add_argument("--sandbox", action="store_true", help="simulate Meta and Google in-process")
    p.set_defaults(func=serve)

    p = sub.add_parser("seed", help="fill the sandbox database with simulated history")
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--orders-per-day", type=int, default=60)
    p.add_argument("--broken-days", type=int, default=7, help="days at the start with broken dedup keys")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--reset", action="store_true", help="delete the sandbox database first")
    p.set_defaults(func=seed)

    p = sub.add_parser("report", help="print the monitoring numbers")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--sandbox", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=report)

    p = sub.add_parser("refresh", help="pull platform data once (quality, processing status, reports)")
    p.add_argument("--sandbox", action="store_true")
    p.set_defaults(func=refresh)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except config.ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
