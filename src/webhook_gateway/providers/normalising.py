"""What every provider's normaliser shares: parsing, error paths and building the envelope."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from webhook_gateway.common.envelope import Envelope, EventType, Provider
from webhook_gateway.common.errors import PayloadError, SchemaError
from webhook_gateway.providers.signatures import SignedRequest

# Up to 9999-12-31T23:59:59Z: anything larger would crash datetime.fromtimestamp.
UnixSeconds = Annotated[int, Field(ge=0, le=253_402_300_799)]


class PayloadModel(BaseModel):
    """Base for provider payload models: JSON types exactly, unknown fields ignored.

    Model only what the gateway reads, so a field it never uses can't fail a request.
    On its own it accepts any JSON object.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="ignore")


@dataclass(frozen=True)
class Receipt:
    """What ingest knows about a request before it reads the payload."""

    event_id: UUID
    received_at: datetime
    correlation_id: str


Normaliser = Callable[[SignedRequest, Receipt], Envelope]


def parse[M: BaseModel](model: type[M], body: bytes) -> M:
    """`body` as `model`. PayloadError if it isn't JSON; SchemaError at the first bad field."""
    try:
        return model.model_validate_json(body)
    except ValidationError as error:
        first = error.errors()[0]
        if first["type"] == "json_invalid":
            raise PayloadError(first["msg"]) from None
        # from None: the ValidationError holds the rejected input, a piece of the raw body.
        raise SchemaError(".".join(str(part) for part in first["loc"]) or "(root)") from None


def required_header(request: SignedRequest, name: str) -> str:
    """A header the normaliser needs: SchemaError if it's missing or empty."""
    value = request.headers.get(name.lower(), "")
    if not value:
        raise SchemaError(f"headers.{name}")
    return value


def from_unix(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


def build_envelope(
    provider: Provider,
    receipt: Receipt,
    *,
    provider_event_id: str,
    event_type: EventType,
    subject_id: str,
    occurred_at: datetime | None,
    data: Mapping[str, JsonValue],
) -> Envelope:
    return Envelope(
        schema_version=1,
        event_id=receipt.event_id,
        idempotency_key=f"{provider}:{provider_event_id}",
        provider=provider,
        provider_event_id=provider_event_id,
        event_type=event_type,
        subject_id=subject_id,
        # Design note section 6: received_at when the payload has no event time.
        occurred_at=occurred_at or receipt.received_at,
        received_at=receipt.received_at,
        correlation_id=receipt.correlation_id,
        data=data,
    )


def unhandled(
    provider: Provider,
    receipt: Receipt,
    *,
    provider_event_id: str,
    provider_type: str,
    occurred_at: datetime | None = None,
) -> Envelope:
    """An event type the gateway doesn't act on, queued anyway so the provider won't retry it.

    Nothing is tracked for it, so its subject is the event itself.
    """
    return build_envelope(
        provider,
        receipt,
        provider_event_id=provider_event_id,
        event_type=EventType.UNHANDLED,
        subject_id=f"{provider}:event:{provider_event_id}",
        occurred_at=occurred_at,
        data={"provider_type": provider_type},
    )
