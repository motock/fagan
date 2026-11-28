"""Handlers that used to swallow failures silently now leave one log record.

Each test drives the public function into the handled failure, asserts the
unchanged return value, and asserts a record naming only the exception class.
"""

from __future__ import annotations

import logging
import subprocess
import types

import pytest

SECRET = "s3cret-token-/home/user/x"


def _boom(*_args, **_kwargs):
    raise RuntimeError(SECRET)


def _assert_logged(caplog, level: int, fragment: str, count: int = 1) -> None:
    records = [r for r in caplog.records if fragment in r.getMessage()]
    assert len(records) == count
    for record in records:
        assert record.levelno == level
        assert "RuntimeError" in record.getMessage()
        assert SECRET not in record.getMessage()
        assert record.exc_info is None


def test_external_status_of_unreadable_store_is_unknown_and_logged(caplog):
    from pipeline import external_refs

    store = types.SimpleNamespace(records=_boom)
    with caplog.at_level(logging.WARNING):
        assert external_refs.status_of(store, "ns", "plan", "key") == external_refs.UNKNOWN_STATUS
    _assert_logged(caplog, logging.WARNING, "external reference unreadable")


def test_github_merge_readiness_unreadable_is_pending_and_logged(monkeypatch, caplog):
    from pipeline.forge_github import GitHubForge

    forge = GitHubForge()
    monkeypatch.setattr(forge, "_run", _boom)
    with caplog.at_level(logging.WARNING):
        assert forge.merge_readiness("/tmp", "branch") == "pending"
    _assert_logged(caplog, logging.WARNING, "merge readiness unreadable")


def test_github_merge_readiness_non_json_is_pending_and_logged(monkeypatch, caplog):
    from pipeline.forge_github import GitHubForge

    forge = GitHubForge()
    proc = types.SimpleNamespace(returncode=0, stdout="not json")
    monkeypatch.setattr(forge, "_run", lambda *a, **k: proc)
    with caplog.at_level(logging.WARNING):
        assert forge.merge_readiness("/tmp", "branch") == "pending"
    records = [r for r in caplog.records if "merge readiness unreadable" in r.getMessage()]
    assert len(records) == 1 and "JSONDecodeError" in records[0].getMessage()


def test_github_merge_readiness_unexpected_error_still_pending(monkeypatch):
    from pipeline.forge_github import GitHubForge

    forge = GitHubForge()
    monkeypatch.setattr(forge, "_run", _boom)
    assert forge.merge_readiness("/tmp", "branch") == "pending"


def test_gitlab_merge_readiness_unreadable_is_pending_and_logged(monkeypatch, caplog):
    from pipeline.forge_gitlab import GitLabForge

    forge = GitLabForge()
    monkeypatch.setattr(forge, "_run", _boom)
    with caplog.at_level(logging.WARNING):
        assert forge.merge_readiness("/tmp", "branch") == "pending"
    _assert_logged(caplog, logging.WARNING, "merge readiness unreadable")


class _HostState:
    def __init__(self, path):
        self._path = path

    def scheduler_health_path(self):
        return self._path


def test_health_extras_unreadable_store_is_logged_and_empty_outbox(tmp_path, caplog):
    from pipeline import health_freshness

    store = types.SimpleNamespace(records=_boom)
    with caplog.at_level(logging.WARNING):
        extras = health_freshness.health_extras(_HostState(tmp_path / "absent.json"), store)
    assert isinstance(extras, dict)
    _assert_logged(caplog, logging.WARNING, "outbox summary unreadable")


def _probe_client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from pipeline import health_freshness

    monkeypatch.setattr(health_freshness, "_freshness_reasons", _boom)
    app = FastAPI()
    health_freshness.register_probes(app, lambda: None)
    return TestClient(app)


@pytest.mark.parametrize("path", ["/livez", "/readyz"])
def test_probe_failure_answers_503_and_logs(monkeypatch, caplog, path):
    client = _probe_client(monkeypatch)
    with caplog.at_level(logging.WARNING):
        response = client.get(path)
    assert response.status_code == 503
    _assert_logged(caplog, logging.WARNING, "health probe failed")


def test_cancel_merge_reverify_pid_probe_failure_logs_debug(monkeypatch, caplog):
    from pipeline import merge_reverify

    monkeypatch.setattr(merge_reverify, "_pid_alive", _boom)
    with caplog.at_level(logging.DEBUG):
        assert merge_reverify.cancel_merge_reverify({"pid": 4242}) is None
    _assert_logged(caplog, logging.DEBUG, "pid liveness probe failed")


def test_cancel_review_job_pid_probe_failure_logs_debug(monkeypatch, caplog):
    from pipeline import review_job

    monkeypatch.setattr(review_job, "_pid_alive", _boom)
    with caplog.at_level(logging.DEBUG):
        assert review_job.cancel_review_job({"pid": 4242}) is None
    _assert_logged(caplog, logging.DEBUG, "pid liveness probe failed")


def test_option_count_settings_failure_is_default_and_logged(monkeypatch, caplog):
    from pipeline import park_options

    monkeypatch.setattr(park_options, "effective", _boom)
    with caplog.at_level(logging.WARNING):
        assert park_options._option_count({}) == park_options.DEFAULT_OPTION_COUNT
    _assert_logged(caplog, logging.WARNING, "park option count unreadable")


def _planner_kwargs():
    return {"dispatch_backend": "claude", "local_model": "m"}


def test_run_planner_failure_is_none_and_logged(monkeypatch, caplog):
    from pipeline import planner

    monkeypatch.setattr(planner, "_resolve_planner_backend", _boom)
    with caplog.at_level(logging.WARNING):
        assert planner._run_planner("do it", **_planner_kwargs()) is None
    _assert_logged(caplog, logging.WARNING, "planner call failed")


def test_run_rework_planner_failure_is_none_and_logged(monkeypatch, caplog):
    from pipeline import planner

    monkeypatch.setattr(planner, "_resolve_planner_backend", _boom)
    with caplog.at_level(logging.WARNING):
        assert planner._run_rework_planner("fix", **_planner_kwargs()) is None
    _assert_logged(caplog, logging.WARNING, "rework planner call failed")


def test_rework_requires_new_tests_failure_is_false_and_logged(monkeypatch, caplog):
    from pipeline import planner

    monkeypatch.setattr(planner, "_resolve_planner_backend", _boom)
    with caplog.at_level(logging.WARNING):
        assert planner._rework_requires_new_tests("fix", **_planner_kwargs()) is False
    _assert_logged(caplog, logging.WARNING, "rework test-need check failed")


def test_preflight_resolver_failure_falls_back_to_env_and_logs(monkeypatch, caplog):
    from pipeline import dispatch, preflight

    monkeypatch.setattr(dispatch, "_resolve_dispatch_target", _boom)
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "Ollama")
    with caplog.at_level(logging.DEBUG):
        assert preflight._resolve_backend() == "ollama"
    _assert_logged(caplog, logging.DEBUG, "dispatch resolver unavailable")


@pytest.mark.parametrize(
    "probe", ["lint_baseline_finding", "suite_baseline_finding", "oracle_finding", "ci_finding"]
)
def test_classify_repo_health_probe_failure_is_skipped_and_logged(monkeypatch, caplog, probe):
    from pipeline import repo_health

    for name in ("lint_baseline_finding", "suite_baseline_finding", "ci_finding"):
        monkeypatch.setattr(repo_health, name, lambda *a, **k: None)
    monkeypatch.setattr(repo_health, "oracle_finding", lambda *a, **k: None)
    monkeypatch.setattr(repo_health, probe, _boom)
    with caplog.at_level(logging.WARNING):
        assert repo_health.classify_repo_health({}, "/tmp") == []
    _assert_logged(caplog, logging.WARNING, "repo health probe failed")


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_watchdog_worker_still_reraises_base_exception_on_caller_thread():
    from pipeline import scheduler_daemon_health

    def _exit():
        raise SystemExit(3)

    daemon = types.SimpleNamespace(
        _consecutive_abandons=0, _abandoned_workers=[], _clock=lambda: 0.0
    )
    with pytest.raises(SystemExit):
        scheduler_daemon_health.run_with_watchdog(
            daemon, _exit, timeout_s=5, label="t"
        )


def test_watchdog_worker_reraises_ordinary_exception_on_caller_thread():
    from pipeline import scheduler_daemon_health

    daemon = types.SimpleNamespace(
        _consecutive_abandons=0, _abandoned_workers=[], _clock=lambda: 0.0
    )
    with pytest.raises(RuntimeError):
        scheduler_daemon_health.run_with_watchdog(daemon, _boom, timeout_s=5, label="t")


def test_forge_subprocess_error_is_not_a_crash(monkeypatch):
    from pipeline.forge_github import GitHubForge

    forge = GitHubForge()

    def _timeout(*_a, **_k):
        raise subprocess.TimeoutExpired("gh", 30)

    monkeypatch.setattr(forge, "_run", _timeout)
    assert forge.merge_readiness("/tmp", "b") == "pending"
