"""Runtime settings from environment variables (WG-08)."""

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from webhook_gateway.common.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = ROOT / ".env.example"

# The same values as .env.example. The account ID is a placeholder, never a real one.
VALID_ENV = {
    "STAGE": "dev",
    "PARAMETER_PATH": "/webhook-gateway/dev",
    "QUEUE_URL": "https://sqs.eu-west-2.amazonaws.com/000000000000/webhook-gateway-dev-events",
    "DEAD_LETTER_QUEUE_URL": "https://sqs.eu-west-2.amazonaws.com/000000000000/webhook-gateway-dev-dlq",
    "TABLE_NAME": "webhook-gateway-dev",
    "ARCHIVE_BUCKET": "webhook-gateway-dev-archive",
}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Every variable set to its valid value, whatever the shell running the tests has."""
    for name, value in VALID_ENV.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


def errors_of(exc_info: pytest.ExceptionInfo[ValidationError]) -> list[tuple[str, Any]]:
    return [(error["type"], error["loc"]) for error in exc_info.value.errors()]


def test_examples_cover_every_setting() -> None:
    assert {name.lower() for name in VALID_ENV} == set(Settings.model_fields)


def test_loads_every_setting_from_the_environment(env: pytest.MonkeyPatch) -> None:
    settings = Settings()

    assert settings.model_dump() == {name.lower(): value for name, value in VALID_ENV.items()}


@pytest.mark.parametrize("name", sorted(VALID_ENV))
def test_every_setting_is_required(env: pytest.MonkeyPatch, name: str) -> None:
    env.delenv(name)

    with pytest.raises(ValidationError) as exc_info:
        Settings()

    assert errors_of(exc_info) == [("missing", (name.lower(),))]


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("STAGE", "Dev"),
        ("STAGE", "dev/../prod"),
        ("PARAMETER_PATH", "webhook-gateway/dev"),
        ("QUEUE_URL", "webhook-gateway-dev-events"),
        ("DEAD_LETTER_QUEUE_URL", "http://sqs.eu-west-2.amazonaws.com/000000000000/dlq"),
        ("TABLE_NAME", ""),
        ("ARCHIVE_BUCKET", ""),
    ],
)
def test_malformed_settings_are_rejected(env: pytest.MonkeyPatch, name: str, value: str) -> None:
    env.setenv(name, value)

    with pytest.raises(ValidationError) as exc_info:
        Settings()

    assert [loc for _, loc in errors_of(exc_info)] == [(name.lower(),)]


def test_parameter_path_must_belong_to_the_stage(env: pytest.MonkeyPatch) -> None:
    env.setenv("PARAMETER_PATH", "/webhook-gateway/prod")

    with pytest.raises(ValidationError, match="parameter_path"):
        Settings()


def test_settings_are_frozen(env: pytest.MonkeyPatch) -> None:
    settings = Settings()

    with pytest.raises(ValidationError):
        settings.stage = "prod"  # type: ignore[misc]


def test_env_example_documents_every_setting() -> None:
    lines = ENV_EXAMPLE.read_text().splitlines()
    assignments = {
        number: line.split("=", 1)
        for number, line in enumerate(lines)
        if line and not line.startswith("#")
    }

    assert dict(assignments.values()) == VALID_ENV
    undocumented = [
        name for number, (name, _) in assignments.items() if not lines[number - 1].startswith("#")
    ]
    assert not undocumented
