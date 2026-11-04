# 2. IG and the two accounts

*Chapter 2 of 4. Previous: [The hedge](01-how-the-hedge-is-worked-out.md). Next: [API reference](03-api-reference.md).*

Options are read from one IG account and the hedge is placed on another (a CFD account in the underlying). The client
(`ig.py`) is written against IG's REST documentation at [labs.ig.com](https://labs.ig.com/) and runs against **demo
only**: the base URL is IG's demo gateway, and `IG_ENVIRONMENT` must be `demo`.

## One session per account

`HedgingService` holds two `IGClient`s, one per account. Each logs in on first use, switches to its own account once,
and renews its tokens when IG answers 401 (and before they are old, `TOKEN_SECONDS` = 6 hours). Nothing is sent to IG
when the app starts. The first version had one client that logged in to the CFD account and back around every hedge,
with the state shared by every request.

## The calls

| Call | Used for | Notes |
|---|---|---|
| `POST /session` (v2) | login | tokens come back in the `CST` and `X-SECURITY-TOKEN` headers |
| `PUT /session` | switch account | skipped when the session is already on the account |
| `GET /positions` (v2) | options and held hedges | |
| `GET /markets/{epic}` (v3) | bid, offer and market status of the underlying | spot is the mid |
| `POST /positions/otc` (v2) | the order | answers with a `dealReference` and nothing else |
| `GET /confirms/{dealReference}` (v1) | what happened to the order | `ACCEPTED` or `REJECTED` |

## Orders

* The client makes the deal reference itself (letters, digits, `_`, `-`, up to 30). If the answer to an order is
  lost, the order can still be looked up with that reference instead of being sent again.
* After the order the confirmation is read, up to three times 0.5 s apart. `ACCEPTED` or `REJECTED` is what is
  reported. If IG has no confirmation, the outcome is **`unconfirmed`** with the reference, and the cooldown starts: the
  order may have been placed, so check it on the IG platform before hedging again.
* A rejected order does not start the cooldown, so it can be retried at once.

## Rate limits and errors

IG reports an exceeded allowance as HTTP 403 with an `error.public-api.exceeded-*` code. The client turns that into
`rate_limit`, which the API answers as 429 with `Retry-After: 60`. It never sleeps inside a request. IG's error text is
not passed on, because a response body can contain request details; only a code made of letters, digits, dots, dashes
and underscores is shown.

Tokens, passwords, headers and response bodies are not logged and are not put in error messages. Every request has a
timeout (`REQUEST_TIMEOUT`).

## What has not been verified

* **The code has never talked to IG.** There were no credentials; the tests use a fake IG (a Python class, and a small
  HTTP server for the end-to-end run) written from the documentation. If the real service differs from the
  documentation, the first run on a demo account will show it.
* IG's documentation lists no strike or underlying on a position or a market, so the strike and the type are read from
  the instrument's **name** (for example `... 5800 CALL`). If your instruments are named differently, positions will
  show up under `unreadable` and block the plan, which is the safe failure.
* The size unit (contracts or the underlying's units), the currency and whether the option and the underlying move
  one for one are not checked against IG's contract specifications.
