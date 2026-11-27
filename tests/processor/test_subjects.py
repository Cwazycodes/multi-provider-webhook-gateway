"""Applying an event: newest wins per subject, whatever order deliveries arrive in (WG-16)."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from mypy_boto3_dynamodb import DynamoDBClient

from webhook_gateway.common.envelope import Envelope, EventType, Provider
from webhook_gateway.processor.idempotency import Outcome
from webhook_gateway.processor.subjects import STATES, Subjects

TABLE = "webhook-gateway-test"  # the table conftest.py makes
START = datetime(2026, 11, 17, 10, 0, 0, tzinfo=UTC)
NOW = START + timedelta(minutes=30)


@pytest.fixture
def subjects(dynamodb: DynamoDBClient) -> Subjects:
    return Subjects(dynamodb, TABLE, clock=lambda: NOW)


def event(event_type: EventType, occurred_at: datetime, subject: str = "pi_1") -> Envelope:
    """A Stripe event about payment intent `subject`, with a key of its own."""
    key = f"evt_{uuid4().hex[:12]}"
    return Envelope(
        schema_version=1,
        event_id=uuid4(),
        idempotency_key=f"stripe:{key}",
        provider=Provider.STRIPE,
        provider_event_id=key,
        event_type=event_type,
        subject_id=f"stripe:{subject}",
        occurred_at=occurred_at,
        received_at=START + timedelta(minutes=10),
        correlation_id="request-1",
        data={},
    )


def subject(dynamodb: DynamoDBClient, subject_id: str = "stripe:pi_1") -> dict[str, Any]:
    """The subject's item as it is now, or {} if there is none."""
    key: dict[str, Any] = {"pk": {"S": f"SUBJECT#{subject_id}"}}
    return dict(dynamodb.get_item(TableName=TABLE, Key=key, ConsistentRead=True).get("Item", {}))


def test_the_first_event_about_a_subject_records_its_state(
    subjects: Subjects, dynamodb: DynamoDBClient
) -> None:
    paid = event(EventType.PAYMENT_SUCCEEDED, START)

    assert subjects.apply(paid) is Outcome.APPLIED
    assert subject(dynamodb) == {
        "pk": {"S": "SUBJECT#stripe:pi_1"},
        "state": {"S": "paid"},
        "last_event_type": {"S": "payment.succeeded"},
        "last_occurred_at": {"S": "2026-11-17T10:00:00.000000Z"},
        "last_event_id": {"S": str(paid.event_id)},
        "updated_at": {"S": "2026-11-17T10:30:00.000000Z"},
    }


# Microseconds, half a second and a day: fixed-width text sorts as the moments do (WG-07).
@pytest.mark.parametrize(
    "later",
    [timedelta(microseconds=1), timedelta(milliseconds=500), timedelta(days=1)],
    ids=["1us", "half-second", "1-day"],
)
def test_a_newer_event_replaces_the_state(
    subjects: Subjects, dynamodb: DynamoDBClient, later: timedelta
) -> None:
    subjects.apply(event(EventType.PAYMENT_SUCCEEDED, START))
    refunded = event(EventType.PAYMENT_REFUNDED, START + later)

    assert subjects.apply(refunded) is Outcome.APPLIED
    assert subject(dynamodb)["state"] == {"S": "refunded"}
    assert subject(dynamodb)["last_event_id"] == {"S": str(refunded.event_id)}


@pytest.mark.parametrize(
    "earlier",
    [timedelta(microseconds=1), timedelta(milliseconds=500), timedelta(days=1), timedelta(0)],
    ids=["1us", "half-second", "1-day", "a-tie"],
)
def test_an_older_or_tied_event_arriving_late_is_stale_and_changes_nothing(
    subjects: Subjects, dynamodb: DynamoDBClient, earlier: timedelta
) -> None:
    subjects.apply(event(EventType.PAYMENT_REFUNDED, START))
    before = subject(dynamodb)

    assert subjects.apply(event(EventType.PAYMENT_SUCCEEDED, START - earlier)) is Outcome.STALE
    assert subject(dynamodb) == before


def test_applying_the_same_event_again_is_still_applied(
    subjects: Subjects, dynamodb: DynamoDBClient
) -> None:
    # A crash between apply and complete: the retry re-applies the event it applied itself.
    paid = event(EventType.PAYMENT_SUCCEEDED, START)
    subjects.apply(paid)
    before = subject(dynamodb)

    assert subjects.apply(paid) is Outcome.APPLIED
    assert subject(dynamodb) == before


def test_subjects_are_ordered_independently(subjects: Subjects, dynamodb: DynamoDBClient) -> None:
    subjects.apply(event(EventType.PAYMENT_REFUNDED, START, subject="pi_1"))
    older_but_elsewhere = event(EventType.PAYMENT_FAILED, START - timedelta(hours=1), "pi_2")

    assert subjects.apply(older_but_elsewhere) is Outcome.APPLIED
    assert subject(dynamodb, "stripe:pi_2")["state"] == {"S": "failed"}


def test_an_unhandled_event_is_recorded_as_such_and_changes_no_state(
    subjects: Subjects, dynamodb: DynamoDBClient
) -> None:
    assert subjects.apply(event(EventType.UNHANDLED, START)) is Outcome.UNHANDLED
    assert subject(dynamodb) == {}


def test_every_handled_event_type_leaves_its_subject_in_a_state() -> None:
    assert set(STATES) == set(EventType) - {EventType.UNHANDLED}
