"""What to trade to make a portfolio of options delta neutral. Pure functions: no network, no state.

The first version's ``hedge_position`` computed ``abs(delta) * size * contract_size`` and then chose the direction with
``BUY if hedge_size > 0 else SELL``. The size is never negative, so **every hedge was a BUY**, whether the option
position was long or short, a call or a put: for half the positions that doubled the exposure instead of removing it.

Sign convention, in units of the underlying: a long option (bought) has the option's delta; a short option (sold) has
the opposite. A held position in the underlying counts +1 per unit bought and -1 per unit sold. The hedge that makes
the portfolio neutral is the opposite of the net delta: net delta positive means SELL, negative means BUY.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum

from .pricing import OptionType


class Direction(Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self is Direction.BUY else -1


@dataclass(frozen=True, slots=True)
class OptionPosition:
    """An option position, as far as the hedge needs it. `direction` BUY means the option was bought (long)."""

    deal_id: str
    name: str
    epic: str
    direction: Direction
    option_type: OptionType
    strike: float
    size: float
    contract_size: float
    expiry: date
    market_price: float | None  # the option's own mid price, if it has one


def position_delta(direction: Direction, option_delta: float, size: float, contract_size: float) -> float:
    """The position's delta in units of the underlying: negative for a short position in a call, and so on."""
    return direction.sign * option_delta * size * contract_size


@dataclass(frozen=True, slots=True)
class HedgeRules:
    delta_threshold: float  # a net delta this small (in units of the underlying) is left alone
    min_size: float  # the smallest order
    max_size: float  # the largest order; a bigger need is hedged in steps


@dataclass(frozen=True, slots=True)
class HedgePlan:
    option_delta: float  # the delta of all the option positions
    held_units: float  # what is already held in the underlying: bought units minus sold units
    net_delta: float
    action: str  # "hedge", "none" (nothing to do) or "blocked" (it cannot be worked out safely)
    direction: Direction | None
    size: float  # the order size, 0 when there is no order
    capped: bool  # the need was larger than max_size
    remaining_net_delta: float  # what is left after the order
    reason: str


def plan_hedge(option_delta: float, held_units: float, rules: HedgeRules) -> HedgePlan:
    net = option_delta + held_units
    if abs(net) <= rules.delta_threshold:
        return HedgePlan(
            option_delta,
            held_units,
            net,
            "none",
            None,
            0.0,
            False,
            net,
            f"the net delta {net:+.4f} is within the threshold {rules.delta_threshold}",
        )
    needed = abs(net)
    size = round(min(needed, rules.max_size), 2)
    if size < rules.min_size:
        return HedgePlan(
            option_delta,
            held_units,
            net,
            "none",
            None,
            0.0,
            False,
            net,
            f"the order would be {size}, below the minimum {rules.min_size}",
        )
    direction = Direction.SELL if net > 0 else Direction.BUY
    remaining = net + direction.sign * size
    capped = needed > rules.max_size
    reason = f"net delta {net:+.4f}: {direction.value} {size}" + (f" (capped at {rules.max_size})" if capped else "")
    return HedgePlan(option_delta, held_units, net, "hedge", direction, size, capped, remaining, reason)


def blocked_plan(reason: str, option_delta: float = 0.0, held_units: float = 0.0) -> HedgePlan:
    """No order can be recommended: some input is missing or cannot be trusted."""
    return HedgePlan(
        option_delta,
        held_units,
        option_delta + held_units,
        "blocked",
        None,
        0.0,
        False,
        option_delta + held_units,
        reason,
    )
