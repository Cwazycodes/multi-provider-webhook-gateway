"""Print signed synthetic webhooks as JSON Lines: `make generate` runs this.

The signing secret comes from the SIGNING_SECRET environment variable, never from
an argument, so it stays out of shell history and process listings.
"""

import argparse
import json
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from webhook_gateway.common.envelope import Provider
from webhook_gateway.synthetic.formats import apigw_event, http_request
from webhook_gateway.synthetic.generator import Delivery, Fault, FaultRates, generate

# --format: the key each line puts the delivery under, and how it is shown.
FORMATS: dict[str, tuple[str, Callable[[Delivery], dict[str, Any]]]] = {
    "apigw": ("event", apigw_event),
    "http": ("request", http_request),
}


def _aware(text: str) -> datetime:
    moment = datetime.fromisoformat(text)
    if moment.utcoffset() is None:
        raise argparse.ArgumentTypeError("needs a UTC offset, e.g. 2026-11-17T10:00:00Z")
    return moment.astimezone(UTC)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="make generate", description="Print signed synthetic webhooks as JSON Lines."
    )
    parser.add_argument("--provider", type=Provider, choices=list(Provider), required=True)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--start",
        type=_aware,
        help="when the first delivery is sent (default: now). Fix it, as well as --seed, "
        "for identical output: Stripe signs the time it sends.",
    )
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between deliveries")
    parser.add_argument("--format", choices=FORMATS, default="apigw")
    for fault in Fault:
        parser.add_argument(f"--{fault.replace('_', '-')}-rate", type=float, default=0.0)
    return parser


def main(argv: list[str]) -> None:
    parser = _parser()
    args = parser.parse_args(argv)
    secret = os.environ.get("SIGNING_SECRET", "")
    if not secret:
        parser.error("set SIGNING_SECRET first, e.g. export SIGNING_SECRET=$(openssl rand -hex 32)")
    try:
        faults = FaultRates(**{fault: getattr(args, f"{fault}_rate") for fault in Fault})
        deliveries = generate(
            args.provider,
            secret,
            count=args.count,
            seed=args.seed,
            start=args.start or datetime.now(UTC),
            interval=timedelta(seconds=args.interval),
            faults=faults,
        )
    except ValueError as error:  # pydantic's ValidationError is a ValueError too
        parser.error(str(error))
    key, shape = FORMATS[args.format]
    for delivery in deliveries:
        print(json.dumps({"fault": delivery.fault, key: shape(delivery)}))


if __name__ == "__main__":
    main(sys.argv[1:])
