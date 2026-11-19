"""Typed errors, and the one place that maps them to responses or retries (WG-08)."""

from http import HTTPStatus

import pytest

from webhook_gateway.common.errors import (
    BodyTooLarge,
    Disposition,
    ErrorResponse,
    PayloadError,
    PermanentError,
    SchemaError,
    SignatureError,
    TransientError,
    UnknownProvider,
    disposition,
    error_response,
)


# Design note section 5, row by row. The messages are for logs: none reaches a body.
@pytest.mark.parametrize(
    ("error", "status", "body"),
    [
        (UnknownProvider("gitlab"), HTTPStatus.NOT_FOUND, {"error": "unknown_provider"}),
        (BodyTooLarge("131073 bytes"), HTTPStatus.CONTENT_TOO_LARGE, {"error": "too_large"}),
        (SignatureError("v1 mismatch"), HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"}),
        (PayloadError("Expecting value"), HTTPStatus.BAD_REQUEST, {"error": "malformed_json"}),
        (
            SchemaError("data.object.amount"),
            HTTPStatus.UNPROCESSABLE_CONTENT,
            {"error": "schema_invalid", "field": "data.object.amount"},
        ),
        (TransientError("SQS timed out"), HTTPStatus.SERVICE_UNAVAILABLE, {"error": "try_again"}),
    ],
)
def test_ingest_errors_map_to_the_response_table(
    error: Exception, status: HTTPStatus, body: dict[str, str]
) -> None:
    assert error_response(error) == ErrorResponse(status, body)


@pytest.mark.parametrize("error", [PermanentError("secret parameter missing"), KeyError("bug")])
def test_anything_else_is_a_500_without_detail(error: Exception) -> None:
    assert error_response(error) == ErrorResponse(
        HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_error"}
    )


def test_a_subclass_maps_like_its_parent() -> None:
    class TimestampOutOfTolerance(SignatureError):
        pass

    assert error_response(TimestampOutOfTolerance("t is 301 s old")) == ErrorResponse(
        HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"}
    )


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TransientError("DynamoDB throttled"), Disposition.RETRY),
        (ConnectionError("not one of ours"), Disposition.RETRY),
        (PermanentError("envelope failed validation"), Disposition.DEAD_LETTER),
        (SchemaError("data.amount"), Disposition.DEAD_LETTER),
    ],
)
def test_processor_retries_transient_and_unknown_errors_and_dead_letters_the_rest(
    error: Exception, expected: Disposition
) -> None:
    assert disposition(error) is expected
