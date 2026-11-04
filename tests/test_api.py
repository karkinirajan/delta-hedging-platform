import json

import httpx
import pytest

from delta_hedging import api
from delta_hedging.config import Settings

from .conftest import AUTH, TOKEN, make_app
from .fake_ig import API_KEY, CFD_ACCOUNT, PASSWORD, UNDERLYING, USERNAME, cfd, error, option


def get(app, path="/api/analysis", **kw):
    return app.test_client().get(path, headers=AUTH, **kw)


def post(app, body=None, **kw):
    return app.test_client().post("/api/hedge", headers=AUTH, json=body if body is not None else {}, **kw)


# -- authentication
@pytest.mark.parametrize("path", ["/api/analysis", "/api/hedge/history", "/api/nothing"])
def test_the_api_needs_the_token(app, path):
    response = app.test_client().get(path)
    assert response.status_code == 401 and response.headers["WWW-Authenticate"] == "Bearer"
    assert "bearer token" in response.get_json()["error"]


def test_placing_a_hedge_needs_the_token_too(live_app, ig):
    response = live_app.test_client().post(
        "/api/hedge", json={"confirm": True, "expected_direction": "SELL", "expected_size": 5}
    )
    assert response.status_code == 401 and ig.orders == [] and ig.requests == []


@pytest.mark.parametrize(
    "header",
    [
        "",
        "Bearer",
        "Bearer ",
        f"Basic {TOKEN}",
        TOKEN,
        "Bearer wrong",
        f"Bearer {TOKEN}x",
        f"Bearer {TOKEN[:-1]}",
        f"bearer  {TOKEN[::-1]}",
    ],
)
def test_a_wrong_credential_is_refused(app, header):
    assert app.test_client().get("/api/analysis", headers={"Authorization": header}).status_code == 401


def test_the_bearer_scheme_is_case_insensitive_and_the_right_token_is_accepted(app):
    for scheme in ("Bearer", "bearer", "BEARER"):
        assert app.test_client().get("/api/analysis", headers={"Authorization": f"{scheme} {TOKEN}"}).status_code == 200


def test_the_dashboard_and_the_health_check_are_public_and_hold_no_data(app, ig):
    client = app.test_client()
    assert client.get("/healthz").get_json() == {"status": "ok"}
    page = client.get("/")
    assert page.status_code == 200 and b"API token" in page.data and b"DIOPT001" not in page.data
    assert ig.requests == []  # neither asked IG for anything


def test_too_many_failed_attempts_are_throttled_per_client(app, monkeypatch):
    clock = {"now": 5000.0}
    monkeypatch.setattr(api.time, "monotonic", lambda: clock["now"])
    client = app.test_client()
    for _ in range(api.MAX_FAILURES):
        assert client.get("/api/analysis", headers={"Authorization": "Bearer wrong"}).status_code == 401
    blocked = client.get("/api/analysis", headers=AUTH)  # even the right token waits
    assert blocked.status_code == 429 and int(blocked.headers["Retry-After"]) >= 1
    other = app.test_client().get("/api/analysis", headers=AUTH, environ_overrides={"REMOTE_ADDR": "10.9.9.9"})
    assert other.status_code == 200  # another address is not affected
    clock["now"] += api.FAILURE_WINDOW + 1
    assert client.get("/api/analysis", headers=AUTH).status_code == 200


def test_successful_requests_do_not_count_as_failures(app):
    client = app.test_client()
    for _ in range(api.MAX_FAILURES * 2):
        assert client.get("/api/analysis", headers=AUTH).status_code == 200


# -- reading
def test_the_analysis_has_the_portfolio_the_plan_and_the_settings(app):
    body = get(app).get_json()
    a, s = body["analysis"], body["settings"]
    assert set(a) == {"generated_at", "spot", "market_status", "positions", "unreadable", "expired", "hedge_positions",
                      "option_delta", "held_units", "plan", "warnings"}  # fmt: skip
    (p,) = a["positions"]
    assert (
        p["deal_id"] == "DIOPT001" and p["direction"] == "BUY" and p["option_type"] == "CALL" and p["strike"] == 5800.0
    )
    assert p["volatility_source"] == "implied" and set(p["greeks"]) == {
        "price",
        "delta",
        "gamma",
        "vega",
        "theta",
        "rho",
    }
    assert a["spot"] == 5800.0 and a["plan"]["action"] == "hedge" and a["plan"]["direction"] == "SELL"
    assert s["environment"] == "demo" and s["hedging_enabled"] is False and s["underlying_epic"] == UNDERLYING


def test_no_secret_is_ever_in_a_response(app):
    client = app.test_client()
    for path in ("/api/analysis", "/api/hedge/history", "/", "/healthz"):
        text = client.get(path, headers=AUTH).get_data(as_text=True)
        for secret in (API_KEY, USERNAME, PASSWORD, TOKEN, "cst-", "xst-"):
            assert secret not in text, (path, secret)


def test_fresh_asks_ig_again_otherwise_the_answer_is_kept_briefly(app, ig):
    get(app)
    get(app)
    assert len(ig.calls("market")) == 1
    get(app, query_string={"fresh": "1"})
    assert len(ig.calls("market")) == 2


def test_the_history_starts_empty(app):
    assert get(app, "/api/hedge/history").get_json() == {"history": []}


# -- previews and orders
@pytest.mark.parametrize("body", [{}, {"confirm": False}, {"confirm": None}, {"confirm": "yes"}])
def test_a_hedge_request_without_a_true_confirm_is_a_preview(live_app, ig, body):
    response = post(live_app, body)
    data = response.get_json()
    assert response.status_code == 200 and data["executed"] is False and data["plan"]["direction"] == "SELL"
    assert data["outcome"] == "" and ig.orders == []


def test_a_request_with_no_body_at_all_is_a_preview(live_app, ig):
    response = live_app.test_client().post("/api/hedge", headers=AUTH)
    assert response.status_code == 200 and response.get_json()["executed"] is False and ig.orders == []


@pytest.mark.parametrize("raw", ["[1, 2]", "not json", '"text"', "5"])
def test_a_body_that_is_not_a_json_object_is_a_400(live_app, ig, raw):
    response = live_app.test_client().post("/api/hedge", headers={**AUTH, "Content-Type": "application/json"}, data=raw)
    assert response.status_code == 400 and "JSON object" in response.get_json()["error"] and ig.orders == []


def test_with_hedging_off_a_confirm_is_a_403_and_sends_nothing(app, ig):
    response = post(app, {"confirm": True, "expected_direction": "SELL", "expected_size": 6.0})
    assert response.status_code == 403 and "dry run" in response.get_json()["error"]
    assert ig.orders == [] and ig.requests == []


def test_a_confirm_without_the_expected_order_is_a_400(live_app, ig):
    response = post(live_app, {"confirm": True})
    assert response.status_code == 400 and "expected_direction" in response.get_json()["error"] and ig.orders == []


def preview(client_app):
    return post(client_app, {}).get_json()["plan"]


def test_a_confirmed_order_is_placed_and_reported(live_app, ig):
    plan = preview(live_app)
    response = post(live_app, {"confirm": True, "expected_direction": plan["direction"], "expected_size": plan["size"]})
    data = response.get_json()
    assert response.status_code == 200 and data["executed"] is True and data["outcome"] == "accepted"
    assert data["deal_reference"] == ig.orders[0]["dealReference"] and data["deal_id"].startswith("DEAL")
    assert ig.orders[0]["direction"] == "SELL" and ig.orders[0]["account"] == CFD_ACCOUNT
    history = get(live_app, "/api/hedge/history").get_json()["history"]
    assert len(history) == 1 and history[0]["outcome"] == "accepted" and history[0]["direction"] == "SELL"


def test_a_stale_confirm_is_a_409_with_the_new_plan(live_app, ig):
    plan = preview(live_app)
    ig.positions[ig_options()] = [option(direction="SELL")]  # the position changed after the preview
    response = post(live_app, {"confirm": True, "expected_direction": plan["direction"], "expected_size": plan["size"]})
    data = response.get_json()
    assert (
        response.status_code == 409 and data["plan"]["direction"] == "BUY" and "look at the new plan" in data["error"]
    )
    assert ig.orders == []


def ig_options():
    from .fake_ig import OPTIONS_ACCOUNT

    return OPTIONS_ACCOUNT


def test_a_second_order_inside_the_cooldown_is_a_429_with_retry_after(env, ig, service_for):
    settings = Settings.from_env({**env, "HEDGING_ENABLED": "true", "COOLDOWN_SECONDS": "60"})
    app = make_app(settings, service_for(settings))
    plan = preview(app)
    body = {"confirm": True, "expected_direction": plan["direction"], "expected_size": plan["size"]}
    assert post(app, body).status_code == 200
    again = post(app, body)
    assert again.status_code == 429 and int(again.headers["Retry-After"]) >= 60 and len(ig.orders) == 1


def test_a_rejected_order_is_a_200_that_says_rejected(live_app, ig):
    plan = preview(live_app)
    ig.deal_status, ig.reason = "REJECTED", "INSUFFICIENT_FUNDS"
    data = post(
        live_app, {"confirm": True, "expected_direction": plan["direction"], "expected_size": plan["size"]}
    ).get_json()
    assert data["executed"] is True and data["outcome"] == "rejected" and data["reason"] == "INSUFFICIENT_FUNDS"


def test_a_blocked_plan_is_reported_and_nothing_is_sent(live_app, ig):
    ig.positions[ig_options()] = [option(), option(deal_id="DIBAD", name="Mystery")]
    data = preview(live_app)
    assert data["action"] == "blocked" and data["size"] == 0
    response = post(live_app, {"confirm": True, "expected_direction": "SELL", "expected_size": 5})
    assert response.get_json()["executed"] is False and ig.orders == []


def test_held_hedges_change_the_plan_through_the_api(app, ig):
    ig.positions[CFD_ACCOUNT] = [cfd(direction="SELL", size=3.0)]
    a = get(app).get_json()["analysis"]
    assert a["held_units"] == -3.0 and a["hedge_positions"][0]["direction"] == "SELL"


# -- errors from IG and from the code
@pytest.mark.parametrize(
    ("failure", "status"),
    [
        (error(403, "error.public-api.exceeded-account-allowance"), 429),
        (httpx.Response(500, json={"errorCode": "error.service.unavailable", "message": "SECRET"}), 502),
        (httpx.Response(200, text="<html>"), 502),
    ],
)
def test_ig_failures_become_clean_json_errors(app, ig, failure, status):
    ig.fail["positions"] = failure
    response = get(app)
    assert response.status_code == status and "error" in response.get_json()
    text = response.get_data(as_text=True)
    assert "SECRET" not in text and "<html>" not in text and API_KEY not in text
    if status == 429:
        assert response.headers["Retry-After"] == "60"


def test_a_login_failure_is_a_502_without_credentials(app, ig):
    ig.login_status = 401
    response = get(app)
    assert response.status_code == 502 and "refused the login" in response.get_json()["error"]
    for secret in (API_KEY, USERNAME, PASSWORD):
        assert secret not in response.get_data(as_text=True)


def test_an_order_igs_refuses_is_a_422_with_its_code(live_app, ig):
    plan = preview(live_app)
    ig.fail["order"] = httpx.Response(400, json={"errorCode": "error.trading.otc.market-orders.not-supported"})
    response = post(live_app, {"confirm": True, "expected_direction": plan["direction"], "expected_size": plan["size"]})
    assert response.status_code == 422 and "market-orders.not-supported" in response.get_json()["error"]


def test_an_unexpected_error_is_a_500_that_says_nothing(app, monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise RuntimeError("SECRET internal detail with token " + TOKEN)

    monkeypatch.setattr(app.extensions["delta_hedging"].hedging, "analyse", boom)
    with caplog.at_level("ERROR"):
        response = get(app)
    assert response.status_code == 500 and response.get_json() == {"error": "Internal error."}
    assert "SECRET" not in response.get_data(as_text=True)


def test_unknown_paths_and_methods_are_json_errors(app):
    client = app.test_client()
    assert client.get("/nothing").status_code == 404 and client.get("/nothing").get_json() == {"error": "Not found"}
    assert client.put("/api/hedge", headers=AUTH).status_code == 405
    assert client.delete("/api/analysis", headers=AUTH).get_json() == {"error": "Method not allowed"}


def test_a_large_request_is_refused(live_app):
    response = live_app.test_client().post("/api/hedge", headers=AUTH, json={"confirm": False, "pad": "x" * 20_000})
    assert response.status_code == 413


def test_creating_the_app_sends_nothing_to_ig(settings, monkeypatch):
    """The first version logged in to IG when its routes module was imported."""
    calls = []
    monkeypatch.setattr(
        httpx.Client, "send", lambda self, *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError("sent"))
    )
    app = make_app_real(settings)
    assert app is not None and calls == []


def make_app_real(settings):
    from delta_hedging.app import create_app

    return create_app(settings)  # builds the real clients, which do nothing until used


def test_the_json_keeps_the_order_the_code_gives_it(app):
    keys = list(json.loads(get(app).get_data(as_text=True))["analysis"])
    assert keys[:3] == ["generated_at", "spot", "market_status"]
