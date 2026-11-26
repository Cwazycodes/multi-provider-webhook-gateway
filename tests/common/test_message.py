"""The SQS message between ingest and the processor (WG-12)."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from webhook_gateway.common.envelope import Envelope, EventType, Provider
from webhook_gateway.common.message import QueueMessage

ENVELOPE = Envelope(
    schema_version=1,
    event_id=uuid4(),
    idempotency_key="stripe:evt_123",
    provider=Provider.STRIPE,
    provider_event_id="evt_123",
    event_type=EventType.PAYMENT_SUCCEEDED,
    subject_id="stripe:pi_123",
    occurred_at=datetime(2026, 11, 17, 9, 59, 50, tzinfo=UTC),
    received_at=datetime(2026, 11, 17, 10, 0, 0, tzinfo=UTC),
    correlation_id="req-abc123",
    data={"amount": 1999},
)
RAW = b'{\n  "id": "evt_123",\n  "name": "Zo\xc3\xab"\n}\xff'  # not even valid UTF-8


def test_the_raw_body_comes_back_byte_for_byte() -> None:
    message = QueueMessage.of(ENVELOPE, RAW, {"content-type": "application/json"})

    assert message.body() == RAW


def test_a_message_survives_the_queue() -> None:
    message = QueueMessage.of(ENVELOPE, RAW, {"content-type": "application/json"})

    assert QueueMessage.model_validate_json(message.model_dump_json()) == message


@pytest.mark.parametrize("raw_body", ["not base64!", "abc", "é"])
def test_the_raw_body_must_be_base64(raw_body: str) -> None:
    payload = QueueMessage.of(ENVELOPE, RAW, {}).model_dump(mode="json")

    with pytest.raises(ValidationError, match="raw_body"):
        QueueMessage.model_validate(payload | {"raw_body": raw_body})


def test_unknown_fields_are_rejected() -> None:
    payload = QueueMessage.of(ENVELOPE, RAW, {}).model_dump(mode="json")

    with pytest.raises(ValidationError, match="extra"):
        QueueMessage.model_validate(payload | {"signature": "sha256=ab"})
