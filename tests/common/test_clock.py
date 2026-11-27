"""The clock, and utc_text: the one way a moment is written as text (WG-15)."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from webhook_gateway.common.clock import utc_now, utc_text


def test_utc_now_is_in_utc() -> None:
    assert utc_now().tzinfo is UTC


@pytest.mark.parametrize(
    ("moment", "text"),
    [
        (datetime(2026, 11, 17, 10, 0, tzinfo=UTC), "2026-11-17T10:00:00.000000Z"),
        (datetime(2026, 11, 17, 10, 0, 0, 5, tzinfo=UTC), "2026-11-17T10:00:00.000005Z"),
        (
            datetime(2026, 11, 17, 12, 0, tzinfo=timezone(timedelta(hours=2))),
            "2026-11-17T10:00:00.000000Z",
        ),
    ],
)
def test_utc_text_is_utc_to_the_microsecond(moment: datetime, text: str) -> None:
    assert utc_text(moment) == text


def test_utc_text_refuses_a_time_with_no_zone() -> None:
    with pytest.raises(ValueError, match="no time zone"):
        utc_text(datetime(2026, 11, 17, 10, 0))
