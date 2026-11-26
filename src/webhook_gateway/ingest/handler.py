"""The ingest function: verify, normalise and queue one webhook, then answer 202 at once."""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import boto3
from aws_lambda_powertools import Logger
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from webhook_gateway.common.clock import utc_now
from webhook_gateway.common.envelope import Envelope, Provider
from webhook_gateway.common.errors import (
    BodyTooLarge,
    TransientError,
    UnknownProvider,
    error_response,
)
from webhook_gateway.common.log import make_logger
from webhook_gateway.common.message import QueueMessage
from webhook_gateway.common.settings import Settings
from webhook_gateway.ingest.secrets import CachedSecrets, SecretSource
from webhook_gateway.providers.normalising import Receipt
from webhook_gateway.providers.registry import PROVIDERS
from webhook_gateway.providers.signatures import SignedRequest

if TYPE_CHECKING:  # boto3-stubs is a dev dependency; Lambda doesn't have it
    from mypy_boto3_sqs import SQSClient

MAX_BODY_BYTES = 128 * 1024  # design note section 5


@dataclass(frozen=True)
class Ingest:
    """Everything ingest depends on, passed in, so tests need no AWS."""

    queue_url: str
    sqs: SQSClient
    secrets: SecretSource
    logger: Logger
    clock: Callable[[], datetime] = utc_now

    def handle(self, event: Mapping[str, Any]) -> dict[str, Any]:
        """One HTTP API (payload 2.0) request in, one response out."""
        received_at = self.clock()
        correlation_id: str = event["requestContext"]["requestId"]
        slug: str = (event.get("pathParameters") or {}).get("provider", "")
        receipt = Receipt(uuid4(), received_at, correlation_id)
        with self.logger.append_context_keys(provider=slug, correlation_id=correlation_id):
            try:
                envelope = self._accept(event, slug, receipt)
            except Exception as error:  # every failure becomes a response: see _reject
                return self._reject(error)
            event_id = str(envelope.event_id)
            self.logger.info(
                "accepted", extra={"event_id": event_id, "event_type": envelope.event_type}
            )
            return _response(HTTPStatus.ACCEPTED, {"event_id": event_id})

    def _accept(self, event: Mapping[str, Any], slug: str, receipt: Receipt) -> Envelope:
        # Design note section 5, in table order: the first failure decides the response.
        try:
            provider = Provider(slug)
        except ValueError:
            raise UnknownProvider(slug) from None
        adapter = PROVIDERS[provider]
        request = SignedRequest.from_apigw_event(event)
        if len(request.body) > MAX_BODY_BYTES:  # before any hashing
            raise BodyTooLarge(f"{len(request.body)} bytes")
        adapter.verifier.verify(request, self.secrets.secrets(provider), receipt.received_at)
        envelope = adapter.normalise(request, receipt)
        kept = {k: v for k, v in request.headers.items() if k in adapter.archived_headers}
        self._send(QueueMessage.of(envelope, request.body, kept))
        return envelope

    def _send(self, message: QueueMessage) -> None:
        envelope = message.envelope
        try:
            self.sqs.send_message(
                QueueUrl=self.queue_url,
                MessageBody=message.model_dump_json(),
                MessageAttributes={
                    "correlation_id": {
                        "DataType": "String",
                        "StringValue": envelope.correlation_id,
                    },
                    "provider": {"DataType": "String", "StringValue": envelope.provider},
                },
            )
        except (BotoCoreError, ClientError) as error:
            # boto3 has already retried: tell the provider to retry later instead.
            raise TransientError(f"SQS send failed after retries: {error}") from error

    def _reject(self, error: Exception) -> dict[str, Any]:
        response = error_response(error)
        if response.status >= HTTPStatus.INTERNAL_SERVER_ERROR:
            self.logger.exception("failed", extra={"reason": response.body["error"]})
        else:
            self.logger.info(
                "rejected", extra={"reason": response.body["error"], "detail": str(error)}
            )
        return _response(response.status, response.body)


def _response(status: HTTPStatus, body: Mapping[str, str]) -> dict[str, Any]:
    return {
        "statusCode": int(status),
        "headers": {"content-type": "application/json"},
        "body": json.dumps(dict(body)),
    }


def make_sqs_client() -> SQSClient:
    # Design note section 9: standard mode, 3 attempts in all. Config's max_attempts
    # would count retries after the first try, so it would mean 4.
    return boto3.client("sqs", config=Config(retries={"mode": "standard", "total_max_attempts": 3}))


def create_ingest(settings: Settings, secrets: SecretSource) -> Ingest:
    """Ingest's dependencies, built once per cold start. WG-24 passes the SSM source."""
    return Ingest(
        queue_url=settings.queue_url,
        sqs=make_sqs_client(),
        secrets=CachedSecrets(secrets),
        logger=make_logger("ingest", settings.stage),
    )
