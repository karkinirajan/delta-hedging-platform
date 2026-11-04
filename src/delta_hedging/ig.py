"""A small client for IG's REST trading API (demo environment only).

Written against IG's documentation (labs.ig.com): ``POST /session`` (version 2: the tokens come back in the ``CST``
and ``X-SECURITY-TOKEN`` headers), ``PUT /session`` to switch account, ``GET /positions`` (v2), ``GET /markets/{epic}``
(v3, the first version with ``snapshot.bid`` and ``snapshot.offer``), ``POST /positions/otc`` (v2, which answers with a
``dealReference`` and nothing else) and ``GET /confirms/{dealReference}`` (v1).

What it does differently from the first version's client:

* an order is not "done" because IG returned a reference: the confirmation is read and an ACCEPTED or REJECTED status
  is reported (the first version recorded a hedge as made whatever happened to the order);
* the client makes its own deal reference, so that an order whose answer was lost can be looked up instead of repeated;
* there is one client (and one session) per account, so no request ever runs on the wrong account (the first version
  logged in to the CFD account and back around every hedge, on state shared by all requests);
* rate limits are HTTP 403 with an ``error.public-api.exceeded-*`` code, as documented (the first version looked for
  429), and are reported instead of sleeping inside a request;
* tokens, passwords, headers and response bodies are never logged or put in an error message (the first version
  printed account ids and logged the login response's headers at DEBUG);
* every request has a timeout.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from .config import Settings
from .hedging import Direction

logger = logging.getLogger(__name__)

# How long the tokens are trusted. IG's guide says "initially valid for 6 hours" (extended while in use) and its FAQ
# says 12 hours between calls; the shorter is used, and a 401 logs in again whatever the clock says.
TOKEN_SECONDS = 6 * 3600
_SAFE_CODE = re.compile(
    r"[A-Za-z0-9._-]{1,80}"
)  # IG's errorCode strings look like error.public-api.exceeded-api-key-allowance
_DEAL_REFERENCE = re.compile(r"[A-Za-z0-9_-]{1,30}")  # the pattern IG documents for a deal reference
_RATE_LIMIT_PREFIX = "error.public-api.exceeded"


class IGError(Exception):
    """IG did not do what was asked. The message is safe to show: no token, URL, header or response body in it.

    `kind` is "auth", "rate_limit", "upstream", "rejected" or "bad_response". `outcome_unknown` is true when an order
    may have reached IG although no answer did.
    """

    def __init__(self, kind: str, message: str, *, code: str = "", outcome_unknown: bool = False) -> None:
        super().__init__(message)
        self.kind = kind
        self.code = code
        self.outcome_unknown = outcome_unknown


@dataclass(frozen=True, slots=True)
class Quote:
    bid: float
    offer: float
    status: str  # IG's marketStatus, e.g. TRADEABLE

    @property
    def mid(self) -> float:
        return (self.bid + self.offer) / 2

    @property
    def tradeable(self) -> bool:
        return self.status == "TRADEABLE"


@dataclass(frozen=True, slots=True)
class Confirmation:
    status: str  # ACCEPTED, REJECTED, or UNKNOWN when IG has no confirmation (yet)
    reason: str
    deal_id: str

    @property
    def accepted(self) -> bool:
        return self.status == "ACCEPTED"


def new_deal_reference() -> str:
    return "dh" + uuid.uuid4().hex[:20]


def _error_code(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    code = body.get("errorCode") if isinstance(body, dict) else None
    return code if isinstance(code, str) and _SAFE_CODE.fullmatch(code) else ""


def _number(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number > 0 and number != float("inf") else None


class IGClient:
    """One IG session, on one account."""

    def __init__(
        self,
        settings: Settings,
        account_id: str,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        for name in ("httpx", "httpcore"):  # at INFO, httpx logs every request line
            if logging.getLogger(name).level < logging.WARNING:
                logging.getLogger(name).setLevel(logging.WARNING)
        self._s = settings
        self._account = account_id
        self._http = client or httpx.Client(timeout=settings.request_timeout)
        self._clock = clock
        self._lock = threading.RLock()
        self._cst = ""
        self._token = ""
        self._expires = 0.0

    # -- the session
    def _headers(self, version: str, *, tokens: bool = True) -> dict[str, str]:
        headers = {
            "X-IG-API-KEY": self._s.ig_api_key,
            "Version": version,
            "Content-Type": "application/json",
            "Accept": "application/json; charset=UTF-8",
        }
        if tokens:
            headers["CST"], headers["X-SECURITY-TOKEN"] = self._cst, self._token
        return headers

    def _send(
        self, method: str, path: str, *, version: str, json: object = None, tokens: bool = True
    ) -> httpx.Response:
        try:
            return self._http.request(
                method, self._s.base_url + path, headers=self._headers(version, tokens=tokens), json=json
            )
        except httpx.HTTPError as exc:
            raise IGError(
                "upstream",
                f"IG could not be reached ({type(exc).__name__}).",
                outcome_unknown=method == "POST",
            ) from None

    def _login(self) -> None:
        response = self._send(
            "POST",
            "/session",
            version="2",
            tokens=False,
            json={"identifier": self._s.ig_username, "password": self._s.ig_password, "encryptedPassword": False},
        )
        if response.status_code != 200:
            code = _error_code(response)
            raise IGError(
                "auth",
                f"IG refused the login (HTTP {response.status_code}{', ' + code if code else ''}).",
                code=code,
            )
        cst, token = response.headers.get("CST"), response.headers.get("X-SECURITY-TOKEN")
        if not cst or not token:
            raise IGError("bad_response", "IG's login answer had no session tokens.")
        self._cst, self._token = cst, token
        try:
            current = response.json().get("currentAccountId")
        except (ValueError, AttributeError):
            current = None
        if current != self._account:
            self._switch_account()
        self._expires = self._clock() + TOKEN_SECONDS

    def _switch_account(self) -> None:
        response = self._send("PUT", "/session", version="1", json={"accountId": self._account})
        code = _error_code(response)
        if response.status_code not in (200, 204) and code != "error.switch.accountId-must-be-different":
            raise IGError(
                "auth",
                "IG would not switch to the configured account "
                f"(HTTP {response.status_code}{', ' + code if code else ''}).",
                code=code,
            )
        # IG may send new tokens with the answer
        self._cst = response.headers.get("CST") or self._cst
        self._token = response.headers.get("X-SECURITY-TOKEN") or self._token

    def _ensure_session(self) -> None:
        with self._lock:
            if not self._token or self._clock() >= self._expires:
                self._login()

    def _request(self, method: str, path: str, *, version: str, json: object = None) -> httpx.Response:
        self._ensure_session()
        response = self._send(method, path, version=version, json=json)
        if response.status_code == 401:  # the tokens were refused: log in once more and repeat
            with self._lock:
                self._login()
            response = self._send(method, path, version=version, json=json)
        if response.status_code in (200, 201, 204):
            return response
        code = _error_code(response)
        if code.startswith(_RATE_LIMIT_PREFIX):
            raise IGError("rate_limit", "IG's request allowance is used up. Try again in a minute.", code=code)
        kind = "auth" if response.status_code in (401, 403) else "upstream"
        logger.warning(
            "IG answered HTTP %s%s for %s %s",
            response.status_code,
            f" ({code})" if code else "",
            method,
            path.split("?")[0],
        )
        raise IGError(kind, f"IG answered HTTP {response.status_code}{', ' + code if code else ''}.", code=code)

    # -- reading
    def positions(self) -> list[dict]:
        """The raw ``positions`` list of the account: elements with a ``position`` and a ``market``."""
        body = self._json(self._request("GET", "/positions", version="2"))
        rows = body.get("positions")
        if not isinstance(rows, list):
            raise IGError("bad_response", "IG's positions answer had no positions list.")
        return [row for row in rows if isinstance(row, dict)]

    def quote(self, epic: str) -> Quote:
        body = self._json(self._request("GET", f"/markets/{epic}", version="3"))
        snapshot = body.get("snapshot")
        bid, offer = (
            (_number(snapshot.get("bid")), _number(snapshot.get("offer")))
            if isinstance(snapshot, dict)
            else (None, None)
        )
        if bid is None or offer is None:
            raise IGError("bad_response", "IG's market answer had no usable bid and offer.")
        return Quote(bid=bid, offer=offer, status=str(snapshot.get("marketStatus") or ""))

    # -- trading
    def open_position(self, epic: str, direction: Direction, size: float, currency: str, deal_reference: str) -> str:
        """Place a market order to open a position and return its deal reference. The order is not confirmed yet."""
        if not _DEAL_REFERENCE.fullmatch(deal_reference):
            raise ValueError("deal reference must be 1 to 30 letters, digits, dashes or underscores")
        if not size > 0:
            raise ValueError("size must be positive")
        order = {
            "epic": epic,
            "expiry": "-",
            "direction": direction.value,
            "size": round(size, 2),
            "orderType": "MARKET",
            "currencyCode": currency,
            "forceOpen": True,
            "guaranteedStop": False,
            "timeInForce": "EXECUTE_AND_ELIMINATE",
            "dealReference": deal_reference,
        }
        logger.info("Placing order %s: %s %s", deal_reference, direction.value, order["size"])
        try:
            response = self._request("POST", "/positions/otc", version="2", json=order)
        except IGError as exc:
            if exc.kind == "upstream" and exc.code == "":
                exc.outcome_unknown = True  # no HTTP answer, or one without a code: the order may have been placed
            elif exc.kind == "upstream":
                raise IGError("rejected", f"IG rejected the order ({exc.code}).", code=exc.code) from None
            raise
        reference = self._json(response).get("dealReference")
        if reference != deal_reference:
            raise IGError("bad_response", "IG's order answer carried another deal reference.", outcome_unknown=True)
        return deal_reference

    def confirm(self, deal_reference: str) -> Confirmation:
        if not _DEAL_REFERENCE.fullmatch(deal_reference):
            raise ValueError("deal reference must be 1 to 30 letters, digits, dashes or underscores")
        try:
            response = self._request("GET", f"/confirms/{deal_reference}", version="1")
        except IGError as exc:
            if exc.code == "error.confirms.deal-not-found":
                return Confirmation(status="UNKNOWN", reason="IG has no confirmation for this reference", deal_id="")
            raise
        body = self._json(response)
        status = str(body.get("dealStatus") or "")
        if status not in ("ACCEPTED", "REJECTED"):
            raise IGError("bad_response", "IG's confirmation had no dealStatus of ACCEPTED or REJECTED.")
        reason, deal_id = str(body.get("reason") or ""), str(body.get("dealId") or "")
        return Confirmation(
            status=status,
            reason=reason if _SAFE_CODE.fullmatch(reason) else "",
            deal_id=deal_id if _DEAL_REFERENCE.fullmatch(deal_id) else "",
        )

    @staticmethod
    def _json(response: httpx.Response) -> dict:
        try:
            body = response.json()
        except ValueError:
            raise IGError("bad_response", "IG answered something that is not JSON.") from None
        if not isinstance(body, dict):
            raise IGError("bad_response", "IG answered something unexpected.")
        return body
