"""Tests for the create_file content-loss guard catching dropped module-level
constants (top-level Assign/AnnAssign targets), extending the existing
_dropped_top_level_defs guard (which only covers def/class).

The guard must UNCONDITIONALLY flag a top-level constant present in the old
file but absent from the new content -- it does NOT require the name to still
be referenced anywhere in the new file (contrast _newly_undefined_module_vars,
which only fires when the dropped name is still referenced). This closes the
gap where a create_file rewrite silently drops a module-level table/constant
that is consumed from other modules.
"""

import importlib.util
import os
from pathlib import Path

os.environ.setdefault("LOCAL_AGENT_MODEL", "test-model")


def _load_local_agent():
    spec = importlib.util.spec_from_file_location(
        "local_agent_dropped_vars",
        str(Path(__file__).parent.parent / "scripts" / "local_agent.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


la = _load_local_agent()


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "_CREATED_THIS_RUN", set())
    monkeypatch.setattr(la, "_VIEWED_THIS_RUN", set())


def test_create_file_rejects_overwrite_that_drops_module_level_constant(tmp_path, monkeypatch):
    """A create_file that drops a module-level constant (FOO = {"a": 1}) must be
    rejected with a message naming FOO and mentioning confirm_removals."""
    _setup(tmp_path, monkeypatch)
    (tmp_path / "mod.py").write_text('FOO = {"a": 1}\n')
    la.run_tool("view_file", {"path": "mod.py"})
    result = la.run_tool("create_file", {"path": "mod.py", "content": "BAR = 1\n"})
    assert result.startswith("ERROR: this create_file overwrite of mod.py would silently drop")
    assert "FOO" in result
    assert "confirm_removals" in result
    # file was NOT overwritten
    assert (tmp_path / "mod.py").read_text() == 'FOO = {"a": 1}\n'


def test_create_file_rejects_dropped_constant_and_def_in_single_message(tmp_path, monkeypatch):
    """Case (a): dropping BOTH a constant and a def in the same call must
    produce a SINGLE rejection naming both FOO and bar."""
    _setup(tmp_path, monkeypatch)
    original = 'FOO = {"a": 1}\n\n\ndef bar():\n    pass\n'
    (tmp_path / "mod.py").write_text(original)
    la.run_tool("view_file", {"path": "mod.py"})
    result = la.run_tool("create_file", {"path": "mod.py", "content": "x = 1\n"})
    assert result.startswith("ERROR: this create_file overwrite of mod.py would silently drop")
    assert "FOO" in result
    assert "bar" in result
    assert (tmp_path / "mod.py").read_text() == original


def test_create_file_dropped_constant_confirm_removals_overwrites(tmp_path, monkeypatch):
    """Case (b): with confirm_removals=True the escape hatch still works and the
    file is overwritten successfully."""
    _setup(tmp_path, monkeypatch)
    original = 'FOO = {"a": 1}\n\n\ndef bar():\n    pass\n'
    (tmp_path / "mod.py").write_text(original)
    la.run_tool("view_file", {"path": "mod.py"})
    new_content = "x = 1\n"
    result = la.run_tool(
        "create_file", {"path": "mod.py", "content": new_content, "confirm_removals": True}
    )
    assert result == "created mod.py"
    assert (tmp_path / "mod.py").read_text() == new_content


def test_create_file_renamed_constant_is_flagged_like_renamed_def(tmp_path, monkeypatch):
    """Case (c): a renamed constant (FOO -> BAR, FOO completely gone from new
    content) is flagged, matching _dropped_top_level_defs's behavior for a
    renamed def (set difference on names: FOO in old set, not in new set)."""
    _setup(tmp_path, monkeypatch)
    (tmp_path / "mod.py").write_text('FOO = {"a": 1}\n')
    la.run_tool("view_file", {"path": "mod.py"})
    result = la.run_tool("create_file", {"path": "mod.py", "content": 'BAR = {"a": 1}\n'})
    assert result.startswith("ERROR: this create_file overwrite of mod.py would silently drop")
    assert "FOO" in result
    assert (tmp_path / "mod.py").read_text() == 'FOO = {"a": 1}\n'


def test_create_file_dropped_constant_non_py_path_no_rejection(tmp_path, monkeypatch):
    """Case (d1): a non-.py path whose old content looks like a constant must
    not be rejected and must not raise."""
    _setup(tmp_path, monkeypatch)
    (tmp_path / "notes.txt").write_text('FOO = {"a": 1}\n')
    la.run_tool("view_file", {"path": "notes.txt"})
    result = la.run_tool("create_file", {"path": "notes.txt", "content": ""})
    assert not result.startswith("ERROR")
    assert (tmp_path / "notes.txt").read_text() == ""


def test_create_file_dropped_constant_unparseable_old_no_rejection(tmp_path, monkeypatch):
    """Case (d2): a .py file whose old content is unparseable must not be
    rejected and must not raise -- the guard returns no names on parse failure."""
    _setup(tmp_path, monkeypatch)
    (tmp_path / "broken.py").write_text("FOO = {{{\n")
    la.run_tool("view_file", {"path": "broken.py"})
    result = la.run_tool("create_file", {"path": "broken.py", "content": "x = 1\n"})
    assert not result.startswith("ERROR")
    assert (tmp_path / "broken.py").read_text() == "x = 1\n"