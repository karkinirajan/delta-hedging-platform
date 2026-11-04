import logging

import httpx
import pytest

from delta_hedging.hedging import Direction
from delta_hedging.ig import TOKEN_SECONDS, IGClient, IGError, new_deal_reference

from .fake_ig import API_KEY, CFD_ACCOUNT, OPTIONS_ACCOUNT, PASSWORD, UNDERLYING, USERNAME, cfd, error, option


def test_logging_in_sends_the_documented_request(options_client, ig):
    options_client.positions()
    (login,) = ig.calls("login")
    assert login.headers["X-IG-API-KEY"] == API_KEY and login.headers["Version"] == "2"
    assert login.read() and b'"identifier"' in login.content and USERNAME.encode() in login.content
    assert b'"encryptedPassword":false' in login.content.replace(b" ", b"")
    assert "CST" not in login.headers  # no tokens on the login itself


def test_the_session_is_used_for_later_requests_and_login_happens_once(options_client, ig):
    options_client.positions()
    options_client.positions()
    options_client.quote(UNDERLYING)
    assert ig.logins == 1
    request = ig.calls("positions")[0]
    assert request.headers["CST"].startswith("cst-0-") and request.headers["X-SECURITY-TOKEN"].startswith("xst-0-")
    assert request.headers["Version"] == "2" and ig.calls("market")[0].headers["Version"] == "3"


def test_it_switches_to_its_own_account_only_when_the_login_is_on_another(options_client, cfd_client, ig):
    options_client.positions()
    assert ig.calls("switch") == []  # the login already was on the options account
    cfd_client.positions()  # a new session, on the CFD account
    (switch,) = ig.calls("switch")
    assert switch.headers["Version"] == "1" and b"CFD456" in switch.content


def test_each_client_reads_only_its_own_account(options_client, cfd_client, ig):
    ig.positions[CFD_ACCOUNT] = [cfd()]
    assert [p["position"]["dealId"] for p in options_client.positions()] == ["DIOPT001"]
    assert [p["position"]["dealId"] for p in cfd_client.positions()] == ["DICFD001"]
    assert [p["position"]["dealId"] for p in options_client.positions()] == [
        "DIOPT001"
    ]  # not left on the other account


def test_switching_to_the_account_already_current_is_not_an_error(settings, ig, clock):
    ig.current_account = CFD_ACCOUNT
    ig.fail["switch"] = error(400, "error.switch.accountId-must-be-different")  # IG's answer if asked anyway
    client = IGClient(settings, CFD_ACCOUNT, ig.client(), clock)
    assert client.positions() == []


def test_a_failed_switch_is_an_auth_error(settings, ig, clock):
    ig.fail["switch"] = error(400, "error.switch.invalid-accountId")
    with pytest.raises(IGError) as caught:
        IGClient(settings, CFD_ACCOUNT, ig.client(), clock).positions()
    assert caught.value.kind == "auth" and "error.switch.invalid-accountId" in str(caught.value)


def test_a_refused_login_is_an_auth_error_that_does_not_leak_credentials(options_client, ig):
    ig.login_status = 401
    with pytest.raises(IGError) as caught:
        options_client.positions()
    text = str(caught.value)
    assert caught.value.kind == "auth" and "refused the login" in text
    for secret in (API_KEY, USERNAME, PASSWORD):
        assert secret not in text


def test_a_login_answer_without_tokens_is_a_bad_response(settings, clock):
    def no_tokens(request):
        return httpx.Response(200, json={"currentAccountId": OPTIONS_ACCOUNT})

    client = IGClient(settings, OPTIONS_ACCOUNT, httpx.Client(transport=httpx.MockTransport(no_tokens)), clock)
    with pytest.raises(IGError) as caught:
        client.positions()
    assert caught.value.kind == "bad_response"


def test_stale_tokens_are_refreshed_once_and_the_request_repeated(options_client, ig):
    options_client.positions()
    ig.expire_tokens()
    assert options_client.positions()  # a 401, a new login, and the answer
    assert ig.logins == 2 and len(ig.calls("positions")) == 3


def test_the_tokens_are_renewed_before_their_time_is_up(options_client, ig, clock):
    options_client.positions()
    clock.advance(TOKEN_SECONDS - 1)
    options_client.positions()
    assert ig.logins == 1
    clock.advance(2)
    options_client.positions()
    assert ig.logins == 2


def test_a_persistent_401_gives_up_after_one_retry(options_client, ig):
    ig.fail["positions"] = error(401, "error.security.client-token-invalid")
    with pytest.raises(IGError) as caught:
        options_client.positions()
    assert caught.value.kind == "auth" and ig.logins == 2


# -- rate limits and errors
@pytest.mark.parametrize(
    "code",
    [
        "error.public-api.exceeded-api-key-allowance",
        "error.public-api.exceeded-account-allowance",
        "error.public-api.exceeded-account-trading-allowance",
    ],
)
def test_a_403_with_an_allowance_code_is_a_rate_limit_reported_without_sleeping(options_client, ig, code):
    """The first version looked for HTTP 429 and slept for a minute inside the request."""
    ig.fail["positions"] = error(403, code)
    with pytest.raises(IGError) as caught:
        options_client.positions()
    assert caught.value.kind == "rate_limit" and caught.value.code == code


def test_other_errors_name_the_status_and_a_safe_code_only(options_client, ig):
    ig.fail["positions"] = httpx.Response(
        500, json={"errorCode": "error.service.unavailable", "message": "SECRET detail"}
    )
    with pytest.raises(IGError) as caught:
        options_client.positions()
    assert "HTTP 500" in str(caught.value) and "SECRET" not in str(caught.value) and caught.value.kind == "upstream"


def test_an_error_code_that_is_not_a_plain_identifier_is_dropped(options_client, ig):
    ig.fail["positions"] = httpx.Response(500, json={"errorCode": "<script>alert(1)</script> SECRET"})
    with pytest.raises(IGError) as caught:
        options_client.positions()
    assert "SECRET" not in str(caught.value) and "<script>" not in str(caught.value)


def test_a_network_failure_names_only_its_kind(settings, clock):
    def boom(request):
        raise httpx.ConnectError(f"cannot reach {request.url} with {request.headers.get('X-IG-API-KEY')}")

    client = IGClient(settings, OPTIONS_ACCOUNT, httpx.Client(transport=httpx.MockTransport(boom)), clock)
    with pytest.raises(IGError) as caught:
        client.positions()
    assert (
        "ConnectError" in str(caught.value)
        and API_KEY not in str(caught.value)
        and "example.test" not in str(caught.value)
    )


@pytest.mark.parametrize(
    "body",
    [
        httpx.Response(200, text="<html>"),
        httpx.Response(200, json=["a"]),
        httpx.Response(200, json={"positions": "no"}),
    ],
)
def test_unexpected_answers_are_bad_responses(options_client, ig, body):
    ig.fail["positions"] = body
    with pytest.raises(IGError) as caught:
        options_client.positions()
    assert caught.value.kind == "bad_response"


def test_secrets_never_reach_the_log(options_client, ig, caplog):
    ig.fail["positions"] = error(403, "error.public-api.exceeded-account-allowance")
    with caplog.at_level(logging.DEBUG), pytest.raises(IGError):
        options_client.positions()
    ig.fail.clear()
    ig.fail["market"] = httpx.Response(500, json={"errorCode": "error.service.x"})
    with caplog.at_level(logging.DEBUG), pytest.raises(IGError):
        options_client.quote(UNDERLYING)
    for secret in (API_KEY, USERNAME, PASSWORD, "cst-", "xst-"):
        assert secret not in caplog.text
    assert "HTTP Request" not in caplog.text  # httpx's own request log is kept off
    assert logging.getLogger("httpx").level >= logging.WARNING


# -- quotes
def test_a_quote_has_bid_offer_mid_and_status(options_client):
    q = options_client.quote(UNDERLYING)
    assert (q.bid, q.offer, q.mid, q.status, q.tradeable) == (5799.0, 5801.0, 5800.0, "TRADEABLE", True)


def test_a_closed_market_is_not_tradeable(options_client, ig):
    ig.quotes[UNDERLYING]["marketStatus"] = "CLOSED"
    assert options_client.quote(UNDERLYING).tradeable is False


@pytest.mark.parametrize(
    "snapshot",
    [
        {},
        {"bid": 5799.0},
        {"bid": 0, "offer": 5801.0},
        {"bid": "x", "offer": "y"},
        {"bid": None, "offer": None},
    ],
)
def test_a_quote_without_a_usable_bid_and_offer_is_a_bad_response(options_client, ig, snapshot):
    ig.quotes[UNDERLYING] = snapshot
    with pytest.raises(IGError) as caught:
        options_client.quote(UNDERLYING)
    assert caught.value.kind == "bad_response"


# -- orders
def test_an_order_has_the_documented_fields_and_our_own_reference(cfd_client, ig):
    ref = new_deal_reference()
    assert cfd_client.open_position(UNDERLYING, Direction.SELL, 6.368, "GBP", ref) == ref
    (order,) = ig.orders
    assert order["account"] == CFD_ACCOUNT  # placed on the CFD account
    assert order["epic"] == UNDERLYING and order["direction"] == "SELL" and order["size"] == 6.37
    assert (order["orderType"], order["currencyCode"], order["expiry"]) == ("MARKET", "GBP", "-")
    assert order["forceOpen"] is True and order["guaranteedStop"] is False and order["dealReference"] == ref
    assert ig.calls("order")[0].headers["Version"] == "2"


def test_deal_references_match_ig_pattern_and_are_unique():
    refs = {new_deal_reference() for _ in range(200)}
    assert len(refs) == 200 and all(1 <= len(r) <= 30 and r.replace("_", "").replace("-", "").isalnum() for r in refs)


@pytest.mark.parametrize("bad", ["", "a b", "../x", "x" * 31, "a/b", "ref;drop"])
def test_a_deal_reference_is_checked_before_it_reaches_a_url_or_a_body(cfd_client, ig, bad):
    with pytest.raises(ValueError, match="deal reference"):
        cfd_client.confirm(bad)
    with pytest.raises(ValueError, match="deal reference"):
        cfd_client.open_position(UNDERLYING, Direction.BUY, 1.0, "GBP", bad)
    assert ig.requests == []


@pytest.mark.parametrize("size", [0, -1, float("nan")])
def test_a_size_that_is_not_positive_is_refused(cfd_client, ig, size):
    with pytest.raises(ValueError, match="size must be positive"):
        cfd_client.open_position(UNDERLYING, Direction.BUY, size, "GBP", "ref1")
    assert ig.orders == []


def test_an_order_is_confirmed_as_accepted(cfd_client):
    ref = cfd_client.open_position(UNDERLYING, Direction.BUY, 2.0, "GBP", new_deal_reference())
    c = cfd_client.confirm(ref)
    assert c.accepted and c.status == "ACCEPTED" and c.reason == "SUCCESS" and c.deal_id.startswith("DEAL")


def test_a_rejected_order_is_reported_as_rejected_not_as_done(cfd_client, ig):
    """The first version recorded a hedge as made as soon as IG returned a reference."""
    ig.deal_status, ig.reason = "REJECTED", "INSUFFICIENT_FUNDS"
    ref = cfd_client.open_position(UNDERLYING, Direction.BUY, 2.0, "GBP", new_deal_reference())
    c = cfd_client.confirm(ref)
    assert not c.accepted and c.status == "REJECTED" and c.reason == "INSUFFICIENT_FUNDS"


def test_a_missing_confirmation_is_unknown_not_an_error(cfd_client, ig):
    ig.confirm_missing = True
    ref = cfd_client.open_position(UNDERLYING, Direction.BUY, 2.0, "GBP", new_deal_reference())
    c = cfd_client.confirm(ref)
    assert c.status == "UNKNOWN" and not c.accepted


def test_a_confirmation_with_another_status_is_a_bad_response(cfd_client, ig):
    ig.deal_status = "PENDING"
    ref = cfd_client.open_position(UNDERLYING, Direction.BUY, 2.0, "GBP", new_deal_reference())
    with pytest.raises(IGError) as caught:
        cfd_client.confirm(ref)
    assert caught.value.kind == "bad_response"


def test_an_order_igs_rejects_at_once_is_a_rejection_with_its_code(cfd_client, ig):
    ig.fail["order"] = httpx.Response(400, json={"errorCode": "error.trading.otc.market-orders.not-supported"})
    with pytest.raises(IGError) as caught:
        cfd_client.open_position(UNDERLYING, Direction.BUY, 1.0, "GBP", "ref1")
    assert caught.value.kind == "rejected" and caught.value.code.endswith("market-orders.not-supported")
    assert caught.value.outcome_unknown is False


def test_an_order_whose_answer_never_arrives_is_flagged_as_possibly_placed(cfd_client, ig):
    ig.drop_order_answer = True
    with pytest.raises(IGError) as caught:
        cfd_client.open_position(UNDERLYING, Direction.BUY, 1.0, "GBP", "ref-lost")
    assert caught.value.outcome_unknown is True and len(ig.orders) == 1  # it did reach IG


def test_an_order_answer_with_another_reference_is_flagged_as_possibly_placed(cfd_client, ig):
    ig.fail["order"] = httpx.Response(200, json={"dealReference": "someone-else"})
    with pytest.raises(IGError) as caught:
        cfd_client.open_position(UNDERLYING, Direction.BUY, 1.0, "GBP", "ref1")
    assert caught.value.kind == "bad_response" and caught.value.outcome_unknown is True


def test_a_rate_limited_order_is_a_rate_limit_and_was_not_placed(cfd_client, ig):
    ig.fail["order"] = error(403, "error.public-api.exceeded-account-trading-allowance")
    with pytest.raises(IGError) as caught:
        cfd_client.open_position(UNDERLYING, Direction.BUY, 1.0, "GBP", "ref1")
    assert caught.value.kind == "rate_limit" and caught.value.outcome_unknown is False


def test_options_are_listed_with_their_fields_untouched(options_client, ig):
    ig.positions[OPTIONS_ACCOUNT] = [
        option(),
        option(deal_id="DIOPT002", name="US 500 5700 PUT", direction="SELL"),
    ]
    rows = options_client.positions()
    assert [r["position"]["dealId"] for r in rows] == ["DIOPT001", "DIOPT002"] and rows[1]["position"][
        "direction"
    ] == "SELL"


def test_requests_have_a_timeout(settings):
    client = IGClient(settings, OPTIONS_ACCOUNT)
    assert client._http.timeout.read == settings.request_timeout
