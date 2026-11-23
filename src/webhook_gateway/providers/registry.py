"""Which verifier checks each provider's requests: one entry per provider."""

from collections.abc import Mapping

from webhook_gateway.common.envelope import Provider
from webhook_gateway.providers.github import GitHubVerifier
from webhook_gateway.providers.shopify import ShopifyVerifier
from webhook_gateway.providers.signatures import Verifier
from webhook_gateway.providers.stripe import StripeVerifier

VERIFIERS: Mapping[Provider, Verifier] = {
    Provider.GITHUB: GitHubVerifier(),
    Provider.SHOPIFY: ShopifyVerifier(),
    Provider.STRIPE: StripeVerifier(),
}
