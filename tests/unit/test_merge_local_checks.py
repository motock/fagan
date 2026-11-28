"""Repo-local checks before the overlord rules on a high-risk merge (coa-05).

In full autonomy the overlord rules on a high-risk merge with GitHub PR checks
as its only test evidence. Before the ruling, the merge gate now runs the
repo's own tests, build and lint on the exact commit (the same detached job as
the merge re-verify, plus lint) and shows the result as a ``LOCAL CHECKS:``
line. These tests pin:

* ``adjudication_due`` mirrors exactly when ``_merge_decision`` asks the overlord;
* a pending job keeps the story ``pr_open`` and never calls the overlord;
* a green result adjudicates once with the line; a red one parks without asking;
* a job error or a missing worktree adjudicates once as ``unavailable``;
* a post-ruling re-verify is skipped when the same commit already passed;
* ``merge_reverify.main`` grades lint only when asked, and the job kinds never
  share a result directory.

Only the seams are faked (the suite, build and lint commands, the job start,
poll and cancel, the overlord). The worktree is a real git repository.
"""

import json
import subprocess
from pathlib import Path

import pytest

import pipeline.ci as _pipeline_ci

# pre-existing import cycle: pipeline.server must be imported first
import pipeline.server  # noqa: F401
from pipeline import advance_merge, merge_local_checks, merge_reverify
from pipeline import config as config_mod
from pipeline import merge as merge_mod
from pipeline import overlord as overlord_mod
from pipeline import persistence as persistence_mod
from pipeline import server as server_mod

PLAN = "go"
KEY = "P1"
PARK_REPLY = "RULING: park\nRATIONALE: held for a human"
REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Recorder:
    """Callable that records its calls and returns ``value`` (mutable)."""

    def __init__(self, value=None):
        self.value = value
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.value

    @property
    def count(self):
        return len(self.calls)


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _make_worktree(tmp_path):
    """A real one-commit git worktree; returns (path, HEAD sha)."""
    repo = tmp_path / "wt"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "f.txt").write_text("x\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo, _git(repo, "rev-parse", "HEAD")


def _story(worktree, **overrides):
    base = {
        "summary": "rewrite the auth token cache",
        "status": "pr_open",
        "review_verdict": "APPROVE",
        "security_review_verdict": "APPROVE",
        "risk": "high",
        "pr_url": "https://example.test/pr/1",
        # Present, so the overlord prompt does not shell out to gh.
        "pr_checks": {"state": "pass", "error": "", "ci": "pass"},
        "worktree": str(worktree),
    }
    base.update(overrides)
    return base


def _write_manifest(plan_dir, stories):
    (plan_dir / f"{PLAN}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": stories}, indent=2)
    )


def _read_story(plan_dir):
    manifest = json.loads((plan_dir / f"{PLAN}.manifest.json").read_text())
    return manifest["stories"][KEY]


def _summary():
    return {
        "parked": [],
        "notify": [],
        "merged": [],
        "failed": [],
        "ci_pending": [],
        "reverify_pending": [],
    }


def _green(sha):
    return {
        "state": "pass",
        "error": "",
        "sha": sha,
        "checks": {"tests": "pass", "build": "pass", "lint": "pass"},
    }


@pytest.fixture
def seams(monkeypatch):
    """Fake every seam the local-checks gate and the tick reach."""
    s = {
        "start": _Recorder({"pid": 4242, "result_path": "r.json", "log_path": "l.log", "started_at": "2026-11-19T12:00:00+00:00"}),
        "collect": _Recorder(None),
        "cancel": _Recorder(None),
        "acceptance": _Recorder({"state": "pass", "error": ""}),
        "build": _Recorder({"state": "pass", "error": ""}),
        "lint": _Recorder({"returncode": 0, "cmd": ["lint"]}),
        "notify": _Recorder(None),
        "overlord": [],
    }

    def _overlord(prompt, plan_role_config=None):
        s["overlord"].append(prompt)
        return PARK_REPLY

    monkeypatch.setattr(config_mod, "PIPELINE_AUTO_MERGE", True)
    monkeypatch.setattr(server_mod, "PIPELINE_AUTONOMY", "full")
    monkeypatch.setattr(server_mod, "PIPELINE_RISK_THRESHOLD", "low")
    monkeypatch.delenv("PIPELINE_TRIAGE_MODE", raising=False)
    monkeypatch.delenv("PIPELINE_STATE_DIR", raising=False)
    monkeypatch.delenv("PIPELINE_MERGE_REVERIFY_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr(merge_reverify, "detached_enabled", lambda: False)
    for name in ("start", "collect", "cancel"):
        monkeypatch.setattr(
            advance_merge, f"{name}_merge_reverify", s[name], raising=False
        )
    monkeypatch.setattr(_pipeline_ci, "_reverify_acceptance", s["acceptance"])
    monkeypatch.setattr(_pipeline_ci, "_reverify_build", s["build"])
    monkeypatch.setattr(server_mod, "_run_lint_gate", s["lint"])
    monkeypatch.setattr(server_mod, "_notify_user", s["notify"])
    monkeypatch.setattr(overlord_mod, "_invoke_overlord", _overlord)
    monkeypatch.setattr(persistence_mod, "_plan_role_config", lambda plan_name: {})
    monkeypatch.setattr(persistence_mod, "_append_decision", lambda plan_name, record: None)
    return s


def _tick(summary=None):
    summary = summary if summary is not None else _summary()
    advance_merge._adjudicate_merges(PLAN, summary)
    return summary


def _detached(monkeypatch):
    monkeypatch.setattr(merge_reverify, "detached_enabled", lambda: True)


# ---------------------------------------------------------------------------
# reverify_timeout and the re-export
# ---------------------------------------------------------------------------


def test_default_timeout_is_1800_and_still_importable_from_advance_merge():
    assert merge_local_checks.reverify_timeout() == 1800.0
    assert advance_merge.MERGE_REVERIFY_TIMEOUT_DEFAULT == 1800.0


def test_env_timeout_overrides_the_default(monkeypatch):
    monkeypatch.setenv("PIPELINE_MERGE_REVERIFY_TIMEOUT_SECONDS", "60")
    assert merge_local_checks.reverify_timeout() == 60.0


@pytest.mark.parametrize("raw", ["nan", "inf", "-5", "0", "soon"])
def test_invalid_env_timeout_falls_back_to_the_default(monkeypatch, raw):
    monkeypatch.setenv("PIPELINE_MERGE_REVERIFY_TIMEOUT_SECONDS", raw)
    assert merge_local_checks.reverify_timeout() == 1800.0


# ---------------------------------------------------------------------------
# adjudication_due parity with _merge_decision
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("approved", [True, False])
@pytest.mark.parametrize("autonomy", ["dry-run", "gated", "full"])
@pytest.mark.parametrize("auto_merge", [True, False])
@pytest.mark.parametrize("risk", ["low", "high", "unknown"])
def test_adjudication_due_is_true_exactly_when_the_overlord_is_asked(
    monkeypatch, approved, autonomy, auto_merge, risk
):
    asked = _Recorder({"action": "park", "reason": "stub"})
    monkeypatch.setattr(merge_mod, "_adjudicate_high_risk_merge", asked)
    monkeypatch.setattr(config_mod, "PIPELINE_AUTO_MERGE", auto_merge)
    monkeypatch.setattr(server_mod, "PIPELINE_AUTONOMY", autonomy)
    monkeypatch.delenv("PIPELINE_TRIAGE_MODE", raising=False)
    story = {
        "review_verdict": "APPROVE" if approved else "REQUEST_CHANGES",
        "risk": risk,
    }

    merge_mod._merge_decision(story, None)

    assert (asked.count == 1) is merge_local_checks.adjudication_due(story, None)


# ---------------------------------------------------------------------------
# Pending job: start, poll, and stay pr_open
# ---------------------------------------------------------------------------


class TestPendingJob:
    def test_first_tick_starts_a_local_checks_job_and_keeps_the_story_pr_open(
        self, plan_dir, tmp_path, monkeypatch, seams
    ):
        _detached(monkeypatch)
        wt, head = _make_worktree(tmp_path)
        _write_manifest(plan_dir, {KEY: _story(wt)})

        summary = _tick()

        story = _read_story(plan_dir)
        assert story["status"] == "pr_open"
        assert story["merge_local_checks_job"]["sha"] == head
        assert seams["overlord"] == []
        assert summary["reverify_pending"] == [KEY]
        assert seams["start"].count == 1
        _args, kwargs = seams["start"].calls[0]
        assert kwargs["kind"] == "merge_local_checks"
        assert kwargs["with_lint"] is True

    def test_a_tick_with_no_result_yet_stays_pending_and_calls_no_overlord(
        self, plan_dir, tmp_path, monkeypatch, seams
    ):
        _detached(monkeypatch)
        wt, _head = _make_worktree(tmp_path)
        _write_manifest(plan_dir, {KEY: _story(wt)})
        _tick()

        summary = _tick()

        assert _read_story(plan_dir)["status"] == "pr_open"
        assert seams["collect"].count == 1
        assert seams["start"].count == 1
        assert seams["overlord"] == []
        assert summary["reverify_pending"] == [KEY]


# ---------------------------------------------------------------------------
# Result handling: green, red, stale, error, missing worktree
# ---------------------------------------------------------------------------


class TestResultHandling:
    def test_green_result_adjudicates_once_with_the_local_checks_line(
        self, plan_dir, tmp_path, monkeypatch, seams
    ):
        _detached(monkeypatch)
        wt, head = _make_worktree(tmp_path)
        _write_manifest(plan_dir, {KEY: _story(wt)})
        _tick()
        seams["collect"].value = _green(head)

        _tick()

        assert len(seams["overlord"]) == 1
        assert (
            f"LOCAL CHECKS: tests pass / lint pass / build pass @ {head[:7]}"
            in seams["overlord"][0]
        )
        assert "merge_local_checks_job" not in _read_story(plan_dir)

    def test_red_result_parks_without_asking_the_overlord(
        self, plan_dir, tmp_path, monkeypatch, seams
    ):
        _detached(monkeypatch)
        wt, head = _make_worktree(tmp_path)
        _write_manifest(plan_dir, {KEY: _story(wt)})
        _tick()
        seams["collect"].value = {
            "state": "fail",
            "error": "acceptance reverify fail: 1 failed",
            "sha": head,
            "checks": {"tests": "fail", "build": "skipped", "lint": "skipped"},
        }

        _tick()

        story = _read_story(plan_dir)
        assert story["status"] == "parked"
        assert story["parked_reason"] == f"high risk held: local checks red @ {head[:7]}"
        assert seams["overlord"] == []

    def test_a_parked_red_hold_is_not_re_asked_on_a_later_tick(
        self, plan_dir, tmp_path, monkeypatch, seams
    ):
        _detached(monkeypatch)
        wt, head = _make_worktree(tmp_path)
        _write_manifest(plan_dir, {KEY: _story(wt)})
        _tick()
        seams["collect"].value = {
            "state": "fail",
            "error": "lint reverify fail",
            "sha": head,
            "checks": {"tests": "pass", "build": "pass", "lint": "fail"},
        }
        _tick()

        _tick()

        assert _read_story(plan_dir)["status"] == "parked"
        assert seams["overlord"] == []

    def test_a_stored_result_for_another_sha_starts_a_new_job(
        self, plan_dir, tmp_path, monkeypatch, seams
    ):
        _detached(monkeypatch)
        wt, head = _make_worktree(tmp_path)
        stale = _story(
            wt,
            local_checks={
                "sha": "1" * 40,
                "state": "pass",
                "checks": {"tests": "pass", "build": "pass", "lint": "pass"},
                "error": "",
            },
        )
        _write_manifest(plan_dir, {KEY: stale})

        _tick()

        story = _read_story(plan_dir)
        assert story["status"] == "pr_open"
        assert story["merge_local_checks_job"]["sha"] == head
        assert seams["start"].count == 1
        assert seams["overlord"] == []

    def test_job_error_adjudicates_once_as_unavailable(
        self, plan_dir, tmp_path, monkeypatch, seams
    ):
        _detached(monkeypatch)
        wt, _head = _make_worktree(tmp_path)
        _write_manifest(plan_dir, {KEY: _story(wt)})
        _tick()
        seams["collect"].value = {
            "state": "error",
            "error": "merge re-verify timed out after 1800s",
        }

        _tick()

        assert len(seams["overlord"]) == 1
        assert (
            "LOCAL CHECKS: unavailable (merge re-verify timed out after 1800s)"
            in seams["overlord"][0]
        )

    def test_a_missing_worktree_is_recorded_as_unavailable_and_adjudicated(
        self, plan_dir, tmp_path, monkeypatch, seams
    ):
        _detached(monkeypatch)
        _write_manifest(plan_dir, {KEY: _story(tmp_path / "gone")})

        _tick()

        assert seams["start"].count == 0
        assert len(seams["overlord"]) == 1
        assert "LOCAL CHECKS: unavailable (worktree unavailable)" in seams["overlord"][0]


# ---------------------------------------------------------------------------
# Inline path (detached off, as under pytest)
# ---------------------------------------------------------------------------


class TestInlinePath:
    def test_inline_run_records_and_adjudicates_in_one_tick(
        self, plan_dir, tmp_path, seams
    ):
        wt, head = _make_worktree(tmp_path)
        _write_manifest(plan_dir, {KEY: _story(wt)})

        _tick()

        assert seams["start"].count == 0
        assert len(seams["overlord"]) == 1
        assert (
            f"LOCAL CHECKS: tests pass / lint pass / build pass @ {head[:7]}"
            in seams["overlord"][0]
        )

    def test_inline_red_tests_park_without_asking_the_overlord(
        self, plan_dir, tmp_path, seams
    ):
        wt, head = _make_worktree(tmp_path)
        seams["acceptance"].value = {"state": "fail", "error": "1 failed"}
        _write_manifest(plan_dir, {KEY: _story(wt)})

        _tick()

        story = _read_story(plan_dir)
        assert story["parked_reason"] == f"high risk held: local checks red @ {head[:7]}"
        assert seams["overlord"] == []


# ---------------------------------------------------------------------------
# Post-ruling re-verify reuse
# ---------------------------------------------------------------------------


class TestPostRulingReuse:
    def test_a_green_local_check_for_the_pushed_sha_skips_the_reverify(
        self, tmp_path, monkeypatch, seams
    ):
        _detached(monkeypatch)
        wt, head = _make_worktree(tmp_path)
        story = _story(
            wt,
            local_checks={
                "sha": head,
                "state": "pass",
                "checks": {"tests": "pass", "build": "pass", "lint": "pass"},
                "error": "",
            },
        )

        result = advance_merge._merge_reverify_gate(
            PLAN, KEY, story, str(wt), head, _summary()
        )

        assert result == ("", False)
        assert seams["start"].count == 0

    def test_a_different_pushed_sha_still_runs_the_reverify(
        self, tmp_path, monkeypatch, seams
    ):
        _detached(monkeypatch)
        wt, head = _make_worktree(tmp_path)
        story = _story(
            wt,
            local_checks={
                "sha": head,
                "state": "pass",
                "checks": {"tests": "pass", "build": "pass", "lint": "pass"},
                "error": "",
            },
        )
        pushed = "f" * 40

        result = advance_merge._merge_reverify_gate(
            PLAN, KEY, story, str(wt), pushed, _summary()
        )

        assert result == ("", True)
        assert seams["start"].count == 1


# ---------------------------------------------------------------------------
# render_local_checks_line
# ---------------------------------------------------------------------------


class TestRenderLine:
    def test_absent_result_renders_not_run(self):
        assert merge_local_checks.render_local_checks_line({}) == "LOCAL CHECKS: not run"

    def test_pass_renders_each_check_and_the_short_sha(self):
        story = {
            "local_checks": {
                "sha": "abc1234def5678",
                "state": "pass",
                "checks": {"tests": "pass", "lint": "not configured", "build": "pass"},
                "error": "",
            }
        }
        assert (
            merge_local_checks.render_local_checks_line(story)
            == "LOCAL CHECKS: tests pass / lint not configured / build pass @ abc1234"
        )

    def test_error_renders_unavailable_with_the_job_error(self):
        story = {"local_checks": {"sha": None, "state": "error", "checks": {}, "error": "worktree unavailable"}}
        assert (
            merge_local_checks.render_local_checks_line(story)
            == "LOCAL CHECKS: unavailable (worktree unavailable)"
        )


# ---------------------------------------------------------------------------
# merge_reverify: job kinds and the --lint argument
# ---------------------------------------------------------------------------


class _Proc:
    pid = 4242


def _fake_popen(monkeypatch):
    calls = {}

    def _popen(argv, **kwargs):
        calls["argv"] = argv
        calls["kwargs"] = kwargs
        return _Proc()

    monkeypatch.setattr(merge_reverify.subprocess, "Popen", _popen)
    return calls


class TestStartKinds:
    def test_an_unknown_kind_is_rejected_before_spawning(self, tmp_path, monkeypatch):
        spawned = _fake_popen(monkeypatch)
        wt = tmp_path / "wt"
        wt.mkdir()

        with pytest.raises(ValueError, match="^invalid kind$"):
            merge_reverify.start_merge_reverify(
                PLAN, KEY, str(wt), tmp_path, kind="bogus"
            )
        assert spawned == {}

    def test_a_local_checks_job_uses_its_own_directory_and_passes_lint(
        self, tmp_path, monkeypatch
    ):
        calls = _fake_popen(monkeypatch)
        wt = tmp_path / "wt"
        wt.mkdir()

        handle = merge_reverify.start_merge_reverify(
            PLAN,
            KEY,
            str(wt),
            tmp_path,
            kind="merge_local_checks",
            with_lint=True,
        )

        assert handle["result_path"] == str(
            (tmp_path / "merge_local_checks" / KEY / "result.json").resolve()
        )
        assert calls["argv"][-1] == "--lint"

    def test_the_default_job_has_no_lint_argument(self, tmp_path, monkeypatch):
        calls = _fake_popen(monkeypatch)
        wt = tmp_path / "wt"
        wt.mkdir()

        merge_reverify.start_merge_reverify(PLAN, KEY, str(wt), tmp_path)

        assert "--lint" not in calls["argv"]

    def test_a_local_checks_job_leaves_the_reverify_result_alone(
        self, tmp_path, monkeypatch
    ):
        _fake_popen(monkeypatch)
        wt = tmp_path / "wt"
        wt.mkdir()
        reverify_dir = tmp_path / "merge_reverify" / KEY
        reverify_dir.mkdir(parents=True)
        (reverify_dir / "result.json").write_text('{"state": "pass"}')

        merge_reverify.start_merge_reverify(
            PLAN, KEY, str(wt), tmp_path, kind="merge_local_checks", with_lint=True
        )

        assert (reverify_dir / "result.json").read_text() == '{"state": "pass"}'


# ---------------------------------------------------------------------------
# merge_reverify.main: the optional --lint fifth argument
# ---------------------------------------------------------------------------


class _ManifestStore:
    def __init__(self, path):
        self._path = path

    def records(self):
        return self

    def get_manifest(self, namespace, plan_name):
        return json.loads(self._path.read_text())


def _main_fixture(tmp_path, monkeypatch, *, lint, build_state="pass"):
    manifest = tmp_path / "P.manifest.json"
    manifest.write_text(json.dumps({"epics": {}, "stories": {"S-1": {}}}))
    monkeypatch.setattr(server_mod, "_store", _ManifestStore(manifest))
    monkeypatch.setattr(
        merge_reverify.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="cafe1234\n", stderr=""),
    )
    monkeypatch.setattr(
        _pipeline_ci,
        "_reverify_acceptance",
        lambda story, path, key, settings=None: {"state": "pass", "error": ""},
    )
    monkeypatch.setattr(
        _pipeline_ci,
        "_reverify_build",
        lambda path, settings=None: {"state": build_state, "error": "build broke"} if build_state == "fail" else {"state": "pass", "error": ""},
    )
    lint_calls = _Recorder(lint)
    monkeypatch.setattr(server_mod, "_run_lint_gate", lint_calls)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    return worktree, tmp_path / "result.json", lint_calls


def test_main_with_lint_grades_a_red_lint_as_a_fail(tmp_path, monkeypatch):
    worktree, result_path, lint_calls = _main_fixture(
        tmp_path, monkeypatch, lint={"returncode": 1, "cmd": ["lint"]}
    )

    rc = merge_reverify.main(["P", "S-1", str(worktree), str(result_path), "--lint"])

    result = json.loads(result_path.read_text())
    assert rc == 0
    assert result["state"] == "fail"
    assert result["error"] == "lint reverify fail"
    assert result["checks"] == {"tests": "pass", "build": "pass", "lint": "fail"}
    assert lint_calls.count == 1


def test_main_without_lint_ignores_a_red_lint(tmp_path, monkeypatch):
    worktree, result_path, lint_calls = _main_fixture(
        tmp_path, monkeypatch, lint={"returncode": 1, "cmd": ["lint"]}
    )

    rc = merge_reverify.main(["P", "S-1", str(worktree), str(result_path)])

    result = json.loads(result_path.read_text())
    assert rc == 0
    assert result["state"] == "pass"
    assert result["checks"]["lint"] == "skipped"
    assert lint_calls.count == 0


def test_main_with_lint_reports_not_configured_when_no_lint_command(tmp_path, monkeypatch):
    worktree, result_path, _lint_calls = _main_fixture(tmp_path, monkeypatch, lint=None)

    merge_reverify.main(["P", "S-1", str(worktree), str(result_path), "--lint"])

    result = json.loads(result_path.read_text())
    assert result["state"] == "pass"
    assert result["checks"]["lint"] == "not configured"


def test_main_skips_lint_when_the_build_fails(tmp_path, monkeypatch):
    worktree, result_path, lint_calls = _main_fixture(
        tmp_path, monkeypatch, lint={"returncode": 1, "cmd": ["lint"]}, build_state="fail"
    )

    merge_reverify.main(["P", "S-1", str(worktree), str(result_path), "--lint"])

    result = json.loads(result_path.read_text())
    assert result["state"] == "fail"
    assert result["checks"] == {"tests": "pass", "build": "fail", "lint": "skipped"}
    assert lint_calls.count == 0


def test_main_rejects_a_fifth_argument_other_than_lint(tmp_path, monkeypatch):
    worktree, result_path, _lint_calls = _main_fixture(
        tmp_path, monkeypatch, lint={"returncode": 0, "cmd": ["lint"]}
    )

    rc = merge_reverify.main(["P", "S-1", str(worktree), str(result_path), "--fast"])

    assert rc == 1
    assert not result_path.exists()


# ---------------------------------------------------------------------------
# Documentation
# ---------------------------------------------------------------------------


def test_overlord_policy_documents_the_local_checks_line():
    assert "LOCAL CHECKS" in (REPO_ROOT / "overlord-policy.md").read_text()


def test_reference_documents_the_local_checks_line():
    assert "LOCAL CHECKS" in (REPO_ROOT / "REFERENCE.md").read_text()
