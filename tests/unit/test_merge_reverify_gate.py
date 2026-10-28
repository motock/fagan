"""Detached merge-gate re-verify (TP-04).

The merge gate must keep every guarantee it has today: no merge without a
passing re-verify of the exact SHA being merged. What changes is *where* that
re-verify runs. Running it inline blocks the advance tick for the length of a
full suite plus a build, so the gate becomes a start / collect / merge cycle:

  tick 1  rebase+push -> start the detached job, record the SHA it grades,
          stay ``pr_open`` and publish the key in ``summary["reverify_pending"]``
  tick 2  job still running -> no rebase, no merge, still pending
  tick 3  job finished -> collect the verdict and adjudicate it exactly like
          the inline result: pass merges; fail takes the ordinary gate_error
          path and counts against ``merge_attempts``; a harness error is
          retried the same way and its log tail is recorded.

Two properties are load-bearing and are pinned here explicitly:

  * a result is DISCARDED when the branch head moved while the job ran, so a
    stale verdict can never authorise a merge of a different commit;
  * a job never outlives the story leaving ``pr_open`` - park, terminal
    failure and both rework routes cancel it before flipping the status.

``merge_reverify.detached_enabled`` is False under pytest, so every
pre-existing merge-gate test keeps exercising the inline path; these tests
patch it True to grade the detached path. Only the seams are faked: the real
``_adjudicate_merges`` runs against a real manifest and a real git worktree,
so the branch-head comparison is the production one.
"""

import json
import subprocess
from pathlib import Path

import pytest

import pipeline.server  # noqa: F401 - pre-existing import cycle, import first
from pipeline import advance_merge, merge_reverify

PLAN = "go"
KEY = "P1"


# ---------------------------------------------------------------------------
# Fixtures: manifest, summary, and a real git worktree.
# ---------------------------------------------------------------------------
def _summary():
    """A per-tick summary with the keys ``_adjudicate_merges`` appends to."""
    return {"parked": [], "notify": [], "merged": [], "failed": [], "ci_pending": []}


def _write_manifest(plan_dir, stories):
    (plan_dir / f"{PLAN}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": stories}, indent=2)
    )


def _read_story(plan_dir):
    manifest = json.loads((plan_dir / f"{PLAN}.manifest.json").read_text())
    return manifest["stories"][KEY]


def _story(worktree, **overrides):
    base = {
        "summary": "ready pr",
        "status": "pr_open",
        "review_verdict": "APPROVE",
        "risk": "low",
        "worktree": str(worktree),
    }
    base.update(overrides)
    return base


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _make_worktree(tmp_path):
    """A real one-commit git worktree; returns (path, HEAD sha)."""
    repo = tmp_path / "wt"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    _git(repo, "checkout", "-q", "-b", "agent/P1")
    (repo / "f.txt").write_text("x\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo, _git(repo, "rev-parse", "HEAD")


# ---------------------------------------------------------------------------
# Seam installation.
# ---------------------------------------------------------------------------
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

    def messages(self):
        return [c[0][1] for c in self.calls if len(c[0]) > 1]


def _install_seams(monkeypatch, *, detached=True, decision=None, ci_state="pass"):
    """Fake every seam the merge gate reaches, and return the recorders.

    ``_adjudicate_merges`` itself is NOT faked: the tick's real control flow
    (rebase skip, gate, merge, rework, park) is what these tests grade.
    """
    import pipeline.server as p

    seams = {
        "start": _Recorder({"pid": 4242}),
        "collect": _Recorder(None),
        "cancel": _Recorder(None),
        "rebase": _Recorder(("", "")),
        "merge_pr": _Recorder(None),
        "acceptance": _Recorder({"state": "pass", "error": ""}),
        "build": _Recorder({"state": "pass", "error": ""}),
        "notify": _Recorder(None),
    }
    monkeypatch.setattr(merge_reverify, "detached_enabled", lambda: detached)
    # ``raising=False`` so an early failure is an assertion about behaviour,
    # never an AttributeError about a missing patch target.
    for name in ("start", "collect", "cancel"):
        monkeypatch.setattr(
            advance_merge, f"{name}_merge_reverify", seams[name], raising=False
        )
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.setattr(p, "_rebase_and_push_for_merge", seams["rebase"])
    monkeypatch.setattr(p, "_merge_pr", seams["merge_pr"])
    monkeypatch.setattr(
        p,
        "_merge_gate_ci_status",
        lambda *a, **k: {"state": ci_state, "error": "checks failed"},
    )
    monkeypatch.setattr(p, "_reverify_acceptance", seams["acceptance"])
    monkeypatch.setattr(p, "_reverify_build", seams["build"])
    monkeypatch.setattr(p, "_notify_user", seams["notify"])
    monkeypatch.setattr(p, "_default_branch", lambda: "master")
    monkeypatch.setattr(p, "_mcp_self_source_touched", lambda *a, **k: [])
    monkeypatch.setattr(p, "_maybe_record_retro", lambda *a, **k: None)
    if decision is not None:
        monkeypatch.setattr(p, "_merge_decision", lambda story: decision)
    # An operator shell must not leak the knobs these tests pin.
    monkeypatch.delenv("PIPELINE_STATE_DIR", raising=False)
    monkeypatch.delenv("PIPELINE_MERGE_REVERIFY_TIMEOUT_SECONDS", raising=False)
    return seams


def _tick(plan_dir, summary):
    advance_merge._adjudicate_merges(PLAN, summary)
    return summary


# ---------------------------------------------------------------------------
# A1 - first tick starts the job and keeps the story pr_open.
# ---------------------------------------------------------------------------
class TestFirstTickStartsJob:
    def test_starts_job_records_sha_and_merges_nothing(
        self, plan_dir, tmp_path, monkeypatch
    ):
        _repo, head = _make_worktree(tmp_path)
        seams = _install_seams(monkeypatch)
        seams["rebase"].value = ("", head)
        _write_manifest(plan_dir, {KEY: _story(tmp_path / "wt")})

        summary = _tick(plan_dir, _summary())

        story = _read_story(plan_dir)
        assert seams["merge_pr"].count == 0
        assert story["status"] == "pr_open"
        assert story["merge_reverify"]["sha"] == head
        assert summary["reverify_pending"] == [KEY]
        assert seams["start"].count == 1
        args, _kwargs = seams["start"].calls[0]
        assert args[0] == PLAN
        assert args[1] == KEY
        assert Path(args[3]) == Path(plan_dir)

    def test_state_root_resolves_a_relative_override_against_the_manifest_dir(
        self, plan_dir, tmp_path, monkeypatch
    ):
        _repo, head = _make_worktree(tmp_path)
        seams = _install_seams(monkeypatch)
        seams["rebase"].value = ("", head)
        monkeypatch.setenv("PIPELINE_STATE_DIR", "state")
        _write_manifest(plan_dir, {KEY: _story(tmp_path / "wt")})

        _tick(plan_dir, _summary())

        assert seams["start"].count == 1
        args, _kwargs = seams["start"].calls[0]
        assert Path(args[3]) == Path(plan_dir) / "state"


# ---------------------------------------------------------------------------
# A2 - a running job is polled, never re-rebased and never merged.
# ---------------------------------------------------------------------------
class TestRunningJobIsPolled:
    def test_pending_collect_skips_rebase_and_merge(
        self, plan_dir, tmp_path, monkeypatch
    ):
        _repo, head = _make_worktree(tmp_path)
        seams = _install_seams(monkeypatch)
        seams["rebase"].value = ("", head)
        _write_manifest(plan_dir, {KEY: _story(tmp_path / "wt")})
        _tick(plan_dir, _summary())
        rebases_after_start = seams["rebase"].count

        summary = _tick(plan_dir, _summary())

        story = _read_story(plan_dir)
        assert seams["rebase"].count == rebases_after_start
        assert seams["merge_pr"].count == 0
        assert story["merge_reverify"]["sha"] == head
        assert summary["reverify_pending"] == [KEY]
        assert seams["collect"].count == 1
        assert seams["collect"].calls[0][0][1] == 1800

    def test_timeout_override_is_passed_to_collect(
        self, plan_dir, tmp_path, monkeypatch
    ):
        _repo, head = _make_worktree(tmp_path)
        seams = _install_seams(monkeypatch)
        seams["rebase"].value = ("", head)
        _write_manifest(plan_dir, {KEY: _story(tmp_path / "wt")})
        _tick(plan_dir, _summary())
        monkeypatch.setenv("PIPELINE_MERGE_REVERIFY_TIMEOUT_SECONDS", "60")

        _tick(plan_dir, _summary())

        assert seams["collect"].count == 1
        assert seams["collect"].calls[0][0][1] == 60

    @pytest.mark.parametrize("raw", ["0", "-5", "abc", "nan", "inf", "-inf", ""])
    def test_malformed_or_non_finite_timeout_falls_back_to_the_default(
        self, plan_dir, tmp_path, monkeypatch, raw
    ):
        _repo, head = _make_worktree(tmp_path)
        seams = _install_seams(monkeypatch)
        seams["rebase"].value = ("", head)
        _write_manifest(plan_dir, {KEY: _story(tmp_path / "wt")})
        _tick(plan_dir, _summary())
        monkeypatch.setenv("PIPELINE_MERGE_REVERIFY_TIMEOUT_SECONDS", raw)

        _tick(plan_dir, _summary())

        assert seams["collect"].count == 1
        assert seams["collect"].calls[0][0][1] == 1800


# ---------------------------------------------------------------------------
# A3 - a passing verdict merges and clears the bookkeeping.
# ---------------------------------------------------------------------------
class TestPassingVerdictMerges:
    def test_pass_merges_and_copies_last_test_check(
        self, plan_dir, tmp_path, monkeypatch
    ):
        _repo, head = _make_worktree(tmp_path)
        seams = _install_seams(monkeypatch)
        seams["rebase"].value = ("", head)
        _write_manifest(plan_dir, {KEY: _story(tmp_path / "wt")})
        _tick(plan_dir, _summary())
        seams["collect"].value = {
            "state": "pass",
            "sha": head,
            "last_test_check": {"ok": 1},
        }

        _tick(plan_dir, _summary())

        story = _read_story(plan_dir)
        assert seams["merge_pr"].count == 1
        assert "merge_reverify" not in story
        assert story.get("last_test_check") == {"ok": 1}


# ---------------------------------------------------------------------------
# A4 - a job that cannot start never merges and names no detail.
# ---------------------------------------------------------------------------
class TestStartFailure:
    def test_start_error_is_reported_without_the_exception_text(
        self, plan_dir, tmp_path, monkeypatch
    ):
        _repo, head = _make_worktree(tmp_path)
        seams = _install_seams(monkeypatch)
        seams["rebase"].value = ("", head)

        def _boom(*args, **kwargs):
            raise ValueError("nope")

        monkeypatch.setattr(advance_merge, "start_merge_reverify", _boom, raising=False)
        _write_manifest(plan_dir, {KEY: _story(tmp_path / "wt")})

        _tick(plan_dir, _summary())

        story = _read_story(plan_dir)
        assert seams["merge_pr"].count == 0
        assert "merge_reverify" not in story
        assert story["status"] == "pr_open"
        assert story["merge_attempts"] == 1
        messages = seams["notify"].messages()
        assert any(
            "merge re-verify could not start: ValueError" in m for m in messages
        )
        assert not any("nope" in m for m in messages)

    def test_next_tick_tries_to_start_again(self, plan_dir, tmp_path, monkeypatch):
        _repo, head = _make_worktree(tmp_path)
        seams = _install_seams(monkeypatch)
        seams["rebase"].value = ("", head)
        attempts = []

        def _start(*args, **kwargs):
            attempts.append(args)
            raise ValueError("bad worktree")

        monkeypatch.setattr(advance_merge, "start_merge_reverify", _start, raising=False)
        _write_manifest(plan_dir, {KEY: _story(tmp_path / "wt")})

        _tick(plan_dir, _summary())
        _tick(plan_dir, _summary())

        assert len(attempts) == 2
        assert seams["merge_pr"].count == 0


# ---------------------------------------------------------------------------
# A5 - regression guard: the inline path is unchanged.
# ---------------------------------------------------------------------------
class TestInlinePathUnchanged:
    def test_inline_path_calls_both_reverify_helpers_once(
        self, plan_dir, tmp_path, monkeypatch
    ):
        _repo, head = _make_worktree(tmp_path)
        seams = _install_seams(monkeypatch, detached=False)
        seams["rebase"].value = ("", head)
        _write_manifest(plan_dir, {KEY: _story(tmp_path / "wt")})

        _tick(plan_dir, _summary())

        assert seams["acceptance"].count == 1
        assert seams["build"].count == 1
        assert "merge_reverify" not in _read_story(plan_dir)
        assert seams["merge_pr"].count == 1


# ---------------------------------------------------------------------------
# B1 - a failing verdict takes the ordinary gate_error path, then retries.
# ---------------------------------------------------------------------------
class TestFailingVerdict:
    def test_fail_counts_a_merge_attempt_and_restarts_on_the_next_tick(
        self, plan_dir, tmp_path, monkeypatch
    ):
        _repo, head = _make_worktree(tmp_path)
        seams = _install_seams(monkeypatch)
        seams["rebase"].value = ("", head)
        _write_manifest(plan_dir, {KEY: _story(tmp_path / "wt")})
        _tick(plan_dir, _summary())
        seams["collect"].value = {
            "state": "fail",
            "error": "unit tests failed",
            "sha": head,
        }

        _tick(plan_dir, _summary())

        story = _read_story(plan_dir)
        assert seams["merge_pr"].count == 0
        assert "merge_reverify" not in story
        assert story["merge_attempts"] == 1

        seams["collect"].value = None
        _tick(plan_dir, _summary())

        story = _read_story(plan_dir)
        assert seams["start"].count == 2
        assert story["merge_reverify"]["sha"] == head
        assert seams["merge_pr"].count == 0


# ---------------------------------------------------------------------------
# B2 - a harness error is retried and its log tail recorded.
# ---------------------------------------------------------------------------
class TestHarnessErrorVerdict:
    def test_error_records_log_tail_and_does_not_merge(
        self, plan_dir, tmp_path, monkeypatch
    ):
        _repo, head = _make_worktree(tmp_path)
        seams = _install_seams(monkeypatch)
        seams["rebase"].value = ("", head)
        _write_manifest(plan_dir, {KEY: _story(tmp_path / "wt")})
        _tick(plan_dir, _summary())
        seams["collect"].value = {
            "state": "error",
            "error": "worker crashed",
            "log_tail": "tb",
            "sha": head,
        }

        _tick(plan_dir, _summary())

        story = _read_story(plan_dir)
        assert seams["merge_pr"].count == 0
        assert "merge_reverify" not in story
        assert story["merge_reverify_log_tail"] == "tb"
        assert story["merge_attempts"] == 1


# ---------------------------------------------------------------------------
# B3 - a verdict for a different commit is discarded.
# ---------------------------------------------------------------------------
class TestStaleVerdict:
    def test_result_for_another_sha_does_not_merge(self, plan_dir, tmp_path,
                                                   monkeypatch):
        _repo, head = _make_worktree(tmp_path)
        seams = _install_seams(monkeypatch)
        seams["rebase"].value = ("", head)
        _write_manifest(plan_dir, {KEY: _story(tmp_path / "wt")})
        _tick(plan_dir, _summary())
        seams["collect"].value = {
            "state": "pass",
            "sha": "9f9f9f9",
            "last_test_check": {"ok": 1},
        }

        _tick(plan_dir, _summary())

        story = _read_story(plan_dir)
        assert seams["merge_pr"].count == 0
        assert "merge_reverify" not in story
        assert story.get("last_test_check") == {"ok": 1}

        seams["collect"].value = None
        _tick(plan_dir, _summary())

        story = _read_story(plan_dir)
        assert seams["start"].count == 2
        assert story["merge_reverify"]["sha"] == head
        assert seams["merge_pr"].count == 0


# ---------------------------------------------------------------------------
# B4 - a moved branch head cancels the job and rebases again.
# ---------------------------------------------------------------------------
class TestMovedHead:
    def test_head_mismatch_cancels_the_job_and_rebases(
        self, plan_dir, tmp_path, monkeypatch
    ):
        _repo, _head = _make_worktree(tmp_path)
        seams = _install_seams(monkeypatch)
        seams["rebase"].value = ("", "c0ffee1")
        stale_job = {"pid": 4242, "sha": "0" * 7}
        _write_manifest(
            plan_dir, {KEY: _story(tmp_path / "wt", merge_reverify=stale_job)}
        )

        _tick(plan_dir, _summary())

        story = _read_story(plan_dir)
        assert seams["cancel"].count == 1
        assert seams["cancel"].calls[0][0][0] == stale_job
        assert seams["rebase"].count == 1
        assert story["merge_reverify"]["sha"] == "c0ffee1"
        assert seams["merge_pr"].count == 0


# ---------------------------------------------------------------------------
# B5 - a park decision cancels the job.
# ---------------------------------------------------------------------------
class TestParkCancelsJob:
    def test_park_cancels_the_job_and_later_ticks_never_collect(
        self, plan_dir, tmp_path, monkeypatch
    ):
        _repo, head = _make_worktree(tmp_path)
        job = {"pid": 4242, "sha": head}
        seams = _install_seams(
            monkeypatch,
            decision={"action": "park", "reason": "high risk held for human review"},
        )
        _write_manifest(plan_dir, {KEY: _story(tmp_path / "wt", merge_reverify=job)})

        _tick(plan_dir, _summary())

        story = _read_story(plan_dir)
        assert seams["cancel"].count == 1
        assert seams["cancel"].calls[0][0][0] == job
        assert "merge_reverify" not in story
        assert story["status"] == "parked"

        _tick(plan_dir, _summary())
        assert seams["collect"].count == 0


# ---------------------------------------------------------------------------
# B6 - a terminal gate failure cancels the job.
# ---------------------------------------------------------------------------
class TestTerminalFailureCancelsJob:
    def test_terminal_failure_cancels_the_job(self, plan_dir, tmp_path, monkeypatch):
        import pipeline.server as p

        _repo, head = _make_worktree(tmp_path)
        job = {"pid": 4242, "sha": head}
        seams = _install_seams(monkeypatch, ci_state="fail")
        _write_manifest(
            plan_dir,
            {
                KEY: _story(
                    tmp_path / "wt",
                    merge_reverify=job,
                    merge_attempts=p.MERGE_MAX_ATTEMPTS - 1,
                )
            },
        )

        _tick(plan_dir, _summary())

        story = _read_story(plan_dir)
        assert story["status"] == "failed"
        assert seams["cancel"].count == 1
        assert seams["cancel"].calls[0][0][0] == job
        assert "merge_reverify" not in story
        assert seams["merge_pr"].count == 0
        assert seams["collect"].count == 0
