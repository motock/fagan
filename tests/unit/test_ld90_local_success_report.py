"""Unit tests for ``scripts/local_success_report.py`` (LD90 local-success CLI).

The report script is a thin I/O + printing layer over ``pipeline.local_success``
(``classify_story`` / ``rolling_rate``).  These tests drive it through
``main(argv)`` against synthetic plan directories built in ``tmp_path``; they
never read a real ``~/.claude/plans`` tree and never assert a live metric value.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "scripts" / "local_success_report.py"


def _load_report_module():
    """Load the report script by path (it is not an importable package module)."""
    assert SCRIPT_PATH.is_file(), f"implementation missing: {SCRIPT_PATH}"
    spec = importlib.util.spec_from_file_location("local_success_report", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["local_success_report"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def report():
    return _load_report_module()


# --------------------------------------------------------------------------- #
# synthetic plan-dir helpers
# --------------------------------------------------------------------------- #


def _story(dispatched_at: str, *, backend: str = "ollama", status: str = "done", **extra):
    story = {"backend": backend, "status": status, "dispatched_at": dispatched_at}
    story.update(extra)
    return story


def _write_manifest(plan_dir: Path, plan: str, stories: dict) -> None:
    payload = {"stories": stories}
    (plan_dir / f"{plan}.manifest.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_records(plan_dir: Path, plan: str, records: list) -> None:
    body = "".join(json.dumps(rec) + "\n" for rec in records)
    (plan_dir / f"{plan}.notifications.jsonl").write_text(body, encoding="utf-8")


def _write_raw_manifest(plan_dir: Path, plan: str, text: str) -> None:
    (plan_dir / f"{plan}.manifest.json").write_text(text, encoding="utf-8")


def _first_line(out: str) -> str:
    for line in out.splitlines():
        if line.strip():
            return line
    return ""


def _block(out: str, label: str) -> list[str]:
    """Return the label line plus its indented continuation lines."""
    lines = out.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(label):
            block = [line]
            for following in lines[index + 1 :]:
                if following.strip() and not following.startswith((" ", "\t")):
                    break
                block.append(following)
            return block
    raise AssertionError(f"no line starting with {label!r} in output:\n{out}")


def _line_index(out: str, label: str) -> int:
    for index, line in enumerate(out.splitlines()):
        if line.startswith(label):
            return index
    raise AssertionError(f"no line starting with {label!r} in output:\n{out}")


def _reason_lines(block: list[str]) -> list[str]:
    return [line.strip() for line in block[1:] if line.strip()]


# --------------------------------------------------------------------------- #
# happy path / window semantics
# --------------------------------------------------------------------------- #


def test_window_zero_reports_full_population(report, tmp_path, capsys):
    _write_manifest(
        tmp_path,
        "a",
        {
            "S1": _story("2026-09-01T00:00:00+00:00"),
            "S2": _story("2026-09-02T00:00:00+00:00"),
        },
    )
    _write_records(
        tmp_path,
        "a",
        [
            {"story_key": "S1", "event": "story_merged"},
            {"story_key": "S2", "event": "escalated"},
        ],
    )

    rc = report.main(["--plan-dir", str(tmp_path), "--window", "0"])
    captured = capsys.readouterr()

    assert isinstance(rc, int)
    assert rc == 0
    assert "overall: 1/2 clean (50.0%)" in captured.out
    assert "escalated: 1" in captured.out


def test_header_names_the_window(report, tmp_path, capsys):
    _write_manifest(tmp_path, "a", {"S1": _story("2026-09-01T00:00:00+00:00")})

    assert report.main(["--plan-dir", str(tmp_path), "--window", "0"]) == 0
    header = _first_line(capsys.readouterr().out)

    assert "window" in header.lower()
    assert "0" in header or "all" in header.lower()


def test_reason_lines_are_indented_under_each_block(report, tmp_path, capsys):
    _write_manifest(
        tmp_path,
        "a",
        {
            "S1": _story("2026-09-01T00:00:00+00:00"),
            "S2": _story("2026-09-02T00:00:00+00:00"),
        },
    )
    _write_records(
        tmp_path,
        "a",
        [
            {"story_key": "S1", "event": "story_merged"},
            {"story_key": "S2", "event": "escalated"},
        ],
    )

    assert report.main(["--plan-dir", str(tmp_path), "--window", "0"]) == 0
    out = capsys.readouterr().out

    overall = _block(out, "overall:")
    assert overall[0] == "overall: 1/2 clean (50.0%)"
    assert "escalated: 1" in _reason_lines(overall)

    # The reason is reported under the tier block too, and indented.
    on_device = _block(out, "on-device:")
    assert "escalated: 1" in _reason_lines(on_device)
    reason_line = next(line for line in on_device[1:] if line.strip() == "escalated: 1")
    assert reason_line.startswith((" ", "\t"))


def test_window_one_keeps_only_latest_story(report, tmp_path, capsys):
    _write_manifest(
        tmp_path,
        "a",
        {
            "S1": _story("2026-09-01T00:00:00+00:00"),
            "S2": _story("2026-09-02T00:00:00+00:00"),
        },
    )
    _write_records(
        tmp_path,
        "a",
        [
            {"story_key": "S1", "event": "story_merged"},
            {"story_key": "S2", "event": "escalated"},
        ],
    )

    assert report.main(["--plan-dir", str(tmp_path), "--window", "1"]) == 0
    out = capsys.readouterr().out

    assert "overall: 0/1 clean (0.0%)" in out
    assert "overall: 1/2 clean" not in out


def test_default_window_is_thirty(report, tmp_path, capsys):
    stories = {}
    records = []
    for day in range(1, 32):
        key = f"S{day:02d}"
        stories[key] = _story(f"2026-08-{day:02d}T00:00:00+00:00")
        records.append({"story_key": key, "event": "escalated" if day == 1 else "story_merged"})
    _write_manifest(tmp_path, "a", stories)
    _write_records(tmp_path, "a", records)

    assert report.main(["--plan-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out

    assert "30" in _first_line(out)
    assert "overall: 30/30 clean (100.0%)" in out
    assert "escalated: 1" not in out

    assert report.main(["--plan-dir", str(tmp_path), "--window", "0"]) == 0
    full = capsys.readouterr().out
    assert "overall: 30/31 clean (96.8%)" in full
    assert "escalated: 1" in full


def test_percentage_uses_one_decimal(report, tmp_path, capsys):
    _write_manifest(
        tmp_path,
        "a",
        {
            "S1": _story("2026-09-01T00:00:00+00:00"),
            "S2": _story("2026-09-02T00:00:00+00:00"),
            "S3": _story("2026-09-03T00:00:00+00:00"),
        },
    )
    _write_records(
        tmp_path,
        "a",
        [
            {"story_key": "S1", "event": "story_merged"},
            {"story_key": "S2", "event": "escalated"},
            {"story_key": "S3", "event": "escalated"},
        ],
    )

    assert report.main(["--plan-dir", str(tmp_path), "--window", "0"]) == 0
    assert "overall: 1/3 clean (33.3%)" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# sidecar handling
# --------------------------------------------------------------------------- #


def test_legacy_notifications_log_is_ignored(report, tmp_path, capsys):
    _write_manifest(
        tmp_path,
        "a",
        {
            "S1": _story("2026-09-01T00:00:00+00:00"),
            "S2": _story("2026-09-02T00:00:00+00:00"),
        },
    )
    _write_records(
        tmp_path,
        "a",
        [
            {"story_key": "S1", "event": "story_merged"},
            {"story_key": "S2", "event": "escalated"},
        ],
    )
    (tmp_path / "a.notifications.log").write_text(
        "S1 escalating to Claude\n", encoding="utf-8"
    )

    assert report.main(["--plan-dir", str(tmp_path), "--window", "0"]) == 0
    out = capsys.readouterr().out

    assert "overall: 1/2 clean (50.0%)" in out
    assert "legacy_message" not in out


def test_missing_notifications_jsonl_yields_no_records(report, tmp_path, capsys):
    _write_manifest(
        tmp_path,
        "a",
        {
            "S1": _story("2026-09-01T00:00:00+00:00"),
            "S2": _story("2026-09-02T00:00:00+00:00"),
        },
    )

    assert report.main(["--plan-dir", str(tmp_path), "--window", "0"]) == 0
    assert "overall: 2/2 clean (100.0%)" in capsys.readouterr().out


def test_non_dict_story_entries_are_skipped(report, tmp_path, capsys):
    _write_manifest(
        tmp_path,
        "a",
        {
            "S1": _story("2026-09-01T00:00:00+00:00"),
            "S2": "not-a-story-dict",
        },
    )

    assert report.main(["--plan-dir", str(tmp_path), "--window", "0"]) == 0
    assert "overall: 1/1 clean (100.0%)" in capsys.readouterr().out


def test_manifest_without_stories_key_is_tolerated(report, tmp_path, capsys):
    _write_raw_manifest(tmp_path, "a", json.dumps({"plan": "a"}))

    assert report.main(["--plan-dir", str(tmp_path), "--window", "0"]) == 0
    assert "overall: 0/0 clean (n/a)" in capsys.readouterr().out


def test_corrupt_manifest_is_skipped_with_warning(report, tmp_path, capsys):
    _write_manifest(
        tmp_path,
        "a",
        {
            "S1": _story("2026-09-01T00:00:00+00:00"),
            "S2": _story("2026-09-02T00:00:00+00:00"),
        },
    )
    _write_records(
        tmp_path,
        "a",
        [
            {"story_key": "S1", "event": "story_merged"},
            {"story_key": "S2", "event": "escalated"},
        ],
    )
    _write_raw_manifest(tmp_path, "b", "{ this is not json")

    rc = report.main(["--plan-dir", str(tmp_path), "--window", "0"])
    captured = capsys.readouterr()

    assert rc == 0
    assert "overall: 1/2 clean (50.0%)" in captured.out
    warnings = [line for line in captured.err.splitlines() if line.strip()]
    assert len(warnings) == 1
    assert "b" in warnings[0]


def test_manifests_are_processed_in_sorted_order(report, tmp_path, monkeypatch):
    from pipeline import story_metrics

    seen: list[str] = []
    real_loader = story_metrics.load_notification_records

    def spy(path):
        seen.append(Path(path).name)
        return real_loader(path)

    # Cover both binding styles: ``from ... import load_notification_records``
    # (patched on the script module) and ``import pipeline.story_metrics``
    # (patched on the source module).
    monkeypatch.setattr(story_metrics, "load_notification_records", spy)
    if hasattr(report, "load_notification_records"):
        monkeypatch.setattr(report, "load_notification_records", spy)

    for plan in ("b", "a"):
        _write_manifest(tmp_path, plan, {"S1": _story("2026-09-01T00:00:00+00:00")})

    assert report.main(["--plan-dir", str(tmp_path), "--window", "0"]) == 0
    assert seen == ["a.notifications.jsonl", "b.notifications.jsonl"]


# --------------------------------------------------------------------------- #
# tiers and reasons
# --------------------------------------------------------------------------- #


def test_tier_lines_are_printed_in_order_with_counts(report, tmp_path, capsys):
    _write_manifest(
        tmp_path,
        "a",
        {
            "S1": _story("2026-09-01T00:00:00+00:00"),
            "S2": _story("2026-09-02T00:00:00+00:00", dispatched_model="qwen3:cloud"),
            "S3": _story("2026-09-03T00:00:00+00:00", backend="claude"),
        },
    )

    assert report.main(["--plan-dir", str(tmp_path), "--window", "0"]) == 0
    out = capsys.readouterr().out

    assert "overall: 2/2 clean (100.0%)" in out
    assert "on-device: 1/1 clean (100.0%)" in out
    assert "cloud-oss: 1/1 clean (100.0%)" in out
    assert "unknown: 0/0 clean (n/a)" in out

    assert (
        _line_index(out, "overall:")
        < _line_index(out, "on-device:")
        < _line_index(out, "cloud-oss:")
        < _line_index(out, "unknown:")
    )


def test_reasons_sorted_by_count_descending(report, tmp_path, capsys):
    _write_manifest(
        tmp_path,
        "a",
        {
            "S1": _story("2026-09-01T00:00:00+00:00"),
            "S2": _story("2026-09-02T00:00:00+00:00"),
            "S3": _story("2026-09-03T00:00:00+00:00"),
            "S4": _story("2026-09-04T00:00:00+00:00"),
        },
    )
    _write_records(
        tmp_path,
        "a",
        [
            {"story_key": "S1", "message": "S1 parked pending review"},
            {"story_key": "S2", "message": "S2 parked pending review"},
            {"story_key": "S3", "event": "escalated"},
            {"story_key": "S4", "event": "story_merged"},
        ],
    )

    assert report.main(["--plan-dir", str(tmp_path), "--window", "0"]) == 0
    out = capsys.readouterr().out

    assert "overall: 1/4 clean (25.0%)" in out
    reasons = _reason_lines(_block(out, "overall:"))
    assert reasons == ["legacy_message: 2", "escalated: 1"]


def test_reasons_tie_broken_by_name(report, tmp_path, capsys):
    _write_manifest(
        tmp_path,
        "a",
        {
            "S1": _story("2026-09-01T00:00:00+00:00"),
            "S2": _story("2026-09-02T00:00:00+00:00"),
        },
    )
    _write_records(
        tmp_path,
        "a",
        [
            {"story_key": "S1", "event": "escalated"},
            {"story_key": "S2", "message": "S2 parked pending review"},
        ],
    )

    assert report.main(["--plan-dir", str(tmp_path), "--window", "0"]) == 0
    out = capsys.readouterr().out

    reasons = _reason_lines(_block(out, "overall:"))
    assert reasons == ["escalated: 1", "legacy_message: 1"]


# --------------------------------------------------------------------------- #
# plan-dir resolution and CLI surface
# --------------------------------------------------------------------------- #


def test_plan_dir_env_var_is_used_and_expanded(report, tmp_path, monkeypatch, capsys):
    plans = tmp_path / "plansdir"
    plans.mkdir()
    _write_manifest(
        plans,
        "a",
        {
            "S1": _story("2026-09-01T00:00:00+00:00"),
            "S2": _story("2026-09-02T00:00:00+00:00"),
        },
    )
    _write_records(
        plans,
        "a",
        [
            {"story_key": "S1", "event": "story_merged"},
            {"story_key": "S2", "event": "escalated"},
        ],
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PLAN_DIR", "~/plansdir")

    assert report.main([]) == 0
    assert "overall: 1/2 clean (50.0%)" in capsys.readouterr().out


def test_plan_dir_env_var_missing_dir_returns_two(report, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PLAN_DIR", str(tmp_path / "does-not-exist"))

    assert report.main([]) == 2
    assert capsys.readouterr().err.strip()


def test_nonexistent_plan_dir_returns_two(report, tmp_path, capsys):
    missing = tmp_path / "nope"

    rc = report.main(["--plan-dir", str(missing), "--window", "0"])
    captured = capsys.readouterr()

    assert rc == 2
    assert captured.err.strip()
    assert "plan" in captured.err.lower() or str(missing) in captured.err


def test_empty_plan_dir_reports_na(report, tmp_path, capsys):
    rc = report.main(["--plan-dir", str(tmp_path), "--window", "0"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "overall: 0/0 clean (n/a)" in out
    assert "on-device: 0/0 clean (n/a)" in out
    assert "cloud-oss: 0/0 clean (n/a)" in out
    assert "unknown: 0/0 clean (n/a)" in out


def test_help_lists_options(report, capsys):
    with pytest.raises(SystemExit) as excinfo:
        report.main(["--help"])

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "--plan-dir" in out
    assert "--window" in out


def test_invalid_window_value_exits_two(report, tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        report.main(["--plan-dir", str(tmp_path), "--window", "not-an-int"])

    assert excinfo.value.code == 2


# --------------------------------------------------------------------------- #
# source-level contract
# --------------------------------------------------------------------------- #


def test_source_contract():
    assert SCRIPT_PATH.is_file(), f"implementation missing: {SCRIPT_PATH}"
    source = SCRIPT_PATH.read_text(encoding="utf-8")

    # Import convention: repo root on sys.path, then the two allowed modules.
    assert "sys.path.insert" in source
    assert "Path(__file__).resolve().parent.parent" in source
    assert "load_notification_records" in source
    assert ".notifications.jsonl" in source
    assert "PLAN_DIR" in source
    assert "expanduser" in source

    # Entry point.
    assert 'if __name__ == "__main__":' in source
    assert "sys.exit(main())" in source

    tree = ast.parse(source)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    assert any(name.startswith("pipeline.local_success") for name in imported)
    assert any(name.startswith("pipeline.story_metrics") for name in imported)

    # Import-time side effects are forbidden: these modules create directories
    # and load configuration when imported.
    for forbidden in ("pipeline.server", "pipeline.paths", "app"):
        assert not any(
            name == forbidden or name.startswith(forbidden + ".") for name in imported
        ), f"forbidden import: {forbidden}"

    doc = ast.get_docstring(tree) or ""
    assert "pipeline/local_success.py" in doc
    assert "scripts/local_success_report.py" in doc
    assert "--window 0" in doc
    assert "baseline" in doc.lower()

    functions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    names = {node.name for node in functions}
    assert "main" in names
    assert len(functions) <= 3

    main_fn = next(node for node in functions if node.name == "main")
    positional = [arg.arg for arg in main_fn.args.args]
    assert positional == ["argv"]
    assert main_fn.args.defaults
    assert isinstance(main_fn.args.defaults[0], ast.Constant)
    assert main_fn.args.defaults[0].value is None
    assert main_fn.returns is not None
    assert "int" in ast.unparse(main_fn.returns)
