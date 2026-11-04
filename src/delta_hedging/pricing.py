"""Black-Scholes pricing, greeks and implied volatility for European options on a non-dividend asset.

The standard library is enough (``math.erf`` gives the normal distribution), so there is no numpy or scipy.

The first version's calculator clamped its inputs silently (a volatility into [0.1, 2.0], a time to expiry up to
0.001 years), called the option's *price* ``time_value``, and solved implied volatility with a Newton step
divided by a vega that was already scaled to 1 % (so every step was a hundred times too big). Inputs outside the
valid range are errors here, nothing is clamped, and the implied volatility is found by bisection.

Units: ``vega`` and ``rho`` are per 1 percentage point of volatility and of the rate; ``theta`` is per calendar day.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum


class OptionType(Enum):
    CALL = "CALL"
    PUT = "PUT"


@dataclass(frozen=True, slots=True)
class Greeks:
    price: float
    delta: float
    gamma: float
    vega: float  # per 1 percentage point of volatility
    theta: float  # per calendar day
    rho: float  # per 1 percentage point of the rate


_SQRT_2PI = math.sqrt(2 * math.pi)


def _cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / _SQRT_2PI


def _check(spot: float, strike: float, years: float, volatility: float, rate: float) -> None:
    for name, value in (
        ("spot", spot),
        ("strike", strike),
        ("years", years),
        ("volatility", volatility),
        ("rate", rate),
    ):
        if not isinstance(value, int | float) or isinstance(value, bool) or not math.isfinite(value):
            raise ValueError(f"{name} must be a finite number")
    if spot <= 0:
        raise ValueError("spot must be positive")
    if strike <= 0:
        raise ValueError("strike must be positive")
    if years <= 0:
        raise ValueError("years to expiry must be positive")
    if volatility <= 0:
        raise ValueError("volatility must be positive")


def _d1_d2(spot: float, strike: float, years: float, volatility: float, rate: float) -> tuple[float, float]:
    root_t = math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + volatility * volatility / 2.0) * years) / (volatility * root_t)
    return d1, d1 - volatility * root_t


def price(
    option_type: OptionType, spot: float, strike: float, years: float, volatility: float, rate: float = 0.0
) -> float:
    _check(spot, strike, years, volatility, rate)
    d1, d2 = _d1_d2(spot, strike, years, volatility, rate)
    discount = math.exp(-rate * years)
    if option_type is OptionType.CALL:
        return spot * _cdf(d1) - strike * discount * _cdf(d2)
    return strike * discount * _cdf(-d2) - spot * _cdf(-d1)


def greeks(
    option_type: OptionType, spot: float, strike: float, years: float, volatility: float, rate: float = 0.0
) -> Greeks:
    _check(spot, strike, years, volatility, rate)
    d1, d2 = _d1_d2(spot, strike, years, volatility, rate)
    root_t = math.sqrt(years)
    discount = math.exp(-rate * years)
    density = _pdf(d1)
    gamma = density / (spot * volatility * root_t)
    vega = spot * root_t * density
    decay = -spot * volatility * density / (2.0 * root_t)
    if option_type is OptionType.CALL:
        delta = _cdf(d1)
        theta = decay - rate * strike * discount * _cdf(d2)
        rho = strike * years * discount * _cdf(d2)
    else:
        delta = _cdf(d1) - 1.0
        theta = decay + rate * strike * discount * _cdf(-d2)
        rho = -strike * years * discount * _cdf(-d2)
    return Greeks(
        price=price(option_type, spot, strike, years, volatility, rate),
        delta=delta,
        gamma=gamma,
        vega=vega / 100.0,
        theta=theta / 365.0,
        rho=rho / 100.0,
    )


def implied_volatility(
    option_type: OptionType,
    market_price: float,
    spot: float,
    strike: float,
    years: float,
    rate: float = 0.0,
    *,
    low: float = 0.001,
    high: float = 2.0,
) -> float:
    """The volatility at which the model price equals `market_price`, by bisection on [low, high].

    The model price rises with volatility, so bisection always finds the answer when there is one. A price that is
    not strictly between what the model gives for `low` and for `high` (a worthless option, a price below the
    intrinsic value or above the no-arbitrage ceiling, or one that needs a volatility outside the range) is a
    ValueError: there is no volatility to report. The search runs until the volatility is known to 1e-12, not until
    the price matches: for a deep in-the-money option the price hardly moves with the volatility.
    """
    _check(spot, strike, years, low, rate)
    if not isinstance(market_price, int | float) or isinstance(market_price, bool) or not math.isfinite(market_price):
        raise ValueError("market price must be a finite number")
    if not 0 < low < high:
        raise ValueError("the volatility range must satisfy 0 < low < high")
    floor = price(option_type, spot, strike, years, low, rate)
    ceiling = price(option_type, spot, strike, years, high, rate)
    if not floor < market_price < ceiling:
        raise ValueError(
            f"market price {market_price} is outside the model's range [{floor:.6f}, {ceiling:.6f}] "
            f"for volatilities from {low} to {high}"
        )
    lo, hi = low, high
    for _ in range(200):
        mid = (lo + hi) / 2.0
        gap = price(option_type, spot, strike, years, mid, rate) - market_price
        if gap == 0.0 or (hi - lo) < 1e-12:
            return mid
        if gap > 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2.0
