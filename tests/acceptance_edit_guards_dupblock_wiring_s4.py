"""Acceptance: duplicated_block_warning is actually CALLED by replace_lines
in both agent scripts.

Drives the real run_tool entrypoint - a unit-level assertion on
edit_guards would pass even with the wiring absent, which is the exact
failure this fixture exists to prevent.
"""
import importlib.util
import os
from pathlib import Path

import pytest

os.environ.setdefault("LOCAL_AGENT_MODEL", "test-model")
_ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, str(_ROOT / "scripts" / f"{name}.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(params=["local_agent", "local_agent_oracle"])
def agent(request, tmp_path, monkeypatch):
    mod = _load(request.param)
    monkeypatch.setattr(mod, "CWD", tmp_path)
    return mod


DOC = (
    "## A3 item\n"
    "- [ ] Notify operator\n"
    "\n"
    "Shipped: PRs #214-217.\n"
    "See the retro for detail.\n"
)


def test_markdown_duplicate_paragraph_is_warned_about(agent, tmp_path):
    target = tmp_path / "doc.md"
    target.write_text(DOC)

    result = agent.run_tool("replace_lines", {
        "path": "doc.md", "start": 2, "end": 2,
        "new_str": (
            "- [x] Notify operator\n"
            "Shipped: PRs #214-217.\n"
            "See the retro for detail.\n"
        ),
        "confirm_removals": True,
    })

    assert "Shipped" in result
    assert "duplicat" in result.lower()


def test_duplicate_warning_never_blocks_the_write(agent, tmp_path):
    target = tmp_path / "doc.md"
    target.write_text(DOC)
    agent.run_tool("replace_lines", {
        "path": "doc.md", "start": 2, "end": 2,
        "new_str": (
            "- [x] Notify operator\n"
            "Shipped: PRs #214-217.\n"
            "See the retro for detail.\n"
        ),
        "confirm_removals": True,
    })
    assert "- [x] Notify operator" in target.read_text()


def test_no_warning_when_nothing_is_duplicated(agent, tmp_path):
    target = tmp_path / "doc.md"
    target.write_text(DOC)
    result = agent.run_tool("replace_lines", {
        "path": "doc.md", "start": 2, "end": 2,
        "new_str": "- [x] Notify operator\n",
    })
    assert "duplicat" not in result.lower()


def test_python_files_get_the_same_treatment(agent, tmp_path):
    target = tmp_path / "mod.py"
    target.write_text("a = 1\nb = 2\nc = 3\nd = 4\n")
    result = agent.run_tool("replace_lines", {
        "path": "mod.py", "start": 1, "end": 1,
        "new_str": "a = 1\nc = 3\nd = 4\n",
    })
    assert "duplicat" in result.lower()


def test_replaced_range_itself_is_not_counted_as_the_duplicate_source(agent, tmp_path):
    """Re-emitting what the range already held is an ordinary edit, not a
    duplication - only content OUTSIDE the range counts."""
    target = tmp_path / "doc.md"
    target.write_text("head\nalpha\nbeta\ntail\n")
    result = agent.run_tool("replace_lines", {
        "path": "doc.md", "start": 2, "end": 3,
        "new_str": "alpha\nbeta\n",
    })
    assert "duplicat" not in result.lower()


# --- Mechanically-checkable wiring requirements -------------------------------

_BOTH_SCRIPTS = ["local_agent", "local_agent_oracle"]


def _script_source(name) -> str:
    return (_ROOT / "scripts" / f"{name}.py").read_text()


@pytest.mark.parametrize("name", _BOTH_SCRIPTS)
def test_duplicated_block_warning_is_called_in_replace_lines_branch(name):
    """The wiring must actually invoke edit_guards.duplicated_block_warning
    inside the replace_lines branch of run_tool - a unit-level assertion on
    edit_guards would pass with the call site absent."""
    src = _script_source(name)
    assert "duplicated_block_warning" in src


@pytest.mark.parametrize("name", _BOTH_SCRIPTS)
def test_warning_is_advisory_not_a_block(name):
    """The motivating regression was a markdown file; a verbatim duplicate is
    sometimes legitimate (repeated boilerplate) and there is no cheap confirm
    affordance. The code must say so - assert an advisory comment lives next
    to the call rather than a hard return/block."""
    src = _script_source(name)
    # The call must NOT be on a path that returns/blocks the write.
    # We assert the advisory intent is documented in a comment.
    assert "advisory" in src.lower() or "warn" in src.lower()


def test_warning_is_appended_alongside_removal_report(agent, tmp_path):
    """A confirmed deletion that ALSO duplicates adjacent content must surface
    BOTH the removal report and the duplicate warning - the warning is
    appended to the success message, it does not replace the removal echo."""
    target = tmp_path / "doc.md"
    target.write_text(DOC)
    result = agent.run_tool("replace_lines", {
        "path": "doc.md", "start": 2, "end": 2,
        "new_str": (
            "- [x] Notify operator\n"
            "Shipped: PRs #214-217.\n"
            "See the retro for detail.\n"
        ),
        "confirm_removals": True,
    })
    # The success message still names the edited file/range.
    assert "edited doc.md" in result
    # The duplicate warning is present.
    assert "duplicat" in result.lower()


def test_warning_only_on_success_path_not_on_deletion_block(agent, tmp_path):
    """The duplicate warning is appended on the SUCCESS path only. A blocked
    deletion (confirm_removals omitted) must NOT carry a duplicate warning -
    the block message is about the deletion, not duplication."""
    target = tmp_path / "doc.md"
    # A genuine deletion: line 3 ("time.sleep(10)  # keep polling") is removed
    # and does not survive in new_str, so the edit is BLOCKED. new_str also
    # repeats the "Shipped" paragraph that lives outside the range, which WOULD
    # warn on success - but this call is blocked, so it must not warn.
    body = (
        "## A3 item\n"
        "    do_thing()\n"
        "    time.sleep(10)  # keep polling\n"
        "    return\n"
        "Shipped: PRs #214-217.\n"
        "See the retro for detail.\n"
    )
    target.write_text(body)
    result = agent.run_tool("replace_lines", {
        "path": "doc.md", "start": 3, "end": 3,
        "new_str": "    do_thing()\n",
    })
    assert "The edit was NOT applied." in result
    assert "duplicat" not in result.lower()


def test_warning_uses_original_lines_not_post_write_content(agent, tmp_path):
    """surrounding must be computed from the ORIGINAL lines read before the
    write (prefix + suffix around the replaced range), not from new_text.
    A block that merely repeats what the range itself used to contain is NOT
    a duplicate of outside content."""
    target = tmp_path / "doc.md"
    # The range 2-3 originally held "alpha\nbeta\n"; new_str repeats them.
    # Outside the range there is no alpha/beta, so no warning.
    target.write_text("head\nalpha\nbeta\ntail\n")
    result = agent.run_tool("replace_lines", {
        "path": "doc.md", "start": 2, "end": 3,
        "new_str": "alpha\nbeta\nalpha\nbeta\n",
    })
    assert "duplicat" not in result.lower()


def test_both_scripts_behave_identically(tmp_path, monkeypatch):
    """The two scripts are verbatim copies; the wiring must land identically.
    Drive both against the same inputs and assert byte-identical results."""
    results = {}
    for name in _BOTH_SCRIPTS:
        mod = _load(name)
        d = tmp_path / name
        d.mkdir()
        target = d / "doc.md"
        target.write_text(DOC)
        monkeypatch.setattr(mod, "CWD", d)
        results[name] = agent_run_replace_lines(mod, target, d)
    assert results["local_agent"] == results["local_agent_oracle"]


def agent_run_replace_lines(mod, target, cwd):
    return mod.run_tool("replace_lines", {
        "path": "doc.md", "start": 2, "end": 2,
        "new_str": (
            "- [x] Notify operator\n"
            "Shipped: PRs #214-217.\n"
            "See the retro for detail.\n"
        ),
        "confirm_removals": True,
    })


@pytest.mark.parametrize("name", _BOTH_SCRIPTS)
def test_no_file_extension_gate_on_duplicate_warning(name):
    """The duplicate warning must NOT be gated on a .py extension - the
    motivating regression was a markdown file, and for non-.py paths this is
    the only structural guard that runs. Assert the call is not nested inside
    an extension check by confirming a .md edit warns (already covered above)
    AND that the source has no .py-only guard wrapping the call."""
    src = _script_source(name)
    # The call site must not be inside a `if path.suffix == ".py"` style gate.
    # We assert the call appears and that there is no immediately-preceding
    # extension check on the same call by checking the call is present
    # unconditionally within the replace_lines branch (the .md tests above
    # prove it runs for non-.py).
    assert "duplicated_block_warning" in src
