"""A live ``claude`` review must emit a scoped usage record, still failing closed.

``ClaudeCliDriver.complete`` only asks the CLI for ``--output-format json`` when
the caller passes a benchmark ``cell_dir``, so a story-scoped *live* call (a
review) got plain text back and no accounting line was ever written even though
``pipeline.usage_record`` had a scope bound. The fix makes the driver's
recording decision ``cell_dir is not None or usage_record.is_recording()`` and
routes the parsed envelope through ``usage_record.record_claude_result``.

The fail-closed guard must survive the change: a live call with a scope bound
that hits a CLI transport error (non-zero returncode, or an ``API Error:``
body) raises instead of handing the CLI's error text back as the review, and
must not append a usage record for the failed call.

No real ``claude`` process is spawned: ``subprocess.run`` is faked with the
same shape as ``tests/unit/test_backend_claude_strict_mcp_config.py``, and
``PLAN_DIR`` is the only seam used to redirect the usage sidecar into
``tmp_path`` (the pattern ``tests/unit/test_usage_record.py`` uses).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import app.backend_claude as bc
import pipeline.persistence

# pipeline.server is imported explicitly; import order is not required for
# cold-importability (every pipeline module imports cold, see
# tests/unit/test_hub_satellite_cold_imports.py).
import pipeline.server
from app import backend as b
from pipeline import usage_record
from tests.unit._backend_helpers import _FakeCompletedProcess

MODEL = "claude-sonnet-4-5-20260101"
PLAN = "demo-plan"
STORY = "S-1"


def _fake_run(captured: dict, *, stdout: str, returncode: int = 0):
    """Install the repo's standard ``subprocess.run`` fake and record the argv."""

    def _run(cmd, cwd, capture_output, text, env=None):
        captured["cmd"] = list(cmd)
        return _FakeCompletedProcess(stdout=stdout, returncode=returncode)

    return _run


def _usage_records(tmp_path: Path) -> list[dict]:
    """Parsed lines of the plan's usage sidecar (empty when it was never written)."""
    path = tmp_path / f"{PLAN}.usage.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _review_records(tmp_path: Path) -> list[dict]:
    return [record for record in _usage_records(tmp_path) if record["role"] == "review"]


@pytest.fixture
def plan_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(pipeline.persistence, "PLAN_DIR", tmp_path)
    return tmp_path


def test_bound_scope_live_review_requests_json_and_records_usage(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bound scope alone (no cell_dir) is enough to record a live review."""
    payload = {"result": "looks good", "usage": {"input_tokens": 120, "output_tokens": 30}}
    captured: dict = {}
    monkeypatch.setattr(
        b.subprocess, "run", _fake_run(captured, stdout=json.dumps(payload))
    )

    with usage_record.usage_scope(PLAN, STORY):
        result = bc.ClaudeCliDriver().complete("review this", model=MODEL, role="review")

    assert result == "looks good"
    assert "--output-format" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--output-format") + 1] == "json"
    matched = _review_records(plan_dir)
    assert matched, "a scope-bound live review must append a review usage record"
    assert matched[0]["backend"] == "claude"
    assert matched[0]["input_tokens"] == 120
    assert matched[0]["output_tokens"] == 30


def test_without_a_scope_live_review_stays_plain_text(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No scope bound: unchanged behavior - text output, raw stdout, no record."""
    captured: dict = {}
    monkeypatch.setattr(
        b.subprocess, "run", _fake_run(captured, stdout="plain text answer")
    )

    result = bc.ClaudeCliDriver().complete("review this", model=MODEL, role="review")

    assert result == "plain text answer"
    assert "--output-format" not in captured["cmd"]
    assert list(plan_dir.glob("*.usage.jsonl")) == []


def test_bound_scope_nonzero_returncode_fails_closed_and_records_nothing(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transport error must raise even on the recording path, leaving no record."""
    captured: dict = {}
    monkeypatch.setattr(
        b.subprocess, "run",
        _fake_run(captured, stdout="boom: CLI crashed\n", returncode=1),
    )

    with usage_record.usage_scope(PLAN, STORY), pytest.raises(RuntimeError):
        bc.ClaudeCliDriver().complete("review this", model=MODEL, role="review")

    assert list(plan_dir.glob("*.usage.jsonl")) == []


def test_bound_scope_api_error_body_fails_closed(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ``API Error:`` body on the recording path must raise, not be reviewed."""
    captured: dict = {}
    monkeypatch.setattr(
        b.subprocess, "run",
        _fake_run(captured, stdout="API Error: rate limited\n", returncode=0),
    )

    with usage_record.usage_scope(PLAN, STORY), pytest.raises(RuntimeError):
        bc.ClaudeCliDriver().complete("review this", model=MODEL, role="review")

    assert list(plan_dir.glob("*.usage.jsonl")) == []


def test_payload_without_usage_key_records_zero_tokens(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``usage`` object: the record still lands, with zeroed token counts."""
    payload = {"result": "no usage here"}
    captured: dict = {}
    monkeypatch.setattr(
        b.subprocess, "run", _fake_run(captured, stdout=json.dumps(payload))
    )

    with usage_record.usage_scope(PLAN, STORY):
        result = bc.ClaudeCliDriver().complete("review this", model=MODEL, role="review")

    assert result == "no usage here"
    matched = _review_records(plan_dir)
    assert matched, "a usage-less payload must still produce a review record"
    assert matched[0]["input_tokens"] == 0
    assert matched[0]["output_tokens"] == 0
