"""CI failure detail reaches the merge-gate rework message.

Two causes made the CI-fail rework message empty: a snapshot answered from the
rollup cache carried no ``detail``, and the ``gh`` calls that read the failing
log ran outside the story repository and read the wrong run. These tests pin
both fixes and the end-to-end message.
"""

import subprocess

from pipeline import ci as ci_mod
from pipeline import ci_rollup
from pipeline.ci import _ci_rework_feedback, _ci_status_once
from pipeline.forge_github import CI_ROLLUP_MAX_AGE_ENV, GitHubForge

BRANCH = "agent/crf-01"
REPO = "/stories/repo"
NODEID = "tests/unit/test_a.py::test_x"
OTHER_NODEID = "tests/unit/test_b.py::test_y"


class _Result:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def _pytest_log(*nodeids):
    return (
        "\n".join(
            f"2026-11-13T10:00:00.0000000Z FAILED {nodeid} - AssertionError: boom"
            for nodeid in nodeids
        )
        + "\n"
    )


class _Settings:
    def value(self, key):
        return True


def _use_repo(monkeypatch):
    monkeypatch.setattr(ci_mod, "_scoped_repo_cwd", lambda: REPO)


def _use_cache(monkeypatch, states):
    """Answer ``checks_snapshot`` from the rollup cache with ``states``."""
    monkeypatch.delenv(CI_ROLLUP_MAX_AGE_ENV, raising=False)
    monkeypatch.setattr(ci_rollup, "cached_snapshot", lambda *_args: dict(states))


def _route_gh(monkeypatch, routes):
    """Answer each ``gh`` argv by the first route whose key it contains.

    Every call is recorded as ``(argv, cwd)``; an unrouted call fails the test.
    """
    calls = []

    def _fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs.get("cwd")))
        joined = " ".join(argv)
        for needle, result in routes.items():
            if needle in joined:
                return result
        raise AssertionError(f"unexpected argv: {argv}")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    return calls


def _job_routes(job_logs):
    """Routes for a branch whose failing jobs are ``job_logs`` (id -> log)."""
    routes = {
        "check-runs": _Result(stdout="".join(f"{jid}\n" for jid in job_logs)),
    }
    for jid, log in job_logs.items():
        routes[f"actions/jobs/{jid}/logs"] = _Result(stdout=log)
    return routes


# ---------------------------------------------------------------------------
# Cached snapshots keep the failing check names
# ---------------------------------------------------------------------------
def test_should_keep_the_failing_check_names_when_the_snapshot_comes_from_the_cache(
    monkeypatch,
):
    _use_repo(monkeypatch)
    _use_cache(monkeypatch, {"Test (3.11)": "fail", "Lint": "pass"})

    snapshot = GitHubForge().checks_snapshot(BRANCH, "abc123")

    assert snapshot.detail == "Test (3.11): fail"


def test_should_report_cancelled_checks_when_nothing_failed(monkeypatch):
    _use_repo(monkeypatch)
    _use_cache(monkeypatch, {"Deploy": "cancelled", "Lint": "pass"})

    snapshot = GitHubForge().checks_snapshot(BRANCH, "abc123")

    assert snapshot.detail == "Deploy: cancelled"


def test_should_name_only_the_failing_checks_when_a_cancelled_check_also_exists(
    monkeypatch,
):
    _use_repo(monkeypatch)
    _use_cache(monkeypatch, {"Deploy": "cancelled", "Lint": "fail", "Test": "fail"})

    snapshot = GitHubForge().checks_snapshot(BRANCH, "abc123")

    assert snapshot.detail == "Lint: fail; Test: fail"


def test_should_leave_detail_empty_when_the_cached_checks_pass(monkeypatch):
    _use_repo(monkeypatch)
    _use_cache(monkeypatch, {"Lint": "pass", "Test (3.11)": "pass"})

    snapshot = GitHubForge().checks_snapshot(BRANCH, "abc123")

    assert snapshot.detail == ""
    assert dict(snapshot) == {"Lint": "pass", "Test (3.11)": "pass"}


# ---------------------------------------------------------------------------
# Every gh call runs in the story repository
# ---------------------------------------------------------------------------
def test_should_run_every_failure_log_gh_call_in_the_story_repository(monkeypatch):
    _use_repo(monkeypatch)
    calls = _route_gh(monkeypatch, _job_routes({11: _pytest_log(NODEID)}))

    GitHubForge().failure_log_excerpt(BRANCH)

    assert calls
    assert all(cwd == REPO for _, cwd in calls)


def test_should_run_every_rerun_gh_call_in_the_story_repository(monkeypatch):
    _use_repo(monkeypatch)
    calls = _route_gh(
        monkeypatch,
        {
            "actions/runs?head_sha": _Result(stdout="7\n"),
            "gh run rerun": _Result(returncode=0),
        },
    )

    assert GitHubForge().rerun_workflow("abc123") is True
    assert [cwd for _, cwd in calls] == [REPO, REPO]


# ---------------------------------------------------------------------------
# The failing job's own log
# ---------------------------------------------------------------------------
def test_should_read_the_failing_jobs_own_log(monkeypatch):
    _use_repo(monkeypatch)
    calls = _route_gh(
        monkeypatch,
        _job_routes(
            {
                11: _pytest_log(NODEID),
                12: _pytest_log(OTHER_NODEID),
            }
        ),
    )

    excerpt = GitHubForge().failure_log_excerpt(BRANCH)

    assert excerpt.startswith("Failing tests:")
    assert NODEID in excerpt
    assert OTHER_NODEID in excerpt
    assert not any(argv[:2] == ["gh", "run"] for argv, _ in calls)


def test_should_read_at_most_three_failing_jobs_in_id_order(monkeypatch):
    _use_repo(monkeypatch)
    jobs = {
        21: _pytest_log("tests/unit/test_d.py::test_d"),
        13: _pytest_log("tests/unit/test_c.py::test_c"),
        11: _pytest_log("tests/unit/test_a.py::test_a"),
        12: _pytest_log("tests/unit/test_b.py::test_b"),
    }
    calls = _route_gh(monkeypatch, _job_routes(jobs))

    excerpt = GitHubForge().failure_log_excerpt(BRANCH)

    job_reads = [argv[-1] for argv, _ in calls if "actions/jobs" in argv[-1]]
    assert job_reads == [
        "repos/{owner}/{repo}/actions/jobs/11/logs",
        "repos/{owner}/{repo}/actions/jobs/12/logs",
        "repos/{owner}/{repo}/actions/jobs/13/logs",
    ]
    assert "test_d" not in excerpt


def test_should_fall_back_to_the_run_log_when_no_check_run_failed(monkeypatch):
    _use_repo(monkeypatch)
    calls = _route_gh(
        monkeypatch,
        {
            "check-runs": _Result(stdout=""),
            "gh run list": _Result(stdout="42\n"),
            "gh run view 42 --log-failed": _Result(stdout=_pytest_log(NODEID)),
        },
    )

    excerpt = GitHubForge().failure_log_excerpt(BRANCH)

    assert excerpt.startswith("Failing tests:")
    assert NODEID in excerpt
    assert any(argv[:3] == ["gh", "run", "list"] for argv, _ in calls)


def test_should_return_none_when_every_lookup_fails(monkeypatch):
    _use_repo(monkeypatch)
    _route_gh(
        monkeypatch,
        {
            "check-runs": _Result(returncode=1),
            "gh run list": _Result(returncode=1),
        },
    )

    assert GitHubForge().failure_log_excerpt(BRANCH) is None


def test_should_return_none_when_gh_is_unavailable(monkeypatch):
    _use_repo(monkeypatch)

    def _boom(argv, **_):
        raise OSError("gh not installed")

    monkeypatch.setattr(subprocess, "run", _boom)

    assert GitHubForge().failure_log_excerpt(BRANCH) is None


def test_should_quote_the_branch_name_in_the_check_runs_path(monkeypatch):
    _use_repo(monkeypatch)
    calls = _route_gh(
        monkeypatch,
        {
            "check-runs": _Result(stdout=""),
            "gh run list": _Result(returncode=1),
        },
    )

    GitHubForge().failure_log_excerpt(BRANCH)

    check_run_argv = next(argv for argv, _ in calls if "check-runs" in " ".join(argv))
    assert "commits/agent%2Fcrf-01/check-runs" in " ".join(check_run_argv)


# ---------------------------------------------------------------------------
# End to end: the rework message names the failing tests
# ---------------------------------------------------------------------------
def test_should_put_the_failing_tests_in_the_rework_message(monkeypatch):
    _use_repo(monkeypatch)
    _use_cache(monkeypatch, {"Test (Python 3.11)": "fail", "Lint": "pass"})
    monkeypatch.setattr(ci_mod, "get_forge", lambda _root: GitHubForge())
    _route_gh(monkeypatch, _job_routes({11: _pytest_log(NODEID)}))

    result = _ci_status_once(BRANCH, sha="abc123", settings=_Settings())

    assert result["state"] == "fail"
    assert result["error"].startswith("Test (Python 3.11): fail")
    message = _ci_rework_feedback(f"ci fail: {result['error']}", 1)
    assert NODEID in message
