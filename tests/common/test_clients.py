"""Every AWS client retries the same way: design note section 9 (WG-16)."""

from collections.abc import Callable
from typing import Any

import pytest

from webhook_gateway.common.clients import make_dynamodb_client, make_sqs_client


@pytest.mark.parametrize("make_client", [make_sqs_client, make_dynamodb_client])
def test_clients_retry_in_standard_mode_three_attempts_in_all(
    aws: None, make_client: Callable[[], Any]
) -> None:
    # Config(retries={"max_attempts": 3}) would mean three *retries*: four attempts.
    assert make_client().meta.config.retries == {"mode": "standard", "total_max_attempts": 3}
