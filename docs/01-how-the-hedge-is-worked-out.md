# 1. How the hedge is worked out

*Chapter 1 of 4. Next: [IG and the two accounts](02-ig-and-the-two-accounts.md).*

An option's **delta** says how many units of the underlying its price moves with. A book of options is *delta
neutral* when the deltas, counted in units of the underlying, add up to zero. This project computes that sum and the
trade in the underlying that brings it to zero.

## Sign convention

All numbers are in **units of the underlying**.

| Held | Counts as |
|---|---|
| a bought (long) option | the option's delta × size × contract size |
| a sold (short) option | **minus** that |
| the underlying, bought | +1 per unit |
| the underlying, sold | −1 per unit |

The **net delta** is the sum of all of these. The hedge is the opposite trade: net delta positive means SELL, negative
means BUY. The code is in `hedging.py` (`position_delta`, `plan_hedge`), pure functions with no network.

## An example

Demo numbers, not market data: spot 5800, two positions, one year or less to expiry.

| Position | Option delta | Counts as |
|---|---|---|
| long 10 × 5800 CALL | +0.5785 | +5.785 |
| short 4 × 5600 PUT | −0.2114 | +0.846 (short, and a put's delta is negative) |
| **net delta** | | **+6.631** |

Both positions are *long* the underlying in effect, so the hedge is **SELL 6.63**. The first version could only ever
BUY: it took `abs(delta)`, then chose the direction with `BUY if size > 0`, and the size is never negative.

## From net delta to an order

1. If `|net| <= DELTA_THRESHOLD`: nothing to do.
2. Otherwise the order is `|net|`, rounded to 2 decimals, at most `MAX_HEDGE_SIZE` (the plan is marked *capped* and
   reports what would remain, so a large need is hedged in steps).
3. If the rounded size is below `MIN_HEDGE_SIZE`: nothing to do.
4. The plan is **blocked**, with the reason, when it cannot be worked out safely (below).

## The greeks

`pricing.py`, Black-Scholes for a European option on an asset without dividends, from the standard library only
(`math.erf` is the normal distribution). Units: `vega` and `rho` are per **1 percentage point** of volatility and of
the rate; `theta` is per **calendar day**. The tests compare the results with published values (a call with spot 100,
strike 100, one year, 20 % volatility and a 5 % rate: price 10.4506, delta 0.6368, gamma 0.01876, vega 0.3752).

No input is adjusted silently. Spot, strike, volatility and time must be positive; anything else raises an error.

### Implied volatility

Each position's volatility is the one that makes the model price equal the position's market price (the mid of the
bid and the offer). It is found by **bisection** on `[MIN_VOLATILITY, MAX_VOLATILITY]`. The price must lie strictly
between what those two volatilities give; a price outside (a worthless option, or below intrinsic value) has no
implied volatility, and the result is then `DEFAULT_VOLATILITY`, marked `volatility_source: "default"` and listed in
the analysis' `warnings`. The first version used a Newton step divided by a vega that was already scaled per
percentage point, which makes every step 100 times too large; replayed on three prices built from a known
volatility, it did not converge on any of them (see `work-done.md`).

## What blocks a plan

A plan is `blocked` (no order recommended, none can be placed) when:

* an option position cannot be read: its strike or type is not in the instrument name, or its direction, size,
  contract size, expiry (`DD-MON-YY`) or epic is missing or invalid (`positions.py`);
* a position in the underlying on the CFD account cannot be read, so what is already hedged is unknown;
* the underlying's market status is not `TRADEABLE`.

Positions past their expiry are left out of the sum and listed under `expired`. Positions in the underlying are only
the ones on the CFD account with exactly the configured epic; other CFD positions are not touched or counted.

## Assumptions of the model

Black-Scholes assumes a European option, no dividends, a constant rate and volatility, and continuous hedging.
Index options on IG may not meet these (the exercise style and the dividends are not checked). The greeks are an
estimate; treat the recommendation as an input to a decision, not as a guarantee of neutrality.
