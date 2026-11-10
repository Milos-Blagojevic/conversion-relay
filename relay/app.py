"""The FastAPI service: event intake (`/v1`), the monitoring dashboard, its JSON, and the sandbox demo.

Access:
- `/v1/events*`: server to server. With RELAY_API_KEY set, requires `Authorization: Bearer <key>`.
- `/v1/beacons`: called by the shop's pages when a browser tag fires; public, CORS for ALLOWED_ORIGINS.
- Everything else (dashboard, `/api`, `/demo`, `/docs`): HTTP basic auth with DASHBOARD_PASSWORD. Without a
  password only loopback hosts are served (no DNS rebinding onto a local instance). Non-GET requests
  from a browser must come from the same origin.
"""

from __future__ import annotations

import base64
import hmac
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from relay import charts, demo, metrics, quality
from relay.models import Batch, Beacon, EventIn
from relay.runtime import InvalidEvent, Relay

HERE = Path(__file__).parent
LOOPBACK = {"localhost", "127.0.0.1", "::1"}
WINDOWS = (1, 7, 14, 30)
SERVER_PATHS = ("/v1/events",)
PUBLIC_PATHS = ("/healthz", "/v1/beacons")


def _host(request: Request) -> str:
    host = request.headers.get("host", "")
    return urlparse(f"//{host}").hostname or ""


def _basic_ok(header: str, password: str) -> bool:
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        _, _, given = base64.b64decode(encoded).decode().partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    return hmac.compare_digest(given.encode(), password.encode())


class DemoOrder(BaseModel):
    scenario: Literal["shared", "broken"] = "shared"
    source: Literal[demo.SOURCES] = "meta_click"  # type: ignore[valid-type]
    blocked: bool = False
    consent: bool = True
    email: bool = True
    phone: bool = False


class DemoRef(BaseModel):
    order_id: str


def create_app(relay: Relay, start_background: bool = True) -> FastAPI:
    settings = relay.settings

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_background:
            relay.start()
        try:
            yield
        finally:
            if start_background:
                relay.stop()

    app = FastAPI(title="conversion-relay", version="1.0", lifespan=lifespan)
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(mode=settings.mode, destinations=list(relay.destinations), windows=WINDOWS,
                                 identifiers=quality.META_KEY_IDENTIFIERS)
    templates.env.filters["ms"] = lambda v: "–" if v is None else f"{v / 1000:.1f} s" if v >= 1000 else f"{v:.0f} ms"
    templates.env.filters["pct"] = lambda v: "–" if v is None else f"{v:.1f}%"
    templates.env.filters["num"] = lambda v: "–" if v is None else f"{v:,.0f}" if float(v).is_integer() else f"{v:,.1f}"
    templates.env.filters["ago"] = _ago
    templates.env.filters["when"] = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    live_demo = demo.LiveDemo(relay) if settings.sandbox else None

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if path in PUBLIC_PATHS or request.method == "OPTIONS" and path.startswith("/v1/"):
            return await call_next(request)
        if path.startswith(SERVER_PATHS):
            if settings.api_key:
                if not hmac.compare_digest(
                    request.headers.get("authorization", "").encode(), f"Bearer {settings.api_key}".encode()
                ):
                    return JSONResponse({"detail": "missing or wrong API key"}, status_code=401)
            elif _host(request) not in LOOPBACK:
                return JSONResponse({"detail": "set RELAY_API_KEY to accept events beyond localhost"}, status_code=403)
            return await call_next(request)
        if settings.dashboard_password:
            if not _basic_ok(request.headers.get("authorization", ""), settings.dashboard_password):
                return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="conversion-relay"'})
        elif _host(request) not in LOOPBACK:
            return Response("Set DASHBOARD_PASSWORD to serve the dashboard beyond localhost", 403)
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD") and origin and urlparse(origin).netloc != request.headers.get("host"):
            return Response("Cross-origin request refused", 403)
        return await call_next(request)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_methods=["POST"],
        allow_headers=["Content-Type"],
    )
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's default echoes each bad input back, which fails on values JSON can not hold (inf)
        # and repeats customer data; location and message are enough.
        errors = [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        return JSONResponse({"detail": errors}, status_code=422)

    # Intake

    @app.post("/v1/events", status_code=202)
    def post_event(event: EventIn, response: Response) -> dict:
        try:
            result = relay.ingest(event)
        except InvalidEvent as exc:
            raise HTTPException(422, str(exc)) from None
        if result.duplicate:
            response.status_code = 200
        return {"event_id": event.event_id, "duplicate": result.duplicate, "deliveries": result.deliveries}

    @app.post("/v1/events/batch", status_code=202)
    def post_batch(batch: Batch) -> dict:
        results = []
        for event in batch.events:
            try:
                result = relay.ingest(event)
            except InvalidEvent as exc:
                results.append({"event_id": event.event_id, "error": str(exc)})
                continue
            results.append({"event_id": event.event_id, "duplicate": result.duplicate, "deliveries": result.deliveries})
        return {"results": results}

    @app.post("/v1/beacons", status_code=202)
    def post_beacon(beacon: Beacon) -> dict:
        relay.store.add_beacon(beacon.event_id, beacon.event_name, beacon.tag, relay.clock.now())
        return {"ok": True}

    @app.get("/healthz")
    def health() -> dict:
        return {"ok": True, "mode": settings.mode, "destinations": list(relay.destinations),
                "pending": relay.store.pending_count()}

    # Dashboard

    def collect(days: int) -> dict:
        return metrics.collect(relay.store, settings, list(relay.destinations), relay.clock.now(), days)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, days: int = Query(7, ge=1, le=90)):
        m = collect(days)
        return templates.TemplateResponse(request, "dashboard.html", {"m": m, "days": days, "charts": _charts(m)})

    @app.get("/events", response_class=HTMLResponse)
    def events(request: Request, limit: int = Query(100, ge=1, le=500)):
        rows = relay.store.all("SELECT * FROM events ORDER BY received_at DESC, id DESC LIMIT ?", (limit,))
        deliveries: dict[int, dict] = {}
        if rows:
            marks = ",".join("?" for _ in rows)
            for d in relay.store.all(
                f"SELECT d.*, (d.sent_at - e.received_at) * 1000 AS ms FROM deliveries d JOIN events e"
                f" ON e.id = d.event_pk WHERE d.event_pk IN ({marks})",
                [r["id"] for r in rows],
            ):
                deliveries.setdefault(d["event_pk"], {})[d["destination"]] = d
        return templates.TemplateResponse(
            request, "events.html",
            {"rows": rows, "deliveries": deliveries, "labels": quality.META_KEY_LABELS, "rating": quality.meta_rating},
        )

    @app.get("/api/metrics")
    def api_metrics(days: int = Query(7, ge=1, le=90)) -> dict:
        return collect(days)

    @app.post("/api/monitor/refresh")
    def refresh(request: Request, days: int = Query(7, ge=1, le=90)):
        results = relay.monitor.refresh()
        if "text/html" in request.headers.get("accept", ""):
            return RedirectResponse(f"/?days={days}", status_code=303)
        return {"results": [{"task": t, "ok": ok, "detail": detail} for t, ok, detail in results]}

    # Sandbox demo

    def need_demo() -> demo.LiveDemo:
        if live_demo is None:
            raise HTTPException(404, "The demo runs in sandbox mode only")
        return live_demo

    @app.get("/demo", response_class=HTMLResponse)
    def demo_page(request: Request):
        need_demo()
        return templates.TemplateResponse(request, "demo.html", {"sources": demo.SOURCES})

    @app.post("/demo/order")
    def demo_order(options: DemoOrder) -> dict:
        order = need_demo().place(**options.model_dump())
        return {
            "order_id": order.order_id,
            "value": order.value,
            "tags_fire": not order.blocked and order.consent,
            "beacons": demo.beacon_ids(order),
        }

    @app.post("/demo/tags")
    def demo_tags(ref: DemoRef) -> dict:
        order = need_demo().get(ref.order_id)
        if order is None:
            raise HTTPException(404, "unknown order")
        return {"fired": demo.fire_platform_tags(relay, order)}

    @app.post("/demo/checkout")
    def demo_checkout(ref: DemoRef) -> dict:
        order = need_demo().get(ref.order_id)
        if order is None:
            raise HTTPException(404, "unknown order")
        result = relay.ingest(demo.server_event(order))
        return {"duplicate": result.duplicate, "deliveries": result.deliveries}

    @app.get("/demo/state")
    def demo_state() -> dict:
        return need_demo().state()

    return app


def _ago(ts: float | None) -> str:
    if ts is None:
        return "never"
    seconds = max(0, datetime.now(timezone.utc).timestamp() - ts)
    if seconds < 90:
        return f"{seconds:.0f} s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min ago"
    if seconds < 172800:
        return f"{seconds / 3600:.0f} h ago"
    return f"{seconds / 86400:.0f} d ago"


def _charts(m: dict) -> dict:
    a = m["attribution"]
    days = [d["day"] for d in a["daily"]]
    out = {
        "meta_attribution": charts.bars(days, [[d["internal_meta"] for d in a["daily"]],
                                               [d["meta_reported"] for d in a["daily"]]]),
        "google_attribution": charts.bars(days, [[d["internal_google"] for d in a["daily"]],
                                                 [d["google_reported"] for d in a["daily"]]]),
        "orders": charts.bars(days, [[d["orders"] for d in a["daily"]],
                                     [d["meta_reported"] + d["google_reported"] for d in a["daily"]]]),
    }
    latency_days = None
    series = []
    for name in m["destinations"]:
        daily = m["latency"][name]["daily"]
        latency_days = [d["day"] for d in daily]
        series.append([d["p95_ms"] for d in daily])
    if latency_days:
        out["latency"] = charts.lines(latency_days, series, " ms")
    q = m["quality"]["meta"]["daily"]
    out["score"] = charts.lines([d["day"] for d in q], [[d["score"] for d in q]], top=10)
    key_match = [m["coverage"][tag]["daily_key_match"] for tag in ("meta_pixel", "google_tag") if m["coverage"][tag]]
    if key_match:
        out["key_match"] = charts.lines([d["day"] for d in key_match[0]], [[d["rate"] for d in s] for s in key_match],
                                        "%", top=100)
    return out
