"""Every provider the gateway accepts: adding one means one module and one entry here."""

from collections.abc import Mapping
from dataclasses import dataclass

from webhook_gateway.common.envelope import Provider
from webhook_gateway.providers import github, shopify, stripe
from webhook_gateway.providers.normalising import Normaliser
from webhook_gateway.providers.signatures import Verifier


@dataclass(frozen=True)
class ProviderAdapter:
    """What the gateway needs from a provider's module."""

    verifier: Verifier
    normalise: Normaliser
    archived_headers: frozenset[str]  # kept with the raw body; never a signature


PROVIDERS: Mapping[Provider, ProviderAdapter] = {
    Provider.GITHUB: ProviderAdapter(
        github.GitHubVerifier(), github.normalise, github.ARCHIVED_HEADERS
    ),
    Provider.SHOPIFY: ProviderAdapter(
        shopify.ShopifyVerifier(), shopify.normalise, shopify.ARCHIVED_HEADERS
    ),
    Provider.STRIPE: ProviderAdapter(
        stripe.StripeVerifier(), stripe.normalise, stripe.ARCHIVED_HEADERS
    ),
}
