"""Shared fixtures."""

import secrets

import pytest


@pytest.fixture
def signing_secret() -> str:
    """A new random signing secret for each test.

    Test secrets are generated like this, or are obviously fake strings such as
    "test-secret-not-real", so gitleaks never needs an allowlist.
    """
    return secrets.token_hex(32)
