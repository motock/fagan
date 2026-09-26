"""The ``record_security_audit`` MCP tool stamps a repo as audited.

It is what resets the ``security_audit_due`` notice: after a review the user
calls it, and the repo's audited commit moves to that point. Only the state
directory is stubbed; git runs for real against a throwaway repo.
"""

import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pipeline import paths, security_audit
from pipeline import server as p

_REFERENCE = (Path(__file__).resolve().parents[2] / "REFERENCE.md").read_text()


def _git(repo, *args):
    return subprocess.run(
        ["git", "-c", "user.email=t@e.com", "-c", "user.name=t", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    for n in range(3):
        (root / "f.txt").write_text(str(n))
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", f"c{n}")
    return root


@pytest.fixture
def plans(tmp_path, monkeypatch):
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(paths, "PLAN_DIR", directory)
    return directory


def test_it_records_head_when_no_sha_is_given(plans, repo):
    head = _git(repo, "rev-parse", "HEAD")

    result = p.record_security_audit(str(repo))

    assert result["ok"] is True
    assert result["last_audited_sha"] == head
    assert security_audit.read_audit_state(plans, str(repo))["last_audited_sha"] == head


def test_it_records_an_explicit_sha(plans, repo):
    first = _git(repo, "rev-list", "--max-parents=0", "HEAD")

    result = p.record_security_audit(str(repo), first)

    assert result["last_audited_sha"] == first


def test_an_abbreviated_sha_is_recorded_in_full(plans, repo):
    first = _git(repo, "rev-list", "--max-parents=0", "HEAD")

    result = p.record_security_audit(str(repo), first[:8])

    assert result["last_audited_sha"] == first


@pytest.mark.parametrize("sha", ["f" * 40, "not-a-sha", "--all", "HEAD; rm -rf /", ""])
def test_a_bad_sha_is_rejected_and_nothing_is_recorded(plans, repo, sha):
    result = p.record_security_audit(str(repo), sha)

    assert result["ok"] is False
    assert "sha" in result["error"]
    assert security_audit.read_audit_state(plans, str(repo)) is None


def test_a_relative_repo_root_is_rejected(plans, repo):
    result = p.record_security_audit("repo")

    assert result["ok"] is False
    assert "repo_root" in result["error"]


def test_a_missing_directory_is_rejected(plans, tmp_path):
    result = p.record_security_audit(str(tmp_path / "gone"))

    assert result["ok"] is False
    assert "repo_root" in result["error"]


def test_a_directory_that_is_not_a_git_repo_is_rejected(plans, tmp_path):
    bare_dir = tmp_path / "plain"
    bare_dir.mkdir()

    result = p.record_security_audit(str(bare_dir))

    assert result["ok"] is False
    assert security_audit.read_audit_state(plans, str(bare_dir)) is None


def test_a_recorded_repo_is_no_longer_due(plans, repo):
    result = p.record_security_audit(str(repo))
    state = security_audit.read_audit_state(plans, str(repo))
    commits = security_audit.commits_since(str(repo), result["last_audited_sha"])

    assert security_audit.audit_due_reason(state, commits, datetime.now(timezone.utc), 1, 1) is None


def test_the_tool_is_registered_on_the_mcp_server():
    registered = p.mcp._tool_manager._tools["record_security_audit"]

    assert registered.fn is p.record_security_audit


def test_server_module_stays_within_the_line_cap():
    lines = (Path(p.__file__)).read_text().splitlines()

    assert len(lines) <= 1000


def test_reference_documents_the_tool_and_the_reminder_section():
    assert "record_security_audit" in _REFERENCE
    assert "## Periodic security-audit reminder" in _REFERENCE
