import pytest

from delta_hedging.config import DEMO_BASE_URL, ConfigError, Settings

MINIMAL = {
    "IG_API_KEY": "demo-api-key-value",
    "IG_USERNAME": "demo-user",
    "IG_PASSWORD": "demo-password-value",
    "IG_OPTIONS_ACCOUNT": "ABC123",
    "IG_CFD_ACCOUNT": "XYZ789",
    "IG_UNDERLYING_EPIC": "IX.D.SPTRD.IFS.IP",
    "API_TOKEN": "a-demo-token-of-at-least-24-characters",
}


def test_defaults():
    s = Settings.from_env(MINIMAL)
    assert s.base_url == DEMO_BASE_URL and s.hedging_enabled is False  # dry run unless switched on
    assert (s.rate, s.default_volatility, s.delta_threshold) == (0.05, 0.2, 0.05)
    assert (s.min_hedge_size, s.max_hedge_size, s.plan_tolerance, s.cooldown_seconds) == (
        0.01,
        100.0,
        0.05,
        60.0,
    )
    assert (s.order_currency, s.market_data_ttl, s.request_timeout) == ("GBP", 5.0, 30.0)


def test_secrets_are_not_in_the_repr():
    text = repr(Settings.from_env(MINIMAL))
    for secret in (
        "demo-api-key-value",
        "demo-user",
        "demo-password-value",
        "a-demo-token-of-at-least-24-characters",
    ):
        assert secret not in text
    assert "ABC123" in text  # an account id is shown (it is not a secret)


@pytest.mark.parametrize("missing", [k for k in MINIMAL if k != "API_TOKEN"])
def test_a_missing_variable_is_named(missing):
    with pytest.raises(ConfigError, match=missing):
        Settings.from_env({k: v for k, v in MINIMAL.items() if k != missing})


def test_the_api_token_is_needed_by_the_web_app_only():
    env = {k: v for k, v in MINIMAL.items() if k != "API_TOKEN"}
    with pytest.raises(ConfigError, match="API_TOKEN"):
        Settings.from_env(env)
    assert Settings.from_env(env, web=False).api_token == ""


def test_a_short_api_token_is_refused():
    with pytest.raises(ConfigError, match="at least 24"):
        Settings.from_env({**MINIMAL, "API_TOKEN": "short"})


@pytest.mark.parametrize("environment", ["live", "LIVE", "prod", "production"])
def test_only_the_demo_environment_is_supported(environment):
    """The first version read IG_ACC_TYPE and ignored it."""
    with pytest.raises(ConfigError, match="only IG's demo environment"):
        Settings.from_env({**MINIMAL, "IG_ENVIRONMENT": environment})


@pytest.mark.parametrize("environment", ["", "demo", "DEMO"])
def test_the_demo_environment_is_accepted(environment):
    assert Settings.from_env({**MINIMAL, "IG_ENVIRONMENT": environment}).base_url == DEMO_BASE_URL


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"IG_OPTIONS_ACCOUNT": "bad account!"}, "IG_OPTIONS_ACCOUNT"),
        ({"IG_CFD_ACCOUNT": "x" * 31}, "IG_CFD_ACCOUNT"),
        ({"IG_UNDERLYING_EPIC": "short"}, "IG_UNDERLYING_EPIC"),
        ({"IG_UNDERLYING_EPIC": "IX D SPTRD"}, "IG_UNDERLYING_EPIC"),
        ({"HEDGING_ENABLED": "maybe"}, "HEDGING_ENABLED"),
        ({"ORDER_CURRENCY": "POUNDS"}, "ORDER_CURRENCY"),
        ({"MIN_VOLATILITY": "0"}, "MIN_VOLATILITY"),
        ({"MIN_VOLATILITY": "3", "MAX_VOLATILITY": "2"}, "below MAX_VOLATILITY"),
        ({"DEFAULT_VOLATILITY": "5"}, "DEFAULT_VOLATILITY"),
        ({"DELTA_THRESHOLD": "0"}, "DELTA_THRESHOLD"),
        ({"DELTA_THRESHOLD": "soon"}, "must be a number"),
        ({"DELTA_THRESHOLD": "nan"}, "finite"),
        ({"MIN_HEDGE_SIZE": "5", "MAX_HEDGE_SIZE": "1"}, "below MAX_HEDGE_SIZE"),
        ({"PLAN_TOLERANCE": "2"}, "PLAN_TOLERANCE"),
        ({"COOLDOWN_SECONDS": "-1"}, "COOLDOWN_SECONDS"),
        ({"REQUEST_TIMEOUT": "0"}, "REQUEST_TIMEOUT"),
    ],
)
def test_invalid_values_are_rejected(change, message):
    with pytest.raises(ConfigError, match=message):
        Settings.from_env({**MINIMAL, **change})


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_hedging_can_be_switched_on_explicitly(value):
    assert Settings.from_env({**MINIMAL, "HEDGING_ENABLED": value}).hedging_enabled is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
def test_anything_else_that_means_no_keeps_it_off(value):
    assert Settings.from_env({**MINIMAL, "HEDGING_ENABLED": value}).hedging_enabled is False


def test_errors_never_contain_the_value():
    with pytest.raises(ConfigError) as caught:
        Settings.from_env({**MINIMAL, "IG_OPTIONS_ACCOUNT": "secret-looking value!"})
    assert "secret-looking" not in str(caught.value)


def test_overrides_are_read():
    s = Settings.from_env(
        {
            **MINIMAL,
            "RISK_FREE_RATE": "0.03",
            "DEFAULT_VOLATILITY": "0.25",
            "MAX_HEDGE_SIZE": "20",
            "ORDER_CURRENCY": "usd",
            "HEDGING_ENABLED": "true",
            "COOLDOWN_SECONDS": "0",
            "IG_BASE_URL": "http://127.0.0.1:9/gateway/deal",
        }
    )
    assert (s.rate, s.default_volatility, s.max_hedge_size, s.order_currency) == (0.03, 0.25, 20.0, "USD")
    assert s.hedging_enabled and s.cooldown_seconds == 0.0 and s.base_url == "http://127.0.0.1:9/gateway/deal"


@pytest.mark.parametrize(
    "url",
    [
        "https://demo-api.ig.com/gateway/deal",
        "https://demo-api.ig.com:443/gateway/deal/",
        "http://127.0.0.1:9/gateway/deal",
        "http://localhost:8080/x",
        "https://127.0.0.1/x",
        "http://[::1]:5000/x",
    ],
)
def test_the_demo_gateway_and_loopback_addresses_are_accepted(url):
    assert Settings.from_env({**MINIMAL, "IG_BASE_URL": url}).base_url == url.rstrip("/")


@pytest.mark.parametrize(
    "url",
    [
        "https://api.ig.com/gateway/deal",  # IG's live gateway
        "https://evil.example.test/gateway/deal",
        "http://demo-api.ig.com/gateway/deal",  # not over TLS
        "https://demo-api.ig.com.evil.example.test/x",
        "https://evil.example.test/@demo-api.ig.com/",
        "https://user:secret@demo-api.ig.com/x",
        "https://demo-api.ig.com:8443/x",
        "https://demo-api.ig.com/x?next=1",
        "https://demo-api.ig.com/x#frag",
        "http://127.0.0.1.evil.example.test/x",
        "http://192.168.0.10/x",
        "http://127.0.0.1:notaport/x",
        "ftp://127.0.0.1/x",
        "demo-api.ig.com/gateway/deal",
        "//demo-api.ig.com/x",
    ],
)
def test_any_other_address_for_ig_is_refused_because_the_password_is_sent_to_it(url):
    """The login sends the account's password to this address."""
    with pytest.raises(ConfigError, match="IG_BASE_URL must be IG's demo gateway"):
        Settings.from_env({**MINIMAL, "IG_BASE_URL": url})


def test_the_refusal_does_not_repeat_the_value():
    with pytest.raises(ConfigError) as caught:
        Settings.from_env({**MINIMAL, "IG_BASE_URL": "https://user:hunter2-secret@evil.example.test/x"})
    assert "hunter2" not in str(caught.value) and "evil.example" not in str(caught.value)


@pytest.mark.parametrize("hops", ["0", "1", "5", " 2 "])
def test_trusted_proxy_hops_accepts_zero_to_five(hops):
    assert Settings.from_env({**MINIMAL, "TRUSTED_PROXY_HOPS": hops}).trusted_proxy_hops == int(hops)


@pytest.mark.parametrize("hops", ["6", "-1", "1.5", "two", "\u0663"])
def test_trusted_proxy_hops_rejects_anything_else(hops):
    with pytest.raises(ConfigError, match="TRUSTED_PROXY_HOPS"):
        Settings.from_env({**MINIMAL, "TRUSTED_PROXY_HOPS": hops})


def test_no_proxy_is_trusted_by_default():
    assert Settings.from_env(MINIMAL).trusted_proxy_hops == 0
