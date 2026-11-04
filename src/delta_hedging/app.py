"""The Flask application factory.

Unlike the first version, creating the app does not log in to IG (that used to happen when ``routes.py`` was
imported), there is no CORS, no debug mode, and every ``/api`` route needs the bearer token.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from flask import Flask, jsonify

from .config import Settings
from .ig import IGClient, IGError
from .service import HedgingError, HedgingService

logger = logging.getLogger(__name__)

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)


@dataclass(slots=True)
class Services:
    settings: Settings
    hedging: HedgingService


def build_service(settings: Settings) -> HedgingService:
    """The service on two IG sessions, one per account. Nothing is sent to IG until the first request."""
    return HedgingService(
        settings, IGClient(settings, settings.ig_options_account), IGClient(settings, settings.ig_cfd_account)
    )


def create_app(settings: Settings | None = None, service: HedgingService | None = None) -> Flask:
    """`settings` default to the environment; tests pass settings and a `service` on a fake IG."""
    settings = settings or Settings.from_env()
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=16 * 1024)  # a request is a few fields
    app.json.sort_keys = False
    app.extensions["delta_hedging"] = Services(settings=settings, hedging=service or build_service(settings))

    from .api import bp

    app.register_blueprint(bp)

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Cross-Origin-Resource-Policy", "same-origin"
        )  # no CORS: other sites cannot read it
        if not response.headers.get("Cache-Control"):
            response.headers["Cache-Control"] = "no-store"  # positions and orders are not for caches
        return response

    @app.errorhandler(HedgingError)
    def hedging_error(error: HedgingError):
        from .api import plan_json

        body: dict = {"error": str(error)}
        headers = {}
        if error.plan is not None:
            body["plan"] = plan_json(error.plan)
        if hasattr(error, "retry_after"):
            headers["Retry-After"] = str(int(error.retry_after) + 1)
        return jsonify(body), error.status, headers

    @app.errorhandler(IGError)
    def ig_error(error: IGError):
        if error.kind == "rate_limit":
            return jsonify(error=str(error)), 429, {"Retry-After": "60"}
        if error.kind == "rejected":
            return jsonify(error=str(error)), 422
        return jsonify(error=str(error)), 502

    for code, message in (
        (400, "Bad request"),
        (404, "Not found"),
        (405, "Method not allowed"),
        (413, "Request too large"),
    ):
        app.register_error_handler(code, lambda error, code=code, message=message: (jsonify(error=message), code))

    @app.errorhandler(Exception)
    def unexpected(error: Exception):
        logger.exception("Unhandled error")  # the stack goes to the log, never to the caller
        return jsonify(error="Internal error."), 500

    return app
