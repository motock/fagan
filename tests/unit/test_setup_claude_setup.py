"""``pipeline.setup.claude_setup`` installs the Claude Code assets non-destructively.

The Python guided installer must reach the same end state as
``scripts/remote-install.sh``: persona subagents and the overlord policy copied
into ``~/.claude`` (never overwriting), the ``pipeline`` MCP server registered
with Claude Code (never re-registered, never removed), and the global rules
bundle installed only when the caller explicitly opted in.

Every process invocation goes through an injectable ``runner`` and ``which``, so
these tests never touch the real Claude Code CLI, the network, or the real
``$HOME``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from pipeline.setup import claude_setup

REPO_ROOT = Path(__file__).resolve().parents[2]
REMOTE_INSTALL = REPO_ROOT / "scripts" / "remote-install.sh"
GLOBAL_RULES_SCRIPT = REPO_ROOT / "scripts" / "install_global_rules.py"

BASH = shutil.which("bash") or "/bin/bash"
GIT = shutil.which("git")

# Every argv recorded by any runner in this module, so the "never remove" rule
# is checked across every test rather than only where it is convenient.
ALL_RECORDED_ARGV: list[list[str]] = []


class RecordingRunner:
    """Fake ``runner``: records argv and replays canned results by subcommand."""

    def __init__(
        self,
        *,
        get_returncode: int = 1,
        add_returncode: int = 0,
        stderr: str = "",
        other_returncode: int = 0,
        raise_oserror: bool = False,
    ) -> None:
        self.calls: list[list[str]] = []
        self.timeouts: list[float | None] = []
        self.get_returncode = get_returncode
        self.add_returncode = add_returncode
        self.stderr = stderr
        self.other_returncode = other_returncode
        self.raise_oserror = raise_oserror

    def __call__(self, argv, timeout=None):
        argv = list(argv)
        assert "remove" not in argv, f"claude mcp remove must never run: {argv}"
        self.calls.append(argv)
        ALL_RECORDED_ARGV.append(argv)
        self.timeouts.append(timeout)
        if self.raise_oserror:
            raise OSError("boom: simulated exec failure")
        if argv[1:3] == ["mcp", "get"]:
            return self.get_returncode, "", ""
        if argv[1:3] == ["mcp", "add"]:
            return self.add_returncode, "", self.stderr
        return self.other_returncode, "", self.stderr

    @property
    def mcp_calls(self) -> list[list[str]]:
        return [call for call in self.calls if call[1:2] == ["mcp"]]


def _assert_no_remove(calls: list[list[str]]) -> None:
    for argv in calls:
        assert "remove" not in argv, f"claude mcp remove must never run: {argv}"


def _make_repo(
    root: Path,
    *,
    personas: dict[str, str] | None = None,
    policy: str | None = "policy v2\n",
    venv_python: bool = True,
    server: bool = True,
    agents_dir: bool = True,
) -> Path:
    """Build a synthetic repo root with the assets the installer consumes."""
    root.mkdir(parents=True, exist_ok=True)
    if agents_dir:
        (root / "agents").mkdir(exist_ok=True)
        for name, body in (personas or {}).items():
            (root / "agents" / name).write_text(body, encoding="utf-8")
    if policy is not None:
        (root / "overlord-policy.md").write_text(policy, encoding="utf-8")
    if venv_python:
        (root / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
        python = root / ".venv" / "bin" / "python3"
        python.write_text("#!/bin/sh\n", encoding="utf-8")
        python.chmod(0o755)
    if server:
        (root / "app").mkdir(parents=True, exist_ok=True)
        (root / "app" / "pipeline_mcp_server.py").write_text("# server\n", encoding="utf-8")
    return root


def _expected_add(repo: Path) -> list[str]:
    return [
        "claude",
        "mcp",
        "add",
        "-s",
        "user",
        "pipeline",
        str(repo / ".venv" / "bin" / "python3"),
        str(repo / "app" / "pipeline_mcp_server.py"),
    ]


def _manual(repo: Path) -> str:
    return (
        "claude mcp add -s user pipeline "
        f"{repo / '.venv' / 'bin' / 'python3'} {repo / 'app' / 'pipeline_mcp_server.py'}"
    )


# --------------------------------------------------------------------------
# install_claude_assets: positive
# --------------------------------------------------------------------------


def test_fresh_repo_copies_every_persona_byte_for_byte_and_the_policy(tmp_path: Path) -> None:
    personas = {
        "code-reviewer.md": "reviewer v2\n",
        "overlord.md": "overlord v2 — ünïcode\n",
    }
    repo = _make_repo(tmp_path / "repo", personas=personas)
    home = tmp_path / "home"

    result = claude_setup.install_claude_assets(repo_root=repo, home=home)

    agents = home / ".claude" / "agents"
    for name, body in personas.items():
        assert (agents / name).read_bytes() == body.encode("utf-8")
    assert (home / ".claude" / "overlord-policy.md").read_text(encoding="utf-8") == "policy v2\n"
    assert result == {
        "installed": ["code-reviewer.md", "overlord-policy.md", "overlord.md"],
        "kept": [],
    }


def test_existing_persona_and_policy_are_kept_while_missing_siblings_install(
    tmp_path: Path,
) -> None:
    repo = _make_repo(
        tmp_path / "repo",
        personas={"code-reviewer.md": "reviewer v2\n", "overlord.md": "overlord v2\n"},
    )
    home = tmp_path / "home"
    agents = home / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "code-reviewer.md").write_text("my customized reviewer\n", encoding="utf-8")
    (home / ".claude" / "overlord-policy.md").write_text("my policy\n", encoding="utf-8")

    result = claude_setup.install_claude_assets(repo_root=repo, home=home)

    assert (agents / "code-reviewer.md").read_text(encoding="utf-8") == "my customized reviewer\n"
    assert (home / ".claude" / "overlord-policy.md").read_text(encoding="utf-8") == "my policy\n"
    assert (agents / "overlord.md").read_text(encoding="utf-8") == "overlord v2\n"
    assert result == {"installed": ["overlord.md"], "kept": ["code-reviewer.md", "overlord-policy.md"]}


def test_install_creates_claude_directories_with_mode_0755(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo", personas={"overlord.md": "o\n"})
    home = tmp_path / "home"

    claude_setup.install_claude_assets(repo_root=repo, home=home)

    for path in (home / ".claude", home / ".claude" / "agents"):
        assert path.is_dir()
        assert path.stat().st_mode & 0o777 == 0o755


def test_persona_name_with_spaces_is_copied_verbatim(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo", personas={"my persona.md": "spaced\n"}, policy=None)
    home = tmp_path / "home"

    result = claude_setup.install_claude_assets(repo_root=repo, home=home)

    assert (home / ".claude" / "agents" / "my persona.md").read_text(encoding="utf-8") == "spaced\n"
    assert result == {"installed": ["my persona.md"], "kept": []}


def test_empty_agents_directory_installs_nothing(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo", personas={}, policy=None)
    home = tmp_path / "home"

    result = claude_setup.install_claude_assets(repo_root=repo, home=home)

    assert result == {"installed": [], "kept": []}
    assert not (home / ".claude").exists()


def test_repo_without_agents_or_policy_creates_nothing(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo", agents_dir=False, policy=None)
    home = tmp_path / "home"

    result = claude_setup.install_claude_assets(repo_root=repo, home=home)

    assert result == {"installed": [], "kept": []}
    assert not (home / ".claude").exists()


def test_missing_source_directories_produce_empty_results(tmp_path: Path) -> None:
    repo = tmp_path / "empty-repo"
    repo.mkdir()

    result = claude_setup.install_claude_assets(repo_root=repo, home=tmp_path / "home")

    assert result == {"installed": [], "kept": []}
    assert not (tmp_path / "home" / ".claude").exists()


# --------------------------------------------------------------------------
# register_mcp_server: positive
# --------------------------------------------------------------------------


def test_mcp_registration_gets_then_adds_with_the_exact_argv(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")
    runner = RecordingRunner(get_returncode=1, add_returncode=0)

    result = claude_setup.register_mcp_server(
        repo_root=repo, home=tmp_path / "home", runner=runner, which=lambda _name: "/usr/bin/claude"
    )

    assert result == {"status": "registered"}
    assert runner.calls == [["claude", "mcp", "get", "pipeline"], _expected_add(repo)]
    assert runner.timeouts[1] == 30
    _assert_no_remove(runner.calls)


def test_pre_registered_server_produces_no_add(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")
    runner = RecordingRunner(get_returncode=0)

    result = claude_setup.register_mcp_server(
        repo_root=repo, home=tmp_path / "home", runner=runner, which=lambda _name: "/usr/bin/claude"
    )

    assert result == {"status": "kept"}
    assert runner.calls == [["claude", "mcp", "get", "pipeline"]]
    _assert_no_remove(runner.calls)


# --------------------------------------------------------------------------
# register_mcp_server: negative
# --------------------------------------------------------------------------


def test_claude_absent_skips_with_the_exact_manual_command(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")
    runner = RecordingRunner()

    result = claude_setup.register_mcp_server(
        repo_root=repo, home=tmp_path / "home", runner=runner, which=lambda _name: None
    )

    assert result["status"] == "skipped"
    assert result["manual"] == _manual(repo)
    assert runner.calls == []


def test_mcp_add_failure_returns_failed_with_manual_command_and_no_exception(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path / "repo")
    runner = RecordingRunner(get_returncode=1, add_returncode=1, stderr="Error: boom\n")

    result = claude_setup.register_mcp_server(
        repo_root=repo, home=tmp_path / "home", runner=runner, which=lambda _name: "/usr/bin/claude"
    )

    assert result["status"] == "failed"
    assert result["manual"] == _manual(repo)
    assert "boom" in result["error"]
    _assert_no_remove(runner.calls)


@pytest.mark.parametrize("missing", ["venv", "server"])
def test_missing_venv_or_server_skips_without_any_claude_call(
    tmp_path: Path, missing: str
) -> None:
    repo = _make_repo(
        tmp_path / "repo",
        venv_python=missing != "venv",
        server=missing != "server",
    )
    runner = RecordingRunner()

    result = claude_setup.register_mcp_server(
        repo_root=repo, home=tmp_path / "home", runner=runner, which=lambda _name: "/usr/bin/claude"
    )

    assert result["status"] == "skipped"
    assert result["reason"]
    assert runner.calls == []


def test_runner_raising_oserror_returns_failed_instead_of_raising(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")
    runner = RecordingRunner(raise_oserror=True)

    result = claude_setup.register_mcp_server(
        repo_root=repo, home=tmp_path / "home", runner=runner, which=lambda _name: "/usr/bin/claude"
    )

    assert result["status"] == "failed"
    assert result["error"]
    _assert_no_remove(runner.calls)


def test_runner_timeout_returns_failed_instead_of_raising(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")

    def runner(argv, timeout=None):
        raise subprocess.TimeoutExpired(cmd=list(argv), timeout=timeout or 0)

    result = claude_setup.register_mcp_server(
        repo_root=repo, home=tmp_path / "home", runner=runner, which=lambda _name: "/usr/bin/claude"
    )

    assert result["status"] == "failed"
    assert result["error"]


def test_result_never_leaks_paths_outside_repo_or_home(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")
    runner = RecordingRunner(get_returncode=1, add_returncode=1, stderr="Error: boom\n")

    result = claude_setup.register_mcp_server(
        repo_root=repo, home=tmp_path / "home", runner=runner, which=lambda _name: "/usr/bin/claude"
    )

    rendered = repr(result)
    assert str(tmp_path / "elsewhere") not in rendered
    assert "SECRET" not in rendered


# --------------------------------------------------------------------------
# global rules: opt-in only
# --------------------------------------------------------------------------


def test_register_and_install_assets_never_install_global_rules(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo", personas={"overlord.md": "o\n"})
    runner = RecordingRunner()

    claude_setup.register_mcp_server(
        repo_root=repo, home=tmp_path / "home", runner=runner, which=lambda _name: "/usr/bin/claude"
    )
    claude_setup.install_claude_assets(repo_root=repo, home=tmp_path / "home")

    assert not any("install_global_rules.py" in arg for argv in runner.calls for arg in argv)


def test_install_global_rules_dry_run_issues_the_exact_argv() -> None:
    runner = RecordingRunner()

    result = claude_setup.install_global_rules(tools=("claude",), runner=runner, dry_run=True)

    assert result == {"status": "dry-run", "error": None}
    assert runner.calls == [
        [
            sys.executable,
            str(GLOBAL_RULES_SCRIPT),
            "--tools=claude",
            "--dry-run",
        ]
    ]
    assert runner.timeouts == [120]


def test_install_global_rules_without_dry_run_omits_the_flag() -> None:
    runner = RecordingRunner()

    result = claude_setup.install_global_rules(tools=("claude", "codex"), runner=runner)

    assert result == {"status": "installed", "error": None}
    assert runner.calls == [
        [sys.executable, str(GLOBAL_RULES_SCRIPT), "--tools=claude,codex"]
    ]


@pytest.mark.parametrize("tools", [(), ("",), ("nope",), ("claude", "nope")])
def test_install_global_rules_rejects_empty_or_unknown_tools_before_any_process(
    tools: tuple[str, ...],
) -> None:
    runner = RecordingRunner()

    with pytest.raises(ValueError):
        claude_setup.install_global_rules(tools=tools, runner=runner)

    assert runner.calls == []


def test_install_global_rules_nonzero_exit_returns_failed_without_raising() -> None:
    runner = RecordingRunner(other_returncode=1, stderr="rules exploded\n")

    result = claude_setup.install_global_rules(tools=("claude",), runner=runner)

    assert result["status"] == "failed"
    assert "rules exploded" in result["error"]


def test_install_global_rules_runner_oserror_returns_failed() -> None:
    runner = RecordingRunner(raise_oserror=True)

    result = claude_setup.install_global_rules(tools=("claude",), runner=runner)

    assert result["status"] == "failed"
    assert result["error"]


# --------------------------------------------------------------------------
# convergence with scripts/remote-install.sh
# --------------------------------------------------------------------------

STUB_CLAUDE = """#!/usr/bin/env bash
echo "$*" >> "$CLAUDE_STUB_LOG"
if [ "$1 $2" = "mcp get" ]; then exit "${CLAUDE_GET_EXIT:-1}"; fi
if [ "$1 $2" = "mcp add" ]; then exit "${CLAUDE_ADD_EXIT:-0}"; fi
exit 0
"""


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run([GIT, *args], cwd=str(cwd), capture_output=True, text=True, check=True)


def _make_remote(tmp_path: Path) -> Path:
    """A local git repo standing in for GitHub, consumed by remote-install.sh."""
    repo = tmp_path / "remote"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "install.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'root="$(cd "$(dirname "$0")/.." && pwd)"\n'
        'mkdir -p "$root/.venv/bin"\n'
        "printf '#!/bin/sh\\n' > \"$root/.venv/bin/python3\"\n"
        'chmod +x "$root/.venv/bin/python3"\n',
        encoding="utf-8",
    )
    (repo / "agents").mkdir()
    (repo / "agents" / "code-reviewer.md").write_text("reviewer v2\n", encoding="utf-8")
    (repo / "agents" / "overlord.md").write_text("overlord v2\n", encoding="utf-8")
    (repo / "overlord-policy.md").write_text("policy v2\n", encoding="utf-8")
    (repo / "app").mkdir()
    (repo / "app" / "pipeline_mcp_server.py").write_text("# server\n", encoding="utf-8")
    _git(["init", "-q"], cwd=repo)
    _git(["add", "-A"], cwd=repo)
    _git(
        ["-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "init"],
        cwd=repo,
    )
    return repo


@pytest.mark.skipif(GIT is None, reason="git is not installed")
def test_python_path_converges_with_remote_install_sh(tmp_path: Path) -> None:
    remote = _make_remote(tmp_path)
    shell_home = tmp_path / "shell-home"
    shell_home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "git").symlink_to(GIT)
    stub = bin_dir / "claude"
    stub.write_text(STUB_CLAUDE, encoding="utf-8")
    stub.chmod(0o755)
    log = tmp_path / "claude.log"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FAGAN_", "CLAUDE"))}
    env.update(
        HOME=str(shell_home),
        PATH=f"{bin_dir}:/usr/bin:/bin",
        FAGAN_REPO_URL=str(remote),
        FAGAN_INSTALL_DIR=str(tmp_path / "install"),
        CLAUDE_STUB_LOG=str(log),
    )

    shell = subprocess.run(
        [BASH, str(REMOTE_INSTALL)], env=env, capture_output=True, text=True, timeout=120, check=False
    )
    assert shell.returncode == 0, shell.stderr

    # The Python path runs against an equivalent fixture repo: same assets, and
    # the venv python that install.sh would have created.
    py_repo = _make_repo(
        tmp_path / "py-repo",
        personas={"code-reviewer.md": "reviewer v2\n", "overlord.md": "overlord v2\n"},
    )
    py_home = tmp_path / "py-home"
    runner = RecordingRunner(get_returncode=1, add_returncode=0)
    claude_setup.install_claude_assets(repo_root=py_repo, home=py_home)
    claude_setup.register_mcp_server(
        repo_root=py_repo, home=py_home, runner=runner, which=lambda _name: "/usr/bin/claude"
    )

    def _files(root: Path) -> set[str]:
        return {
            str(path.relative_to(root))
            for path in root.rglob("*")
            if path.is_file()
        }

    assert _files(py_home / ".claude") == _files(shell_home / ".claude")

    shell_adds = [
        line for line in log.read_text(encoding="utf-8").splitlines() if line.startswith("mcp add ")
    ]
    py_adds = [" ".join(argv) for argv in runner.calls if argv[1:3] == ["mcp", "add"]]
    assert len(shell_adds) == 1
    assert len(py_adds) == 1
    shell_argv = shell_adds[0].split()
    py_argv = py_adds[0].split()
    # Same subcommand and scope; only the repo root differs between fixtures.
    assert py_argv[0] == "claude"
    assert shell_argv[:5] == py_argv[1:6] == ["mcp", "add", "-s", "user", "pipeline"]
    assert shell_argv[5:] == [
        str(tmp_path / "install" / ".venv" / "bin" / "python3"),
        str(tmp_path / "install" / "app" / "pipeline_mcp_server.py"),
    ]
    assert py_argv[6:] == [
        str(py_repo / ".venv" / "bin" / "python3"),
        str(py_repo / "app" / "pipeline_mcp_server.py"),
    ]
    _assert_no_remove(runner.calls)


def test_no_test_in_this_module_ever_ran_mcp_remove() -> None:
    _assert_no_remove(ALL_RECORDED_ARGV)
