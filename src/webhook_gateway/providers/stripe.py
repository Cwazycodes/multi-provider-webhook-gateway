"""Stripe: Stripe-Signature is t=<seconds>,v1=<hex HMAC-SHA256 of "<t>.<raw body>">."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from webhook_gateway.common.errors import SignatureError
from webhook_gateway.providers.signatures import SignedRequest, signed_by_any

HEADER = "Stripe-Signature"
# ASCII digits only: int() would also take "1_763", "+1763", " 1763" and other scripts' digits.
_SECONDS = re.compile(r"[0-9]+")


@dataclass(frozen=True)
class StripeVerifier:
    tolerance: timedelta = timedelta(minutes=5)

    def verify(self, request: SignedRequest, secrets: Sequence[str], now: datetime) -> None:
        timestamp, signatures = _parse(request.header(HEADER))
        # The timestamp exactly as sent, not re-rendered from an int.
        signed_payload = timestamp.encode() + b"." + request.body
        if not signed_by_any(secrets, signed_payload, signatures, encode=bytes.hex):
            raise SignatureError(f"{HEADER} has no v1 matching any of the secrets")
        # Only once the signature proves Stripe chose t: is it recent, in either direction?
        skew = abs(int(now.timestamp()) - int(timestamp))
        if skew > self.tolerance.total_seconds():
            raise SignatureError(f"{HEADER} was signed {skew} s from now")


def _parse(header: str) -> tuple[str, list[str]]:
    """The one t and every v1. v0 and Stripe's debugging entries are ignored."""
    timestamps: list[str] = []
    signatures: list[str] = []
    for entry in header.split(","):
        key, _, value = entry.partition("=")
        if key == "t":
            timestamps.append(value)
        elif key == "v1":
            signatures.append(value)
    if len(timestamps) != 1 or not _SECONDS.fullmatch(timestamps[0]) or not signatures:
        raise SignatureError(f"{HEADER} isn't t=<seconds>,v1=<hex>")
    return timestamps[0], signatures
