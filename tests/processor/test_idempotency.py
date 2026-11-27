"""The idempotent claim, against the template's own table in moto (WG-15).

Each test plays one or two processor invocations ("workers") that share the table and a
clock. Every envelope() is a fresh delivery of the same event: a new event_id, one key.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import boto3
import pytest
from botocore.exceptions import ClientError
from cfnlint.decode import decode_str
from mypy_boto3_dynamodb import DynamoDBClient

from webhook_gateway.common.envelope import Envelope, EventType, Provider
from webhook_gateway.processor.idempotency import Claim, EventRecords, Outcome

TEMPLATE = Path(__file__).resolve().parents[2] / "template.yaml"
TABLE = "webhook-gateway-test"
START = datetime(2026, 11, 17, 10, 0, 0, tzinfo=UTC)
LOCK = timedelta(seconds=60)  # design note section 9; the module's constant isn't trusted here


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


class Step:
    """A stand-in for applying an event, which counts its runs and can crash."""

    def __init__(self, outcome: Outcome = Outcome.APPLIED, crash: bool = False) -> None:
        self.outcome = outcome
        self.crash = crash
        self.runs = 0

    def __call__(self, envelope: Envelope) -> Outcome:
        self.runs += 1
        if self.crash:
            raise RuntimeError("the processor died mid-apply")
        return self.outcome


def the_templates_table() -> dict[str, Any]:
    decoded, matches = decode_str(TEMPLATE.read_text())
    assert decoded is not None, matches
    properties: dict[str, Any] = decoded["Resources"]["EventsTable"]["Properties"]
    return properties


@pytest.fixture
def dynamodb(aws: None) -> DynamoDBClient:
    client = boto3.client("dynamodb")
    table = the_templates_table()
    client.create_table(
        TableName=TABLE,
        KeySchema=table["KeySchema"],
        AttributeDefinitions=table["AttributeDefinitions"],
        BillingMode=table["BillingMode"],
    )
    return client


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def worker(dynamodb: DynamoDBClient, clock: Clock) -> EventRecords:
    return EventRecords(dynamodb, TABLE, clock)


@pytest.fixture
def other_worker(dynamodb: DynamoDBClient, clock: Clock) -> EventRecords:
    """A second invocation, working on the same table at the same moment."""
    return EventRecords(dynamodb, TABLE, clock)


def envelope() -> Envelope:
    return Envelope(
        schema_version=1,
        event_id=uuid4(),
        idempotency_key="stripe:evt_1",
        provider=Provider.STRIPE,
        provider_event_id="evt_1",
        event_type=EventType.PAYMENT_SUCCEEDED,
        subject_id="stripe:pi_1",
        occurred_at=START - timedelta(seconds=5),
        received_at=START - timedelta(seconds=1),
        correlation_id="request-1",
        data={},
    )


def record(dynamodb: DynamoDBClient) -> dict[str, Any]:
    """The event's record as it is now, or {} if there is none."""
    key: dict[str, Any] = {"pk": {"S": "EVENT#stripe:evt_1"}}
    return dict(dynamodb.get_item(TableName=TABLE, Key=key, ConsistentRead=True).get("Item", {}))


def epoch(moment: datetime) -> dict[str, str]:
    return {"N": str(int(moment.timestamp()))}


# Claiming --------------------------------------------------------------------------------


def test_the_first_claim_wins_the_event_and_locks_it_for_60_seconds(
    worker: EventRecords, dynamodb: DynamoDBClient
) -> None:
    first = envelope()

    assert worker.claim(first) is Claim.CLAIMED
    assert record(dynamodb) == {
        "pk": {"S": "EVENT#stripe:evt_1"},
        "status": {"S": "PROCESSING"},
        "lock_expires_at": epoch(START + LOCK),
        "event_id": {"S": str(first.event_id)},
        "event_type": {"S": "payment.succeeded"},
        "subject_id": {"S": "stripe:pi_1"},
        "occurred_at": {"S": "2026-11-17T09:59:55.000000Z"},
    }


@pytest.mark.parametrize("later", [timedelta(0), LOCK - timedelta(seconds=1)])
def test_a_claim_while_the_lock_is_live_must_retry_later(
    worker: EventRecords,
    other_worker: EventRecords,
    dynamodb: DynamoDBClient,
    clock: Clock,
    later: timedelta,
) -> None:
    worker.claim(envelope())
    held = record(dynamodb)
    clock.now += later

    assert other_worker.claim(envelope()) is Claim.BUSY
    assert record(dynamodb) == held  # the lock is neither moved nor stolen


@pytest.mark.parametrize("later", [LOCK, LOCK + timedelta(minutes=5)])
def test_a_claim_after_the_lock_expires_takes_the_event_over(
    worker: EventRecords,
    other_worker: EventRecords,
    dynamodb: DynamoDBClient,
    clock: Clock,
    later: timedelta,
) -> None:
    worker.claim(envelope())
    held = record(dynamodb)
    clock.now += later

    assert other_worker.claim(envelope()) is Claim.TAKEN_OVER
    assert record(dynamodb) == held | {"lock_expires_at": epoch(clock.now + LOCK)}


def test_of_two_that_find_the_same_expired_lock_only_one_takes_over(
    worker: EventRecords, other_worker: EventRecords, clock: Clock
) -> None:
    worker.claim(envelope())
    clock.now += LOCK
    third = EventRecords(boto3.client("dynamodb"), TABLE, clock)  # a client of its own
    results: list[Claim] = []

    def other_goes_first(**_: Any) -> None:
        results.append(other_worker.claim(envelope()))

    # The third worker has read the expired lock; the other takes over before it writes.
    third.dynamodb.meta.events.register("before-call.dynamodb.UpdateItem", other_goes_first)
    results.append(third.claim(envelope()))

    assert results == [Claim.TAKEN_OVER, Claim.BUSY]


def test_a_claim_on_a_completed_event_is_a_duplicate(
    worker: EventRecords,
    other_worker: EventRecords,
    dynamodb: DynamoDBClient,
    clock: Clock,
) -> None:
    worker.claim(first := envelope())
    worker.complete(first, Outcome.APPLIED)
    done = record(dynamodb)
    clock.now += LOCK * 10  # a completed record has no lock to expire

    assert other_worker.claim(envelope()) is Claim.DUPLICATE
    assert record(dynamodb) == done


def test_other_errors_are_not_mistaken_for_a_lost_claim(dynamodb: DynamoDBClient) -> None:
    missing = EventRecords(dynamodb, "no-such-table")

    with pytest.raises(ClientError, match="ResourceNotFoundException"):
        missing.claim(envelope())


# Completing ------------------------------------------------------------------------------


def test_completing_keeps_the_outcome_for_7_days_and_drops_the_lock(
    worker: EventRecords, dynamodb: DynamoDBClient, clock: Clock
) -> None:
    worker.claim(first := envelope())
    clock.now += timedelta(seconds=2)
    worker.complete(first, Outcome.STALE)

    assert record(dynamodb) == {
        "pk": {"S": "EVENT#stripe:evt_1"},
        "status": {"S": "COMPLETED"},
        "event_id": {"S": str(first.event_id)},
        "event_type": {"S": "payment.succeeded"},
        "subject_id": {"S": "stripe:pi_1"},
        "occurred_at": {"S": "2026-11-17T09:59:55.000000Z"},
        "outcome": {"S": "stale"},
        "processed_at": {"S": "2026-11-17T10:00:02.000000Z"},
        "expires_at": epoch(clock.now + timedelta(days=7)),
    }


def test_the_record_expires_by_the_attribute_the_table_has_ttl_on(
    worker: EventRecords, dynamodb: DynamoDBClient
) -> None:
    ttl = the_templates_table()["TimeToLiveSpecification"]
    worker.claim(first := envelope())
    worker.complete(first, Outcome.APPLIED)

    assert ttl["Enabled"] is True
    assert ttl["AttributeName"] in record(dynamodb)


# Claim, apply, complete: what the processor runs for each event --------------------------


def test_an_event_delivered_five_times_is_applied_once(worker: EventRecords) -> None:
    apply = Step()

    claims = [worker.run_once(envelope(), apply) for _ in range(5)]

    assert claims == [Claim.CLAIMED] + [Claim.DUPLICATE] * 4
    assert apply.runs == 1


def test_a_delivery_that_arrives_mid_apply_never_applies_it(
    worker: EventRecords, other_worker: EventRecords, dynamodb: DynamoDBClient
) -> None:
    second_apply = Step()
    seen: list[Claim] = []

    def apply_while_another_delivery_arrives(event: Envelope) -> Outcome:
        seen.append(other_worker.run_once(envelope(), second_apply))
        return Outcome.APPLIED

    assert worker.run_once(envelope(), apply_while_another_delivery_arrives) is Claim.CLAIMED
    assert seen == [Claim.BUSY]
    assert second_apply.runs == 0
    assert record(dynamodb)["status"] == {"S": "COMPLETED"}


def test_after_a_crash_the_event_is_applied_once_the_lock_expires(
    worker: EventRecords,
    other_worker: EventRecords,
    dynamodb: DynamoDBClient,
    clock: Clock,
) -> None:
    apply = Step()
    with pytest.raises(RuntimeError):
        worker.run_once(envelope(), Step(crash=True))

    assert other_worker.run_once(envelope(), apply) is Claim.BUSY
    clock.now += LOCK
    assert other_worker.run_once(envelope(), apply) is Claim.TAKEN_OVER
    assert worker.run_once(envelope(), apply) is Claim.DUPLICATE
    assert apply.runs == 1
    assert record(dynamodb)["outcome"] == {"S": "applied"}


@pytest.mark.parametrize("outcome", list(Outcome))
def test_the_record_keeps_what_applying_returned(
    worker: EventRecords, dynamodb: DynamoDBClient, outcome: Outcome
) -> None:
    worker.run_once(envelope(), Step(outcome))

    assert record(dynamodb)["outcome"] == {"S": outcome.value}
