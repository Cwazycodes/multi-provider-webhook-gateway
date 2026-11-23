"""What every provider's verifier shares: the request's exact bytes, and how to compare."""

import base64
import hmac
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any, Protocol, Self

from webhook_gateway.common.errors import PermanentError, SignatureError


@dataclass(frozen=True)
class SignedRequest:
    """What a signature covers, and where it travels: the body's exact bytes and the headers."""

    headers: Mapping[str, str]  # names lowercased, as an HTTP API passes them
    body: bytes

    @classmethod
    def of(cls, headers: Mapping[str, str], body: bytes) -> Self:
        return cls({name.lower(): value for name, value in headers.items()}, body)

    @classmethod
    def from_apigw_event(cls, event: Mapping[str, Any]) -> Self:
        """The request in an HTTP API (payload 2.0) event, decoding a base64-encoded body."""
        text: str = event.get("body") or ""
        if event.get("isBase64Encoded"):
            body = base64.b64decode(text, validate=True)
        else:
            body = text.encode()
        return cls.of(event.get("headers") or {}, body)

    def header(self, name: str) -> str:
        """The named header, in any case. A SignatureError if it isn't there."""
        value = self.headers.get(name.lower())
        if value is None:
            raise SignatureError(f"missing {name} header")
        return value


class Verifier(Protocol):
    def verify(self, request: SignedRequest, secrets: Sequence[str], now: datetime) -> None:
        """Return if one of `secrets` signed `request`; raise SignatureError if none did."""


def signed_by_any(
    secrets: Sequence[str],
    message: bytes,
    signatures: Sequence[str],
    encode: Callable[[bytes], str],
) -> bool:
    """Whether any signature is encode(HMAC-SHA256(secret, message)) for any secret."""
    if not secrets:
        raise PermanentError("no signing secrets configured")
    expected = [encode(hmac.new(key.encode(), message, sha256).digest()) for key in secrets]
    # compare_digest raises TypeError for str holding non-ASCII characters, and the
    # sender chooses the header: compare bytes, so a bad header is a 401, never a 500.
    return any(
        hmac.compare_digest(want.encode(), got.encode()) for got in signatures for want in expected
    )
