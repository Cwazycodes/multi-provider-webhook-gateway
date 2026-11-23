"""Shopify-style fakes: order and product webhooks, signed as Shopify signs them."""

import base64
import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from random import Random

from webhook_gateway.synthetic.fakes import FakeEvent, FakeProvider, Payload, fake_uuid, hmac_sha256

# products/update isn't in the gateway's vocabulary.
TOPICS = ("orders/create", "orders/paid", "products/update")
SHOPS = ("acme-outdoor", "northwind-tea", "blue-hen-books")
PRODUCTS = ("Trail flask", "Oolong sampler", "Field notebook", "Canvas tote")
PRICES = ("4.99", "12.50", "19.99", "24.00")
# Shops stamp times in their own time zone.
OFFSETS = (timedelta(hours=-5), timedelta(0), timedelta(hours=1))


def event(rng: Random, at: datetime) -> FakeEvent:
    topic = rng.choice(TOPICS)
    happened = at - timedelta(seconds=rng.randint(0, 120))
    local = happened.astimezone(timezone(rng.choice(OFFSETS)))
    if topic == "products/update":
        payload = _product(rng, local)
    else:
        payload = _order(rng, local, paid=topic == "orders/paid")
    webhook_id = fake_uuid(rng)
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-API-Version": "2026-10",
        "X-Shopify-Event-Id": fake_uuid(rng),
        "X-Shopify-Shop-Domain": f"{rng.choice(SHOPS)}.myshopify.com",
        "X-Shopify-Topic": topic,
        "X-Shopify-Triggered-At": happened.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "X-Shopify-Webhook-Id": webhook_id,
    }
    return FakeEvent(provider_event_id=webhook_id, headers=headers, payload=payload)


def _order(rng: Random, happened: datetime, *, paid: bool) -> Payload:
    order_id = rng.randrange(5 * 10**12, 6 * 10**12)
    number = rng.randint(1001, 9999)
    items = [_line_item(rng) for _ in range(rng.randint(1, 3))]
    subtotal = sum((Decimal(item["price"]) * item["quantity"] for item in items), Decimal(0))
    tax = (subtotal * Decimal("0.2")).quantize(Decimal("0.01"))
    created = happened - timedelta(minutes=rng.randint(1, 30)) if paid else happened
    return {
        "id": order_id,
        "admin_graphql_api_id": f"gid://shopify/Order/{order_id}",
        "name": f"#{number}",
        "order_number": number,
        "created_at": created.isoformat("T", "seconds"),
        "processed_at": created.isoformat("T", "seconds"),
        "updated_at": happened.isoformat("T", "seconds"),
        "currency": "GBP",
        "financial_status": "paid" if paid else "pending",
        # Shopify sends money as decimal strings.
        "subtotal_price": str(subtotal),
        "total_tax": str(tax),
        "total_price": str(subtotal + tax),
        "line_items": items,
        "customer": {"id": rng.randrange(6 * 10**12, 7 * 10**12)},
    }


def _line_item(rng: Random) -> Payload:
    return {
        "id": rng.randrange(10**13, 2 * 10**13),
        "title": rng.choice(PRODUCTS),
        "quantity": rng.randint(1, 3),
        "price": rng.choice(PRICES),
        "sku": f"SKU-{rng.randint(1000, 9999)}",
    }


def _product(rng: Random, happened: datetime) -> Payload:
    product_id = rng.randrange(8 * 10**12, 9 * 10**12)
    title = rng.choice(PRODUCTS)
    return {
        "id": product_id,
        "admin_graphql_api_id": f"gid://shopify/Product/{product_id}",
        "title": title,
        "handle": title.lower().replace(" ", "-"),
        "status": "active",
        "updated_at": happened.isoformat("T", "seconds"),
        "variants": [{"id": rng.randrange(10**13, 2 * 10**13), "price": rng.choice(PRICES)}],
    }


def render(payload: Payload) -> str:
    return json.dumps(payload, separators=(",", ":"))


def sign(secret: str, body: str, signed_at: datetime) -> dict[str, str]:
    return {"X-Shopify-Hmac-Sha256": base64.b64encode(hmac_sha256(secret, body)).decode()}


FAKE = FakeProvider(
    event=event, render=render, sign=sign, required_field="id", signs_timestamp=False
)
