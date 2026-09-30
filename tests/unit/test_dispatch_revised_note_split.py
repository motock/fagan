"""RH-13: the revised-instructions note moved out of pipeline/dispatch.py."""

from pathlib import Path

from pipeline import dispatch, dispatch_revised_note

REPO_ROOT = Path(__file__).resolve().parents[2]
LIMIT = 1000


def test_should_reexport_moved_symbol_as_same_object():
    assert (
        dispatch._revised_instructions_note
        is dispatch_revised_note._revised_instructions_note
    )


def test_should_import_moved_symbol_from_new_module():
    from pipeline.dispatch_revised_note import _revised_instructions_note

    assert callable(_revised_instructions_note)


def test_should_keep_dispatch_module_under_line_limit():
    lines = sum(1 for _ in (REPO_ROOT / "pipeline/dispatch.py").open())
    assert lines < LIMIT


def test_should_keep_new_module_under_line_limit():
    lines = sum(1 for _ in (REPO_ROOT / "pipeline/dispatch_revised_note.py").open())
    assert lines < LIMIT


def test_should_not_list_dispatch_in_line_limit_allowlist():
    source = (REPO_ROOT / "scripts/check_line_limit.py").read_text()
    assert '"pipeline/dispatch.py"' not in source


def test_should_emit_note_when_instructions_changed_on_transcript_resume():
    note = dispatch._revised_instructions_note(True, "new brief", "old brief")
    assert "Revised instructions from your tech lead" in note
    assert note.endswith("new brief")


def test_should_emit_nothing_when_instructions_unchanged():
    assert dispatch._revised_instructions_note(True, "same", "same") == ""


def test_should_emit_nothing_when_no_prior_snapshot():
    assert dispatch._revised_instructions_note(True, "new", None) == ""


def test_should_emit_nothing_when_not_resuming_via_transcript():
    assert dispatch._revised_instructions_note(False, "new", "old") == ""


def test_should_apply_patched_reexport_end_to_end_in_dispatch(monkeypatch):
    """Monkeypatch reach: dispatch_story's call site resolves the name via the
    dispatch module global, so patching pipeline.dispatch changes the prompt
    seam. Verified by source binding: the impl looks the name up in
    dispatch's globals at call time."""
    monkeypatch.setattr(dispatch, "_revised_instructions_note", lambda *a: "X")
    assert dispatch._dispatch_story_impl.__globals__["_revised_instructions_note"](
        True, "a", "b"
    ) == "X"
