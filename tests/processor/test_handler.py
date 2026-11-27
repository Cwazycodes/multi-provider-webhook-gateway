"""The processor function: one SQS batch in, only the failed message IDs out (WG-16).

Batches are shaped as Lambda's SQS trigger passes them, and the table and both queues
are moto's. Every envelope() is a fresh delivery: a new event_id, and the key it's given.
"""

import dataclasses
import io
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import boto3
import pytest
from botocore.exceptions import ClientError, ReadTimeoutError
from mypy_boto3_dynamodb import DynamoDBClient

from webhook_gateway.common.clients import make_sqs_client
from webhook_gateway.common.envelope import Envelope, EventType, Provider
from webhook_gateway.common.log import make_logger
from webhook_gateway.common.message import QueueMessage
from webhook_gateway.processor.handler import Processor, handler, processor_from_environment
from webhook_gateway.processor.idempotency import EventRecords
from webhook_gateway.processor.subjects import Subjects

TABLE = "webhook-gateway-test"  # the table conftest.py makes
START = datetime(2026, 11, 17, 10, 0, 0, tzinfo=UTC)
LOCK = timedelta(seconds=60)  # design note section 9


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def dead_letter_queue(aws: None) -> str:
    return boto3.client("sqs").create_queue(QueueName="webhook-gateway-test-dlq")["QueueUrl"]


@pytest.fixture
def stream() -> io.StringIO:
    return io.StringIO()


@pytest.fixture
def processor(
    dynamodb: DynamoDBClient, dead_letter_queue: str, clock: Clock, stream: io.StringIO
) -> Processor:
    # A fresh service name per test: Powertools sets up each name once per process.
    logger = make_logger(f"test-{uuid4().hex[:8]}", stage="test", stream=stream)
    return Processor(
        records=EventRecords(dynamodb, TABLE, clock),
        subjects=Subjects(dynamodb, TABLE, clock),
        sqs=make_sqs_client(),
        dead_letter_queue_url=dead_letter_queue,
        logger=logger,
    )


def envelope(
    event_type: EventType = EventType.PAYMENT_SUCCEEDED,
    occurred_at: datetime = START,
    key: str = "evt_1",
    subject: str = "pi_1",
) -> Envelope:
    return Envelope(
        schema_version=1,
        event_id=uuid4(),
        idempotency_key=f"stripe:{key}",
        provider=Provider.STRIPE,
        provider_event_id=key,
        event_type=event_type,
        subject_id=f"stripe:{subject}",
        occurred_at=occurred_at,
        received_at=START,
        correlation_id=f"request-{key}",
        data={},
    )


def record(body: str, correlation_id: str = "request-1") -> dict[str, Any]:
    """One record of a batch from Lambda's SQS trigger, with ingest's message attributes."""

    def text(value: str) -> dict[str, Any]:
        return {
            "stringValue": value,
            "stringListValues": [],
            "binaryListValues": [],
            "dataType": "String",
        }

    return {
        "messageId": str(uuid4()),
        "receiptHandle": f"handle-{uuid4().hex}",
        "body": body,
        "attributes": {"ApproximateReceiveCount": "1", "SentTimestamp": "1794909600000"},
        "messageAttributes": {"correlation_id": text(correlation_id), "provider": text("stripe")},
        "eventSource": "aws:sqs",
        "eventSourceARN": "arn:aws:sqs:eu-west-2:000000000000:webhook-gateway-test-events",
        "awsRegion": "eu-west-2",
    }


def queued(event: Envelope) -> dict[str, Any]:
    """The record for the message ingest sends for `event`."""
    body = QueueMessage.of(event, b'{"id": "evt"}', {}).model_dump_json()
    return record(body, correlation_id=event.correlation_id)


def batch(*records: dict[str, Any]) -> dict[str, Any]:
    return {"Records": list(records)}


def failed(*records: dict[str, Any]) -> dict[str, Any]:
    """The response that asks SQS to deliver exactly `records` again."""
    return {"batchItemFailures": [{"itemIdentifier": r["messageId"]} for r in records]}


def done(event: Envelope, outcome: str) -> dict[str, str]:
    """How an event's record reads once it has completed with `outcome`."""
    return {"status": "COMPLETED", "outcome": outcome, "event_id": str(event.event_id)}


def table(dynamodb: DynamoDBClient) -> dict[str, dict[str, str]]:
    """Every item in the table, by pk, as the attributes that say what happened to it."""
    shown = ("status", "outcome", "event_id", "state", "last_event_id")
    items = dynamodb.scan(TableName=TABLE, ConsistentRead=True)["Items"]
    return {item["pk"]["S"]: {k: item[k]["S"] for k in shown if k in item} for item in items}


def dead_letters(queue_url: str) -> list[tuple[str, dict[str, str]]]:
    """Each message in the DLQ, as its body and its attributes' values."""
    received = boto3.client("sqs").receive_message(
        QueueUrl=queue_url, MaxNumberOfMessages=10, MessageAttributeNames=["All"]
    )
    return [
        (m["Body"], {k: v["StringValue"] for k, v in m.get("MessageAttributes", {}).items()})
        for m in received.get("Messages", [])
    ]


def lines(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


@pytest.fixture
def subject_writes(dynamodb: DynamoDBClient) -> list[str]:
    """The event_id of every write the processor sends to a SUBJECT item, in order."""
    writes: list[str] = []

    def spy(params: dict[str, Any], **_: Any) -> None:
        if params["Key"]["pk"]["S"].startswith("SUBJECT#"):
            writes.append(params["ExpressionAttributeValues"][":event_id"]["S"])

    dynamodb.meta.events.register("before-parameter-build.dynamodb.UpdateItem", spy)
    return writes


Picks = Callable[[dict[str, Any]], bool]


def fail_once(dynamodb: DynamoDBClient, error: Exception, when: Picks) -> None:
    """Make the next UpdateItem that `when` picks raise `error`, as if boto3 had given up."""
    left = [error]

    def fail(params: dict[str, Any], **_: Any) -> None:
        if left and when(params):
            raise left.pop()

    dynamodb.meta.events.register("before-parameter-build.dynamodb.UpdateItem", fail)


def applying_to(subject_id: str) -> Picks:
    return lambda params: params["Key"]["pk"]["S"] == f"SUBJECT#{subject_id}"


def completing(params: dict[str, Any]) -> bool:
    return ":completed" in params["ExpressionAttributeValues"]


# Applying --------------------------------------------------------------------------------


def test_a_new_event_is_applied_completed_and_acknowledged(
    processor: Processor, dynamodb: DynamoDBClient
) -> None:
    paid = envelope()

    assert processor.handle(batch(queued(paid))) == failed()
    assert table(dynamodb) == {
        "EVENT#stripe:evt_1": done(paid, "applied"),
        "SUBJECT#stripe:pi_1": {"state": "paid", "last_event_id": str(paid.event_id)},
    }


@pytest.mark.parametrize("apart", [False, True], ids=["same-batch", "next-batch"])
def test_a_duplicate_is_acknowledged_and_never_applied(
    processor: Processor, subject_writes: list[str], apart: bool
) -> None:
    first, again = envelope(), envelope()  # one key: the provider sent it twice
    deliveries = [queued(first), queued(again)]
    batches = [batch(d) for d in deliveries] if apart else [batch(*deliveries)]

    assert [processor.handle(b) for b in batches] == [failed()] * len(batches)
    assert subject_writes == [str(first.event_id)]


def test_an_older_event_arriving_later_never_regresses_the_state(
    processor: Processor, dynamodb: DynamoDBClient
) -> None:
    refunded = envelope(EventType.PAYMENT_REFUNDED, START, key="evt_2")
    paid = envelope(EventType.PAYMENT_SUCCEEDED, START - timedelta(minutes=5), key="evt_1")

    processor.handle(batch(queued(refunded)))
    processor.handle(batch(queued(paid)))

    assert table(dynamodb)["SUBJECT#stripe:pi_1"]["state"] == "refunded"
    assert table(dynamodb)["EVENT#stripe:evt_1"]["outcome"] == "stale"


# Failing: only the failed IDs go back ----------------------------------------------------


def test_only_the_failed_message_ids_are_returned(
    processor: Processor, dynamodb: DynamoDBClient, clock: Clock
) -> None:
    good, throttled, busy, also_good = (
        envelope(key=f"evt_{n}", subject=f"pi_{n}") for n in range(1, 5)
    )
    throttling = ClientError({"Error": {"Code": "ThrottlingException"}}, "UpdateItem")
    fail_once(dynamodb, throttling, applying_to("stripe:pi_2"))
    EventRecords(dynamodb, TABLE, clock).claim(busy)  # another invocation is applying it
    records = [queued(event) for event in (good, throttled, busy, also_good)]

    assert processor.handle(batch(*records)) == failed(records[1], records[2])


@pytest.mark.parametrize(
    "error",
    [
        ClientError({"Error": {"Code": "ThrottlingException"}}, "UpdateItem"),
        ReadTimeoutError(endpoint_url="https://dynamodb.eu-west-2.amazonaws.com/"),
        RuntimeError("a bug: not ours to dead-letter"),
    ],
    ids=["throttled", "timed-out", "a-bug"],
)
def test_a_transient_or_unknown_error_is_retried_and_the_retry_applies_it(
    processor: Processor,
    dynamodb: DynamoDBClient,
    dead_letter_queue: str,
    clock: Clock,
    error: Exception,
) -> None:
    paid = queued(envelope())
    fail_once(dynamodb, error, applying_to("stripe:pi_1"))

    assert processor.handle(batch(paid)) == failed(paid)
    assert table(dynamodb)["EVENT#stripe:evt_1"]["status"] == "PROCESSING"
    assert dead_letters(dead_letter_queue) == []

    clock.now += LOCK  # SQS redelivers after 180 s: the lock has long expired
    assert processor.handle(batch(paid)) == failed()
    assert table(dynamodb)["EVENT#stripe:evt_1"]["outcome"] == "applied"


def test_a_retry_after_apply_but_before_complete_still_records_applied(
    processor: Processor, dynamodb: DynamoDBClient, clock: Clock
) -> None:
    paid = queued(envelope())
    fail_once(dynamodb, ConnectionError("the network dropped mid-batch"), completing)

    assert processor.handle(batch(paid)) == failed(paid)
    assert table(dynamodb)["SUBJECT#stripe:pi_1"]["state"] == "paid"  # applied, not completed
    clock.now += LOCK
    assert processor.handle(batch(paid)) == failed()
    assert table(dynamodb)["EVENT#stripe:evt_1"]["outcome"] == "applied"


FUTURE = queued(envelope())["body"].replace('"schema_version":1', '"schema_version":2')
POISON = [
    pytest.param("{not json", "json_invalid at the top level", id="not-json"),
    pytest.param(json.dumps({"hello": "world"}), "missing at envelope", id="not-a-message"),
    pytest.param(FUTURE, "literal_error at envelope.schema_version", id="a-future-envelope"),
]


@pytest.mark.parametrize(("body", "reason"), POISON)
def test_a_poison_message_is_dead_lettered_with_its_reason_then_acknowledged(
    processor: Processor,
    dynamodb: DynamoDBClient,
    dead_letter_queue: str,
    body: str,
    reason: str,
) -> None:
    poison = record(body)

    assert processor.handle(batch(poison)) == failed()
    assert dead_letters(dead_letter_queue) == [
        (
            body,
            {
                "correlation_id": "request-1",
                "provider": "stripe",
                "error_type": "PermanentError",
                "error_reason": f"not a queue message: {reason}",
            },
        )
    ]
    assert table(dynamodb) == {}


def test_a_poison_message_the_dlq_cant_take_is_retried_instead(
    processor: Processor, dead_letter_queue: str
) -> None:
    missing = dataclasses.replace(
        processor, dead_letter_queue_url=dead_letter_queue.replace("dlq", "missing")
    )
    poison = record("{not json")

    assert missing.handle(batch(poison)) == failed(poison)


# The ticket's "done when" ----------------------------------------------------------------


def test_a_mixed_batch_leaves_exactly_the_expected_table_and_dlq(
    processor: Processor, dynamodb: DynamoDBClient, dead_letter_queue: str
) -> None:
    paid = envelope(EventType.PAYMENT_SUCCEEDED, START, key="evt_1")
    paid_again = envelope(EventType.PAYMENT_SUCCEEDED, START, key="evt_1")
    refunded = envelope(EventType.PAYMENT_REFUNDED, START + timedelta(hours=1), key="evt_2")
    failed_late = envelope(EventType.PAYMENT_FAILED, START - timedelta(hours=1), key="evt_3")
    created = envelope(EventType.ORDER_CREATED, START, key="evt_4", subject="pi_2")
    unhandled = envelope(EventType.UNHANDLED, START, key="evt_5", subject="cus_1")
    poison = record("{not json", correlation_id="request-poison")
    records = [queued(e) for e in (paid, paid_again, refunded, failed_late, created, unhandled)]

    response = processor.handle(batch(records[0], poison, *records[1:]))

    assert response == failed()
    assert table(dynamodb) == {
        "EVENT#stripe:evt_1": done(paid, "applied"),  # paid_again was a duplicate
        "EVENT#stripe:evt_2": done(refunded, "applied"),
        "EVENT#stripe:evt_3": done(failed_late, "stale"),  # older than the refund
        "EVENT#stripe:evt_4": done(created, "applied"),
        "EVENT#stripe:evt_5": done(unhandled, "unhandled"),
        "SUBJECT#stripe:pi_1": {"state": "refunded", "last_event_id": str(refunded.event_id)},
        "SUBJECT#stripe:pi_2": {"state": "created", "last_event_id": str(created.event_id)},
    }
    assert dead_letters(dead_letter_queue) == [
        (
            "{not json",
            {
                "correlation_id": "request-poison",
                "provider": "stripe",
                "error_type": "PermanentError",
                "error_reason": "not a queue message: json_invalid at the top level",
            },
        )
    ]


# Logs and wiring -------------------------------------------------------------------------


def test_each_message_logs_one_line_bound_to_its_correlation_id(
    processor: Processor, dynamodb: DynamoDBClient, stream: io.StringIO, clock: Clock
) -> None:
    paid = envelope()
    busy = envelope(key="evt_2")
    EventRecords(dynamodb, TABLE, clock).claim(busy)  # another invocation is applying it
    good, poison = queued(paid), record("{not json", correlation_id="request-poison")

    processor.handle(batch(good, poison, queued(busy), queued(envelope())))

    processed, dead_lettered, waiting, duplicate = lines(stream)
    assert {
        "message": "processed",
        "correlation_id": "request-evt_1",
        "message_id": good["messageId"],
        "provider": "stripe",
        "event_id": str(paid.event_id),
        "claim": "claimed",
        "outcome": "applied",
    }.items() <= processed.items()
    assert {
        "message": "dead_lettered",
        "correlation_id": "request-poison",
        "reason": "not a queue message: json_invalid at the top level",
    }.items() <= dead_lettered.items()
    assert {"message": "busy", "correlation_id": "request-evt_2"}.items() <= waiting.items()
    assert {"message": "processed", "claim": "duplicate"}.items() <= duplicate.items()
    assert "outcome" not in duplicate


@pytest.fixture
def environment(
    dynamodb: DynamoDBClient, dead_letter_queue: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    settings = {
        "STAGE": "dev",
        "PARAMETER_PATH": "/webhook-gateway/dev",
        "QUEUE_URL": dead_letter_queue.replace("dlq", "events"),
        "DEAD_LETTER_QUEUE_URL": dead_letter_queue,
        "TABLE_NAME": TABLE,
        "ARCHIVE_BUCKET": "webhook-gateway-dev-archive",
    }
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    processor_from_environment.cache_clear()  # built from this test's settings, not another's
    yield
    processor_from_environment.cache_clear()


def test_the_entry_point_builds_one_processor_from_the_settings(
    environment: None, dynamodb: DynamoDBClient, dead_letter_queue: str
) -> None:
    paid, poison = queued(envelope()), record("{not json")

    assert handler(batch(paid), None) == failed()
    assert handler(batch(poison), None) == failed()
    assert processor_from_environment.cache_info().misses == 1  # built once, used twice
    assert table(dynamodb)["EVENT#stripe:evt_1"]["outcome"] == "applied"
    assert len(dead_letters(dead_letter_queue)) == 1
