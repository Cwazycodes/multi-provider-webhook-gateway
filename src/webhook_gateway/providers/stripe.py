"""Stripe: verifying Stripe-Signature, and normalising payment intent and charge events."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated

from pydantic import Field, JsonValue, StringConstraints

from webhook_gateway.common.envelope import Envelope, EventType, NonEmptyStr, Provider
from webhook_gateway.common.errors import SignatureError
from webhook_gateway.providers.normalising import (
    PayloadModel,
    Receipt,
    UnixSeconds,
    build_envelope,
    from_unix,
    parse,
    unhandled,
)
from webhook_gateway.providers.signatures import SignedRequest, signed_by_any

HEADER = "Stripe-Signature"

# Kept with the raw body on the queue and in the archive (lowercased). Never a signature.
ARCHIVED_HEADERS = frozenset({"content-type", "user-agent"})

# ASCII digits only: int() would also take "1_763", "+1763", " 1763" and other scripts' digits.
_SECONDS = re.compile(r"[0-9]+")
PAYMENT_INTENT_TYPES = {
    "payment_intent.succeeded": EventType.PAYMENT_SUCCEEDED,
    "payment_intent.payment_failed": EventType.PAYMENT_FAILED,
}

MinorUnits = Annotated[int, Field(ge=0)]  # 1999 is £19.99
Currency = Annotated[str, StringConstraints(pattern=r"^[a-z]{3}$")]


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


class StripeEvent(PayloadModel):
    """What every Stripe event has, handled or not."""

    id: NonEmptyStr
    type: NonEmptyStr
    created: UnixSeconds


class PaymentError(PayloadModel):
    code: NonEmptyStr


class PaymentIntent(PayloadModel):
    id: NonEmptyStr
    amount: MinorUnits
    currency: Currency
    status: NonEmptyStr
    last_payment_error: PaymentError | None = None


class PaymentIntentData(PayloadModel):
    object: PaymentIntent


class PaymentIntentEvent(StripeEvent):
    data: PaymentIntentData


class Charge(PayloadModel):
    id: NonEmptyStr
    amount_refunded: MinorUnits
    currency: Currency
    payment_intent: NonEmptyStr  # the subject: a refund is about the payment


class ChargeData(PayloadModel):
    object: Charge


class ChargeEvent(StripeEvent):
    data: ChargeData


def normalise(request: SignedRequest, receipt: Receipt) -> Envelope:
    event = parse(StripeEvent, request.body)
    occurred_at = from_unix(event.created)
    if event.type in PAYMENT_INTENT_TYPES:
        intent = parse(PaymentIntentEvent, request.body).data.object
        data: dict[str, JsonValue] = {
            "amount": intent.amount,
            "currency": intent.currency.upper(),
            "status": intent.status,
        }
        if intent.last_payment_error is not None:
            data["failure_code"] = intent.last_payment_error.code
        return build_envelope(
            Provider.STRIPE,
            receipt,
            provider_event_id=event.id,
            event_type=PAYMENT_INTENT_TYPES[event.type],
            subject_id=f"stripe:{intent.id}",
            occurred_at=occurred_at,
            data=data,
        )
    if event.type == "charge.refunded":
        charge = parse(ChargeEvent, request.body).data.object
        return build_envelope(
            Provider.STRIPE,
            receipt,
            provider_event_id=event.id,
            event_type=EventType.PAYMENT_REFUNDED,
            subject_id=f"stripe:{charge.payment_intent}",
            occurred_at=occurred_at,
            data={
                "charge": charge.id,
                "amount_refunded": charge.amount_refunded,
                "currency": charge.currency.upper(),
            },
        )
    return unhandled(
        Provider.STRIPE,
        receipt,
        provider_event_id=event.id,
        provider_type=event.type,
        occurred_at=occurred_at,
    )
