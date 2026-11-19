"""Structured JSON log lines and the redaction rule (WG-08)."""

import hmac
import io
import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import uuid4

import pytest
from aws_lambda_powertools import Logger

from webhook_gateway.common.log import REDACTED, make_logger


@pytest.fixture
def service() -> str:
    # Powertools sets up a service's logger once per process, so a name reused by
    # another test would keep writing to that test's stream.
    return f"test-{uuid4().hex[:8]}"


@pytest.fixture
def stream() -> io.StringIO:
    return io.StringIO()


@pytest.fixture
def logger(service: str, stream: io.StringIO) -> Logger:
    return make_logger(service, stage="dev", stream=stream)


@pytest.fixture
def new_york_clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The machine's clock in New York time, as a laptop's might be."""
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def lines(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def strings_in(value: Any) -> Iterator[str]:
    """Every key and string value in parsed JSON, so escaping can't hide a match."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from strings_in(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings_in(item)
    elif isinstance(value, str):
        yield value


def test_a_log_line_is_one_json_object_with_the_required_keys(
    logger: Logger, stream: io.StringIO, service: str
) -> None:
    with logger.append_context_keys(provider="stripe", correlation_id="req-abc123"):
        logger.info("accepted", extra={"event_id": "0b6f6c58-6f3e-4d55-9c1a-2f4c1d1e7a10"})

    (line,) = lines(stream)
    assert {
        "level": "INFO",
        "message": "accepted",
        "service": service,
        "stage": "dev",
        "provider": "stripe",
        "correlation_id": "req-abc123",
        "event_id": "0b6f6c58-6f3e-4d55-9c1a-2f4c1d1e7a10",
    }.items() <= line.items()


@pytest.mark.usefixtures("new_york_clock")
def test_timestamps_are_utc_whatever_the_machine_clock(logger: Logger, stream: io.StringIO) -> None:
    logger.info("tick")

    (line,) = lines(stream)
    stamp = datetime.fromisoformat(line["timestamp"])
    assert stamp.utcoffset() == timedelta(0)
    assert abs(datetime.now(UTC) - stamp) < timedelta(minutes=1)


def test_a_log_line_never_holds_a_signature_secret_raw_body_or_headers(
    logger: Logger, stream: io.StringIO, signing_secret: str
) -> None:
    body = '{"id": "evt_123", "type": "payment_intent.succeeded", "amount": 1999}'
    signature = hmac.new(signing_secret.encode(), f"1763373600.{body}".encode(), sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "Stripe-Signature": f"t=1763373600,v1={signature}",
        "User-Agent": "Stripe/1.0 (+https://stripe.com/docs/webhooks)",
    }
    event = {"headers": headers, "body": body, "requestContext": {"requestId": "req-abc123"}}

    logger.info(
        "careless",
        extra={
            "headers": headers,
            "raw_body": body,
            "signature": signature,
            "signing_secret": signing_secret,
            "event_id": "0b6f6c58-6f3e-4d55-9c1a-2f4c1d1e7a10",
        },
    )
    logger.info(event)  # what Powertools logs if POWERTOOLS_LOGGER_LOG_EVENT is ever set
    logger.append_keys(headers=headers)
    logger.info("persistent key")

    careless, whole_event, persistent = lines(stream)
    text = "\n".join(strings_in([careless, whole_event, persistent]))
    for leaked in (signature, signing_secret, body, *headers, *headers.values()):
        assert leaked not in text
    # Only the sensitive values went: the rest of each line is intact.
    assert careless["event_id"] == "0b6f6c58-6f3e-4d55-9c1a-2f4c1d1e7a10"
    assert whole_event["message"]["requestContext"] == {"requestId": "req-abc123"}
    assert persistent["headers"] == REDACTED


def test_redaction_reaches_into_lists(logger: Logger, stream: io.StringIO) -> None:
    sqs_batch = {"Records": [{"messageId": "m-1", "body": "an envelope and its raw body"}]}

    logger.info(sqs_batch)

    (line,) = lines(stream)
    assert line["message"] == {"Records": [{"messageId": "m-1", "body": REDACTED}]}


@pytest.mark.parametrize(
    ("key", "redacted"),
    [
        ("Stripe-Signature", True),
        ("X-Hub-Signature-256", True),
        ("X-Shopify-Hmac-Sha256", True),
        ("Authorization", True),
        ("Cookie", True),
        ("X-Amz-Security-Token", True),
        ("secrets", True),
        ("raw_body", True),
        ("rawBody", True),
        ("multiValueHeaders", True),
        ("body_size", False),
        ("X-GitHub-Event", False),
        ("provider_event_id", False),
    ],
)
def test_keys_match_however_they_are_spelt(
    logger: Logger, stream: io.StringIO, key: str, redacted: bool
) -> None:
    logger.info("one key", extra={key: "the-value"})

    (line,) = lines(stream)
    assert line[key] == (REDACTED if redacted else "the-value")
