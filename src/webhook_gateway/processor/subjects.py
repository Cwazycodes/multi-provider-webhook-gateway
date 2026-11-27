"""Applying an event: each subject keeps the state its newest event left (design note section 7).

Events about one subject can arrive in any order, so SUBJECT#<subject_id> changes only
when an event is newer than the last one applied. A late delivery never overwrites newer
state.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final

from webhook_gateway.common.clock import utc_now, utc_text
from webhook_gateway.common.envelope import Envelope, EventType
from webhook_gateway.processor.idempotency import Outcome

if TYPE_CHECKING:  # boto3-stubs is a dev dependency; Lambda doesn't have it
    from mypy_boto3_dynamodb import DynamoDBClient

# The state each event type leaves its subject in. An unhandled event changes nothing.
STATES: Final[Mapping[EventType, str]] = {
    EventType.CODE_PUSHED: "pushed",
    EventType.PULL_REQUEST_MERGED: "merged",
    EventType.ORDER_CREATED: "created",
    EventType.ORDER_PAID: "paid",
    EventType.PAYMENT_SUCCEEDED: "paid",
    EventType.PAYMENT_FAILED: "failed",
    EventType.PAYMENT_REFUNDED: "refunded",
}


@dataclass(frozen=True)
class Subjects:
    """The subject states in one table, each written only by a newer event."""

    dynamodb: DynamoDBClient
    table_name: str
    clock: Callable[[], datetime] = utc_now

    def apply(self, envelope: Envelope) -> Outcome:
        """Make `envelope` its subject's latest event, unless the subject has a newer one."""
        state = STATES.get(envelope.event_type)
        if state is None:
            return Outcome.UNHANDLED
        try:
            self.dynamodb.update_item(
                TableName=self.table_name,
                Key={"pk": {"S": f"SUBJECT#{envelope.subject_id}"}},
                UpdateExpression=(
                    "SET #state = :state, last_event_type = :event_type,"
                    " last_occurred_at = :occurred_at, last_event_id = :event_id,"
                    " updated_at = :now"
                ),
                # Newer wins, and a tie keeps what is there. The last clause lets a retry after
                # a crash apply again the event it already applied, so it is still `applied`.
                ConditionExpression=(
                    "attribute_not_exists(last_occurred_at) OR last_occurred_at < :occurred_at"
                    " OR last_event_id = :event_id"
                ),
                ExpressionAttributeNames={"#state": "state"},  # a reserved word
                ExpressionAttributeValues={
                    ":state": {"S": state},
                    ":event_type": {"S": envelope.event_type},
                    ":occurred_at": {"S": utc_text(envelope.occurred_at)},
                    ":event_id": {"S": str(envelope.event_id)},
                    ":now": {"S": utc_text(self.clock())},
                },
            )
        except self.dynamodb.exceptions.ConditionalCheckFailedException:
            return Outcome.STALE
        return Outcome.APPLIED
