"""Each provider's payloads become valid envelopes, or fail with the right error (WG-11)."""

import json
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Any
from uuid import UUID, uuid4

import pytest

from webhook_gateway.common.envelope import Envelope, EventType, Provider
from webhook_gateway.common.errors import ErrorResponse, PayloadError, SchemaError, error_response
from webhook_gateway.providers.normalising import Receipt
from webhook_gateway.providers.registry import PROVIDERS
from webhook_gateway.providers.signatures import SignedRequest
from webhook_gateway.synthetic.formats import apigw_event
from webhook_gateway.synthetic.generator import FAKES, Fault, FaultRates, generate

RECEIPT = Receipt(
    event_id=UUID("0b6f6c58-6f3e-4d55-9c1a-2f4c1d1e7a10"),
    received_at=datetime(2026, 11, 17, 10, 0, 5, tzinfo=UTC),
    correlation_id="req-abc123",
)
DELIVERY = "72d3162e-cc78-11e3-81ab-4c9367dc0958"
WEBHOOK_ID = "b54557e4-bdd9-4b37-8a5f-bf7d70bcd043"
SHA = "9fceb02d0ae598e95dc970b74767f19372d61af8"


def github(event: str) -> dict[str, str]:
    return {"X-GitHub-Event": event, "X-GitHub-Delivery": DELIVERY}


def shopify(topic: str) -> dict[str, str]:
    return {"X-Shopify-Topic": topic, "X-Shopify-Webhook-Id": WEBHOOK_ID}


REPOSITORY = {"id": 186853002, "full_name": "acme/widgets", "private": False}
PUSH = {
    "ref": "refs/heads/main",
    "before": "0" * 40,
    "after": SHA,
    "commits": [{"id": SHA, "message": "Fix flaky retry test"}],
    "repository": REPOSITORY | {"pushed_at": 1794909600},
    "sender": {"login": "ada-l", "id": 21031067},
}
PULL_REQUEST = {
    "id": 2083421345,
    "number": 42,
    "title": "Add order export",
    "state": "closed",
    "merged": True,
    "merged_at": "2026-11-17T09:59:30Z",
    "merge_commit_sha": SHA,
    "base": {"ref": "main"},
}
MERGED = {"action": "closed", "number": 42, "pull_request": PULL_REQUEST, "repository": REPOSITORY}
ORDER = {
    "id": 5302442819640,
    "order_number": 1042,
    "created_at": "2026-11-17T04:58:00-05:00",
    "updated_at": "2026-11-17T04:59:30-05:00",
    "currency": "GBP",
    "total_price": "23.99",
    "financial_status": "pending",
    "line_items": [{"title": "Trail flask", "quantity": 1, "price": "19.99"}],
}
INTENT = {
    "id": "pi_3QAbcdEfGhIjKl",
    "object": "payment_intent",
    "amount": 1999,
    "currency": "gbp",
    "status": "succeeded",
    "last_payment_error": None,
}
DECLINED = INTENT | {
    "amount_received": 0,
    "status": "requires_payment_method",
    "last_payment_error": {"code": "card_declined", "message": "Your card was declined."},
}
CHARGE = {
    "id": "ch_3QAbcdEfGhIjKl",
    "object": "charge",
    "amount": 1999,
    "amount_refunded": 1999,
    "currency": "gbp",
    "payment_intent": "pi_3QAbcdEfGhIjKl",
    "refunded": True,
}


def stripe_event(event_type: str, data_object: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "evt_1QAbcdEfGhIjKl",
        "object": "event",
        "created": 1794909590,
        "type": event_type,
        "data": {"object": data_object},
        "livemode": False,
    }


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


# (provider, headers, payload) -> (event_type, provider_event_id, subject_id, occurred_at, data)
HANDLED = [
    pytest.param(
        Provider.GITHUB,
        github("push"),
        PUSH,
        EventType.CODE_PUSHED,
        DELIVERY,
        "github:repo:186853002",
        at("2026-11-17T10:00:00Z"),
        {"repository": "acme/widgets", "ref": "refs/heads/main", "head": SHA, "commits": 1},
        id="github-push",
    ),
    pytest.param(
        Provider.GITHUB,
        github("pull_request"),
        MERGED,
        EventType.PULL_REQUEST_MERGED,
        DELIVERY,
        "github:pr:2083421345",
        at("2026-11-17T09:59:30Z"),
        {
            "repository": "acme/widgets",
            "number": 42,
            "title": "Add order export",
            "base": "main",
            "merge_commit": SHA,
        },
        id="github-pull-request-merged",
    ),
    pytest.param(
        Provider.SHOPIFY,
        shopify("orders/create"),
        ORDER,
        EventType.ORDER_CREATED,
        WEBHOOK_ID,
        "shopify:order:5302442819640",
        at("2026-11-17T09:58:00Z"),
        {
            "order_number": 1042,
            "total_price": "23.99",
            "currency": "GBP",
            "financial_status": "pending",
        },
        id="shopify-order-created",
    ),
    pytest.param(
        Provider.SHOPIFY,
        shopify("orders/paid"),
        ORDER | {"financial_status": "paid"},
        EventType.ORDER_PAID,
        WEBHOOK_ID,
        "shopify:order:5302442819640",
        at("2026-11-17T09:59:30Z"),
        {
            "order_number": 1042,
            "total_price": "23.99",
            "currency": "GBP",
            "financial_status": "paid",
        },
        id="shopify-order-paid",
    ),
    pytest.param(
        Provider.STRIPE,
        {},
        stripe_event("payment_intent.succeeded", INTENT),
        EventType.PAYMENT_SUCCEEDED,
        "evt_1QAbcdEfGhIjKl",
        "stripe:pi_3QAbcdEfGhIjKl",
        at("2026-11-17T09:59:50Z"),
        {"amount": 1999, "currency": "GBP", "status": "succeeded"},
        id="stripe-payment-succeeded",
    ),
    pytest.param(
        Provider.STRIPE,
        {},
        stripe_event("payment_intent.payment_failed", DECLINED),
        EventType.PAYMENT_FAILED,
        "evt_1QAbcdEfGhIjKl",
        "stripe:pi_3QAbcdEfGhIjKl",
        at("2026-11-17T09:59:50Z"),
        {
            "amount": 1999,
            "currency": "GBP",
            "status": "requires_payment_method",
            "failure_code": "card_declined",
        },
        id="stripe-payment-failed",
    ),
    pytest.param(
        Provider.STRIPE,
        {},
        stripe_event("charge.refunded", CHARGE),
        EventType.PAYMENT_REFUNDED,
        "evt_1QAbcdEfGhIjKl",
        "stripe:pi_3QAbcdEfGhIjKl",
        at("2026-11-17T09:59:50Z"),
        {"charge": "ch_3QAbcdEfGhIjKl", "amount_refunded": 1999, "currency": "GBP"},
        id="stripe-payment-refunded",
    ),
]

# (provider, headers, payload, the provider's own name for it, occurred_at)
UNHANDLED = [
    pytest.param(
        Provider.GITHUB,
        github("ping"),
        {"zen": "Keep it logically awesome.", "hook_id": 1},
        "ping",
        None,
        id="github-ping",
    ),
    pytest.param(
        Provider.GITHUB,
        github("pull_request"),
        MERGED | {"action": "opened"},
        "pull_request",
        None,
        id="github-pull-request-opened",
    ),
    pytest.param(
        Provider.GITHUB,
        github("pull_request"),
        MERGED | {"pull_request": PULL_REQUEST | {"merged": False, "merged_at": None}},
        "pull_request",
        None,
        id="github-pull-request-closed-unmerged",
    ),
    pytest.param(
        Provider.SHOPIFY,
        shopify("products/update"),
        {"id": 8123456789012, "title": "Trail flask"},
        "products/update",
        None,
        id="shopify-product-updated",
    ),
    pytest.param(
        Provider.STRIPE,
        {},
        stripe_event("customer.created", {"id": "cus_QAbcdEfGhIj", "object": "customer"}),
        "customer.created",
        at("2026-11-17T09:59:50Z"),
        id="stripe-customer-created",
    ),
]

MALFORMED = ErrorResponse(HTTPStatus.BAD_REQUEST, {"error": "malformed_json"})


def schema_invalid(field: str) -> ErrorResponse:
    return ErrorResponse(
        HTTPStatus.UNPROCESSABLE_CONTENT, {"error": "schema_invalid", "field": field}
    )


# (provider, headers, body) -> the response ingest must give
BAD = [
    pytest.param(Provider.GITHUB, github("push"), b'{"ref": "refs/', MALFORMED, id="cut-short"),
    pytest.param(Provider.STRIPE, {}, b"", MALFORMED, id="empty"),
    pytest.param(Provider.SHOPIFY, shopify("orders/paid"), b"\xff\xfe", MALFORMED, id="not-utf-8"),
    pytest.param(Provider.GITHUB, {}, b"{", MALFORMED, id="not-json-beats-missing-headers"),
    pytest.param(
        Provider.SHOPIFY, shopify("orders/create"), b"[]", schema_invalid("(root)"), id="array"
    ),
    pytest.param(
        Provider.GITHUB,
        {"X-GitHub-Event": "push"},
        PUSH,
        schema_invalid("headers.X-GitHub-Delivery"),
        id="github-no-delivery-id",
    ),
    pytest.param(
        Provider.GITHUB,
        {"X-GitHub-Delivery": DELIVERY},
        PUSH,
        schema_invalid("headers.X-GitHub-Event"),
        id="github-no-event-name",
    ),
    pytest.param(
        Provider.GITHUB,
        github("push"),
        {k: v for k, v in PUSH.items() if k != "repository"},
        schema_invalid("repository"),
        id="github-push-no-repository",
    ),
    pytest.param(
        Provider.GITHUB,
        github("push"),
        PUSH | {"repository": REPOSITORY | {"pushed_at": "2026-11-17T10:00:00Z"}},
        schema_invalid("repository.pushed_at"),
        id="github-push-pushed-at-not-seconds",
    ),
    pytest.param(
        Provider.GITHUB,
        github("push"),
        PUSH | {"repository": REPOSITORY | {"full_name": "", "pushed_at": 1794909600}},
        schema_invalid("repository.full_name"),
        id="github-push-empty-repository-name",
    ),
    pytest.param(
        Provider.GITHUB,
        github("pull_request"),
        {k: v for k, v in MERGED.items() if k != "action"},
        schema_invalid("action"),
        id="github-pull-request-no-action",
    ),
    pytest.param(
        Provider.GITHUB,
        github("pull_request"),
        MERGED | {"pull_request": PULL_REQUEST | {"merged_at": None}},
        schema_invalid("pull_request.merged_at"),
        id="github-merged-without-merged-at",
    ),
    pytest.param(
        Provider.SHOPIFY,
        {"X-Shopify-Webhook-Id": WEBHOOK_ID},
        ORDER,
        schema_invalid("headers.X-Shopify-Topic"),
        id="shopify-no-topic",
    ),
    pytest.param(
        Provider.SHOPIFY,
        shopify("orders/create"),
        ORDER | {"total_price": 23.99},
        schema_invalid("total_price"),
        id="shopify-price-as-number",
    ),
    pytest.param(
        Provider.SHOPIFY,
        shopify("orders/create"),
        ORDER | {"total_price": "NaN"},
        schema_invalid("total_price"),
        id="shopify-price-nan",
    ),
    pytest.param(
        Provider.SHOPIFY,
        shopify("orders/create"),
        ORDER | {"created_at": "2026-11-17T04:58:00"},
        schema_invalid("created_at"),
        id="shopify-naive-time",
    ),
    pytest.param(
        Provider.SHOPIFY,
        shopify("orders/create"),
        ORDER | {"created_at": "0001-01-01T00:00:00+05:00"},
        schema_invalid("created_at"),
        id="shopify-time-before-year-1-in-utc",
    ),
    pytest.param(
        Provider.STRIPE,
        {},
        {k: v for k, v in stripe_event("customer.created", {}).items() if k != "id"},
        schema_invalid("id"),
        id="stripe-no-id-even-when-unhandled",
    ),
    pytest.param(
        Provider.STRIPE,
        {},
        stripe_event("payment_intent.succeeded", INTENT) | {"id": ""},
        schema_invalid("id"),
        id="stripe-empty-id",
    ),
    pytest.param(
        Provider.STRIPE,
        {},
        stripe_event("payment_intent.succeeded", INTENT) | {"created": 10**20},
        schema_invalid("created"),
        id="stripe-created-out-of-range",
    ),
    pytest.param(
        Provider.STRIPE,
        {},
        stripe_event("payment_intent.succeeded", INTENT | {"amount": "1999"}),
        schema_invalid("data.object.amount"),
        id="stripe-amount-as-string",
    ),
    pytest.param(
        Provider.STRIPE,
        {},
        stripe_event("charge.refunded", CHARGE | {"payment_intent": None}),
        schema_invalid("data.object.payment_intent"),
        id="stripe-refund-without-payment-intent",
    ),
]


def normalise(provider: Provider, headers: dict[str, str], body: bytes) -> Envelope:
    return PROVIDERS[provider].normalise(SignedRequest.of(headers, body), RECEIPT)


def as_body(payload: Any) -> bytes:
    return payload if isinstance(payload, bytes) else json.dumps(payload).encode()


def test_every_provider_is_registered() -> None:
    assert set(PROVIDERS) == set(Provider)


def test_the_fixtures_cover_every_event_type() -> None:
    covered = {param.values[3] for param in HANDLED} | {EventType.UNHANDLED}

    assert covered == set(EventType)


@pytest.mark.parametrize(
    (
        "provider",
        "headers",
        "payload",
        "event_type",
        "event_id",
        "subject_id",
        "occurred_at",
        "data",
    ),
    HANDLED,
)
def test_each_supported_event_normalises_to_its_envelope(
    provider: Provider,
    headers: dict[str, str],
    payload: dict[str, Any],
    event_type: EventType,
    event_id: str,
    subject_id: str,
    occurred_at: datetime,
    data: dict[str, Any],
) -> None:
    envelope = normalise(provider, headers, as_body(payload))

    assert envelope == Envelope(
        schema_version=1,
        event_id=RECEIPT.event_id,
        idempotency_key=f"{provider}:{event_id}",
        provider=provider,
        provider_event_id=event_id,
        event_type=event_type,
        subject_id=subject_id,
        occurred_at=occurred_at,
        received_at=RECEIPT.received_at,
        correlation_id=RECEIPT.correlation_id,
        data=data,
    )


@pytest.mark.parametrize(
    ("provider", "headers", "payload", "provider_type", "occurred_at"), UNHANDLED
)
def test_other_event_types_are_accepted_and_marked_unhandled(
    provider: Provider,
    headers: dict[str, str],
    payload: dict[str, Any],
    provider_type: str,
    occurred_at: datetime | None,
) -> None:
    envelope = normalise(provider, headers, as_body(payload))

    assert envelope.event_type is EventType.UNHANDLED
    assert envelope.subject_id == f"{provider}:event:{envelope.provider_event_id}"
    assert envelope.occurred_at == (occurred_at or RECEIPT.received_at)
    assert envelope.data == {"provider_type": provider_type}


@pytest.mark.parametrize(("provider", "headers", "payload", "response"), BAD)
def test_every_bad_fixture_maps_to_the_right_error(
    provider: Provider, headers: dict[str, str], payload: Any, response: ErrorResponse
) -> None:
    with pytest.raises((PayloadError, SchemaError)) as exc_info:
        normalise(provider, headers, as_body(payload))

    assert error_response(exc_info.value) == response


def handles(delivery_headers: dict[str, str], body: str, provider: Provider) -> bool:
    """Whether the gateway reads this event's payload, so that a missing field matters."""
    match provider:
        case Provider.GITHUB:
            event = delivery_headers["X-GitHub-Event"]
            return event == "push" or json.loads(body).get("action") == "closed"
        case Provider.SHOPIFY:
            return delivery_headers["X-Shopify-Topic"].startswith("orders/")
        case Provider.STRIPE:
            return True  # every Stripe event carries the ID the gateway needs


@pytest.mark.parametrize("provider", list(Provider))
def test_generated_traffic_normalises_or_fails_with_the_right_error(
    provider: Provider, signing_secret: str
) -> None:
    start = datetime(2026, 11, 17, 10, 0, 0, tzinfo=UTC)
    rates = FaultRates(malformed=0.15, schema_invalid=0.15, duplicate=0.1)
    keys: set[str] = set()
    event_types: set[EventType] = set()
    for delivery in generate(
        provider, signing_secret, count=600, seed=21, start=start, faults=rates
    ):
        request = SignedRequest.from_apigw_event(apigw_event(delivery))
        receipt = Receipt(uuid4(), delivery.sent_at, delivery.request_id)
        headers = dict(delivery.headers)
        if delivery.fault is Fault.MALFORMED:
            with pytest.raises(PayloadError):
                PROVIDERS[provider].normalise(request, receipt)
            continue
        if delivery.fault is Fault.SCHEMA_INVALID and handles(headers, delivery.body, provider):
            with pytest.raises(SchemaError) as exc_info:
                PROVIDERS[provider].normalise(request, receipt)
            assert exc_info.value.field == FAKES[provider].required_field
            continue

        envelope = PROVIDERS[provider].normalise(request, receipt)

        if delivery.fault is Fault.SCHEMA_INVALID:  # a field the gateway never reads
            assert envelope.event_type is EventType.UNHANDLED
        if delivery.fault is Fault.DUPLICATE:
            assert envelope.idempotency_key in keys
        keys.add(envelope.idempotency_key)
        event_types.add(envelope.event_type)
        assert envelope.provider_event_id == delivery.provider_event_id
        assert envelope.correlation_id == delivery.request_id
        assert Envelope.model_validate_json(envelope.model_dump_json()) == envelope

    expected = {
        Provider.GITHUB: {EventType.CODE_PUSHED, EventType.PULL_REQUEST_MERGED},
        Provider.SHOPIFY: {EventType.ORDER_CREATED, EventType.ORDER_PAID},
        Provider.STRIPE: {
            EventType.PAYMENT_SUCCEEDED,
            EventType.PAYMENT_FAILED,
            EventType.PAYMENT_REFUNDED,
        },
    }
    assert event_types == expected[provider] | {EventType.UNHANDLED}
