"""OA2-04: the security-reviewer pass gets the same deferral handling as the
main reviewer.

A high-risk story's security-engineer pass runs only after the main reviewer
APPROVEs. Before this change that call site had none of the deferral handling
the main reviewer call got in LD90-W5: an exception raised by the security
backend propagated into ``advance.py``'s review loop and aborted the whole
plan's review pass for the tick, and a non-rate-limited UNKNOWN verdict fell
through to the inconclusive/rework handling instead of deferring.

These tests pin the required behaviour: a rate-limited, transient, or
unexpectedly-failing security backend defers the review (no rework routing, no
crash, and no ``review_inconclusive_count`` charge), and a non-rate-limited
UNKNOWN security verdict defers too. The negative controls pin that a clean
APPROVE still proceeds exactly as today and that a REQUEST_CHANGES carrying
real findings still routes to rework.
"""
# ruff: noqa: F811 - the plan_dir/agents_dir pytest fixtures are imported from
# the shared helpers module and also used as same-named test-function
# parameters; ruff's F811 flags that standard fixture-sharing pattern as a
# "redefinition" false positive.
from pipeline import server as p
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _read_manifest,
    _write_manifest,
    agents_dir,
    plan_dir,
)


def _high_risk_story(plan_dir, **extra):
    story = {
        "summary": "Rotate the signing key",
        "status": "tests_passed",
        "worktree": str(plan_dir / "wt"),
        "risk": "high",
    }
    story.update(extra)
    return story


def _approving_main_reviewer(wt, br, **k):
    return "VERDICT: APPROVE"


def _no_pr(*a, **k):
    raise AssertionError("no PR may be opened on a deferred review")


def _install(monkeypatch, security_reviewer, open_pr=None):
    monkeypatch.setattr(p, "_run_reviewer", _approving_main_reviewer)
    monkeypatch.setattr(p, "_run_security_reviewer", security_reviewer)
    monkeypatch.setattr(p, "_open_pr", open_pr or _no_pr)
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)


def test_rate_limited_security_reviewer_defers_without_rework_or_crash(
    plan_dir, agents_dir, monkeypatch
):
    # One short of the default REVIEW_INCONCLUSIVE_MAX (2): a deferral must not
    # push the story over the edge into escalation/parking.
    _write_manifest(
        plan_dir,
        "sec_defer_429",
        {"S1": _high_risk_story(plan_dir, review_inconclusive_count=1)},
    )

    def _security(wt, br, **k):
        raise p.backend.RateLimitedError("429 too many requests")

    _install(monkeypatch, _security)

    result = p.review_story("sec_defer_429", "S1")

    assert result["deferred"] == "rate_limited"
    assert result["status"] == "tests_passed"
    story = _read_manifest(plan_dir, "sec_defer_429")["stories"]["S1"]
    assert story["status"] == "tests_passed"
    assert story.get("rework_attempts", 0) == 0
    assert story["review_inconclusive_count"] == 1
    assert not story.get("escalated")


def test_transient_security_backend_exception_defers(
    plan_dir, agents_dir, monkeypatch
):
    _write_manifest(
        plan_dir,
        "sec_defer_timeout",
        {"S1": _high_risk_story(plan_dir, review_inconclusive_count=1)},
    )

    def _security(wt, br, **k):
        raise TimeoutError("timed out")

    _install(monkeypatch, _security)

    result = p.review_story("sec_defer_timeout", "S1")

    assert result["deferred"] == "transient_backend"
    assert result["status"] == "tests_passed"
    story = _read_manifest(plan_dir, "sec_defer_timeout")["stories"]["S1"]
    assert story["status"] == "tests_passed"
    assert story.get("rework_attempts", 0) == 0
    assert story["review_inconclusive_count"] == 1


def test_unexpected_security_backend_exception_defers_without_propagation(
    plan_dir, agents_dir, monkeypatch
):
    _write_manifest(
        plan_dir,
        "sec_defer_boom",
        {"S1": _high_risk_story(plan_dir, review_inconclusive_count=1)},
    )

    def _security(wt, br, **k):
        raise RuntimeError("boom")

    _install(monkeypatch, _security)

    # Must not propagate into advance.py's review loop.
    result = p.review_story("sec_defer_boom", "S1")

    assert result["deferred"] == "backend_error"
    assert result["status"] == "tests_passed"
    story = _read_manifest(plan_dir, "sec_defer_boom")["stories"]["S1"]
    assert story["status"] == "tests_passed"
    assert story.get("rework_attempts", 0) == 0
    assert story["review_inconclusive_count"] == 1
    assert not story.get("escalated")


def test_non_rate_limited_unknown_security_verdict_defers(
    plan_dir, agents_dir, monkeypatch
):
    _write_manifest(
        plan_dir,
        "sec_defer_unknown",
        {"S1": _high_risk_story(plan_dir, review_inconclusive_count=1)},
    )

    _install(monkeypatch, lambda wt, br, **k: "no verdict here")

    result = p.review_story("sec_defer_unknown", "S1")

    assert result["deferred"] == "unknown_verdict"
    assert result.get("verdict") != "REQUEST_CHANGES"
    assert result["status"] == "tests_passed"
    story = _read_manifest(plan_dir, "sec_defer_unknown")["stories"]["S1"]
    assert story["status"] == "tests_passed"
    assert story.get("rework_attempts", 0) == 0
    assert story["review_inconclusive_count"] == 1
    assert not story.get("escalated")


def test_deferral_does_not_corrupt_counter_for_the_followup_call(
    plan_dir, agents_dir, monkeypatch
):
    # The worked example: a deferral must leave review_inconclusive_count where
    # it was, so the NEXT security review (a clean APPROVE) is not wrongly
    # escalated/parked by a counter the deferral silently bumped.
    _write_manifest(
        plan_dir,
        "sec_defer_followup",
        {"S1": _high_risk_story(plan_dir, review_inconclusive_count=1)},
    )
    state = {"fail": True}

    def _security(wt, br, **k):
        if state["fail"]:
            raise p.backend.RateLimitedError("429 too many requests")
        return "VERDICT: APPROVE"

    opened = []
    _install(
        monkeypatch,
        _security,
        open_pr=lambda wt, key, story: opened.append(key) or "https://gh/pr/1",
    )

    first = p.review_story("sec_defer_followup", "S1")
    assert first["deferred"] == "rate_limited"
    story = _read_manifest(plan_dir, "sec_defer_followup")["stories"]["S1"]
    assert story["review_inconclusive_count"] == 1

    state["fail"] = False
    second = p.review_story("sec_defer_followup", "S1")

    assert second["verdict"] == "APPROVE"
    assert second["status"] == "pr_open"
    assert opened == ["S1"]
    story = _read_manifest(plan_dir, "sec_defer_followup")["stories"]["S1"]
    assert story["status"] == "pr_open"
    assert not story.get("escalated")


def test_clean_approve_security_verdict_proceeds_as_today(
    plan_dir, agents_dir, monkeypatch
):
    _write_manifest(plan_dir, "sec_approve", {"S1": _high_risk_story(plan_dir)})

    opened = []
    _install(
        monkeypatch,
        lambda wt, br, **k: "VERDICT: APPROVE",
        open_pr=lambda wt, key, story: opened.append(key) or "https://gh/pr/1",
    )

    result = p.review_story("sec_approve", "S1")

    assert result["verdict"] == "APPROVE"
    assert result["status"] == "pr_open"
    assert opened == ["S1"]
    story = _read_manifest(plan_dir, "sec_approve")["stories"]["S1"]
    assert story["security_review_verdict"] == "APPROVE"


def test_security_request_changes_with_findings_still_routes_to_rework(
    plan_dir, agents_dir, monkeypatch
):
    _write_manifest(plan_dir, "sec_rework", {"S1": _high_risk_story(plan_dir)})

    _install(
        monkeypatch,
        lambda wt, br, **k: (
            "Blocking: hardcoded credential in config.py\n"
            "VERDICT: REQUEST_CHANGES"
        ),
    )

    result = p.review_story("sec_rework", "S1")

    assert result.get("deferred") is None
    assert result["verdict"] == "REQUEST_CHANGES"
    story = _read_manifest(plan_dir, "sec_rework")["stories"]["S1"]
    assert story["rework_attempts"] == 1
    assert story["status"] in ("changes_requested", "parked")
    assert story["security_review_verdict"] == "REQUEST_CHANGES"
