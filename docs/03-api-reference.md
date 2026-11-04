# 3. API reference

*Chapter 3 of 4. Previous: [IG and the two accounts](02-ig-and-the-two-accounts.md). Next: [Safety model](04-safety-model.md).*

Every `/api` route needs `Authorization: Bearer <API_TOKEN>` (the scheme is case-insensitive). `/` (the dashboard) and
`/healthz` are public and hold no data. Responses are JSON, are never cached (`Cache-Control: no-store`) and carry no CORS
headers.

## `GET /api/analysis`

Query: `fresh=1` skips the short cache (`MARKET_DATA_TTL`) and asks IG again.

```json
{
  "analysis": {
    "generated_at": "2026-11-04T06:54:48+00:00",
    "spot": 5800.0,
    "market_status": "TRADEABLE",
    "positions": [{
      "deal_id": "...", "name": "...", "epic": "...", "direction": "BUY", "option_type": "CALL",
      "strike": 5800.0, "size": 10.0, "contract_size": 1.0, "expiry": "2027-01-17", "years": 0.205,
      "spot": 5800.0, "market_price": 172.0, "volatility": 0.135, "volatility_source": "implied",
      "greeks": {"price": 0, "delta": 0, "gamma": 0, "vega": 0, "theta": 0, "rho": 0},
      "delta_units": 5.786
    }],
    "unreadable": [{"deal_id": "...", "name": "...", "reason": "..."}],
    "expired": [{"deal_id": "...", "name": "...", "expiry": "..."}],
    "hedge_positions": [{"deal_id": "...", "direction": "SELL", "size": 3.0, "contract_size": 1.0, "units": -3.0}],
    "option_delta": 6.631, "held_units": 0.0,
    "plan": {"action": "hedge", "direction": "SELL", "size": 6.63, "capped": false, "option_delta": 6.631,
             "held_units": 0.0, "net_delta": 6.631, "remaining_net_delta": 0.001, "reason": "..."},
    "warnings": []
  },
  "settings": {"environment": "demo", "hedging_enabled": false, "underlying_epic": "...", "order_currency": "GBP",
               "delta_threshold": 0.05, "min_hedge_size": 0.01, "max_hedge_size": 100.0, "plan_tolerance": 0.05,
               "cooldown_seconds": 60.0, "rate": 0.05, "default_volatility": 0.2}
}
```

(The numbers are examples; `greeks` values are filled per position.) `plan.action` is `hedge`, `none` or `blocked`.
No credential, account id or token appears in any response.

## `POST /api/hedge`

Without `"confirm": true` (a JSON `true`, not `"yes"` or `1`) this is a **preview** and always answers 200:

```json
{"executed": false, "plan": {"action": "hedge", "direction": "SELL", "size": 6.63, "...": "..."},
 "outcome": "", "reason": "", "deal_reference": null, "deal_id": null}
```

To place the order, send exactly what the preview showed:

```json
{"confirm": true, "expected_direction": "SELL", "expected_size": 6.63}
```

| Status | When |
|---|---|
| 200, `executed: true` | the order was sent; `outcome` is `accepted`, `rejected` (with `reason`) or `unconfirmed` (IG had no confirmation, or it could not be read) |
| 200, `executed: false` | nothing to hedge now (plan `none` or `blocked`), or a preview |
| 400 | the body is not a JSON object |
| 400 | confirm is true but `expected_direction` / `expected_size` are missing or invalid |
| 401 | no or wrong token (`WWW-Authenticate: Bearer`) |
| 403 | `HEDGING_ENABLED` is off: a dry run, nothing is sent to IG |
| 409 | the plan is no longer what was previewed; the body has the new `plan` |
| 409 | another hedge is being placed at this moment |
| 413 | the request is over 16 KiB |
| 422 | IG refused the order outright |
| 429 | too many failed tokens from this address (a request with a correct token is never refused for that), the cooldown is running, or IG's allowance is used up; `Retry-After` is set |
| 502 | IG failed or refused the login |
| 500 | an unexpected error; the response says only `Internal error.` and the stack goes to the log |

## `GET /api/hedge/history`

`{"history": [{"time", "deal_reference", "direction", "size", "net_delta_before", "outcome", "reason", "deal_id"}]}`, newest
first. It is kept in memory: a restart empties it.

## `GET /healthz`

`{"status": "ok"}`. It does not call IG.
