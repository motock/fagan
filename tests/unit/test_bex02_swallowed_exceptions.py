"""Handlers that used to swallow failures silently now leave one log record.

Each test drives the public function into the handled failure, asserts the
unchanged return value, and asserts a record naming only the exception class.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

SECRET = "s3cret-token-/home/user/x"


def _boom(*_args, **_kwargs):
    raise RuntimeError(SECRET)


def _assert_logged(caplog, level: int, fragment: str) -> None:
    records = [r for r in caplog.records if fragment in r.getMessage()]
    assert len(records) == 1
    assert records[0].levelno == level
    assert "RuntimeError" in records[0].getMessage()
    assert SECRET not in records[0].getMessage()
    assert records[0].exc_info is None


def test_load_env_file_logs_and_leaves_environ_untouched(monkeypatch, tmp_path, caplog):
    import pipeline
    from pipeline import env_file

    monkeypatch.delenv("PIPELINE_SKIP_ENV_FILE", raising=False)
    monkeypatch.setattr(env_file, "find_env_file", _boom)
    environ: dict[str, str] = {}
    with caplog.at_level(logging.WARNING, logger="pipeline"):
        pipeline._load_env_file(tmp_path, environ)
    assert environ == {}
    _assert_logged(caplog, logging.WARNING, "env file not loaded")


def test_authorization_unreadable_manifest_is_empty_and_logged(monkeypatch, caplog):
    from pipeline import authorization, store

    monkeypatch.setattr(store, "build_store", _boom)
    with caplog.at_level(logging.WARNING, logger="pipeline"):
        assert authorization._load_manifest("plan") == {}
    _assert_logged(caplog, logging.WARNING, "manifest unreadable")


def test_branch_commit_count_failure_is_none_and_logged(tmp_path, caplog):
    from pipeline import checkpoint

    with caplog.at_level(logging.DEBUG, logger=checkpoint.logger.name):
        assert checkpoint._branch_commit_count(str(tmp_path)) is None
    records = [r for r in caplog.records if "commit count unavailable" in r.getMessage()]
    assert len(records) == 1 and records[0].levelno == logging.DEBUG


def test_snapshot_agent_log_failure_is_none_and_logged(monkeypatch, tmp_path, caplog):
    from pipeline import checkpoint, host_state

    (tmp_path / "agent.log").write_text("x")
    monkeypatch.setattr(host_state, "build_host_state", _boom)
    with caplog.at_level(logging.WARNING, logger=checkpoint.logger.name):
        assert checkpoint._snapshot_agent_log("plan", "key", tmp_path) is None
    _assert_logged(caplog, logging.WARNING, "log tail not saved")


def test_pid_started_after_dispatch_failure_is_false_and_logged(monkeypatch, caplog):
    from pipeline import checkpoint

    monkeypatch.setattr(checkpoint.subprocess, "run", _boom)
    with caplog.at_level(logging.WARNING, logger=checkpoint.logger.name):
        assert checkpoint._pid_started_after_dispatch(1, "2026-01-01T00:00:00+00:00") is False
    _assert_logged(caplog, logging.WARNING, "pid start time unreadable")


def test_translate_line_unexpected_shape_is_empty_and_logged(monkeypatch, caplog):
    from pipeline import claude_log_translate as clt

    monkeypatch.setattr(clt, "_step_lines", _boom)
    with caplog.at_level(logging.WARNING, logger="pipeline"):
        assert clt.translate_line('{"type": "assistant"}', clt.new_state()) == []
    _assert_logged(caplog, logging.WARNING, "not translated")


def test_translate_line_non_json_passes_through_silently(caplog):
    from pipeline import claude_log_translate as clt

    with caplog.at_level(logging.DEBUG):
        assert clt.translate_line("plain text", clt.new_state()) == ["plain text"]
    assert caplog.records == []


def test_fresh_manifest_merge_failure_returns_manifest_and_logs(monkeypatch, caplog):
    from pipeline import detached_grade, story_fields

    monkeypatch.setattr(story_fields, "merge_into_fresh_manifest", _boom)
    manifest = {"stories": {}}
    with caplog.at_level(logging.WARNING, logger=detached_grade.logger.name):
        assert detached_grade._fresh_manifest("plan", "key", {}, manifest) is manifest
    _assert_logged(caplog, logging.WARNING, "manifest merge failed")


def test_transcript_ends_with_done_unreadable_is_false_and_logged(tmp_path, caplog):
    from pipeline import dispatch_attempt

    with caplog.at_level(logging.DEBUG, logger="pipeline"):
        assert dispatch_attempt._transcript_ends_with_done(tmp_path / "missing.json") is False
    records = [r for r in caplog.records if "transcript unreadable" in r.getMessage()]
    assert len(records) == 1 and records[0].levelno == logging.DEBUG


def test_detected_key_parts_failure_is_none_and_logged(monkeypatch, tmp_path, caplog):
    from pipeline import dispatch_baseline

    monkeypatch.setattr(dispatch_baseline, "detect_test_command", _boom)
    with caplog.at_level(logging.WARNING, logger=dispatch_baseline.logger.name):
        assert dispatch_baseline._detected_key_parts(tmp_path) is None
    _assert_logged(caplog, logging.WARNING, "cache key not derived")


def test_mark_worker_abandoned_failure_is_swallowed_and_logged(monkeypatch, caplog):
    from pipeline import dispatch_fence

    monkeypatch.setattr(dispatch_fence, "_prune_dead_workers_locked", _boom)
    with caplog.at_level(logging.WARNING, logger=dispatch_fence._LOG.name):
        assert dispatch_fence.mark_worker_abandoned(threading.current_thread()) is None
    _assert_logged(caplog, logging.WARNING, "not marked abandoned")


def test_worker_is_abandoned_failure_is_false_and_logged(monkeypatch, caplog):
    from pipeline import dispatch_fence

    monkeypatch.setattr(dispatch_fence, "_prune_dead_workers_locked", _boom)
    with caplog.at_level(logging.WARNING, logger=dispatch_fence._LOG.name):
        assert dispatch_fence.worker_is_abandoned(threading.current_thread()) is False
    _assert_logged(caplog, logging.WARNING, "lookup failed")


def test_side_effect_probe_failure_is_false_and_logged(monkeypatch, tmp_path, caplog):
    from pipeline import dispatch_fence

    monkeypatch.setattr(dispatch_fence, "_has_commits_beyond_base", _boom)
    with caplog.at_level(logging.WARNING, logger=dispatch_fence._LOG.name):
        assert dispatch_fence.dispatch_side_effect_in_progress(tmp_path) is False
    _assert_logged(caplog, logging.WARNING, "side-effect probe failed")


def test_blocks_reclaim_failure_is_false_and_logged(monkeypatch, caplog):
    from pipeline import dispatch_fence

    monkeypatch.setattr(dispatch_fence, "lease_is_live", _boom)
    story = {"dispatch_lease_expires_at": "2020-01-01T00:00:00+00:00"}
    with caplog.at_level(logging.WARNING, logger=dispatch_fence._LOG.name):
        assert dispatch_fence.blocks_reclaim(story, "/tmp") is False
    _assert_logged(caplog, logging.WARNING, "reclaim block probe failed")


def test_planner_checklist_git_failure_degrades_and_logs(monkeypatch, tmp_path, caplog):
    from pipeline import dispatch_phases

    marker = tmp_path / ".marker"
    marker.write_text("x")
    seen: dict = {}

    def fake_planner(*_a, **kw):
        seen.update(kw)
        return ""

    monkeypatch.setattr(dispatch_phases, "_test_files_added_on_branch", _boom)
    monkeypatch.setattr(dispatch_phases, "_default_branch", lambda: "master", raising=False)
    monkeypatch.setattr(dispatch_phases, "_run_planner", fake_planner)
    monkeypatch.setattr(dispatch_phases, "_plan_role_config", lambda _n: {})
    monkeypatch.setattr(dispatch_phases, "_LOCAL_BACKEND_NAMES", {"local"})
    spec = {"model": "m", "prompt": "p"}
    with caplog.at_level(logging.WARNING, logger="pipeline"):
        dispatch_phases._apply_planner_checklist(
            {"agent_instructions": "x"}, spec, dispatch_backend="local", resuming=False,
            worktree_path=tmp_path, test_author_marker=marker, plan_name="plan",
        )
    assert seen["authored_test_files"] == []
    _assert_logged(caplog, logging.WARNING, "authored test files not listed")


def _concurrent_dispatch_home(monkeypatch):
    from pipeline import dispatch as home

    monkeypatch.setattr(home, "MAX_CONCURRENT_AGENTS", 2)
    monkeypatch.setattr(home, "_count_in_progress_agents", lambda: 1)
    monkeypatch.setattr(home, "_LOCAL_BACKEND_NAMES", {"local"})
    return home


def _warn_concurrent(story_model="m"):
    from pipeline.dispatch_prompt_build import _warn_on_concurrent_local_dispatch

    _warn_on_concurrent_local_dispatch("plan", "key", {"model": story_model}, "local", None, {})


def test_loaded_models_probe_failure_is_debug_logged(monkeypatch, caplog):
    home = _concurrent_dispatch_home(monkeypatch)
    monkeypatch.setattr(home.backend, "_resolve_local_model", lambda m: m)
    monkeypatch.setattr(home.backend, "_ollama_loaded_models", _boom)
    monkeypatch.setattr(home.backend, "_ollama_serving_parallelism", lambda: None)
    with caplog.at_level(logging.DEBUG, logger="pipeline"):
        _warn_concurrent()
    records = [r for r in caplog.records if "loaded-models probe failed" in r.getMessage()]
    assert len(records) == 1 and records[0].levelno == logging.DEBUG
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_parallelism_probe_failure_is_debug_logged(monkeypatch, caplog):
    home = _concurrent_dispatch_home(monkeypatch)
    monkeypatch.setattr(home.backend, "_resolve_local_model", lambda m: m)
    monkeypatch.setattr(home.backend, "_ollama_loaded_models", lambda _e: set())
    monkeypatch.setattr(home.backend, "_ollama_serving_parallelism", _boom)
    with caplog.at_level(logging.DEBUG, logger="pipeline"):
        _warn_concurrent()
    records = [r for r in caplog.records if "parallelism probe failed" in r.getMessage()]
    assert len(records) == 1 and records[0].levelno == logging.DEBUG
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_feedback_wording_unreadable_transcript_reinjects_and_logs(tmp_path, caplog):
    from pipeline.dispatch_transcript_resume import _feedback_wording

    with caplog.at_level(logging.DEBUG, logger="pipeline"):
        result = _feedback_wording(tmp_path / "missing.json", "feedback")
    assert "feedback" in " ".join(result)
    records = [r for r in caplog.records if "prior transcript unreadable" in r.getMessage()]
    assert len(records) == 1 and records[0].levelno == logging.DEBUG


def test_consumer_status_failure_is_unreachable_and_logged(monkeypatch, caplog):
    from pipeline import events_nats_consumer as nats

    monkeypatch.setitem(nats._status_cache, "expires_at", None)
    monkeypatch.setattr(nats, "_collect_consumer_status", _boom)
    with caplog.at_level(logging.WARNING, logger=nats.logger.name):
        assert nats.consumer_status() == {"reachable": False}
    _assert_logged(caplog, logging.WARNING, "status unavailable")


def test_health_unreadable_fingerprint_degrades_and_logs_debug(monkeypatch, caplog):
    from app import dashboard

    monkeypatch.setattr(dashboard._host_state, "read_scheduler_health", _boom)
    with caplog.at_level(logging.DEBUG, logger=dashboard.logger.name):
        body = dashboard.health()
    assert body["scheduler"] is None and body["config_mismatch"] == []
    records = [r for r in caplog.records if "fingerprint unreadable" in r.getMessage()]
    assert len(records) == 1 and records[0].levelno == logging.DEBUG
    assert SECRET not in records[0].getMessage()


def test_usage_backend_status_failure_degrades_and_logs(monkeypatch, tmp_path: Path, caplog):
    from app import dashboard

    monkeypatch.setattr(dashboard, "collect_backend_status", _boom)
    monkeypatch.setattr(dashboard, "USAGE_STATE_PATH", tmp_path / "absent.json")
    with caplog.at_level(logging.WARNING, logger=dashboard.logger.name):
        assert dashboard.usage() == {"available": False, "backends": []}
    _assert_logged(caplog, logging.WARNING, "backend status collection failed")


def test_audit_contract_threads_do_not_capture_base_exceptions():
    import inspect

    from pipeline import audit_contract

    assert "except BaseException" not in inspect.getsource(audit_contract)
