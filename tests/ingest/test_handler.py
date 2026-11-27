"""The ingest handler: one test per row of the response table (WG-12)."""

import base64
import dataclasses
import io
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import boto3
import pytest
from botocore.config import Config
from mypy_boto3_sqs.type_defs import MessageTypeDef

from webhook_gateway.common.clients import make_sqs_client
from webhook_gateway.common.envelope import EventType, Provider
from webhook_gateway.common.log import make_logger
from webhook_gateway.common.message import QueueMessage
from webhook_gateway.common.settings import Settings
from webhook_gateway.ingest.handler import MAX_BODY_BYTES, Ingest, create_ingest
from webhook_gateway.ingest.secrets import CachedSecrets, StaticSecrets
from webhook_gateway.providers.registry import PROVIDERS
from webhook_gateway.synthetic.formats import apigw_event
from webhook_gateway.synthetic.generator import FAKES, Delivery, Fault, FaultRates, generate

SENT_AT = datetime(2026, 11, 17, 10, 0, 0, tzinfo=UTC)
SIGNATURE_HEADERS = {"x-hub-signature", "x-hub-signature-256", "x-shopify-hmac-sha256"}
SIGNATURE_HEADERS |= {"stripe-signature"}


class Clock:
    def __init__(self) -> None:
        self.now = SENT_AT

    def __call__(self) -> datetime:
        return self.now


class RecordingSecrets:
    """Static secrets that remember whether anyone asked, i.e. whether hashing started."""

    def __init__(self, secret: str) -> None:
        self.inner = StaticSecrets({provider: [secret] for provider in Provider})
        self.asked = 0

    def secrets(self, provider: Provider) -> Sequence[str]:
        self.asked += 1
        return self.inner.secrets(provider)


@pytest.fixture
def queue_url(aws: None) -> str:
    return boto3.client("sqs").create_queue(QueueName="webhook-gateway-test-events")["QueueUrl"]


@pytest.fixture
def secrets(signing_secret: str) -> RecordingSecrets:
    return RecordingSecrets(signing_secret)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def stream() -> io.StringIO:
    return io.StringIO()


@pytest.fixture
def ingest(queue_url: str, secrets: RecordingSecrets, clock: Clock, stream: io.StringIO) -> Ingest:
    # A fresh service name per test: Powertools sets up each name once per process.
    logger = make_logger(f"test-{uuid4().hex[:8]}", stage="test", stream=stream)
    return Ingest(
        queue_url=queue_url, sqs=make_sqs_client(), secrets=secrets, logger=logger, clock=clock
    )


def delivery(provider: Provider, secret: str, **faults: float) -> Delivery:
    """The first generated delivery; a fault at rate 1 makes it that fault."""
    rates = FaultRates(**faults)
    return next(generate(provider, secret, count=1, seed=3, start=SENT_AT, faults=rates))


def answer(response: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    assert response["headers"] == {"content-type": "application/json"}
    return response["statusCode"], json.loads(response["body"])


def queued(queue_url: str) -> list[MessageTypeDef]:
    received = boto3.client("sqs").receive_message(
        QueueUrl=queue_url, MaxNumberOfMessages=10, MessageAttributeNames=["All"]
    )
    return received.get("Messages", [])


def queue_depth(queue_url: str) -> int:
    attributes = boto3.client("sqs").get_queue_attributes(
        QueueUrl=queue_url, AttributeNames=["ApproximateNumberOfMessages"]
    )
    return int(attributes["Attributes"]["ApproximateNumberOfMessages"])


def lines(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


# Row 202 -------------------------------------------------------------------------


@pytest.mark.parametrize("provider", list(Provider))
def test_a_valid_request_is_202_and_leaves_exactly_one_message(
    ingest: Ingest, queue_url: str, signing_secret: str, provider: Provider
) -> None:
    sent = delivery(provider, signing_secret)

    status, body = answer(ingest.handle(apigw_event(sent)))

    assert status == 202
    (message,) = queued(queue_url)
    parsed = QueueMessage.model_validate_json(message["Body"])
    assert body == {"event_id": str(parsed.envelope.event_id)}
    assert parsed.envelope.provider_event_id == sent.provider_event_id
    assert parsed.body() == sent.body.encode()
    assert set(parsed.headers) <= PROVIDERS[provider].archived_headers
    assert message["MessageAttributes"] == {
        "correlation_id": {"DataType": "String", "StringValue": sent.request_id},
        "provider": {"DataType": "String", "StringValue": str(provider)},
    }


def test_an_unhandled_event_type_is_still_202_and_queued(
    ingest: Ingest, queue_url: str, clock: Clock, signing_secret: str
) -> None:
    unhandled = next(
        sent
        for sent in generate(Provider.STRIPE, signing_secret, count=50, seed=5, start=SENT_AT)
        if json.loads(sent.body)["type"] == "customer.created"
    )
    clock.now = unhandled.sent_at

    status, _ = answer(ingest.handle(apigw_event(unhandled)))

    assert status == 202
    (message,) = queued(queue_url)
    assert QueueMessage.model_validate_json(message["Body"]).envelope.event_type is (
        EventType.UNHANDLED
    )


def test_a_redelivery_is_queued_again_for_the_processor_to_skip(
    ingest: Ingest, queue_url: str, signing_secret: str
) -> None:
    sent = delivery(Provider.GITHUB, signing_secret)

    first = answer(ingest.handle(apigw_event(sent)))
    second = answer(ingest.handle(apigw_event(sent)))

    assert first[0] == second[0] == 202
    assert first[1] != second[1]  # each request gets its own event_id
    keys = {
        QueueMessage.model_validate_json(m["Body"]).envelope.idempotency_key
        for m in queued(queue_url)
    }
    assert keys == {f"github:{sent.provider_event_id}"}


def test_a_base64_encoded_body_is_queued_decoded(
    ingest: Ingest, queue_url: str, signing_secret: str
) -> None:
    sent = delivery(Provider.SHOPIFY, signing_secret)
    encoded = base64.b64encode(sent.body.encode()).decode()
    event = apigw_event(sent) | {"body": encoded, "isBase64Encoded": True}

    status, _ = answer(ingest.handle(event))

    assert status == 202
    (message,) = queued(queue_url)
    assert QueueMessage.model_validate_json(message["Body"]).body() == sent.body.encode()


def test_the_largest_body_accepted_still_fits_an_sqs_message(
    ingest: Ingest, queue_url: str, signing_secret: str
) -> None:
    sent = delivery(Provider.STRIPE, signing_secret)
    payload = json.loads(sent.body) | {"padding": ""}
    padding = MAX_BODY_BYTES - len(FAKES[Provider.STRIPE].render(payload))
    body = FAKES[Provider.STRIPE].render(payload | {"padding": "x" * padding})
    signature = FAKES[Provider.STRIPE].sign(signing_secret, body, SENT_AT)
    biggest = dataclasses.replace(sent, body=body, headers={**sent.headers, **signature})

    status, _ = answer(ingest.handle(apigw_event(biggest)))

    assert len(body.encode()) == MAX_BODY_BYTES
    assert status == 202
    (message,) = queued(queue_url)
    assert len(message["Body"].encode()) < 256 * 1024


# Rows 404, 413, 401, 400 and 422 ------------------------------------------------------


def test_an_unknown_provider_is_404(
    ingest: Ingest, queue_url: str, secrets: RecordingSecrets, signing_secret: str
) -> None:
    event = apigw_event(delivery(Provider.STRIPE, signing_secret))
    event = event | {"pathParameters": {"provider": "gitlab"}, "rawPath": "/webhooks/gitlab"}

    assert answer(ingest.handle(event)) == (404, {"error": "unknown_provider"})
    assert queue_depth(queue_url) == 0
    assert secrets.asked == 0


@pytest.mark.parametrize("encoded", [False, True], ids=["text", "base64"])
def test_a_body_over_128_kb_is_413_before_any_hashing(
    ingest: Ingest,
    queue_url: str,
    secrets: RecordingSecrets,
    signing_secret: str,
    encoded: bool,
) -> None:
    raw = b"x" * (MAX_BODY_BYTES + 1)
    body = base64.b64encode(raw).decode() if encoded else raw.decode()
    event = apigw_event(delivery(Provider.GITHUB, signing_secret))
    event = event | {"body": body, "isBase64Encoded": encoded}

    assert answer(ingest.handle(event)) == (413, {"error": "too_large"})
    assert queue_depth(queue_url) == 0
    assert secrets.asked == 0


@pytest.mark.parametrize(
    ("provider", "faults"),
    [
        pytest.param(Provider.GITHUB, {"bad_signature": 1.0}, id="wrong-secret"),
        pytest.param(Provider.STRIPE, {"stale_timestamp": 1.0}, id="stale-timestamp"),
        pytest.param(Provider.SHOPIFY, {}, id="missing-header"),
    ],
)
def test_a_bad_signature_is_401_without_detail(
    ingest: Ingest,
    queue_url: str,
    signing_secret: str,
    provider: Provider,
    faults: dict[str, float],
) -> None:
    sent = delivery(provider, signing_secret, **faults)
    if not faults:
        sent = dataclasses.replace(
            sent, headers={k: v for k, v in sent.headers.items() if "Hmac" not in k}
        )

    assert answer(ingest.handle(apigw_event(sent))) == (401, {"error": "unauthorized"})
    assert queue_depth(queue_url) == 0


def test_a_validly_signed_body_that_isnt_json_is_400(
    ingest: Ingest, queue_url: str, signing_secret: str
) -> None:
    sent = delivery(Provider.STRIPE, signing_secret, malformed=1.0)

    assert answer(ingest.handle(apigw_event(sent))) == (400, {"error": "malformed_json"})
    assert queue_depth(queue_url) == 0


def test_validly_signed_json_that_fails_the_schema_is_422_with_the_field(
    ingest: Ingest, queue_url: str, signing_secret: str
) -> None:
    sent = delivery(Provider.STRIPE, signing_secret, schema_invalid=1.0)

    assert answer(ingest.handle(apigw_event(sent))) == (
        422,
        {"error": "schema_invalid", "field": "id"},
    )
    assert queue_depth(queue_url) == 0


# Row 503, and the 5xx rule -------------------------------------------------------------


def test_sqs_still_failing_after_retries_is_503(
    ingest: Ingest, queue_url: str, signing_secret: str
) -> None:
    missing = dataclasses.replace(ingest, queue_url=queue_url.replace("events", "missing"))

    response = missing.handle(apigw_event(delivery(Provider.STRIPE, signing_secret)))

    assert answer(response) == (503, {"error": "try_again"})


def test_an_unreachable_queue_is_503_too(ingest: Ingest, signing_secret: str) -> None:
    # A network failure is a BotoCoreError, not a ClientError. One attempt keeps it quick.
    unreachable = boto3.client(
        "sqs",
        endpoint_url="http://127.0.0.1:9",
        config=Config(retries={"total_max_attempts": 1}, connect_timeout=1),
    )
    offline = dataclasses.replace(ingest, sqs=unreachable)

    response = offline.handle(apigw_event(delivery(Provider.STRIPE, signing_secret)))

    assert answer(response) == (503, {"error": "try_again"})


def test_no_secrets_configured_is_the_gateways_own_fault(
    ingest: Ingest, queue_url: str, signing_secret: str
) -> None:
    unconfigured = dataclasses.replace(ingest, secrets=StaticSecrets({}))

    response = unconfigured.handle(apigw_event(delivery(Provider.STRIPE, signing_secret)))

    assert answer(response) == (500, {"error": "internal_error"})
    assert queue_depth(queue_url) == 0


@pytest.mark.parametrize("provider", list(Provider))
def test_generated_traffic_gets_the_right_status_and_never_a_5xx(
    ingest: Ingest, queue_url: str, clock: Clock, signing_secret: str, provider: Provider
) -> None:
    rates = FaultRates(
        bad_signature=0.1,
        stale_timestamp=0.1 if provider is Provider.STRIPE else 0.0,
        malformed=0.1,
        schema_invalid=0.1,
        duplicate=0.1,
    )
    # schema_invalid is 422 when the gateway reads that event type, else 202 (WG-11).
    expected = {
        Fault.BAD_SIGNATURE: {401},
        Fault.STALE_TIMESTAMP: {401},
        Fault.MALFORMED: {400},
        Fault.SCHEMA_INVALID: {422, 202},
        Fault.DUPLICATE: {202},
        None: {202},
    }
    accepted = 0
    for sent in generate(provider, signing_secret, count=300, seed=8, start=SENT_AT, faults=rates):
        clock.now = sent.sent_at
        status, _ = answer(ingest.handle(apigw_event(sent)))
        assert status in expected[sent.fault], sent.fault
        accepted += status == 202

    assert queue_depth(queue_url) == accepted


# Logs and wiring -------------------------------------------------------------------------


def test_each_request_logs_one_summary_line(
    ingest: Ingest, stream: io.StringIO, signing_secret: str
) -> None:
    good = delivery(Provider.STRIPE, signing_secret)
    bad = delivery(Provider.STRIPE, signing_secret, bad_signature=1.0)

    ingest.handle(apigw_event(good))
    ingest.handle(apigw_event(bad))

    accepted, rejected = lines(stream)
    assert {
        "message": "accepted",
        "provider": "stripe",
        "correlation_id": good.request_id,
    }.items() <= accepted.items()
    assert "event_id" in accepted
    assert {"message": "rejected", "reason": "unauthorized"}.items() <= rejected.items()
    assert bad.headers["Stripe-Signature"] not in json.dumps(rejected)


def test_archived_headers_never_include_a_signature() -> None:
    for adapter in PROVIDERS.values():
        assert not adapter.archived_headers & SIGNATURE_HEADERS
        assert all(name == name.lower() for name in adapter.archived_headers)


def test_create_ingest_wires_settings_a_cached_secret_source_and_the_queue(
    aws: None, monkeypatch: pytest.MonkeyPatch, signing_secret: str
) -> None:
    queue = boto3.client("sqs").create_queue(QueueName="webhook-gateway-dev-events")["QueueUrl"]
    environment = {
        "STAGE": "dev",
        "PARAMETER_PATH": "/webhook-gateway/dev",
        "QUEUE_URL": queue,
        "DEAD_LETTER_QUEUE_URL": queue.replace("events", "dlq"),
        "TABLE_NAME": "webhook-gateway-dev",
        "ARCHIVE_BUCKET": "webhook-gateway-dev-archive",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    ingest = create_ingest(Settings(), StaticSecrets({Provider.GITHUB: [signing_secret]}))
    sent = next(generate(Provider.GITHUB, signing_secret, count=1, seed=1, start=SENT_AT))

    assert isinstance(ingest.secrets, CachedSecrets)
    assert answer(ingest.handle(apigw_event(sent)))[0] == 202
    assert queue_depth(queue) == 1
