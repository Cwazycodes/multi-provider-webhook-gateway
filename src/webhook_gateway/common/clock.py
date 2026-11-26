"""The wall clock, as a default: anything that tells the time takes a clock argument."""

from datetime import UTC, datetime


def utc_now() -> datetime:
    return datetime.now(UTC)
