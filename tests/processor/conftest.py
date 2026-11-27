"""The processor tests' shared table: made in moto exactly as template.yaml declares it."""

from pathlib import Path
from typing import Any

import boto3
import pytest
from cfnlint.decode import decode_str
from mypy_boto3_dynamodb import DynamoDBClient

TEMPLATE = Path(__file__).resolve().parents[2] / "template.yaml"


@pytest.fixture
def events_table() -> dict[str, Any]:
    """EventsTable's properties in template.yaml."""
    decoded, matches = decode_str(TEMPLATE.read_text())
    assert decoded is not None, matches
    properties: dict[str, Any] = decoded["Resources"]["EventsTable"]["Properties"]
    return properties


@pytest.fixture
def dynamodb(aws: None, events_table: dict[str, Any]) -> DynamoDBClient:
    """moto's DynamoDB with one table, webhook-gateway-test, keyed as the template keys it."""
    client = boto3.client("dynamodb")
    client.create_table(
        TableName="webhook-gateway-test",
        KeySchema=events_table["KeySchema"],
        AttributeDefinitions=events_table["AttributeDefinitions"],
        BillingMode=events_table["BillingMode"],
    )
    return client
