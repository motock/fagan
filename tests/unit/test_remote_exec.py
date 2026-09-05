"""Unit tests for pipeline/remote_exec.py.

``remote_exec.main`` is the off-box supervisor entrypoint: it prepares the
remote side via ``remote_sync.ensure_remote_worktree``, runs the harness over
a single ssh argument built by ``build_remote_shell``, and brings the story
commits back with ``remote_sync.sync_back_commits``.

External boundaries are faked, internal logic is not:

* ``ssh`` is a stub shell script on a tmp-dir PATH (prepended via
  ``mock.patch.dict(os.environ, ...)``). The stub records its argv to a
  capture file, executes the command it was handed for real (so the
  ``ensure_remote_worktree`` setup commands run actual git), and - when the
  command is the harness invocation - creates a commit on the remote side by
  pushing to a ``file://`` bare remote the test prepared.
* git is exercised for real via subprocess against tmp_path repos - git is
  never mocked.

Helpers live at module level so each test body is straight-line: no loops,
no conditionals, one behavioral outcome per test.
"""

from __future__ import annotations

import inspect
import json
import os
import shlex
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from pipeline import remote_exec

BRANCH = "agent/remote-exec"
HOST = "gpu-box.invalid"
GIT_EMAIL = "remote-exec-tests@example.invalid"
GIT_NAME = "remote-exec-tests"
_GIT_IDENTITY = ("-c", f"user.email={GIT_EMAIL}", "-c", f"user.name={GIT_NAME}")
# Marker the stub ssh greps for to recognize the harness invocation (as
# opposed to the ensure_remote_worktree setup commands it must execute).
HARNESS_MARKER = "harness-output.txt"


def _git(cwd: Path, *args: str) -> str:
    """Run a real git command in ``cwd``; return stripped stdout."""
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, (
        f"git -C {cwd} {' '.join(args)} failed: {proc.stderr.strip()}"
    )
    return proc.stdout.strip()


def _make_story_worktree(root: Path) -> Path:
    """Local story worktree with exactly one commit on BRANCH."""
    worktree = root / "story-worktree"
    worktree.mkdir()
    _git(worktree, "init", "-q", "-b", BRANCH)
    (worktree / "story.md").write_text("story body\n")
    _git(worktree, "add", ".")
    _git(worktree, *_GIT_IDENTITY, "commit", "-q", "-m", "initial story commit")
    return worktree


def _remote_paths(root: Path) -> tuple[Path, Path, str]:
    """(bare repo path, remote worktree path, file:// remote_url)."""
    bare = root / "remote-mirror.git"
    remote_cwd = root / "remote-worktree"
    return bare, remote_cwd, f"file://{bare}"


def _capture_path(root: Path) -> Path:
    """Where the stub ssh records the argv of every invocation."""
    return root / "ssh-capture.txt"


def _write_stub_ssh(
    stub_dir: Path,
    capture: Path,
    bare: Path,
    remote_cwd: Path,
    *,
    local_worktree: Path | None = None,
    fail_all: bool = False,
) -> None:
    """Write a stub ``ssh`` script into ``stub_dir`` (caller prepends PATH).

    Every invocation: record argv to ``capture`` (one line per invocation,
    unit-separator between args), execute the handed command for real via
    /bin/sh, and exit with that command's status. When ``fail_all`` is set
    the stub exits nonzero before doing anything, simulating an unreachable
    host. On the harness invocation (command contains HARNESS_MARKER) the
    stub additionally commits the harness output in the remote worktree and
    pushes it to ``bare`` - the remote side contributed a commit. When
    ``local_worktree`` is also given, the stub writes to the LOCAL story
    worktree during the run, so the two tips diverge and
    ``sync_back_commits`` refuses.
    """
    stub_dir.mkdir(parents=True, exist_ok=True)
    fail_block = "exit 7\n" if fail_all else ""
    if local_worktree is not None:
        diverge_block = (
            f'git -C "{local_worktree}" {_IDENTITY} commit -q --allow-empty '
            f"-m local-write-during-run\n"
        )
    else:
        diverge_block = ""
    script = f"""#!/bin/sh
# stub ssh: record argv, execute the handed command, exit with its status
printf '%s\\037' "$@" >> "{capture}"
printf '\\n' >> "{capture}"
{fail_block}last="$#"
i=0
cmd=""
for a in "$@"; do
  i=$((i+1))
  if [ "$i" -eq "$last" ]; then
    cmd="$a"
  fi
done
/bin/sh -c "$cmd"
rc=$?
case "$cmd" in
  *{HARNESS_MARKER}*)
    git -C "{remote_cwd}" add -A
    git -C "{remote_cwd}" {_IDENTITY} commit -q --allow-empty -m remote-commit
    git -C "{remote_cwd}" push -q "{bare}" "{BRANCH}"
{diverge_block}    ;;
esac
exit $rc
"""
    stub = stub_dir / "ssh"
    stub.write_text(script)
    stub.chmod(0o755)


_IDENTITY = f"-c user.email={GIT_EMAIL} -c user.name={GIT_NAME}"


def _spec_file(root: Path, cmd: list[str], env: dict[str, str] | None) -> Path:
    """Write the dispatch spec JSON the caller (ssh driver) provides."""
    _, remote_cwd, _ = _remote_paths(root)
    spec = {
        "cmd": cmd,
        "env": env,
        "branch": BRANCH,
        "remote_cwd": str(remote_cwd),
    }
    path = root / "spec.json"
    path.write_text(json.dumps(spec))
    return path


def _argv(root: Path, spec: Path, host: str = HOST) -> list[str]:
    """Supervisor argv with all four required flags."""
    worktree = root / "story-worktree"
    _, _, remote_url = _remote_paths(root)
    return [
        "--worktree",
        str(worktree),
        "--remote-url",
        remote_url,
        "--host",
        host,
        "--spec-file",
        str(spec),
    ]


def _captured_invocations(capture: Path) -> list[list[str]]:
    """Parse the stub's capture file into one argv list per invocation."""
    lines = capture.read_text().splitlines()
    return [line[:-1].split("\037") for line in lines if line.strip()]


def _run_main(root: Path, argv: list[str], stub_dir: Path) -> int:
    """Run remote_exec.main with the stub ssh first on PATH; return its code."""
    real_path = os.environ.get("PATH", "")
    with mock.patch.dict(os.environ, {"PATH": f"{stub_dir}{os.pathsep}{real_path}"}):
        return remote_exec.main(argv)


# ---- build_remote_shell: quoting is a security control, not cosmetics ----


def test_build_remote_shell_quotes_cmd_element_with_space_and_single_quote():
    """A cmd element containing a space and a single quote survives quoting."""
    shell = remote_exec.build_remote_shell(
        ["echo", "hello world's"], {"MSG": "two words"}, "/srv/gpu/w"
    )
    assert shell == (
        "cd /srv/gpu/w && env 'MSG=two words' echo 'hello world'\"'\"'s'"
    )


def test_build_remote_shell_env_none_omits_env_prefix():
    """env=None omits the env prefix entirely."""
    shell = remote_exec.build_remote_shell(["echo", "hi"], None, "/srv/w")
    assert shell == "cd /srv/w && echo hi"


def test_build_remote_shell_empty_env_dict_omits_env_prefix():
    """env={} (boundary: empty collection) omits the env prefix entirely."""
    shell = remote_exec.build_remote_shell(["echo", "hi"], {}, "/srv/w")
    assert shell == "cd /srv/w && echo hi"


def test_build_remote_shell_multiple_env_entries_in_order():
    """Each env entry becomes one quoted K=V, in dict insertion order."""
    shell = remote_exec.build_remote_shell(
        ["python", "-c", "harness.py"], {"A": "1", "B": "2 x"}, "/srv/w"
    )
    assert shell == "cd /srv/w && env A=1 'B=2 x' python -c harness.py"


def test_build_remote_shell_single_cmd_element():
    """One cmd element (boundary: min) is appended after the cd prefix."""
    shell = remote_exec.build_remote_shell(["echo"], None, "/srv/w")
    assert shell == "cd /srv/w && echo"


def test_build_remote_shell_round_trips_through_shlex_split():
    """shlex.split of the built string yields exactly the intended words."""
    shell = remote_exec.build_remote_shell(
        ["echo", "hello world's"], {"MSG": "two words"}, "/srv/my dir"
    )
    assert shlex.split(shell) == [
        "cd",
        "/srv/my dir",
        "&&",
        "env",
        "MSG=two words",
        "echo",
        "hello world's",
    ]


def test_build_remote_shell_signatures():
    """build_remote_shell keeps the contracted parameter names/annotation."""
    sig = inspect.signature(remote_exec.build_remote_shell)
    assert list(sig.parameters) == ["cmd", "env", "remote_cwd"]
    assert str(sig.return_annotation).endswith("str")


# ---- main: end-to-end over the stub ssh boundary ----


def test_main_hands_ssh_one_shell_argument_with_cd_and_quoted_args(
    tmp_path, stub_ssh
):
    """The last argv the stub ssh captured is exactly the contracted argv."""
    root = tmp_path
    _make_story_worktree(root)
    _, remote_cwd, _ = _remote_paths(root)
    spec = _spec_file(root, ["echo", "hello world's"], {"MSG": "two words"})
    code = _run_main(root, _argv(root, spec), stub_ssh)
    assert code == 0
    invocations = _captured_invocations(_capture_path(root))
    assert invocations[-1] == [
        "-o",
        "BatchMode=yes",
        HOST,
        "cd "
        + shlex.quote(str(remote_cwd))
        + " && env 'MSG=two words' echo 'hello world'\"'\"'s'",
    ]


def test_main_propagates_harness_exit_code(tmp_path, stub_ssh):
    """A harness that exits 3 makes main return 3."""
    root = tmp_path
    _make_story_worktree(root)
    spec = _spec_file(root, ["sh", "-c", "exit 3"], None)
    code = _run_main(root, _argv(root, spec), stub_ssh)
    assert code == 3


def test_main_fast_forwards_local_worktree_when_remote_committed(
    tmp_path, stub_ssh
):
    """The harness output committed remotely exists locally after main."""
    root = tmp_path
    worktree = _make_story_worktree(root)
    spec = _spec_file(root, ["touch", HARNESS_MARKER], None)
    code = _run_main(root, _argv(root, spec), stub_ssh)
    assert code == 0
    assert (worktree / HARNESS_MARKER).exists()


def test_main_returns_1_when_sync_back_fails_but_harness_exited_0(
    tmp_path, capfd
):
    """Divergence + harness exit 0: main returns 1 and logs the refusal."""
    root = tmp_path
    _make_story_worktree(root)
    spec = _spec_file(root, ["touch", HARNESS_MARKER], None)
    divergent_stub = _divergent_stub_dir(root)
    code = _run_main(root, _argv(root, spec), divergent_stub)
    assert code == 1
    stderr = capfd.readouterr().err
    assert "refusing to sync back" in stderr


def test_sync_back_failure_does_not_mask_nonzero_harness_exit_code(
    tmp_path,
):
    """Divergence + harness exit 3: main returns 3, not 1."""
    root = tmp_path
    _make_story_worktree(root)
    spec = _spec_file(root, ["sh", "-c", f"touch {HARNESS_MARKER}; exit 3"], None)
    divergent_stub = _divergent_stub_dir(root)
    code = _run_main(root, _argv(root, spec), divergent_stub)
    assert code == 3


def test_unreachable_host_called_process_error_propagates_out_of_main(
    tmp_path,
):
    """A failing _ssh_run inside ensure_remote_worktree raises out of main."""
    root = tmp_path
    _make_story_worktree(root)
    spec = _spec_file(root, ["echo", "harness ran"], None)
    failing_stub = _failing_stub_dir(root)
    with pytest.raises(subprocess.CalledProcessError):
        _run_main(root, _argv(root, spec), failing_stub)
    invocations = _captured_invocations(_capture_path(root))
    assert "git init --bare" in invocations[0][-1]


def test_harness_output_streams_through_supervisor_stdout(tmp_path, stub_ssh, capfd):
    """No redirection: harness output lands in the supervisor's stdout."""
    root = tmp_path
    _make_story_worktree(root)
    spec = _spec_file(root, ["echo", "harness-stream-marker"], None)
    _run_main(root, _argv(root, spec), stub_ssh)
    assert "harness-stream-marker" in capfd.readouterr().out


def test_main_never_creates_a_log_file(tmp_path, stub_ssh):
    """The supervisor opens/truncates/writes no log file of its own."""
    root = tmp_path
    _make_story_worktree(root)
    spec = _spec_file(root, ["echo", "harness ran"], None)
    _run_main(root, _argv(root, spec), stub_ssh)
    logs = [p for p in root.rglob("*") if p.is_file() and p.suffix == ".log"]
    assert logs == []


def test_main_deletes_spec_file_after_read(tmp_path, stub_ssh):
    """The spec file must not survive past main() returning.

    The spec embeds the caller's harness env verbatim (including credentials
    such as ANTHROPIC_API_KEY for the Claude backend); main() is the spec's
    sole reader, so if it never unlinks the file, every ssh dispatch leaves a
    secret-bearing JSON file behind in the shared temp dir forever. Checked
    across two sequential dispatches (two distinct spec files) to prove
    cleanup isn't accidentally tied to one specific path.
    """
    root = tmp_path
    _make_story_worktree(root)
    _, remote_cwd, _ = _remote_paths(root)

    first_spec = _spec_file(root, ["echo", "first"], {"ANTHROPIC_API_KEY": "sk-ant-first"})
    code = _run_main(root, _argv(root, first_spec), stub_ssh)
    assert code == 0
    assert not first_spec.exists()

    second_spec = root / "spec-2.json"
    second_spec.write_text(
        json.dumps(
            {
                "cmd": ["echo", "second"],
                "env": {"ANTHROPIC_API_KEY": "sk-ant-second"},
                "branch": BRANCH,
                "remote_cwd": str(remote_cwd),
            }
        )
    )
    code = _run_main(root, _argv(root, second_spec), stub_ssh)
    assert code == 0
    assert not second_spec.exists()


# ---- main: malformed inputs and missing required fields ----


def test_main_missing_required_flag_raises(tmp_path, stub_ssh):
    """Omitting --host is rejected (argparse SystemExit or ValueError)."""
    root = tmp_path
    _make_story_worktree(root)
    spec = _spec_file(root, ["echo", "harness ran"], None)
    argv = _argv(root, spec)
    i = argv.index("--host")
    without_host = argv[:i] + argv[i + 2 :]
    with pytest.raises((SystemExit, ValueError)):
        _run_main(root, without_host, stub_ssh)


def test_main_missing_spec_file_raises_file_not_found(tmp_path, stub_ssh):
    """A --spec-file path that does not exist raises FileNotFoundError."""
    root = tmp_path
    _make_story_worktree(root)
    argv = _argv(root, root / "no-such-spec.json")
    with pytest.raises(FileNotFoundError):
        _run_main(root, argv, stub_ssh)


def test_main_spec_missing_required_key_raises(tmp_path, stub_ssh):
    """A spec without ``branch`` is rejected, not silently defaulted."""
    root = tmp_path
    _make_story_worktree(root)
    _, remote_cwd, _ = _remote_paths(root)
    spec_path = root / "spec.json"
    spec_path.write_text(
        json.dumps({"cmd": ["echo", "x"], "env": None, "remote_cwd": str(remote_cwd)})
    )
    with pytest.raises((KeyError, ValueError)):
        _run_main(root, _argv(root, spec_path), stub_ssh)


def test_main_malformed_spec_json_raises_value_error(tmp_path, stub_ssh):
    """Spec bytes that are not valid JSON raise ValueError."""
    root = tmp_path
    _make_story_worktree(root)
    spec_path = root / "spec.json"
    spec_path.write_text("{not json")
    with pytest.raises(ValueError):
        _run_main(root, _argv(root, spec_path), stub_ssh)


# ---- module surface: delegation, no reimplementation, entrypoint guard ----


def test_module_delegates_to_remote_sync_instead_of_reimplementing():
    """remote_exec imports the remote_sync primitives; none are redefined."""
    source = _module_source()
    assert "sync_back_commits" in source
    assert "ensure_remote_worktree" in source
    assert "from pipeline" in source or "import pipeline" in source
    for redefined in (
        "def ensure_remote_worktree",
        "def sync_back_commits",
        "def _ssh_run",
    ):
        assert redefined not in source


def test_main_wires_ensure_runner_via_functools_partial_of_ssh_run():
    """ensure_remote_worktree gets run_remote=functools.partial(_ssh_run, host)."""
    source = _module_source()
    assert "functools.partial(remote_sync._ssh_run" in source


def test_module_source_uses_shlex_quote_as_security_control():
    """Every dynamic value goes through shlex.quote (done criterion: >= 3)."""
    source = _module_source()
    assert source.count("shlex.quote") >= 3


def test_module_source_names_sync_back_commits():
    """sync_back_commits is wired into main (done criterion: grep >= 1)."""
    source = _module_source()
    assert source.count("sync_back_commits") >= 1


def test_module_source_never_names_the_local_agent_log():
    """The supervisor must not open/truncate/write agent.log itself."""
    source = _module_source()
    assert "agent.log" not in source


def test_module_has_system_exit_main_entrypoint_guard():
    """Module bottom: raise SystemExit(main(sys.argv[1:])) under __main__."""
    source = _module_source()
    assert 'if __name__ == "__main__":' in source
    assert "raise SystemExit(main(sys.argv[1:]))" in source


def test_main_signature():
    """main keeps the contracted parameter name and int return annotation."""
    sig = inspect.signature(remote_exec.main)
    assert list(sig.parameters) == ["argv"]
    assert str(sig.return_annotation).endswith("int")


# ---- fixtures and module-source helper ----


def _module_source() -> str:
    return inspect.getsource(remote_exec)


@pytest.fixture
def stub_ssh(tmp_path):
    """Stub ssh dir: records argv, executes commands, remote-commits harness."""
    root = tmp_path
    bare, remote_cwd, _ = _remote_paths(root)
    capture = root / "ssh-capture.txt"
    _write_stub_ssh(
        root / "stub-bin", capture, bare, remote_cwd, local_worktree=None
    )
    return root / "stub-bin"


def _divergent_stub_dir(root: Path) -> Path:
    """Stub variant that also writes to the local worktree during the run."""
    bare, remote_cwd, _ = _remote_paths(root)
    capture = root / "ssh-capture.txt"
    stub_dir = root / "divergent-stub-bin"
    _write_stub_ssh(
        stub_dir,
        capture,
        bare,
        remote_cwd,
        local_worktree=root / "story-worktree",
    )
    return stub_dir


def _failing_stub_dir(root: Path) -> Path:
    """Stub variant that always exits nonzero (unreachable host)."""
    bare, remote_cwd, _ = _remote_paths(root)
    capture = root / "ssh-capture.txt"
    stub_dir = root / "failing-stub-bin"
    _write_stub_ssh(
        stub_dir, capture, bare, remote_cwd, fail_all=True
    )
    return stub_dir