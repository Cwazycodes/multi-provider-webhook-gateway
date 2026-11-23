"""Each provider's fake: realistic events, signed exactly as the provider signs (WG-09)."""

import json
from collections import Counter
from datetime import UTC, datetime
from random import Random

import pytest

from webhook_gateway.common.envelope import Provider
from webhook_gateway.synthetic import github, shopify, stripe
from webhook_gateway.synthetic.fakes import FakeEvent, FakeProvider

FAKES: dict[Provider, FakeProvider] = {
    Provider.GITHUB: github.FAKE,
    Provider.SHOPIFY: shopify.FAKE,
    Provider.STRIPE: stripe.FAKE,
}
SENT_AT = datetime(2026, 11, 17, 10, 0, 0, tzinfo=UTC)

# Known answers, computed with openssl (see the WG-09 guide), not with Python:
#   printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET"            (github)
#   printf '%s' "$BODY" | openssl dgst -sha1 -hmac "$SECRET"              (github, legacy)
#   printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" -binary | base64  (shopify)
#   printf '%s.%s' "$T" "$BODY" | openssl dgst -sha256 -hmac "$SECRET"    (stripe v1)
#   printf '%s.%s' "$T" "$BODY" | openssl dgst -sha256                    (stripe v0)
SECRET = "test-secret-not-real"
BODY = '{"id":"evt_test","object":"event"}'
SIGNED_AT = datetime.fromtimestamp(1763373600, tz=UTC)
KNOWN_ANSWERS = {
    Provider.GITHUB: {
        "X-Hub-Signature": "sha1=811bb04ee81e79595792b529b5446ac51c975191",
        "X-Hub-Signature-256": (
            "sha256=23692869dfe4744e7ff1d152fbb3db4cf3a7e01e23f4604fe33ce4a3168c99f8"
        ),
    },
    Provider.SHOPIFY: {"X-Shopify-Hmac-Sha256": "I2koad/kdE5/8dFS+7PbTPOn4B4j9GBP4zzkoxaMmfg="},
    Provider.STRIPE: {
        "Stripe-Signature": (
            "t=1763373600,"
            "v1=119b62eb37767ed4e8b75c60c3d7886369d9f8532876becd19f88fbd66ca5acf,"
            "v0=fa40eacd66e4182fa23a553a4336e1aa0edce9646da8cbc3a262992b7f5d6e20"
        )
    },
}

# What each provider documents sending (Stripe documents only its signature header;
# the other two are what its deliveries carry).
HEADER_NAMES = {
    Provider.GITHUB: {
        "Content-Type",
        "User-Agent",
        "X-GitHub-Delivery",
        "X-GitHub-Event",
        "X-GitHub-Hook-ID",
        "X-GitHub-Hook-Installation-Target-ID",
        "X-GitHub-Hook-Installation-Target-Type",
        "X-Hub-Signature",
        "X-Hub-Signature-256",
    },
    Provider.SHOPIFY: {
        "Content-Type",
        "X-Shopify-API-Version",
        "X-Shopify-Event-Id",
        "X-Shopify-Hmac-Sha256",
        "X-Shopify-Shop-Domain",
        "X-Shopify-Topic",
        "X-Shopify-Triggered-At",
        "X-Shopify-Webhook-Id",
    },
    Provider.STRIPE: {"Content-Type", "Stripe-Signature", "User-Agent"},
}

# Design note section 6, plus one type per provider that the gateway doesn't handle.
EVENT_KINDS = {
    Provider.GITHUB: {"push", "pull_request:closed", "pull_request:opened"},
    Provider.SHOPIFY: {"orders/create", "orders/paid", "products/update"},
    Provider.STRIPE: {
        "payment_intent.succeeded",
        "payment_intent.payment_failed",
        "charge.refunded",
        "customer.created",
    },
}


def kind_of(provider: Provider, event: FakeEvent) -> str:
    match provider:
        case Provider.GITHUB:
            action = event.payload.get("action")
            name = event.headers["X-GitHub-Event"]
            return f"{name}:{action}" if action else name
        case Provider.SHOPIFY:
            return event.headers["X-Shopify-Topic"]
        case Provider.STRIPE:
            kind: str = event.payload["type"]
            return kind


def events(provider: Provider, count: int, seed: int = 1) -> list[FakeEvent]:
    rng = Random(seed)
    return [FAKES[provider].event(rng, SENT_AT) for _ in range(count)]


@pytest.mark.parametrize("provider", list(Provider))
def test_signatures_match_openssl(provider: Provider) -> None:
    assert FAKES[provider].sign(SECRET, BODY, SIGNED_AT) == KNOWN_ANSWERS[provider]


@pytest.mark.parametrize("provider", list(Provider))
def test_headers_are_exactly_the_ones_the_provider_sends(provider: Provider) -> None:
    fake = FAKES[provider]
    event = events(provider, 1)[0]

    headers = {**event.headers, **fake.sign(SECRET, fake.render(event.payload), SENT_AT)}

    assert set(headers) == HEADER_NAMES[provider]


@pytest.mark.parametrize("provider", list(Provider))
def test_every_event_kind_is_generated_and_none_dominates(provider: Provider) -> None:
    kinds = Counter(kind_of(provider, event) for event in events(provider, 600))

    assert set(kinds) == EVENT_KINDS[provider]
    assert min(kinds.values()) > 600 / len(kinds) / 2


@pytest.mark.parametrize("provider", list(Provider))
def test_provider_event_ids_are_where_ingest_will_look(provider: Provider) -> None:
    for event in events(provider, 50):
        match provider:
            case Provider.GITHUB:
                assert event.provider_event_id == event.headers["X-GitHub-Delivery"]
            case Provider.SHOPIFY:
                assert event.provider_event_id == event.headers["X-Shopify-Webhook-Id"]
            case Provider.STRIPE:
                assert event.provider_event_id == event.payload["id"]


@pytest.mark.parametrize("provider", list(Provider))
def test_bodies_are_formatted_as_the_provider_formats_them(provider: Provider) -> None:
    fake = FAKES[provider]
    event = events(provider, 1)[0]

    body = fake.render(event.payload)

    assert json.loads(body) == event.payload
    # Stripe's bodies span many lines; GitHub's and Shopify's are one compact line.
    assert ("\n" in body) == (provider is Provider.STRIPE)


@pytest.mark.parametrize("provider", list(Provider))
def test_the_same_random_state_gives_the_same_events(provider: Provider) -> None:
    assert events(provider, 20, seed=3) == events(provider, 20, seed=3)
    assert events(provider, 20, seed=3) != events(provider, 20, seed=4)
