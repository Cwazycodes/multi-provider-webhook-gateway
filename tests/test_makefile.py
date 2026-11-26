"""The Makefile is the repo's single entry point (WG-04)."""

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = ROOT / "Makefile"
REQUIRED_TARGETS = {
    "help",
    "install",
    "fmt",
    "lint",
    "typecheck",
    "test",
    "check",
    "hooks",
    "scan-secrets",
    "schema",
    "generate",
    "validate",
    "build",
}

# A parent make (`make test` runs pytest) exports these, and a shell may export
# the AWS ones. Any of them would change what the make under test does.
LEAKY_ENV = {"MAKEFLAGS", "MFLAGS", "MAKELEVEL", "STAGE", "AWS_PROFILE"}

# Test-only targets, loaded next to the real Makefile so it needs no fakes.
PROBES = """
.PHONY: probe-pipefail probe-unset aws-probe probe-telemetry
probe-pipefail:
\tfalse | true
probe-unset:
\techo "$$NOT_SET_ANYWHERE"
aws-probe:
\t$(require-aws)
\t@echo "would reach AWS: $(STAGE) as $(AWS_PROFILE)"
probe-telemetry:
\t@echo "telemetry=$$SAM_CLI_TELEMETRY"
"""


def run_make(
    *args: str, probes: Path | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    clean_env = {k: v for k, v in os.environ.items() if k not in LEAKY_ENV} | (env or {})
    files = ["-f", str(MAKEFILE)] + (["-f", str(probes)] if probes else [])
    return subprocess.run(
        ["make", *files, *args],
        cwd=ROOT,
        env=clean_env,
        capture_output=True,
        text=True,
        check=False,
    )


def defined_targets() -> set[str]:
    return set(re.findall(r"^([a-zA-Z0-9][\w-]*):(?!=)", MAKEFILE.read_text(), re.MULTILINE))


def phony_targets() -> set[str]:
    lines = re.findall(r"^\.PHONY:(.*)$", MAKEFILE.read_text(), re.MULTILINE)
    return {name for line in lines for name in line.split()}


@pytest.fixture
def probes(tmp_path: Path) -> Path:
    path = tmp_path / "probes.mk"
    path.write_text(PROBES)
    return path


def test_required_targets_exist() -> None:
    missing = REQUIRED_TARGETS - defined_targets()
    assert not missing


def test_every_target_is_phony() -> None:
    not_phony = defined_targets() - phony_targets()
    assert not not_phony


def test_help_is_the_default_goal_and_lists_every_target() -> None:
    result = run_make()

    assert result.returncode == 0, result.stderr
    listed = set(re.findall(r"^  make (\S+) +\S", result.stdout, re.MULTILINE))
    assert listed == defined_targets()


def test_check_runs_lint_typecheck_and_test() -> None:
    dry_run = run_make("--dry-run", "check")

    assert dry_run.returncode == 0, dry_run.stderr
    for command in ("ruff check", "ruff format --check", "mypy", "pytest"):
        assert command in dry_run.stdout


@pytest.mark.parametrize("probe", ["probe-pipefail", "probe-unset"])
def test_recipes_run_in_strict_bash(probes: Path, probe: str) -> None:
    result = run_make(probe, probes=probes)

    # The recipe itself must fail, not make (e.g. a missing Makefile).
    assert f"{probe}] Error 1" in result.stderr


@pytest.mark.parametrize(
    ("args", "env", "missing"),
    [
        pytest.param([], {}, "STAGE", id="neither"),
        pytest.param(["STAGE=dev"], {}, "AWS_PROFILE", id="no-profile"),
        pytest.param(["AWS_PROFILE=dev"], {}, "STAGE", id="no-stage"),
        pytest.param(["STAGE=", "AWS_PROFILE=dev"], {}, "STAGE", id="empty-stage"),
        pytest.param([], {"STAGE": "dev", "AWS_PROFILE": "dev"}, "STAGE", id="only-in-shell"),
    ],
)
def test_aws_targets_refuse_without_explicit_stage_and_profile(
    probes: Path, args: list[str], env: dict[str, str], missing: str
) -> None:
    result = run_make("aws-probe", *args, probes=probes, env=env)

    assert result.returncode != 0
    assert f"pass {missing} explicitly" in result.stderr
    assert "would reach AWS" not in result.stdout


def test_aws_targets_run_with_explicit_stage_and_profile(probes: Path) -> None:
    result = run_make("aws-probe", "STAGE=dev", "AWS_PROFILE=webhook-dev", probes=probes)

    assert result.returncode == 0, result.stderr
    assert "would reach AWS: dev as webhook-dev" in result.stdout


def test_test_target_writes_the_coverage_xml_ci_uploads() -> None:
    dry_run = run_make("--dry-run", "test")

    assert dry_run.returncode == 0, dry_run.stderr
    assert "--cov-report=xml" in dry_run.stdout


def test_validate_lints_with_cfn_lint_and_needs_no_aws_account() -> None:
    dry_run = run_make("--dry-run", "validate")

    assert dry_run.returncode == 0, dry_run.stderr
    assert dry_run.stdout == "sam validate --lint --region eu-west-2\n"


def test_build_exports_the_runtime_dependencies_then_runs_sam_build() -> None:
    dry_run = run_make("--dry-run", "build")

    assert dry_run.returncode == 0, dry_run.stderr
    assert [line.strip() for line in dry_run.stdout.splitlines()] == [
        "poetry export --only main --format requirements.txt --output src/requirements.txt",
        "sam build",
    ]


def test_build_can_use_a_container_instead_of_a_local_python() -> None:
    dry_run = run_make("--dry-run", "build", "SAM_BUILD_ARGS=--use-container")

    assert "sam build --use-container" in dry_run.stdout


def test_sam_telemetry_is_off_for_every_recipe(probes: Path) -> None:
    result = run_make("probe-telemetry", probes=probes, env={"SAM_CLI_TELEMETRY": "1"})

    assert result.stdout.strip() == "telemetry=0"


def test_the_exported_requirements_are_never_committed() -> None:
    ignored = subprocess.run(
        ["git", "check-ignore", "--quiet", "src/requirements.txt"], cwd=ROOT, check=False
    )

    assert ignored.returncode == 0
