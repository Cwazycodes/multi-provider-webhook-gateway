"""The gateway's errors, and the one place that decides what each means to a caller.

Raise them with a message for the logs. Ingest turns them into HTTP responses
(design note section 5); the processor turns them into retries or dead-letters.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus
from typing import Final


class GatewayError(Exception):
    """Base for every error the gateway raises on purpose."""


class UnknownProvider(GatewayError):
    """The path names a provider the gateway doesn't know."""


class BodyTooLarge(GatewayError):
    """The body is over the size limit, so it was never hashed."""


class SignatureError(GatewayError):
    """Signature missing or wrong, or timestamp out of tolerance. The caller never learns which."""


class PayloadError(GatewayError):
    """The signature is valid but the body isn't JSON."""


class SchemaError(GatewayError):
    """Valid JSON that fails the provider's schema at `field`.

    Raise it `from None`. A chained pydantic ValidationError would put the
    rejected input, a piece of the raw body, into any logged traceback.
    """

    def __init__(self, field: str) -> None:
        super().__init__(f"schema invalid at {field}")
        self.field = field


class TransientError(GatewayError):
    """A dependency failed in a way that a retry may fix."""


class PermanentError(GatewayError):
    """A retry would fail the same way."""


@dataclass(frozen=True)
class ErrorResponse:
    status: HTTPStatus
    body: Mapping[str, str]


class Disposition(StrEnum):
    RETRY = "retry"  # a batch item failure: SQS redelivers, and dead-letters after 5 receives
    DEAD_LETTER = "dead_letter"  # sent to the DLQ now, with the error attached


# Design note section 5, top to bottom: the error, its status and the body's "error".
_RESPONSES: Final[tuple[tuple[type[GatewayError], HTTPStatus, str], ...]] = (
    (UnknownProvider, HTTPStatus.NOT_FOUND, "unknown_provider"),
    (BodyTooLarge, HTTPStatus.CONTENT_TOO_LARGE, "too_large"),
    (SignatureError, HTTPStatus.UNAUTHORIZED, "unauthorized"),
    (PayloadError, HTTPStatus.BAD_REQUEST, "malformed_json"),
    (SchemaError, HTTPStatus.UNPROCESSABLE_CONTENT, "schema_invalid"),
    (TransientError, HTTPStatus.SERVICE_UNAVAILABLE, "try_again"),
)


def error_response(error: Exception) -> ErrorResponse:
    """What ingest answers when handling a request raised `error`."""
    for error_type, status, code in _RESPONSES:
        if isinstance(error, error_type):
            body = {"error": code}
            if isinstance(error, SchemaError):
                body["field"] = error.field
            return ErrorResponse(status, body)
    # PermanentError or a bug: the gateway's own failure, so a 5xx the provider retries.
    return ErrorResponse(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_error"})


def disposition(error: Exception) -> Disposition:
    """What the processor does with a message whose processing raised `error`."""
    if isinstance(error, GatewayError) and not isinstance(error, TransientError):
        return Disposition.DEAD_LETTER
    return Disposition.RETRY
