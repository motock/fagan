"""RH-10: scripts/local_agent.py must come under the 1000-line limit by moving a
concern into a NEW sibling module, without breaking the re-export contract or
monkeypatch reach.

The new module's name is the implementer's choice, so it is discovered as any
scripts/*.py file that did not exist before the split (_BASELINE_SCRIPTS).
"""
import ast
import importlib
import importlib.util
import os
from pathlib import Path

import pytest

from scripts import check_line_limit

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LOCAL_AGENT = REPO_ROOT / "scripts" / "local_agent.py"
LINE_LIMIT = 1000

_BASELINE_SCRIPTS = frozenset({
    "check_line_limit.py", "choose_providers.py", "install_checks.py",
    "install_global_rules.py", "local_agent.py", "local_agent_chat.py",
    "local_agent_config.py", "local_agent_git.py", "local_agent_guards.py",
    "local_agent_oracle.py", "local_agent_oracle_chat.py",
    "local_agent_oracle_config.py", "local_agent_oracle_git.py",
    "local_agent_oracle_guards.py", "local_agent_oracle_recovery.py",
    "local_agent_oracle_repair.py", "local_agent_oracle_tools.py",
    "local_agent_recovery.py", "local_agent_repair.py", "local_agent_tools.py",
    "local_success_report.py", "mlx_server_supervisor.py",
    "mlx_server_wrapper.py", "reset_false_positive_tests_passed.py",
    "smoke_getting_started.py",
})

# Every top-level function local_agent.py defined before the split: each must
# stay reachable as an attribute of the module (survivor list).
_PRE_SPLIT_FUNCTIONS = (
    "_effective_chars_per_token", "read_correlation_id", "emit_step_line",
    "_apply_off_task_action", "_stream_one_turn", "_provider_chat_turn",
    "_ollama_payload", "chat", "_repair_triple_quoted_strings",
    "recover_tool_calls", "git", "exclude_runtime_artifacts", "worktree_dirty",
    "auto_wip_commit", "_full_suite_result", "_step_cap_auto_done",
    "_reject_done_for_suite", "run_tool", "safe_run_tool",
    "recover_from_oversized_5xx", "_main_impl", "main", "write_done_marker",
)


def _load_local_agent():
    os.environ.setdefault("LOCAL_AGENT_MODEL", "test-model")
    spec = importlib.util.spec_from_file_location("local_agent_split_under_test", str(LOCAL_AGENT))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _new_module_paths():
    return sorted(
        p for p in (REPO_ROOT / "scripts").glob("*.py") if p.name not in _BASELINE_SCRIPTS
    )


def _top_level_names(path):
    tree = ast.parse(path.read_text())
    return [
        node.name for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.ClassDef))
    ]


def _line_count(path):
    return len(path.read_text().splitlines())


def test_local_agent_is_under_the_line_limit():
    assert _line_count(LOCAL_AGENT) < LINE_LIMIT


def test_local_agent_allowlist_entry_is_retired():
    assert "scripts/local_agent.py" not in check_line_limit._BLOCKING_ALLOWLIST


def test_line_limit_gate_passes_local_agent_without_an_exemption(tmp_path, monkeypatch, capsys):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "local_agent.py").write_text(LOCAL_AGENT.read_text())
    monkeypatch.setattr(check_line_limit, "_BLOCKING_ALLOWLIST", {})

    rc = check_line_limit.check(tmp_path)

    assert rc == 0, capsys.readouterr().out


def test_extraction_created_a_new_sibling_module():
    assert _new_module_paths() != []


def test_new_module_is_under_the_line_limit():
    oversized = [p.name for p in _new_module_paths() if _line_count(p) >= LINE_LIMIT]
    assert _new_module_paths() != [] and oversized == []


def test_moved_names_are_importable_from_the_new_module():
    missing = []
    for path in _new_module_paths():
        module = importlib.import_module(f"scripts.{path.stem}")
        missing += [n for n in _top_level_names(path) if not hasattr(module, n)]
    assert _new_module_paths() != [] and missing == []


def test_local_agent_reexports_the_identical_moved_objects():
    la = _load_local_agent()
    exposed = {}
    for path in _new_module_paths():
        module = importlib.import_module(f"scripts.{path.stem}")
        for name in _top_level_names(path):
            exposed[name] = (getattr(module, name), getattr(la, name, None))
    not_identical = [n for n, (new, old) in exposed.items() if old is not new]
    assert exposed != {} and not_identical == []


def test_local_agent_still_exposes_every_pre_split_function():
    la = _load_local_agent()
    missing = [n for n in _PRE_SPLIT_FUNCTIONS if not callable(getattr(la, n, None))]
    assert missing == []


def _run_main_with(la, monkeypatch, tmp_path, responses):
    monkeypatch.setattr(la, "CWD", tmp_path)
    calls = []

    def fake_chat(messages):
        calls.append(messages)
        fn, args = responses[min(len(calls) - 1, len(responses) - 1)]
        return {"role": "assistant", "content": "",
                "tool_calls": [{"function": {"name": fn, "arguments": args}}]}

    monkeypatch.setattr(la, "chat", fake_chat)
    return la.main(), calls


def test_patching_chat_on_local_agent_reaches_the_step_loop(tmp_path, monkeypatch):
    la = _load_local_agent()

    rc, calls = _run_main_with(la, monkeypatch, tmp_path, [("done", {"summary": "ok"})])

    assert (rc, len(calls)) == (0, 1)


def test_patching_safe_run_tool_on_local_agent_reaches_the_step_loop(tmp_path, monkeypatch):
    la = _load_local_agent()
    seen = []
    monkeypatch.setattr(la, "safe_run_tool", lambda fn, args: seen.append(fn) or "faked")

    _run_main_with(la, monkeypatch, tmp_path, [("bash", {"command": "true"}), ("done", {"summary": "ok"})])

    assert "bash" in seen


def test_patched_safe_run_tool_is_not_cached_after_monkeypatch_restores(tmp_path):
    la = _load_local_agent()
    seen = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(la, "safe_run_tool", lambda fn, args: seen.append(fn) or "faked")
        _run_main_with(la, mp, tmp_path, [("bash", {"command": "true"}), ("done", {"summary": "ok"})])
    seen.clear()
    with pytest.MonkeyPatch.context() as mp:
        _run_main_with(la, mp, tmp_path, [("bash", {"command": "true"}), ("done", {"summary": "ok"})])

    assert seen == []
