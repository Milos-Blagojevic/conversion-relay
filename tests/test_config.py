import pytest

from relay import config
from relay.store import ModeMismatch, Store


def test_a_partly_configured_platform_is_an_error():
    with pytest.raises(config.ConfigError, match="META_ACCESS_TOKEN"):
        config.from_env({"META_PIXEL_ID": "1"})
    with pytest.raises(config.ConfigError, match="GOOGLE_REFRESH_TOKEN"):
        config.from_env({"GOOGLE_CUSTOMER_ID": "1", "GOOGLE_CONVERSION_ACTION_ID": "2", "GOOGLE_CLIENT_ID": "a",
                         "GOOGLE_CLIENT_SECRET": "b"})


def test_ids_are_accepted_in_the_forms_the_platforms_show_them():
    settings = config.from_env({
        "META_PIXEL_ID": "1", "META_ACCESS_TOKEN": "t", "META_AD_ACCOUNT_ID": "act_42",
        "GOOGLE_CUSTOMER_ID": "123-456-7890", "GOOGLE_LOGIN_CUSTOMER_ID": "999-888-7777",
        "GOOGLE_CONVERSION_ACTION_ID": "2", "GOOGLE_CLIENT_ID": "a", "GOOGLE_CLIENT_SECRET": "b",
        "GOOGLE_REFRESH_TOKEN": "r", "GOOGLE_EVENTS": "Purchase, Lead",
    })
    assert settings.meta.ad_account_id == "42"
    assert (settings.google.customer_id, settings.google.login_customer_id) == ("1234567890", "9998887777")
    assert settings.google.events == ("Purchase", "Lead")


def test_sandbox_mode_uses_placeholder_accounts_and_its_own_database():
    settings = config.from_env({"RELAY_MODE": "sandbox", "META_PIXEL_ID": "real", "META_ACCESS_TOKEN": "real"})
    assert settings.meta == config.SANDBOX_META and settings.google == config.SANDBOX_GOOGLE
    assert settings.db_path == "sandbox.db" and settings.monitor_interval == 30


def test_a_database_keeps_the_mode_it_was_created_in(tmp_path):
    path = str(tmp_path / "x.db")
    Store(path, "sandbox").close()
    with pytest.raises(ModeMismatch):
        Store(path, "live")
