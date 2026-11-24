"""The normalised event envelope (WG-07)."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from pydantic import ValidationError

from webhook_gateway.common.envelope import Envelope, EventType, Provider

# One envelope as it arrives on the queue: plain JSON values only.
VALID_JSON: dict[str, Any] = {
    "schema_version": 1,
    "event_id": "0b6f6c58-6f3e-4d55-9c1a-2f4c1d1e7a10",
    "idempotency_key": "stripe:evt_123",
    "provider": "stripe",
    "provider_event_id": "evt_123",
    "event_type": "payment.succeeded",
    "subject_id": "stripe:pi_123",
    "occurred_at": "2026-11-17T10:00:00Z",
    "received_at": "2026-11-17T10:00:05.250000Z",
    "correlation_id": "req-abc123",
    "data": {"amount": 1999, "currency": "gbp"},
}


def valid_fields() -> dict[str, Any]:
    """The same envelope as the ingest code builds it: Python objects."""
    return {
        "schema_version": 1,
        "event_id": uuid4(),
        "idempotency_key": "stripe:evt_123",
        "provider": Provider.STRIPE,
        "provider_event_id": "evt_123",
        "event_type": EventType.PAYMENT_SUCCEEDED,
        "subject_id": "stripe:pi_123",
        "occurred_at": datetime(2026, 11, 17, 10, 0, 0, tzinfo=UTC),
        "received_at": datetime(2026, 11, 17, 10, 0, 5, tzinfo=UTC),
        "correlation_id": "req-abc123",
        "data": {"amount": 1999, "currency": "gbp"},
    }


def from_json(payload: dict[str, Any]) -> Envelope:
    return Envelope.model_validate_json(json.dumps(payload))


def errors_of(exc_info: pytest.ExceptionInfo[ValidationError]) -> list[tuple[str, Any]]:
    return [(error["type"], error["loc"]) for error in exc_info.value.errors()]


def test_examples_cover_every_field() -> None:
    assert set(VALID_JSON) == set(Envelope.model_fields) == set(valid_fields())


def test_valid_envelope_parses_from_queue_json() -> None:
    envelope = from_json(VALID_JSON)

    assert envelope.event_id == UUID("0b6f6c58-6f3e-4d55-9c1a-2f4c1d1e7a10")
    assert envelope.provider is Provider.STRIPE
    assert envelope.event_type is EventType.PAYMENT_SUCCEEDED
    assert envelope.occurred_at == datetime(2026, 11, 17, 10, 0, 0, tzinfo=UTC)
    assert envelope.data == {"amount": 1999, "currency": "gbp"}


def test_valid_envelope_round_trips_through_json() -> None:
    envelope = Envelope(**valid_fields())

    assert Envelope.model_validate_json(envelope.model_dump_json()) == envelope


@pytest.mark.parametrize("field", sorted(VALID_JSON))
def test_every_field_is_required(field: str) -> None:
    payload = {key: value for key, value in VALID_JSON.items() if key != field}

    with pytest.raises(ValidationError) as exc_info:
        from_json(payload)

    assert errors_of(exc_info) == [("missing", (field,))]


@pytest.mark.parametrize(
    ("field", "value", "error_type"),
    [
        ("schema_version", 2, "literal_error"),
        ("schema_version", "1", "literal_error"),
        ("event_id", "not-a-uuid", "uuid_parsing"),
        ("event_id", "a8098c1a-f86e-11da-bd1a-00112444be1e", "uuid_version"),
        ("provider", "gitlab", "enum"),
        ("event_type", "payment_intent.succeeded", "enum"),
        ("occurred_at", 1763373600, "datetime_type"),
        ("received_at", "yesterday", "datetime_parsing"),
        ("correlation_id", 123, "string_type"),
        ("provider_event_id", "", "string_too_short"),
        ("data", [1, 2], "dict_type"),
    ],
)
def test_bad_types_are_rejected(field: str, value: Any, error_type: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        from_json(VALID_JSON | {field: value})

    assert errors_of(exc_info) == [(error_type, (field,))]


@pytest.mark.parametrize("field", ["occurred_at", "received_at"])
def test_naive_datetimes_are_rejected(field: str) -> None:
    with pytest.raises(ValidationError) as from_queue:
        from_json(VALID_JSON | {field: "2026-11-17T10:00:00"})
    with pytest.raises(ValidationError) as from_code:
        Envelope(**valid_fields() | {field: datetime(2026, 11, 17, 10, 0, 0)})

    assert errors_of(from_queue) == errors_of(from_code) == [("timezone_aware", (field,))]


def test_other_utc_offsets_are_converted_to_utc() -> None:
    envelope = from_json(VALID_JSON | {"occurred_at": "2026-11-17T15:30:00+05:30"})

    assert envelope.occurred_at == datetime(2026, 11, 17, 10, 0, 0, tzinfo=UTC)
    assert envelope.occurred_at.tzinfo is UTC


@pytest.mark.parametrize("moment", ["0001-01-01T00:00:00+05:00", "9999-12-31T23:59:59-05:00"])
def test_a_time_outside_the_utc_range_is_rejected_not_a_crash(moment: str) -> None:
    # Both are valid aware datetimes, but in UTC one is before year 1 and one after 9999.
    with pytest.raises(ValidationError) as exc_info:
        from_json(VALID_JSON | {"occurred_at": moment})

    assert errors_of(exc_info) == [("value_error", ("occurred_at",))]


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError) as exc_info:
        from_json(VALID_JSON | {"amount": 1999})

    assert errors_of(exc_info) == [("extra_forbidden", ("amount",))]


@pytest.mark.parametrize("key", ["stripe:evt_999", "github:evt_123", "evt_123"])
def test_idempotency_key_must_be_provider_and_provider_event_id(key: str) -> None:
    with pytest.raises(ValidationError, match="idempotency_key"):
        from_json(VALID_JSON | {"idempotency_key": key})


@pytest.mark.parametrize("subject_id", ["pi_123", "github:pi_123", "stripe-pi_123"])
def test_subject_id_must_start_with_the_provider(subject_id: str) -> None:
    with pytest.raises(ValidationError, match="subject_id"):
        from_json(VALID_JSON | {"subject_id": subject_id})


def test_envelope_is_frozen() -> None:
    envelope = Envelope(**valid_fields())

    with pytest.raises(ValidationError) as exc_info:
        envelope.provider_event_id = "evt_456"  # type: ignore[misc]

    assert errors_of(exc_info) == [("frozen_instance", ("provider_event_id",))]


UTC_TIMES = st.datetimes(
    min_value=datetime(2000, 1, 1), max_value=datetime(2100, 1, 1), timezones=st.just(UTC)
)
# Half the instants are whole seconds, as in Stripe's and GitHub's payloads, and
# the gaps are small, so pairs often share a second. A serialiser that drops
# ".000000" sorts exactly those pairs the wrong way round.
INSTANTS = st.one_of(UTC_TIMES, UTC_TIMES.map(lambda moment: moment.replace(microsecond=0)))
GAPS = st.timedeltas(min_value=timedelta(seconds=-2), max_value=timedelta(seconds=2))


@given(first=INSTANTS, gap=GAPS)
@example(first=datetime(2026, 11, 17, 10, 0, 0, tzinfo=UTC), gap=timedelta(milliseconds=500))
def test_serialised_timestamps_sort_like_the_instants(first: datetime, gap: timedelta) -> None:
    second = first + gap

    def serialised(moment: datetime) -> str:
        dumped = Envelope(**valid_fields() | {"occurred_at": moment}).model_dump(mode="json")
        text: str = dumped["occurred_at"]
        return text

    assert (serialised(first) < serialised(second)) == (first < second)
