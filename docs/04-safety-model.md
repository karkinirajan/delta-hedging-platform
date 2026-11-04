# 4. Safety model

*Chapter 4 of 4. Previous: [API reference](03-api-reference.md).*

This program can place orders. This chapter lists what stands between a request and an order, what each guard is for,
and what is **not** covered.

## The guards, in the order a request meets them

1. **Demo only.** The base URL and `IG_ENVIRONMENT` are checked at start; anything but IG's demo is refused.
2. **A bearer token** on every `/api` route, compared in constant time, at least 24 characters. A correct token is
   **always** let through. Failed attempts are counted per client address; from the tenth within a minute they get a 429
   instead of a 401 (a signal to slow down, and visible in the log). The count is a signal, not the protection: a fast
   guesser is not stopped by it, the length of the token is what protects it, so generate it with
   `secrets.token_urlsafe(32)`. The table of addresses is bounded.
3. **No CORS and no cookies.** No other web page can read a response, and there is no ambient credential for a page
   to ride on (the first version had `CORS(app)` and no authentication at all, on an endpoint that places orders).
4. **Dry run by default.** With `HEDGING_ENABLED=false` a confirmed request is a 403 and nothing is sent.
5. **An explicit confirm** (`confirm` must be the JSON `true`) with the **direction and size that were previewed**.
6. **The plan is worked out again** from IG's data of that moment. If its direction differs, or its size differs by
   more than `PLAN_TOLERANCE` (at least 0.01), the answer is a 409 with the new plan and nothing is sent.
7. **A cooldown** between orders (`COOLDOWN_SECONDS`), and **one order at a time** (a second concurrent one is refused).
8. **Fail closed on data.** Unreadable positions, an untradeable market or an expired option never become defaults;
   they block the plan or are left out and reported.
9. **The result is checked** with IG's confirmation; `rejected` and `unconfirmed` are reported as they are. Once an
   order may exist at IG, the cooldown and the history entry are recorded **even if the confirmation cannot be read**.

Order size is also bounded by `MIN_HEDGE_SIZE` and `MAX_HEDGE_SIZE`.

## Headers and the page

A Content-Security-Policy of `default-src 'self'` with `frame-ancestors 'none'`, `nosniff`, `no-referrer`,
`X-Frame-Options: DENY` and `Cross-Origin-Resource-Policy: same-origin`. The dashboard has no inline script or style and
loads nothing from another site; it writes text with `textContent`, never `innerHTML`. The API token is kept in the
page's memory only: not in `localStorage`, a cookie or the URL, and reloading the page forgets it.

## What is not covered

* **Single process.** The cooldown, the throttle and the history are in memory. Run one worker (the Docker image does);
  two workers would each have their own.
* **The failure count keys on the client address Flask sees.** Behind a reverse proxy that is the proxy, so every client
  shares one count (harmless, because a correct token is never refused, but it makes the count useless). Set
  `TRUSTED_PROXY_HOPS` to the number of proxies in front of the app and `X-Forwarded-For` is believed for exactly that
  many hops; with 0 (the default) the header is ignored, so a client cannot choose its own address.
* **A shared token.** There is one token, no users and no audit trail beyond the log and the in-memory history.
* **No TLS.** Put it behind a proxy that terminates TLS; the token travels in a header.
* **Live IG.** The IG client has not been run against IG (see [chapter 2](02-ig-and-the-two-accounts.md)).
* **No automatic hedging.** Nothing places an order unless a request confirms it.
