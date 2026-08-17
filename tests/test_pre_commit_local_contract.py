"""Fail-closed contracts for the repository-local pre-commit gate."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
HOOK = ROOT / "scripts" / "pre-commit-local.sh"


def _prepare_repository(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", tmp_path], check=True)
    source = tmp_path / "changed.py"
    source.write_text("value = 1\n")
    subprocess.run(["git", "-C", tmp_path, "add", "changed.py"], check=True)
    hook = tmp_path / "scripts" / "pre-commit-local.sh"
    hook.parent.mkdir()
    hook.write_bytes(HOOK.read_bytes())
    return hook


def test_local_gate_has_no_declared_fail_open_checks() -> None:
    source = HOOK.read_text()

    assert "|| true" not in source
    assert "skipping" not in source.lower()
    assert 'ruff" format --check' in source
    assert 'mypy" "${python_files[@]}"' in source
    assert "bandit" not in source
    assert "typos" not in source


def test_no_staged_python_files_is_a_valid_noop(tmp_path: Path) -> None:

    subprocess.run(["git", "init", "-q", tmp_path], check=True)
    hook = tmp_path / "scripts" / "pre-commit-local.sh"
    hook.parent.mkdir()
    hook.write_bytes(HOOK.read_bytes())

    result = subprocess.run(
        ["bash", hook],
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "All repo-specific checks passed" in result.stdout


def test_missing_required_python_tools_fails_the_gate(tmp_path: Path) -> None:
    hook = _prepare_repository(tmp_path)

    result = subprocess.run(
        ["bash", hook],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "required tool is unavailable" in result.stderr


def test_linter_failure_is_propagated(tmp_path: Path) -> None:
    hook = _prepare_repository(tmp_path)

    bin_dir = tmp_path / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    ruff = bin_dir / "ruff"
    ruff.write_text("#!/bin/sh\nexit 23\n")
    ruff.chmod(0o755)
    mypy = bin_dir / "mypy"
    mypy.write_text("#!/bin/sh\nexit 0\n")
    mypy.chmod(0o755)

    result = subprocess.run(
        ["bash", hook],
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 23


def test_mypy_policy_has_one_repository_configuration() -> None:
    assert not (ROOT / ".mypy.ini").exists()
    assert "[tool.mypy]" in (ROOT / "pyproject.toml").read_text()
    assert "mypy scripts/ tests/" in (ROOT / "Makefile").read_text()
