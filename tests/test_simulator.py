"""The sandbox platforms' dedup and attribution rules, driven through the real relay (no HTTP)."""

import json

from support import NOW, event


def meta_rows(relay, order_ref="order-1"):
    return [dict(r) for r in relay.store.all(
        "SELECT channel, counted, dedup FROM sim_meta WHERE order_ref = ? ORDER BY id", (order_ref,))]


def google_rows(relay, order_ref="order-1"):
    return [dict(r) for r in relay.store.all(
        "SELECT channel, counted, merged FROM sim_google WHERE order_ref = ? ORDER BY id", (order_ref,))]


def pixel(relay, event_id, fbp="fb.1.1.42"):
    relay.simulator.meta_pixel(event_name="Purchase", event_id=event_id, order_ref="order-1", fbp=fbp, fbc=None,
                               value=49.0, event_time=NOW - 60)


def send(relay, **overrides):
    relay.ingest(event(order_id="order-1", **overrides))
    relay.dispatcher.run_due()


def test_meta_counts_a_browser_and_server_pair_once_when_they_share_the_event_id(sandbox):
    pixel(sandbox, "order-1")
    send(sandbox)
    assert meta_rows(sandbox) == [
        {"channel": "browser", "counted": 1, "dedup": None},
        {"channel": "server", "counted": 0, "dedup": "event_id"},
    ]


def test_meta_counts_twice_when_the_ids_differ_and_no_fallback_key_matches(sandbox):
    pixel(sandbox, "px-random")
    send(sandbox, user={"fbp": None})
    assert [r["counted"] for r in meta_rows(sandbox)] == [1, 1]


def test_meta_fbp_fallback_works_only_browser_first(sandbox):
    pixel(sandbox, "px-random", fbp="fb.1.1.42")
    send(sandbox, user={"fbp": "fb.1.1.42"})
    assert meta_rows(sandbox)[1] == {"channel": "server", "counted": 0, "dedup": "fbp"}

    # Server first, browser after: the fallback does not apply, so both count.
    sandbox.ingest(event(event_id="order-2", order_id="order-2", user={"fbp": "fb.1.1.77"}))
    sandbox.dispatcher.run_due()
    sandbox.simulator.meta_pixel(event_name="Purchase", event_id="px-other", order_ref="order-2", fbp="fb.1.1.77",
                                 fbc=None, value=49.0, event_time=NOW - 60)
    assert [r["counted"] for r in meta_rows(sandbox, "order-2")] == [1, 1]


def test_google_merges_a_server_event_into_the_tag_conversion_with_the_same_transaction_id(sandbox):
    sandbox.simulator.google_tag(order_ref="order-1", transaction_id="order-1", value=49.0, gclid=None,
                                 event_time=NOW - 60)
    send(sandbox)
    assert google_rows(sandbox) == [
        {"channel": "tag", "counted": 1, "merged": 0},
        {"channel": "api", "counted": 0, "merged": 1},
    ]


def test_google_counts_twice_when_the_tag_sent_no_transaction_id(sandbox):
    sandbox.simulator.google_tag(order_ref="order-1", transaction_id=None, value=49.0, gclid=None, event_time=NOW - 60)
    send(sandbox)
    assert [r["counted"] for r in google_rows(sandbox)] == [1, 1]


def test_google_reports_a_repeated_transaction_id_as_a_processing_error(sandbox, clock):
    send(sandbox)
    # Forget the event so the relay's own idempotency lets the same transaction through again.
    sandbox.store.conn.execute("DELETE FROM deliveries")
    sandbox.store.conn.execute("DELETE FROM events")
    send(sandbox)
    clock.sleep(60)
    sandbox.monitor.refresh()
    reasons = [
        e["reason"]
        for row in sandbox.store.all(
            "SELECT diagnostics FROM requests WHERE destination = 'google'"
            " AND processing_status IN ('SUCCESS', 'PARTIAL_SUCCESS', 'FAILED')"
        )
        for e in json.loads(row["diagnostics"])["errors"]
    ]
    assert reasons == ["PROCESSING_ERROR_REASON_DUPLICATE_TRANSACTION_ID"]


def test_platforms_credit_their_own_last_touch_so_both_claim_one_order(sandbox):
    sandbox.simulator.add_journey("order-1", [
        {"channel": "meta", "kind": "click", "at": NOW - 86400, "campaign_id": "m1", "campaign_name": "Meta 1"},
        {"channel": "google", "kind": "click", "at": NOW - 3600, "campaign_id": "g1", "campaign_name": "Google 1"},
    ])
    send(sandbox)
    sandbox.monitor.refresh()
    reports = {r["platform"]: (r["campaign_id"], r["conversions"])
               for r in sandbox.store.all("SELECT * FROM platform_reports")}
    assert reports == {"meta": ("m1", 1.0), "google": ("g1", 1.0)}
    assert sandbox.store.scalar("SELECT channel FROM events") == "google", "internal last click credits Google only"
