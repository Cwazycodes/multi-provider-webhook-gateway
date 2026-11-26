"""Signing secrets, from an injected source, cached in memory (WG-12)."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from webhook_gateway.common.envelope import Provider
from webhook_gateway.ingest.secrets import CachedSecrets, StaticSecrets

START = datetime(2026, 11, 17, 10, 0, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


class CountingSource:
    """A stand-in for SSM that counts how often it is asked."""

    def __init__(self, fail_first: bool = False) -> None:
        self.calls: list[Provider] = []
        self.fail_first = fail_first

    def secrets(self, provider: Provider) -> Sequence[str]:
        self.calls.append(provider)
        if self.fail_first and len(self.calls) == 1:
            raise ConnectionError("parameter store unavailable")
        return [f"{provider}-secret-{len(self.calls)}"]


def test_static_secrets_are_kept_per_provider() -> None:
    source = StaticSecrets({Provider.STRIPE: ["new", "old"]})

    assert source.secrets(Provider.STRIPE) == ("new", "old")
    assert source.secrets(Provider.GITHUB) == ()


def test_the_cache_asks_the_source_once_per_provider_within_the_ttl() -> None:
    source, clock = CountingSource(), Clock()
    cached = CachedSecrets(source, clock=clock)

    first = cached.secrets(Provider.STRIPE)
    clock.now += timedelta(minutes=4, seconds=59)
    again = cached.secrets(Provider.STRIPE)
    cached.secrets(Provider.GITHUB)

    assert first == again == ("stripe-secret-1",)
    assert source.calls == [Provider.STRIPE, Provider.GITHUB]


def test_the_cache_asks_again_after_five_minutes() -> None:
    source, clock = CountingSource(), Clock()
    cached = CachedSecrets(source, clock=clock)

    cached.secrets(Provider.STRIPE)
    clock.now += timedelta(minutes=5)

    assert cached.secrets(Provider.STRIPE) == ("stripe-secret-2",)


def test_a_failure_is_not_cached() -> None:
    source = CountingSource(fail_first=True)
    cached = CachedSecrets(source, clock=Clock())

    with pytest.raises(ConnectionError):
        cached.secrets(Provider.STRIPE)

    assert cached.secrets(Provider.STRIPE) == ("stripe-secret-2",)
