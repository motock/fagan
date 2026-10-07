"""Tests for tests/benchmark/run_published_matrix.sh, the published-run driver.

The script is the one-command entry point for the published benchmark run: a
fixed task x arm grid, two phases (main arms into DIR/main, then the step arm
into DIR/steps), a run_meta.json provenance record written before anything
runs, and --dry-run. Every test drives it the way an operator does - `bash
<script>` from an unrelated cwd - because the whole point of the script is that
it resolves matrix.py, git and the registry against its own location rather
than the caller's.
"""
import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from tests.benchmark import models

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "tests" / "benchmark" / "run_published_matrix.sh"
TASKS_DIR = REPO / "tests" / "benchmark" / "tasks"
README = REPO / "tests" / "benchmark" / "README.md"

MAIN_ARMS = ("sonnet", "glm_claude_review", "gptoss_claude_review_s60")
STEP_ARM = "gptoss_claude_review_s120"
TASKS = (
    "cron_field", "interval_merge", "lru_cache", "retry_backoff",
    "token_bucket", "ratelimiter_bugfix", "inventory_pagination",
)

_FAKE_PY = """#!/usr/bin/env bash
d="$(cd "$(dirname "$0")" && pwd)"
{
  printf 'CWD %s\\n' "$PWD"
  for a in "$@"; do printf 'ARG %s\\n' "$a"; done
} >> "$d/py_calls.txt"
exit {code}
"""


def _run(args, cwd, env=None):
    """Invoke the script via bash from an unrelated cwd, like an operator."""
    full = dict(os.environ)
    full.pop("PY", None)
    full.pop("PIPELINE_MODEL_REGISTRY_PATH", None)
    full.update(env or {})
    return subprocess.run(
        ["bash", str(SCRIPT), *args], cwd=str(cwd), env=full,
        capture_output=True, text=True, check=False,
    )


def _git(*args):
    return subprocess.run(
        ["git", *args], cwd=str(REPO), check=True, capture_output=True, text=True,
    ).stdout.strip()


def _fake_py(tmp_path, exit_code=0):
    """A stand-in interpreter that records each invocation (cwd + argv)."""
    py = tmp_path / "fake_py.sh"
    py.write_text(_FAKE_PY.replace("{code}", str(exit_code)))
    py.chmod(0o755)
    return py


def _calls(tmp_path):
    record = tmp_path / "py_calls.txt"
    if not record.exists():
        return []
    calls, cur = [], None
    for line in record.read_text().splitlines():
        if line.startswith("CWD "):
            cur = {"cwd": line[4:], "args": []}
            calls.append(cur)
        elif line.startswith("ARG ") and cur is not None:
            cur["args"].append(line[4:])
    return calls


def _dry_lines(proc):
    return [
        ln for ln in (*proc.stdout.splitlines(), *proc.stderr.splitlines())
        if ln.startswith("DRY-RUN: ")
    ]


def _argv(line):
    return line[len("DRY-RUN: "):].split()


def _flag_args(argv, flag):
    for i, tok in enumerate(argv):
        if tok == flag:
            out = []
            for t in argv[i + 1:]:
                if t.startswith("--"):
                    break
                out.append(t)
            return out
    return []


def _meta(workdir):
    return json.loads((Path(workdir) / "run_meta.json").read_text())


def test_script_exists_and_is_executable():
    assert SCRIPT.is_file(), "tests/benchmark/run_published_matrix.sh must exist"
    assert os.access(SCRIPT, os.X_OK), "the script must be committed with mode 100755"


def test_dry_run_prints_the_two_phase_grid(tmp_path):
    py = _fake_py(tmp_path)
    proc = _run(["--dry-run", "--workdir", str(tmp_path / "wd")], cwd=tmp_path,
                env={"PY": str(py)})
    assert proc.returncode == 0, proc.stderr
    lines = _dry_lines(proc)
    assert len(lines) == 2, f"expected exactly two DRY-RUN lines, got: {lines}"

    first, second = lines
    assert first.startswith(f"DRY-RUN: {py} "), (
        "the first DRY-RUN line must name the interpreter PY resolves to"
    )
    argv = _argv(first)
    assert "matrix.py" in argv
    assert set(MAIN_ARMS) <= set(_flag_args(argv, "--models"))
    assert set(TASKS) <= set(_flag_args(argv, "--tasks"))
    assert _flag_args(argv, "--trials") == ["3"]
    assert "--resume" in argv

    assert STEP_ARM in _flag_args(_argv(second), "--models")


def test_run_meta_records_repo_state_and_grid(tmp_path):
    wd = tmp_path / "wd"
    proc = _run(["--dry-run", "--workdir", str(wd)], cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    meta = _meta(wd)

    assert meta["repo_sha"] == _git("rev-parse", "HEAD")
    assert meta["repo_dirty"] is bool(_git("status", "--porcelain"))
    assert meta["trials"] == 3
    assert len(meta["tasks"]) == 7
    assert set(TASKS) <= set(meta["tasks"])
    assert "inventory_pagination" in meta["tasks"]
    assert set(MAIN_ARMS) <= set(meta["main_arms"])
    assert STEP_ARM in meta["step_arm"]

    started = datetime.fromisoformat(meta["started_utc"].replace("Z", "+00:00"))
    assert started.utcoffset().total_seconds() == 0, "started_utc must be UTC"
    assert abs((started - datetime.now(timezone.utc)).total_seconds()) < 3600


def test_registry_provenance_follows_pipeline_env(tmp_path):
    reg = tmp_path / "reg.json"
    reg.write_text('{"providers": {"ollama": {"models": {}}}}\n')
    wd = tmp_path / "wd"
    proc = _run(["--dry-run", "--workdir", str(wd)], cwd=tmp_path,
                env={"PIPELINE_MODEL_REGISTRY_PATH": str(reg)})
    assert proc.returncode == 0, proc.stderr
    recorded = _meta(wd)["registry"]
    assert os.path.realpath(recorded["path"]) == os.path.realpath(str(reg))
    assert recorded["sha256"] == hashlib.sha256(reg.read_bytes()).hexdigest()


def test_registry_default_and_boundary_cases(tmp_path):
    default = REPO / "model_registry.json"
    expected_sha = (
        hashlib.sha256(default.read_bytes()).hexdigest() if default.is_file() else None
    )

    # Unset -> the repo's checked-in registry.
    wd = tmp_path / "wd"
    proc = _run(["--dry-run", "--workdir", str(wd)], cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    recorded = _meta(wd)["registry"]
    assert os.path.realpath(recorded["path"]) == os.path.realpath(str(default))
    assert recorded["sha256"] == expected_sha

    # Empty string behaves as unset (boundary).
    wd2 = tmp_path / "wd2"
    proc = _run(["--dry-run", "--workdir", str(wd2)], cwd=tmp_path,
                env={"PIPELINE_MODEL_REGISTRY_PATH": ""})
    assert proc.returncode == 0, proc.stderr
    assert os.path.realpath(_meta(wd2)["registry"]["path"]) == os.path.realpath(str(default))

    # A path that does not exist: null digest, not a failure.
    wd3 = tmp_path / "wd3"
    proc = _run(["--dry-run", "--workdir", str(wd3)], cwd=tmp_path,
                env={"PIPELINE_MODEL_REGISTRY_PATH": str(tmp_path / "nope.json")})
    assert proc.returncode == 0, proc.stderr
    assert _meta(wd3)["registry"]["sha256"] is None


def test_dry_run_never_invokes_matrix_py(tmp_path):
    py = _fake_py(tmp_path)
    wd = tmp_path / "wd"
    proc = _run(["--dry-run", "--workdir", str(wd)], cwd=tmp_path, env={"PY": str(py)})
    assert proc.returncode == 0, proc.stderr
    assert not (tmp_path / "py_calls.txt").exists(), "--dry-run must run nothing"
    assert _meta(wd)["repo_sha"], "run_meta.json is still written on --dry-run"


def test_run_invokes_matrix_py_twice_from_the_script_dir(tmp_path):
    py = _fake_py(tmp_path)
    wd = tmp_path / "wd"
    proc = _run(["--workdir", str(wd)], cwd=tmp_path, env={"PY": str(py)})
    assert proc.returncode == 0, proc.stderr
    calls = _calls(tmp_path)
    assert len(calls) == 2, f"expected the two phases, got {len(calls)}"

    first, second = calls
    assert first["args"][0].endswith("matrix.py")
    invoked = Path(first["cwd"]) / first["args"][0]
    assert os.path.realpath(str(invoked)) == os.path.realpath(str(SCRIPT.parent / "matrix.py")), (
        "matrix.py must resolve against the script's own directory, not the caller's cwd"
    )
    assert os.path.realpath(first["cwd"]) == os.path.realpath(str(SCRIPT.parent))
    assert "--resume" in first["args"] and "--resume" in second["args"]
    assert _flag_args(first["args"], "--trials") == ["3"]
    assert set(MAIN_ARMS) <= set(_flag_args(first["args"], "--models"))
    assert set(TASKS) <= set(_flag_args(first["args"], "--tasks"))
    assert STEP_ARM in _flag_args(second["args"], "--models")
    assert os.path.realpath(_flag_args(first["args"], "--workdir")[0]) == os.path.realpath(str(wd / "main"))
    assert os.path.realpath(_flag_args(second["args"], "--workdir")[0]) == os.path.realpath(str(wd / "steps"))


def test_run_meta_is_written_before_anything_runs(tmp_path):
    py = _fake_py(tmp_path, exit_code=1)
    wd = tmp_path / "wd"
    proc = _run(["--workdir", str(wd)], cwd=tmp_path, env={"PY": str(py)})
    assert proc.returncode != 0, "set -euo pipefail must abort on a failed phase"
    meta = _meta(wd)
    assert meta["repo_sha"] == _git("rev-parse", "HEAD"), (
        "run_meta.json must be written before the first matrix.py invocation"
    )


def test_missing_workdir_and_unknown_flag_exit_2_with_usage(tmp_path):
    proc = _run(["--dry-run"], cwd=tmp_path)
    assert proc.returncode == 2, f"missing --workdir: {proc.returncode} {proc.stderr}"
    assert "usage" in proc.stderr.lower()
    assert "--workdir" in proc.stderr

    proc = _run(["--workdir", str(tmp_path / "wd"), "--bogus"], cwd=tmp_path)
    assert proc.returncode == 2, f"unknown flag: {proc.returncode} {proc.stderr}"
    assert "usage" in proc.stderr.lower()


def test_every_named_model_and_task_actually_exists(tmp_path):
    proc = _run(["--dry-run", "--workdir", str(tmp_path / "wd")], cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    lines = _dry_lines(proc)
    assert lines, "the dry run must print the commands it would run"
    named_models, named_tasks = set(), set()
    for line in lines:
        argv = _argv(line)
        named_models.update(_flag_args(argv, "--models"))
        named_tasks.update(_flag_args(argv, "--tasks"))
    assert named_models and named_tasks
    for arm in named_models:
        assert arm in models.MODELS, f"arm {arm!r} is missing from tests/benchmark/models.py"
    for task in named_tasks:
        assert (TASKS_DIR / task).is_dir(), f"task {task!r} has no directory under tests/benchmark/tasks"


def test_default_interpreter_is_repo_venv_with_python3_fallback(tmp_path):
    proc = _run(["--dry-run", "--workdir", str(tmp_path / "wd")], cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    lines = _dry_lines(proc)
    assert lines
    interp = _argv(lines[0])[0]
    venv = REPO / ".venv" / "bin" / "python"
    if venv.exists():
        assert os.path.realpath(interp) == os.path.realpath(str(venv))
    else:
        assert interp == "python3", "must fall back to python3 when the repo venv is absent"


def test_readme_documents_the_published_run():
    text = README.read_text()
    m = re.search(r"^## Published run\b", text, flags=re.M)
    assert m, "README needs a '## Published run' section"
    running = re.search(r"^## Running\b", text, flags=re.M)
    assert running and running.start() < m.start(), (
        "'## Published run' must sit under '## Running'"
    )
    nxt = re.search(r"^## ", text[m.end():], flags=re.M)
    section = text[m.end():m.end() + nxt.start()] if nxt else text[m.end():]
    for anchor in (
        "run_published_matrix.sh", "run_meta.json", "PIPELINE_MODEL_REGISTRY_PATH",
        "sha256", "--resume", "dispatched_model", "jq", "result.json",
    ):
        assert anchor in section, f"the Published run section must mention {anchor!r}"
    assert re.search(r"\bmain\b", section), "the section must describe the main-arms phase"
    assert re.search(r"\bsteps\b", section), "the section must describe the step-arm phase"
    assert "infra" in section.lower(), "the section must cover infra-skipped cells"