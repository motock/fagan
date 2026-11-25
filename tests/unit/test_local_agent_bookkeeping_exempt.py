"""Agent bookkeeping files are exempt from the off-task guard and from the
agent's automatic commits.

Guard half: version numbers, absolute/home paths and the agent's own
``.agent_*`` runtime files are not off-task project paths.

Commit half: ``auto_wip_commit_impl`` and ``auto_commit_impl`` never stage
the bookkeeping files, even when the repository does not ignore them.
"""
import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

os.environ.setdefault("LOCAL_AGENT_MODEL", "test-model")
_spec = importlib.util.spec_from_file_location(
    "local_agent", str(Path(__file__).parent.parent.parent / "scripts" / "local_agent.py")
)
la = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(la)

from scripts.local_agent_git import auto_wip_commit_impl
from scripts.local_agent_oracle_git import auto_commit_impl

EXPECTED = {"pom.xml"}


# --- off-task guard: bash command paths -----------------------------------

def test_bash_guard_ignores_version_number_and_flags_real_output_path():
    assert la._bash_off_task_path("grep -c 4.2.0 pom.xml > out.txt", EXPECTED) == "out.txt"


def test_bash_guard_ignores_leading_dot_agent_checkpoint_redirect():
    assert la._bash_off_task_path("cat > .agent_checkpoint.tmp <<'EOF'", EXPECTED) is None


def test_bash_guard_ignores_absolute_path_outside_worktree():
    assert la._bash_off_task_path("curl -s https://example.org/x > /tmp/h.xml", EXPECTED) is None


def test_bash_guard_ignores_home_path():
    assert la._bash_off_task_path("echo x >> ~/notes.txt", EXPECTED) is None


def test_bash_guard_still_flags_off_task_in_place_edit():
    assert la._bash_off_task_path("sed -i s/a/b/ src/Other.java", EXPECTED) == "src/Other.java"


def test_bash_guard_still_flags_ordinary_temp_file_outside_agent_prefix():
    assert la._bash_off_task_path("echo x > notes.tmp", EXPECTED) == "notes.tmp"


def test_bash_guard_ignores_dot_slash_agent_scratchpad():
    assert la._bash_off_task_path("echo x > ./.agent_scratchpad.md", EXPECTED) is None


def test_bash_guard_returns_none_for_empty_command():
    assert la._bash_off_task_path("", EXPECTED) is None


def test_bash_guard_returns_none_when_brief_names_no_files():
    assert la._bash_off_task_path("sed -i s/a/b/ src/Other.java", set()) is None


# --- off-task guard: file-edit tool paths ---------------------------------

def test_path_guard_exempts_agent_scratchpad():
    assert la._is_off_task_path(".agent_scratchpad.md", EXPECTED) is False


def test_path_guard_still_flags_off_task_project_file():
    assert la._is_off_task_path("src/Other.java", EXPECTED) is True


def test_path_guard_returns_false_for_empty_expected():
    assert la._is_off_task_path("src/Other.java", set()) is False


# --- commit helpers -------------------------------------------------------

def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, check=False, capture_output=True, text=True)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "wt"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.org")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("seed\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def _origin(repo: Path) -> dict:
    def git(*args):
        return _git(repo, *args)

    return {"CWD": repo, "git": git}


def _write_work_and_bookkeeping(repo: Path) -> None:
    (repo / "pom.xml").write_text("<project/>\n")
    (repo / ".agent_scratchpad.md").write_text("notes\n")
    (repo / ".agent_checkpoint").write_text("step 3\n")
    (repo / ".agent_checkpoint.tmp").write_text("step 4\n")


def _tracked_files(repo: Path) -> set[str]:
    return set(_git(repo, "show", "--name-only", "--format=", "HEAD").stdout.split())


@pytest.mark.parametrize("commit_impl", [auto_wip_commit_impl, auto_commit_impl], ids=["wip", "oracle"])
def test_auto_commit_stages_only_project_files(tmp_path: Path, commit_impl):
    repo = _make_repo(tmp_path)
    _write_work_and_bookkeeping(repo)
    commit_impl(_origin(repo), "parked on off-task drift")
    assert _tracked_files(repo) == {"pom.xml"}


@pytest.mark.parametrize("commit_impl", [auto_wip_commit_impl, auto_commit_impl], ids=["wip", "oracle"])
def test_auto_commit_leaves_bookkeeping_files_on_disk_and_untracked(tmp_path: Path, commit_impl):
    repo = _make_repo(tmp_path)
    _write_work_and_bookkeeping(repo)
    commit_impl(_origin(repo), "parked on off-task drift")
    for name in (".agent_scratchpad.md", ".agent_checkpoint", ".agent_checkpoint.tmp"):
        assert (repo / name).exists(), name
    status = _git(repo, "status", "--porcelain").stdout
    assert "?? .agent_scratchpad.md" in status
    assert "?? .agent_checkpoint" in status
    assert "?? .agent_checkpoint.tmp" in status


@pytest.mark.parametrize("commit_impl", [auto_wip_commit_impl, auto_commit_impl], ids=["wip", "oracle"])
def test_auto_commit_makes_no_commit_when_only_bookkeeping_changed(tmp_path: Path, commit_impl):
    repo = _make_repo(tmp_path)
    head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / ".agent_scratchpad.md").write_text("notes\n")
    (repo / ".agent_checkpoint.tmp").write_text("step 4\n")
    commit_impl(_origin(repo), "parked on off-task drift")
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head_before
