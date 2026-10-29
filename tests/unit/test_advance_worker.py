"""SQ-04: the long-lived advance worker process (``pipeline.advance_worker``).

``pipeline.advance_one`` pays a 0.9-1.4 s import of ``pipeline.server`` for
every single plan it advances. Advancing plans one at a time in one persistent
process is safe even though ``REPO_ROOT`` is a module global, because
``_scoped_repo_root`` swaps and restores it per advance, exactly as the serial
in-process loop does; only concurrent advances in one interpreter are unsafe.

The worker therefore reads one JSON request per line (``{"plan": <name>}``)
from an inherited pipe fd, advances that plan under the scheduler identity and
a fresh correlation id, and writes exactly one JSON result line to a second
inherited fd. Protocol traffic never touches stdout/stderr: those stay the
child's diagnostics channel, and both protocol fds are made non-inheritable so
agents the advance launches cannot hold the protocol open past this process.

It recycles (returns 0) after ``PIPELINE_ADVANCE_WORKER_MAX_TASKS`` requests or
when the operator env file changes, so a config edit takes effect without a
daemon restart.

These tests drive ``main`` in-process over ``os.pipe()`` pairs with
``advance_pipeline`` patched -- no subprocess and no real plan.
"""

# ruff: noqa: I001  -- pipeline.server must be imported before any other
# pipeline module (pre-existing import cycle; see test_triage_cap_silent_skip).
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import pipeline.server
import pipeline.actor
from pipeline.actor import SYSTEM_SCHEDULER


def _worker():
    """Import the module under test lazily (keeps collection cheap)."""
    import pipeline.advance_worker

    return pipeline.advance_worker


def _drive(
    requests: list[str],
    monkeypatch: pytest.MonkeyPatch,
    *,
    max_tasks: int | str | None = None,
    env_file: Path | None = None,
) -> tuple[int, list[str]]:
    """Run ``worker.main`` over pipe pairs and return ``(rc, result_lines)``.

    ``requests`` are written as whole lines before ``main`` runs (tiny payloads,
    well under the pipe buffer), so the worker sees a finite stream.
    """
    if max_tasks is None:
        monkeypatch.delenv("PIPELINE_ADVANCE_WORKER_MAX_TASKS", raising=False)
    else:
        monkeypatch.setenv("PIPELINE_ADVANCE_WORKER_MAX_TASKS", str(max_tasks))
    if env_file is not None:
        monkeypatch.setattr(_worker(), "_env_file_path", lambda: env_file)

    in_r, in_w = os.pipe()
    out_r, out_w = os.pipe()
    try:
        payload = "".join(line + "\n" for line in requests).encode()
        with os.fdopen(in_w, "wb") as writer:
            writer.write(payload)
        rc = _worker().main([str(in_r), str(out_w)])
        # The worker leaves the inherited fds to its parent: close our own
        # write end so the read below reaches EOF.
        os.close(out_w)
        with os.fdopen(out_r, "rb") as reader:
            output = reader.read().decode()
    finally:
        for fd in (in_r, out_w):
            try:
                os.close(fd)
            except OSError:
                pass
    return rc, [line for line in output.splitlines() if line]


def test_two_requests_produce_two_result_lines_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_advance(plan: str) -> dict:
        calls.append(plan)
        return {"ok": True, "plan": plan, "n": len(calls)}

    monkeypatch.setattr(pipeline.server, "advance_pipeline", fake_advance)

    rc, lines = _drive(['{"plan": "alpha"}', '{"plan": "beta"}'], monkeypatch)

    assert rc == 0
    assert calls == ["alpha", "beta"]
    assert [json.loads(line) for line in lines] == [
        {"ok": True, "plan": "alpha", "n": 1},
        {"ok": True, "plan": "beta", "n": 2},
    ]


def test_results_are_serialised_with_default_str(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pipeline.server,
        "advance_pipeline",
        lambda plan: {"ok": True, "path": Path("/tmp") / "x"},
    )

    rc, lines = _drive(['{"plan": "alpha"}'], monkeypatch)

    assert rc == 0
    assert json.loads(lines[0]) == {"ok": True, "path": "/tmp/x"}


def test_advance_runs_under_scheduler_identity_and_fresh_correlation_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[object, str]] = []

    def fake_advance(plan: str) -> dict:
        seen.append((pipeline.actor.current_principal(), pipeline.actor.current_correlation_id()))
        return {"ok": True}

    monkeypatch.setattr(pipeline.server, "advance_pipeline", fake_advance)

    rc, _ = _drive(['{"plan": "alpha"}', '{"plan": "beta"}'], monkeypatch)

    assert rc == 0
    assert [principal for principal, _ in seen] == [SYSTEM_SCHEDULER, SYSTEM_SCHEDULER]
    first, second = (correlation for _, correlation in seen)
    assert first and second
    assert first != second


@pytest.mark.parametrize(
    "request_line",
    [
        "not json at all",
        '["alpha"]',
        '"alpha"',
        '{"plan": "../x"}',
        '{"plan": ""}',
        '{"stories": ["alpha"]}',
    ],
)
def test_invalid_requests_are_rejected_and_the_loop_continues(
    request_line: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake_advance(plan: str) -> dict:
        calls.append(plan)
        return {"ok": True, "plan": plan}

    monkeypatch.setattr(pipeline.server, "advance_pipeline", fake_advance)

    rc, lines = _drive([request_line, '{"plan": "alpha"}'], monkeypatch)

    assert rc == 0
    assert calls == ["alpha"]
    assert [json.loads(line) for line in lines] == [
        {"ok": False, "error": "invalid request"},
        {"ok": True, "plan": "alpha"},
    ]


def test_raising_advance_reports_class_name_only_and_the_loop_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_advance(plan: str) -> dict:
        calls.append(plan)
        if len(calls) == 1:
            raise RuntimeError("secret detail from /tmp/leaky/path")
        return {"ok": True, "plan": plan}

    monkeypatch.setattr(pipeline.server, "advance_pipeline", fake_advance)

    rc, lines = _drive(['{"plan": "alpha"}', '{"plan": "beta"}'], monkeypatch)

    assert rc == 0
    assert calls == ["alpha", "beta"]
    assert json.loads(lines[0]) == {"ok": False, "error": "advance raised RuntimeError"}
    assert "secret detail" not in lines[0]
    assert "/tmp/leaky/path" not in lines[0]
    assert json.loads(lines[1]) == {"ok": True, "plan": "beta"}


def test_recycles_after_max_tasks_and_does_not_read_the_next_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        pipeline.server,
        "advance_pipeline",
        lambda plan: (calls.append(plan), {"ok": True, "plan": plan})[1],
    )

    rc, lines = _drive(
        ['{"plan": "alpha"}', '{"plan": "beta"}', '{"plan": "gamma"}'],
        monkeypatch,
        max_tasks=2,
    )

    assert rc == 0
    assert calls == ["alpha", "beta"]
    assert len(lines) == 2


@pytest.mark.parametrize("bad_value", ["nope", "0", "-3", ""])
def test_bad_max_tasks_falls_back_to_the_default(
    bad_value: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        pipeline.server,
        "advance_pipeline",
        lambda plan: (calls.append(plan), {"ok": True})[1],
    )

    with caplog.at_level("WARNING", logger="pipeline.advance_worker"):
        rc, lines = _drive(['{"plan": "alpha"}', '{"plan": "beta"}'], monkeypatch, max_tasks=bad_value)

    assert rc == 0
    assert calls == ["alpha", "beta"]
    assert len(lines) == 2
    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert warnings, "an unparseable cap must be reported"
    assert warnings[0].variable == "PIPELINE_ADVANCE_WORKER_MAX_TASKS"
    assert warnings[0].default_max_tasks == 200


def test_env_file_change_recycles_after_the_current_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".pipeline.env"
    env_file.write_text("PIPELINE_AUTONOMY=gated\n", encoding="utf-8")
    os.utime(env_file, ns=(1_000_000_000, 1_000_000_000))

    calls: list[str] = []

    def fake_advance(plan: str) -> dict:
        calls.append(plan)
        # An operator edits the env file while this advance runs.
        os.utime(env_file, ns=(2_000_000_000, 2_000_000_000))
        return {"ok": True, "plan": plan}

    monkeypatch.setattr(pipeline.server, "advance_pipeline", fake_advance)

    rc, lines = _drive(
        ['{"plan": "alpha"}', '{"plan": "beta"}'],
        monkeypatch,
        env_file=env_file,
    )

    assert rc == 0
    assert calls == ["alpha"]
    assert [json.loads(line) for line in lines] == [{"ok": True, "plan": "alpha"}]


def test_missing_env_file_does_not_force_a_recycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        pipeline.server,
        "advance_pipeline",
        lambda plan: (calls.append(plan), {"ok": True})[1],
    )

    rc, lines = _drive(
        ['{"plan": "alpha"}', '{"plan": "beta"}'],
        monkeypatch,
        env_file=tmp_path / "absent.env",
    )

    assert rc == 0
    assert calls == ["alpha", "beta"]
    assert len(lines) == 2


@pytest.mark.parametrize("argv", [[], ["a", "b"], ["-1", "3"], ["3"], ["1", "2", "3"], ["1.5", "3"]])
def test_bad_argv_returns_2_without_writing(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    called: list[str] = []
    monkeypatch.setattr(
        pipeline.server, "advance_pipeline", lambda plan: called.append(plan) or {"ok": True}
    )

    assert _worker().main(argv) == 2
    assert called == []
    assert capsys.readouterr().out == ""


def test_protocol_fds_are_made_non_inheritable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pipeline.server, "advance_pipeline", lambda plan: {"ok": True})
    observed: list[tuple[int, bool]] = []
    real = os.set_inheritable

    def spy(fd: int, inheritable: bool) -> None:
        observed.append((fd, inheritable))
        real(fd, inheritable)

    monkeypatch.setattr(os, "set_inheritable", spy)

    in_r, in_w = os.pipe()
    out_r, out_w = os.pipe()
    os.close(in_w)
    try:
        rc = _worker().main([str(in_r), str(out_w)])
        os.close(out_w)
        with os.fdopen(out_r, "rb") as reader:
            output = reader.read().decode()
    finally:
        for fd in (in_r, in_w, out_r, out_w):
            try:
                os.close(fd)
            except OSError:
                pass

    assert rc == 0
    assert output == ""
    assert (in_r, False) in observed
    assert (out_w, False) in observed


def test_advance_one_main_still_writes_its_result_and_configures_the_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pipeline.advance_one

    configured: list[int] = []
    monkeypatch.setattr(pipeline.advance_one, "configure_child", lambda: configured.append(1))
    monkeypatch.setattr(pipeline.server, "advance_pipeline", lambda plan: {"ok": True, "plan": plan})

    result_path = tmp_path / "result.json"
    assert pipeline.advance_one.main(["alpha", str(result_path)]) == 0

    assert configured == [1]
    assert json.loads(result_path.read_text(encoding="utf-8")) == {"ok": True, "plan": "alpha"}
