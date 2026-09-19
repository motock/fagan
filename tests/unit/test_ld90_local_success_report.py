"""Tests for the LD90-W0 local first-pass-clean reporting CLI.

The CLI is driven through ``main([...])``; every fixture is built under
``tmp_path`` so nothing reads or writes the real ``~/.claude/plans``.
"""

from __future__ import annotations

import ast
import inspect
import json
import re
from pathlib import Path

import pytest

from scripts.local_success_report import main

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "local_success_report.py"
TIERS = ("on-device", "cloud-oss", "unknown")


def _story(**over):
    """A story that is clean, in population, on-device and dispatched."""
    return {
        "status": "done",
        "backend": "ollama",
        "dispatched_at": "2026-01-01T00:00:00Z",
        "dispatched_model": "m",
        **over,
    }


def _plan(plan_dir, plan, stories, records=None):
    """Write ``<plan>.manifest.json`` and, when given, its JSONL sidecar."""
    (plan_dir / f"{plan}.manifest.json").write_text(
        json.dumps({"stories": stories}), encoding="utf-8"
    )
    if records is not None:
        (plan_dir / f"{plan}.notifications.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
        )


def _run(capsys, plan_dir, *args):
    """Run the CLI, assert success, and return its stdout."""
    assert main(["--plan-dir", str(plan_dir), *args]) == 0
    return capsys.readouterr().out


def _blocks(out):
    """Map each unindented ``<label>: ... clean (...)`` header to its block."""
    blocks, current = {}, None
    for line in out.splitlines():
        if line[:1].isspace():
            if current and line.strip():
                blocks[current]["reasons"].append(line.strip())
            continue
        label, sep, rest = line.strip().partition(":")
        current = label if sep and " clean (" in rest else None
        if current:
            blocks[current] = {"header": line.strip(), "reasons": []}
    return blocks


def _header(out, label):
    blocks = _blocks(out)
    assert label in blocks, f"no {label!r} block in output:\n{out}"
    return blocks[label]["header"]


def _reasons(out, label="overall"):
    blocks = _blocks(out)
    assert label in blocks, f"no {label!r} block in output:\n{out}"
    return blocks[label]["reasons"]


def _assert_na(out, label):
    """A block with no stories in the window prints ``n/a``, not ``0.0%``."""
    header = _header(out, label)
    assert re.fullmatch(rf"{re.escape(label)}: 0/0 clean \(n/a%?\)", header), header


def test_clean_story_reports_full_rate(tmp_path, capsys):
    _plan(tmp_path, "alpha", {"s1": _story()})
    out = _run(capsys, tmp_path)
    assert _header(out, "overall") == "overall: 1/1 clean (100.0%)"
    assert _header(out, "on-device") == "on-device: 1/1 clean (100.0%)"
    assert _reasons(out) == []


def test_escalated_and_parked_stories_are_not_clean(tmp_path, capsys):
    recs = [
        {"event": "escalated", "story_key": "s1"},
        {"event": "story_parked", "story_key": "s2"},
    ]
    _plan(tmp_path, "alpha", {"s1": _story(), "s2": _story()}, recs)
    out = _run(capsys, tmp_path)
    assert _header(out, "overall") == "overall: 0/2 clean (0.0%)"
    assert _reasons(out) == ["escalated: 1", "story_parked: 1"]


def test_claude_story_is_out_of_population(tmp_path, capsys):
    _plan(tmp_path, "alpha", {"s1": _story(backend="claude")})
    out = _run(capsys, tmp_path)
    _assert_na(out, "overall")
    assert "1/1" not in out


def test_cloud_model_reports_in_cloud_oss_tier(tmp_path, capsys):
    _plan(tmp_path, "alpha", {"s1": _story(dispatched_model="glm-5.3-flash:cloud")})
    out = _run(capsys, tmp_path)
    assert _header(out, "cloud-oss") == "cloud-oss: 1/1 clean (100.0%)"
    _assert_na(out, "on-device")
    assert _header(out, "overall") == "overall: 1/1 clean (100.0%)"


def test_window_zero_keeps_all_and_small_window_keeps_newest(tmp_path, capsys):
    stories = {
        "old": _story(status="parked", dispatched_at="2026-01-01T00:00:00Z"),
        "mid": _story(dispatched_at="2026-01-02T00:00:00Z"),
        "new": _story(dispatched_at="2026-01-03T00:00:00Z"),
    }
    _plan(tmp_path, "alpha", stories)
    out = _run(capsys, tmp_path, "--window", "0")
    assert _header(out, "overall") == "overall: 2/3 clean (66.7%)"
    assert "not_done: 1" in _reasons(out)

    out = _run(capsys, tmp_path, "--window", "2")
    assert _header(out, "overall") == "overall: 2/2 clean (100.0%)"
    assert _reasons(out) == []
    header = next(ln for ln in out.splitlines() if ln.strip())
    assert "window" in header.lower() and "2" in header


def test_missing_plan_dir_returns_two(tmp_path, capsys):
    assert main(["--plan-dir", str(tmp_path / "nope")]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip()
    with pytest.raises(SystemExit):
        main(["--plan-dir", str(tmp_path), "--window", "abc"])


def test_malformed_manifest_is_warned_and_skipped(tmp_path, capsys):
    (tmp_path / "broken.manifest.json").write_text("{", encoding="utf-8")
    _plan(tmp_path, "alpha", {"s1": _story()})
    assert main(["--plan-dir", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert "broken" in captured.err
    assert _header(captured.out, "overall") == "overall: 1/1 clean (100.0%)"


def test_empty_plan_dir_reports_na_blocks(tmp_path, capsys):
    out = _run(capsys, tmp_path)
    for label in ("overall", *TIERS):
        _assert_na(out, label)


def test_default_window_is_thirty(tmp_path, capsys):
    stories = {
        f"s{i:02d}": _story(dispatched_at=f"2026-01-{i:02d}T00:00:00Z")
        for i in range(1, 32)
    }
    stories["s01"]["status"] = "parked"
    _plan(tmp_path, "alpha", stories)
    assert _header(_run(capsys, tmp_path), "overall") == "overall: 30/30 clean (100.0%)"
    out = _run(capsys, tmp_path, "--window", "0")
    assert _header(out, "overall") == "overall: 30/31 clean (96.8%)"


def test_plan_dir_defaults_to_env_and_expands_user(tmp_path, capsys, monkeypatch):
    _plan(tmp_path, "alpha", {"s1": _story()})
    monkeypatch.setenv("PLAN_DIR", str(tmp_path))
    assert main([]) == 0
    assert _header(capsys.readouterr().out, "overall") == "overall: 1/1 clean (100.0%)"

    home = tmp_path / "home"
    (home / "plans").mkdir(parents=True)
    _plan(home / "plans", "beta", {"s2": _story()})
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PLAN_DIR", "~/plans")
    assert main([]) == 0
    assert _header(capsys.readouterr().out, "overall") == "overall: 1/1 clean (100.0%)"


def test_every_reason_label_and_reason_ordering(tmp_path, capsys):
    stories = {
        "notdone": _story(status="parked"),
        "fallback": _story(),
        "patched": _story(),
        "legacy1": _story(),
        "legacy2": _story(),
        "rewrite": _story(agent_instructions="Please REWORK the brief."),
    }
    recs = [
        {"event": "model_fallback", "story_key": "fallback"},
        {"event": "brief_patched", "story_key": "patched"},
        {"story_key": "legacy1", "message": "escalation triage wedge"},
        {"story_key": "legacy2", "message": "story parked"},
    ]
    _plan(tmp_path, "alpha", stories, recs)
    out = _run(capsys, tmp_path)
    assert _header(out, "overall") == "overall: 0/6 clean (0.0%)"
    assert _reasons(out) == [
        "legacy_message: 2",
        "brief_patched: 1",
        "brief_rewrite_marker: 1",
        "model_fallback: 1",
        "not_done: 1",
    ]


def test_legacy_notifications_log_is_ignored(tmp_path, capsys):
    _plan(tmp_path, "alpha", {"s1": _story()}, records=[])
    (tmp_path / "alpha.notifications.log").write_text(
        json.dumps({"event": "escalated", "story_key": "s1"}) + "\n", encoding="utf-8"
    )
    out = _run(capsys, tmp_path)
    assert _header(out, "overall") == "overall: 1/1 clean (100.0%)"
    assert _reasons(out) == []


def test_non_dict_stories_and_missing_stories_key_are_ignored(tmp_path, capsys):
    (tmp_path / "beta.manifest.json").write_text(
        json.dumps({"stories": {"good": _story(), "bad": "not-a-dict"}}), encoding="utf-8"
    )
    (tmp_path / "gamma.manifest.json").write_text("{}", encoding="utf-8")
    out = _run(capsys, tmp_path)
    assert _header(out, "overall") == "overall: 1/1 clean (100.0%)"


def test_script_source_contract():
    source = SCRIPT.read_text(encoding="utf-8")
    assert not source.startswith("#!")
    assert source.lstrip().startswith('"""')
    for required in (
        "ROOT = Path(__file__).resolve().parent.parent",
        "sys.path.insert(0, str(ROOT))",
        "from pipeline.local_success import classify_story, rolling_rate",
        "from pipeline.story_metrics import load_notification_records",
        '.removesuffix(".manifest.json")',
        'if __name__ == "__main__":',
        "sys.exit(main())",
    ):
        assert required in source, required

    tree = ast.parse(source)
    docstring = ast.get_docstring(tree) or ""
    for required in ("pipeline/local_success.py", "scripts/local_success_report.py", "--window 0"):
        assert required in docstring, required

    modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.append(node.module or "")
    assert not any(m.startswith(("pipeline.server", "pipeline.paths", "app")) for m in modules)

    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    assert len(functions) <= 3
    assert inspect.signature(main).parameters["argv"].default is None
