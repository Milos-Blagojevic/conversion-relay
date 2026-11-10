"""The HTTP surface: access rules, intake responses, dashboard and demo pages."""

import base64
import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from support import NOW, event

from relay import demo
from relay.app import create_app

LOCAL = "http://127.0.0.1"


def client(relay, **settings) -> TestClient:
    relay.settings = replace(relay.settings, **settings)
    return TestClient(create_app(relay, start_background=False), base_url=LOCAL)


def payload(**overrides) -> dict:
    return event(**overrides).model_dump(mode="json")


def basic(password: str) -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"owner:{password}".encode()).decode()}


def test_events_need_the_api_key_when_one_is_set(live):
    c = client(live, api_key="k3y")
    assert c.post("/v1/events", json=payload()).status_code == 401
    assert c.post("/v1/events", json=payload(), headers={"Authorization": "Bearer nope"}).status_code == 401
    response = c.post("/v1/events", json=payload(), headers={"Authorization": "Bearer k3y"})
    assert response.status_code == 202
    assert response.json() == {"event_id": "order-1", "duplicate": False,
                               "deliveries": {"meta": "pending", "google": "pending"}}
    again = c.post("/v1/events", json=payload(), headers={"Authorization": "Bearer k3y"})
    assert (again.status_code, again.json()["duplicate"]) == (200, True)


def test_batch_reports_each_event(live):
    c = client(live)
    body = {"events": [payload(), payload(event_id="later", event_time=NOW + 3600)]}
    results = c.post("/v1/events/batch", json=body).json()["results"]
    assert results[0]["duplicate"] is False
    assert results[1] == {"event_id": "later", "error": "event_time is in the future"}


def test_invalid_events_are_refused_with_the_reason(live):
    c = client(live)
    response = c.post("/v1/events", json=payload() | {"currency": "dollars"})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "currency"]
    # An infinite value would make the platform request unserialisable and stall the whole batch.
    body = json.dumps(payload()).replace('"value": 49.0', '"value": 1e999')
    response = c.post("/v1/events", content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "value"]


def test_beacons_are_public_and_recorded(live):
    c = client(live, api_key="k3y", dashboard_password="pw")
    response = c.post("/v1/beacons", json={"event_id": "order-1", "tag": "meta_pixel"})
    assert response.status_code == 202
    assert live.store.scalar("SELECT tag FROM beacons") == "meta_pixel"


def test_dashboard_needs_the_password_when_one_is_set(live):
    c = client(live, dashboard_password="pw")
    assert c.get("/").status_code == 401
    assert c.get("/", headers=basic("wrong")).status_code == 401
    assert c.get("/", headers=basic("pw")).status_code == 200


def test_without_secrets_only_loopback_hosts_are_served(live):
    c = client(live)
    assert c.get("/").status_code == 200
    assert c.get("/", headers={"Host": "relay.example.com"}).status_code == 403
    # Without RELAY_API_KEY a page elsewhere could post fake conversions through DNS rebinding.
    assert c.post("/v1/events", json=payload(), headers={"Host": "relay.example.com"}).status_code == 403
    assert c.post("/v1/events", json=payload(), headers={"Host": "localhost:8000"}).status_code == 202


def test_cross_origin_posts_to_the_dashboard_are_refused(live):
    c = client(live)
    assert c.post("/api/monitor/refresh", headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.post("/api/monitor/refresh", headers={"Origin": LOCAL}).status_code == 200


def test_the_demo_exists_only_in_sandbox_mode(live):
    assert client(live).get("/demo").status_code == 404


@pytest.fixture
def seeded(sandbox):
    demo.seed(sandbox, days=4, orders_per_day=30, broken_days=2, seed=3)
    return sandbox


def test_dashboard_events_and_metrics_render_from_seeded_history(seeded):
    c = client(seeded)
    page = c.get("/?days=7")
    assert page.status_code == 200
    for heading in ("Deduplication", "Event match quality", "Latency", "Delivery coverage", "Attribution"):
        assert heading in page.text
    assert c.get("/events").status_code == 200
    m = c.get("/api/metrics?days=7").json()
    daily = [d["rate"] for d in m["coverage"]["meta_pixel"]["daily_key_match"] if d["rate"] is not None]
    # Broken days share no keys; on fixed days every tag matches a server event, except on the last day
    # where some delayed server events have not arrived yet.
    assert daily[:3] == [0.0, 0.0, 100.0] and daily[3] > 80


def test_demo_order_flow_counts_broken_orders_twice(sandbox):
    c = client(sandbox)
    for scenario in ("shared", "broken"):
        order = c.post("/demo/order", json={"scenario": scenario, "source": "meta_click"}).json()
        c.post("/demo/tags", json={"order_id": order["order_id"]})
        for tag, event_id in order["beacons"].items():
            c.post("/v1/beacons", json={"event_id": event_id, "event_name": "Purchase", "tag": tag})
        assert c.post("/demo/checkout", json={"order_id": order["order_id"]}).status_code == 200
    sandbox.dispatcher.run_due()
    state = c.get("/demo/state").json()
    counted = {o["scenario"]: sum(h["counted"] for h in o["meta"]) for o in state["orders"]}
    assert counted == {"shared": 1, "broken": 2}
    assert state["totals"]["orders"] == 2 and state["totals"]["meta_counted"] == 3
