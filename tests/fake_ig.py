"""An in-process IG demo: the endpoints the client uses, with their documented behaviour, and a record of requests."""

from __future__ import annotations

import json

import httpx

BASE = "https://demo-api.ig.com/gateway/deal"  # intercepted by the fake transport: nothing leaves the machine
API_KEY, USERNAME, PASSWORD = "demo-api-key-value", "demo-user", "demo-password-value"
OPTIONS_ACCOUNT, CFD_ACCOUNT = "OPT123", "CFD456"
UNDERLYING = "IX.D.SPTRD.IFS.IP"


def option(
    deal_id="DIOPT001",
    name="US 500 Cash (£10) 5800 CALL",
    direction="BUY",
    size=10,
    contract=1,
    expiry="17-JAN-27",
    bid=170.0,
    offer=174.0,
):
    return {
        "position": {
            "dealId": deal_id,
            "direction": direction,
            "size": size,
            "contractSize": contract,
            "level": 150.0,
            "currency": "GBP",
        },
        "market": {
            "instrumentName": name,
            "instrumentType": "OPT_INDICES",
            "epic": f"OP.D.{deal_id}.IP",
            "expiry": expiry,
            "bid": bid,
            "offer": offer,
        },
    }


def cfd(deal_id="DICFD001", direction="BUY", size=1.0, contract=1.0, epic=UNDERLYING):
    return {
        "position": {
            "dealId": deal_id,
            "direction": direction,
            "size": size,
            "contractSize": contract,
            "level": 5800.0,
            "currency": "GBP",
        },
        "market": {
            "instrumentName": "US 500 Cash",
            "instrumentType": "INDICES",
            "epic": epic,
            "expiry": "-",
            "bid": 5799.0,
            "offer": 5801.0,
        },
    }


def error(status: int, code: str) -> httpx.Response:
    return httpx.Response(status, json={"errorCode": code})


class FakeIG:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.current_account = OPTIONS_ACCOUNT  # what a new login is on
        self.session_account: dict[str, str] = {}  # CST -> the account of that session
        self.generation = 0  # tokens of an earlier generation are refused
        self.logins = 0
        self.positions = {OPTIONS_ACCOUNT: [option()], CFD_ACCOUNT: []}
        self.quotes = {UNDERLYING: {"bid": 5799.0, "offer": 5801.0, "marketStatus": "TRADEABLE"}}
        self.orders: list[dict] = []  # (account, body) of every accepted POST
        self.confirmations: dict[str, dict] = {}
        self.deal_status = "ACCEPTED"  # what a new order is confirmed as
        self.reason = "SUCCESS"
        self.login_status = 200
        self.fail: dict[str, httpx.Response] = {}  # route -> the response to give
        self.drop_order_answer = False  # an order that is placed but whose answer never arrives
        self.confirm_missing = False

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def calls(self, route: str) -> list[httpx.Request]:
        return [r for r in self.requests if self._route(r) == route]

    @staticmethod
    def _route(request: httpx.Request) -> str:
        path = request.url.path.removeprefix("/gateway/deal")
        if path == "/session":
            return "login" if request.method == "POST" else "switch"
        if path == "/positions":
            return "positions"
        if path.startswith("/markets/"):
            return "market"
        if path == "/positions/otc":
            return "order"
        if path.startswith("/confirms/"):
            return "confirm"
        return "unknown"

    def expire_tokens(self) -> None:
        self.generation += 1

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self._route(request)
        if request.headers.get("X-IG-API-KEY") != API_KEY:
            return error(403, "error.security.api-key-invalid")
        if route in self.fail:
            return self.fail[route]
        if route == "login":
            body = json.loads(request.content)
            if self.login_status != 200 or body.get("identifier") != USERNAME or body.get("password") != PASSWORD:
                return error(401, "error.security.invalid-details")
            self.logins += 1
            cst = f"cst-{self.generation}-{self.logins}"
            self.session_account[cst] = self.current_account
            return httpx.Response(
                200,
                json={"currentAccountId": self.current_account, "accounts": []},
                headers={"CST": cst, "X-SECURITY-TOKEN": f"xst-{self.generation}-{self.logins}"},
            )
        cst = request.headers.get("CST", "")
        if not cst.startswith(f"cst-{self.generation}-") or cst not in self.session_account:
            return error(401, "error.security.client-token-invalid")
        account = self.session_account[cst]
        if route == "switch":
            target = json.loads(request.content)["accountId"]
            if target == account:
                return error(400, "error.switch.accountId-must-be-different")
            self.session_account[cst] = target
            return httpx.Response(200, json={"dealingEnabled": True})
        if route == "positions":
            return httpx.Response(200, json={"positions": self.positions.get(account, [])})
        if route == "market":
            epic = request.url.path.rsplit("/", 1)[1]
            quote = self.quotes.get(epic)
            if quote is None:
                return error(404, "error.service.marketdata.instrument.epic.unavailable")
            return httpx.Response(200, json={"instrument": {"epic": epic}, "snapshot": quote})
        if route == "order":
            body = json.loads(request.content)
            self.orders.append({"account": account, **body})
            self.confirmations[body["dealReference"]] = {
                "dealStatus": self.deal_status,
                "reason": self.reason,
                "dealId": "DEAL" + body["dealReference"][-6:],
                "dealReference": body["dealReference"],
            }
            if self.drop_order_answer:  # IG placed the order, but its answer never arrives
                raise httpx.ReadTimeout("no answer")
            return httpx.Response(200, json={"dealReference": body["dealReference"]})
        if route == "confirm":
            reference = request.url.path.rsplit("/", 1)[1]
            if self.confirm_missing or reference not in self.confirmations:
                return error(404, "error.confirms.deal-not-found")
            return httpx.Response(200, json=self.confirmations[reference])
        return error(404, "error.unknown-route")
