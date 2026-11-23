"""Deterministic deliveries with faults injected at the rates asked for (WG-09)."""

import base64
import hmac
import json
import math
from collections import Counter
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

import pytest
from pydantic import ValidationError

from webhook_gateway.common.envelope import Provider
from webhook_gateway.synthetic.generator import Delivery, Fault, FaultRates, generate

START = datetime(2026, 11, 17, 10, 0, 0, tzinfo=UTC)
TEN_PERCENT_EACH = FaultRates(
    bad_signature=0.1, stale_timestamp=0.1, malformed=0.1, schema_invalid=0.1, duplicate=0.1
)
# The field a schema-invalid body is missing: one every event of that provider has.
REQUIRED_FIELD = {Provider.GITHUB: "repository", Provider.SHOPIFY: "id", Provider.STRIPE: "id"}


def rates_for(provider: Provider) -> FaultRates:
    """Ten percent of each fault; only Stripe signs a timestamp that can go stale."""
    if provider is Provider.STRIPE:
        return TEN_PERCENT_EACH
    return TEN_PERCENT_EACH.model_copy(update={"stale_timestamp": 0.0})


def deliveries(provider: Provider, secret: str, count: int, seed: int = 7) -> list[Delivery]:
    return list(
        generate(provider, secret, count=count, seed=seed, start=START, faults=rates_for(provider))
    )


def stripe_entries(header: str) -> dict[str, str]:
    return dict(entry.split("=", 1) for entry in header.split(","))


def signature_is_valid(delivery: Delivery, secret: str) -> bool:
    """An independent check, written the way each provider documents it."""
    key, body, headers = secret.encode(), delivery.body.encode(), delivery.headers
    match delivery.provider:
        case Provider.GITHUB:
            expected = "sha256=" + hmac.new(key, body, sha256).hexdigest()
            return hmac.compare_digest(headers["X-Hub-Signature-256"], expected)
        case Provider.SHOPIFY:
            expected = base64.b64encode(hmac.new(key, body, sha256).digest()).decode()
            return hmac.compare_digest(headers["X-Shopify-Hmac-Sha256"], expected)
        case Provider.STRIPE:
            entries = stripe_entries(headers["Stripe-Signature"])
            signed = f"{entries['t']}.".encode() + body
            return hmac.compare_digest(entries["v1"], hmac.new(key, signed, sha256).hexdigest())


def parsed(body: str) -> Any:
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


@pytest.mark.parametrize("provider", list(Provider))
def test_the_same_seed_gives_identical_deliveries(provider: Provider, signing_secret: str) -> None:
    first = deliveries(provider, signing_secret, 200, seed=7)

    assert deliveries(provider, signing_secret, 200, seed=7) == first
    assert deliveries(provider, signing_secret, 200, seed=8) != first


@pytest.mark.parametrize("provider", list(Provider))
def test_faults_are_injected_at_the_rates_asked_for(
    provider: Provider, signing_secret: str
) -> None:
    count = 4000
    rates = rates_for(provider)

    faults = Counter(delivery.fault for delivery in deliveries(provider, signing_secret, count))

    for fault in Fault:
        rate = getattr(rates, fault)
        # Four standard deviations of a binomial count: wide enough never to be
        # luck, narrow enough to catch a rate that is wired up wrongly.
        allowed = 4 * math.sqrt(count * rate * (1 - rate))
        assert abs(faults[fault] - count * rate) <= allowed, fault


@pytest.mark.parametrize("provider", list(Provider))
def test_every_fault_is_what_its_label_says(provider: Provider, signing_secret: str) -> None:
    seen: dict[str, str] = {}
    for delivery in deliveries(provider, signing_secret, 1000):
        fault, payload = delivery.fault, parsed(delivery.body)

        assert signature_is_valid(delivery, signing_secret) == (fault is not Fault.BAD_SIGNATURE)
        assert (payload is None) == (fault is Fault.MALFORMED)
        if payload is not None:
            missing = REQUIRED_FIELD[provider] not in payload
            assert missing == (fault is Fault.SCHEMA_INVALID)
        if provider is Provider.STRIPE:
            signed_at = int(stripe_entries(delivery.headers["Stripe-Signature"])["t"])
            age = delivery.sent_at.timestamp() - signed_at
            assert (age > 300) == (fault is Fault.STALE_TIMESTAMP)
        if fault is Fault.DUPLICATE:
            # A redelivery of an earlier event: the same ID and the same body.
            assert seen[delivery.provider_event_id] == delivery.body
        else:
            assert delivery.provider_event_id not in seen
            seen[delivery.provider_event_id] = delivery.body


def test_stripe_signs_each_delivery_when_it_is_sent(signing_secret: str) -> None:
    sent = list(
        generate(
            Provider.STRIPE,
            signing_secret,
            count=5,
            seed=1,
            start=START,
            interval=timedelta(seconds=30),
        )
    )

    assert [delivery.sent_at for delivery in sent] == [
        START + timedelta(seconds=30 * index) for index in range(5)
    ]
    for delivery in sent:
        signed_at = stripe_entries(delivery.headers["Stripe-Signature"])["t"]
        assert int(signed_at) == int(delivery.sent_at.timestamp())


def test_a_redelivered_stripe_event_is_signed_afresh(signing_secret: str) -> None:
    sent = list(
        generate(
            Provider.STRIPE,
            signing_secret,
            count=50,
            seed=2,
            start=START,
            faults=FaultRates(duplicate=0.5),
        )
    )
    first_delivery: dict[str, Delivery] = {}
    for delivery in sent:
        first_delivery.setdefault(delivery.provider_event_id, delivery)

    redeliveries = [delivery for delivery in sent if delivery.fault is Fault.DUPLICATE]
    assert redeliveries
    for delivery in redeliveries:
        original = first_delivery[delivery.provider_event_id]
        assert delivery.body == original.body
        assert delivery.headers["Stripe-Signature"] != original.headers["Stripe-Signature"]


def test_fault_rates_name_every_fault() -> None:
    assert set(FaultRates.model_fields) == {fault.value for fault in Fault}


@pytest.mark.parametrize(
    "rates",
    [
        {"malformed": -0.1},
        {"duplicate": 1.5},
        {"bad_signature": 0.5, "malformed": 0.3, "duplicate": 0.3},
    ],
)
def test_fault_rates_are_probabilities_that_add_up_to_at_most_one(rates: dict[str, float]) -> None:
    with pytest.raises(ValidationError):
        FaultRates(**rates)


@pytest.mark.parametrize("provider", [Provider.GITHUB, Provider.SHOPIFY])
def test_only_a_signed_timestamp_can_be_stale(provider: Provider, signing_secret: str) -> None:
    with pytest.raises(ValueError, match="no timestamp"):
        generate(
            provider,
            signing_secret,
            count=1,
            seed=1,
            start=START,
            faults=FaultRates(stale_timestamp=0.1),
        )


@pytest.mark.parametrize(
    ("secret", "start", "message"),
    [
        ("", START, "secret"),
        ("a-secret-for-this-test", datetime(2026, 11, 17, 10, 0, 0), "timezone"),
    ],
)
def test_needs_a_secret_and_an_aware_start(secret: str, start: datetime, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        generate(Provider.STRIPE, secret, count=1, seed=1, start=start)
