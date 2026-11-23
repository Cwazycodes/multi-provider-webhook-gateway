"""What each provider's fake supplies, and the helpers the fakes share."""

import hmac
import string
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from random import Random
from typing import Any
from uuid import UUID

Payload = dict[str, Any]


@dataclass(frozen=True)
class FakeEvent:
    """One provider event before it is delivered: no body bytes and no signature yet."""

    provider_event_id: str  # what ingest will build the idempotency key from
    headers: Mapping[str, str]  # the provider's own headers, in its casing
    payload: Payload


@dataclass(frozen=True)
class FakeProvider:
    """One provider, as the generator uses it."""

    event: Callable[[Random, datetime], FakeEvent]  # a realistic event, sent at that time
    render: Callable[[Payload], str]  # the body, laid out as this provider lays it out
    sign: Callable[[str, str, datetime], Mapping[str, str]]  # (secret, body, signed at)
    required_field: str  # the top-level field a schema-invalid body loses
    signs_timestamp: bool  # only then can a signature's timestamp be stale


def hmac_sha256(secret: str, message: str) -> bytes:
    return hmac.new(secret.encode(), message.encode(), sha256).digest()


def fake_uuid(rng: Random) -> str:
    return str(UUID(int=rng.getrandbits(128), version=4))


def fake_sha(rng: Random) -> str:
    """A 40-character hex ID, like a git commit SHA."""
    return rng.randbytes(20).hex()


_ALPHANUMERIC = string.ascii_letters + string.digits


def fake_id(rng: Random, prefix: str, length: int = 24) -> str:
    """A Stripe-style ID, such as evt_ followed by 24 letters and digits."""
    return prefix + "".join(rng.choices(_ALPHANUMERIC, k=length))
