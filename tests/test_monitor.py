"""Platform data the monitor pulls in live mode: Meta Dataset Quality and Insights, Google Ads reporting."""

import json
from urllib.parse import parse_qs, urlparse

import responses
from support import token

from relay import metrics

QUALITY = "https://graph.facebook.com/v27.0/dataset_quality"
INSIGHTS = "https://graph.facebook.com/v27.0/act_222/insights"
SEARCH = "https://googleads.googleapis.com/v26/customers/1234567890/googleAds:search"


def mock_meta(http):
    http.add(responses.GET, QUALITY, json={"web": [{
        "event_name": "Purchase",
        "event_match_quality": {
            "composite_score": 7.4,
            "match_key_feedback": [{"identifier": "email", "coverage": {"percentage": 91.0}}],
            "diagnostics": [],
        },
        "event_coverage": {"percentage": 88.0, "goal_percentage": 75},
        "dedupe_key_feedback": [{"dedupe_key": "event_id",
                                 "browser_events_with_dedupe_key": {"percentage": 100},
                                 "server_events_with_dedupe_key": {"percentage": 100},
                                 "overall_browser_coverage_from_dedupe_key": {"percentage": 86.0}}],
        "data_freshness": {"upload_frequency": "real_time"},
        "event_delay": {"pct_delayed_over_2h": 1.5, "pct_delayed_over_24h": 0.0},
    }]})
    page_two = f"{INSIGHTS}?after=abc"
    http.add(responses.GET, page_two, json={"data": [
        {"date_start": "2026-11-09", "campaign_id": "c2", "campaign_name": "Retargeting",
         "actions": [{"action_type": "offsite_conversion.fb_pixel_purchase", "value": "2"}],
         "action_values": [{"action_type": "offsite_conversion.fb_pixel_purchase", "value": "80.5"}]},
    ], "paging": {}})
    http.add(responses.GET, INSIGHTS, json={"data": [
        {"date_start": "2026-11-09", "campaign_id": "c1", "campaign_name": "Prospecting",
         "actions": [{"action_type": "offsite_conversion.fb_pixel_purchase", "value": "3"},
                     {"action_type": "link_click", "value": "40"}],
         "action_values": [{"action_type": "offsite_conversion.fb_pixel_purchase", "value": "120"}]},
    ], "paging": {"next": page_two}})


def test_meta_quality_and_reported_conversions_are_stored(live, http):
    live.destinations.pop("google")
    mock_meta(http)
    results = {task: ok for task, ok, _ in live.monitor.refresh()}
    assert results == {"meta_quality": True, "meta_report": True}

    quality = metrics.platform_quality(live.store, 0)["meta"]["events"][0]
    assert (quality["score"], quality["rating"], quality["coverage"]) == (7.4, "Good", 88.0)
    assert quality["keys"] == {"email": 91.0}
    rows = [tuple(r) for r in live.store.all(
        "SELECT day, campaign_id, conversions, value FROM platform_reports WHERE platform = 'meta' ORDER BY campaign_id")]
    assert rows == [("2026-11-09", "c1", 3.0, 120.0), ("2026-11-09", "c2", 2.0, 80.5)]

    insights = parse_qs(urlparse(http.calls[1].request.url).query)
    assert insights["action_report_time"] == ["conversion"]
    assert json.loads(insights["time_range"][0]) == {"since": "2026-10-12", "until": "2026-11-10"}


def test_google_report_pages_through_results_with_the_ads_api_headers(live, http):
    live.destinations.pop("meta")
    token(http)

    def search(request):
        page = json.loads(request.body).get("pageToken")
        row = {"segments": {"date": "2026-11-09"}, "campaign": {"id": "g2" if page else "g1", "name": "Search"},
               "metrics": {"allConversionsByConversionDate": 1.5, "allConversionsValueByConversionDate": 60.0}}
        return 200, {}, json.dumps({"results": [row]} | ({} if page else {"nextPageToken": "p2"}))

    http.add_callback(responses.POST, SEARCH, callback=search)
    results = {task: ok for task, ok, _ in live.monitor.refresh()}
    assert results["google_report"] is True

    call = next(c for c in http.calls if c.request.url == SEARCH)
    assert call.request.headers["developer-token"] == "dev-token"
    assert call.request.headers["login-customer-id"] == "9998887777"
    query = json.loads(call.request.body)["query"]
    assert "segments.conversion_action = 'customers/1234567890/conversionActions/555'" in query
    assert "BETWEEN '2026-10-12' AND '2026-11-10'" in query
    assert live.store.scalar("SELECT SUM(conversions) FROM platform_reports WHERE platform = 'google'") == 3.0


def test_a_failing_platform_call_is_recorded_and_the_others_still_run(live, http):
    live.destinations.pop("google")
    http.add(responses.GET, QUALITY, status=400, json={"error": {"message": "Missing permission"}})
    http.add(responses.GET, INSIGHTS, json={"data": [], "paging": {}})
    results = {task: (ok, detail) for task, ok, detail in live.monitor.refresh()}
    assert results["meta_quality"] == (False, "Meta 400: Missing permission")
    assert results["meta_report"][0] is True
    assert {r["task"]: r["ok"] for r in live.store.last_monitor_runs()} == {"meta_quality": 0, "meta_report": 1}
