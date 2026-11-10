from __future__ import annotations

import pytest
import responses
from support import GOOGLE, META, NOW

from relay import config, runtime
from relay.clock import ManualClock


@pytest.fixture(autouse=True)
def http():
    """Every test runs inside `responses`: an HTTP call nobody mocked fails instead of going out."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        yield mock


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(NOW)


@pytest.fixture
def live(tmp_path, clock):
    """A live-mode relay with both destinations, over the mocked HTTP layer."""
    settings = config.Settings(db_path=str(tmp_path / "relay.db"), meta=META, google=GOOGLE)
    relay = runtime.build(settings, clock=clock)
    yield relay
    relay.store.close()


@pytest.fixture
def sandbox(tmp_path, clock):
    """A sandbox relay: real relay code, simulated platforms, no HTTP at all."""
    settings = config.as_sandbox(config.Settings(db_path=str(tmp_path / "sandbox.db")))
    relay = runtime.build(settings, clock=clock)
    relay.destinations["meta"].transport.failure_rate = 0  # deterministic: no injected outages
    yield relay
    relay.store.close()
