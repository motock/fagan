"""Acceptance fixture for pipeline/token_report.py (E2E validation story).

Exercises summarize_token_costs against review_token_costs.jsonl-shaped
data: a missing file, an empty file, a corrupt line mixed with valid
ones, None-valued costs (the Ollama driver never populates
total_cost_usd), and the since= filter excluding both older and
unparseable-timestamp records. This is a standalone-utility story with
no call-site wiring, so the fixture drives the public function directly
rather than a production entrypoint.
"""
import json
from datetime import datetime, timedelta, timezone

from pipeline.token_report import summarize_token_costs


def _write_lines(path, records):
    with open(path, "w") as f:
        f.writelines(json.dumps(r) + "\n" for r in records)


def test_missing_file_returns_zeroed_summary(tmp_path):
    missing = tmp_path / "does_not_exist.jsonl"
    result = summarize_token_costs(missing)
    assert result["total_records"] == 0
    assert result["by_backend"] == {}
    assert result["by_role"] == {}


def test_empty_file_returns_zeroed_summary(tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    result = summarize_token_costs(empty)
    assert result["total_records"] == 0
    assert result["by_backend"] == {}
    assert result["by_role"] == {}


def test_skips_malformed_json_lines(tmp_path):
    path = tmp_path / "mixed.jsonl"
    with open(path, "w") as f:
        f.write(json.dumps({
            "ts": "2026-07-29T05:00:00+00:00", "backend": "claude",
            "role": "complete", "input_tokens": 10, "output_tokens": 20,
            "total_cost_usd": 0.5,
        }) + "\n")
        f.write("{not valid json,,,\n")
        f.write(json.dumps({
            "ts": "2026-07-29T05:01:00+00:00", "backend": "claude",
            "role": "complete", "input_tokens": 5, "output_tokens": 5,
            "total_cost_usd": 0.1,
        }) + "\n")
    result = summarize_token_costs(path)
    assert result["total_records"] == 2
    assert result["by_backend"]["claude"]["input_tokens"] == 15
    assert result["by_backend"]["claude"]["output_tokens"] == 25


def test_aggregates_none_costs_without_raising(tmp_path):
    path = tmp_path / "none_costs.jsonl"
    _write_lines(path, [
        {"ts": "2026-07-29T05:00:00+00:00", "backend": "claude",
         "role": "complete", "input_tokens": 10, "output_tokens": 20,
         "total_cost_usd": 0.5},
        {"ts": "2026-07-29T05:00:01+00:00", "backend": "ollama",
         "role": "review", "input_tokens": 0, "output_tokens": 0,
         "total_cost_usd": None},
    ])
    result = summarize_token_costs(path)
    assert result["total_records"] == 2
    assert result["by_backend"]["ollama"]["total_cost_usd"] == 0.0
    assert result["by_backend"]["claude"]["total_cost_usd"] == 0.5
    assert result["by_role"]["review"]["count"] == 1
    assert result["by_role"]["complete"]["count"] == 1


def test_since_filter_excludes_older_and_unparseable_timestamps(tmp_path):
    path = tmp_path / "since.jsonl"
    now = datetime(2026, 7, 29, 6, 0, 0, tzinfo=timezone.utc)
    older = now - timedelta(hours=2)
    _write_lines(path, [
        {"ts": older.isoformat(), "backend": "claude", "role": "complete",
         "input_tokens": 100, "output_tokens": 100, "total_cost_usd": 1.0},
        {"ts": now.isoformat(), "backend": "claude", "role": "complete",
         "input_tokens": 1, "output_tokens": 1, "total_cost_usd": 0.01},
        {"backend": "claude", "role": "complete",
         "input_tokens": 999, "output_tokens": 999, "total_cost_usd": 9.0},
    ])
    result = summarize_token_costs(path, since=now)
    assert result["total_records"] == 1
    assert result["by_backend"]["claude"]["input_tokens"] == 1


def test_missing_role_and_backend_bucket_as_unknown(tmp_path):
    path = tmp_path / "missing_keys.jsonl"
    _write_lines(path, [
        {"ts": "2026-07-29T05:00:00+00:00", "input_tokens": 3,
         "output_tokens": 4, "total_cost_usd": 0.2},
    ])
    result = summarize_token_costs(path)
    assert result["total_records"] == 1
    assert result["by_backend"]["unknown"]["input_tokens"] == 3
    assert result["by_role"]["unknown"]["output_tokens"] == 4


def test_single_record_boundary_aggregates_once(tmp_path):
    path = tmp_path / "single.jsonl"
    _write_lines(path, [
        {"ts": "2026-07-29T05:00:00+00:00", "backend": "claude",
         "role": "complete", "input_tokens": 7, "output_tokens": 9,
         "total_cost_usd": 0.3},
    ])
    result = summarize_token_costs(path)
    assert result["total_records"] == 1
    assert result["by_backend"]["claude"]["input_tokens"] == 7
    assert result["by_backend"]["claude"]["output_tokens"] == 9
    assert result["by_backend"]["claude"]["total_cost_usd"] == 0.3
    assert result["by_role"]["complete"]["count"] == 1


def test_zero_token_record_is_counted_not_skipped(tmp_path):
    path = tmp_path / "zero_tokens.jsonl"
    _write_lines(path, [
        {"ts": "2026-07-29T05:00:00+00:00", "backend": "ollama",
         "role": "review", "input_tokens": 0, "output_tokens": 0,
         "total_cost_usd": 0.0},
    ])
    result = summarize_token_costs(path)
    assert result["total_records"] == 1
    assert result["by_backend"]["ollama"]["input_tokens"] == 0
    assert result["by_backend"]["ollama"]["output_tokens"] == 0
    assert result["by_backend"]["ollama"]["total_cost_usd"] == 0.0
    assert result["by_role"]["review"]["count"] == 1


def test_since_filter_is_inclusive_at_boundary(tmp_path):
    path = tmp_path / "since_boundary.jsonl"
    boundary = datetime(2026, 7, 29, 6, 0, 0, tzinfo=timezone.utc)
    _write_lines(path, [
        {"ts": boundary.isoformat(), "backend": "claude", "role": "complete",
         "input_tokens": 1, "output_tokens": 1, "total_cost_usd": 0.01},
    ])
    result = summarize_token_costs(path, since=boundary)
    assert result["total_records"] == 1
    assert result["by_backend"]["claude"]["input_tokens"] == 1


def test_missing_token_fields_default_to_zero(tmp_path):
    path = tmp_path / "missing_token_fields.jsonl"
    _write_lines(path, [
        {"ts": "2026-07-29T05:00:00+00:00", "backend": "claude",
         "role": "complete", "total_cost_usd": 0.4},
    ])
    result = summarize_token_costs(path)
    assert result["total_records"] == 1
    assert result["by_backend"]["claude"]["input_tokens"] == 0
    assert result["by_backend"]["claude"]["output_tokens"] == 0
    assert result["by_backend"]["claude"]["total_cost_usd"] == 0.4


def test_multiple_backends_and_roles_aggregate_separately(tmp_path):
    path = tmp_path / "multi.jsonl"
    _write_lines(path, [
        {"ts": "2026-07-29T05:00:00+00:00", "backend": "claude",
         "role": "complete", "input_tokens": 10, "output_tokens": 20,
         "total_cost_usd": 0.5},
        {"ts": "2026-07-29T05:01:00+00:00", "backend": "ollama",
         "role": "review", "input_tokens": 30, "output_tokens": 40,
         "total_cost_usd": None},
        {"ts": "2026-07-29T05:02:00+00:00", "backend": "claude",
         "role": "review", "input_tokens": 5, "output_tokens": 5,
         "total_cost_usd": 0.1},
    ])
    result = summarize_token_costs(path)
    assert result["total_records"] == 3
    assert result["by_backend"]["claude"]["input_tokens"] == 15
    assert result["by_backend"]["claude"]["output_tokens"] == 25
    assert result["by_backend"]["ollama"]["input_tokens"] == 30
    assert result["by_role"]["complete"]["count"] == 1
    assert result["by_role"]["review"]["count"] == 2

