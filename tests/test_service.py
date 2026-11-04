from datetime import UTC, datetime

import httpx
import pytest

from delta_hedging.config import Settings
from delta_hedging.hedging import Direction
from delta_hedging.ig import IGError
from delta_hedging.pricing import OptionType, greeks, price
from delta_hedging.service import Busy, HedgingDisabled, HedgingService, NotConfirmed, PlanChanged, TooSoon

from .fake_ig import CFD_ACCOUNT, OPTIONS_ACCOUNT, UNDERLYING, cfd, error, option

NOW = datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC)  # the options expire on 17 January 2027: about 0.2 years


def make_service(env, ig, clock, options_client, cfd_client, **overrides):
    settings = Settings.from_env({**env, **overrides})
    sleeps = []
    service = HedgingService(
        settings, options_client, cfd_client, now=lambda: NOW, monotonic=clock, sleep=sleeps.append
    )
    service.sleeps = sleeps
    return service


@pytest.fixture
def service(env, ig, clock, options_client, cfd_client):
    return make_service(env, ig, clock, options_client, cfd_client)


@pytest.fixture
def live(env, ig, clock, options_client, cfd_client):
    """A service that may place orders (against the fake)."""
    return make_service(env, ig, clock, options_client, cfd_client, HEDGING_ENABLED="true")


# -- analysis
def test_a_long_call_is_analysed_from_the_underlying_price_and_its_own_implied_volatility(service, ig):
    a = service.analyse()
    (p,) = a.positions
    assert a.spot == 5800.0 and p.spot == 5800.0 and p.position.strike == 5800.0
    assert p.volatility_source == "implied" and 0.01 < p.volatility < 2.0
    # the model price at the implied volatility is the option's market mid price
    assert price(OptionType.CALL, 5800.0, 5800.0, p.years, p.volatility, 0.05) == pytest.approx(172.0, abs=1e-4)
    assert 0 < p.greeks.delta < 1 and p.delta_units == pytest.approx(p.greeks.delta * 10)  # long: positive
    assert a.option_delta == pytest.approx(p.delta_units) and a.held_units == 0.0


def test_the_options_own_price_is_never_used_as_the_underlying_price(service, ig):
    """The first version asked for the market data of the option's epic and used that price as the spot."""
    service.analyse()
    epics = [r.url.path.rsplit("/", 1)[1] for r in ig.calls("market")]
    assert epics == [UNDERLYING]


def test_a_different_underlying_price_gives_a_different_delta(service, ig):
    base = service.analyse(fresh=True).positions[0].greeks.delta
    ig.quotes[UNDERLYING].update(bid=6199.0, offer=6201.0)  # 7 % higher: the call is deeper in the money
    assert service.analyse(fresh=True).positions[0].greeks.delta > base + 0.1


def test_years_to_expiry_come_from_the_clock_and_the_expiry_date(service):
    (p,) = service.analyse().positions
    assert p.years == pytest.approx(74.5 / 365, abs=0.002)


def test_a_missing_option_price_falls_back_to_the_default_volatility_and_says_so(service, ig):
    ig.positions[OPTIONS_ACCOUNT] = [option(bid=None, offer=None)]
    a = service.analyse()
    assert a.positions[0].volatility_source == "default" and a.positions[0].volatility == 0.2
    assert len(a.warnings) == 1 and "default 0.2" in a.warnings[0] and "DIOPT001" in a.warnings[0]


def test_a_price_the_model_cannot_give_also_falls_back_and_says_so(service, ig):
    ig.positions[OPTIONS_ACCOUNT] = [option(bid=0.01, offer=0.03)]  # an at-the-money option worth almost nothing
    p = service.analyse().positions[0]
    assert p.volatility_source == "default" and "default" in service.analyse().warnings[0]


def test_the_delta_is_computed_at_the_volatility_that_was_used(service):
    p = service.analyse().positions[0]
    expected = greeks(OptionType.CALL, 5800.0, 5800.0, p.years, p.volatility, 0.05)
    assert p.greeks == expected


def test_a_short_position_has_the_opposite_delta(service, ig):
    ig.positions[OPTIONS_ACCOUNT] = [option(direction="SELL")]
    a = service.analyse()
    assert a.option_delta < 0 and a.plan.direction is Direction.BUY


def test_positions_are_netted_in_one_plan(service, ig):
    ig.positions[OPTIONS_ACCOUNT] = [
        option(),
        option(deal_id="DIOPT002", direction="SELL"),
    ]  # long and short the same option
    a = service.analyse()
    assert len(a.positions) == 2 and a.option_delta == pytest.approx(0.0, abs=1e-9)
    assert a.plan.action == "none"


def test_a_long_call_and_a_long_put_at_the_money_largely_offset(service, ig):
    ig.positions[OPTIONS_ACCOUNT] = [option(), option(deal_id="DIOPT002", name="US 500 Cash (£10) 5800 PUT")]
    a = service.analyse()
    call_alone = a.positions[0].delta_units
    assert abs(a.option_delta) < call_alone


# -- what is already held
def test_a_hedge_already_held_is_counted(service, ig):
    first = service.analyse().plan
    assert first.direction is Direction.SELL
    ig.positions[CFD_ACCOUNT] = [cfd(direction="SELL", size=first.size)]
    again = service.analyse(fresh=True)
    assert again.held_units == pytest.approx(-first.size) and again.plan.action == "none"
    assert again.hedge_positions[0]["units"] == pytest.approx(-first.size)


def test_positions_in_other_instruments_do_not_count_as_a_hedge(service, ig):
    ig.positions[CFD_ACCOUNT] = [cfd(epic="IX.D.FTSE.DAILY.IP", size=50.0)]
    assert service.analyse().held_units == 0.0


def test_buying_the_underlying_adds_to_a_long_calls_delta(service, ig):
    base = service.analyse().plan.size
    ig.positions[CFD_ACCOUNT] = [cfd(direction="BUY", size=2.0)]
    assert service.analyse(fresh=True).plan.size == pytest.approx(base + 2.0, abs=0.01)


@pytest.mark.parametrize("bad", [{"size": 0}, {"direction": "LONG"}, {"contract": 0}])
def test_a_hedge_position_that_cannot_be_read_blocks_the_plan(service, ig, bad):
    ig.positions[CFD_ACCOUNT] = [cfd(**bad)]
    plan = service.analyse().plan
    assert plan.action == "blocked" and "already hedged is unknown" in plan.reason and plan.size == 0.0


# -- positions that cannot be used
def test_an_option_that_cannot_be_read_blocks_the_plan_instead_of_being_guessed(service, ig):
    ig.positions[OPTIONS_ACCOUNT] = [option(), option(deal_id="DIBAD", name="Mystery instrument")]
    a = service.analyse()
    assert [u.deal_id for u in a.unreadable] == ["DIBAD"] and len(a.positions) == 1
    assert a.plan.action == "blocked" and "1 option position(s) cannot be read" in a.plan.reason
    assert a.option_delta != 0  # the delta of the ones that could be read is still shown


def test_positions_that_are_not_options_are_ignored(service, ig):
    ig.positions[OPTIONS_ACCOUNT] = [option(), cfd(deal_id="DICFD9")]
    a = service.analyse()
    assert len(a.positions) == 1 and a.unreadable == () and a.plan.action == "hedge"


def test_an_expired_option_is_reported_and_left_out_of_the_delta(service, ig):
    ig.positions[OPTIONS_ACCOUNT] = [option(), option(deal_id="DIOLD", expiry="01-NOV-26")]
    a = service.analyse()
    assert [e.deal_id for e in a.expired] == ["DIOLD"] and len(a.positions) == 1
    assert a.plan.action == "hedge"  # an expired option does not block the plan


def test_no_positions_at_all_needs_no_hedge(service, ig):
    ig.positions[OPTIONS_ACCOUNT] = []
    a = service.analyse()
    assert a.plan.action == "none" and a.positions == () and a.option_delta == 0.0


def test_a_market_that_is_not_tradeable_blocks_the_plan_but_the_delta_is_still_shown(service, ig):
    ig.quotes[UNDERLYING]["marketStatus"] = "CLOSED"
    a = service.analyse()
    assert a.plan.action == "blocked" and "not tradeable (status CLOSED)" in a.plan.reason
    assert a.option_delta > 0 and a.market_status == "CLOSED"


# -- caching and failures
def test_the_analysis_is_kept_briefly_so_that_refreshing_does_not_use_up_igs_allowance(service, ig, clock):
    service.analyse()
    service.analyse()
    assert (
        len(ig.calls("positions")) == 2 and len(ig.calls("market")) == 1
    )  # one set: options + cfd positions, one quote
    clock.advance(4.9)
    service.analyse()
    assert len(ig.calls("market")) == 1
    clock.advance(0.2)
    service.analyse()
    assert len(ig.calls("market")) == 2
    service.analyse(fresh=True)
    assert len(ig.calls("market")) == 3


def test_an_ig_failure_is_not_hidden(service, ig):
    ig.fail["positions"] = error(403, "error.public-api.exceeded-account-allowance")
    with pytest.raises(IGError) as caught:
        service.analyse()
    assert caught.value.kind == "rate_limit"


# -- placing hedges
def plan_of(service):
    plan = service.analyse(fresh=True).plan
    assert plan.action == "hedge"
    return plan


@pytest.mark.parametrize("confirm", [False, None, "yes", 1, 0])
def test_without_a_true_confirm_the_call_is_a_preview_and_sends_nothing(live, ig, confirm):
    result = live.hedge(confirm=confirm, expected_direction=None, expected_size=None)
    assert result.executed is False and result.plan.action == "hedge" and ig.orders == []


def test_a_preview_works_when_hedging_is_off(service, ig):
    result = service.hedge(confirm=False, expected_direction=None, expected_size=None)
    assert result.executed is False and result.plan.direction is Direction.SELL and ig.orders == []


def test_when_hedging_is_off_a_confirm_is_refused_before_anything_is_sent(service, ig):
    with pytest.raises(HedgingDisabled, match="dry run"):
        service.hedge(confirm=True, expected_direction="SELL", expected_size=6.0)
    assert ig.orders == [] and ig.requests == []


@pytest.mark.parametrize(
    ("direction", "size"),
    [
        (None, 6.0),
        ("SELL", None),
        ("sell", 6.0),
        ("HOLD", 6.0),
        ("SELL", 0),
        ("SELL", -1),
        ("SELL", "6"),
        ("SELL", True),
        (None, None),
    ],
)
def test_a_confirm_must_say_which_order_it_confirms(live, ig, direction, size):
    with pytest.raises(NotConfirmed, match="expected_direction"):
        live.hedge(confirm=True, expected_direction=direction, expected_size=size)
    assert ig.orders == []


def test_a_confirmed_hedge_is_placed_on_the_cfd_account_and_confirmed_by_ig(live, ig):
    plan = plan_of(live)
    result = live.hedge(confirm=True, expected_direction=plan.direction.value, expected_size=plan.size)
    assert (result.executed, result.outcome, result.reason) == (True, "accepted", "")
    (order,) = ig.orders
    assert order["account"] == CFD_ACCOUNT and order["epic"] == UNDERLYING
    assert order["direction"] == "SELL" and order["size"] == plan.size and order["currencyCode"] == "GBP"
    assert result.deal_reference == order["dealReference"] and result.deal_id.startswith("DEAL")
    assert len(ig.calls("confirm")) == 1  # the order's outcome was read, not assumed


def test_the_hedge_is_a_sell_for_a_long_call_and_a_buy_for_a_short_call(live, ig):
    """The first version's hedge was always a BUY."""
    p1 = plan_of(live)
    live.hedge(confirm=True, expected_direction=p1.direction.value, expected_size=p1.size)
    ig.positions[OPTIONS_ACCOUNT] = [option(direction="SELL")]
    live._state.last_order_at = None
    p2 = plan_of(live)
    live.hedge(confirm=True, expected_direction=p2.direction.value, expected_size=p2.size)
    assert [o["direction"] for o in ig.orders] == ["SELL", "BUY"]


def test_the_plan_is_worked_out_again_at_the_moment_of_the_order(live, ig):
    plan = plan_of(live)
    before = len(ig.calls("positions"))
    live.hedge(confirm=True, expected_direction=plan.direction.value, expected_size=plan.size)
    assert len(ig.calls("positions")) > before  # fresh data, not the preview's


def test_a_plan_that_has_moved_in_direction_is_refused_and_returned(live, ig):
    plan = plan_of(live)
    ig.positions[OPTIONS_ACCOUNT] = [option(direction="SELL")]  # the position changed after the preview
    with pytest.raises(PlanChanged) as caught:
        live.hedge(confirm=True, expected_direction=plan.direction.value, expected_size=plan.size)
    assert caught.value.plan.direction is Direction.BUY and caught.value.status == 409 and ig.orders == []


def test_a_plan_that_has_moved_in_size_is_refused(live, ig):
    plan = plan_of(live)
    with pytest.raises(PlanChanged, match="look at the new plan"):
        live.hedge(confirm=True, expected_direction=plan.direction.value, expected_size=plan.size * 1.5)
    assert ig.orders == []


def test_a_small_move_inside_the_tolerance_goes_ahead(live, ig):
    plan = plan_of(live)
    result = live.hedge(confirm=True, expected_direction=plan.direction.value, expected_size=plan.size * 1.03)
    assert result.executed and len(ig.orders) == 1


def test_a_market_that_moved_between_preview_and_order_is_caught(live, ig):
    plan = plan_of(live)
    ig.quotes[UNDERLYING].update(bid=6499.0, offer=6501.0)  # the delta, and so the hedge, changes a lot
    with pytest.raises(PlanChanged):
        live.hedge(confirm=True, expected_direction=plan.direction.value, expected_size=plan.size)
    assert ig.orders == []


def test_when_no_hedge_is_needed_any_more_nothing_is_sent(live, ig):
    plan = plan_of(live)
    ig.positions[CFD_ACCOUNT] = [cfd(direction="SELL", size=plan.size)]  # someone else hedged meanwhile
    result = live.hedge(confirm=True, expected_direction=plan.direction.value, expected_size=plan.size)
    assert result.executed is False and result.plan.action == "none" and ig.orders == []


def test_a_blocked_plan_is_not_executed(live, ig):
    plan = plan_of(live)
    ig.quotes[UNDERLYING]["marketStatus"] = "CLOSED"
    result = live.hedge(confirm=True, expected_direction=plan.direction.value, expected_size=plan.size)
    assert result.executed is False and result.plan.action == "blocked" and ig.orders == []


def test_a_rejected_order_is_reported_as_rejected_and_may_be_retried_at_once(
    env, ig, clock, options_client, cfd_client
):
    service = make_service(env, ig, clock, options_client, cfd_client, HEDGING_ENABLED="true", COOLDOWN_SECONDS="60")
    plan = plan_of(service)
    ig.deal_status, ig.reason = "REJECTED", "INSUFFICIENT_FUNDS"
    result = service.hedge(confirm=True, expected_direction=plan.direction.value, expected_size=plan.size)
    assert (result.executed, result.outcome, result.reason) == (True, "rejected", "INSUFFICIENT_FUNDS")
    ig.deal_status = "ACCEPTED"
    assert (
        service.hedge(confirm=True, expected_direction=plan.direction.value, expected_size=plan.size).outcome
        == "accepted"
    )
    assert service.history()[1]["outcome"] == "rejected"


def test_after_an_order_there_is_a_cooldown(env, ig, clock, options_client, cfd_client):
    service = make_service(env, ig, clock, options_client, cfd_client, HEDGING_ENABLED="true", COOLDOWN_SECONDS="60")
    plan = plan_of(service)
    service.hedge(confirm=True, expected_direction=plan.direction.value, expected_size=plan.size)
    with pytest.raises(TooSoon) as caught:
        service.hedge(confirm=True, expected_direction=plan.direction.value, expected_size=plan.size)
    assert caught.value.status == 429 and caught.value.retry_after == pytest.approx(60)
    assert len(ig.orders) == 1
    clock.advance(30)
    with pytest.raises(TooSoon) as caught:
        service.hedge(confirm=True, expected_direction=plan.direction.value, expected_size=plan.size)
    assert caught.value.retry_after == pytest.approx(30)
    clock.advance(31)
    service.hedge(
        confirm=True, expected_direction=plan.direction.value, expected_size=plan.size
    )  # allowed again (the plan may differ)


def test_the_cooldown_does_not_apply_to_previews(env, ig, clock, options_client, cfd_client):
    service = make_service(env, ig, clock, options_client, cfd_client, HEDGING_ENABLED="true", COOLDOWN_SECONDS="60")
    plan = plan_of(service)
    service.hedge(confirm=True, expected_direction=plan.direction.value, expected_size=plan.size)
    assert service.hedge(confirm=False, expected_direction=None, expected_size=None).executed is False


def test_an_order_whose_answer_was_lost_is_looked_up_and_not_repeated(live, ig):
    plan = plan_of(live)
    ig.drop_order_answer = True
    result = live.hedge(confirm=True, expected_direction=plan.direction.value, expected_size=plan.size)
    assert (
        result.outcome == "accepted" and len(ig.orders) == 1
    )  # IG had placed it; the reference found its confirmation


def test_an_order_with_no_answer_and_no_confirmation_is_unconfirmed_and_says_what_to_check(
    env, ig, clock, options_client, cfd_client
):
    service = make_service(env, ig, clock, options_client, cfd_client, HEDGING_ENABLED="true", COOLDOWN_SECONDS="60")
    plan = plan_of(service)
    ig.drop_order_answer = True
    ig.confirm_missing = True
    result = service.hedge(confirm=True, expected_direction=plan.direction.value, expected_size=plan.size)
    assert (
        result.outcome == "unconfirmed"
        and result.deal_reference in result.reason
        and "may have been placed" in result.reason
    )
    assert service.sleeps == [0.5, 0.5]  # it looked three times
    with pytest.raises(TooSoon):  # and it will not send another order at once
        service.hedge(confirm=True, expected_direction=plan.direction.value, expected_size=plan.size)


def test_a_confirmation_that_appears_on_the_second_look_is_used(live, ig):
    plan = plan_of(live)
    ig.confirm_missing = True
    live._sleep = lambda seconds: setattr(ig, "confirm_missing", False)
    result = live.hedge(confirm=True, expected_direction=plan.direction.value, expected_size=plan.size)
    assert result.outcome == "accepted"


def test_an_order_igs_refuses_outright_raises_and_starts_no_cooldown(env, ig, clock, options_client, cfd_client):
    service = make_service(env, ig, clock, options_client, cfd_client, HEDGING_ENABLED="true", COOLDOWN_SECONDS="60")
    plan = plan_of(service)
    ig.fail["order"] = httpx.Response(400, json={"errorCode": "error.trading.otc.market-orders.not-supported"})
    with pytest.raises(IGError) as caught:
        service.hedge(confirm=True, expected_direction=plan.direction.value, expected_size=plan.size)
    assert caught.value.kind == "rejected" and service.history() == []
    ig.fail.clear()
    assert service.hedge(confirm=True, expected_direction=plan.direction.value, expected_size=plan.size).executed


def test_a_rate_limited_order_raises(live, ig):
    plan = plan_of(live)
    ig.fail["order"] = error(403, "error.public-api.exceeded-account-trading-allowance")
    with pytest.raises(IGError) as caught:
        live.hedge(confirm=True, expected_direction=plan.direction.value, expected_size=plan.size)
    assert caught.value.kind == "rate_limit" and live.history() == []


def test_only_one_order_can_be_in_flight(live, ig):
    plan = plan_of(live)
    assert live._order_lock.acquire(blocking=False)
    try:
        with pytest.raises(Busy):
            live.hedge(confirm=True, expected_direction=plan.direction.value, expected_size=plan.size)
    finally:
        live._order_lock.release()
    assert ig.orders == []


def test_the_history_records_each_order_newest_first(live, ig):
    for _ in range(2):
        plan = plan_of(live)
        live.hedge(confirm=True, expected_direction=plan.direction.value, expected_size=plan.size)
        ig.positions[CFD_ACCOUNT] = []  # as if the hedge had been closed again
    history = live.history()
    assert len(history) == 2 and history[0]["time"] >= history[1]["time"]
    assert set(history[0]) == {
        "time",
        "deal_reference",
        "direction",
        "size",
        "net_delta_before",
        "outcome",
        "reason",
        "deal_id",
    }
    assert history[0]["direction"] == "SELL" and history[0]["outcome"] == "accepted"


def test_a_capped_order_is_placed_in_steps(env, ig, clock, options_client, cfd_client):
    service = make_service(env, ig, clock, options_client, cfd_client, HEDGING_ENABLED="true", MAX_HEDGE_SIZE="2")
    plan = service.analyse().plan
    assert plan.capped and plan.size == 2.0
    result = service.hedge(confirm=True, expected_direction=plan.direction.value, expected_size=plan.size)
    assert result.executed and ig.orders[0]["size"] == 2.0 and result.plan.remaining_net_delta > 0
