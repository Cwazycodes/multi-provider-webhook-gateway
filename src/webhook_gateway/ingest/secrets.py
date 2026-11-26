"""Where ingest gets each provider's signing secrets, cached in memory between requests."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from webhook_gateway.common.clock import utc_now
from webhook_gateway.common.envelope import Provider


class SecretSource(Protocol):
    def secrets(self, provider: Provider) -> Sequence[str]:
        """Every active signing secret for `provider`, newest first."""


@dataclass(frozen=True)
class StaticSecrets:
    """Secrets held in memory, for tests and local runs. WG-24 adds the SSM-backed source."""

    by_provider: Mapping[Provider, Sequence[str]]

    def secrets(self, provider: Provider) -> Sequence[str]:
        return tuple(self.by_provider.get(provider, ()))


@dataclass
class CachedSecrets:
    """Asks `source` at most once per provider in each `ttl`; design note section 10."""

    source: SecretSource
    ttl: timedelta = timedelta(minutes=5)
    clock: Callable[[], datetime] = utc_now
    _cache: dict[Provider, tuple[datetime, tuple[str, ...]]] = field(
        default_factory=dict, init=False, repr=False
    )

    def secrets(self, provider: Provider) -> Sequence[str]:
        now = self.clock()
        cached = self._cache.get(provider)
        if cached is not None and now < cached[0]:
            return cached[1]
        fresh = tuple(self.source.secrets(provider))  # an exception here caches nothing
        self._cache[provider] = (now + self.ttl, fresh)
        return fresh
