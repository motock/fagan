# ruff: noqa: I001  -- pipeline.server must be imported before any other
# pipeline module (pre-existing import cycle; see test_triage_cap_silent_skip).
"""TP-01: the pool runner that advances one plan in a child process.

``advance_plan_subprocess`` spawns ``python -m pipeline.advance_one <plan>
<result>`` and joins it with a hard timeout. The timeout is a real one: the
child's per-plan ``flock`` is released by process death (the fd is not
inheritable), which an abandoned in-process thread cannot offer.

Two invariants are pinned here beyond the happy path:

* the child always runs with ``cwd`` set to the control plane's own source
  root -- never a story worktree. The daemon is started as ``python -m
  pipeline.scheduler_daemon`` from its checkout, so ``-m`` puts the cwd first
  on ``sys.path``; a child started inside a worktree of this repository would
  import the story branch's unreviewed ``pipeline`` package and run it as the
  scheduler.
* each call owns a private temp directory, removed in every outcome, so a
  timed-out call cannot leak a stale ``result.json`` into the next one.

The child's own process group is left alone (no ``start_new_session``, no
``os.killpg``): agent processes it launched share its group and must survive.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import ModuleType

import pytest

import pipeline.server
import pipeline


def _advance_pool() -> ModuleType:
    """Import the module under test lazily so telemetry tests still collect."""
    import pipeline.advance_pool

    return pipeline.advance_pool


def _advance_dirs() -> set[str]:
    """Snapshot the pool's private temp directories (prefix ``advance-``)."""
    return {p.name for p in Path(tempfile.gettempdir()).glob("advance-*")}


@pytest.fixture(autouse=True)
def no_leaked_temp_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> object:
    """Every outcome -- success, failure, timeout -- must clean up after itself.

    ``tempfile.tempdir`` is redirected into the test's own directory so the
    check sees only this test's directories; the suite runs in parallel
    workers that share the system temp directory.
    """
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    before = _advance_dirs()
    yield
    assert _advance_dirs() - before == set()


def _patch_argv(monkeypatch: pytest.MonkeyPatch, script: str) -> None:
    """Run *script* instead of the real child module, keeping the argv shape."""
    mod = _advance_pool()
    monkeypatch.setattr(
        mod,
        "_child_argv",
        lambda plan_name, result_path: [sys.executable, "-c", script, result_path],
    )


def test_returns_parsed_result_written_by_the_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _advance_pool()
    _patch_argv(
        monkeypatch,
        'import json, sys\nwith open(sys.argv[-1], "w") as fh:\n    json.dump({"ok": True, "x": 1}, fh)\n',
    )

    assert mod.advance_plan_subprocess("demo-plan", 30.0) == {"ok": True, "x": 1}


def test_reports_exit_code_when_child_writes_no_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _advance_pool()
    _patch_argv(monkeypatch, "import sys\nsys.exit(3)\n")

    result = mod.advance_plan_subprocess("demo-plan", 30.0)

    assert result["ok"] is False
    assert "3" in result["error"]


@pytest.mark.parametrize(
    "script",
    [
        pytest.param("import sys\nsys.exit(0)\n", id="missing-file"),
        pytest.param(
            'import sys\nopen(sys.argv[-1], "w").write("not json")\n', id="invalid-json"
        ),
        pytest.param('import sys\nopen(sys.argv[-1], "w").write("[1, 2]")\n', id="not-a-dict"),
    ],
)
def test_reports_exit_code_when_child_result_is_unusable(
    script: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _advance_pool()
    _patch_argv(monkeypatch, script)

    result = mod.advance_plan_subprocess("demo-plan", 30.0)

    assert result["ok"] is False
    assert "no result" in result["error"]


def test_timeout_terminates_the_child_and_reports_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _advance_pool()
    _patch_argv(monkeypatch, "import time\ntime.sleep(60)\n")

    spawned: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def recording_popen(*args: object, **kwargs: object) -> subprocess.Popen:
        proc = real_popen(*args, **kwargs)  # type: ignore[arg-type]
        spawned.append(proc)
        return proc

    monkeypatch.setattr(subprocess, "Popen", recording_popen)

    start = time.monotonic()
    result = mod.advance_plan_subprocess("demo-plan", 0.5)
    elapsed = time.monotonic() - start

    assert result == {"ok": False, "error": "advance timed out", "timed_out": True}
    assert elapsed < 15
    # proc.wait() after the kill reaps the child: nothing is left running.
    assert len(spawned) == 1
    assert spawned[0].poll() is not None


def test_invalid_plan_name_never_spawns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _advance_pool()

    def exploding_popen(*args: object, **kwargs: object) -> subprocess.Popen:
        raise AssertionError("must not spawn")

    monkeypatch.setattr(subprocess, "Popen", exploding_popen)

    assert mod.advance_plan_subprocess("../evil", 30.0) == {
        "ok": False,
        "error": "invalid plan name",
    }


def test_child_runs_in_the_source_root_not_the_caller_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mod = _advance_pool()
    _patch_argv(
        monkeypatch,
        'import json, sys\nwith open(sys.argv[-1], "w") as fh:\n    json.dump({"ok": True}, fh)\n',
    )
    seen: dict[str, object] = {}
    real_popen = subprocess.Popen

    def recording_popen(*args: object, **kwargs: object) -> subprocess.Popen:
        seen["cwd"] = kwargs.get("cwd")
        return real_popen(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(subprocess, "Popen", recording_popen)
    monkeypatch.chdir(tmp_path)

    assert mod.advance_plan_subprocess("demo-plan", 30.0) == {"ok": True}
    assert seen["cwd"] == str(Path(pipeline.__file__).resolve().parent.parent)
    assert seen["cwd"] != str(tmp_path)
