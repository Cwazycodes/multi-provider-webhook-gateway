"""The idempotent claim: an event delivered five times is applied once (design note section 7).

Each event has one record, EVENT#<idempotency_key>, in the table template.yaml declares.
An invocation claims the record before applying the event and completes it afterwards.
Claiming and taking over are conditional writes, so of two invocations holding the same
event, only one applies it.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, NamedTuple

from botocore.exceptions import ClientError

from webhook_gateway.common.clock import utc_now, utc_text
from webhook_gateway.common.envelope import Envelope

if TYPE_CHECKING:  # boto3-stubs is a dev dependency; Lambda doesn't have it
    from mypy_boto3_dynamodb import DynamoDBClient
    from mypy_boto3_dynamodb.type_defs import AttributeValueTypeDef

LOCK = timedelta(seconds=60)  # design note section 9: longer than the processor's 30 s timeout
KEEP = timedelta(days=7)  # longer than any provider retries: Stripe's last 3 days


class Status(StrEnum):
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"


class Claim(StrEnum):
    """What claiming an event found, and so what the processor does with the message."""

    CLAIMED = "claimed"  # a new event: apply it
    TAKEN_OVER = "taken_over"  # an earlier attempt died holding it: apply it
    DUPLICATE = "duplicate"  # applied already: acknowledge the message and skip it
    BUSY = "busy"  # another invocation holds a live lock: retry the message later


class Outcome(StrEnum):
    """What applying an event did (design note section 8), kept when it completes."""

    APPLIED = "applied"
    STALE = "stale"
    UNHANDLED = "unhandled"


class Run(NamedTuple):
    """What run_once did: the claim it found and, if it applied the event, the outcome."""

    claim: Claim
    outcome: Outcome | None = None


def epoch(moment: datetime) -> int:
    """Whole seconds since 1970: what DynamoDB's TTL reads, and what locks are compared in."""
    return int(moment.timestamp())


@dataclass(frozen=True)
class EventRecords:
    """The event records in one table, written only through conditional writes."""

    dynamodb: DynamoDBClient
    table_name: str
    clock: Callable[[], datetime] = utc_now

    def run_once(self, envelope: Envelope, apply: Callable[[Envelope], Outcome]) -> Run:
        """Claim, apply, complete: `apply` runs only while this invocation holds the claim.

        If `apply` raises, the record stays PROCESSING, and once its lock expires the
        next delivery takes it over.
        """
        claim = self.claim(envelope)
        if claim not in (Claim.CLAIMED, Claim.TAKEN_OVER):
            return Run(claim)
        outcome = apply(envelope)
        self.complete(envelope, outcome)
        return Run(claim, outcome)

    def claim(self, envelope: Envelope) -> Claim:
        now = self.clock()
        try:
            self.dynamodb.put_item(
                TableName=self.table_name,
                Item={
                    **_key(envelope),
                    "status": {"S": Status.PROCESSING},
                    "lock_expires_at": {"N": str(epoch(now + LOCK))},
                    "event_id": {"S": str(envelope.event_id)},
                    "event_type": {"S": envelope.event_type},
                    "subject_id": {"S": envelope.subject_id},
                    "occurred_at": {"S": utc_text(envelope.occurred_at)},
                },
                ConditionExpression="attribute_not_exists(pk)",
                # On failure, return the record that is there: no second read, and no race.
                ReturnValuesOnConditionCheckFailure="ALL_OLD",
            )
        except self.dynamodb.exceptions.ConditionalCheckFailedException as error:
            existing = _existing(error)
        else:
            return Claim.CLAIMED
        if existing["status"]["S"] == Status.COMPLETED:
            return Claim.DUPLICATE
        lock = existing["lock_expires_at"]["N"]
        if int(lock) > epoch(now):
            return Claim.BUSY
        return self._take_over(envelope, lock, now)

    def _take_over(self, envelope: Envelope, lock: str, now: datetime) -> Claim:
        try:
            self.dynamodb.update_item(
                TableName=self.table_name,
                Key=_key(envelope),
                UpdateExpression="SET lock_expires_at = :new_lock",
                # Only if the lock is still the expired one read above: of two invocations
                # that both read it, the first to write wins and the other must retry.
                ConditionExpression="#status = :processing AND lock_expires_at = :old_lock",
                ExpressionAttributeNames={"#status": "status"},  # a reserved word
                ExpressionAttributeValues={
                    ":new_lock": {"N": str(epoch(now + LOCK))},
                    ":processing": {"S": Status.PROCESSING},
                    ":old_lock": {"N": lock},
                },
            )
        except self.dynamodb.exceptions.ConditionalCheckFailedException:
            return Claim.BUSY
        return Claim.TAKEN_OVER

    def complete(self, envelope: Envelope, outcome: Outcome) -> None:
        now = self.clock()
        self.dynamodb.update_item(
            TableName=self.table_name,
            Key=_key(envelope),
            UpdateExpression=(
                "SET #status = :completed, outcome = :outcome, processed_at = :now,"
                " expires_at = :expires_at REMOVE lock_expires_at"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":completed": {"S": Status.COMPLETED},
                ":outcome": {"S": outcome},
                ":now": {"S": utc_text(now)},
                ":expires_at": {"N": str(epoch(now + KEEP))},
            },
        )


def _key(envelope: Envelope) -> dict[str, AttributeValueTypeDef]:
    return {"pk": {"S": f"EVENT#{envelope.idempotency_key}"}}


def _existing(error: ClientError) -> Mapping[str, AttributeValueTypeDef]:
    # boto3 puts the record that failed the condition here, though its stubs don't say so.
    response: Mapping[str, Any] = error.response
    existing: Mapping[str, AttributeValueTypeDef] = response["Item"]
    return existing
