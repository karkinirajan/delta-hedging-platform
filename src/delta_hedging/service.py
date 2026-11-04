"""The hedging service: analyse the option positions, plan a hedge, and (only when switched on) place it.

What the first version did instead: it took the *option's own* price as the price of the underlying, used the opening
price as the strike, used the day's percentage change as an annual volatility, hedged each position on its own (so
two positions were never netted and a hedge already held was never counted), and executed from a web endpoint that
anyone could call. A hedge here is worked out from the whole portfolio, from the underlying's real price, and a
position that cannot be read blocks the plan instead of being guessed.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .config import Settings
from .hedging import HedgePlan, HedgeRules, OptionPosition, blocked_plan, plan_hedge, position_delta
from .ig import IGClient, IGError, new_deal_reference
from .positions import Unreadable, read_option_position, years_to_expiry
from .pricing import Greeks, greeks, implied_volatility

logger = logging.getLogger(__name__)


class HedgingError(Exception):
    """An order was refused before anything was sent. `status` is the HTTP status the API answers with."""

    status = 400

    def __init__(self, message: str, plan: HedgePlan | None = None) -> None:
        super().__init__(message)
        self.plan = plan


class HedgingDisabled(HedgingError):
    status = 403


class NotConfirmed(HedgingError):
    status = 400


class PlanChanged(HedgingError):
    status = 409


class Busy(HedgingError):
    status = 409


class TooSoon(HedgingError):
    status = 429

    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class PositionAnalysis:
    position: OptionPosition
    years: float
    spot: float
    volatility: float
    volatility_source: str  # "implied" (from the option's own price) or "default" (the configured fallback)
    greeks: Greeks
    delta_units: float  # the position's delta in units of the underlying, signed by long or short


@dataclass(frozen=True, slots=True)
class Analysis:
    generated_at: datetime
    spot: float | None
    market_status: str
    positions: tuple[PositionAnalysis, ...]
    unreadable: tuple[Unreadable, ...]
    expired: tuple[OptionPosition, ...]
    hedge_positions: tuple[dict, ...]  # the positions already held in the underlying
    option_delta: float
    held_units: float
    plan: HedgePlan
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HedgeResult:
    executed: bool
    plan: HedgePlan
    deal_reference: str = ""
    outcome: str = ""  # "accepted", "rejected" or "unconfirmed"; empty when nothing was sent
    reason: str = ""
    deal_id: str = ""


@dataclass(slots=True)
class _State:
    last_order_at: float | None = None
    history: deque = field(default_factory=lambda: deque(maxlen=100))
    analysis: tuple[float, Analysis] | None = None


class HedgingService:
    def __init__(
        self,
        settings: Settings,
        options_client: IGClient,
        cfd_client: IGClient,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._s = settings
        self._options, self._cfd = options_client, cfd_client
        self._now, self._monotonic, self._sleep = now, monotonic, sleep
        self._rules = HedgeRules(settings.delta_threshold, settings.min_hedge_size, settings.max_hedge_size)
        self._state = _State()
        self._analysis_lock = threading.Lock()
        self._order_lock = threading.Lock()

    # -- reading
    def analyse(self, *, fresh: bool = False) -> Analysis:
        with self._analysis_lock:
            cached = self._state.analysis
            if not fresh and cached and self._monotonic() < cached[0]:
                return cached[1]
            analysis = self._build()
            self._state.analysis = (self._monotonic() + self._s.market_data_ttl, analysis)
            return analysis

    def _build(self) -> Analysis:
        now = self._now()
        quote = self._options.quote(self._s.underlying_epic)
        spot = quote.mid
        positions, unreadable, expired, warnings = [], [], [], []
        for entry in self._options.positions():
            read = read_option_position(entry)
            if read is None:
                continue
            if isinstance(read, Unreadable):
                unreadable.append(read)
                continue
            years = years_to_expiry(read.expiry, now)
            if years <= 0:
                expired.append(read)
                continue
            analysed = self._analyse_position(read, spot, years)
            if analysed.volatility_source == "default":
                warnings.append(
                    f"{read.name} ({read.deal_id}): no implied volatility could be found, so the default "
                    f"{self._s.default_volatility} is used"
                )
            positions.append(analysed)
        hedge_positions, held_units, hedge_unreadable = self._read_hedge_positions()
        option_delta = sum(p.delta_units for p in positions)
        plan = self._plan(option_delta, held_units, unreadable, hedge_unreadable, quote.status)
        return Analysis(
            generated_at=now,
            spot=spot,
            market_status=quote.status,
            positions=tuple(positions),
            unreadable=tuple(unreadable),
            expired=tuple(expired),
            hedge_positions=tuple(hedge_positions),
            option_delta=option_delta,
            held_units=held_units,
            plan=plan,
            warnings=tuple(warnings),
        )

    def _analyse_position(self, position: OptionPosition, spot: float, years: float) -> PositionAnalysis:
        volatility, source = self._s.default_volatility, "default"
        if position.market_price is not None:
            try:
                volatility = implied_volatility(
                    position.option_type,
                    position.market_price,
                    spot,
                    position.strike,
                    years,
                    self._s.rate,
                    low=self._s.min_volatility,
                    high=self._s.max_volatility,
                )
                source = "implied"
            except ValueError:
                pass  # the price is not one the model can give: fall back, and say so
        g = greeks(position.option_type, spot, position.strike, years, volatility, self._s.rate)
        return PositionAnalysis(
            position=position,
            years=years,
            spot=spot,
            volatility=volatility,
            volatility_source=source,
            greeks=g,
            delta_units=position_delta(position.direction, g.delta, position.size, position.contract_size),
        )

    def _read_hedge_positions(self) -> tuple[list[dict], float, list[str]]:
        """What is held in the underlying on the CFD account: (summaries, signed units, ids it could not read)."""
        held, units, unreadable = [], 0.0, []
        for entry in self._cfd.positions():
            market, position = entry.get("market"), entry.get("position")
            if (
                not isinstance(market, dict)
                or not isinstance(position, dict)
                or market.get("epic") != self._s.underlying_epic
            ):
                continue
            direction = position.get("direction")
            try:
                size, contract = float(position["size"]), float(position.get("contractSize", 1.0))
            except (KeyError, TypeError, ValueError):
                size = contract = 0.0
            if direction not in ("BUY", "SELL") or not size > 0 or not contract > 0:
                unreadable.append(str(position.get("dealId") or "?"))
                continue
            signed = (1 if direction == "BUY" else -1) * size * contract
            units += signed
            held.append(
                {
                    "deal_id": str(position.get("dealId") or ""),
                    "direction": direction,
                    "size": size,
                    "contract_size": contract,
                    "units": signed,
                }
            )
        return held, units, unreadable

    def _plan(
        self, option_delta: float, held_units: float, unreadable: list, hedge_unreadable: list, status: str
    ) -> HedgePlan:
        if unreadable:
            return blocked_plan(
                f"{len(unreadable)} option position(s) cannot be read, so the portfolio's delta is unknown: "
                "no hedge is recommended until that is fixed",
                option_delta,
                held_units,
            )
        if hedge_unreadable:
            return blocked_plan(
                f"{len(hedge_unreadable)} position(s) in the underlying cannot be read, "
                "so what is already hedged is unknown",
                option_delta,
                held_units,
            )
        if status != "TRADEABLE":
            return blocked_plan(
                f"the underlying market is not tradeable (status {status or 'unknown'})",
                option_delta,
                held_units,
            )
        return plan_hedge(option_delta, held_units, self._rules)

    # -- trading
    def hedge(self, *, confirm: bool, expected_direction: str | None, expected_size: float | None) -> HedgeResult:
        """Place the hedge the plan recommends, if hedging is switched on and the caller confirms the plan it saw."""
        if confirm is not True:
            return HedgeResult(executed=False, plan=self.analyse().plan)  # a preview
        if not self._s.hedging_enabled:
            raise HedgingDisabled("hedging is switched off (HEDGING_ENABLED): this is a dry run, nothing is sent to IG")
        if (
            expected_direction not in ("BUY", "SELL")
            or not isinstance(expected_size, int | float)
            or isinstance(expected_size, bool)
            or not expected_size > 0
        ):
            raise NotConfirmed(
                "confirm the order you previewed: send expected_direction (BUY or SELL) and expected_size"
            )
        if not self._order_lock.acquire(blocking=False):
            raise Busy("another hedge is being placed")
        try:
            return self._place(expected_direction, float(expected_size))
        finally:
            self._order_lock.release()

    def _place(self, expected_direction: str, expected_size: float) -> HedgeResult:
        last = self._state.last_order_at
        if last is not None and (wait := self._s.cooldown_seconds - (self._monotonic() - last)) > 0:
            raise TooSoon(
                f"an order was placed less than {self._s.cooldown_seconds:g} seconds ago; wait {wait:.0f} s",
                wait,
            )
        plan = self.analyse(fresh=True).plan  # worked out again, from IG's data of this moment
        if plan.action != "hedge" or plan.direction is None:
            return HedgeResult(executed=False, plan=plan)
        tolerance = max(0.01, self._s.plan_tolerance * plan.size)
        if plan.direction.value != expected_direction or abs(plan.size - expected_size) > tolerance:
            raise PlanChanged(
                f"the hedge is now {plan.direction.value} {plan.size}, not {expected_direction} {expected_size}: "
                "look at the new plan and confirm that",
                plan,
            )
        reference = new_deal_reference()
        outcome_unknown = False
        try:
            self._cfd.open_position(
                self._s.underlying_epic, plan.direction, plan.size, self._s.order_currency, reference
            )
        except IGError as exc:
            if not exc.outcome_unknown:
                raise
            outcome_unknown = True
        confirmation = self._confirm(reference)
        if confirmation.accepted:
            outcome, reason = "accepted", ""
        elif confirmation.status == "REJECTED":
            outcome, reason = "rejected", confirmation.reason
        else:
            outcome = "unconfirmed"
            reason = (
                "IG did not answer the order and has no confirmation: it may have been placed. Check deal reference "
                f"{reference} on the IG platform before hedging again"
                if outcome_unknown
                else "IG has no confirmation for the order yet. Check the deal reference on the IG platform"
            )
        if outcome != "rejected":
            self._state.last_order_at = self._monotonic()  # a rejected order may be tried again at once
        self._state.analysis = None
        self._state.history.appendleft(
            {
                "time": self._now().isoformat(),
                "deal_reference": reference,
                "direction": plan.direction.value,
                "size": plan.size,
                "net_delta_before": plan.net_delta,
                "outcome": outcome,
                "reason": reason,
                "deal_id": confirmation.deal_id,
            }
        )
        logger.info("Hedge %s: %s %s -> %s", reference, plan.direction.value, plan.size, outcome)
        return HedgeResult(True, plan, reference, outcome, reason, confirmation.deal_id)

    def _confirm(self, reference: str):
        for attempt in range(3):
            confirmation = self._cfd.confirm(reference)
            if confirmation.status != "UNKNOWN" or attempt == 2:
                return confirmation
            self._sleep(0.5)
        raise AssertionError("unreachable")  # pragma: no cover

    def history(self) -> list[dict]:
        return list(self._state.history)
