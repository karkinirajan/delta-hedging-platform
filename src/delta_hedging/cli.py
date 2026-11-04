"""Command line: ``delta-hedging run`` (a development server) and ``delta-hedging greeks`` (an offline calculator)."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from dotenv import find_dotenv, load_dotenv

from .config import ConfigError
from .pricing import OptionType, greeks


def _greeks(args: argparse.Namespace) -> int:
    years = args.years if args.years is not None else args.days / 365
    try:
        g = greeks(OptionType(args.type.upper()), args.spot, args.strike, years, args.volatility, args.rate)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        f"{args.type} spot={args.spot} strike={args.strike} years={years:.6f} "
        f"volatility={args.volatility} rate={args.rate}"
    )
    for name in ("price", "delta", "gamma", "vega", "theta", "rho"):
        print(f"{name:>6}: {getattr(g, name):.6f}")
    print("units: vega and rho per percentage point, theta per calendar day")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="delta-hedging")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="start a development server (use gunicorn in production)")
    run.add_argument("--host", default="127.0.0.1")
    run.add_argument("--port", type=int, default=8000)
    calc = sub.add_parser("greeks", help="price and greeks of one option (no IG access needed)")
    calc.add_argument("--type", choices=["call", "put"], required=True)
    calc.add_argument("--spot", type=float, required=True)
    calc.add_argument("--strike", type=float, required=True)
    when = calc.add_mutually_exclusive_group(required=True)
    when.add_argument("--years", type=float)
    when.add_argument("--days", type=float, help="calendar days to expiry")
    calc.add_argument("--volatility", type=float, required=True, help="annual, e.g. 0.2")
    calc.add_argument("--rate", type=float, default=0.0, help="risk-free rate, e.g. 0.05")
    args = parser.parse_args(argv)
    if args.command == "greeks":
        return _greeks(args)

    load_dotenv(find_dotenv(usecwd=True))  # a .env in the current directory; the real environment wins
    from .app import create_app

    try:
        app = create_app()
    except ConfigError as exc:
        print(f"error: {exc}. See .env.example.", file=sys.stderr)
        return 2
    # Never in debug mode, and on the loopback address unless told otherwise: the first version ran with debug=True
    # on 0.0.0.0, which exposes the Werkzeug console and an unauthenticated trading API to the whole network.
    app.run(host=args.host, port=args.port, debug=False)
    return 0
