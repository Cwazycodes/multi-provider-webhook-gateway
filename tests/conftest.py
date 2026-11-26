"""Shared fixtures."""

import secrets
from collections.abc import Iterator

import pytest
from moto import mock_aws


@pytest.fixture
def signing_secret() -> str:
    """A new random signing secret for each test.

    Test secrets are generated like this, or are obviously fake strings such as
    "test-secret-not-real", so gitleaks never needs an allowlist.
    """
    return secrets.token_hex(32)


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """moto's in-memory AWS, with fake credentials, so no test can reach a real account."""
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-2")
    with mock_aws():
        yield
