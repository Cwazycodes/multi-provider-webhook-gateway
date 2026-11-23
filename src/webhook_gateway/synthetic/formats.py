"""A delivery as Lambda receives it from the HTTP API, or as the provider sends it."""

from datetime import UTC
from typing import Any

from webhook_gateway.synthetic.generator import Delivery

ROUTE_KEY = "POST /webhooks/{provider}"
# Placeholders, like the account ID in .env.example: never a real API's.
ACCOUNT_ID = "000000000000"
API_ID = "0000000000"
DOMAIN = f"{API_ID}.execute-api.eu-west-2.amazonaws.com"
SOURCE_IP = "192.0.2.10"  # from the range reserved for documentation (RFC 5737)


def apigw_event(delivery: Delivery) -> dict[str, Any]:
    """The HTTP API event (payload format 2.0) for this delivery, for handler tests."""
    path = f"/webhooks/{delivery.provider}"
    sent_at = delivery.sent_at.astimezone(UTC)
    # An HTTP API lowercases header names and adds a few of its own.
    headers = {name.lower(): value for name, value in delivery.headers.items()} | {
        "content-length": str(len(delivery.body.encode())),
        "host": DOMAIN,
        "x-forwarded-for": SOURCE_IP,
        "x-forwarded-port": "443",
        "x-forwarded-proto": "https",
    }
    return {
        "version": "2.0",
        "routeKey": ROUTE_KEY,
        "rawPath": path,
        "rawQueryString": "",
        "headers": headers,
        "requestContext": {
            "accountId": ACCOUNT_ID,
            "apiId": API_ID,
            "domainName": DOMAIN,
            "domainPrefix": API_ID,
            "http": {
                "method": "POST",
                "path": path,
                "protocol": "HTTP/1.1",
                "sourceIp": SOURCE_IP,
                "userAgent": headers.get("user-agent", ""),
            },
            "requestId": delivery.request_id,
            "routeKey": ROUTE_KEY,
            "stage": "$default",
            "time": sent_at.strftime("%d/%b/%Y:%H:%M:%S +0000"),
            "timeEpoch": int(sent_at.timestamp() * 1000),
        },
        "pathParameters": {"provider": str(delivery.provider)},
        "body": delivery.body,
        "isBase64Encoded": False,
    }


def http_request(delivery: Delivery) -> dict[str, Any]:
    """The request as the provider sends it; the load test adds the base URL."""
    return {
        "method": "POST",
        "path": f"/webhooks/{delivery.provider}",
        "headers": dict(delivery.headers),
        "body": delivery.body,
    }
