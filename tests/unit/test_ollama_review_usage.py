"""Live (story-scoped) usage records for the Ollama driver.

Token accounting for a live review used to exist only as the benchmark
sidecar written when a ``cell_dir`` is passed, so a production review turn
produced no accounting row at all. These tests pin the story-scoped path:
with a usage scope bound, ``OllamaDriver.complete`` and every review-loop
model turn append exactly one record to the plan's usage sidecar, and with
no scope bound nothing is written.

The plan directory is the seam (``pipeline.persistence.PLAN_DIR``), the same
one ``tests/unit/test_usage_record.py`` patches.
"""

import json

import pytest

import pipeline.server  # noqa: F401  (must precede other pipeline imports: pre-existing import cycle)
from app import backend as b
from pipeline import persistence, usage_record
from tests.unit._backend_helpers import _envelope

# Captured at import time, i.e. before tests/unit/conftest.py's autouse
# _hermetic_ollama_seams_default replaces OllamaDriver.complete with a no-op
# for every module outside its allowlist. These tests measure the real
# complete()/review-loop accounting, so they restore it for this module.
_REAL_COMPLETE = b.OllamaDriver.complete


@pytest.fixture(autouse=True)
def _use_real_ollama_complete(monkeypatch):
    monkeypatch.setattr(b.OllamaDriver, "complete", _REAL_COMPLETE)

_PLAN = "demo-plan"
_STORY = "S-1"


class _FakeProvider:
    """Fake Ollama transport: replays envelopes in order, then repeats the last."""

    name = "ollama"

    def __init__(self, envelopes):
        self._envelopes = list(envelopes)
        self.calls = 0

    def chat(self, messages, *, model, **kwargs):
        self.calls += 1
        if len(self._envelopes) > 1:
            return self._envelopes.pop(0)
        return self._envelopes[0]


def _driver(monkeypatch, envelopes):
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    driver = b.OllamaDriver()
    monkeypatch.setattr(driver, "provider", _FakeProvider(envelopes))
    return driver


def _envelope_with_usage(body, prompt_eval_count=None, eval_count=None):
    envelope = _envelope(body)
    if prompt_eval_count is not None:
        envelope["prompt_eval_count"] = prompt_eval_count
    if eval_count is not None:
        envelope["eval_count"] = eval_count
    return envelope


def _records(plan_name=_PLAN):
    path = persistence._usage_jsonl_path(plan_name)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_complete_with_scope_and_no_cell_dir_records_one_usage_row(tmp_path, monkeypatch):
    """The live complete() path (cell_dir is None) must still record usage."""
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    driver = _driver(monkeypatch, [
        _envelope_with_usage({"content": "RULING: ok"}, 120, 34),
    ])

    with usage_record.usage_scope(_PLAN, _STORY):
        out = driver.complete("hello", model="sonnet", cell_dir=None)

    assert out == "RULING: ok"
    records = _records()
    assert len(records) == 1
    assert records[0]["backend"] == "ollama"
    assert records[0]["input_tokens"] == 120
    assert records[0]["output_tokens"] == 34
    assert records[0]["plan"] == _PLAN
    assert records[0]["story_key"] == _STORY


def test_review_loop_records_one_row_per_model_turn(tmp_path, monkeypatch):
    """Two model turns -> two records; the zero-token verdict annotation adds none."""
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    cell_dir = tmp_path / "cell"
    cell_dir.mkdir()
    driver = _driver(monkeypatch, [
        _envelope_with_usage(
            {"tool_calls": [{"function": {
                "name": "bash", "arguments": {"command": "echo ran-tests"}}}]},
            120, 34,
        ),
        _envelope_with_usage(
            {"tool_calls": [{"function": {
                "name": "submit_review",
                "arguments": {"verdict": "APPROVE", "summary": "clean"}}}]},
            90, 12,
        ),
    ])

    with usage_record.usage_scope(_PLAN, _STORY):
        out = driver.complete("review the branch", system="reviewer body",
                              model="sonnet", allowed_tools="Bash,Read",
                              cwd=str(tmp_path), cell_dir=str(cell_dir))

    assert "VERDICT: APPROVE" in out
    records = _records()
    assert len(records) == 2
    assert records[0]["input_tokens"] == 120
    assert records[0]["output_tokens"] == 34
    assert records[1]["input_tokens"] == 90
    assert records[1]["output_tokens"] == 12
    assert all(record["role"] == "review" for record in records)


def test_no_scope_bound_writes_no_usage_file_and_returns_unchanged(tmp_path, monkeypatch):
    """Without a bound scope the call is not attributable: nothing is written."""
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    driver = _driver(monkeypatch, [{"message": {"content": "RULING: ok"}}])

    assert usage_record.is_recording() is False
    first = driver.complete("hello", model="sonnet", cell_dir=None)
    second = driver.complete("hello", model="sonnet", cell_dir=None)

    assert first == second == "RULING: ok"
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


def test_envelope_without_prompt_eval_count_records_zero_input_tokens(tmp_path, monkeypatch):
    """A usage-less envelope records input 0, and later turns are not cumulative."""
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    driver = _driver(monkeypatch, [
        _envelope_with_usage({"content": "first"}, None, 7),
        _envelope_with_usage({"content": "second"}, 5, 3),
    ])

    with usage_record.usage_scope(_PLAN, _STORY):
        assert driver.complete("hello", model="sonnet", cell_dir=None) == "first"
        assert driver.complete("hello", model="sonnet", cell_dir=None) == "second"

    records = _records()
    assert len(records) == 2
    assert records[0]["input_tokens"] == 0
    assert records[0]["output_tokens"] == 7
    assert records[1]["input_tokens"] == 5
    assert records[1]["output_tokens"] == 3
