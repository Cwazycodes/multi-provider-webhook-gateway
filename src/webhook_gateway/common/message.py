"""The SQS message ingest sends and the processor reads: one per accepted request."""

import base64
from collections.abc import Mapping
from typing import Annotated, Self

from pydantic import AfterValidator, BaseModel, ConfigDict

from webhook_gateway.common.envelope import Envelope


def _decodable(text: str) -> str:
    base64.b64decode(text, validate=True)  # its binascii.Error is a ValueError: pydantic reports it
    return text


Base64Text = Annotated[str, AfterValidator(_decodable)]


class QueueMessage(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    envelope: Envelope
    raw_body: Base64Text  # the body exactly as the provider sent it
    headers: Mapping[str, str]  # the provider's archived headers, lowercased; never a signature

    @classmethod
    def of(cls, envelope: Envelope, body: bytes, headers: Mapping[str, str]) -> Self:
        return cls(envelope=envelope, raw_body=base64.b64encode(body).decode(), headers=headers)

    def body(self) -> bytes:
        return base64.b64decode(self.raw_body)
