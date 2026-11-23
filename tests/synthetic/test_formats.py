"""Deliveries as HTTP API v2 events and as plain HTTP requests (WG-09)."""

from datetime import UTC, datetime

import pytest
from aws_lambda_powertools.utilities.data_classes import APIGatewayProxyEventV2

from webhook_gateway.common.envelope import Provider
from webhook_gateway.synthetic.formats import apigw_event, http_request
from webhook_gateway.synthetic.generator import Delivery, generate

START = datetime(2026, 11, 17, 10, 0, 0, tzinfo=UTC)


def one_delivery(provider: Provider, signing_secret: str) -> Delivery:
    return next(generate(provider, signing_secret, count=1, seed=1, start=START))


@pytest.mark.parametrize("provider", list(Provider))
def test_apigw_event_is_what_lambda_receives_from_an_http_api(
    provider: Provider, signing_secret: str
) -> None:
    delivery = one_delivery(provider, signing_secret)

    event = APIGatewayProxyEventV2(apigw_event(delivery))

    assert event.version == "2.0"
    assert event.route_key == "POST /webhooks/{provider}"
    assert event.raw_path == f"/webhooks/{provider}"
    assert event.path_parameters == {"provider": str(provider)}
    assert event.request_context.http.method == "POST"
    assert event.request_context.request_id == delivery.request_id
    assert event.request_context.time_epoch == int(START.timestamp() * 1000)
    assert event.body == delivery.body
    assert event.is_base64_encoded is False
    assert event.headers["content-length"] == str(len(delivery.body.encode()))


@pytest.mark.parametrize("provider", list(Provider))
def test_apigw_event_headers_are_lowercased_as_an_http_api_passes_them(
    provider: Provider, signing_secret: str
) -> None:
    delivery = one_delivery(provider, signing_secret)

    headers = apigw_event(delivery)["headers"]

    assert all(name == name.lower() for name in headers)
    for name, value in delivery.headers.items():
        assert headers[name.lower()] == value


@pytest.mark.parametrize("provider", list(Provider))
def test_http_request_is_exactly_what_the_provider_sends(
    provider: Provider, signing_secret: str
) -> None:
    delivery = one_delivery(provider, signing_secret)

    assert http_request(delivery) == {
        "method": "POST",
        "path": f"/webhooks/{provider}",
        "headers": dict(delivery.headers),
        "body": delivery.body,
    }
