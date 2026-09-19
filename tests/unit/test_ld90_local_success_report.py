"""LD90-W0-07: the local-success reporting CLI (``scripts/local_success_report.py``).

The CLI is a thin I/O + printing layer over the pure classifier in
``pipeline/local_success``: it walks ``*.manifest.json`` files in a plan
directory, loads the matching ``.notifications.jsonl`` sidecar, classifies
every story dict in the manifest, and prints a windowed clean-rate block for
"overall" plus each tier, with the reason breakdown underneath each block.

Everything here is synthetic: manifests and sidecars are written into
``tmp_path`` and the CLI is imported by path.  No real plan directory (e.g.
``~/.claude/plans``) is read and no live baseline number is asserted - the
"reproduces the recorded baseline" check is a manual, post-merge operator
check run with ``--window 0`` against real data.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "local_success_report.py"

TIER_LABELS = ("on-device", "cloud-oss", "unknown")


# --------------------------------------------------------------------------- #
# Loading the CLI under test
# --------------------------------------------------------------------------- #


def _load_report():
    """Import ``scripts/local_success_report.py`` by path, at call time.

    Deferred to call time (not module import) so a bare
    ``pytest --collect-only`` still collects this file when the script does
    not exist yet; each test then fails naming the missing script.
    """
    assert SCRIPT_PATH.is_file(), f"missing CLI script: {SCRIPT_PATH}"
    spec = importlib.util.spec_from_file_location("local_success_report", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None, f"cannot load {SCRIPT_PATH}"
    module = importlib.util.module_from_spec(spec)
    sys.modules["local_success_report"] = module
    spec.loader.exec_module(module)
    return module


def _run(module, argv, capsys):
    """Call ``main(argv)`` and return ``(returncode, stdout, stderr)``."""
    rc = module.main(argv)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


# --------------------------------------------------------------------------- #
# Synthetic plan directories
# --------------------------------------------------------------------------- #


def _write_manifest(plan_dir: Path, plan: str, stories: dict) -> Path:
    path = plan_dir / f"{plan}.manifest.json"
    path.write_text(json.dumps({"stories": stories}), encoding="utf-8")
    return path


def _write_records(plan_dir: Path, plan: str, records: list[dict]) -> Path:
    path = plan_dir / f"{plan}.notifications.jsonl"
    body = "".join(json.dumps(rec) + "\n" for rec in records)
    path.write_text(body, encoding="utf-8")
    return path


def _story(dispatched_at: str, *, backend: str = "ollama", status: str = "done") -> dict:
    return {"backend": backend, "status": status, "dispatched_at": dispatched_at}


def _write_plan_a(plan_dir: Path) -> None:
    """Plan "a": S1 clean (merged), S2 dirty (escalated), S2 dispatched later."""
    _write_manifest(
        plan_dir,
        "a",
        {
            "S1": _story("2026-09-01T00:00:00+00:00"),
            "S2": _story("2026-09-02T00:00:00+00:00"),
        },
    )
    _write_records(
        plan_dir,
        "a",
        [
            {"event": "story_merged", "story_key": "S1"},
            {"event": "escalated", "story_key": "S2"},
        ],
    )


@pytest.fixture
def plan_dir(tmp_path: Path) -> Path:
    _write_plan_a(tmp_path)
    return tmp_path


# --------------------------------------------------------------------------- #
# stdout parsing helpers
# --------------------------------------------------------------------------- #


def _lines(stdout: str) -> list[str]:
    return stdout.splitlines()


def _label_index(stdout: str, label: str) -> int | None:
    for index, line in enumerate(_lines(stdout)):
        if line.startswith(f"{label}:"):
            return index
    return None


def _block(stdout: str, label: str) -> list[str]:
    """Return the indented reason lines printed under the ``<label>:`` line."""
    start = _label_index(stdout, label)
    assert start is not None, f"no {label!r} line in stdout:\n{stdout}"
    reasons: list[str] = []
    for line in _lines(stdout)[start + 1 :]:
        if not line.startswith((" ", "\t")):
            break
        reasons.append(line.strip())
    return reasons


def _assert_na_line(stdout: str, label: str) -> None:
    """Assert ``<label>: 0/0 clean (n/a)``.

    The brief's template is ``<label>: <clean>/<count> clean (<pct>%)`` with
    ``n/a`` substituted for the percentage when the count is zero, so the
    trailing ``%`` is accepted either way.
    """
    pattern = rf"^{re.escape(label)}: 0/0 clean \(n/a%?\)$"
    assert re.search(pattern, stdout, re.MULTILINE), (
        f"no {label!r} n/a line in stdout:\n{stdout}"
    )


def _reason_line(stdout: str, text: str) -> str:
    matches = [line for line in _lines(stdout) if text in line]
    assert matches, f"no line containing {text!r} in stdout:\n{stdout}"
    return matches[0]


# --------------------------------------------------------------------------- #
# Behaviour: windowing and the printed blocks
# --------------------------------------------------------------------------- #


def test_window_zero_reports_the_whole_population(plan_dir, capsys):
    module = _load_report()
    rc, out, _ = _run(module, ["--plan-dir", str(plan_dir), "--window", "0"], capsys)

    assert rc == 0
    assert "overall: 1/2 clean (50.0%)" in out
    assert "escalated: 1" in _block(out, "overall")


def test_reason_lines_are_indented_under_their_block(plan_dir, capsys):
    module = _load_report()
    _, out, _ = _run(module, ["--plan-dir", str(plan_dir), "--window", "0"], capsys)

    line = _reason_line(out, "escalated: 1")
    assert line.startswith((" ", "\t")), f"reason line is not indented: {line!r}"


def test_window_one_keeps_only_the_latest_story(plan_dir, capsys):
    module = _load_report()
    rc, out, _ = _run(module, ["--plan-dir", str(plan_dir), "--window", "1"], capsys)

    assert rc == 0
    # S2 (2026-09-02) is the latest story and it escalated, so the single
    # windowed story is dirty.
    assert "overall: 0/1 clean (0.0%)" in out
    assert "escalated: 1" in _block(out, "overall")


def test_overall_block_precedes_tier_blocks_in_tier_order(plan_dir, capsys):
    module = _load_report()
    rc, out, _ = _run(module, ["--plan-dir", str(plan_dir), "--window", "0"], capsys)

    assert rc == 0
    positions = []
    for label in ("overall", *TIER_LABELS):
        index = _label_index(out, label)
        assert index is not None, f"missing {label!r} block in stdout:\n{out}"
        positions.append(index)
    assert positions == sorted(positions), f"blocks out of order in stdout:\n{out}"


def test_tier_blocks_are_always_printed_with_na_for_empty_tiers(plan_dir, capsys):
    module = _load_report()
    _, out, _ = _run(module, ["--plan-dir", str(plan_dir), "--window", "0"], capsys)

    assert "on-device: 1/2 clean (50.0%)" in out
    _assert_na_line(out, "cloud-oss")
    _assert_na_line(out, "unknown")


def test_header_line_names_the_window(plan_dir, capsys):
    module = _load_report()

    _, default_out, _ = _run(module, ["--plan-dir", str(plan_dir)], capsys)
    default_header = _lines(default_out)[0]
    assert "window" in default_header.lower()
    assert re.search(r"\b30\b", default_header), default_header

    _, zero_out, _ = _run(module, ["--plan-dir", str(plan_dir), "--window", "0"], capsys)
    zero_header = _lines(zero_out)[0]
    assert "window" in zero_header.lower()
    assert re.search(r"\b0\b", zero_header), zero_header


def test_reason_lines_sort_by_count_descending_then_name(tmp_path, capsys):
    _write_manifest(
        tmp_path,
        "c",
        {
            "C1": _story("2026-09-01T00:00:00+00:00"),
            "C2": _story("2026-09-02T00:00:00+00:00"),
            "C3": _story("2026-09-03T00:00:00+00:00", status="dispatched"),
            "C4": _story("2026-09-04T00:00:00+00:00"),
            "C5": _story("2026-09-05T00:00:00+00:00"),
        },
    )
    _write_records(
        tmp_path,
        "c",
        [
            {"event": "escalated", "story_key": "C1"},
            {"event": "escalated", "story_key": "C2"},
            {"event": "story_parked", "story_key": "C4"},
            {"event": "brief_patched", "story_key": "C5"},
        ],
    )

    module = _load_report()
    rc, out, _ = _run(module, ["--plan-dir", str(tmp_path), "--window", "0"], capsys)

    assert rc == 0
    reasons = _block(out, "overall")
    assert "escalated: 2" in reasons
    assert "not_done: 1" in reasons
    assert "story_parked: 1" in reasons
    assert "brief_patched: 1" in reasons
    # Highest count first, then ties broken by reason name ascending.
    assert reasons.index("escalated: 2") < reasons.index("brief_patched: 1")
    assert reasons.index("brief_patched: 1") < reasons.index("not_done: 1")
    assert reasons.index("not_done: 1") < reasons.index("story_parked: 1")


# --------------------------------------------------------------------------- #
# Behaviour: sidecar selection
# --------------------------------------------------------------------------- #


def test_legacy_notifications_log_sidecar_is_ignored(plan_dir, capsys):
    (plan_dir / "a.notifications.log").write_text(
        "S1 escalating to Claude\n", encoding="utf-8"
    )

    module = _load_report()
    rc, out, _ = _run(module, ["--plan-dir", str(plan_dir), "--window", "0"], capsys)

    assert rc == 0
    assert "overall: 1/2 clean (50.0%)" in out
    assert "legacy_message" not in out


def test_legacy_log_alone_is_not_read_as_a_fallback(tmp_path, capsys):
    _write_manifest(tmp_path, "g", {"G1": _story("2026-09-01T00:00:00+00:00")})
    (tmp_path / "g.notifications.log").write_text(
        "G1 escalating to Claude\n", encoding="utf-8"
    )

    module = _load_report()
    rc, out, _ = _run(module, ["--plan-dir", str(tmp_path), "--window", "0"], capsys)

    assert rc == 0
    assert "overall: 1/1 clean (100.0%)" in out
    assert "legacy_message" not in out


def test_missing_notifications_sidecar_yields_no_records(tmp_path, capsys):
    _write_manifest(tmp_path, "d", {"D1": _story("2026-09-01T00:00:00+00:00")})

    module = _load_report()
    rc, out, _ = _run(module, ["--plan-dir", str(tmp_path), "--window", "0"], capsys)

    assert rc == 0
    assert "overall: 1/1 clean (100.0%)" in out


# --------------------------------------------------------------------------- #
# Behaviour: malformed / partial manifests
# --------------------------------------------------------------------------- #


def test_corrupt_manifest_is_skipped_with_one_warning_line(plan_dir, capsys):
    (plan_dir / "b.manifest.json").write_text("{not valid json", encoding="utf-8")

    module = _load_report()
    rc, out, err = _run(module, ["--plan-dir", str(plan_dir), "--window", "0"], capsys)

    assert rc == 0
    assert "overall: 1/2 clean (50.0%)" in out
    warning_lines = [line for line in err.splitlines() if line.strip()]
    assert len(warning_lines) == 1, f"expected exactly one warning line, got: {err!r}"
    assert re.search(r"\bb\b", err), f"warning does not name the plan: {err!r}"


def test_unreadable_manifest_is_skipped_with_a_warning(plan_dir, capsys):
    # A directory named like a manifest makes the read raise OSError.
    (plan_dir / "b.manifest.json").mkdir()

    module = _load_report()
    rc, out, err = _run(module, ["--plan-dir", str(plan_dir), "--window", "0"], capsys)

    assert rc == 0
    assert "overall: 1/2 clean (50.0%)" in out
    assert re.search(r"\bb\b", err), f"warning does not name the plan: {err!r}"


def test_manifest_without_stories_key_is_empty_not_an_error(tmp_path, capsys):
    (tmp_path / "e.manifest.json").write_text(json.dumps({}), encoding="utf-8")

    module = _load_report()
    rc, out, _ = _run(module, ["--plan-dir", str(tmp_path), "--window", "0"], capsys)

    assert rc == 0
    _assert_na_line(out, "overall")


def test_non_dict_story_entries_are_ignored(tmp_path, capsys):
    _write_manifest(
        tmp_path,
        "f",
        {"F1": _story("2026-09-01T00:00:00+00:00"), "F2": "not-a-story-dict"},
    )

    module = _load_report()
    rc, out, _ = _run(module, ["--plan-dir", str(tmp_path), "--window", "0"], capsys)

    assert rc == 0
    assert "overall: 1/1 clean (100.0%)" in out


# --------------------------------------------------------------------------- #
# Behaviour: plan-dir resolution and exit codes
# --------------------------------------------------------------------------- #


def test_nonexistent_plan_dir_returns_2_with_stderr_message(tmp_path, capsys):
    module = _load_report()
    rc, out, err = _run(
        module, ["--plan-dir", str(tmp_path / "does-not-exist"), "--window", "0"], capsys
    )

    assert rc == 2
    assert err.strip(), "expected a message on stderr for a missing plan dir"
    assert "overall:" not in out


def test_empty_plan_dir_returns_0_and_prints_na(tmp_path, capsys):
    module = _load_report()
    rc, out, _ = _run(module, ["--plan-dir", str(tmp_path), "--window", "0"], capsys)

    assert rc == 0
    _assert_na_line(out, "overall")
    for label in TIER_LABELS:
        _assert_na_line(out, label)


def test_plan_dir_defaults_to_the_plan_dir_env_var(plan_dir, capsys, monkeypatch):
    monkeypatch.setenv("PLAN_DIR", str(plan_dir))

    module = _load_report()
    rc, out, _ = _run(module, ["--window", "0"], capsys)

    assert rc == 0
    assert "overall: 1/2 clean (50.0%)" in out


def test_plan_dir_env_value_is_expanded_with_expanduser(tmp_path, capsys, monkeypatch):
    home = tmp_path / "home"
    plans = home / "plans"
    plans.mkdir(parents=True)
    _write_plan_a(plans)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PLAN_DIR", "~/plans")

    module = _load_report()
    rc, out, _ = _run(module, ["--window", "0"], capsys)

    assert rc == 0
    assert "overall: 1/2 clean (50.0%)" in out


def test_plan_dir_default_is_claude_plans_under_home(tmp_path, capsys, monkeypatch):
    home = tmp_path / "home"
    plans = home / ".claude" / "plans"
    plans.mkdir(parents=True)
    _write_plan_a(plans)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PLAN_DIR", raising=False)

    module = _load_report()
    rc, out, _ = _run(module, ["--window", "0"], capsys)

    assert rc == 0
    assert "overall: 1/2 clean (50.0%)" in out


# --------------------------------------------------------------------------- #
# CLI surface
# --------------------------------------------------------------------------- #


def test_main_accepts_an_optional_argv_list():
    module = _load_report()
    signature = inspect.signature(module.main)
    assert "argv" in signature.parameters
    assert signature.parameters["argv"].default is None
    assert str(signature.return_annotation) in {"int", "<class 'int'>"}


def test_help_exits_zero_and_documents_both_options(capsys):
    module = _load_report()
    with pytest.raises(SystemExit) as excinfo:
        module.main(["--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "--plan-dir" in out
    assert "--window" in out


def test_non_integer_window_is_rejected_by_argparse(capsys):
    module = _load_report()
    with pytest.raises(SystemExit) as excinfo:
        module.main(["--window", "not-a-number"])
    assert excinfo.value.code == 2


# --------------------------------------------------------------------------- #
# Source-level requirements (imports, no reimplementation, docstring)
# --------------------------------------------------------------------------- #


def _source() -> str:
    assert SCRIPT_PATH.is_file(), f"missing CLI script: {SCRIPT_PATH}"
    return SCRIPT_PATH.read_text(encoding="utf-8")


def _imported_modules(source: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_imports_the_classifier_and_the_sidecar_loader_only():
    source = _source()
    imported = _imported_modules(source)

    assert "pipeline.local_success" in imported
    assert "pipeline.story_metrics" in imported
    assert "load_notification_records" in source
    assert "classify_story" in source
    assert "rolling_rate" in source


def test_does_not_import_config_loading_modules():
    imported = _imported_modules(_source())

    assert "pipeline.server" not in imported
    assert "pipeline.paths" not in imported
    assert not any(name == "app" or name.startswith("app.") for name in imported)


def test_does_not_reimplement_classification_or_read_legacy_logs():
    source = _source()

    assert "def classify_story" not in source
    assert "def rolling_rate" not in source
    assert ".notifications.jsonl" in source


def test_follows_the_scripts_import_convention():
    source = _source()

    assert "Path(__file__).resolve().parent.parent" in source
    assert "sys.path.insert" in source
    assert "expanduser" in source


def test_has_a_main_guard_and_at_most_three_functions():
    source = _source()
    tree = ast.parse(source)

    assert 'if __name__ == "__main__":' in source
    assert "sys.exit(main())" in source

    functions = [node.name for node in tree.body if isinstance(node, ast.FunctionDef)]
    assert "main" in functions
    assert len(functions) <= 3, f"expected main plus at most two helpers, got {functions}"


def test_module_docstring_documents_purpose_definitions_and_usage():
    doc = ast.get_docstring(ast.parse(_source()))
    assert doc, "module docstring is missing"

    assert "pipeline/local_success.py" in doc
    assert "scripts/local_success_report.py" in doc
    assert "--window" in doc
    assert "--plan-dir" in doc
    assert "--window 0" in doc
