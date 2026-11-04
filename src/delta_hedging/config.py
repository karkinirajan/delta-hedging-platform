"""Settings, read from the environment.

The first version wrote an IG username in ``config/settings.py``, read ``IG_ACC_TYPE`` and ignored it, and had an IP
address in its web page. Nothing is written in the code now; secrets are kept out of ``repr``.

Only IG's **demo** environment is supported. The first version's client always used the demo URL (so its README's
``IG_ACC_TYPE=DEMO/LIVE`` was never true); that is kept, and asking for another environment is an error.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

DEMO_BASE_URL = "https://demo-api.ig.com/gateway/deal"
ACCOUNT_ID = re.compile(r"[A-Za-z0-9\-]{1,30}")  # the pattern IG documents for an account id
EPIC = re.compile(r"[A-Za-z0-9._]{6,30}")  # ... and for an epic
CURRENCY = re.compile(r"[A-Z]{3}")
TRUE = {"1", "true", "yes", "on"}
FALSE = {"", "0", "false", "no", "off"}


class ConfigError(Exception):
    """A setting is missing or invalid. The message names the variable, never its value."""


def _number(
    environ: Mapping[str, str], name: str, default: float, *, minimum: float = 0.0, exclusive: bool = False
) -> float:
    raw = environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number") from None
    if value != value or value in (float("inf"), float("-inf")):
        raise ConfigError(f"{name} must be a finite number")
    if value < minimum or (exclusive and value == minimum):
        raise ConfigError(f"{name} must be {'greater than' if exclusive else 'at least'} {minimum:g}")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    # repr=False on every secret: a Settings object ends up in logs and tracebacks
    ig_api_key: str = field(repr=False)
    ig_username: str = field(repr=False)
    ig_password: str = field(repr=False)
    ig_options_account: str
    ig_cfd_account: str
    underlying_epic: str
    api_token: str = field(default="", repr=False)
    base_url: str = DEMO_BASE_URL
    hedging_enabled: bool = False  # off: the API only shows what it would do
    order_currency: str = "GBP"
    rate: float = 0.05  # the risk-free rate of the model
    default_volatility: float = 0.2  # used (and labelled as such) when no implied volatility can be found
    min_volatility: float = 0.001
    max_volatility: float = 2.0
    delta_threshold: float = 0.05  # in units of the underlying: a net delta this small is left alone
    min_hedge_size: float = 0.01
    max_hedge_size: float = 100.0
    plan_tolerance: float = 0.05  # how far the size may move between a preview and the order, as a fraction
    cooldown_seconds: float = 60.0  # at least this long between two orders
    market_data_ttl: float = 5.0
    request_timeout: float = 30.0

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, *, web: bool = True) -> Settings:
        environ = os.environ if environ is None else environ

        def get(name: str) -> str:
            return environ.get(name, "").strip()

        required = [
            "IG_API_KEY",
            "IG_USERNAME",
            "IG_PASSWORD",
            "IG_OPTIONS_ACCOUNT",
            "IG_CFD_ACCOUNT",
            "IG_UNDERLYING_EPIC",
        ]
        if web:
            required.append("API_TOKEN")
        missing = [n for n in required if not get(n)]
        if missing:
            raise ConfigError("Missing environment variables: " + ", ".join(missing))
        if get("IG_ENVIRONMENT").lower() not in ("", "demo"):
            raise ConfigError("IG_ENVIRONMENT must be demo: only IG's demo environment is supported")
        if web and len(get("API_TOKEN")) < 24:
            raise ConfigError("API_TOKEN must be at least 24 characters")
        for name in ("IG_OPTIONS_ACCOUNT", "IG_CFD_ACCOUNT"):
            if not ACCOUNT_ID.fullmatch(get(name)):
                raise ConfigError(f"{name} must be an IG account id (letters, digits and dashes, up to 30)")
        if not EPIC.fullmatch(get("IG_UNDERLYING_EPIC")):
            raise ConfigError("IG_UNDERLYING_EPIC must be an IG epic (letters, digits, dots and underscores, 6 to 30)")
        enabled = get("HEDGING_ENABLED").lower()
        if enabled not in TRUE | FALSE:
            raise ConfigError("HEDGING_ENABLED must be true or false")
        currency = get("ORDER_CURRENCY").upper() or "GBP"
        if not CURRENCY.fullmatch(currency):
            raise ConfigError("ORDER_CURRENCY must be a three-letter currency code")
        min_vol = _number(environ, "MIN_VOLATILITY", 0.001, exclusive=True)
        max_vol = _number(environ, "MAX_VOLATILITY", 2.0, exclusive=True)
        default_vol = _number(environ, "DEFAULT_VOLATILITY", 0.2, exclusive=True)
        if not min_vol < max_vol:
            raise ConfigError("MIN_VOLATILITY must be below MAX_VOLATILITY")
        if not min_vol <= default_vol <= max_vol:
            raise ConfigError("DEFAULT_VOLATILITY must be between MIN_VOLATILITY and MAX_VOLATILITY")
        min_size = _number(environ, "MIN_HEDGE_SIZE", 0.01, exclusive=True)
        max_size = _number(environ, "MAX_HEDGE_SIZE", 100.0, exclusive=True)
        if not min_size < max_size:
            raise ConfigError("MIN_HEDGE_SIZE must be below MAX_HEDGE_SIZE")
        tolerance = _number(environ, "PLAN_TOLERANCE", 0.05)
        if tolerance > 1:
            raise ConfigError("PLAN_TOLERANCE is a fraction and must be at most 1")
        return cls(
            ig_api_key=get("IG_API_KEY"),
            ig_username=get("IG_USERNAME"),
            ig_password=get("IG_PASSWORD"),
            ig_options_account=get("IG_OPTIONS_ACCOUNT"),
            ig_cfd_account=get("IG_CFD_ACCOUNT"),
            underlying_epic=get("IG_UNDERLYING_EPIC"),
            api_token=get("API_TOKEN"),
            base_url=get("IG_BASE_URL") or DEMO_BASE_URL,
            hedging_enabled=enabled in TRUE,
            order_currency=currency,
            rate=_number(environ, "RISK_FREE_RATE", 0.05, minimum=-1.0),
            default_volatility=default_vol,
            min_volatility=min_vol,
            max_volatility=max_vol,
            delta_threshold=_number(environ, "DELTA_THRESHOLD", 0.05, exclusive=True),
            min_hedge_size=min_size,
            max_hedge_size=max_size,
            plan_tolerance=tolerance,
            cooldown_seconds=_number(environ, "COOLDOWN_SECONDS", 60.0),
            market_data_ttl=_number(environ, "MARKET_DATA_TTL", 5.0),
            request_timeout=_number(environ, "REQUEST_TIMEOUT", 30.0, minimum=1.0),
        )
