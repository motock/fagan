"""Acceptance oracle: acceptance fixtures depending on macOS-only binaries must
be flagged at ingest, because dispatch/grading run on macOS while CI runs
ubuntu-latest only - so such a fixture passes every local gate and fails only
after the PR is open.
"""
import inspect

import pipeline.server as srv
from pipeline.build_detect import _platform_locked_fixture_warning


def _story(source):
    return {
        "summary": "s",
        "agent_instructions": "do the thing",
        "acceptance": [{"path": "tests/unit/test_x.py", "source": source}],
    }


def test_flags_a_fixture_shelling_out_to_plutil():
    msg = _platform_locked_fixture_warning(
        _story('subprocess.run(["plutil", "-convert", "xml1", p])')
    )
    assert msg is not None
    assert "plutil" in msg


def test_flags_other_macos_only_binaries():
    for tool in ("sw_vers", "osascript"):
        msg = _platform_locked_fixture_warning(_story(f'subprocess.run(["{tool}"])'))
        assert msg is not None, tool
        assert tool in msg


def test_flags_a_hardcoded_macos_system_path():
    msg = _platform_locked_fixture_warning(
        _story('open("/System/Library/LaunchDaemons/x.plist")')
    )
    assert msg is not None


def test_portable_fixture_is_not_flagged():
    assert _platform_locked_fixture_warning(
        _story("import plistlib\nplistlib.loads(b'')")
    ) is None


def test_story_without_acceptance_is_not_flagged():
    assert _platform_locked_fixture_warning({"summary": "s"}) is None


def test_the_lint_is_wired_into_ingest_plan():
    # ingest_plan is a thin @mcp.tool() delegate onto _ingest_plan_impl (the
    # PipelineService W1a extraction); the lint calls live in the impl.
    src = inspect.getsource(srv._ingest_plan_impl)
    assert "_platform_locked_fixture_warning" in src, (
        "ingest_plan must call the lint; a lint nothing calls flags nothing"
    )
    assert "_isolation_only_acceptance_warning" in src, (
        "the existing isolation-only warning must keep running alongside it"
    )
