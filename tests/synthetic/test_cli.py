"""`make generate`: signed synthetic deliveries as JSON Lines (WG-09)."""

import json
import os
import subprocess
import sys
from typing import Any

import pytest

from webhook_gateway.synthetic.cli import main

FIXED = ["--seed", "7", "--start", "2026-11-17T10:00:00Z"]
PROVIDERS = ["github", "shopify", "stripe"]


def every_fault(provider: str) -> list[str]:
    """Ten percent of each fault; only Stripe signs a timestamp that can go stale."""
    flags = ["--bad-signature-rate", "0.1", "--malformed-rate", "0.1"]
    flags += ["--schema-invalid-rate", "0.1", "--duplicate-rate", "0.1"]
    return flags + (["--stale-timestamp-rate", "0.1"] if provider == "stripe" else [])


@pytest.fixture(autouse=True)
def secret(monkeypatch: pytest.MonkeyPatch, signing_secret: str) -> str:
    monkeypatch.setenv("SIGNING_SECRET", signing_secret)
    return signing_secret


def run(capsys: pytest.CaptureFixture[str], *args: str) -> list[dict[str, Any]]:
    main(list(args))
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def test_prints_one_json_line_per_delivery(capsys: pytest.CaptureFixture[str]) -> None:
    lines = run(capsys, "--provider", "stripe", "--count", "3", *FIXED)

    assert len(lines) == 3
    assert [line["fault"] for line in lines] == [None, None, None]
    assert {line["event"]["version"] for line in lines} == {"2.0"}


def test_http_format_prints_requests(capsys: pytest.CaptureFixture[str]) -> None:
    (line,) = run(capsys, "--provider", "github", "--count", "1", "--format", "http", *FIXED)

    assert line["request"]["path"] == "/webhooks/github"
    assert "X-Hub-Signature-256" in line["request"]["headers"]


@pytest.mark.parametrize("provider", PROVIDERS)
def test_the_same_seed_and_start_give_identical_output(
    capsys: pytest.CaptureFixture[str], provider: str
) -> None:
    args = ["--provider", provider, "--count", "50", *FIXED, *every_fault(provider)]

    assert run(capsys, *args) == run(capsys, *args)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_the_same_seed_gives_identical_output_in_a_new_process(provider: str) -> None:
    # Each Python process salts string hashes differently, so this catches any
    # dependence on set or hash order that a single-process test can't see.
    command = [sys.executable, "-m", "webhook_gateway.synthetic.cli", "--provider", provider]
    command += ["--count", "50", *FIXED, *every_fault(provider)]
    outputs = {
        subprocess.run(
            command,
            env=os.environ | {"PYTHONHASHSEED": hash_seed},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for hash_seed in ("1", "2")
    }

    assert len(outputs) == 1


def test_fault_flags_reach_the_generator(capsys: pytest.CaptureFixture[str]) -> None:
    lines = run(capsys, "--provider", "shopify", "--count", "5", "--malformed-rate", "1", *FIXED)

    assert [line["fault"] for line in lines] == ["malformed"] * 5


def test_start_defaults_to_now(capsys: pytest.CaptureFixture[str]) -> None:
    (line,) = run(capsys, "--provider", "stripe", "--count", "1")

    assert line["event"]["requestContext"]["timeEpoch"] > 1_763_373_600_000


def test_refuses_to_run_without_a_secret(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SIGNING_SECRET")

    with pytest.raises(SystemExit) as exit_info:
        main(["--provider", "stripe"])

    assert exit_info.value.code == 2
    assert "SIGNING_SECRET" in capsys.readouterr().err


@pytest.mark.parametrize(
    "args",
    [
        ["--provider", "gitlab"],
        ["--provider", "stripe", "--malformed-rate", "1.5"],
        ["--provider", "stripe", "--malformed-rate", "0.6", "--duplicate-rate", "0.6"],
        ["--provider", "github", "--stale-timestamp-rate", "0.1"],
        ["--provider", "stripe", "--start", "2026-11-17T10:00:00"],
    ],
)
def test_bad_arguments_exit_with_a_usage_error(args: list[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(args)

    assert exit_info.value.code == 2
