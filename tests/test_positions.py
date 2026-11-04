from datetime import UTC, date, datetime

import pytest

from delta_hedging.hedging import Direction
from delta_hedging.positions import (
    SECONDS_PER_YEAR,
    Unreadable,
    parse_expiry,
    read_option_position,
    years_to_expiry,
)
from delta_hedging.pricing import OptionType


def entry(**changes):
    position = {"dealId": "DIAAAA1", "direction": "BUY", "size": 2, "contractSize": 10, "level": 123.4}
    market = {
        "instrumentName": "Wall Street Cash (£2) 41600 PUT",
        "instrumentType": "OPT_INDICES",
        "epic": "OP.D.OTCDWS.41600P.IP",
        "expiry": "17-JAN-25",
        "bid": 100.0,
        "offer": 104.0,
    }
    position.update(changes.pop("position", {}))
    market.update(changes.pop("market", {}))
    return {"position": position, "market": market, **changes}


def test_an_option_position_is_read_from_ig_data():
    p = read_option_position(entry())
    assert (p.deal_id, p.direction, p.option_type, p.strike) == (
        "DIAAAA1",
        Direction.BUY,
        OptionType.PUT,
        41600.0,
    )
    assert (p.size, p.contract_size, p.expiry, p.epic) == (
        2.0,
        10.0,
        date(2025, 1, 17),
        "OP.D.OTCDWS.41600P.IP",
    )
    assert p.market_price == 102.0  # the mid of bid and offer


def test_the_strike_is_never_the_opening_level():
    """The first version used position['level'] (the price paid for the option) as the strike."""
    p = read_option_position(entry(position={"level": 777.7}))
    assert p.strike == 41600.0 and p.strike != 777.7


@pytest.mark.parametrize(
    ("name", "strike", "kind"),
    [
        ("Wall Street Cash (£2) 41600 PUT", 41600.0, OptionType.PUT),
        ("US 500 5800 Call", 5800.0, OptionType.CALL),
        ("us 500 cash 5800.5 call", 5800.5, OptionType.CALL),
        ("5800 CALL ... 5800 CALL", 5800.0, OptionType.CALL),  # the same reading twice is still one reading
        ("Germany 40 (€5) 22000 (Dec) PUT", 22000.0, OptionType.PUT),
    ],
)
def test_the_strike_and_type_come_from_the_instrument_name(name, strike, kind):
    p = read_option_position(entry(market={"instrumentName": name}))
    assert (p.strike, p.option_type) == (strike, kind)


@pytest.mark.parametrize(
    "name", ["", "Wall Street Cash", "41600", "5800 CALL and 5900 PUT", "5800 CALL / 5700 CALL", "PUT 5800"]
)
def test_a_name_that_does_not_give_one_strike_and_type_is_unreadable_never_guessed(name):
    """The first version made every option a CALL unless the text said PUT."""
    result = read_option_position(entry(market={"instrumentName": name}))
    assert isinstance(result, Unreadable) and "strike and type" in result.reason and result.deal_id == "DIAAAA1"


@pytest.mark.parametrize("kind", ["SHARES", "CURRENCIES", "UNKNOWN", "", None])
def test_a_position_that_is_not_an_option_is_ignored(kind):
    assert read_option_position(entry(market={"instrumentType": kind})) is None


@pytest.mark.parametrize("kind", ["OPT_INDICES", "OPT_SHARES", "opt_commodities"])
def test_the_option_instrument_types_are_recognised(kind):
    assert read_option_position(entry(market={"instrumentType": kind})) is not None


@pytest.mark.parametrize(
    "change",
    [
        {"position": {"direction": "LONG"}},
        {"position": {"direction": None}},
        {"position": {"size": 0}},
        {"position": {"size": "x"}},
        {"position": {"size": -1}},
        {"position": {"contractSize": None}},
        {"position": {"contractSize": 0}},
        {"market": {"expiry": "JAN-25"}},  # no day
        {"market": {"expiry": "DFB"}},
        {"market": {"expiry": "-"}},
        {"market": {"expiry": None}},
        {"market": {"epic": ""}},
    ],
)
def test_a_missing_or_invalid_field_makes_the_position_unreadable_rather_than_defaulted(change):
    result = read_option_position(entry(**change))
    assert isinstance(result, Unreadable) and "missing or invalid" in result.reason


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"position": {}},
        {"market": {}},
        {"position": "x", "market": {}},
        {"position": {"dealId": "D"}, "market": None},
    ],
)
def test_an_entry_without_position_and_market_is_unreadable(bad):
    assert isinstance(read_option_position(bad), Unreadable)


@pytest.mark.parametrize(
    "market",
    [
        {"bid": None, "offer": 5},
        {"bid": 5, "offer": None},
        {"bid": 0, "offer": 0},
        {"bid": "x", "offer": "y"},
    ],
)
def test_a_missing_option_price_is_none_not_zero(market):
    assert read_option_position(entry(market=market)).market_price is None


# -- expiry
@pytest.mark.parametrize(
    ("text", "expected"),
    [("17-JAN-25", date(2025, 1, 17)), ("01-dec-26", date(2026, 12, 1)), (" 29-FEB-28 ", date(2028, 2, 29))],
)
def test_expiry_dates_are_parsed_without_the_machines_locale(text, expected):
    assert parse_expiry(text) == expected


@pytest.mark.parametrize(
    "text", ["JAN-25", "-", "DFB", "", None, "31-FEB-25", "17-XXX-25", "2025-01-17", "17-JAN-2025", 5]
)
def test_other_expiry_forms_have_no_known_day(text):
    assert parse_expiry(text) is None


def test_years_to_expiry_runs_to_the_end_of_the_expiry_day_in_utc():
    noon = datetime(2025, 1, 17, 12, 0, 0, tzinfo=UTC)
    assert years_to_expiry(date(2025, 1, 17), noon) == pytest.approx((11 * 3600 + 59 * 60 + 59) / SECONDS_PER_YEAR)
    assert years_to_expiry(date(2025, 1, 18), noon) == pytest.approx(
        years_to_expiry(date(2025, 1, 17), noon) + 86400 / SECONDS_PER_YEAR
    )
    assert years_to_expiry(date(2025, 1, 16), noon) < 0  # expired


def test_years_to_expiry_converts_the_clock_to_utc():
    from datetime import timedelta, timezone

    plus_five = datetime(2025, 1, 17, 17, 0, 0, tzinfo=timezone(timedelta(hours=5)))  # that is 12:00 UTC
    assert years_to_expiry(date(2025, 1, 17), plus_five) == pytest.approx(
        years_to_expiry(date(2025, 1, 17), datetime(2025, 1, 17, 12, 0, 0, tzinfo=UTC))
    )
