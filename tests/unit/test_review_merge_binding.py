"""A merge must be covered by the approved review of the commit being merged.

``review_verdict == "APPROVE"`` alone says nothing about WHICH commit was
graded: a commit pushed after the review (agent rework, a human push) merged
on the earlier approval. ``pipeline.review_verdict_binding.bind_verdict``
records the reviewed HEAD as ``review_verdict_sha``; this module is the
consumer that finally enforces it at merge time.

The gate is deliberately narrow and deny-by-default, and it is bound to the
story-branch tip captured BEFORE the merge-time rebase. The pipeline's own
post-review commits (the ``wip(merge-gate): pre-rebase-checkpoint`` commit and
the merge-gate rebase/force-push) are expected: they are handled by
re-binding the verdict at that push, never by loosening this check, and the
check always runs first so a human push is refused before any re-bind could
bless it.
"""

import json
from pathlib import Path

import pytest

import pipeline.server  # noqa: F401 - pre-existing import cycle, import first
from pipeline.review_merge_binding import check

PLAN = "go"
KEY = "P1"
REVIEWED = "abc123"
REVIEWED_FULL = "abc123def4567890abc123def4567890abc12345"


# ---------------------------------------------------------------------------
# check() - pure, deny by default.
# ---------------------------------------------------------------------------
def _approved(**overrides):
    story = {"review_verdict": "APPROVE", "review_verdict_sha": REVIEWED}
    story.update(overrides)
    return story


@pytest.mark.parametrize(
    ("story", "head", "ok"),
    [
        (_approved(), REVIEWED, True),
        (_approved(review_verdict_sha=REVIEWED_FULL), REVIEWED_FULL, True),
        (_approved(), "deadbeef", False),
        (_approved(review_verdict_sha=""), REVIEWED, False),
        (_approved(review_verdict_sha=None), REVIEWED, False),
        (_approved(review_verdict_sha=12345), "12345", False),
        (_approved(review_verdict_sha=["abc123"]), REVIEWED, False),
        ({"review_verdict": "APPROVE"}, REVIEWED, False),
        (_approved(review_verdict="REJECT"), REVIEWED, False),
        (_approved(review_verdict=""), REVIEWED, False),
        (_approved(review_verdict=None), REVIEWED, False),
        (_approved(), None, False),
        (_approved(), "", False),
        (_approved(), 12345, False),
        ({}, REVIEWED, False),
        (None, REVIEWED, False),
        ("not-a-story", REVIEWED, False),
        (_approved(), REVIEWED.upper(), False),
        (_approved(), REVIEWED[:-1], False),
        (_approved(), f" {REVIEWED}", False),
    ],
)
def test_check_allows_only_a_bound_approval_of_this_exact_head(story, head, ok):
    assert check(story, head)["ok"] is ok


@pytest.mark.parametrize(
    ("story", "head", "fragments"),
    [
        (_approved(), "deadbeefdeadbeef", ("abc123", "deadbeefdead")),
        (
            _approved(review_verdict_sha="deadbeefdeadbeef"),
            "cafebabecafebabe",
            ("deadbeefdead", "cafebabecafe"),
        ),
        (_approved(), None, ("abc123", "unavailable")),
        (_approved(), "", ("abc123", "unavailable")),
        (_approved(review_verdict_sha=None), REVIEWED, ("None", REVIEWED)),
        (_approved(review_verdict_sha=""), REVIEWED, (REVIEWED,)),
        (_approved(review_verdict_sha=12345), "12345", ("12345",)),
        ({"review_verdict": "APPROVE"}, REVIEWED, (REVIEWED, "not bound")),
        (_approved(review_verdict="REJECT"), REVIEWED, ("'REJECT'",)),
    ],
)
def test_denial_names_the_shas_and_points_at_review_story(story, head, fragments):
    reason = check(story, head)["reason"]
    assert "review_story" in reason
    assert all(f in reason for f in fragments)


def test_check_is_pure_and_never_mutates_the_story():
    story = {"review_verdict": "APPROVE", "review_verdict_sha": REVIEWED}
    before = json.dumps(story, sort_keys=True)
    # A denied call must not clear/corrupt the binding for the next caller.
    assert check(story, None)["ok"] is False
    assert json.dumps(story, sort_keys=True) == before
    assert check(story, REVIEWED)["ok"] is True


@pytest.mark.parametrize("head", [None, "", "abc123", "x" * 41, 7, object()])
def test_check_never_raises(head):
    assert isinstance(check(_approved(), head), dict)


def test_result_is_a_fresh_dict_each_call():
    story = _approved()
    first = check(story, REVIEWED)
    first["ok"] = False
    assert check(story, REVIEWED)["ok"] is True


# ---------------------------------------------------------------------------
# approve_merge - refuse a stale approval, without rebasing or pushing first.
# ---------------------------------------------------------------------------
def _write_manifest(plan_dir, story):
    (plan_dir / f"{PLAN}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": {KEY: story}}, indent=2)
    )


def _read_story(plan_dir):
    return json.loads((plan_dir / f"{PLAN}.manifest.json").read_text())["stories"][KEY]


class _Recorder:
    """Callable that records its calls and returns a fixed value."""
    def __init__(self, value=None):
        self.value = value
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.value

    @property
    def count(self):
        return len(self.calls)


def _audit_records(plan_name):
    from pipeline import persistence

    path = persistence.PLAN_DIR / f"{plan_name}.audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _bindings(plan_name):
    return [
        r
        for r in _audit_records(plan_name)
        if r["action"] == "merge.review_binding_denied"
    ]


# ---------------------------------------------------------------------------
# approve_merge - refuse a stale approval, without rebasing or pushing first.
# ---------------------------------------------------------------------------
def _fail_subprocess(cmd, cwd):
    """Any git/gh spawn on this path is a bug: name it instead of running it."""
    raise RuntimeError(f"unexpected subprocess {cmd!r} (cwd={cwd!r})")


def _install_approve_merge_seams(monkeypatch, head):
    """Fake every boundary ``_approve_merge_impl`` reaches, plus the head probe.

    Boundary stubs only: the two git-mutating seams (``_rebase_onto_master``
    and ``_merge_pr``) are recorders, so a refusal that reached either one is
    visible as a call count rather than inferred from a return value.
    """
    import contextlib

    import pipeline.authorization as pa
    import pipeline.server as p
    from pipeline import merge, review_merge_binding

    seams = {
        "rebase": _Recorder({"ok": True, "error": "", "auto_resolved": False}),
        "merge_pr": _Recorder(None),
        "notify": _Recorder(None),
        "head": _Recorder(head),
    }
    monkeypatch.setattr(pa, "authorize", lambda *a, **k: None)
    monkeypatch.setattr(p, "_scoped_repo_root", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(p, "_rebase_onto_master", seams["rebase"])
    monkeypatch.setattr(p, "_merge_pr", seams["merge_pr"])
    monkeypatch.setattr(p, "_notify_user", seams["notify"])
    monkeypatch.setattr(p, "_default_branch", lambda: "master")
    monkeypatch.setattr(p, "_mark_plane_done", lambda *a, **k: None)
    monkeypatch.setattr(p, "_mcp_self_source_touched", lambda *a, **k: [])
    monkeypatch.setattr(merge, "_maybe_record_retro", lambda *a, **k: None)
    monkeypatch.setattr(
        merge.subprocess,
        "run",
        lambda cmd, *a, **k: _fail_subprocess(cmd, k.get("cwd")),
    )
    monkeypatch.setattr(p, "_ci_status", lambda *a, **k: {"state": "pass", "error": ""})
    monkeypatch.setattr(
        p, "_reverify_acceptance", lambda *a, **k: {"state": "pass", "error": ""}
    )
    monkeypatch.setattr(
        p, "_reverify_build", lambda *a, **k: {"state": "pass", "error": ""}
    )
    monkeypatch.setattr(
        review_merge_binding,
        "branch_head_sha",
        lambda branch, **kw: seams["head"](branch),
    )
    return seams


def _stale_story(**overrides):
    story = {
        "summary": "stale approval",
        "status": "pr_open",
        "review_verdict": "APPROVE",
        "review_verdict_sha": REVIEWED,
        "worktree": "/nonexistent-worktree",
        "pr_url": "https://example.invalid/pr/1",
        "risk": "low",
    }
    story.update(overrides)
    return story


def test_approve_merge_refuses_stale_approval_without_rebase_or_push(
    plan_dir, monkeypatch
):
    from pipeline import merge

    _write_manifest(plan_dir, _stale_story())
    seams = _install_approve_merge_seams(monkeypatch, "deadbeefcafe99")

    result = merge._approve_merge_impl(PLAN, KEY)

    assert result["ok"] is False
    assert REVIEWED in result["error"]
    assert "deadbeefcafe" in result["error"]
    assert "review_story" in result["error"]
    assert seams["rebase"].count == 0
    assert seams["merge_pr"].count == 0
    assert _read_story(plan_dir)["status"] == "pr_open"


def test_approve_merge_allows_an_approval_that_covers_the_head(plan_dir, monkeypatch):
    from pipeline import merge

    _write_manifest(plan_dir, _stale_story())
    seams = _install_approve_merge_seams(monkeypatch, REVIEWED)

    result = merge._approve_merge_impl(PLAN, KEY)

    assert seams["rebase"].count == 1
    assert seams["merge_pr"].count == 1, result


def test_denial_audit_record_carries_identifiers_only(plan_dir, monkeypatch):
    from pipeline import merge

    _write_manifest(plan_dir, _stale_story(review_feedback="SECRET-FEEDBACK-TEXT"))
    _install_approve_merge_seams(monkeypatch, "deadbeefcafe99")

    merge._approve_merge_impl(PLAN, KEY)

    denials = _bindings(PLAN)
    assert len(denials) == 1
    assert denials[0]["detail"] == {
        "plan": PLAN,
        "story": KEY,
        "review_verdict_sha": REVIEWED,
        "branch_head_sha": "deadbeefcafe",
    }
    assert denials[0]["target"] == {"plan": PLAN, "story": KEY}
    blob = json.dumps(denials[0])
    assert "SECRET-FEEDBACK-TEXT" not in blob
    assert "diff" not in blob
    assert "@@" not in blob


# ---------------------------------------------------------------------------
# scheduler merge path - refuse likewise.
# ---------------------------------------------------------------------------
def _install_scheduler_seams(monkeypatch, head, pushed="deadbeefcafe99"):
    import pipeline.server as p
    from pipeline import review_merge_binding

    seams = {
        "rebase": _Recorder(("", pushed)),
        "merge_pr": _Recorder(None),
        "notify": _Recorder(None),
        "head": _Recorder(head),
    }
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "full")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(
        p, "_rebase_and_push_for_merge", lambda *a, **k: seams["rebase"](*a, **k)
    )
    monkeypatch.setattr(p, "_merge_pr", seams["merge_pr"])
    monkeypatch.setattr(p, "_notify_user", seams["notify"])
    monkeypatch.setattr(p, "_default_branch", lambda: "master")
    monkeypatch.setattr(p, "_mcp_self_source_touched", lambda *a, **k: [])
    monkeypatch.setattr(p, "_maybe_record_retro", lambda *a, **k: None)
    monkeypatch.setattr(
        p, "_merge_gate_ci_status", lambda *a, **k: {"state": "pass", "error": ""}
    )
    monkeypatch.setattr(
        p, "_reverify_acceptance", lambda *a, **k: {"state": "pass", "error": ""}
    )
    monkeypatch.setattr(
        p, "_reverify_build", lambda *a, **k: {"state": "pass", "error": ""}
    )
    monkeypatch.setattr(
        p, "_merge_decision", lambda story: {"action": "merge", "reason": ""}
    )
    monkeypatch.setattr(
        review_merge_binding,
        "branch_head_sha",
        lambda branch, **kw: seams["head"](branch),
    )
    return seams


def _summary():
    return {"parked": [], "notify": [], "merged": [], "failed": [], "ci_pending": []}


def _scheduler_story(**overrides):
    story = {
        "summary": "ready pr",
        "status": "pr_open",
        "review_verdict": "APPROVE",
        "review_verdict_sha": REVIEWED,
        "risk": "low",
        "pr_url": "https://example.invalid/pr/1",
        # A real directory: with no worktree the scheduler skips the binding.
        "worktree": str(Path(__file__).resolve().parent),
    }
    story.update(overrides)
    return story


def test_scheduler_path_refuses_stale_approval_without_rebase_or_push(
    plan_dir, monkeypatch
):
    from pipeline import advance_merge

    _write_manifest(plan_dir, _scheduler_story())
    seams = _install_scheduler_seams(monkeypatch, "cafebabecafe99")

    advance_merge._adjudicate_merges(PLAN, _summary())

    assert seams["rebase"].count == 0
    assert seams["merge_pr"].count == 0
    assert _read_story(plan_dir)["status"] != "done"
    denials = _bindings(PLAN)
    assert len(denials) == 1
    assert set(denials[0]["detail"]) == {
        "plan",
        "story",
        "review_verdict_sha",
        "branch_head_sha",
    }


def test_scheduler_path_rebinds_after_its_own_rebase_and_push(plan_dir, monkeypatch):
    """The pipeline's own merge-gate push is expected: re-bind, never loosen.

    Tick 1 rebases and force-pushes a NEW head (``deadbeefcafe99``). That head
    is what the next tick sees as the remote tip, so the verdict must be
    re-bound to it here, or every retry tick would deny known-good work -
    measured live on obs-metrics-rename-and-otel-config story 064def7e.
    """
    from pipeline import advance_merge

    _write_manifest(plan_dir, _scheduler_story())
    _install_scheduler_seams(monkeypatch, REVIEWED)

    advance_merge._adjudicate_merges(PLAN, _summary())

    story = _read_story(plan_dir)
    assert story["review_verdict"] == "APPROVE"
    assert story["review_verdict_sha"] == "deadbeefcafe99"
    # The re-bound head is accepted; a later human push still denies.
    assert check(story, "deadbeefcafe99")["ok"] is True
    assert check(story, "999aaa")["ok"] is False


# ---------------------------------------------------------------------------
# approve_merge - its own checkpoint/rebase/force-push is re-bound, not denied.
# ---------------------------------------------------------------------------
def test_approve_merge_rebinds_after_its_own_rebase_and_push(plan_dir, monkeypatch):
    """approve_merge force-pushes a new head too: re-bind at that commit point.

    Without this, a retry after a CI failure on the gate's own push would see a
    head no review covers and deny known-good work.
    """
    import subprocess as sp

    from pipeline import merge

    pushed = "deadbeefcafe99"
    _write_manifest(plan_dir, _stale_story(worktree=str(plan_dir)))
    _install_approve_merge_seams(monkeypatch, REVIEWED)

    def _fake_run(cmd, *a, **k):
        out = f"{pushed}\n" if "rev-parse" in cmd else ""
        return sp.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(merge.subprocess, "run", _fake_run)

    result = merge._approve_merge_impl(PLAN, KEY)

    assert result["ok"] is True
    story = _read_story(plan_dir)
    assert story["review_verdict"] == "APPROVE"
    assert story["review_verdict_sha"] == pushed
    assert check(story, pushed)["ok"] is True
    # Re-binding is not a licence for a head nobody reviewed.
    assert check(story, "999aaa")["ok"] is False


def _no_worktree_story(**overrides):
    """A reviewed story with NO local worktree key at all."""
    story = {
        "summary": "ready pr",
        "status": "pr_open",
        "review_verdict": "APPROVE",
        "review_verdict_sha": REVIEWED,
        "risk": "low",
        "pr_url": "https://example.invalid/pr/1",
    }
    story.update(overrides)
    return story


def _install_no_worktree_probe(monkeypatch, head):
    """Record every branch the gate probes, returning ``head``."""
    from pipeline import review_merge_binding

    probes = []

    def _probe(branch, **kwargs):
        probes.append(branch)
        return head

    monkeypatch.setattr(review_merge_binding, "branch_head_sha", _probe)
    return probes


def test_scheduler_path_without_a_worktree_probes_the_degraded_branch_and_denies_on_mismatch(
    plan_dir, monkeypatch
):
    """A missing checkout is not a missing branch: the gate must still run.

    The gate used to be keyed on ``branch``, which is derived from the local
    worktree, so "no local checkout" was misread as "no branch to bind
    against" and the story merged with zero binding verification. The branch
    still exists remotely, so the same fallback name the CI poll uses must be
    probed instead, and a stale approval denied.
    """
    from pipeline import advance_merge
    from pipeline.advance import _degraded_ci_branch

    _write_manifest(plan_dir, _no_worktree_story())
    seams = _install_scheduler_seams(monkeypatch, "cafebabecafe99")
    expected = _degraded_ci_branch(KEY)
    probes = _install_no_worktree_probe(monkeypatch, "cafebabecafe99")

    advance_merge._adjudicate_merges(PLAN, _summary())

    assert probes == [expected]
    assert seams["merge_pr"].count == 0
    denials = _bindings(PLAN)
    assert len(denials) == 1
    assert denials[0]["action"] == "merge.review_binding_denied"


def test_scheduler_path_without_a_worktree_denies_an_unavailable_head(
    plan_dir, monkeypatch
):
    """An unprobeable remote head on the no-worktree path is not ok either."""
    from pipeline import advance_merge
    from pipeline.advance import _degraded_ci_branch

    _write_manifest(plan_dir, _no_worktree_story())
    seams = _install_scheduler_seams(monkeypatch, "cafebabecafe99")
    expected = _degraded_ci_branch(KEY)
    probes = _install_no_worktree_probe(monkeypatch, None)

    advance_merge._adjudicate_merges(PLAN, _summary())

    assert probes == [expected]
    assert seams["merge_pr"].count == 0
    assert len(_bindings(PLAN)) == 1


def test_scheduler_path_without_a_worktree_allows_a_head_the_review_covers(
    plan_dir, monkeypatch
):
    """The same probe accepts the reviewed head, so the fix is not a blanket deny."""
    from pipeline import advance_merge
    from pipeline.advance import _degraded_ci_branch

    _write_manifest(plan_dir, _no_worktree_story())
    seams = _install_scheduler_seams(monkeypatch, "cafebabecafe99")
    expected = _degraded_ci_branch(KEY)
    probes = _install_no_worktree_probe(monkeypatch, REVIEWED)

    advance_merge._adjudicate_merges(PLAN, _summary())

    assert probes == [expected]
    assert seams["merge_pr"].count == 1
    assert _bindings(PLAN) == []
    assert _read_story(plan_dir)["status"] == "done"


def test_scheduler_path_without_a_worktree_denies_on_every_tick_and_never_writes_the_fallback(
    plan_dir, monkeypatch
):
    """The gate is read-only across ticks: no tick may persist the fallback name.

    If the fix wrote the computed branch back into the story, a later tick
    would start from mutated state and the guard-vs-no-guard divergence the
    reviewer rejected would return. The second tick re-opens the story the way
    a manual unpark does, so both ticks actually reach the gate.
    """
    from pipeline import advance_merge
    from pipeline.advance import _degraded_ci_branch

    _write_manifest(plan_dir, _no_worktree_story())
    seams = _install_scheduler_seams(monkeypatch, "cafebabecafe99")
    expected = _degraded_ci_branch(KEY)
    probes = _install_no_worktree_probe(monkeypatch, "cafebabecafe99")

    advance_merge._adjudicate_merges(PLAN, _summary())
    # Re-open, as a human unpark does: a parked story is skipped on the next
    # tick, which would otherwise make the second call vacuous.
    _write_manifest(plan_dir, _no_worktree_story())
    advance_merge._adjudicate_merges(PLAN, _summary())

    assert probes == [expected, expected]
    assert seams["merge_pr"].count == 0
    assert len(_bindings(PLAN)) == 2
    story = _read_story(plan_dir)
    assert "worktree" not in story
    assert "branch" not in story


# ---------------------------------------------------------------------------
# binding_denial_action - a stale approval CAN be re-earned by re-reviewing.
#
# b4 evidence (2026-11-09/10): at 09:30:16 a story parked with "approved
# e698038a but the branch head is 62dfcaf3; the review does not cover the
# commit being merged". The head had moved after the APPROVE (the agent's
# review-fix push at 09:27:45). The recovery was a manual forced re-review,
# which APPROVED the new head - so a denial is not always terminal:
# ``review_story`` can re-earn the approval for the head that will be merged.
# ---------------------------------------------------------------------------
def _denial_action(story, attempts):
    from pipeline import review_merge_binding

    return review_merge_binding.binding_denial_action(story, attempts)


def test_binding_denial_action_rereviews_an_approve_below_the_cap():
    """An APPROVE whose binding no longer covers the head is re-earned by
    re-reviewing it: attempt 0 and attempt 1 both re-review."""
    assert _denial_action({"review_verdict": "APPROVE"}, 0) == "rereview"
    assert _denial_action({"review_verdict": "APPROVE"}, 1) == "rereview"


def test_binding_denial_action_parks_at_and_above_the_cap():
    """The cap is 2 re-reviews: the third denial is terminal."""
    assert _denial_action({"review_verdict": "APPROVE"}, 2) == "park"
    assert _denial_action({"review_verdict": "APPROVE"}, 3) == "park"


def test_binding_denial_action_parks_for_a_non_approve_verdict():
    """Only an APPROVE can be re-earned; anything else parks as today."""
    assert _denial_action({"review_verdict": "REQUEST_CHANGES"}, 0) == "park"
    assert _denial_action({"review_verdict": "REJECT"}, 0) == "park"
    assert _denial_action({"review_verdict": ""}, 0) == "park"
    assert _denial_action({"review_verdict": None}, 0) == "park"
    assert _denial_action({}, 0) == "park"


def test_binding_denial_action_does_not_mutate_the_story():
    """The decision is pure: it reads the story and leaves it alone."""
    story = {"review_verdict": "APPROVE", "review_verdict_sha": REVIEWED}
    assert _denial_action(story, 0) == "rereview"
    assert story == {"review_verdict": "APPROVE", "review_verdict_sha": REVIEWED}


# ---------------------------------------------------------------------------
# The merge gate re-reviews a moved head instead of parking the story.
# ---------------------------------------------------------------------------
MOVED_HEAD = "cafebabecafe99"


def _moved_head_story(**overrides):
    """An approved story whose branch head moved after the approval."""
    return _scheduler_story(
        last_reviewed_sha="oldsha1234",
        **overrides,
    )


def test_moved_head_after_the_approval_is_re_reviewed_not_parked(
    plan_dir, monkeypatch
):
    """Attempt 0: the gate re-reviews the new head instead of parking.

    The story goes back to ``tests_passed`` (the review path re-reviews the
    head that will be merged), the stale binding and the stale reviewed sha
    are cleared, the attempt counter advances, and exactly one notice fires.
    Nothing is rebased, pushed or merged.
    """
    from pipeline import advance_merge

    _write_manifest(plan_dir, _moved_head_story())
    seams = _install_scheduler_seams(monkeypatch, MOVED_HEAD)

    advance_merge._adjudicate_merges(PLAN, _summary())

    story = _read_story(plan_dir)
    assert story["status"] == "tests_passed", (
        "a moved head must go back to the review path, not to the parked "
        f"state; got {story['status']!r}"
    )
    assert story["review_rebind_attempts"] == 1
    # The stale binding must be cleared so the re-review binds afresh: a
    # surviving sha would let the OLD approval cover the NEW head.
    assert not story.get("review_verdict_sha"), (
        "the stale binding must be cleared so the re-review binds afresh; "
        f"got {story.get('review_verdict_sha')!r}"
    )
    assert not story.get("last_reviewed_sha"), (
        "the stale reviewed sha must be cleared so the re-review is not "
        "skipped as 'HEAD unchanged since the last review'"
    )
    assert seams["rebase"].count == 0
    assert seams["merge_pr"].count == 0
    assert _bindings(PLAN) == [], "a re-review is not a binding denial"
    assert len(seams["notify"].calls) == 1
    args, kwargs = seams["notify"].calls[0]
    assert kwargs.get("event") == "review_rebind"
    assert "/2" in args[1], (
        f"the notice must name the attempt as N/2; got {args[1]!r}"
    )


def test_second_moved_head_is_re_reviewed_again(plan_dir, monkeypatch):
    """Attempt 1 is still within the cap of two re-reviews."""
    from pipeline import advance_merge

    _write_manifest(plan_dir, _moved_head_story(review_rebind_attempts=1))
    seams = _install_scheduler_seams(monkeypatch, MOVED_HEAD)

    advance_merge._adjudicate_merges(PLAN, _summary())

    story = _read_story(plan_dir)
    assert story["status"] == "tests_passed"
    assert story["review_rebind_attempts"] == 2
    assert seams["merge_pr"].count == 0
    assert _bindings(PLAN) == []
    assert len(seams["notify"].calls) == 1
    assert seams["notify"].calls[0][1].get("event") == "review_rebind"


def test_third_moved_head_parks(plan_dir, monkeypatch):
    """Attempt 2 exhausts the cap: the story parks, as today."""
    from pipeline import advance_merge

    _write_manifest(plan_dir, _moved_head_story(review_rebind_attempts=2))
    seams = _install_scheduler_seams(monkeypatch, MOVED_HEAD)

    advance_merge._adjudicate_merges(PLAN, _summary())

    story = _read_story(plan_dir)
    assert story["status"] == "parked"
    assert len(_bindings(PLAN)) == 1
    assert seams["merge_pr"].count == 0
    assert story.get("review_rebind_attempts", 2) == 2


def test_non_approve_verdict_with_a_moved_head_still_parks(plan_dir, monkeypatch):
    """Only an APPROVE can be re-earned; any other verdict parks as today."""
    from pipeline import advance_merge

    _write_manifest(
        plan_dir, _moved_head_story(review_verdict="REQUEST_CHANGES")
    )
    seams = _install_scheduler_seams(monkeypatch, MOVED_HEAD)

    advance_merge._adjudicate_merges(PLAN, _summary())

    story = _read_story(plan_dir)
    assert story["status"] == "parked"
    assert len(_bindings(PLAN)) == 1
    assert seams["merge_pr"].count == 0


# ---------------------------------------------------------------------------
# A fresh APPROVE resets the re-bind counter.
# ---------------------------------------------------------------------------
@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    import pipeline.server as p

    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(p, "AGENTS_DIR", d)
    return d


def test_a_fresh_approve_resets_the_rebind_attempt_counter(
    plan_dir, agents_dir, monkeypatch
):
    """Once the moved head is APPROVED again, the counter starts over.

    The re-review of the new head is a fresh approval, so the next moved head
    gets the full budget of re-reviews instead of inheriting the old count.
    """
    import pipeline.server as p

    story = {
        "summary": "Add thing",
        "status": "tests_passed",
        "worktree": str(plan_dir / "wt"),
        "risk": "low",
        "backend": "ollama",
        "model": "gpt-oss:20b",
        "review_rebind_attempts": 2,
    }
    (plan_dir / f"{PLAN}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": {KEY: story}}, indent=2)
    )
    monkeypatch.setattr(p, "_run_reviewer", lambda *a, **k: "VERDICT: APPROVE")
    monkeypatch.setattr(
        p, "_open_pr", lambda *a, **k: "https://example.invalid/pr/2"
    )

    p.review_story(PLAN, KEY)

    on_disk = _read_story(plan_dir)
    assert on_disk["review_verdict"] == "APPROVE"
    assert on_disk.get("review_rebind_attempts", 0) == 0, (
        "a fresh APPROVE must reset the re-bind attempt counter"
    )
