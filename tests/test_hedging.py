import itertools
from dataclasses import FrozenInstanceError
from datetime import date

import pytest

from delta_hedging.hedging import (
    Direction,
    HedgePlan,
    HedgeRules,
    OptionPosition,
    blocked_plan,
    plan_hedge,
    position_delta,
)
from delta_hedging.pricing import OptionType, greeks

RULES = HedgeRules(delta_threshold=0.05, min_size=0.01, max_size=100.0)
S, K, T, SIGMA, R = 100.0, 100.0, 0.5, 0.2, 0.05


def delta_of(direction: Direction, option_type: OptionType, size=10.0, contract_size=1.0) -> float:
    return position_delta(direction, greeks(option_type, S, K, T, SIGMA, R).delta, size, contract_size)


# -- the sign of a position's delta
@pytest.mark.parametrize(
    ("direction", "option_type", "sign"),
    [
        (Direction.BUY, OptionType.CALL, +1),  # long a call: gains when the price rises
        (Direction.SELL, OptionType.CALL, -1),  # short a call
        (Direction.BUY, OptionType.PUT, -1),  # long a put: gains when the price falls
        (Direction.SELL, OptionType.PUT, +1),  # short a put
    ],
)
def test_the_sign_of_a_positions_delta(direction, option_type, sign):
    assert delta_of(direction, option_type) * sign > 0


def test_a_positions_delta_scales_with_size_and_contract_size():
    base = delta_of(Direction.BUY, OptionType.CALL, size=1, contract_size=1)
    assert delta_of(Direction.BUY, OptionType.CALL, size=3, contract_size=2.5) == pytest.approx(base * 7.5)


# -- the direction of the hedge: the bug of the first version
@pytest.mark.parametrize(
    ("direction", "option_type", "expected"),
    [
        (Direction.BUY, OptionType.CALL, Direction.SELL),  # long call: positive delta, so sell the underlying
        (Direction.SELL, OptionType.CALL, Direction.BUY),  # short call: negative delta, so buy
        (Direction.BUY, OptionType.PUT, Direction.BUY),  # long put: negative delta, so buy
        (Direction.SELL, OptionType.PUT, Direction.SELL),  # short put: positive delta, so sell
    ],
)
def test_every_kind_of_position_gets_the_hedge_that_neutralises_it(direction, option_type, expected):
    """The first version chose BUY if hedge_size > 0, and the size was never negative: it always bought."""
    plan = plan_hedge(delta_of(direction, option_type), 0.0, RULES)
    assert plan.action == "hedge" and plan.direction is expected


@pytest.mark.parametrize(
    ("direction", "option_type", "size", "contract_size"),
    list(itertools.product(Direction, OptionType, [0.5, 10.0, 37.25], [1.0, 2.0])),
)
def test_the_recommended_order_brings_the_net_delta_to_nothing(direction, option_type, size, contract_size):
    option_delta = delta_of(direction, option_type, size, contract_size)
    plan = plan_hedge(option_delta, 0.0, RULES)
    assert plan.action == "hedge"
    assert abs(option_delta + plan.direction.sign * plan.size) <= 0.005 + 1e-9  # what rounding to 0.01 can leave
    assert plan.remaining_net_delta == pytest.approx(option_delta + plan.direction.sign * plan.size)


def test_a_wrong_direction_would_double_the_exposure_instead_of_removing_it():
    option_delta = delta_of(Direction.SELL, OptionType.CALL)  # negative: the portfolio loses when the price rises
    plan = plan_hedge(option_delta, 0.0, RULES)
    assert plan.direction is Direction.BUY
    assert abs(option_delta + plan.size) < abs(option_delta)  # buying shrinks the exposure ...
    assert abs(option_delta - plan.size) > abs(option_delta)  # ... selling, as the first version would have, doubles it


# -- what is already held counts
def test_a_hedge_already_held_is_counted_and_not_added_to():
    option_delta = delta_of(Direction.BUY, OptionType.CALL)  # about +6.9
    first = plan_hedge(option_delta, 0.0, RULES)
    held = -first.size  # the order was filled: that many units are now sold
    again = plan_hedge(option_delta, held, RULES)
    assert again.action == "none" and again.net_delta == pytest.approx(option_delta + held)
    assert "within the threshold" in again.reason


def test_a_partly_hedged_portfolio_gets_only_the_rest():
    plan = plan_hedge(8.0, -5.0, RULES)
    assert plan.direction is Direction.SELL and plan.size == 3.0


def test_an_over_hedged_portfolio_is_bought_back():
    plan = plan_hedge(8.0, -12.0, RULES)  # net -4: too much sold
    assert plan.direction is Direction.BUY and plan.size == 4.0


def test_positions_net_against_each_other():
    straddle = delta_of(Direction.BUY, OptionType.CALL) + delta_of(Direction.BUY, OptionType.PUT)
    call_alone = plan_hedge(delta_of(Direction.BUY, OptionType.CALL), 0.0, RULES)
    assert abs(straddle) < call_alone.size  # a long call and a long put at one strike largely offset
    spread = delta_of(Direction.BUY, OptionType.CALL) + delta_of(Direction.SELL, OptionType.CALL)
    assert plan_hedge(spread, 0.0, RULES).action == "none"


# -- thresholds, limits, rounding
def test_a_net_delta_inside_the_threshold_is_left_alone():
    for net in (0.0, 0.05, -0.05, 0.0499):
        plan = plan_hedge(net, 0.0, RULES)
        assert plan.action == "none" and plan.size == 0.0 and plan.direction is None


def test_just_outside_the_threshold_is_hedged():
    plan = plan_hedge(0.06, 0.0, RULES)
    assert plan.action == "hedge" and plan.size == 0.06 and plan.direction is Direction.SELL


def test_a_large_need_is_capped_and_the_rest_is_reported():
    plan = plan_hedge(250.0, 0.0, RULES)
    assert (plan.action, plan.direction, plan.size, plan.capped) == ("hedge", Direction.SELL, 100.0, True)
    assert plan.remaining_net_delta == pytest.approx(150.0) and "capped" in plan.reason
    assert plan_hedge(-250.0, 0.0, RULES).remaining_net_delta == pytest.approx(-150.0)


def test_an_order_smaller_than_the_minimum_is_not_placed():
    rules = HedgeRules(delta_threshold=0.001, min_size=0.05, max_size=100.0)
    plan = plan_hedge(0.004, 0.0, rules)
    assert plan.action == "none" and "below the minimum" in plan.reason


def test_sizes_are_rounded_to_two_decimals():
    assert plan_hedge(6.368345, 0.0, RULES).size == 6.37
    assert plan_hedge(-6.3649, 0.0, RULES).size == 6.36


def test_there_is_no_minimal_hedge_for_a_position_that_does_not_need_one():
    """The first version opened a 'minimal hedge' of max(0.01, size * 0.1) when the delta was 0 or near expiry."""
    assert plan_hedge(0.0, 0.0, RULES).action == "none"


def test_a_blocked_plan_orders_nothing():
    plan = blocked_plan("a position could not be read", option_delta=3.0, held_units=-1.0)
    assert plan.action == "blocked" and plan.size == 0.0 and plan.direction is None
    assert plan.net_delta == 2.0 and plan.reason == "a position could not be read"


def test_plans_and_positions_are_immutable():
    plan = plan_hedge(5.0, 0.0, RULES)
    assert isinstance(plan, HedgePlan)
    with pytest.raises(FrozenInstanceError):
        plan.size = 99  # type: ignore[misc]
    position = OptionPosition("D1", "n", "e", Direction.BUY, OptionType.CALL, 100.0, 1.0, 1.0, date(2030, 1, 1), None)
    with pytest.raises(FrozenInstanceError):
        position.strike = 1.0  # type: ignore[misc]


def test_directions_have_signs_and_string_values():
    assert Direction.BUY.sign == 1 and Direction.SELL.sign == -1
    assert [d.value for d in Direction] == ["BUY", "SELL"]
