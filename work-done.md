# Work done on delta-hedging-platform (branch `chore/modernize`)

Local commit only; nothing was pushed. The first version was a Flask app that read an IG demo account's option
positions, computed Black-Scholes greeks and placed a "hedge" order in the underlying. It is rebuilt as a small tested
package, **and its core was wrong in ways that matter for a program that places orders** (see the findings). Read the
findings first.

## Read this first: what to check

A pattern scan of every commit (Python, HTML, JS, text and Markdown files) found **no API key, password or token
shape**, and no `.env`, log or database file was ever tracked (the old `.gitignore` ignored `.env`). It did find two
identifiers, which are not secrets by themselves:

| What | Where in history | Do |
|---|---|---|
| a **public IP address** as the API address of the page, over plain HTTP | `app/templates/index.html`, 29 of the 52 commits | if a server still answers there, check it does not run the first version (no authentication, any origin allowed, debugger on) and shut that down or put the new version behind TLS |
| an IG **username / account identifier** written into the settings | `config/settings.py`, 38 of the 52 commits | treat it as an identifier that is now public if this repo is; the demo credentials themselves were not found |

The scan matches shapes of keys and literals assigned to key/password/token names; it cannot prove that nothing
else was ever committed. History was not rewritten; only you can push anything.

## What was there

* 52 commits (2025-01-13 to 2025-04-21); 21 tracked files at the last commit: `app/api/routes.py` (413 lines),
  `app/core/delta_hedger.py` (336), `app/core/option_calculator.py` (195), `app/models/{position,hedge_record,enums}.py`
  (250, 54, 18), `app/services/ig_client.py` (555), `app/templates/index.html` (851), `script.py` (426), `main.py` (14),
  `config/settings.py` (13), `requirements.txt` (31), a 163-line README, an MIT `LICENSE` and a 264-line `.gitignore`.
* A Flask API with ten routes (`/`, `/api/monitor/start`, `/api/positions`, `/api/positions/<id>`,
  `/api/positions/sold`, `/api/hedge/<id>`, `/api/hedge/all`, `/api/hedge/status`, `/api/settings`,
  `/api/analytics/<id>`), a Black-Scholes calculator on NumPy and SciPy, a requests-based IG client, and a page.
  `script.py` is a stand-alone second copy of the login, positions and order code with its own `Position` class.

## Findings in the first version

All checked against the code before they were written down here.

| Where | What was wrong |
|---|---|
| `delta_hedger.py:149` | `direction = BUY if hedge_size > 0 else SELL`, after `hedge_size` had been made positive with `abs()` and `max()`. **Every hedge was a BUY**, long or short, call or put: for half the positions the "hedge" doubled the exposure |
| `delta_hedger.py:143-146` | the size was `abs(delta) * size * contract`, and when the delta was exactly 0 or under 0.001 years remained, **an order of 10 % of the position size** was placed anyway |
| `position.py:30-55` | the strike defaulted to the position's opening level, the option type to CALL unless the instrument-type text said PUT, an unreadable expiry to 0.25 years, and the underlying was one hard-coded index |
| `delta_hedger.py` (`calculate_position_delta`), `ig_client.py` (`get_market_data`) | the price of the **underlying** was the option's own price (`get_market_data(position.epic)`), and the volatility was the day's percentage price change (`abs(percentageChange)/100`, at least 0.1): a one-day move used as an annual volatility |
| `delta_hedger.py` | each position was hedged on its own: nothing was netted, and a hedge already held was never counted |
| `ig_client.py` (`create_hedge_position`), `delta_hedger.py` (`hedge_position`) | an order counted as made when IG returned a `dealReference`; the confirmation was never read, so a rejected order was recorded as a hedge. The client logged in to the CFD account and back around every hedge, on state shared by all requests |
| `option_calculator.py` (`calculate_implied_volatility`) | implied volatility by a Newton step divided by a vega already scaled per percentage point, so every step was 100 times too large. **Replayed in a stand-alone script that copies its formulas, on three prices built from a known volatility (at the money, in the money, out of the money), it did not converge on any of them** (it jumped between the bounds 0.001 and 2.0) |
| `routes.py` | no authentication on any route, including the ones that place orders (`/api/hedge/<id>`, `/api/hedge/all`) and `POST /api/settings`; `CORS(app)` allowed every origin, so any web page open in the operator's browser could have placed an order |
| `main.py` | `app.run(host="0.0.0.0", debug=True)` |
| `routes.py:45-78` | `POST /api/monitor/start` called `hedger.start_monitoring(...)`, a method that does not exist (the only `def start_monitoring` is the route itself): the endpoint could only fail, and the error text was returned to the caller. All routes returned `str(e)` of internal errors |
| `ig_client.py` | logged in when the client was created (at import); printed the account ids; logged the login response's **headers and body at DEBUG** (the session tokens) |
| `index.html` | the API address was a public IP over plain HTTP; Tailwind from a CDN |
| `config/settings.py` | an IG username literal; `min_hedge_size` of 0.00 |
| README | said `python app.py` (there is no such file; the entry point is `main.py`), Python 3.8+, and did not list `IG_OPTIONS_ACCOUNT` and `IG_CFD_ACCOUNT`, which the client needs; `requirements.txt` was a `pip freeze` that included IPython |

## What was done, file by file

| First version | Now |
|---|---|
| `app/core/option_calculator.py` | `src/delta_hedging/pricing.py`: the standard library only (no NumPy/SciPy); inputs never clamped; implied volatility by bisection; vega and rho per 1 point, theta per day |
| `app/core/delta_hedger.py` | `hedging.py` (the plan, pure) and `service.py` (analysis, guarded order, cooldown, history) |
| `app/models/position.py`, `enums.py` | `positions.py`: reads a position or says why it cannot; no defaults |
| `app/models/hedge_record.py` | the in-memory order history in `service.py` |
| `app/services/ig_client.py`, `script.py` | `ig.py`: one client per account, confirmation read, own deal reference, no secrets in logs, timeouts |
| `app/api/routes.py`, `app/__init__.py` | `api.py` (3 routes, bearer token, throttle) and `app.py` (factory, headers, error handlers); no CORS |
| `app/templates/index.html` (851 lines) | `templates/dashboard.html`, `static/style.css`, `static/dashboard.js`: no CDN, no inline code, light and dark |
| `config/settings.py` (with a `POST /api/settings`) | `config.py`: validated environment variables, read-only at run time |
| `main.py` | `cli.py` (`run` on 127.0.0.1 without debug; `greeks`), `__main__.py` |
| `requirements.txt` | `pyproject.toml` + `uv.lock` (Python 3.13; 4 direct dependencies) |
| `README.md`, `.gitignore` | rewritten (the 264-line template `.gitignore` is now a short one) |
| (new) | `.env.example`, `Dockerfile`, `.dockerignore`, `.github/workflows/ci.yml`, `VERSIONS.md`, `docs/` (4 chapters), 490 test cases (191 test functions) |

### Behaviour that differs from the first version

* **A hedge is a decision about the whole portfolio**, with the sign fixed: a positive net delta is SELL.
* **Dry run by default**, and an order needs a confirmation of the previewed direction and size; the plan is worked
  out again at the order and a different one is a 409. A cooldown, one order at a time, and the result is read from
  IG's confirmation (`accepted`, `rejected` or `unconfirmed`).
* **Nothing is guessed.** An option position that cannot be read, a market that is not tradeable or an unreadable hedge
  position blocks the plan. Expired options are left out and reported.
* **Dropped, deliberately** (ask if you want any back): automatic monitoring (it never worked), hedging position by
  position (`/api/hedge/<id>`, `/api/hedge/all`), per-position endpoints, analytics and P&L (`/api/analytics`,
  `HedgeRecord`, `pnl_threshold`), the sold-positions listing, and changing settings through the API.
* The strike and the type are read from the instrument's **name**, since IG's documentation lists no such field.
* No numpy, scipy, requests or flask-cors.

## Verified (commands actually run)

| Check | Result |
|---|---|
| `pytest -q -W error --cov` | 490 passed, 1 skipped (warnings are errors); 99 % line and branch coverage (825 statements, 11 missed). The skip is a conditional test on a price below what the solver can tell apart |
| `ruff check`, `ruff format --check`, `uv lock --check` | pass |
| mutation check | 47 defects put back one at a time; the suite failed for all 47; files restored afterwards. Among them: **every hedge is a BUY**, held hedges ignored, no threshold, no confirm needed, dry run ignored, stale plan accepted, no cooldown, rejection treated as success, order sent on the wrong account, no authentication, no throttle, debug server, live IG accepted, hedging on by default, secrets in `repr`, vega not scaled, put delta without the -1, a price the model cannot give accepted. One pattern failed to match the reformatted line on the first run; it was fixed and re-run |
| `gunicorn` (1 worker, 4 threads) against a fake IG **over HTTP**, in dry-run and live mode | nothing sent to IG at start; 401 without or with a wrong token; analysis from the fake (two positions, implied volatilities, net delta +6.63, plan SELL 6.63); dry-run confirm = 403 with 0 orders at IG; no CORS header on a cross-origin preflight; live: preview sends nothing, the confirmed order reached the CFD account as SELL 6.63 and its confirmation was read; a second order = 429 with `Retry-After`; IG's rate limit = 429; a closed market = a blocked plan; gunicorn logs (151 and 154 lines, non-empty) contain **no key, username, password, token or traceback** |
| real browser (Chromium via Playwright), light and dark, 1280 / 768 / 375 px | wrong token message; loaded view; the token is in no storage and not in the page; the order button is disabled until the confirmation box is ticked; placing an order shows its reference and a history row (found and fixed: the result message was wiped by the reload that follows an order); keyboard only: Enter submits the token, Space ticks, Tab reaches the button, Enter places the order, and the box is unchecked afterwards; no page-level horizontal overflow (the positions table scrolls inside its box on narrow screens); text contrast 4.53:1 or more in both themes, form-field outline 4.56:1 and 5.47:1 |
| `docker build --no-cache`, then run with the HTTP fake | image 197 MB, user `app` (uid 10001), the container's HEALTHCHECK turned `healthy`, only `.venv README.md pyproject.toml src uv.lock .python-version` inside (no `.env`, tests or docs), 401 without a token, an order placed and confirmed, a second one refused, **no secret in the container log** |
| scan of the tree being committed and of all 52 commits | the list above; nothing key-shaped |

## Not verified

* **No request was ever sent to IG.** There were no credentials; everything ran against fakes written from IG's
  documentation (see `VERSIONS.md` and `docs/02`). Login, the account switch, the response shapes, `POST
  /positions/otc` and `GET /confirms/...` may differ on the real demo service.
* **Whether IG's instrument names contain `<strike> CALL|PUT`.** Positions that do not are reported as unreadable and
  block the plan; if that is your case, `positions.py` is the place to change.
* The model's assumptions (European exercise, no dividends) are not checked against the instruments, and neither are
  the size unit and the contract size against IG's contract specifications.
* GitHub Actions has not run (the same commands were run locally).

## Questions for the owner

1. **Should it hedge automatically?** The first version's monitoring never worked. A background loop would place
   orders without a request, which changes the safety model (`docs/04`); it is left out until you decide.
2. Is the instrument's name really `... <strike> CALL|PUT` on your account? If not, what identifies the strike and the
   type?
3. Which of the dropped features (per-position hedging, P&L and analytics, changing settings from the page) do you
   need? Each would be new work, with its own tests.
4. The API token is one shared secret and the history is in memory: is that enough, or do you need users and a
   persistent order log?
5. What should happen in the last days before expiry? The first version placed a minimal order near expiry; the new
   version hedges the delta that is there, and gamma near expiry is large, so it can change quickly.
