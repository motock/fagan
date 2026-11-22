"""Acceptance-fixture pytest dry-run collects from inside the repository.

A sandboxed command (tenant_exec) mounts only the story's worktree, so a
fixture directory under the system temp dir does not exist inside the sandbox
and collection cannot run. When ``repo_root`` is set the fixtures are
materialised in a ``.pipeline-fixtures-*`` directory inside that repo instead.
``_commit_wip`` must never commit such a directory left behind by a killed
process.
"""

import subprocess
from pathlib import Path

from pipeline.build_detect import _pytest_acceptance_fixtures
from pipeline.git_ops import _commit_wip

STORY = {
    "summary": "s",
    "acceptance": [{"path": "tests/acceptance_x.py", "source": "def test_x():\n    assert True\n"}],
}


def _install_fake_run(monkeypatch, returncode=0, raise_timeout=False):
    """Stub the process boundary (shutil.which + subprocess.run) and record argv/cwd."""
    monkeypatch.setattr("pipeline.build_detect.shutil.which", lambda name: "/usr/bin/pytest")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["cwd"] = kwargs.get("cwd")
        if raise_timeout:
            raise subprocess.TimeoutExpired(cmd, timeout=120)
        return subprocess.CompletedProcess(cmd, returncode=returncode, stdout="", stderr="")

    monkeypatch.setattr("pipeline.build_detect.subprocess.run", fake_run)
    return captured


def _collected_path(captured):
    """The fixture directory is the last argv element pytest is asked to collect."""
    return Path(captured["cmd"][-1])


def test_collects_from_directory_inside_repo_root(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    captured = _install_fake_run(monkeypatch)

    outcome = _pytest_acceptance_fixtures(STORY, repo_root=str(repo))

    assert outcome == ("clean", None)
    collected = _collected_path(captured)
    assert collected.parent == repo
    assert collected.name.startswith(".pipeline-fixtures-")


def test_fixture_directory_removed_after_collection_inside_repo(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    captured = _install_fake_run(monkeypatch)

    _pytest_acceptance_fixtures(STORY, repo_root=str(repo))

    assert not _collected_path(captured).exists()


def test_repo_root_unset_collects_outside_current_directory(monkeypatch, tmp_path):
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    captured = _install_fake_run(monkeypatch)

    outcome = _pytest_acceptance_fixtures(STORY)

    assert outcome == ("clean", None)
    collected = _collected_path(captured)
    assert workdir not in collected.parents
    assert not collected.name.startswith(".pipeline-fixtures-")
    assert captured["cwd"] == str(collected)


def test_timeout_removes_fixture_directory_and_skips(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    captured = _install_fake_run(monkeypatch, raise_timeout=True)

    outcome = _pytest_acceptance_fixtures(STORY, repo_root=str(repo))

    assert outcome[0] == "skipped"
    assert "timed out" in outcome[1]
    assert not _collected_path(captured).exists()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
    ).stdout


def test_commit_wip_leaves_fixture_directory_out_of_commit(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test Runner")
    (repo / "code.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")

    (repo / "code.py").write_text("x = 2\n")
    fixture_dir = repo / ".pipeline-fixtures-abc"
    fixture_dir.mkdir()
    (fixture_dir / "test_x.py").write_text("def test_x():\n    assert True\n")

    _commit_wip(str(repo), "S1", "checkpoint")

    committed = _git(repo, "show", "--name-only", "--pretty=format:", "HEAD").split()
    assert "code.py" in committed
    assert not any(name.startswith(".pipeline-fixtures-") for name in committed)
    assert "test_x.py" not in committed
    assert fixture_dir.joinpath("test_x.py").exists()
