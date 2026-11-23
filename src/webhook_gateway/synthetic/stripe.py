"""Stripe-style fakes: payment intent, charge and customer events, signed as Stripe signs them."""

import json
from datetime import datetime, timedelta
from hashlib import sha256
from random import Random

from webhook_gateway.synthetic.fakes import (
    FakeEvent,
    FakeProvider,
    Payload,
    fake_id,
    fake_uuid,
    hmac_sha256,
)

# customer.created isn't in the gateway's vocabulary.
EVENT_TYPES = (
    "payment_intent.succeeded",
    "payment_intent.payment_failed",
    "charge.refunded",
    "customer.created",
)
NAMES = ("Ada Lovelace", "Grace Hopper", "Alan Turing", "Katherine Johnson")
HEADERS = {
    "Content-Type": "application/json; charset=utf-8",
    "User-Agent": "Stripe/1.0 (+https://stripe.com/docs/webhooks)",
}


def event(rng: Random, at: datetime) -> FakeEvent:
    event_type = rng.choice(EVENT_TYPES)
    # Stripe's times are whole Unix seconds.
    created = int((at - timedelta(seconds=rng.randint(0, 120))).timestamp())
    event_id = fake_id(rng, "evt_")
    payload = {
        "id": event_id,
        "object": "event",
        "api_version": "2025-09-30.clover",
        "created": created,
        "data": {"object": _object(rng, event_type, created)},
        "livemode": False,
        "pending_webhooks": 1,
        "request": {"id": fake_id(rng, "req_", 14), "idempotency_key": fake_uuid(rng)},
        "type": event_type,
    }
    return FakeEvent(provider_event_id=event_id, headers=HEADERS, payload=payload)


def _object(rng: Random, event_type: str, created: int) -> Payload:
    if event_type == "customer.created":
        return {
            "id": fake_id(rng, "cus_", 14),
            "object": "customer",
            "created": created,
            "name": rng.choice(NAMES),
        }
    amount = rng.randrange(500, 50_000)  # in the currency's smallest unit
    currency = rng.choice(("gbp", "eur", "usd"))
    intent_id = fake_id(rng, "pi_")
    if event_type == "charge.refunded":
        return {
            "id": fake_id(rng, "ch_"),
            "object": "charge",
            "amount": amount,
            "amount_refunded": amount,
            "currency": currency,
            "payment_intent": intent_id,
            "refunded": True,
            "status": "succeeded",
            "created": created,
        }
    succeeded = event_type == "payment_intent.succeeded"
    declined = {
        "code": "card_declined",
        "decline_code": "insufficient_funds",
        "message": "Your card has insufficient funds.",
    }
    return {
        "id": intent_id,
        "object": "payment_intent",
        "amount": amount,
        "amount_received": amount if succeeded else 0,
        "currency": currency,
        "customer": fake_id(rng, "cus_", 14),
        "latest_charge": fake_id(rng, "ch_"),
        "last_payment_error": None if succeeded else declined,
        "status": "succeeded" if succeeded else "requires_payment_method",
        "created": created,
    }


def render(payload: Payload) -> str:
    return json.dumps(payload, indent=2)


def sign(secret: str, body: str, signed_at: datetime) -> dict[str, str]:
    timestamp = int(signed_at.timestamp())
    signed_payload = f"{timestamp}.{body}"
    v1 = hmac_sha256(secret, signed_payload).hex()
    # Sandbox deliveries also carry a v0 that isn't a real signature: verifiers must ignore it.
    v0 = sha256(signed_payload.encode()).hexdigest()
    return {"Stripe-Signature": f"t={timestamp},v1={v1},v0={v0}"}


FAKE = FakeProvider(
    event=event, render=render, sign=sign, required_field="id", signs_timestamp=True
)
