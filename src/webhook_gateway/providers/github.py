"""GitHub: X-Hub-Signature-256 is sha256=<hex HMAC-SHA256 of the raw body>."""

from collections.abc import Sequence
from datetime import datetime

from webhook_gateway.common.errors import SignatureError
from webhook_gateway.providers.signatures import SignedRequest, signed_by_any

HEADER = "X-Hub-Signature-256"


class GitHubVerifier:
    def verify(self, request: SignedRequest, secrets: Sequence[str], now: datetime) -> None:
        # GitHub also sends a legacy SHA-1 X-Hub-Signature header: never trust that one.
        scheme, _, signature = request.header(HEADER).partition("=")
        if scheme != "sha256":
            raise SignatureError(f"{HEADER} isn't sha256=<hex>")
        if not signed_by_any(secrets, request.body, [signature], encode=bytes.hex):
            raise SignatureError(f"{HEADER} matches none of the secrets")
