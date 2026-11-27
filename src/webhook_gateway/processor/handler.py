"""The processor function: claim, apply and complete each message of an SQS batch.

One message's failure never makes the others retry: the response names only the
messages SQS should deliver again (design note section 4).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Any

from aws_lambda_powertools import Logger
from pydantic import ValidationError

from webhook_gateway.common.clients import make_dynamodb_client, make_sqs_client
from webhook_gateway.common.errors import Disposition, PermanentError, disposition
from webhook_gateway.common.log import make_logger
from webhook_gateway.common.message import QueueMessage
from webhook_gateway.common.settings import Settings
from webhook_gateway.processor.idempotency import Claim, EventRecords
from webhook_gateway.processor.subjects import Subjects

if TYPE_CHECKING:  # boto3-stubs is a dev dependency; Lambda doesn't have it
    from mypy_boto3_sqs import SQSClient
    from mypy_boto3_sqs.type_defs import MessageAttributeValueTypeDef


@dataclass(frozen=True)
class Processor:
    """Everything the processor depends on, passed in, so tests can use moto."""

    records: EventRecords
    subjects: Subjects
    sqs: SQSClient
    dead_letter_queue_url: str
    logger: Logger

    def handle(self, event: Mapping[str, Any]) -> dict[str, Any]:
        """One batch from Lambda's SQS trigger in; the messages to deliver again out."""
        retry = [record["messageId"] for record in event["Records"] if not self._settle(record)]
        return {"batchItemFailures": [{"itemIdentifier": message_id} for message_id in retry]}

    def _settle(self, record: Mapping[str, Any]) -> bool:
        """Process one message. True means SQS can delete it; False, deliver it again."""
        with self.logger.append_context_keys(
            message_id=record["messageId"],
            correlation_id=_attribute(record, "correlation_id"),
            provider=_attribute(record, "provider"),
        ):
            try:
                envelope = decode(record["body"]).envelope
                run = self.records.run_once(envelope, self.subjects.apply)
            except Exception as error:  # every failure stays with its own message: see _failed
                return self._failed(record, error)
            summary = {
                "event_id": str(envelope.event_id),
                "event_type": envelope.event_type,
                "claim": run.claim,
                "outcome": run.outcome,  # None, so left out, unless this invocation applied it
            }
            if run.claim is Claim.BUSY:  # another invocation holds a live lock: try again later
                self.logger.info("busy", extra=summary)
                return False
            self.logger.info("processed", extra=summary)
            return True

    def _failed(self, record: Mapping[str, Any], error: Exception) -> bool:
        if disposition(error) is Disposition.RETRY:
            self.logger.exception("retrying")
            return False
        try:
            self._dead_letter(record, error)
        except Exception:  # the message isn't parked anywhere yet, so SQS must keep it
            self.logger.exception("retrying", extra={"reason": "the DLQ refused the message"})
            return False
        self.logger.error("dead_lettered", extra={"reason": str(error)})
        return True

    def _dead_letter(self, record: Mapping[str, Any], error: Exception) -> None:
        """Park the message as it arrived, with ingest's attributes and why it failed."""
        attributes: dict[str, MessageAttributeValueTypeDef] = {
            name: {"DataType": value["dataType"], "StringValue": value["stringValue"]}
            for name, value in record.get("messageAttributes", {}).items()
            if "stringValue" in value
        }
        attributes["error_type"] = {"DataType": "String", "StringValue": type(error).__name__}
        attributes["error_reason"] = {"DataType": "String", "StringValue": str(error)}
        self.sqs.send_message(
            QueueUrl=self.dead_letter_queue_url,
            MessageBody=record["body"],
            MessageAttributes=attributes,
        )


def decode(body: str) -> QueueMessage:
    """The message ingest sent, or a PermanentError: a retry would read the same bytes."""
    try:
        return QueueMessage.model_validate_json(body)
    except ValidationError as error:
        first = error.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "the top level"
        # from None: a chained ValidationError would carry pieces of the body into the logs.
        raise PermanentError(f"not a queue message: {first['type']} at {where}") from None


def _attribute(record: Mapping[str, Any], name: str) -> str | None:
    attribute: Mapping[str, Any] = record.get("messageAttributes", {}).get(name, {})
    return attribute.get("stringValue")


def create_processor(settings: Settings) -> Processor:
    """The processor's dependencies, built once per cold start."""
    dynamodb = make_dynamodb_client()
    return Processor(
        records=EventRecords(dynamodb, settings.table_name),
        subjects=Subjects(dynamodb, settings.table_name),
        sqs=make_sqs_client(),
        dead_letter_queue_url=settings.dead_letter_queue_url,
        logger=make_logger("processor", settings.stage),
    )


@cache
def processor_from_environment() -> Processor:
    """The processor this container uses: built on its first batch, then kept."""
    return create_processor(Settings())


def handler(event: Mapping[str, Any], context: object) -> dict[str, Any]:
    """The entry point template.yaml names."""
    return processor_from_environment().handle(event)
