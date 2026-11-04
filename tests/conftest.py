"""Shared fixtures: demo settings (no real credential anywhere), a fake IG, and the services on top of it."""

import pytest

from delta_hedging.config import Settings
from delta_hedging.ig import IGClient

from .fake_ig import API_KEY, CFD_ACCOUNT, OPTIONS_ACCOUNT, PASSWORD, UNDERLYING, USERNAME, FakeIG

TOKEN = "a-demo-api-token-of-24-or-more-characters"
ENV = {
    "IG_API_KEY": API_KEY,
    "IG_USERNAME": USERNAME,
    "IG_PASSWORD": PASSWORD,
    "IG_OPTIONS_ACCOUNT": OPTIONS_ACCOUNT,
    "IG_CFD_ACCOUNT": CFD_ACCOUNT,
    "IG_UNDERLYING_EPIC": UNDERLYING,
    "API_TOKEN": TOKEN,
    "COOLDOWN_SECONDS": "0",
}
OPTIONAL = [
    "IG_ENVIRONMENT",
    "HEDGING_ENABLED",
    "ORDER_CURRENCY",
    "RISK_FREE_RATE",
    "DEFAULT_VOLATILITY",
    "MIN_VOLATILITY",
    "MAX_VOLATILITY",
    "DELTA_THRESHOLD",
    "MIN_HEDGE_SIZE",
    "MAX_HEDGE_SIZE",
    "PLAN_TOLERANCE",
    "MARKET_DATA_TTL",
    "REQUEST_TIMEOUT",
    "IG_BASE_URL",
    "TRUSTED_PROXY_HOPS",
]


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    for name in [*ENV, *OPTIONAL]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def env():
    return dict(ENV)


@pytest.fixture
def settings(env):
    return Settings.from_env(env)


@pytest.fixture
def ig():
    return FakeIG()


@pytest.fixture
def options_client(settings, ig, clock):
    return IGClient(settings, OPTIONS_ACCOUNT, ig.client(), clock)


@pytest.fixture
def cfd_client(settings, ig, clock):
    return IGClient(settings, CFD_ACCOUNT, ig.client(), clock)


@pytest.fixture
def service_for(ig, clock, options_client, cfd_client):
    """A service on the fake IG for the given settings (hedging switched on or off by the settings)."""
    from delta_hedging.service import HedgingService

    def make(settings):
        return HedgingService(settings, options_client, cfd_client, monotonic=clock, sleep=lambda seconds: None)

    return make


@pytest.fixture(autouse=True)
def _reset_throttle():
    from delta_hedging import api

    api._failures.clear()
    yield
    api._failures.clear()


def make_app(settings, service):
    from delta_hedging.app import create_app

    return create_app(settings, service=service)


@pytest.fixture
def app(settings, service_for):
    return make_app(settings, service_for(settings))


@pytest.fixture
def live_app(env, service_for):
    live_settings = Settings.from_env({**env, "HEDGING_ENABLED": "true"})
    return make_app(live_settings, service_for(live_settings))


AUTH = {"Authorization": f"Bearer {TOKEN}"}
