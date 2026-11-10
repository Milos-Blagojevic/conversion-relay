"""Dashboard numbers computed from known store contents."""

from zoneinfo import ZoneInfo

from support import NOW, event

from relay import metrics

UTC = ZoneInfo("UTC")


def today() -> metrics.Window:
    return metrics.window(NOW, 1, UTC)


def test_percentile_interpolates():
    assert metrics.percentile([], 50) is None
    assert metrics.percentile([100.0], 95) == 100.0
    assert metrics.percentile([0.0, 10.0, 20.0, 30.0, 40.0], 50) == 20.0
    assert metrics.percentile([0.0, 100.0], 95) == 95.0


def test_the_window_is_whole_days_in_the_report_time_zone():
    w = metrics.window(NOW, 2, ZoneInfo("America/Los_Angeles"))  # NOW is 04:00 on Nov 10 in Los Angeles
    assert w.days == ["2026-11-09", "2026-11-10"]
    assert w.start == NOW - 4 * 3600 - 86400
    assert w.day(NOW - 5 * 3600) == "2026-11-09"


def test_coverage_pairs_beacons_with_server_events_by_dedup_key(live):
    live.ingest(event(event_id="o1", order_id="o1"))   # tag fired with the same key
    live.ingest(event(event_id="o2", order_id="o2"))   # no tag: ad blocker
    live.ingest(event(event_id="o3", order_id="o3"))   # tag fired with a made-up key
    live.ingest(event(event_id="o4", order_id="o4", consent={"ad_user_data": False}))  # nothing fires or is sent
    live.store.add_beacon("o1", "Purchase", "meta_pixel", NOW)
    live.store.add_beacon("px-123", "Purchase", "meta_pixel", NOW)
    live.store.add_beacon("o1", "Purchase", "google_tag", NOW)

    coverage = metrics.coverage(live.store, today())
    meta = coverage["meta_pixel"]
    assert (meta["browser"], meta["server"], meta["both"], meta["server_only"], meta["browser_only"]) == (2, 3, 1, 2, 1)
    assert meta["key_match_rate"] == 50.0
    assert coverage["google_tag"]["key_match_rate"] == 100.0


def test_delivery_coverage_counts_skipped_events_but_delivery_rate_does_not(live):
    live.ingest(event(event_id="a"))
    live.ingest(event(event_id="b", consent={"ad_user_data": False}))
    live.store.conn.execute("UPDATE deliveries SET status = 'sent', sent_at = ? WHERE status = 'pending'", (NOW,))
    meta = metrics.delivery(live.store, today(), ["meta", "google"])["meta"]
    assert (meta["total"], meta["delivered"], meta["delivery_rate"], meta["coverage"]) == (2, 1, 100.0, 50.0)
    assert meta["reasons"] == [("skipped: consent", 1)]


def test_latency_measures_from_receipt_to_acceptance(live, clock):
    live.ingest(event(event_id="a"))
    clock.sleep(0.25)
    live.store.conn.execute("UPDATE deliveries SET status = 'sent', sent_at = ?", (clock.now(),))
    latency = metrics.latency(live.store, today(), ["meta"])["meta"]
    assert latency["p50_ms"] == 250.0 and latency["delivered"] == 1
    assert latency["over_2h"] == 0.0


def test_attribution_sets_last_click_against_platform_reports_per_campaign(live):
    live.ingest(event(event_id="g1"))  # gclid, utm_id c-9: Google
    meta_click = {"gclid": None, "fbclid": "IwAR1", "utm_source": "facebook", "utm_medium": "paid_social",
                  "utm_id": None, "utm_campaign": "Prospecting"}
    live.ingest(event(event_id="m1", click=meta_click))
    live.ingest(event(event_id="d1", click={"gclid": None, "utm_source": None, "utm_medium": None, "utm_id": None}))
    day = "2026-11-10"
    live.store.replace_reports("google", day, day, [
        {"day": day, "campaign_id": "c-9", "campaign_name": "Brand", "conversions": 3.0, "value": 147.0}], NOW)
    live.store.replace_reports("meta", day, day, [
        {"day": day, "campaign_id": "120", "campaign_name": "prospecting", "conversions": 2.0, "value": 98.0}], NOW)

    a = metrics.attribution(live.store, "Purchase", today())
    assert a["orders"] == 3 and a["by_channel"] == {"meta": 1, "google": 1, "other": 0, "direct": 1}
    assert a["platforms"]["google"]["ratio"] == 3.0 and a["platforms"]["meta"]["ratio"] == 2.0
    assert (a["claims"], a["internal_paid"], a["claims_vs_internal"]) == (5.0, 2, 2.5)
    campaigns = {(c["platform"], c["campaign"]): (c["internal"], c["reported"]) for c in a["campaigns"]}
    # Google joins on the id (utm_id), Meta on the name (utm_campaign) when no id was passed.
    assert campaigns == {("google", "Brand"): (1, 3.0), ("meta", "prospecting"): (1, 2.0)}


def test_orders_are_dated_in_the_report_time_zone_like_the_platform_reports(live):
    live.ingest(event(event_id="late", event_time=NOW - 5 * 3600))  # 07:00 UTC, 23:00 the day before in LA
    la = metrics.attribution(live.store, "Purchase", metrics.window(NOW, 2, ZoneInfo("America/Los_Angeles")))
    assert [d["orders"] for d in la["daily"]] == [1, 0]
    utc = metrics.attribution(live.store, "Purchase", metrics.window(NOW, 2, UTC))
    assert [d["orders"] for d in utc["daily"]] == [0, 1]
