import math

import pytest

from delta_hedging.pricing import Greeks, OptionType, greeks, implied_volatility, price

CALL, PUT = OptionType.CALL, OptionType.PUT
# The standard textbook case (Hull): S = K = 100, r = 5 %, sigma = 20 %, one year
S, K, T, SIGMA, R = 100.0, 100.0, 1.0, 0.2, 0.05


def test_the_textbook_call():
    g = greeks(CALL, S, K, T, SIGMA, R)
    assert g.price == pytest.approx(10.4506, abs=1e-4)
    assert g.delta == pytest.approx(0.6368, abs=1e-4)
    assert g.gamma == pytest.approx(0.018762, abs=1e-6)
    assert g.vega == pytest.approx(0.37524, abs=1e-5)  # per percentage point of volatility
    assert g.theta == pytest.approx(-6.4140 / 365, abs=1e-5)  # per calendar day
    assert g.rho == pytest.approx(0.53232, abs=1e-5)  # per percentage point of the rate


def test_the_textbook_put():
    g = greeks(PUT, S, K, T, SIGMA, R)
    assert g.price == pytest.approx(5.5735, abs=1e-4)
    assert g.delta == pytest.approx(-0.3632, abs=1e-4)
    assert g.rho < 0 and g.gamma == pytest.approx(greeks(CALL, S, K, T, SIGMA, R).gamma)


@pytest.mark.parametrize("spot", [60, 95, 100, 130])
@pytest.mark.parametrize("strike", [80, 100, 120])
@pytest.mark.parametrize("years", [0.02, 0.5, 2.0])
@pytest.mark.parametrize("rate", [0.0, 0.05, -0.01])
def test_put_call_parity_and_delta_relations(spot, strike, years, rate):
    call, put = greeks(CALL, spot, strike, years, 0.3, rate), greeks(PUT, spot, strike, years, 0.3, rate)
    assert call.price - put.price == pytest.approx(spot - strike * math.exp(-rate * years), abs=1e-9)
    assert call.delta - put.delta == pytest.approx(1.0, abs=1e-12)
    assert call.vega == pytest.approx(put.vega) and call.gamma == pytest.approx(put.gamma)


@pytest.mark.parametrize("option_type", [CALL, PUT])
def test_the_greeks_agree_with_finite_differences_of_the_price(option_type):
    h = 1e-4
    g = greeks(option_type, S, 105.0, 0.75, 0.25, R)
    up, down = (
        price(option_type, S + h, 105.0, 0.75, 0.25, R),
        price(option_type, S - h, 105.0, 0.75, 0.25, R),
    )
    assert g.delta == pytest.approx((up - down) / (2 * h), abs=1e-6)
    assert g.gamma == pytest.approx((up - 2 * g.price + down) / h**2, abs=1e-4)
    vega = (price(option_type, S, 105.0, 0.75, 0.25 + h, R) - price(option_type, S, 105.0, 0.75, 0.25 - h, R)) / (2 * h)
    assert g.vega == pytest.approx(vega / 100, abs=1e-7)
    rho = (price(option_type, S, 105.0, 0.75, 0.25, R + h) - price(option_type, S, 105.0, 0.75, 0.25, R - h)) / (2 * h)
    assert g.rho == pytest.approx(rho / 100, abs=1e-6)
    theta = -(price(option_type, S, 105.0, 0.75 + h, 0.25, R) - price(option_type, S, 105.0, 0.75 - h, 0.25, R)) / (
        2 * h
    )
    assert g.theta == pytest.approx(theta / 365, abs=1e-7)


def test_the_price_equals_the_greeks_price_and_is_a_dataclass_of_floats():
    g = greeks(CALL, S, K, T, SIGMA, R)
    assert isinstance(g, Greeks) and g.price == price(CALL, S, K, T, SIGMA, R)


def test_limits_deep_in_and_out_of_the_money():
    assert greeks(CALL, 200, 100, 0.5, 0.2, 0.0).delta == pytest.approx(1.0, abs=1e-6)
    assert greeks(CALL, 50, 100, 0.5, 0.2, 0.0).delta == pytest.approx(0.0, abs=1e-6)
    assert greeks(PUT, 200, 100, 0.5, 0.2, 0.0).delta == pytest.approx(0.0, abs=1e-6)
    assert greeks(PUT, 50, 100, 0.5, 0.2, 0.0).delta == pytest.approx(-1.0, abs=1e-6)


def test_a_very_short_expiry_is_computed_as_it_is_not_clamped():
    """The first version raised the time to expiry to 0.001 years (about 9 hours) whatever it was."""
    seconds = 3600 / (365 * 24 * 3600)  # one hour
    assert greeks(CALL, 100, 100, seconds, 0.2, 0.0).gamma > greeks(CALL, 100, 100, 0.001, 0.2, 0.0).gamma * 2


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ((0, 100, 1, 0.2, 0.0), "spot must be positive"),
        ((-5, 100, 1, 0.2, 0.0), "spot must be positive"),
        ((100, 0, 1, 0.2, 0.0), "strike must be positive"),
        ((100, 100, 0, 0.2, 0.0), "years to expiry must be positive"),
        ((100, 100, -1, 0.2, 0.0), "years to expiry must be positive"),
        ((100, 100, 1, 0, 0.0), "volatility must be positive"),
        ((100, 100, 1, float("nan"), 0.0), "volatility must be a finite number"),
        ((100, 100, float("inf"), 0.2, 0.0), "years must be a finite number"),
        ((100, 100, 1, 0.2, float("nan")), "rate must be a finite number"),
        ((True, 100, 1, 0.2, 0.0), "spot must be a finite number"),
        (("100", 100, 1, 0.2, 0.0), "spot must be a finite number"),
    ],
)
def test_invalid_inputs_are_errors_never_clamped(args, message):
    for function in (price, greeks):
        with pytest.raises(ValueError, match=message):
            function(CALL, *args)


# -- implied volatility
@pytest.mark.parametrize("option_type", [CALL, PUT])
@pytest.mark.parametrize("sigma", [0.05, 0.2, 0.6, 1.5])
@pytest.mark.parametrize("strike", [85.0, 100.0, 115.0])
def test_implied_volatility_round_trips(option_type, sigma, strike):
    target = price(option_type, S, strike, 0.5, sigma, R)
    if target < 1e-6:
        pytest.skip("the price is below what the solver can tell apart")
    assert implied_volatility(option_type, target, S, strike, 0.5, R) == pytest.approx(sigma, abs=1e-5)


def test_a_price_the_model_cannot_give_is_an_error():
    with pytest.raises(ValueError, match="outside the model's range"):
        implied_volatility(CALL, 0.0, S, 150.0, 0.25, R)  # worth nothing: below the price at the lowest volatility
    with pytest.raises(ValueError, match="outside the model's range"):
        implied_volatility(CALL, 120.0, S, K, 1.0, R)  # more than the underlying itself
    with pytest.raises(ValueError, match="outside the model's range"):
        implied_volatility(PUT, 15.0, S, 90.0, 1.0, R, high=0.3)  # needs more volatility than the allowed maximum


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "5", True])
def test_a_bad_market_price_is_an_error(bad):
    with pytest.raises(ValueError, match="finite number"):
        implied_volatility(CALL, bad, S, K, 1.0, R)


def test_the_range_must_be_ordered():
    with pytest.raises(ValueError, match="0 < low < high"):
        implied_volatility(CALL, 10.0, S, K, 1.0, R, low=0.5, high=0.3)


def test_implied_volatility_converges_in_few_steps_where_the_old_newton_step_did_not():
    """The old step divided by a vega already scaled to 1 %: a hundred times too large, so it overshot every time."""
    target = price(CALL, S, K, 1.0, 0.35, R)
    assert implied_volatility(CALL, target, S, K, 1.0, R) == pytest.approx(0.35, abs=1e-9)
