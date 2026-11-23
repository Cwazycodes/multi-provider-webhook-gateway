"""Signed synthetic deliveries, with faults injected at chosen rates.

Everything random comes from one Random(seed), and every time from `start`, so
the same arguments always give the same deliveries.
"""

import base64
import math
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from random import Random
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from webhook_gateway.common.envelope import Provider
from webhook_gateway.synthetic import github, shopify, stripe
from webhook_gateway.synthetic.fakes import FakeEvent, FakeProvider

FAKES: Mapping[Provider, FakeProvider] = {
    Provider.GITHUB: github.FAKE,
    Provider.SHOPIFY: shopify.FAKE,
    Provider.STRIPE: stripe.FAKE,
}


class Fault(StrEnum):
    BAD_SIGNATURE = "bad_signature"  # signed with the wrong secret
    STALE_TIMESTAMP = "stale_timestamp"  # validly signed, but over 5 minutes before sending
    MALFORMED = "malformed"  # validly signed, but the body is cut short: not JSON
    SCHEMA_INVALID = "schema_invalid"  # validly signed JSON missing a required field
    DUPLICATE = "duplicate"  # an earlier event delivered again


Rate = Annotated[float, Field(ge=0, le=1)]


class FaultRates(BaseModel):
    """The share of deliveries given each fault. A delivery has at most one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    bad_signature: Rate = 0.0
    stale_timestamp: Rate = 0.0
    malformed: Rate = 0.0
    schema_invalid: Rate = 0.0
    duplicate: Rate = 0.0

    @model_validator(mode="after")
    def _add_up_to_at_most_one(self) -> Self:
        if math.fsum(self.model_dump().values()) > 1:
            raise ValueError("fault rates must add up to at most 1")
        return self


NO_FAULTS = FaultRates()


@dataclass(frozen=True)
class Delivery:
    """One request as the provider sends it, plus what the generator knows about it."""

    provider: Provider
    headers: Mapping[str, str]
    body: str
    sent_at: datetime
    request_id: str  # what API Gateway would assign: ingest's correlation_id
    provider_event_id: str
    fault: Fault | None


def generate(
    provider: Provider,
    secret: str,
    *,
    count: int,
    seed: int,
    start: datetime,
    interval: timedelta = timedelta(seconds=1),
    faults: FaultRates = NO_FAULTS,
) -> Iterator[Delivery]:
    """`count` deliveries, sent `interval` apart from `start`, made one at a time."""
    fake = FAKES[provider]
    if not secret:
        raise ValueError("the signing secret must not be empty")
    if start.utcoffset() is None:
        raise ValueError("start must be timezone-aware")
    if faults.stale_timestamp and not fake.signs_timestamp:
        raise ValueError(f"{provider} signatures carry no timestamp, so none can be stale")

    def deliveries() -> Iterator[Delivery]:
        rng = Random(seed)
        delivered: list[FakeEvent] = []
        for index in range(count):
            sent_at = start + index * interval
            fault = _draw(rng, faults)
            if fault is Fault.DUPLICATE and not delivered:
                fault = None  # nothing to deliver again yet
            event = rng.choice(delivered) if fault is Fault.DUPLICATE else fake.event(rng, sent_at)

            payload = event.payload
            if fault is Fault.SCHEMA_INVALID:
                payload = {k: v for k, v in payload.items() if k != fake.required_field}
            body = fake.render(payload)
            if fault is Fault.MALFORMED:
                body = body[: len(body) // 2]
            key = rng.randbytes(32).hex() if fault is Fault.BAD_SIGNATURE else secret
            signed_at = sent_at
            if fault is Fault.STALE_TIMESTAMP:
                signed_at -= timedelta(seconds=rng.randint(301, 3600))

            if fault is None:
                delivered.append(event)
            yield Delivery(
                provider=provider,
                headers={**event.headers, **fake.sign(key, body, signed_at)},
                body=body,
                sent_at=sent_at,
                request_id=base64.b64encode(rng.randbytes(11)).decode(),
                provider_event_id=event.provider_event_id,
                fault=fault,
            )

    return deliveries()


def _draw(rng: Random, faults: FaultRates) -> Fault | None:
    """One roll per delivery: each fault owns a slice of [0, 1) as wide as its rate."""
    roll = rng.random()
    for fault in Fault:
        rate: float = getattr(faults, fault)
        if roll < rate:
            return fault
        roll -= rate
    return None
