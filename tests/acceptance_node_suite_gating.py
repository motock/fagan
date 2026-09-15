"""Read-only acceptance oracle: the Node frontend suites actually gate.

Graded independently of the story's own test file -- it drives the
discovery and runner helpers of ``tests/unit/test_node_suites.py`` and
asserts the three properties the gate must have to be worth anything:

  1. the known-green .mjs suites are discovered (membership only, never
     an exact total, so suites added later still satisfy this),
  2. nothing in ``_KNOWN_BROKEN`` currently exits 0 -- the ratchet, kept
     honest here so a skip list nobody prunes cannot outlive the debt it
     documents (an unrunnable suite is precisely how the duplicate-reply
     regression reached master undetected),
  3. ``_run_suite`` reports failure for a suite that really fails, so the
     gate can actually go red.

The helper import happens at call time, so this fixture stays RED until
``tests/unit/test_node_suites.py`` exists with those three names -- that
is the intended TDD direction, not a broken oracle.
"""
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Suites green at plan time. Membership only -- a newly added suite is fine.
KNOWN_GREEN = (
    "tests/test_app_workspace.mjs",
    "tests/test_app_workspace_picker.mjs",
    "tests/unit/test_comms_sse_stream.mjs",
    "tests/unit/test_markdown_no_interactive_controls.mjs",
    "tests/unit/test_markdown_render.mjs",
    "tests/unit/test_patch_module.mjs",
    "tests/unit/test_patch_ui_wiring.mjs",
    "tests/unit/test_plan_list_scoping.mjs",
    "tests/unit/test_usage_backends_render.mjs",
)


def _node_suites():
    """Import the story's module at call time so collection never breaks."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from tests.unit.test_node_suites import (
        _KNOWN_BROKEN,
        _discover_suites,
        _run_suite,
    )

    return _discover_suites, _run_suite, _KNOWN_BROKEN


def _relative(suite_path):
    path = Path(suite_path)
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path.resolve().relative_to(REPO_ROOT).as_posix()


def test_known_green_suites_are_discovered_without_scratch_copies():
    _discover_suites, _run_suite, _KNOWN_BROKEN = _node_suites()
    discovered = {_relative(p) for p in _discover_suites(REPO_ROOT)}

    missing = sorted(set(KNOWN_GREEN) - discovered)
    assert not missing, f"discovered nothing for: {missing}"

    # Benchmark scratch runs leave whole duplicate trees under
    # tests/benchmark/_runs/. Excluding '_'-prefixed path components keeps
    # them (and '_'-prefixed helpers) out without pinning a file list.
    leaked = sorted(
        p for p in discovered if any(part.startswith("_") for part in Path(p).parts)
    )
    assert not leaked, f"discovery leaked non-suite paths: {leaked}"


def test_known_broken_never_claims_a_suite_that_now_passes():
    _discover_suites, _run_suite, _KNOWN_BROKEN = _node_suites()
    stale = {}
    for suite in _discover_suites(REPO_ROOT):
        rel = _relative(suite)
        if rel not in _KNOWN_BROKEN:
            continue
        if _run_suite(suite).returncode == 0:
            stale[rel] = "exits 0 - remove it from _KNOWN_BROKEN"
    assert not stale, f"stale skip list entries: {stale}"


def test_runner_goes_red_for_a_genuinely_failing_suite(tmp_path):
    _discover_suites, _run_suite, _KNOWN_BROKEN = _node_suites()
    assert shutil.which("node"), "node is required for the frontend gate"

    failing = tmp_path / "test_deliberately_failing.mjs"
    failing.write_text("process.exit(1);\n", encoding="utf-8")
    assert _run_suite(failing).returncode != 0, "a failing suite must report failure"

    passing = tmp_path / "test_deliberately_passing.mjs"
    passing.write_text("process.exit(0);\n", encoding="utf-8")
    assert _run_suite(passing).returncode == 0, "a passing suite must report success"
