"""Ingest and delivery in live mode, with Meta and Google mocked at the HTTP level."""

import json
from dataclasses import replace

import pytest
import responses
from support import GOOGLE, GOOGLE_INGEST, GOOGLE_STATUS, META_EVENTS, NOW, event, token

from relay import config, runtime
from relay.dispatcher import MAX_ATTEMPTS
from relay.runtime import InvalidEvent


def deliveries(relay) -> dict:
    return {row["destination"]: row for row in relay.store.all("SELECT * FROM deliveries")}


def body(call) -> dict:
    return json.loads(call.request.body)


def test_a_repeated_event_is_stored_and_sent_once(live, http):
    first = live.ingest(event())
    again = live.ingest(event(value=99.0))
    assert not first.duplicate and again.duplicate
    assert again.event_pk == first.event_pk
    assert live.store.scalar("SELECT COUNT(*) FROM deliveries") == 2
    assert live.store.scalar("SELECT duplicate_hits FROM events") == 1


def test_refused_consent_skips_both_platforms(live, http):
    live.ingest(event(consent={"ad_user_data": False}))
    live.dispatcher.run_due()
    rows = deliveries(live)
    assert {(r["status"], r["reason"]) for r in rows.values()} == {("skipped", "consent")}
    assert not http.calls


def test_google_gets_only_its_configured_events(live):
    result = live.ingest(event(event_name="Lead"))
    assert result.deliveries == {"meta": "pending"}


def test_meta_skips_website_events_without_a_user_agent(live):
    result = live.ingest(event(user={"user_agent": None}))
    assert result.deliveries["meta"] == "skipped"
    assert live.store.scalar("SELECT reason FROM deliveries WHERE destination = 'meta'") == "website event without user agent"


def test_events_from_the_future_are_refused(live):
    with pytest.raises(InvalidEvent):
        live.ingest(event(event_time=NOW + 3600))


def test_meta_receives_hashed_user_data_and_the_shared_event_id(live, http):
    http.add(responses.POST, META_EVENTS, json={"events_received": 1, "fbtrace_id": "T1"})
    token(http)
    http.add(responses.POST, GOOGLE_INGEST, json={"requestId": "r-1"})
    live.ingest(event())
    live.dispatcher.run_due()

    sent = body(next(c for c in http.calls if c.request.url == META_EVENTS))
    assert sent["access_token"] == "meta-token" and sent["test_event_code"] == "TEST123"
    (meta_event,) = sent["data"]
    assert meta_event["event_id"] == "order-1" and meta_event["event_name"] == "Purchase"
    assert meta_event["event_time"] == int(NOW - 60)
    assert meta_event["custom_data"] == {"value": 49.0, "currency": "USD"}
    assert len(meta_event["user_data"]["em"][0]) == 64 and "@" not in json.dumps(meta_event)
    rows = deliveries(live)
    assert rows["meta"]["status"] == "sent" and rows["meta"]["payload"] is None
    assert live.store.scalar("SELECT platform_ref FROM requests WHERE destination = 'meta'") == "T1"


def test_google_receives_a_multi_source_event_keyed_by_transaction_id(live, http):
    http.add(responses.POST, META_EVENTS, json={"events_received": 1})
    token(http, "access-xyz")
    http.add(responses.POST, GOOGLE_INGEST, json={"requestId": "r-1"})
    live.ingest(event(order_id="T-1001"))
    live.dispatcher.run_due()

    call = next(c for c in http.calls if c.request.url == GOOGLE_INGEST)
    assert call.request.headers["Authorization"] == "Bearer access-xyz"
    sent = body(call)
    assert sent["encoding"] == "HEX" and sent["validateOnly"] is False
    assert sent["destinations"] == [
        {
            "operatingAccount": {"accountType": "GOOGLE_ADS", "accountId": "1234567890"},
            "productDestinationId": "555",
            "loginAccount": {"accountType": "GOOGLE_ADS", "accountId": "9998887777"},
        }
    ]
    (google_event,) = sent["events"]
    assert google_event["transactionId"] == "T-1001"
    assert google_event["eventSource"] == "WEB"
    assert google_event["adIdentifiers"]["gclid"].startswith("Cj0KCQiA")
    assert google_event["eventDeviceInfo"] == {"userAgent": "Mozilla/5.0", "ipAddress": "203.0.113.7"}
    assert google_event["consent"] == {"adUserData": "CONSENT_GRANTED", "adPersonalization": "CONSENT_GRANTED"}
    assert live.store.scalar("SELECT processing_status FROM requests WHERE destination = 'google'") == "PROCESSING"


def test_a_platform_outage_is_retried_with_backoff(live, http, clock):
    http.add(responses.POST, META_EVENTS, status=500, json={"error": {"message": "boom", "code": 2}})
    http.add(responses.POST, META_EVENTS, json={"events_received": 1})
    live.destinations.pop("google")
    live.ingest(event())
    live.dispatcher.run_due()
    row = deliveries(live)["meta"]
    assert (row["status"], row["attempts"]) == ("pending", 1)
    assert row["next_attempt_at"] > clock.now()

    live.dispatcher.run_due()
    assert len(http.calls) == 1, "not due yet"
    clock.sleep(60)
    live.dispatcher.run_due()
    assert deliveries(live)["meta"]["status"] == "sent"


def test_a_rejected_batch_is_split_so_one_bad_event_does_not_sink_the_rest(live, http):
    live.destinations.pop("google")
    rejected = {"error": {"message": "Invalid parameter", "code": 100, "error_user_msg": "bad value"}}

    def answer(request):
        events = json.loads(request.body)["data"]
        if any(e["event_id"] == "bad" for e in events):
            return 400, {}, json.dumps(rejected)
        return 200, {}, json.dumps({"events_received": len(events)})

    http.add_callback(responses.POST, META_EVENTS, callback=answer)
    live.ingest(event(event_id="good"))
    live.ingest(event(event_id="bad"))
    live.dispatcher.run_due()
    statuses = dict(live.store.all("SELECT e.event_id, d.status FROM deliveries d JOIN events e ON e.id = d.event_pk"))
    assert statuses == {"good": "sent", "bad": "failed"}
    assert live.store.scalar("SELECT reason FROM deliveries WHERE status = 'failed'") == "meta 400: bad value"


def test_events_older_than_seven_days_fail_without_sinking_the_request(live, http, clock):
    live.destinations.pop("google")
    http.add(responses.POST, META_EVENTS, json={"events_received": 1})
    live.ingest(event(event_id="old", event_time=NOW - 6 * 86400))
    live.ingest(event(event_id="new"))
    clock.sleep(2 * 86400)
    live.dispatcher.run_due()
    assert [e["event_id"] for e in body(http.calls[0])["data"]] == ["new"]
    assert live.store.scalar("SELECT reason FROM deliveries WHERE status = 'failed'") == "event_time older than 7 days"


def test_a_bad_token_keeps_events_queued_instead_of_dropping_them(live, http):
    live.destinations.pop("google")
    http.add(responses.POST, META_EVENTS, status=400, json={"error": {"message": "Invalid OAuth access token", "code": 190}})
    live.ingest(event())
    live.dispatcher.run_due()
    row = deliveries(live)["meta"]
    assert row["status"] == "pending" and "auth" in row["reason"]


def test_retries_stop_after_the_last_attempt(live, http, clock):
    live.destinations.pop("google")
    http.add(responses.POST, META_EVENTS, status=503, json={"error": {"message": "down"}})
    live.ingest(event())
    for _ in range(MAX_ATTEMPTS):
        live.dispatcher.run_due()
        clock.sleep(3600)
    row = deliveries(live)["meta"]
    assert (row["status"], row["attempts"]) == ("failed", MAX_ATTEMPTS)


def test_an_expired_google_token_is_refreshed_and_the_event_retried(live, http, clock):
    live.destinations.pop("meta")
    token(http, "old")
    http.add(responses.POST, GOOGLE_INGEST, status=401, json={"error": {"message": "expired"}})
    live.ingest(event())
    live.dispatcher.run_due()
    assert deliveries(live)["google"]["status"] == "pending"

    http.replace(responses.POST, "https://oauth2.googleapis.com/token", json={"access_token": "new", "expires_in": 3600})
    http.replace(responses.POST, GOOGLE_INGEST, json={"requestId": "r-2"})
    clock.sleep(60)
    live.dispatcher.run_due()
    assert http.calls[-1].request.headers["Authorization"] == "Bearer new"
    assert deliveries(live)["google"]["status"] == "sent"


def test_validate_only_marks_events_validated_not_sent(tmp_path, clock, http):
    settings = config.Settings(db_path=str(tmp_path / "v.db"), google=replace(GOOGLE, validate_only=True))
    relay = runtime.build(settings, clock=clock)
    token(http)
    http.add(responses.POST, GOOGLE_INGEST, json={})
    relay.ingest(event())
    relay.dispatcher.run_due()
    assert body(http.calls[-1])["validateOnly"] is True
    assert deliveries(relay)["google"]["status"] == "validated"
    relay.store.close()


def test_google_processing_errors_reject_the_matching_records(live, http, clock):
    live.destinations.pop("meta")
    token(http)
    http.add(responses.POST, GOOGLE_INGEST, json={"requestId": "r-9"})
    live.ingest(event(event_id="a", order_id="a"))
    live.ingest(event(event_id="b", order_id="b"))
    live.ingest(event(event_id="c", order_id="c"))
    live.dispatcher.run_due()
    http.add(
        responses.GET,
        GOOGLE_STATUS,
        json={
            "requestStatusPerDestination": [
                {
                    "requestStatus": "PARTIAL_SUCCESS",
                    "eventsIngestionStatus": {"recordCount": "3"},
                    "errorInfo": {"errorCounts": [{"recordCount": "1", "reason": "PROCESSING_ERROR_REASON_INVALID_GCLID",
                                                   "recordIndices": [1]}]},
                }
            ]
        },
    )
    live.monitor.refresh()
    assert live.store.scalar("SELECT COUNT(*) FROM requests WHERE processing_status = 'PROCESSING'") == 1, (
        "Google asks for 30 minutes before the first status check"
    )
    clock.sleep(1800)
    live.monitor.refresh()
    statuses = dict(live.store.all("SELECT e.event_id, d.status FROM deliveries d JOIN events e ON e.id = d.event_pk"))
    assert statuses == {"a": "sent", "b": "rejected", "c": "sent"}
    assert live.store.scalar("SELECT reason FROM deliveries WHERE status = 'rejected'") == (
        "PROCESSING_ERROR_REASON_INVALID_GCLID"
    )
    assert any(c.request.url == f"{GOOGLE_STATUS}?requestId=r-9" for c in http.calls)
