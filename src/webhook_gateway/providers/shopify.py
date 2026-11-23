"""Shopify: X-Shopify-Hmac-Sha256 is the base64 HMAC-SHA256 of the raw body."""

import base64
from collections.abc import Sequence
from datetime import datetime

from webhook_gateway.common.errors import SignatureError
from webhook_gateway.providers.signatures import SignedRequest, signed_by_any

HEADER = "X-Shopify-Hmac-Sha256"


def _base64(digest: bytes) -> str:
    return base64.b64encode(digest).decode()


class ShopifyVerifier:
    def verify(self, request: SignedRequest, secrets: Sequence[str], now: datetime) -> None:
        if not signed_by_any(secrets, request.body, [request.header(HEADER)], encode=_base64):
            raise SignatureError(f"{HEADER} matches none of the secrets")
