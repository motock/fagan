"""Tests for ``pipeline.plan_summary.format_plan_summary``.

Contract under test:

* Signature: ``format_plan_summary(plan_name: str, manifest: dict,
  records: list[dict]) -> str`` and it is the module's ONLY public function.
* Pure: no file I/O, no network, no env reads, no clock reads.
* Reuses the existing metrics helpers instead of recomputing:
  ``pipeline.story_metrics.compute_story_metrics(records)`` and
  ``pipeline.story_metrics.compute_plan_rollup(...)``.
* Output contains the plan name, the total story count, one line per story
  (story key + summary + pr_url when present) and the plan-level rollup
  numbers that ``compute_plan_rollup`` returns.
* Outbound-channel safety: no record's raw ``message`` text and no
  ``manifest["repo_root"]`` (or any other filesystem path) may appear in the
  output, because notification messages carry raw CI stderr, gate errors,
  branch names and absolute worktree paths.

Fixtures mirror the real shapes documented in ``pipeline/story_metrics.py``:
records carry ``ts``/``plan``/``message``/``story_key``/``severity``/
``event``/``dedup_key``; ``compute_story_metrics`` returns per-group payloads
with ``story_key``/``correlation_id``/``dispatch_failures``/
``rework_cycles``/``escalations``/``merged``/``merged_ts``/``cost``;
``compute_plan_rollup`` reduces those to ``stories_total``/
``stories_merged``/``total_rework_cycles``/``total_escalations``/
``total_dispatch_failures``/``total_cost``/``cost_per_merged_story``.

These tests are intentionally RED (ModuleNotFoundError on
``pipeline.plan_summary``) until the implementation lands.
"""

from __future__ import annotations

import builtins
import inspect
import io
import os
import pathlib
import socket
import subprocess
import time
from typing import Any

import pipeline.plan_summary as plan_summary_module
import pipeline.story_metrics as story_metrics_module
from pipeline.plan_summary import format_plan_summary
from pipeline.story_metrics import compute_plan_rollup, compute_story_metrics

PLAN_NAME = "widget-refresh"

#: ``(story_key, summary, pr_url)`` triples for the standard two-story fixture.
TWO_STORIES = (
    ("ALPHA-A", "wire the retry loop", "https://plane.example.com/pr/alpha"),
    ("BETA-B", "add the rollback hook", "https://plane.example.com/pr/beta"),
)


def _record(
    story_key: str,
    event: str,
    message: str = "ok",
    ts: str = "2024-01-01T00:00:00Z",
    severity: str = "info",
    **extra: Any,
) -> dict[str, Any]:
    """Build one notification record in the documented sidecar shape."""
    record = {
        "ts": ts,
        "plan": PLAN_NAME,
        "message": message,
        "story_key": story_key,
        "severity": severity,
        "event": event,
        "dedup_key": f"{story_key}:{event}",
    }
    record.update(extra)
    return record


def _manifest(*stories: tuple[str, str, str], repo_root: str = "/repo/under-test") -> dict[str, Any]:
    """Build a plan manifest from ``(story_key, summary, pr_url)`` triples."""
    return {
        "repo_root": repo_root,
        "stories": {
            key: {"summary": summary, "status": "done", "pr_url": pr_url, "risk": "low"}
            for key, summary, pr_url in stories
        },
    }


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------


def test_format_plan_summary_has_the_required_signature() -> None:
    signature = inspect.signature(format_plan_summary)
    params = list(signature.parameters.values())
    assert [param.name for param in params] == ["plan_name", "manifest", "records"]
    for param in params:
        assert param.default is inspect.Parameter.empty, param.name
        assert param.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD, param.name
    # Annotations: exact names as specified, tolerant of equivalent spellings
    # (``dict`` vs ``dict[str, Any]``, ``list`` vs ``Sequence``).
    assert "str" in str(params[0].annotation), params[0].annotation
    assert "dict" in str(params[1].annotation) or "Mapping" in str(params[1].annotation)
    assert "list" in str(params[2].annotation) or "Sequence" in str(params[2].annotation)
    assert "str" in str(signature.return_annotation), signature.return_annotation


def test_module_exposes_exactly_one_public_function() -> None:
    public_callables = {
        name: obj
        for name, obj in vars(plan_summary_module).items()
        if not name.startswith("_")
        and callable(obj)
        and getattr(obj, "__module__", None) == plan_summary_module.__name__
    }
    assert public_callables == {"format_plan_summary": format_plan_summary}, (
        f"pipeline.plan_summary must expose exactly one public function; "
        f"found {sorted(public_callables)}"
    )
    declared = getattr(plan_summary_module, "__all__", None)
    if declared is not None:
        assert "format_plan_summary" in declared


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_two_story_plan_renders_keys_summaries_and_pr_urls() -> None:
    manifest = _manifest(*TWO_STORIES)
    records = [
        _record("ALPHA-A", "story_merged", message="merged cleanly"),
        _record("BETA-B", "dispatch_failed", message="dispatch attempt failed"),
        _record("BETA-B", "escalated", message="model fallback"),
    ]

    out = format_plan_summary(PLAN_NAME, manifest, records)

    assert isinstance(out, str)
    lines = out.splitlines()
    for key, summary, pr_url in TWO_STORIES:
        assert key in out, f"story key {key!r} missing from summary:\n{out}"
        assert summary in out, f"summary {summary!r} missing from summary:\n{out}"
        assert pr_url in out, f"pr_url {pr_url!r} missing from summary:\n{out}"
        on_one_line = [
            line for line in lines if key in line and summary in line and pr_url in line
        ]
        assert on_one_line, (
            f"expected one line per story naming {key!r} with its summary and "
            f"pr_url; got:\n{out}"
        )


def test_plan_name_and_story_count_appear_in_output() -> None:
    # Twelve stories so the count "12" cannot be confused with any timestamp
    # digit or rollup coincidence in the fixture.
    stories = tuple(
        (f"STORY-{letter.upper()}", f"summary for {letter}", f"https://plane.example.com/pr/{letter}")
        for letter in "abcdefghijkl"
    )
    manifest = _manifest(*stories)
    records = [_record(key, "story_merged") for key, _summary, _pr in stories]

    out = format_plan_summary(PLAN_NAME, manifest, records)

    assert isinstance(out, str)
    assert PLAN_NAME in out, f"plan name missing from summary:\n{out}"
    assert "12" in out, f"story count 12 missing from summary:\n{out}"


def test_rollup_numbers_from_compute_plan_rollup_appear_in_output() -> None:
    records = [_record("ALPHA-A", "story_merged")]
    records.extend(_record("BETA-B", "dispatch_failed") for _ in range(7))
    records.extend(_record("BETA-B", "tests_failed") for _ in range(3))
    records.extend(_record("BETA-B", "escalated") for _ in range(2))
    manifest = _manifest(*TWO_STORIES)

    out = format_plan_summary(PLAN_NAME, manifest, records)

    # Guard the fixture itself against story_metrics drift.
    expected = compute_plan_rollup(list(compute_story_metrics(records).values()))
    assert expected["stories_total"] == 2
    assert expected["stories_merged"] == 1
    assert expected["total_rework_cycles"] == 3
    assert expected["total_escalations"] == 2
    assert expected["total_dispatch_failures"] == 7
    assert expected["total_cost"] == 14
    assert expected["cost_per_merged_story"] == 14.0
    for value in expected.values():
        if value is None:
            continue
        assert str(value) in out, f"rollup value {value!r} missing from summary:\n{out}"
    assert "14.0" in out, f"cost_per_merged_story missing from summary:\n{out}"


def test_rollup_numbers_are_taken_from_the_story_metrics_helpers(monkeypatch) -> None:
    calls: dict[str, Any] = {}

    def fake_compute_story_metrics(records_arg: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        calls["metrics_records"] = records_arg
        return {
            "ALPHA-A": {
                "story_key": "ALPHA-A",
                "correlation_id": None,
                "dispatch_failures": 0,
                "rework_cycles": 0,
                "escalations": 0,
                "merged": True,
                "merged_ts": "2024-01-01T00:00:00Z",
                "cost": 1,
            },
            "BETA-B": {
                "story_key": "BETA-B",
                "correlation_id": None,
                "dispatch_failures": 1,
                "rework_cycles": 1,
                "escalations": 1,
                "merged": False,
                "merged_ts": None,
                "cost": 4,
            },
        }

    sentinel_rollup = {
        "stories_total": 12,
        "stories_merged": 5,
        "total_rework_cycles": 3,
        "total_escalations": 2,
        "total_dispatch_failures": 4,
        "total_cost": 21,
        "cost_per_merged_story": 4.2,
    }

    def fake_compute_plan_rollup(stories_arg: list[dict[str, Any]]) -> dict[str, Any]:
        calls["rollup_stories"] = stories_arg
        return dict(sentinel_rollup)

    # Patch both import styles: ``from pipeline.story_metrics import ...``
    # binds into pipeline.plan_summary's namespace, while ``from pipeline
    # import story_metrics`` keeps the lookup on the source module.
    monkeypatch.setattr(story_metrics_module, "compute_story_metrics", fake_compute_story_metrics)
    monkeypatch.setattr(
        plan_summary_module, "compute_story_metrics", fake_compute_story_metrics, raising=False
    )
    monkeypatch.setattr(story_metrics_module, "compute_plan_rollup", fake_compute_plan_rollup)
    monkeypatch.setattr(
        plan_summary_module, "compute_plan_rollup", fake_compute_plan_rollup, raising=False
    )

    manifest = _manifest(*TWO_STORIES)
    records = [
        _record("ALPHA-A", "story_merged", message="merged cleanly"),
        _record("BETA-B", "dispatch_failed", message="dispatch attempt failed"),
    ]

    out = format_plan_summary(PLAN_NAME, manifest, records)

    assert "metrics_records" in calls, (
        "format_plan_summary must call pipeline.story_metrics.compute_story_metrics "
        "instead of recomputing per-story metrics"
    )
    assert list(calls["metrics_records"]) == records
    assert "rollup_stories" in calls, (
        "format_plan_summary must call pipeline.story_metrics.compute_plan_rollup "
        "instead of recomputing the plan totals"
    )
    for number in ("12", "5", "3", "2", "4", "21", "4.2"):
        assert number in out, f"rollup number {number!r} missing from summary:\n{out}"


# ---------------------------------------------------------------------------
# Boundary / negative cases
# ---------------------------------------------------------------------------


def test_empty_stories_manifest_reports_zero_stories_without_raising() -> None:
    manifest: dict[str, Any] = {"repo_root": "/repo/under-test", "stories": {}}

    out = format_plan_summary(PLAN_NAME, manifest, [])

    assert isinstance(out, str)
    assert "0" in out, f"zero-story count missing from summary:\n{out}"
    assert PLAN_NAME in out, f"plan name missing from summary:\n{out}"


def test_story_missing_pr_url_renders_without_none() -> None:
    manifest = {
        "repo_root": "/repo/under-test",
        "stories": {
            "ALPHA-A": {"summary": "wire the retry loop", "status": "done", "risk": "low"},
            "BETA-B": {
                "summary": "add the rollback hook",
                "status": "done",
                "pr_url": None,
                "risk": "low",
            },
        },
    }
    records = [_record("ALPHA-A", "story_merged"), _record("BETA-B", "story_merged")]

    out = format_plan_summary(PLAN_NAME, manifest, records)

    assert isinstance(out, str)
    assert "None" not in out, f"literal None leaked into the summary:\n{out}"
    assert "ALPHA-A" in out and "wire the retry loop" in out


def test_story_missing_summary_renders_without_raising() -> None:
    manifest = {
        "repo_root": "/repo/under-test",
        "stories": {
            "ALPHA-A": {
                "status": "done",
                "pr_url": "https://plane.example.com/pr/alpha",
                "risk": "low",
            },
        },
    }
    records = [_record("ALPHA-A", "story_merged")]

    out = format_plan_summary(PLAN_NAME, manifest, records)

    assert isinstance(out, str)
    assert "ALPHA-A" in out, f"story key missing from summary:\n{out}"
    assert "https://plane.example.com/pr/alpha" in out, f"pr_url missing from summary:\n{out}"


def test_empty_records_still_render_the_manifest_stories() -> None:
    manifest = _manifest(
        ("ALPHA-A", "wire the retry loop", "https://plane.example.com/pr/alpha")
    )

    out = format_plan_summary(PLAN_NAME, manifest, [])

    assert isinstance(out, str)
    assert PLAN_NAME in out, f"plan name missing from summary:\n{out}"
    assert "ALPHA-A" in out, f"story key missing from summary:\n{out}"
    assert "wire the retry loop" in out, f"summary missing from summary:\n{out}"
    assert "https://plane.example.com/pr/alpha" in out, f"pr_url missing from summary:\n{out}"


def test_non_ascii_summary_round_trips_unmangled() -> None:
    manifest = _manifest(
        ("ALPHA-A", "café — naïve", "https://plane.example.com/pr/unicode"),
        ("BETA-B", "日本語のサマリ", "https://plane.example.com/pr/unicode-2"),
    )
    records = [_record("ALPHA-A", "story_merged"), _record("BETA-B", "story_merged")]

    out = format_plan_summary(PLAN_NAME, manifest, records)

    assert isinstance(out, str)
    assert "café — naïve" in out, f"non-ASCII summary mangled:\n{out}"
    assert "日本語のサマリ" in out, f"non-ASCII summary mangled:\n{out}"
    out.encode("utf-8")  # must be encodable without loss or surrogates


# ---------------------------------------------------------------------------
# Security / data minimisation
# ---------------------------------------------------------------------------


def test_raw_message_and_filesystem_paths_never_reach_the_output() -> None:
    repo_root = "/Users/secret/repo-root-sentinel"
    worktree_leak = "/Users/secret/worktree/oops"
    branch_leak = "agent/SECRET-BRANCH-NAME"
    leaked_message = f"gate error: {worktree_leak} on branch {branch_leak}"
    manifest = {
        "repo_root": repo_root,
        "stories": {
            "ALPHA-A": {
                "summary": "clean summary text",
                "status": "done",
                "pr_url": "https://plane.example.com/pr/clean",
                "risk": "high",
            },
        },
    }
    records = [
        _record("ALPHA-A", "tests_failed", message=leaked_message),
        _record("ALPHA-A", "story_merged", message=f"merged from {worktree_leak}"),
    ]

    out = format_plan_summary(PLAN_NAME, manifest, records)

    assert isinstance(out, str)
    assert worktree_leak not in out, f"worktree path leaked into the summary:\n{out}"
    assert branch_leak not in out, f"branch name leaked into the summary:\n{out}"
    assert repo_root not in out, f"manifest repo_root leaked into the summary:\n{out}"
    for record in records:
        assert record["message"] not in out, (
            f"raw notification message leaked into the summary:\n{out}"
        )
    # The summary must not be vacuously empty: real content still renders.
    assert "ALPHA-A" in out
    assert "clean summary text" in out
    assert "https://plane.example.com/pr/clean" in out


# ---------------------------------------------------------------------------
# Purity
# ---------------------------------------------------------------------------


def _forbid(what: str):
    def _raise(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError(f"format_plan_summary must be pure: it called {what}")

    return _raise


class _ForbiddenEnviron(dict):
    def __getitem__(self, key: str) -> None:
        raise AssertionError(f"format_plan_summary must be pure: it read os.environ[{key!r}]")

    def get(self, key: str, default: Any = None) -> None:
        raise AssertionError(f"format_plan_summary must be pure: it read os.environ[{key!r}]")


def test_format_plan_summary_is_pure(monkeypatch) -> None:
    """Best-effort tripwire: the common I/O surfaces are rigged to raise.

    A pure ``format_plan_summary`` completes without touching any of them.
    """
    manifest = _manifest(
        ("ALPHA-A", "wire the retry loop", "https://plane.example.com/pr/alpha")
    )
    records = [_record("ALPHA-A", "story_merged")]

    monkeypatch.setattr(builtins, "open", _forbid("open()"))
    monkeypatch.setattr(io, "open", _forbid("io.open()"))
    monkeypatch.setattr(pathlib.Path, "open", _forbid("Path.open()"))
    monkeypatch.setattr(os, "stat", _forbid("os.stat()"))
    monkeypatch.setattr(os, "getcwd", _forbid("os.getcwd()"))
    monkeypatch.setattr(os, "getenv", _forbid("os.getenv()"))
    monkeypatch.setattr(os, "environ", _ForbiddenEnviron())
    for name in ("time", "monotonic", "gmtime", "localtime", "strftime"):
        monkeypatch.setattr(time, name, _forbid(f"time.{name}()"))
    monkeypatch.setattr(socket, "socket", _forbid("socket.socket()"))
    monkeypatch.setattr(socket, "create_connection", _forbid("socket.create_connection()"))
    monkeypatch.setattr(socket, "getaddrinfo", _forbid("socket.getaddrinfo()"))
    monkeypatch.setattr(subprocess, "run", _forbid("subprocess.run()"))
    monkeypatch.setattr(subprocess, "Popen", _forbid("subprocess.Popen()"))

    out = format_plan_summary(PLAN_NAME, manifest, records)

    assert isinstance(out, str)
    assert "ALPHA-A" in out