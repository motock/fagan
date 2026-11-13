"""Regression guard: the dashboard no longer parses PLAN_DIR artifacts directly.

After W3b-A2-seam rerouted every GET-handler call site to the _store/_service
seam, the on-disk-layout parse helpers in app/dashboard.py became dead code.
This story (W3b-A3) deletes those helper DEFINITIONS so the dashboard no
longer carries a dependency on the PLAN_DIR on-disk layout -- a prerequisite
for W4's move of plan storage into the database.

This test grades that the decoupling actually landed, not half-done: it FAILS
while the helpers are still defined and PASSES only once every one of them is
removed. It also pins the one intentional survivor -- PLAN_DIR stays for the
_dashboard_ui_state_path preference file -- so an over-eager deletion of the
PLAN_DIR module global (or its import) is caught too.
"""
import re

from app import dashboard


def _source() -> str:
    """Return the full source text of app/dashboard.py as a single string."""
    import inspect

    return inspect.getsource(dashboard)


# ---------------------------------------------------------------------------
# 1. The dead parse-helper DEFINITIONS must be gone.
# ---------------------------------------------------------------------------
# Each entry is (forbidden_call_text, human_label). We assert the source text
# contains no occurrence of the helper being *defined* or *called* -- after the
# deletions there should be neither a `def _foo(` line nor a stray `_foo(`
# call site left behind. The success-criteria grep in the story brief checks
# the `def` lines; we additionally forbid the call forms so a half-done edit
# (definition removed but a call left dangling) is still caught.
_FORBIDDEN_HELPERS = [
    "_manifest_path",
    "_read_manifest",
    "_list_plan_names",
    "_journal_path",
    "_read_journal",
    "_journal_final_ts",
    "_decisions_path",
    "_read_decisions",
    "_notifications_path",
    "_tail_notifications",
    "_notifications_jsonl_path",
    "_tail_notification_records",
    "_read_story_log",
    "_read_worktree_file",
]


def test_no_dead_parse_helper_definitions_remain():
    """No `def _<helper>(` line may remain in app/dashboard.py source."""
    src = _source()
    # Match a top-level (module-scope) def line for each forbidden helper.
    # `^def ` at column 0 == top-level; helpers in this module are all
    # module-level functions, so this is the precise signal the brief's grep
    # checks.
    pattern = re.compile(
        r"^def ("
        + "|".join(re.escape(h) for h in _FORBIDDEN_HELPERS)
        + r")\b",
        re.MULTILINE,
    )
    matches = pattern.findall(src)
    assert matches == [], (
        "app/dashboard.py still defines dead parse helpers: "
        + ", ".join(sorted(set(matches)))
    )


def test_no_dead_parse_helper_call_sites_remain():
    """No `_<helper>(` call site may remain either -- catches dangling refs.

    A definition can be deleted while a call is left behind (e.g. inside
    another helper or a handler). That would be a NameError at runtime, but we
    want to catch it statically here so the implementer cannot ship a
    half-finished edit that merely passes the `def`-line grep.
    """
    src = _source()
    offenders = []
    for helper in _FORBIDDEN_HELPERS:
        # `helper(` as a bare call. We use a word-boundary-ish check: the
        # helpers are all underscore-prefixed, so a preceding word char would
        # only come from a longer identifier -- guard against that with a
        # negative lookbehind for an identifier char.
        for m in re.finditer(r"(?<![A-Za-z0-9_])" + re.escape(helper) + r"\s*\(", src):
            offenders.append(helper)
            break
    assert offenders == [], (
        "app/dashboard.py still references dead parse helpers: "
        + ", ".join(sorted(set(offenders)))
    )


# ---------------------------------------------------------------------------
# 2. No direct PLAN_DIR on-disk-layout parsing patterns may remain.
# ---------------------------------------------------------------------------
# These are the concrete "the dashboard reaches into the PLAN_DIR layout"
# patterns the story brief names. Even if a helper were inlined, these
# patterns would reappear, so we forbid them directly too.
_FORBIDDEN_PATTERNS = [
    r"PLAN_DIR\.glob\s*\(",   # globbing the plan directory for manifests
    r"PLAN_DIR\s*/\s*f[\"']",  # f-string path composition off PLAN_DIR (CFG-A3 regression)
]


def test_no_plan_dir_glob_parsing_remains():
    src = _source()
    for pat in _FORBIDDEN_PATTERNS:
        assert not re.search(pat, src), (
            f"app/dashboard.py still contains forbidden PLAN_DIR parse pattern: {pat!r}"
        )


# ---------------------------------------------------------------------------
# 3. PLAN_DIR must be GONE -- the UI preference file moved behind HostState.
# ---------------------------------------------------------------------------
def test_plan_dir_module_global_is_removed():
    """PLAN_DIR is no longer a dashboard global (c3-3).

    Its only dashboard use was the UI preferences file path, which now goes
    through the host state, so the binding must not come back.
    """
    assert not hasattr(dashboard, "PLAN_DIR")


def test_dashboard_ui_state_path_helper_is_removed():
    """_dashboard_ui_state_path was dead once the UI state moved to HostState."""
    assert not hasattr(dashboard, "_dashboard_ui_state_path")


def test_plan_dir_assignment_line_is_removed():
    """No module-level ``PLAN_DIR =`` assignment may remain in the source."""
    assert not re.search(r"^PLAN_DIR\s*=", _source(), re.MULTILINE)


# ---------------------------------------------------------------------------
# 4. The four helper-pinning tests in test_dashboard.py must be removed.
# ---------------------------------------------------------------------------
# These four existing tests assert behavior of the very helpers this story
# deletes (they call d._journal_path / d._read_worktree_file). They are
# pre-authorized for deletion and the regression guard above replaces their
# intent. We assert they are gone so an implementer who deletes the helpers
# but leaves the now-broken tests behind is caught.
_REMOVED_DASHBOARD_TESTS = [
    "test_journal_path_helper_mirrors_pipeline_mcp_naming",
    "test_journal_path_helper_uses_journal_dir_from_dashboard",
    "test_read_worktree_file_rejects_filename_with_traversal",
    "test_read_worktree_file_helper_reads_named_artifact",
]


def test_helper_pinning_tests_removed_from_dashboard_suite():
    """The four helper-pinning tests must be deleted from the dashboard suite.

    test_dashboard.py (3,117 lines) was later split into test_dashboard_api.py,
    test_dashboard_frontend.py, and test_dashboard_checklist_config.py to keep
    each file under the project's line-count target - reads each split file's
    source text (not importing them, so a missing helper referenced by a
    leftover test does not blow up this guard at collection time) and asserts
    none of the four `def test_...` lines remain in any of them.
    """
    from pathlib import Path

    dashboard_test_files = [
        Path(__file__).with_name("test_dashboard_api.py"),
        Path(__file__).with_name("test_dashboard_frontend.py"),
        Path(__file__).with_name("test_dashboard_checklist_config.py"),
    ]
    src = "\n".join(f.read_text(encoding="utf-8") for f in dashboard_test_files)
    remaining = [
        name
        for name in _REMOVED_DASHBOARD_TESTS
        if re.search(r"^def " + re.escape(name) + r"\b", src, re.MULTILINE)
    ]
    assert remaining == [], (
        "the dashboard test suite still defines helper-pinning tests that "
        "should have been deleted: " + ", ".join(remaining)
    )