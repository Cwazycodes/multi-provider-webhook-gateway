"""Each provider's verifier: known answers, every tampered case, rotation (WG-10)."""

import base64
import hmac
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from random import Random
from secrets import token_hex

import pytest
from hypothesis import given
from hypothesis import strategies as st

from webhook_gateway.common.envelope import Provider
from webhook_gateway.common.errors import SignatureError
from webhook_gateway.providers.registry import PROVIDERS
from webhook_gateway.providers.signatures import SignedRequest
from webhook_gateway.synthetic.formats import apigw_event
from webhook_gateway.synthetic.generator import FAKES, Fault, FaultRates, generate

# Known answers, computed once with openssl (see the WG-10 guide), never with Python:
#   openssl dgst -sha256 -hmac "$SECRET" < body                         (github)
#   openssl dgst -sha256 -hmac "$SECRET" -binary < body | base64        (shopify)
#   { printf '%s.' "$T"; cat body; } | openssl dgst -sha256 -hmac "$SECRET"   (stripe)
SECRET = "test-secret-not-real"
T = 1763373600
SIGNED_AT = datetime.fromtimestamp(T, tz=UTC)
COMPACT = b'{"id":"evt_test","object":"event"}'
MULTILINE = '{\n  "id": "evt_test",\n  "name": "Zoë"\n}'.encode()
SIGNATURE_HEADER = {
    Provider.GITHUB: "X-Hub-Signature-256",
    Provider.SHOPIFY: "X-Shopify-Hmac-Sha256",
    Provider.STRIPE: "Stripe-Signature",
}
KNOWN_ANSWERS = [
    pytest.param(
        Provider.GITHUB,
        COMPACT,
        "sha256=23692869dfe4744e7ff1d152fbb3db4cf3a7e01e23f4604fe33ce4a3168c99f8",
        id="github-compact",
    ),
    pytest.param(
        Provider.GITHUB,
        MULTILINE,
        "sha256=247efd24aa65a31b27d1632eb54ba2eea37ac2795e946bf26de0cb8d3049a5df",
        id="github-multiline",
    ),
    pytest.param(
        Provider.SHOPIFY,
        COMPACT,
        "I2koad/kdE5/8dFS+7PbTPOn4B4j9GBP4zzkoxaMmfg=",
        id="shopify-compact",
    ),
    pytest.param(
        Provider.SHOPIFY,
        MULTILINE,
        "JH79JKploxsn0WMutUui7qN6wnlelGvybeDLjTBJpd8=",
        id="shopify-multiline",
    ),
    pytest.param(
        Provider.STRIPE,
        COMPACT,
        "t=1763373600,v1=119b62eb37767ed4e8b75c60c3d7886369d9f8532876becd19f88fbd66ca5acf",
        id="stripe-compact",
    ),
    pytest.param(
        Provider.STRIPE,
        MULTILINE,
        "t=1763373600,v1=f50f4a138fdffd3ed1d8e497162640b84f5ab5cab34f750efcfe605b85a9c133",
        id="stripe-multiline",
    ),
]
STRIPE_V1 = "119b62eb37767ed4e8b75c60c3d7886369d9f8532876becd19f88fbd66ca5acf"  # COMPACT at T


def headers_for(provider: Provider, signature: str) -> dict[str, str]:
    return {"Content-Type": "application/json", SIGNATURE_HEADER[provider]: signature}


def verify(
    provider: Provider,
    headers: Mapping[str, str],
    body: bytes,
    *,
    secrets: Sequence[str] = (SECRET,),
    now: datetime = SIGNED_AT,
) -> None:
    PROVIDERS[provider].verifier.verify(SignedRequest.of(headers, body), secrets, now)


def stripe_header(header: str, *, now: datetime = SIGNED_AT) -> None:
    verify(Provider.STRIPE, {"Stripe-Signature": header}, COMPACT, now=now)


def test_every_provider_has_a_verifier() -> None:
    assert set(PROVIDERS) == set(Provider)


@pytest.mark.parametrize(("provider", "body", "signature"), KNOWN_ANSWERS)
def test_known_answers_verify(provider: Provider, body: bytes, signature: str) -> None:
    verify(provider, headers_for(provider, signature), body)


@pytest.mark.parametrize(("provider", "body", "signature"), KNOWN_ANSWERS)
def test_a_tampered_body_is_rejected(provider: Provider, body: bytes, signature: str) -> None:
    with pytest.raises(SignatureError, match=r"none of the secrets|no v1 matching"):
        verify(provider, headers_for(provider, signature), body.replace(b"evt_test", b"evt_tesu"))


@pytest.mark.parametrize(("provider", "body", "signature"), KNOWN_ANSWERS)
def test_the_wrong_secret_is_rejected_without_echoing_the_signature(
    provider: Provider, body: bytes, signature: str
) -> None:
    with pytest.raises(SignatureError) as exc_info:
        verify(provider, headers_for(provider, signature), body, secrets=[token_hex(32)])

    assert signature[-20:] not in str(exc_info.value)


@pytest.mark.parametrize(("provider", "body", "signature"), KNOWN_ANSWERS)
def test_a_missing_header_is_rejected(provider: Provider, body: bytes, signature: str) -> None:
    with pytest.raises(SignatureError, match="missing"):
        verify(provider, {"Content-Type": "application/json"}, body)


@pytest.mark.parametrize(("provider", "body", "signature"), KNOWN_ANSWERS)
def test_re_serialised_json_is_rejected(provider: Provider, body: bytes, signature: str) -> None:
    reserialised = json.dumps(json.loads(body)).encode()

    assert reserialised != body
    with pytest.raises(SignatureError):
        verify(provider, headers_for(provider, signature), reserialised)


@pytest.mark.parametrize("provider", list(Provider))
def test_a_rotated_secret_and_its_predecessor_both_verify(provider: Provider) -> None:
    old, new, retired = token_hex(32), token_hex(32), token_hex(32)

    def signed_with(key: str) -> Mapping[str, str]:
        return FAKES[provider].sign(key, COMPACT.decode(), SIGNED_AT)

    verify(provider, signed_with(old), COMPACT, secrets=[new, old])
    verify(provider, signed_with(new), COMPACT, secrets=[new, old])
    with pytest.raises(SignatureError):
        verify(provider, signed_with(retired), COMPACT, secrets=[new, old])


@pytest.mark.parametrize(
    "header",
    [
        "sha1=811bb04ee81e79595792b529b5446ac51c975191",
        "sha1=23692869dfe4744e7ff1d152fbb3db4cf3a7e01e23f4604fe33ce4a3168c99f8",
        "23692869dfe4744e7ff1d152fbb3db4cf3a7e01e23f4604fe33ce4a3168c99f8",
        "sha256=" + "é" * 64,
    ],
)
def test_github_needs_a_sha256_signature(header: str) -> None:
    with pytest.raises(SignatureError):
        verify(Provider.GITHUB, {"X-Hub-Signature-256": header}, COMPACT)


def test_github_ignores_the_legacy_sha1_header() -> None:
    legacy = {"X-Hub-Signature": "sha1=811bb04ee81e79595792b529b5446ac51c975191"}

    with pytest.raises(SignatureError, match="missing X-Hub-Signature-256"):
        verify(Provider.GITHUB, legacy, COMPACT)


@pytest.mark.parametrize("seconds", [0, 300, -300])
def test_stripe_accepts_a_timestamp_within_five_minutes(seconds: int) -> None:
    stripe_header(f"t={T},v1={STRIPE_V1}", now=SIGNED_AT + timedelta(seconds=seconds))


@pytest.mark.parametrize("seconds", [301, -301, 86400])
def test_stripe_rejects_a_timestamp_outside_five_minutes(seconds: int) -> None:
    with pytest.raises(SignatureError, match="signed"):
        stripe_header(f"t={T},v1={STRIPE_V1}", now=SIGNED_AT + timedelta(seconds=seconds))


def test_stripe_rejects_a_timestamp_the_signature_does_not_cover() -> None:
    with pytest.raises(SignatureError, match="no v1 matching"):
        stripe_header(f"t={T + 60},v1={STRIPE_V1}", now=SIGNED_AT + timedelta(seconds=60))


def test_stripe_accepts_any_matching_v1_and_ignores_other_entries() -> None:
    stripe_header(
        f"t={T},v1={'0' * 64},v1={STRIPE_V1},v0={'1' * 64},destination=we_123,payload=a25aefc6"
    )


@pytest.mark.parametrize(
    "header",
    [
        pytest.param(f"t={T},v0={STRIPE_V1}", id="v0-only"),
        pytest.param(f"v1={STRIPE_V1}", id="no-timestamp"),
        pytest.param(f"t={T}", id="no-signature"),
        # Either t would verify if picked, so the header itself must be refused.
        pytest.param(f"t={T},t={T + 1},v1={STRIPE_V1}", id="two-timestamps-good-first"),
        pytest.param(f"t={T + 1},t={T},v1={STRIPE_V1}", id="two-timestamps-good-last"),
        pytest.param(f"t={T},v1={'é' * 64}", id="non-ascii"),
    ],
)
def test_stripe_rejects_malformed_headers(header: str) -> None:
    with pytest.raises(SignatureError):
        stripe_header(header)


@pytest.mark.parametrize("timestamp", ["1_763_373_600", "+1763373600", " 1763373600", "١٧٦٣٣٧٣٦٠٠"])
def test_stripe_timestamps_are_plain_ascii_digits(timestamp: str) -> None:
    # int() accepts every one of these; the v1 here really does cover them.
    signed = f"{timestamp}.".encode() + COMPACT
    v1 = hmac.new(SECRET.encode(), signed, sha256).hexdigest()

    with pytest.raises(SignatureError, match="isn't t="):
        stripe_header(f"t={timestamp},v1={v1}")


def realistic_body(provider: Provider, seed: int) -> str:
    fake = FAKES[provider]
    return fake.render(fake.event(Random(seed), SIGNED_AT).payload)


@pytest.mark.parametrize("provider", list(Provider))
@given(key=st.text(min_size=1), data=st.data())
def test_changing_any_single_byte_of_the_body_fails(
    provider: Provider, key: str, data: st.DataObject
) -> None:
    # Any text, or a realistic payload: Hypothesis favours short text, and a verifier
    # that only hashed the start of the body would pass on short bodies alone.
    payloads = st.builds(realistic_body, st.just(provider), st.integers(min_value=0))
    body = data.draw(st.one_of(st.text(min_size=1), payloads))
    headers = FAKES[provider].sign(key, body, SIGNED_AT)
    raw = bytearray(body.encode())
    verify(provider, headers, bytes(raw), secrets=[key])

    raw[data.draw(st.integers(0, len(raw) - 1))] ^= data.draw(st.integers(1, 255))

    with pytest.raises(SignatureError):
        verify(provider, headers, bytes(raw), secrets=[key])


@pytest.mark.parametrize("provider", list(Provider))
def test_generated_traffic_verifies_unless_its_signature_is_the_fault(
    provider: Provider, signing_secret: str
) -> None:
    rates = FaultRates(
        bad_signature=0.1,
        stale_timestamp=0.1 if provider is Provider.STRIPE else 0.0,
        malformed=0.1,
        schema_invalid=0.1,
        duplicate=0.1,
    )
    start = datetime(2026, 11, 17, 10, 0, 0, tzinfo=UTC)
    for delivery in generate(
        provider, signing_secret, count=500, seed=11, start=start, faults=rates
    ):
        request = SignedRequest.from_apigw_event(apigw_event(delivery))
        verifier = PROVIDERS[provider].verifier
        if delivery.fault in {Fault.BAD_SIGNATURE, Fault.STALE_TIMESTAMP}:
            with pytest.raises(SignatureError):
                verifier.verify(request, [signing_secret], delivery.sent_at)
        else:
            verifier.verify(request, [signing_secret], delivery.sent_at)


@pytest.mark.parametrize("provider", list(Provider))
def test_a_base64_encoded_body_verifies_only_once_decoded(
    provider: Provider, signing_secret: str
) -> None:
    start = datetime(2026, 11, 17, 10, 0, 0, tzinfo=UTC)
    delivery = next(generate(provider, signing_secret, count=1, seed=1, start=start))
    encoded = base64.b64encode(delivery.body.encode()).decode()
    event = apigw_event(delivery) | {"body": encoded, "isBase64Encoded": True}

    decoded = SignedRequest.from_apigw_event(event)
    PROVIDERS[provider].verifier.verify(decoded, [signing_secret], delivery.sent_at)
    as_text = SignedRequest.of(event["headers"], encoded.encode())
    with pytest.raises(SignatureError):
        PROVIDERS[provider].verifier.verify(as_text, [signing_secret], delivery.sent_at)
