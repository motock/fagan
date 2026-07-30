"""Tests for pipeline.build_detect._isolation_only_acceptance_warning.

Root-cailed live 2026-07-28 on the harness-targeted-done-nudge story: the
acceptance fixture tested `_no_tool_nudge` in isolation while the brief told
the agent to *wire it at the call site* (pass content at line 1550). That
created a graded path that bypassed the integration wiring, so a weak local
executor skipped the ungraded step, passed the oracle, and shipped dead code.
The done-bar (full suite) didn't catch it either — it doesn't grade the call
site. The lesson: an acceptance fixture should grade the *integration*, not
just the unit.

_isolation_only_acceptance_warning is a non-blocking heuristic that flags
this shape at ingest (advisory only — never blocks). These tests pin its
behavior: warn when instructions require wiring but the fixture carries no
integration-grading evidence; stay silent otherwise.
"""
import pipeline.build_detect as bd


def _story(instructions: str, source: str) -> dict:
    return {
        "summary": "wire the nudge at the call site",
        "agent_instructions": instructions,
        "acceptance": [{"path": "tests/test_nudge.py", "source": source}],
    }


# The exact shape that escaped the gate live: instructions name the call-site
# wiring, but the fixture only calls the unit function directly.
_ISOLATION_SOURCE = (
    "from nudge import _no_tool_nudge\n"
    "def test_generic():\n"
    "    assert _no_tool_nudge(0) == 'Call a tool now.'\n"
)

_WIRING_INSTRUCTIONS = (
    "Wire _no_tool_nudge to receive the turn content at the call site "
    "(local_agent.py:1550) so the nudge can be targeted. Update the call "
    "site to pass the last user message."
)


def test_warns_when_instructions_require_call_site_but_fixture_is_isolation_only():
    msg = bd._isolation_only_acceptance_warning(_story(_WIRING_INSTRUCTIONS, _ISOLATION_SOURCE))
    assert msg is not None
    assert "isolation-only" in msg
    # The warning must name the story so an operator can find it.
    assert "wire the nudge at the call site" in msg


def test_returns_none_when_instructions_have_no_wiring_signal():
    instructions = "Add a `_no_tool_nudge` helper that returns a generic nudge string."
    assert bd._isolation_only_acceptance_warning(_story(instructions, _ISOLATION_SOURCE)) is None


def test_returns_none_when_story_has_no_acceptance():
    story = {"summary": "x", "agent_instructions": _WIRING_INSTRUCTIONS, "acceptance": []}
    assert bd._isolation_only_acceptance_warning(story) is None


def test_returns_none_when_no_instructions():
    story = {"summary": "x", "agent_instructions": "", "acceptance": [{"path": "p", "source": "x"}]}
    assert bd._isolation_only_acceptance_warning(story) is None


def test_suppressed_when_fixture_drives_real_loop_with_main():
    # Integration-graded fixture: drives the real agent loop via main().
    source = (
        "def test_done_prose_directs_targeted_nudge(monkeypatch, capsys):\n"
        "    monkeypatch.setattr(la, 'chat', _done_chat)\n"
        "    rc = la.main()\n"
        "    assert 'done' in seen[1][-1]['content']\n"
    )
    assert bd._isolation_only_acceptance_warning(_story(_WIRING_INSTRUCTIONS, source)) is None


def test_suppressed_when_fixture_uses_mock_assert_called():
    source = (
        "def test_call_site_passes_content(mocker):\n"
        "    mocker.patch('nudge._no_tool_nudge')\n"
        "    run_loop()\n"
        "    assert _no_tool_nudge.assert_called\n"
    )
    assert bd._isolation_only_acceptance_warning(_story(_WIRING_INSTRUCTIONS, source)) is None


def test_suppressed_when_fixture_reads_source_to_assert_wiring():
    # A fixture that reads the production source to assert the call site is
    # wired is integration-grading evidence.
    source = (
        "def test_call_site_wired():\n"
        "    src = Path('local_agent.py').read_text()\n"
        "    assert '_no_tool_nudge(' in src\n"
    )
    assert bd._isolation_only_acceptance_warning(_story(_WIRING_INSTRUCTIONS, source)) is None


def test_warns_for_wire_keyword_not_just_call_site():
    instructions = "Wire the helper into the dispatch prompt."
    assert bd._isolation_only_acceptance_warning(_story(instructions, _ISOLATION_SOURCE)) is not None


def test_warns_for_registration_path_instructions():
    instructions = (
        "Move the @mcp.tool() decorator so the registration path itself is "
        "exercised, not just the bare attribute."
    )
    assert bd._isolation_only_acceptance_warning(_story(instructions, _ISOLATION_SOURCE)) is not None


def test_no_warning_when_wiring_signal_but_fixture_imports_and_invokes_real_module():
    # exec_module / importlib loading the real module counts as integration
    # evidence.
    source = (
        "import importlib\n"
        "def test_real():\n"
        "    la = importlib.import_module('local_agent')\n"
        "    assert la._no_tool_nudge(0)\n"
    )
    assert bd._isolation_only_acceptance_warning(_story(_WIRING_INSTRUCTIONS, source)) is None