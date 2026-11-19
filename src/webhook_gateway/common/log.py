"""JSON log lines for every function: Powertools Logger with redaction built in."""

from collections.abc import Mapping
from typing import IO, Any

from aws_lambda_powertools import Logger
from aws_lambda_powertools.logging.formatter import LambdaPowertoolsFormatter
from aws_lambda_powertools.logging.types import LogRecord

REDACTED = "[REDACTED]"
# Keys are compared lowercased with "-" and "_" removed, so "X-Hub-Signature-256",
# "raw_body" and "rawBody" all match. A matching key stays in the line; its value goes.
_SENSITIVE_KEYS = frozenset({"body", "headers", "multivalueheaders", "rawbody"})
_SENSITIVE_PARTS = ("authorization", "cookie", "hmac", "secret", "signature", "token")


def _is_sensitive(key: object) -> bool:
    name = str(key).lower().replace("-", "").replace("_", "")
    return name in _SENSITIVE_KEYS or any(part in name for part in _SENSITIVE_PARTS)


def redact(value: Any) -> Any:
    """`value` with the value of every sensitive key, at any depth, replaced by REDACTED."""
    if isinstance(value, Mapping):
        return {
            key: REDACTED if _is_sensitive(key) else redact(item) for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(item) for item in value]
    return value


class RedactingFormatter(LambdaPowertoolsFormatter):
    """Powertools' JSON formatter, redacting each line just before it's written."""

    def serialize(self, log: LogRecord) -> str:
        # Every line passes through here, whether a value came from the message
        # (even a whole event), extra=, append_keys or append_context_keys.
        return super().serialize(log=redact(log))


def make_logger(service: str, stage: str, *, stream: IO[str] | None = None) -> Logger:
    """The logger for one function; make it once, at import.

    Powertools sets up each service name once per process and ignores the
    arguments of any later Logger with the same name.
    """
    return Logger(
        service=service,
        stream=stream,
        # Logger(utc=True) is ignored once you pass a formatter, so the timestamp
        # options go here. use_rfc3339 also makes the offset read +00:00.
        logger_formatter=RedactingFormatter(utc=True, use_rfc3339=True),
        stage=stage,
    )
