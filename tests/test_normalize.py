from support import NOW, event

from relay import normalize
from relay.normalize import sha256_hex


def test_google_hashes_match_the_data_manager_examples():
    # https://developers.google.com/data-manager/api/devguides/events/google-ads/online/send-events
    prepared = normalize.prepare(event())
    identifiers = {next(iter(i)): next(iter(i.values())) for i in prepared.google_user_identifiers}
    assert identifiers["emailAddress"].upper() == "8E621E3D0368631D263D07A351FA8D34FBA0D17C15FBCDEC11A5F58008D022A0"
    assert identifiers["phoneNumber"].upper() == "78724165620B8A76386CAB242569EBC85DD2346F3F5AE88DC13BB57E31020B90"
    assert identifiers["address"] == {
        "givenName": "96d9632f363564cc3032521409cf22a852f2032eec099ed5967c0d000cec607a",
        "familyName": "db98d2607efffa28aff66975868bf54c075eca7157e35064dce08e20b85b1081",
        "regionCode": "US",
        "postalCode": "94045",
    }


def test_gmail_dots_and_tags_are_dropped_for_google_only():
    prepared = normalize.prepare(event(user={"email": "Cloudy.SanFrancisco+shop@gmail.com"}))
    google = next(i["emailAddress"] for i in prepared.google_user_identifiers if "emailAddress" in i)
    assert google == sha256_hex("cloudysanfrancisco@gmail.com")
    assert prepared.meta_user_data["em"] == [sha256_hex("cloudy.sanfrancisco+shop@gmail.com")]


def test_phone_is_e164_for_google_and_bare_digits_for_meta():
    prepared = normalize.prepare(event(user={"phone": "(512) 555-0147", "country": "us"}))
    assert prepared.meta_user_data["ph"] == [sha256_hex("15125550147")]
    assert {"phoneNumber": sha256_hex("+15125550147")} in prepared.google_user_identifiers


def test_invalid_phone_and_email_are_not_sent():
    prepared = normalize.prepare(event(user={"phone": "12", "email": "not-an-email"}))
    assert "ph" not in prepared.meta_user_data and "em" not in prepared.meta_user_data
    assert prepared.google_keys == ["address", "gclid", "ip"]


def test_meta_names_and_us_zip_are_normalised_before_hashing():
    prepared = normalize.prepare(
        event(user={"first_name": "Mary-Ann", "city": "San Francisco", "zip": "94045-1234", "state": "CA"})
    )
    assert prepared.meta_user_data["fn"] == [sha256_hex("maryann")]
    assert prepared.meta_user_data["ct"] == [sha256_hex("sanfrancisco")]
    assert prepared.meta_user_data["zp"] == [sha256_hex("94045")]
    assert prepared.meta_user_data["st"] == [sha256_hex("ca")]
    assert prepared.meta_user_data["country"] == [sha256_hex("us")]


def test_google_address_needs_all_four_parts():
    prepared = normalize.prepare(event(user={"zip": None}))
    assert not any("address" in i for i in prepared.google_user_identifiers)


def test_fbc_is_built_from_fbclid_when_the_cookie_is_missing():
    clicked = NOW - 3600
    prepared = normalize.prepare(event(click={"fbclid": "IwAR123", "clicked_at": clicked}))
    assert prepared.meta_user_data["fbc"] == f"fb.1.{int(clicked * 1000)}.IwAR123"
    cookie = normalize.prepare(event(user={"fbc": "fb.1.1.cookie"}, click={"fbclid": "IwAR123"}))
    assert cookie.meta_user_data["fbc"] == "fb.1.1.cookie"


def test_ip_user_agent_and_browser_ids_are_sent_unhashed():
    data = normalize.prepare(event()).meta_user_data
    assert data["client_ip_address"] == "203.0.113.7"
    assert data["client_user_agent"] == "Mozilla/5.0"
    assert data["fbp"] == "fb.1.1700000000000.123"
