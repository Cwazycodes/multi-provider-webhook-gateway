"""The wall clock, as a default: anything that tells the time takes a clock argument."""

from datetime import UTC, datetime


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_text(moment: datetime) -> str:
    """`moment` as UTC text of one fixed width, so that texts sort as the moments do."""
    if moment.tzinfo is None:
        raise ValueError(f"{moment} has no time zone, so it isn't a moment")

    return moment.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="microseconds") + "Z"
