"""Every provider the gateway accepts: adding one means one module and one entry here."""

from collections.abc import Mapping
from dataclasses import dataclass

from webhook_gateway.common.envelope import Provider
from webhook_gateway.providers import github, shopify, stripe
from webhook_gateway.providers.normalising import Normaliser
from webhook_gateway.providers.signatures import Verifier


@dataclass(frozen=True)
class ProviderAdapter:
    """What the gateway needs from a provider's module: how to verify and how to normalise."""

    verifier: Verifier
    normalise: Normaliser


PROVIDERS: Mapping[Provider, ProviderAdapter] = {
    Provider.GITHUB: ProviderAdapter(github.GitHubVerifier(), github.normalise),
    Provider.SHOPIFY: ProviderAdapter(shopify.ShopifyVerifier(), shopify.normalise),
    Provider.STRIPE: ProviderAdapter(stripe.StripeVerifier(), stripe.normalise),
}
