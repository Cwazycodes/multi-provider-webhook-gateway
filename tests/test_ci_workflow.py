"""Guards for the CI workflow set up in WG-06."""

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
# The ruleset on main requires a check with this name. Renaming the job leaves
# every PR waiting for a check that never reports.
REQUIRED_CHECK = "check"
PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


def workflow() -> dict[Any, Any]:
    loaded: dict[Any, Any] = yaml.safe_load(WORKFLOW.read_text())
    return loaded


def jobs() -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = workflow()["jobs"]
    return found


def steps() -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = jobs()[REQUIRED_CHECK]["steps"]
    return found


def step_using(action: str) -> dict[str, Any]:
    return next(step for step in steps() if step.get("uses", "").startswith(f"{action}@"))


def test_runs_on_pull_requests_and_pushes_to_main() -> None:
    # PyYAML follows YAML 1.1, where a bare `on` key loads as the boolean True.
    triggers = workflow()[True]

    assert "pull_request" in triggers
    assert triggers["push"]["branches"] == ["main"]


def test_token_can_only_read_the_repo() -> None:
    assert workflow()["permissions"] == {"contents": "read"}
    widened = [name for name, job in jobs().items() if "permissions" in job]
    assert not widened


def test_every_action_is_pinned_to_a_full_commit_sha() -> None:
    uses = [step["uses"] for job in jobs().values() for step in job["steps"] if "uses" in step]

    assert uses
    unpinned = [action for action in uses if not PINNED.match(action)]
    assert not unpinned


def test_required_check_keeps_its_name() -> None:
    assert jobs()[REQUIRED_CHECK].get("name", REQUIRED_CHECK) == REQUIRED_CHECK


def test_required_check_runs_the_make_targets_in_order() -> None:
    make_runs = [step["run"] for step in steps() if step.get("run", "").startswith("make ")]
    assert make_runs == ["make install", "make scan-secrets", "make check"]


def test_checkout_gives_gitleaks_all_history_and_keeps_no_token() -> None:
    checkout = step_using("actions/checkout")["with"]

    assert checkout["fetch-depth"] == 0
    assert checkout["persist-credentials"] is False


def test_coverage_report_is_uploaded() -> None:
    upload = step_using("actions/upload-artifact")["with"]

    assert "coverage.xml" in upload["path"]
    assert upload["if-no-files-found"] == "error"
