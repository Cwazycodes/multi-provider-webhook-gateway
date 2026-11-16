"""Guards for the project scaffold set up in WG-03."""

import importlib
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet

ROOT = Path(__file__).resolve().parents[1]
SUBPACKAGES = ["providers", "ingest", "processor", "replay", "common"]


def _pyproject() -> dict[str, Any]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())


@pytest.mark.parametrize("name", SUBPACKAGES)
def test_subpackage_is_importable(name: str) -> None:
    importlib.import_module(f"webhook_gateway.{name}")


def test_python_version_is_pinned_consistently() -> None:
    pinned = (ROOT / ".python-version").read_text().strip()
    running = f"{sys.version_info.major}.{sys.version_info.minor}"
    allowed = SpecifierSet(_pyproject()["project"]["requires-python"])

    assert running == pinned, f"venv runs {running}, .python-version says {pinned}"
    assert f"{pinned}.0" in allowed, f"requires-python {allowed} excludes {pinned}"


def test_boto3_is_not_a_runtime_dependency() -> None:
    runtime = {Requirement(dep).name.lower() for dep in _pyproject()["project"]["dependencies"]}
    assert not runtime & {"boto3", "botocore"}, "the Lambda runtime already ships boto3"
