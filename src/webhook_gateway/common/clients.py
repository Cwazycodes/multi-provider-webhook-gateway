"""AWS clients for every function, all with the retry policy of design note section 9."""

from typing import TYPE_CHECKING

import boto3
from botocore.config import Config

if TYPE_CHECKING:  # boto3-stubs is a dev dependency; Lambda doesn't have it
    from mypy_boto3_dynamodb import DynamoDBClient
    from mypy_boto3_sqs import SQSClient

# Standard mode, 3 attempts in all. Config's max_attempts would count retries after
# the first try, so it would mean 4.
RETRIES = Config(retries={"mode": "standard", "total_max_attempts": 3})


def make_sqs_client() -> SQSClient:
    return boto3.client("sqs", config=RETRIES)


def make_dynamodb_client() -> DynamoDBClient:
    return boto3.client("dynamodb", config=RETRIES)
