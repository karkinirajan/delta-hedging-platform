"""Reading IG's position data into option positions, without guessing.

The first version filled what IG does not give with defaults: the strike became the position's *opening price*, the
option type became CALL unless the text said PUT, an unreadable expiry became "three months" and the underlying was
one hard-coded index. A hedge computed from those is wrong in ways nobody can see. IG's documentation lists no strike
or underlying field on a position or a market, so here the strike and the type are read from the instrument's name
and everything else must be present: a position that cannot be read is reported as such (and blocks the hedge).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time

from .hedging import Direction, OptionPosition
from .pricing import OptionType

# e.g. "... (£2) 41600 PUT": a number, perhaps a bracketed note, then CALL or PUT
_NAME = re.compile(r"(?P<strike>\d+(?:\.\d+)?)\s*(?:\([^)]*\)\s*)?(?P<type>CALL|PUT)\b", re.IGNORECASE)
_EXPIRY = re.compile(r"(?P<day>\d{2})-(?P<month>[A-Z]{3})-(?P<year>\d{2})")
_MONTHS = {
    m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)
}
SECONDS_PER_YEAR = 365 * 24 * 3600


@dataclass(frozen=True, slots=True)
class Unreadable:
    deal_id: str
    name: str
    reason: str


def parse_expiry(text: object) -> date | None:
    """A date from IG's ``DD-MON-YY``. ``MON-YY`` (no day), ``-`` and ``DFB`` give None: the day is not known."""
    match = _EXPIRY.fullmatch(str(text or "").strip().upper())
    if not match or match["month"] not in _MONTHS:
        return None
    try:
        return date(2000 + int(match["year"]), _MONTHS[match["month"]], int(match["day"]))
    except ValueError:
        return None


def years_to_expiry(expiry: date, now: datetime) -> float:
    """Years until the end of the expiry day (UTC): the time of day is an assumption, not something IG states."""
    end = datetime.combine(expiry, time(23, 59, 59), tzinfo=UTC)
    return (end - now.astimezone(UTC)).total_seconds() / SECONDS_PER_YEAR


def _positive(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number > 0 and number == number and number != float("inf") else None


def read_option_position(entry: dict) -> OptionPosition | Unreadable | None:
    """An option position from one element of IG's ``GET /positions``; None for a position that is not an option."""
    position, market = entry.get("position"), entry.get("market")
    if not isinstance(position, dict) or not isinstance(market, dict):
        return Unreadable(str(entry.get("deal_id", "?")), "", "the entry has no position and market")
    deal_id = str(position.get("dealId") or "?")
    name = str(market.get("instrumentName") or "")
    if not str(market.get("instrumentType") or "").upper().startswith("OPT_"):
        return None  # a CFD or a share in the same account: not part of the option delta
    matches = {(m["strike"], m["type"].upper()) for m in _NAME.finditer(name)}
    if len(matches) != 1:
        return Unreadable(deal_id, name, "the strike and type (CALL or PUT) cannot be read from the instrument name")
    ((strike_text, type_text),) = matches
    strike = _positive(strike_text)
    direction = position.get("direction")
    size, contract_size = _positive(position.get("size")), _positive(position.get("contractSize"))
    expiry = parse_expiry(market.get("expiry"))
    epic = str(market.get("epic") or "")
    if (
        strike is None
        or direction not in ("BUY", "SELL")
        or size is None
        or contract_size is None
        or expiry is None
        or not epic
    ):
        return Unreadable(
            deal_id,
            name,
            "the direction, size, contract size, expiry (DD-MON-YY) or epic is missing or invalid",
        )
    bid, offer = _positive(market.get("bid")), _positive(market.get("offer"))
    return OptionPosition(
        deal_id=deal_id,
        name=name,
        epic=epic,
        direction=Direction(direction),
        option_type=OptionType(type_text),
        strike=strike,
        size=size,
        contract_size=contract_size,
        expiry=expiry,
        market_price=(bid + offer) / 2 if bid is not None and offer is not None else None,
    )
