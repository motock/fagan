"""Tests for pipeline.triage.collect_triage_evidence.

This module is the failure-triage layer. Its contract (C2 of
docs/plans/OVERLORD_FAILURE_TRIAGE_PLAN.md): EVERY function fails open, and
today's park-and-notify is the invariant default on every error path. The
discipline is modelled on pipeline.rebrief.diagnose_failure, which returns
None on every error path by explicit design.

collect_triage_evidence builds a bounded prompt string from four sections
(a)-(d) and must NEVER raise - a missing worktree, an unreadable worktree, or
an empty story dict all yield a valid (possibly minimal) string.

These tests are written FIRST (TDD). They import pipeline.triage, which does
not exist yet, so they currently fail with ImportError - the correct RED
state. A later dispatch implements pipeline/triage.py against them.
"""
from pipeline import triage

# ---------------------------------------------------------------------------
# Section (a): TRIAGE QUESTION header
# ---------------------------------------------------------------------------

def test_empty_story_yields_triage_question_and_story_state():
    """An empty story dict must not raise KeyError; it yields a non-empty
    string containing both the TRIAGE QUESTION and STORY STATE sections."""
    result = triage.collect_triage_evidence("/nonexistent/worktree", {})
    assert isinstance(result, str)
    assert result  # non-empty
    assert "TRIAGE QUESTION" in result
    assert "STORY STATE:" in result


def test_triage_question_contains_status():
    """Section (a) must use story.get('status', '?') in its question line."""
    result = triage.collect_triage_evidence("/nonexistent/worktree", {"status": "parked"})
    assert "TRIAGE QUESTION" in result
    assert "parked" in result


def test_triage_question_status_defaults_to_question_mark_when_absent():
    result = triage.collect_triage_evidence("/nonexistent/worktree", {})
    # The question line references status=? when status is absent.
    assert "status=?" in result


# ---------------------------------------------------------------------------
# Section (b): STORY STATE block, ordered keys, absent -> '-'
# ---------------------------------------------------------------------------

_STORY_KEYS = [
    "status",
    "parked_reason",
    "backend",
    "model",
    "dispatched_model",
    "escalated",
    "risk",
    "persona",
    "dispatch_attempts",
    "rework_attempts",
    "review_inconclusive_count",
    "step_cap_streak",
    "merge_attempts",
    "triage_attempts",
    "triage_actions",
]


def test_parked_story_renders_status_and_parked_reason():
    story = {"status": "parked", "parked_reason": "rework budget exhausted"}
    result = triage.collect_triage_evidence("/nonexistent/worktree", story)
    assert "parked" in result
    assert "rework budget exhausted" in result


def test_absent_keys_render_as_dash():
    """A specific absent key must render as '-'. Assert on a concrete line."""
    result = triage.collect_triage_evidence("/nonexistent/worktree", {})
    lines = result.splitlines()
    # Find the merge_attempts line and assert it renders the dash.
    merge_lines = [ln for ln in lines if ln.startswith("merge_attempts:")]
    assert merge_lines, "expected a 'merge_attempts:' line in STORY STATE"
    assert merge_lines[0] == "merge_attempts: -"


def test_story_state_keys_in_exact_order():
    """The STORY STATE block must list keys in the exact prescribed order."""
    # Build a story where every key has a distinct, recognizable value.
    story = {k: f"VAL_{k}" for k in _STORY_KEYS}
    result = triage.collect_triage_evidence("/nonexistent/worktree", story)
    lines = result.splitlines()
    # Collect the ordered list of "<key>:" lines that appear.
    seen_keys = [
        ln.split(":", 1)[0]
        for ln in lines
        if ":" in ln and ln.split(":", 1)[0] in _STORY_KEYS
    ]
    assert seen_keys == _STORY_KEYS, (
        f"STORY STATE keys out of order or missing: {seen_keys}"
    )


def test_story_state_present_value_rendered():
    story = {"status": "parked", "merge_attempts": 3}
    result = triage.collect_triage_evidence("/nonexistent/worktree", story)
    lines = result.splitlines()
    merge_lines = [ln for ln in lines if ln.startswith("merge_attempts:")]
    assert merge_lines == ["merge_attempts: 3"]


def test_story_state_never_raises_on_sparse_dict():
    """A dict with only some keys must not raise KeyError for the others."""
    story = {"status": "parked", "risk": "high"}
    # Should not raise.
    result = triage.collect_triage_evidence("/nonexistent/worktree", story)
    assert "STORY STATE:" in result
    assert "status: parked" in result
    assert "risk: high" in result


# ---------------------------------------------------------------------------
# Section (c): findings via format_findings
# ---------------------------------------------------------------------------

def test_findings_none_omits_repo_health_section(monkeypatch):
    """findings=None -> the 'REPO-HEALTH FINDINGS' heading must NOT appear."""
    monkeypatch.setattr(triage, "format_findings", lambda f: "")
    result = triage.collect_triage_evidence("/nonexistent/worktree", {}, findings=None)
    assert "REPO-HEALTH FINDINGS" not in result


def test_findings_empty_omits_repo_health_section(monkeypatch):
    """findings=[] (empty/falsy) -> section omitted entirely."""
    monkeypatch.setattr(triage, "format_findings", lambda f: "")
    result = triage.collect_triage_evidence("/nonexistent/worktree", {}, findings=[])
    assert "REPO-HEALTH FINDINGS" not in result


def test_findings_truthy_includes_format_findings_output(monkeypatch):
    """When findings is truthy, format_findings output is included."""
    monkeypatch.setattr(
        triage, "format_findings", lambda f: "REPO-HEALTH FINDINGS:\n- marker"
    )
    result = triage.collect_triage_evidence(
        "/nonexistent/worktree", {}, findings=[{"kind": "x"}]
    )
    assert "REPO-HEALTH FINDINGS" in result
    assert "marker" in result


def test_findings_with_real_format_findings_renders_kind():
    """Using the real format_findings, a finding's kind appears in output."""
    findings = [{"kind": "lint_baseline_red", "detail": "E501"}]
    result = triage.collect_triage_evidence("/nonexistent/worktree", {}, findings=findings)
    assert "lint_baseline_red" in result
    assert "E501" in result


# ---------------------------------------------------------------------------
# Section (d): collect_failure_evidence, wrapped in try/except
# ---------------------------------------------------------------------------

def test_collect_failure_evidence_huge_output_trimmed_to_limit(monkeypatch):
    """collect_failure_evidence returning 100000 chars must be trimmed so the
    whole result is <= limit, and STORY STATE must still be present."""
    monkeypatch.setattr(triage, "format_findings", lambda f: "")
    monkeypatch.setattr(
        triage, "collect_failure_evidence", lambda w, s, limit=6000: "X" * 100000
    )
    result = triage.collect_triage_evidence(
        "/nonexistent/worktree", {"status": "parked"}, limit=8000
    )
    assert len(result) <= 8000
    assert "STORY STATE:" in result


def test_collect_failure_evidence_raising_does_not_propagate(monkeypatch):
    """If collect_failure_evidence raises, sections (a)-(c) still appear and
    nothing propagates - fail open."""
    monkeypatch.setattr(triage, "format_findings", lambda f: "")

    def _boom(worktree, story, limit=6000):
        raise RuntimeError("evidence blew up")

    monkeypatch.setattr(triage, "collect_failure_evidence", _boom)
    result = triage.collect_triage_evidence(
        "/nonexistent/worktree", {"status": "parked"}
    )
    assert "STORY STATE:" in result
    assert "TRIAGE QUESTION" in result


def test_collect_failure_evidence_called_with_remaining_budget(monkeypatch):
    """collect_failure_evidence must be called with a `limit` keyword that is
    the budget remaining after sections (a)-(c)."""
    monkeypatch.setattr(triage, "format_findings", lambda f: "")
    captured = {}

    def _spy(worktree, story, limit=6000):
        captured["limit"] = limit
        return "EVIDENCE"

    monkeypatch.setattr(triage, "collect_failure_evidence", _spy)
    triage.collect_triage_evidence(
        "/nonexistent/worktree", {"status": "parked"}, limit=8000
    )
    assert "limit" in captured
    # The remaining budget must be positive and not exceed the overall limit.
    assert 0 < captured["limit"] <= 8000


# ---------------------------------------------------------------------------
# Never-raises invariants
# ---------------------------------------------------------------------------

# Add new test after this function

def test_limit_respected_when_failure_evidence_empty_and_fixed_over_budget(monkeypatch):
    """When failure_evidence is empty and fixed_text alone exceeds limit,
    the result must be trimmed to the limit.
    """
    limit = 8000
    # Make fixed_text > limit by having format_findings produce a large section.
    monkeypatch.setattr(triage, "format_findings", lambda f: "F" * 8600)
    # Ensure collect_failure_evidence returns empty string.
    monkeypatch.setattr(triage, "collect_failure_evidence", lambda w, s, limit=6000: "")
    result = triage.collect_triage_evidence(
        "/nonexistent/worktree", {"status": "parked"}, limit=limit
    )
    assert len(result) <= limit


# ---------------------------------------------------------------------------
# Never-raises invariants
# ---------------------------------------------------------------------------

def test_missing_worktree_does_not_raise():
    """A missing worktree path must yield a valid string, not raise."""
    result = triage.collect_triage_evidence(
        "/definitely/does/not/exist/anywhere", {"status": "parked"}
    )
    assert isinstance(result, str)
    assert "TRIAGE QUESTION" in result


def test_none_worktree_does_not_raise():
    """A None worktree must not raise."""
    result = triage.collect_triage_evidence(None, {"status": "parked"})
    assert isinstance(result, str)
    assert "TRIAGE QUESTION" in result


def test_empty_story_does_not_raise():
    result = triage.collect_triage_evidence("/nonexistent/worktree", {})
    assert isinstance(result, str)
    assert "TRIAGE QUESTION" in result


def test_result_respects_limit_default(monkeypatch):
    """Default limit is 8000; result must never exceed it."""
    monkeypatch.setattr(triage, "format_findings", lambda f: "")
    monkeypatch.setattr(
        triage, "collect_failure_evidence", lambda w, s, limit=6000: "Y" * 50000
    )
    result = triage.collect_triage_evidence("/nonexistent/worktree", {"status": "parked"})
    assert len(result) <= 8000

    """Default limit is 8000; result must never exceed it."""
    monkeypatch.setattr(triage, "format_findings", lambda f: "")
    monkeypatch.setattr(
        triage, "collect_failure_evidence", lambda w, s, limit=6000: "Y" * 50000
    )
    result = triage.collect_triage_evidence("/nonexistent/worktree", {"status": "parked"})
    assert len(result) <= 8000


def test_result_respects_limit_when_failure_evidence_empty_and_fixed_over_budget(monkeypatch):
    """Regression: when ``failure_evidence`` is empty/falsy AND ``fixed_text``
    alone already exceeds ``limit``, the result must still be trimmed to
    ``limit``.

    This boundary is the one ``test_result_respects_limit_default`` and
    ``test_result_respects_custom_limit`` miss: both monkeypatch
    ``collect_failure_evidence`` to return a *truthy* huge string, so they only
    exercise the ``if failure_evidence:`` trim path. Here we make the fixed
    sections (TRIAGE QUESTION + STORY STATE + findings) large enough to exceed
    ``limit`` on their own, and have ``collect_failure_evidence`` return ``""``
    (which is exactly what happens in practice when
    ``remaining = max(0, limit - fixed_len)`` computes to ``0`` and the call is
    made with ``limit=0``). With the documented ``limit`` maximum, the returned
    string must never exceed ``limit``.
    """
    limit = 500
    # Make the findings section large enough that fixed_text alone exceeds
    # `limit` even before any failure evidence is appended.
    monkeypatch.setattr(triage, "format_findings", lambda f: "F" * 600)
    # collect_failure_evidence returns empty (the real behaviour when the
    # remaining budget is 0).
    monkeypatch.setattr(
        triage, "collect_failure_evidence", lambda w, s, limit=6000: ""
    )
    result = triage.collect_triage_evidence(
        "/nonexistent/worktree", {"status": "parked"},
        findings=[{"kind": "x"}],
        limit=limit,
    )
    assert len(result) <= limit, (
        f"result length {len(result)} exceeds limit {limit} when "
        f"failure_evidence is empty and fixed_text alone is over budget"
    )

    monkeypatch.setattr(triage, "format_findings", lambda f: "")
    monkeypatch.setattr(
        triage, "collect_failure_evidence", lambda w, s, limit=6000: "Z" * 50000
    )
    result = triage.collect_triage_evidence(
        "/nonexistent/worktree", {"status": "parked"}, limit=500
    )
    assert len(result) <= 500
    assert "STORY STATE:" in result




# ---------------------------------------------------------------------------
# Module-level contract: imports and __all__
# ---------------------------------------------------------------------------

def test_module_imports_collect_failure_evidence_from_rebrief():
    """The module must import collect_failure_evidence from .rebrief at module
    level (so it is patchable as triage.collect_failure_evidence)."""
    assert hasattr(triage, "collect_failure_evidence")


def test_module_imports_format_findings_from_repo_health():
    """The module must import format_findings from .repo_health at module
    level (so it is patchable as triage.format_findings)."""
    assert hasattr(triage, "format_findings")


def test_module_all_exports_collect_triage_evidence():
    assert hasattr(triage, "__all__")
    assert "collect_triage_evidence" in triage.__all__


def test_module_all_exports_public_api():
    """__all__ must export the public API surface: collect_triage_evidence
    and the _current_suite_state helper (codebase convention for
    underscore-prefixed helpers that tests reference directly)."""
    assert triage.__all__ == ["_current_suite_state", "collect_triage_evidence"]


def test_module_docstring_states_fail_open_contract():
    """The module docstring must state the fail-open / park-and-notify
    invariant contract for this module."""
    doc = triage.__doc__ or ""
    assert doc, "module must have a docstring"
    low = doc.lower()
    assert "fail open" in low or "fails open" in low
    assert "park" in low  # park-and-notify invariant


def test_module_does_not_import_server_at_module_level():
    """CIRCULAR-IMPORT RULE: pipeline.triage must never import
    pipeline.server at module level (server imports triage at module level)."""
    # The server module should not appear as a loaded submodule of pipeline
    # solely because we imported pipeline.triage. We check that 'pipeline'
    # does not gain a 'server' attribute from importing triage by inspecting
    # the module's own namespace for a server binding.
    src = open(triage.__file__).read()  # noqa: SIM115
    # No top-level (module-level) import of .server / pipeline.server.
    # A lazy import inside a function body is allowed and is indented.
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "import" in stripped and "server" in stripped:
            # Must be indented (i.e. inside a function body, a lazy import).
            assert line.startswith((" ", "\t")), (
                f"module-level server import forbidden (circular): {line!r}"
            )


# ---------------------------------------------------------------------------
# Section (b2): current suite state wiring into collect_triage_evidence
#
# These verify the WIRING of _current_suite_state into collect_triage_evidence
# actually works end to end. A passing unit test of _current_suite_state alone
# would not catch a wiring bug where it is implemented but never called. They
# monkeypatch pipeline.triage._current_suite_state so no real subprocess runs.
# ---------------------------------------------------------------------------

_PASS_STRING = (
    "CURRENT STATE: full test suite PASSES at the worktree's current HEAD."
)


def test_current_suite_state_pass_string_appears_after_story_state(monkeypatch):
    """When _current_suite_state returns the pass string, that exact string
    must appear in collect_triage_evidence's result, positioned AFTER the
    'STORY STATE:' section."""
    monkeypatch.setattr(triage, "_current_suite_state", lambda w: _PASS_STRING)
    monkeypatch.setattr(triage, "collect_failure_evidence", lambda w, s, limit=6000: "")
    result = triage.collect_triage_evidence("/nonexistent/worktree", {"status": "parked"})
    assert _PASS_STRING in result
    assert "STORY STATE:" in result
    assert result.index("STORY STATE:") < result.index(_PASS_STRING), (
        "current suite state section must appear AFTER the STORY STATE section"
    )


def test_current_suite_state_empty_omits_current_state_section(monkeypatch):
    """When _current_suite_state returns '', 'CURRENT STATE' must NOT appear
    anywhere in the result."""
    monkeypatch.setattr(triage, "_current_suite_state", lambda w: "")
    monkeypatch.setattr(triage, "collect_failure_evidence", lambda w, s, limit=6000: "")
    result = triage.collect_triage_evidence("/nonexistent/worktree", {"status": "parked"})
    assert "CURRENT STATE" not in result


def test_current_suite_state_raising_does_not_propagate(monkeypatch):
    """If _current_suite_state raises, collect_triage_evidence's own
    try/except around the call must catch it (defense in depth, matching how
    format_findings is double-wrapped just below). The result still contains
    'STORY STATE:' and nothing propagates."""
    def _boom(w):
        raise RuntimeError("boom from _current_suite_state")

    monkeypatch.setattr(triage, "_current_suite_state", _boom)
    monkeypatch.setattr(triage, "collect_failure_evidence", lambda w, s, limit=6000: "")
    result = triage.collect_triage_evidence("/nonexistent/worktree", {"status": "parked"})
    assert "STORY STATE:" in result
    assert "CURRENT STATE" not in result


def test_current_suite_state_participates_in_trim_budget(monkeypatch):
    """With a non-empty _current_suite_state return AND collect_failure_evidence
    stubbed to return 100000 characters, len(result) <= limit must still hold.
    This exercises the pre-existing trim path with the new fixed section
    present (current_state_section participates in fixed_parts/fixed_len
    accounting)."""
    monkeypatch.setattr(triage, "_current_suite_state", lambda w: _PASS_STRING)
    monkeypatch.setattr(
        triage, "collect_failure_evidence", lambda w, s, limit=6000: "X" * 100000
    )
    limit = 8000
    result = triage.collect_triage_evidence(
        "/nonexistent/worktree", {"status": "parked"}, limit=limit
    )
    assert len(result) <= limit, (
        f"result length {len(result)} exceeds limit {limit} with the new "
        f"current_state_section present"
    )
    # The fixed sections (including current_state) must survive the trim.
    assert "STORY STATE:" in result
    assert _PASS_STRING in result
