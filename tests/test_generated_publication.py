"""Exercise the workflow publication commands against a real, isolated Git remote."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parent.parent / ".github/workflows/validate-and-release.yml"
PUBLISHERS = (
    (
        "commit-release-specs",
        "Open a PR if the published specs changed",
        "sync/regenerate-release-specs",
        681,
    ),
    ("update-docs", "Commit and PR if changed", "docs/update-validation-report", 723),
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(cwd), *args], text=True).strip()


@pytest.fixture
def publication_repo(tmp_path: Path):
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    work = tmp_path / "work"
    runner = tmp_path / "runner"
    bin_dir = tmp_path / "bin"
    for directory in (seed, runner, bin_dir):
        directory.mkdir()
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(seed, "init", "-b", "main")
    _git(seed, "config", "user.name", "Test Publisher")
    _git(seed, "config", "user.email", "publisher@example.com")
    (seed / "release/specs").mkdir(parents=True)
    (seed / "docs/en").mkdir(parents=True)
    (seed / "release/specs/old.json").write_text('{"title": "old"}\n')
    (seed / "release/specs/.spec_metadata.json").write_text(
        json.dumps({"archive_sha256": "old", "download_timestamp": "old"}) + "\n"
    )
    (seed / "docs/en/01-validation-report.mdx").write_text("Old report\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "chore: initial fixture")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "main")
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    subprocess.run(["git", "clone", str(remote), str(work)], check=True, capture_output=True)
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$GH_CALL_LOG"\n'
        'if [ "$1 $2" = "pr list" ] && [ "${PR_EXISTS:-}" = true ]; then echo 42; fi\n'
    )
    gh.chmod(0o700)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "RUNNER_TEMP": str(runner),
        "GITHUB_REPOSITORY": "fixture/api-specs",
        "GH_TOKEN": "fixture",
        "GH_CALL_LOG": str(tmp_path / "gh.log"),
        "GIT_TERMINAL_PROMPT": "0",
    }
    return remote, seed, work, env


def _command(job: str, name: str, remote: Path) -> str:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    command = next(
        step["run"] for step in workflow["jobs"][job]["steps"] if step.get("name") == name
    )
    return command.replace("https://github.com/${GITHUB_REPOSITORY}.git", str(remote))


def _generate(work: Path, job: str, text: str) -> None:
    if job == "commit-release-specs":
        (work / "release/specs/old.json").unlink(missing_ok=True)
        (work / "release/specs/new.json").write_text(json.dumps({"title": text}) + "\n")
    else:
        (work / "docs/en/01-validation-report.mdx").write_text(text + "\n")


@pytest.mark.parametrize(("job", "name", "branch", "issue"), PUBLISHERS)
def test_publication_reuses_pr_and_rebuilds_from_current_main(
    publication_repo, job, name, branch, issue
):
    remote, seed, work, env = publication_repo
    command = _command(job, name, remote)
    _generate(work, job, "first")
    subprocess.run(["bash", "-e", "-o", "pipefail", "-c", command], cwd=work, env=env, check=True)
    first_head = _git(remote, "rev-parse", f"refs/heads/{branch}")
    assert (
        _git(remote, "show", f"{branch}:release/specs/old.json")
        if job == "update-docs"
        else ("old.json" not in _git(remote, "ls-tree", "-r", "--name-only", branch))
    )
    (seed / "main-change.txt").write_text("New upstream main content\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "chore: advance main")
    _git(seed, "push", "origin", "main")
    main_head = _git(seed, "rev-parse", "HEAD")
    _generate(work, job, "second")
    env["PR_EXISTS"] = "true"
    subprocess.run(["bash", "-e", "-o", "pipefail", "-c", command], cwd=work, env=env, check=True)
    assert _git(remote, "rev-parse", f"{branch}^") == main_head
    assert _git(remote, "rev-parse", branch) != first_head
    assert _git(remote, "show", f"{branch}:main-change.txt") == "New upstream main content"
    calls = Path(env["GH_CALL_LOG"]).read_text().splitlines()
    assert sum(call.startswith("pr create ") for call in calls) == 1
    assert sum(call.startswith("pr edit ") for call in calls) == 1
    assert f"Closes #{issue}" in next(call for call in calls if call.startswith("pr edit "))
    assert not (Path(env["RUNNER_TEMP"]) / "git-askpass-helper.sh").exists()


@pytest.mark.parametrize(("job", "name", "branch", "issue"), PUBLISHERS)
def test_publication_rejects_a_concurrent_branch_change(publication_repo, job, name, branch, issue):
    remote, seed, work, env = publication_repo
    command = _command(job, name, remote)
    _generate(work, job, "first")
    subprocess.run(["bash", "-e", "-o", "pipefail", "-c", command], cwd=work, env=env, check=True)
    (seed / "concurrent.txt").write_text("Concurrent change\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "chore: concurrent candidate")
    _git(seed, "push", "origin", "HEAD:refs/heads/concurrent")
    concurrent_head = _git(seed, "rev-parse", "HEAD")
    _generate(work, job, "second")
    # Simulate the other writer after observation and immediately before the actual push.
    push_line = next(line for line in command.splitlines() if "git push " in line)
    race = f'git -C "{remote}" update-ref "refs/heads/{branch}" "{concurrent_head}"\n'
    command = command.replace(push_line, race + push_line)
    before_calls = Path(env["GH_CALL_LOG"]).read_text()
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", command],
        cwd=work,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "stale info" in result.stderr
    assert _git(remote, "rev-parse", branch) == concurrent_head
    assert Path(env["GH_CALL_LOG"]).read_text() == before_calls


def test_specs_ignore_download_time_churn_but_publish_changed_provenance(publication_repo):
    remote, seed, work, env = publication_repo
    job, name, branch, _ = PUBLISHERS[0]
    metadata = work / "release/specs/.spec_metadata.json"
    metadata.write_text(json.dumps({"archive_sha256": "old", "download_timestamp": "new"}) + "\n")
    command = _command(job, name, remote)
    subprocess.run(["bash", "-e", "-o", "pipefail", "-c", command], cwd=work, env=env, check=True)
    assert not Path(env["GH_CALL_LOG"]).exists()
    assert _git(work, "status", "--porcelain") == ""
    metadata.write_text(json.dumps({"archive_sha256": "new", "download_timestamp": "new"}) + "\n")
    subprocess.run(["bash", "-e", "-o", "pipefail", "-c", command], cwd=work, env=env, check=True)
    assert (
        json.loads(_git(remote, "show", f"{branch}:release/specs/.spec_metadata.json"))[
            "archive_sha256"
        ]
        == "new"
    )
