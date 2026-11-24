"""What every normaliser shares: parsing, error paths and the envelope (WG-11)."""

import json
import traceback
from datetime import UTC, datetime
from uuid import UUID

import pytest

from webhook_gateway.common.envelope import EventType, Provider
from webhook_gateway.common.errors import PayloadError, SchemaError
from webhook_gateway.providers.normalising import (
    PayloadModel,
    Receipt,
    UnixSeconds,
    build_envelope,
    parse,
    required_header,
)
from webhook_gateway.providers.signatures import SignedRequest

RECEIPT = Receipt(
    event_id=UUID("0b6f6c58-6f3e-4d55-9c1a-2f4c1d1e7a10"),
    received_at=datetime(2026, 11, 17, 10, 0, 5, tzinfo=UTC),
    correlation_id="req-abc123",
)


class Price(PayloadModel):
    amount: int


class LineItem(PayloadModel):
    price: Price


class Order(PayloadModel):
    line_items: list[LineItem]
    created: UnixSeconds


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b'{"line_items": [', id="cut-short"),
        pytest.param(b"", id="empty"),
        pytest.param(b'{"name": "\xff"}', id="not-utf-8"),
    ],
)
def test_a_body_that_isnt_json_is_a_payload_error(body: bytes) -> None:
    with pytest.raises(PayloadError, match="Invalid JSON"):
        parse(Order, body)


@pytest.mark.parametrize(
    ("body", "field"),
    [
        pytest.param(b"[1, 2]", "(root)", id="not-an-object"),
        pytest.param(b'{"created": 1}', "line_items", id="missing"),
        pytest.param(
            b'{"line_items": [{"price": {"amount": "5"}}], "created": 1}',
            "line_items.0.price.amount",
            id="nested-wrong-type",
        ),
        pytest.param(b'{"line_items": [], "created": 100000000000000000000}', "created", id="huge"),
        pytest.param(b'{"line_items": [], "created": -1}', "created", id="negative"),
    ],
)
def test_a_schema_mismatch_is_a_schema_error_at_the_field(body: bytes, field: str) -> None:
    with pytest.raises(SchemaError) as exc_info:
        parse(Order, body)

    assert exc_info.value.field == field


def test_a_schema_error_carries_no_part_of_the_body() -> None:
    body = json.dumps({"line_items": [{"price": {"amount": "card-4242"}}], "created": 1})

    with pytest.raises(SchemaError) as exc_info:
        parse(Order, body.encode())

    # What logger.exception would print: no chained ValidationError holding the input.
    logged = "".join(traceback.format_exception(exc_info.value))
    assert "card-4242" not in logged


def test_unknown_fields_are_ignored() -> None:
    order = parse(Order, b'{"line_items": [], "created": 1, "note": "gift"}')

    assert order == Order(line_items=[], created=1)


def test_required_headers_are_read_in_any_case() -> None:
    request = SignedRequest.of({"X-Shopify-Topic": "orders/paid"}, b"{}")

    assert required_header(request, "x-shopify-topic") == "orders/paid"


@pytest.mark.parametrize("headers", [{}, {"X-Shopify-Topic": ""}])
def test_a_missing_or_empty_header_is_a_schema_error(headers: dict[str, str]) -> None:
    request = SignedRequest.of(headers, b"{}")

    with pytest.raises(SchemaError) as exc_info:
        required_header(request, "X-Shopify-Topic")

    assert exc_info.value.field == "headers.X-Shopify-Topic"


def test_the_envelope_takes_its_ids_and_times_from_the_receipt() -> None:
    occurred = datetime(2026, 11, 17, 9, 59, 50, tzinfo=UTC)

    envelope = build_envelope(
        Provider.STRIPE,
        RECEIPT,
        provider_event_id="evt_123",
        event_type=EventType.PAYMENT_SUCCEEDED,
        subject_id="stripe:pi_123",
        occurred_at=occurred,
        data={"amount": 1999},
    )

    assert envelope.event_id == RECEIPT.event_id
    assert envelope.idempotency_key == "stripe:evt_123"
    assert envelope.occurred_at == occurred
    assert envelope.received_at == RECEIPT.received_at
    assert envelope.correlation_id == "req-abc123"


def test_with_no_event_time_the_envelope_falls_back_to_received_at() -> None:
    envelope = build_envelope(
        Provider.GITHUB,
        RECEIPT,
        provider_event_id="72d3162e-cc78-11e3-81ab-4c9367dc0958",
        event_type=EventType.UNHANDLED,
        subject_id="github:event:72d3162e-cc78-11e3-81ab-4c9367dc0958",
        occurred_at=None,
        data={},
    )

    assert envelope.occurred_at == RECEIPT.received_at
