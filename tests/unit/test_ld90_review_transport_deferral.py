"""LD90-W5-02: a reviewer transport failure defers the review like a rate limit.

A transient backend failure that survives the single inline retry, or a
transport exception raised by the reviewer backend, is infrastructure - not a
review outcome. It must increment ``review_deferred_count`` and leave
``review_inconclusive_count`` untouched, exactly like the rate-limit path.
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


def _story(plan_dir):
    return {
        "summary": "Add thing",
        "status": "tests_passed",
        "worktree": str(plan_dir / "wt"),
        "risk": "low",
    }


def _no_pr(*a, **k):
    raise AssertionError("no PR on a deferred review")


def test_transient_502_on_both_calls_defers_without_burning_inconclusive(
    plan_dir, agents_dir, monkeypatch
):
    _write_manifest(plan_dir, "defer_502", {"S1": _story(plan_dir)})

    call_count = {"n": 0}

    def _reviewer(wt, br, backend_name=None, **k):
        call_count["n"] += 1
        return "HTTP 502 Bad Gateway"

    monkeypatch.setattr(p, "_run_reviewer", _reviewer)
    monkeypatch.setattr(p, "_open_pr", _no_pr)
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)

    result = p.review_story("defer_502", "S1")

    assert call_count["n"] == 2  # original + exactly one inline retry
    assert result["deferred"] == "transient_backend"
    assert result["status"] == "tests_passed"
    story = _read_manifest(plan_dir, "defer_502")["stories"]["S1"]
    assert "review_inconclusive_count" not in story
    assert story["review_deferred_count"] == 1
    assert story["status"] == "tests_passed"


def test_transient_503_at_inconclusive_boundary_does_not_park_or_escalate(
    plan_dir, agents_dir, monkeypatch
):
    # One short of the default REVIEW_INCONCLUSIVE_MAX (2): a deferral must not
    # push the story over the edge into escalation/parking.
    story = _story(plan_dir)
    story["review_inconclusive_count"] = 1
    _write_manifest(plan_dir, "defer_boundary", {"S1": story})

    monkeypatch.setattr(p, "_run_reviewer", lambda wt, br, **k: "503 Service Unavailable")
    monkeypatch.setattr(p, "_open_pr", _no_pr)
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)

    result = p.review_story("defer_boundary", "S1")

    assert result["deferred"] == "transient_backend"
    story = _read_manifest(plan_dir, "defer_boundary")["stories"]["S1"]
    assert story["review_inconclusive_count"] == 1
    assert story["review_deferred_count"] == 1
    assert not story.get("escalated")
    assert story["status"] == "tests_passed"


def test_transient_then_non_transient_unknown_still_charges_inconclusive(
    plan_dir, agents_dir, monkeypatch
):
    # The retry is not itself a transient failure, so this is a genuine
    # inconclusive review and must take the existing inconclusive path.
    _write_manifest(plan_dir, "defer_mixed", {"S1": _story(plan_dir)})

    call_count = {"n": 0}

    def _reviewer(wt, br, backend_name=None, **k):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return "HTTP 502 Bad Gateway"
        return "no verdict here"

    monkeypatch.setattr(p, "_run_reviewer", _reviewer)
    monkeypatch.setattr(p, "_open_pr", _no_pr)
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)

    result = p.review_story("defer_mixed", "S1")

    assert call_count["n"] == 2
    assert result.get("deferred") is None
    story = _read_manifest(plan_dir, "defer_mixed")["stories"]["S1"]
    assert story["review_inconclusive_count"] == 1
    assert story.get("review_deferred_count", 0) == 0


def test_transport_exception_defers_and_never_leaks_exception_text(
    plan_dir, agents_dir, monkeypatch
):
    _write_manifest(plan_dir, "defer_timeout", {"S1": _story(plan_dir)})

    call_count = {"n": 0}

    def _reviewer(wt, br, backend_name=None, **k):
        call_count["n"] += 1
        raise TimeoutError("timed out")

    notes = []
    monkeypatch.setattr(p, "_run_reviewer", _reviewer)
    monkeypatch.setattr(p, "_open_pr", _no_pr)
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: notes.append(a[1] if len(a) > 1 else ""))

    result = p.review_story("defer_timeout", "S1")

    assert call_count["n"] == 1  # the exception path has no inline retry
    assert result["deferred"] == "transient_backend"
    assert result["status"] == "tests_passed"
    story = _read_manifest(plan_dir, "defer_timeout")["stories"]["S1"]
    assert "review_inconclusive_count" not in story
    assert story["review_deferred_count"] == 1
    assert any("TimeoutError" in n for n in notes)
    assert not any("timed out" in n for n in notes)


def test_chained_transport_exception_defers_without_leaking_text(
    plan_dir, agents_dir, monkeypatch
):
    class ReadTimeout(Exception):
        pass

    _write_manifest(plan_dir, "defer_chained", {"S1": _story(plan_dir)})

    def _reviewer(wt, br, backend_name=None, **k):
        raise RuntimeError(
            "backend errored after 3 attempt(s) in 180.0s"
        ) from ReadTimeout("read")

    notes = []
    monkeypatch.setattr(p, "_run_reviewer", _reviewer)
    monkeypatch.setattr(p, "_open_pr", _no_pr)
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: notes.append(a[1] if len(a) > 1 else ""))

    result = p.review_story("defer_chained", "S1")

    assert result["deferred"] == "transient_backend"
    story = _read_manifest(plan_dir, "defer_chained")["stories"]["S1"]
    assert "review_inconclusive_count" not in story
    assert story["review_deferred_count"] == 1
    assert not any("180.0s" in n for n in notes)


def test_non_transport_exception_still_falls_to_inconclusive(
    plan_dir, agents_dir, monkeypatch
):
    class OddBackendError(Exception):
        pass

    _write_manifest(plan_dir, "defer_odd", {"S1": _story(plan_dir)})

    def _reviewer(wt, br, backend_name=None, **k):
        raise OddBackendError("tool call shape")

    monkeypatch.setattr(p, "_run_reviewer", _reviewer)
    monkeypatch.setattr(p, "_open_pr", _no_pr)
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)

    result = p.review_story("defer_odd", "S1")

    assert result.get("deferred") is None
    story = _read_manifest(plan_dir, "defer_odd")["stories"]["S1"]
    assert story["review_inconclusive_count"] == 1
    assert story.get("review_deferred_count", 0) == 0


def test_approving_review_after_deferral_resets_deferred_count(
    plan_dir, agents_dir, monkeypatch
):
    _write_manifest(plan_dir, "defer_then_approve", {"S1": _story(plan_dir)})

    call_count = {"n": 0}

    def _reviewer(wt, br, backend_name=None, **k):
        call_count["n"] += 1
        if call_count["n"] <= 2:
            return "HTTP 502 Bad Gateway"
        return "Looks good.\nVERDICT: APPROVE"

    monkeypatch.setattr(p, "_run_reviewer", _reviewer)
    monkeypatch.setattr(p, "_open_pr", lambda *a, **k: "https://pr/1")
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)

    first = p.review_story("defer_then_approve", "S1")
    assert first["deferred"] == "transient_backend"
    story = _read_manifest(plan_dir, "defer_then_approve")["stories"]["S1"]
    assert story["review_deferred_count"] == 1

    second = p.review_story("defer_then_approve", "S1")

    assert second["verdict"] == "APPROVE"
    story = _read_manifest(plan_dir, "defer_then_approve")["stories"]["S1"]
    assert story["review_deferred_count"] == 0
    # No phantom inconclusive charge survives the deferral: the approving
    # review leaves the counter at zero.
    assert story.get("review_inconclusive_count", 0) == 0
