"""Settings. The only module that reads the environment; everything else gets a `Settings`."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MODES = ("live", "sandbox")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class MetaSettings:
    pixel_id: str
    access_token: str
    api_version: str = "v27.0"
    test_event_code: str | None = None
    # Insights (platform-reported attribution) needs the ad account; delivery does not.
    ad_account_id: str | None = None
    action_type: str = "offsite_conversion.fb_pixel_purchase"
    graph_url: str = "https://graph.facebook.com"


@dataclass(frozen=True)
class GoogleSettings:
    customer_id: str
    conversion_action_id: str
    client_id: str
    client_secret: str
    refresh_token: str
    login_customer_id: str | None = None
    # The Data Manager API needs no developer token; Google Ads API reporting does.
    developer_token: str | None = None
    ads_api_version: str = "v26"
    validate_only: bool = False
    events: tuple[str, ...] = ("Purchase",)
    datamanager_url: str = "https://datamanager.googleapis.com"
    ads_url: str = "https://googleads.googleapis.com"
    token_url: str = "https://oauth2.googleapis.com/token"


@dataclass(frozen=True)
class Settings:
    mode: str = "live"
    db_path: str = "relay.db"
    api_key: str | None = None
    dashboard_password: str | None = None
    allowed_origins: tuple[str, ...] = ()
    meta: MetaSettings | None = None
    google: GoogleSettings | None = None
    monitor_interval: int = 900
    click_lookback_days: int = 7
    report_days: int = 30
    # The event the attribution comparison counts (internal orders against platform conversions).
    conversion_event: str = "Purchase"
    # The ad accounts' time zone: Meta and Google date their reports in it, so the relay's days follow it.
    report_timezone: str = "UTC"

    @property
    def sandbox(self) -> bool:
        return self.mode == "sandbox"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.report_timezone)


# Placeholder accounts for sandbox mode. Requests to them never leave the process: sandbox mode
# swaps the HTTP transport for the simulator (see relay/simulator.py).
SANDBOX_META = MetaSettings(
    pixel_id="100000000000001",
    access_token="sandbox-token",
    ad_account_id="200000000000002",
)
SANDBOX_GOOGLE = GoogleSettings(
    customer_id="1234567890",
    conversion_action_id="987654321",
    client_id="sandbox-client",
    client_secret="sandbox-secret",
    refresh_token="sandbox-refresh",
    developer_token="sandbox-developer-token",
)


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None
    if value <= 0:
        raise ConfigError(f"{name} must be positive, got {value}")
    return value


def _get(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name, "").strip()
    return value or None


def _group(env: Mapping[str, str], required: tuple[str, ...], label: str) -> bool:
    """True when every variable of a group is set, False when none is; a partial group is an error."""
    present = [name for name in required if _get(env, name)]
    if not present:
        return False
    missing = [name for name in required if not _get(env, name)]
    if missing:
        raise ConfigError(f"{label} is partly configured; missing: {', '.join(missing)}")
    return True


def _meta(env: Mapping[str, str]) -> MetaSettings | None:
    if not _group(env, ("META_PIXEL_ID", "META_ACCESS_TOKEN"), "Meta"):
        return None
    defaults = MetaSettings(pixel_id="", access_token="")
    return MetaSettings(
        pixel_id=env["META_PIXEL_ID"].strip(),
        access_token=env["META_ACCESS_TOKEN"].strip(),
        api_version=_get(env, "META_API_VERSION") or defaults.api_version,
        test_event_code=_get(env, "META_TEST_EVENT_CODE"),
        ad_account_id=(_get(env, "META_AD_ACCOUNT_ID") or "").removeprefix("act_") or None,
        action_type=_get(env, "META_ACTION_TYPE") or defaults.action_type,
    )


def _customer_id(env: Mapping[str, str], name: str) -> str | None:
    value = _get(env, name)
    return value.replace("-", "") if value else None


def _google(env: Mapping[str, str]) -> GoogleSettings | None:
    required = (
        "GOOGLE_CUSTOMER_ID",
        "GOOGLE_CONVERSION_ACTION_ID",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "GOOGLE_REFRESH_TOKEN",
    )
    if not _group(env, required, "Google"):
        return None
    defaults = SANDBOX_GOOGLE
    events = tuple(e.strip() for e in (_get(env, "GOOGLE_EVENTS") or "Purchase").split(",") if e.strip())
    return GoogleSettings(
        customer_id=_customer_id(env, "GOOGLE_CUSTOMER_ID") or "",
        conversion_action_id=env["GOOGLE_CONVERSION_ACTION_ID"].strip(),
        client_id=env["GOOGLE_CLIENT_ID"].strip(),
        client_secret=env["GOOGLE_CLIENT_SECRET"].strip(),
        refresh_token=env["GOOGLE_REFRESH_TOKEN"].strip(),
        login_customer_id=_customer_id(env, "GOOGLE_LOGIN_CUSTOMER_ID"),
        developer_token=_get(env, "GOOGLE_DEVELOPER_TOKEN"),
        ads_api_version=_get(env, "GOOGLE_ADS_API_VERSION") or defaults.ads_api_version,
        validate_only=_flag(env.get("GOOGLE_VALIDATE_ONLY")),
        events=events,
    )


def from_env(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    mode = (_get(env, "RELAY_MODE") or "live").lower()
    if mode not in MODES:
        raise ConfigError(f"RELAY_MODE must be one of {', '.join(MODES)}, got {mode!r}")
    origins = tuple(o.strip().rstrip("/") for o in (env.get("ALLOWED_ORIGINS") or "").split(",") if o.strip())
    settings = Settings(
        mode=mode,
        db_path=_get(env, "RELAY_DB") or ("sandbox.db" if mode == "sandbox" else "relay.db"),
        api_key=_get(env, "RELAY_API_KEY"),
        dashboard_password=_get(env, "DASHBOARD_PASSWORD"),
        allowed_origins=origins,
        # The sandbox's platforms answer at once, so the demo refreshes platform data every 30 s.
        monitor_interval=_int(env, "MONITOR_INTERVAL_SECONDS", 30 if mode == "sandbox" else 900),
        click_lookback_days=_int(env, "CLICK_LOOKBACK_DAYS", 7),
        report_days=_int(env, "REPORT_DAYS", 30),
        conversion_event=_get(env, "CONVERSION_EVENT") or "Purchase",
        report_timezone=_timezone(_get(env, "REPORT_TIMEZONE") or "UTC"),
    )
    if mode == "sandbox":
        return as_sandbox(settings)
    return replace(settings, meta=_meta(env), google=_google(env))


def _timezone(name: str) -> str:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"REPORT_TIMEZONE must be an IANA time zone such as Europe/Belgrade, got {name!r}") from None
    return name


def as_sandbox(settings: Settings) -> Settings:
    """Sandbox mode: both destinations on, pointed at placeholder accounts the simulator answers for."""
    db_path = settings.db_path if settings.db_path != "relay.db" else "sandbox.db"
    return replace(settings, mode="sandbox", db_path=db_path, meta=SANDBOX_META, google=SANDBOX_GOOGLE)
