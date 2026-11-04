# Delta hedging on the IG demo API

Reads the option positions of an [IG](https://www.ig.com/) **demo** account, prices each with Black-Scholes (greeks, and
the implied volatility found from the option's own market price), adds the deltas up across the whole portfolio and
works out the trade in the underlying that makes it delta neutral. A dashboard and a small JSON API show the result.
An order is placed only when hedging is switched on **and** a request confirms the exact order that was previewed.

Only IG's demo environment is supported: the code refuses to start with anything else.

## Contents

1. [Features](#features)
2. [Quickstart](#quickstart)
3. [Configuration](#configuration)
4. [The API](#the-api)
5. [How a hedge is decided](#how-a-hedge-is-decided)
6. [Project structure](#project-structure)
7. [Testing](#testing)
8. [Docker](#docker)
9. [Versions](#versions)
10. [License](#license)

The documentation is a short book, in reading order:
[1 The hedge](docs/01-how-the-hedge-is-worked-out.md) ·
[2 IG and the two accounts](docs/02-ig-and-the-two-accounts.md) ·
[3 API reference](docs/03-api-reference.md) ·
[4 Safety model](docs/04-safety-model.md).

## Features

* **Greeks** (price, delta, gamma, vega, theta, rho) from the standard library alone, checked against textbook values.
  No input is clamped: a volatility or a time outside the valid range is an error, not a silently different number.
* **Implied volatility** from each position's market price, by bisection (it always converges inside the interval, or
  says the price is not attainable). When none can be found the configured default is used and **labelled as such**.
* **A portfolio hedge, not one per position**: long and short positions, calls and puts are netted, and hedges already
  held in the underlying are counted. A positive net delta means SELL, a negative one BUY.
* **Fails closed.** A position whose strike, type or expiry cannot be read, or a market that is not tradeable, blocks
  the plan; nothing is guessed from defaults.
* **Dry run by default** (`HEDGING_ENABLED=false`). With it on, an order needs `confirm: true` *and* the direction and
  size that were previewed; if the book moved in between, the answer is a 409 with the new plan.
* **The order is checked.** IG answers an order with a reference only; the confirmation is read, and ACCEPTED or
  REJECTED is what is reported and recorded.
* A cooldown between orders, a counter on failed token checks (a correct token is never refused), no CORS, a strict Content-Security-Policy, tokens and
  passwords never in logs, URLs, pages or error messages.
* One IG session per account (options, CFD): no request ever runs on the wrong account.

What it does **not** do: it does not hedge by itself. There is no background loop; a hedge happens only when a request
confirms one (the first version's "start monitoring" endpoint called a method that did not exist).

## Quickstart

You need [uv](https://docs.astral.sh/uv/) and an IG **demo** account with an API key
(see [IG's labs site](https://labs.ig.com/)).

```bash
uv sync
cp .env.example .env        # fill in your demo account; never commit it
uv run delta-hedging run    # http://127.0.0.1:8000
```

Open the page, paste the `API_TOKEN` from your `.env`, press Load. With `HEDGING_ENABLED=false` nothing but reads is
ever sent to IG.

The same from the command line:

```bash
curl -s -H "Authorization: Bearer $API_TOKEN" http://127.0.0.1:8000/api/analysis | jq .analysis.plan
curl -s -H "Authorization: Bearer $API_TOKEN" -X POST http://127.0.0.1:8000/api/hedge          # a preview
```

The calculator needs no IG access at all:

```bash
uv run delta-hedging greeks --type call --spot 100 --strike 100 --years 1 --volatility 0.2 --rate 0.05
```

The development server (`run`) listens on `127.0.0.1` only and never in debug mode. For anything else use gunicorn (see
[Docker](#docker)) behind a TLS proxy; the API token travels in a header.

## Configuration

All values come from the environment (a `.env` in the working directory is read). Missing or invalid values stop the
start with a message naming the variable. [`.env.example`](.env.example) lists every variable.

| Variable | Default | Meaning |
|---|---|---|
| `IG_API_KEY`, `IG_USERNAME`, `IG_PASSWORD` | required | the demo account's credentials |
| `IG_OPTIONS_ACCOUNT`, `IG_CFD_ACCOUNT` | required | where the options are read, and where hedges are placed |
| `IG_UNDERLYING_EPIC` | required | the instrument the hedges are placed in |
| `API_TOKEN` | required | bearer token for `/api`, at least 24 characters |
| `HEDGING_ENABLED` | `false` | `true` lets a confirmed request place an order |
| `ORDER_CURRENCY` | `GBP` | currency code of the orders |
| `DELTA_THRESHOLD` | `0.05` | net delta (units of the underlying) left alone |
| `MIN_HEDGE_SIZE`, `MAX_HEDGE_SIZE` | `0.01`, `100` | smallest and largest order; a larger need is hedged in steps |
| `PLAN_TOLERANCE` | `0.05` | how far the size may move between preview and order (fraction) |
| `COOLDOWN_SECONDS` | `60` | minimum time between two orders |
| `RISK_FREE_RATE` | `0.05` | the model's rate |
| `DEFAULT_VOLATILITY`, `MIN_VOLATILITY`, `MAX_VOLATILITY` | `0.2`, `0.001`, `2.0` | used, and labelled, when no implied volatility is found |
| `MARKET_DATA_TTL`, `REQUEST_TIMEOUT` | `5`, `30` | seconds an answer of IG is kept; timeout of a request to IG |
| `TRUSTED_PROXY_HOPS` | `0` | reverse proxies in front of the app whose `X-Forwarded-For` is believed (0 to 5); see [docs/04](docs/04-safety-model.md) |
| `IG_ENVIRONMENT` | `demo` | only `demo` is accepted |
| `IG_BASE_URL` | IG's demo gateway | only for local fakes, and only a loopback address: any other address is refused because the login sends the password to it |

## The API

Every `/api` route needs `Authorization: Bearer <API_TOKEN>`. The page `/` and `/healthz` are public and carry no data.

| Route | Does |
|---|---|
| `GET /api/analysis` | positions with their greeks, the portfolio delta, the plan and the settings; `?fresh=1` skips the short cache |
| `POST /api/hedge` | a preview; with `{"confirm": true, "expected_direction": "SELL", "expected_size": 6.63}` the order |
| `GET /api/hedge/history` | orders placed since the server started |

Errors are JSON `{"error": ...}`: 401 (token), 403 (hedging is off), 409 (the plan changed, or another hedge is being placed), 422 (IG refused the
order), 429 (throttled, cooling down or IG's allowance used up; `Retry-After` is set), 502 (IG failed), 500
(nothing internal is shown). Full reference: [docs/03-api-reference.md](docs/03-api-reference.md).

## How a hedge is decided

1. Each option position gets a delta from Black-Scholes: spot from the underlying's bid and offer, strike and type from
   the instrument's name, time to expiry from its expiry date, volatility implied by its market price.
2. A long position counts with the option's delta, a short one with the opposite; times size and contract size.
3. The positions are added up, and the held hedges (long +, short −) are added to that: the **net delta**.
4. If its size is within `DELTA_THRESHOLD` nothing is done. Otherwise the order is the opposite of the net delta, at
   least `MIN_HEDGE_SIZE`, at most `MAX_HEDGE_SIZE`.

Worked through with numbers in [docs/01](docs/01-how-the-hedge-is-worked-out.md).

## Project structure

```
src/delta_hedging/
  pricing.py     Black-Scholes price, greeks, implied volatility (pure)
  hedging.py     the hedge plan from net delta and rules (pure)
  positions.py   IG position data -> option positions, or "unreadable" (pure)
  ig.py          the IG REST client, one per account
  service.py     analysis, plan, guarded order, cooldown, history
  config.py      settings from the environment, validated
  api.py         routes, bearer-token check, failed-attempt throttle
  app.py         the Flask factory, headers, error handlers
  cli.py         `run` and `greeks`
  templates/ static/   the dashboard (no inline code, nothing from other sites)
tests/           one module per source module, plus security and hygiene tests
docs/            the four-chapter book
```

## Testing

```bash
uv run pytest -q -W error --cov       # warnings are errors
uv run ruff check . && uv run ruff format --check .
```

Tests run against a fake IG; no network and no credentials are needed. Besides the usual tests there is a hygiene
module that fails if a credential, a database, an IP address or a debug server is committed, and the pricing is
checked against published values. The suite was also tested by **mutation**: 62 defects (among them the first
version's "every hedge is a BUY") were put back one at a time and the suite failed for each.

## Docker

```bash
docker build -t delta-hedging .
docker run --rm -p 8000:8000 --env-file .env delta-hedging
```

The image runs gunicorn as an unprivileged user with **one worker** (several threads): the cooldown and the list of
orders live in the process, so a second worker would have its own. Nothing is baked in; the settings come from the
environment.

## Versions

Python 3.13, pinned with `uv.lock`. See [VERSIONS.md](VERSIONS.md).

## License

MIT, see [LICENSE](LICENSE).
