"""What every verifier shares: the request's exact bytes, its headers, the comparison (WG-10)."""

import base64

import pytest

from webhook_gateway.common.errors import PermanentError, SignatureError
from webhook_gateway.providers.signatures import SignedRequest, signed_by_any

BODY = '{"name": "Zoë"}'


def test_headers_are_found_whatever_their_case() -> None:
    request = SignedRequest.of({"X-Hub-Signature-256": "sha256=ab"}, b"{}")

    assert request.header("x-hub-signature-256") == "sha256=ab"
    assert request.header("X-HUB-SIGNATURE-256") == "sha256=ab"


def test_a_missing_header_is_a_signature_error() -> None:
    with pytest.raises(SignatureError, match="missing Stripe-Signature"):
        SignedRequest.of({}, b"{}").header("Stripe-Signature")


def test_a_text_body_is_kept_byte_for_byte() -> None:
    event = {"headers": {}, "body": BODY, "isBase64Encoded": False}

    assert SignedRequest.from_apigw_event(event).body == BODY.encode()


def test_a_base64_body_is_decoded_first() -> None:
    encoded = base64.b64encode(BODY.encode()).decode()
    event = {"headers": {}, "body": encoded, "isBase64Encoded": True}

    assert SignedRequest.from_apigw_event(event).body == BODY.encode()


def test_an_event_without_a_body_or_headers_has_empty_ones() -> None:
    request = SignedRequest.from_apigw_event({"isBase64Encoded": False})

    assert request == SignedRequest(headers={}, body=b"")


def test_no_secrets_is_the_gateways_fault_not_the_callers() -> None:
    with pytest.raises(PermanentError, match="no signing secrets"):
        signed_by_any([], b"{}", ["ab"], encode=bytes.hex)


def test_a_non_ascii_signature_is_a_mismatch_not_a_crash() -> None:
    # hmac.compare_digest raises TypeError for str holding non-ASCII characters.
    assert signed_by_any(["a-test-key"], b"{}", ["é" * 64], encode=bytes.hex) is False
