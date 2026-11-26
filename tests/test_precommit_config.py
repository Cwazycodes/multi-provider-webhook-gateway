"""Guards for the pre-commit hooks set up in WG-05."""

import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_HOOKS = {
    "ruff-check",
    "ruff-format",
    "mypy",
    "end-of-file-fixer",
    "check-yaml",
    "gitleaks",
    "cfn-lint",
}
PYTHON_HOOKS = {"ruff-check", "ruff-format", "mypy", "cfn-lint"}


def all_hooks() -> list[dict[str, Any]]:
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
    return [hook for repo in config["repos"] for hook in repo["hooks"]]


def commit_hooks() -> dict[str, dict[str, Any]]:
    """The hooks that run on every commit: those not limited to other stages."""
    return {
        hook["id"]: hook
        for hook in all_hooks()
        if "pre-commit" in hook.get("stages", ["pre-commit"])
    }


def tracked_files() -> list[Path]:
    listing = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return [ROOT / name for name in listing.stdout.split("\0") if name]


def test_required_hooks_run_on_every_commit() -> None:
    missing = REQUIRED_HOOKS - commit_hooks().keys()
    assert not missing


def test_check_yaml_accepts_cloudformation_tags() -> None:
    assert "--unsafe" in commit_hooks()["check-yaml"].get("args", [])


def test_python_hooks_use_the_locked_tools() -> None:
    for hook_id in PYTHON_HOOKS:
        assert commit_hooks()[hook_id]["entry"].startswith("poetry run "), hook_id


def test_cfn_lint_runs_whenever_the_template_changes() -> None:
    hook = commit_hooks()["cfn-lint"]

    assert re.fullmatch(hook["files"], "template.yaml")
    assert hook["pass_filenames"] is False  # .cfnlintrc.yaml names the templates


def test_history_scan_hook_runs_only_on_demand() -> None:
    history = [hook for hook in all_hooks() if hook.get("alias") == "gitleaks-history"]
    assert len(history) == 1
    assert history[0]["stages"] == ["manual"]


def test_gitleaks_has_no_allowlist() -> None:
    inline_allow = "gitleaks" + ":allow"  # split so this file doesn't match itself
    files = tracked_files()

    assert not [path for path in files if path.name == ".gitleaksignore"]
    assert not [path for path in files if inline_allow in path.read_text(errors="ignore")]
    gitleaks_toml = ROOT / ".gitleaks.toml"
    assert not gitleaks_toml.exists() or "allowlist" not in gitleaks_toml.read_text()
