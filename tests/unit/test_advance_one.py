"""TP-01: the child advance process (``pipeline.advance_one``) and telemetry flush.

``main`` is the child-process entrypoint that runs exactly one plan advance
outside the daemon's own interpreter: ``_scoped_repo_root`` swaps the
module-global ``REPO_ROOT`` per plan and ~100 call sites read it, so two plans
cannot advance concurrently in one process. The child re-establishes the
identity (``SYSTEM_SCHEDULER``) and a correlation id the daemon would have
bound, re-applies the scheduler's model-call clamp (the child re-reads
``.pipeline.env`` on import and that file wins over the inherited
environment), announces itself to telemetry with a per-process
``service.instance.id``, and hands the advance result back through a JSON file
written atomically with mode 0600.

The error contract matters for security: the result file is read by the pool
runner and lands in daemon logs, so an exception is reported by CLASS NAME
only -- the message can carry paths.

``pipeline.telemetry.flush`` is the child's guaranteed last act: exporters are
force-flushed and shut down before the process dies, so a fast-exiting child
does not silently drop its spans.
"""

# ruff: noqa: I001  -- pipeline.server must be imported before any other
# pipeline module (pre-existing import cycle; see test_triage_cap_silent_skip).
from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

import pipeline.server
import pipeline.actor
import pipeline.telemetry
from pipeline.actor import (
    SYSTEM_SCHEDULER,
    current_correlation_id,
    current_principal,
)


def _advance_one():
    """Import the module under test lazily so telemetry tests still collect."""
    import pipeline.advance_one

    return pipeline.advance_one


# ---------------------------------------------------------------------------
# main(): success path
# ---------------------------------------------------------------------------


def test_main_writes_result_atomically_with_scheduler_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _advance_one()
    result_path = tmp_path / "result.json"
    seen: dict[str, object] = {}

    def stub_advance_pipeline(plan_name: str) -> dict[str, object]:
        seen["plan"] = plan_name
        seen["principal"] = current_principal()
        seen["correlation"] = current_correlation_id()
        return {"ok": True, "plan": plan_name}

    monkeypatch.setattr(pipeline.server, "advance_pipeline", stub_advance_pipeline)

    assert mod.main(["demo-plan", str(result_path)]) == 0

    assert seen["plan"] == "demo-plan"
    assert seen["principal"] is SYSTEM_SCHEDULER
    assert isinstance(seen["correlation"], str) and seen["correlation"]

    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload == {"ok": True, "plan": "demo-plan"}
    assert os.stat(result_path).st_mode & 0o777 == 0o600
    # The temp file used for the atomic replace is gone, not left beside it.
    assert [entry.name for entry in result_path.parent.iterdir()] == ["result.json"]


def test_main_serialises_values_json_cannot_encode_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _advance_one()
    result_path = tmp_path / "result.json"
    stamp = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)

    monkeypatch.setattr(
        pipeline.server, "advance_pipeline", lambda plan_name: {"ok": True, "when": stamp}
    )

    assert mod.main(["demo-plan", str(result_path)]) == 0

    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["when"] == str(stamp)


# ---------------------------------------------------------------------------
# main(): boundary validation -- every rejection returns 2 and writes nothing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param([], id="no-args"),
        pytest.param(["demo-plan"], id="one-arg"),
        pytest.param(["demo-plan", "extra-a", "extra-b"], id="three-args"),
    ],
)
def test_main_rejects_wrong_argument_count(
    argv: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _advance_one()
    monkeypatch.setattr(
        pipeline.server,
        "advance_pipeline",
        lambda plan_name: pytest.fail("advance must not run for invalid argv"),
    )
    monkeypatch.chdir(tmp_path)

    assert mod.main(argv) == 2
    assert list(tmp_path.iterdir()) == []


def test_main_rejects_invalid_plan_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _advance_one()
    result_path = tmp_path / "result.json"
    monkeypatch.setattr(
        pipeline.server,
        "advance_pipeline",
        lambda plan_name: pytest.fail("advance must not run for an invalid name"),
    )

    assert mod.main(["../evil", str(result_path)]) == 2
    assert not result_path.exists()
    assert list(tmp_path.iterdir()) == []


def test_main_rejects_relative_result_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _advance_one()
    monkeypatch.setattr(
        pipeline.server,
        "advance_pipeline",
        lambda plan_name: pytest.fail("advance must not run for a relative path"),
    )
    monkeypatch.chdir(tmp_path)

    assert mod.main(["demo-plan", "result.json"]) == 2
    assert not (tmp_path / "result.json").exists()
    assert list(tmp_path.iterdir()) == []


def test_main_rejects_missing_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _advance_one()
    result_path = tmp_path / "no-such-dir" / "result.json"
    monkeypatch.setattr(
        pipeline.server,
        "advance_pipeline",
        lambda plan_name: pytest.fail("advance must not run for a bad parent dir"),
    )

    assert mod.main(["demo-plan", str(result_path)]) == 2
    assert not result_path.exists()


# ---------------------------------------------------------------------------
# main(): failure path -- class name only, never the exception message
# ---------------------------------------------------------------------------


def test_main_reports_exception_class_without_leaking_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _advance_one()
    result_path = tmp_path / "result.json"
    flushed: list[str] = []

    class _Recorder:
        def __init__(self, name: str) -> None:
            self._name = name

        def force_flush(self, timeout_millis: int = 5000) -> None:
            flushed.append(f"{self._name}:force_flush")

        def shutdown(self) -> None:
            flushed.append(f"{self._name}:shutdown")

    monkeypatch.setattr(pipeline.telemetry, "_PROVIDER", _Recorder("tracer"))
    monkeypatch.setattr(pipeline.telemetry, "_METER_PROVIDER", _Recorder("meter"))

    def stub_advance_pipeline(plan_name: str) -> dict[str, object]:
        raise RuntimeError("/secret/path leaked")

    monkeypatch.setattr(pipeline.server, "advance_pipeline", stub_advance_pipeline)

    assert mod.main(["demo-plan", str(result_path)]) == 1

    text = result_path.read_text(encoding="utf-8")
    assert json.loads(text) == {"ok": False, "error": "advance raised RuntimeError"}
    assert "/secret/path" not in text
    # flush() sits in a finally block, so it runs on the failure path too.
    assert flushed == [
        "tracer:force_flush",
        "tracer:shutdown",
        "meter:force_flush",
        "meter:shutdown",
    ]


# ---------------------------------------------------------------------------
# pipeline.telemetry.flush()
# ---------------------------------------------------------------------------


class _ProviderStub:
    def __init__(self, name: str, calls: list[str], *, fail_force_flush: bool = False) -> None:
        self.name = name
        self.calls = calls
        self.fail_force_flush = fail_force_flush

    def force_flush(self, timeout_millis: int = 5000) -> None:
        self.calls.append(f"{self.name}:force_flush:{timeout_millis}")
        if self.fail_force_flush:
            raise RuntimeError("/secret/path")

    def shutdown(self) -> None:
        self.calls.append(f"{self.name}:shutdown")


def test_flush_with_nothing_configured_does_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pipeline.telemetry, "_PROVIDER", None)
    monkeypatch.setattr(pipeline.telemetry, "_METER_PROVIDER", None)

    assert pipeline.telemetry.flush() is None


def test_flush_force_flushes_then_shuts_down_each_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    tracer = _ProviderStub("tracer", calls)
    meter = _ProviderStub("meter", calls)
    monkeypatch.setattr(pipeline.telemetry, "_PROVIDER", tracer)
    monkeypatch.setattr(pipeline.telemetry, "_METER_PROVIDER", meter)

    pipeline.telemetry.flush()
    assert calls == [
        "tracer:force_flush:5000",
        "tracer:shutdown",
        "meter:force_flush:5000",
        "meter:shutdown",
    ]

    # Providers stay registered: a second flush must be just as effective.
    pipeline.telemetry.flush(1000)
    assert calls[4:] == [
        "tracer:force_flush:1000",
        "tracer:shutdown",
        "meter:force_flush:1000",
        "meter:shutdown",
    ]


def test_flush_swallows_provider_errors_and_logs_class_only(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[str] = []
    tracer = _ProviderStub("tracer", calls, fail_force_flush=True)
    meter = _ProviderStub("meter", calls)
    monkeypatch.setattr(pipeline.telemetry, "_PROVIDER", tracer)
    monkeypatch.setattr(pipeline.telemetry, "_METER_PROVIDER", meter)

    with caplog.at_level(logging.WARNING, logger="pipeline.telemetry"):
        pipeline.telemetry.flush()

    assert calls == [
        "tracer:force_flush:5000",
        "tracer:shutdown",
        "meter:force_flush:5000",
        "meter:shutdown",
    ]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "RuntimeError" in message
    assert "/secret/path" not in message
