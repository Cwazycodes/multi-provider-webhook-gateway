"""Shopify: verifying X-Shopify-Hmac-Sha256, and normalising order webhooks."""

import base64
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated

from pydantic import StringConstraints

from webhook_gateway.common.envelope import (
    Envelope,
    EventType,
    NonEmptyStr,
    Provider,
    UtcDatetime,
)
from webhook_gateway.common.errors import SignatureError
from webhook_gateway.providers.normalising import (
    PayloadModel,
    Receipt,
    build_envelope,
    parse,
    required_header,
    unhandled,
)
from webhook_gateway.providers.signatures import SignedRequest, signed_by_any

HEADER = "X-Shopify-Hmac-Sha256"
EVENT_TYPES = {"orders/create": EventType.ORDER_CREATED, "orders/paid": EventType.ORDER_PAID}

# Shopify sends money as decimal strings; they stay strings, so nothing is rounded.
Money = Annotated[str, StringConstraints(pattern=r"^[0-9]+(\.[0-9]+)?$")]
Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]


def _base64(digest: bytes) -> str:
    return base64.b64encode(digest).decode()


class ShopifyVerifier:
    def verify(self, request: SignedRequest, secrets: Sequence[str], now: datetime) -> None:
        if not signed_by_any(secrets, request.body, [request.header(HEADER)], encode=_base64):
            raise SignatureError(f"{HEADER} matches none of the secrets")


class Order(PayloadModel):
    id: int
    order_number: int
    created_at: UtcDatetime
    updated_at: UtcDatetime
    currency: Currency
    total_price: Money
    financial_status: NonEmptyStr


def normalise(request: SignedRequest, receipt: Receipt) -> Envelope:
    parse(PayloadModel, request.body)  # not JSON (400) is reported before any 422
    webhook_id = required_header(request, "X-Shopify-Webhook-Id")
    topic = required_header(request, "X-Shopify-Topic")
    event_type = EVENT_TYPES.get(topic)
    if event_type is None:
        return unhandled(
            Provider.SHOPIFY, receipt, provider_event_id=webhook_id, provider_type=topic
        )
    order = parse(Order, request.body)
    return build_envelope(
        Provider.SHOPIFY,
        receipt,
        provider_event_id=webhook_id,
        event_type=event_type,
        subject_id=f"shopify:order:{order.id}",
        # An order is created once; it is paid when it was last updated.
        occurred_at=order.created_at if event_type is EventType.ORDER_CREATED else order.updated_at,
        data={
            "order_number": order.order_number,
            "total_price": order.total_price,
            "currency": order.currency,
            "financial_status": order.financial_status,
        },
    )
