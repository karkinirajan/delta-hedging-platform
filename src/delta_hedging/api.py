"""The JSON API and the dashboard page. Every ``/api`` route needs ``Authorization: Bearer <API_TOKEN>``.

The first version had no authentication at all, allowed every origin (``CORS(app)``) and bound ``0.0.0.0`` with the
debugger on, on an endpoint that places orders: any web page open in the operator's browser could have placed one.
"""

from __future__ import annotations

import hmac
import time
from collections import defaultdict, deque

from flask import Blueprint, current_app, jsonify, render_template, request

from .hedging import HedgePlan
from .positions import Unreadable
from .service import Analysis, HedgeResult, HedgingService, PositionAnalysis

bp = Blueprint("api", __name__)

MAX_FAILURES, FAILURE_WINDOW = 10, 60.0  # failed token checks per client address, per minute
_failures: dict[str, deque] = defaultdict(deque)


def _services():
    return current_app.extensions["delta_hedging"]


def hedging() -> HedgingService:
    return _services().hedging


def _throttled(client: str) -> float:
    """Seconds to wait if `client` has failed the token check too often lately, else 0."""
    now = time.monotonic()
    attempts = _failures[client]
    while attempts and now - attempts[0] > FAILURE_WINDOW:
        attempts.popleft()
    return FAILURE_WINDOW - (now - attempts[0]) if len(attempts) >= MAX_FAILURES else 0.0


@bp.before_app_request
def require_token():
    if not request.path.startswith("/api/"):
        return None
    client = request.remote_addr or "?"
    wait = _throttled(client)
    if wait > 0:
        return (
            jsonify(error="Too many failed attempts. Wait a minute."),
            429,
            {"Retry-After": str(int(wait) + 1)},
        )
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    expected = _services().settings.api_token
    if scheme.lower() == "bearer" and expected and hmac.compare_digest(token.strip().encode(), expected.encode()):
        return None
    _failures[client].append(time.monotonic())
    return jsonify(error="A valid bearer token is required."), 401, {"WWW-Authenticate": "Bearer"}


# -- serialisation
def plan_json(plan: HedgePlan) -> dict:
    return {
        "action": plan.action,
        "direction": plan.direction.value if plan.direction else None,
        "size": plan.size,
        "capped": plan.capped,
        "option_delta": plan.option_delta,
        "held_units": plan.held_units,
        "net_delta": plan.net_delta,
        "remaining_net_delta": plan.remaining_net_delta,
        "reason": plan.reason,
    }


def _position_json(a: PositionAnalysis) -> dict:
    p, g = a.position, a.greeks
    return {
        "deal_id": p.deal_id,
        "name": p.name,
        "epic": p.epic,
        "direction": p.direction.value,
        "option_type": p.option_type.value,
        "strike": p.strike,
        "size": p.size,
        "contract_size": p.contract_size,
        "expiry": p.expiry.isoformat(),
        "years": a.years,
        "spot": a.spot,
        "market_price": p.market_price,
        "volatility": a.volatility,
        "volatility_source": a.volatility_source,
        "greeks": {
            "price": g.price,
            "delta": g.delta,
            "gamma": g.gamma,
            "vega": g.vega,
            "theta": g.theta,
            "rho": g.rho,
        },
        "delta_units": a.delta_units,
    }


def analysis_json(a: Analysis) -> dict:
    return {
        "generated_at": a.generated_at.isoformat(),
        "spot": a.spot,
        "market_status": a.market_status,
        "positions": [_position_json(p) for p in a.positions],
        "unreadable": [_unreadable_json(u) for u in a.unreadable],
        "expired": [{"deal_id": e.deal_id, "name": e.name, "expiry": e.expiry.isoformat()} for e in a.expired],
        "hedge_positions": list(a.hedge_positions),
        "option_delta": a.option_delta,
        "held_units": a.held_units,
        "plan": plan_json(a.plan),
        "warnings": list(a.warnings),
    }


def _unreadable_json(u: Unreadable) -> dict:
    return {"deal_id": u.deal_id, "name": u.name, "reason": u.reason}


def _result_json(r: HedgeResult) -> dict:
    return {
        "executed": r.executed,
        "plan": plan_json(r.plan),
        "outcome": r.outcome,
        "reason": r.reason,
        "deal_reference": r.deal_reference,
        "deal_id": r.deal_id,
    }


def settings_json() -> dict:
    s = _services().settings
    return {
        "environment": "demo",
        "hedging_enabled": s.hedging_enabled,
        "underlying_epic": s.underlying_epic,
        "order_currency": s.order_currency,
        "delta_threshold": s.delta_threshold,
        "min_hedge_size": s.min_hedge_size,
        "max_hedge_size": s.max_hedge_size,
        "plan_tolerance": s.plan_tolerance,
        "cooldown_seconds": s.cooldown_seconds,
        "rate": s.rate,
        "default_volatility": s.default_volatility,
    }


# -- routes
@bp.get("/")
def dashboard():
    return render_template("dashboard.html")


@bp.get("/healthz")
def healthz():
    return jsonify(status="ok")


@bp.get("/api/analysis")
def analysis():
    return jsonify(
        analysis=analysis_json(hedging().analyse(fresh=request.args.get("fresh") == "1")),
        settings=settings_json(),
    )


@bp.post("/api/hedge")
def hedge():
    """Without ``confirm: true`` this is a preview. With it, and only when HEDGING_ENABLED is on, the order is sent."""
    body = request.get_json(silent=True) if request.is_json else {}
    if not isinstance(body, dict):
        return jsonify(error="The body must be a JSON object."), 400
    result = hedging().hedge(
        confirm=body.get("confirm"),
        expected_direction=body.get("expected_direction"),
        expected_size=body.get("expected_size"),
    )
    return jsonify(_result_json(result))


@bp.get("/api/hedge/history")
def history():
    return jsonify(history=hedging().history())
