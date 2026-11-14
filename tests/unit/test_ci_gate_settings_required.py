"""The merge CI and build gates take their settings context as a required
keyword (ts-08).

The gates used to read ``PIPELINE_MERGE_CI_GATE`` and friends into module
constants at import time, so every merge in one process shared one value. Now
each gate call names the (namespace, team, repo) it resolves for. A call site
that forgets the context must fail loudly, not fall back to a global.
"""

from __future__ import annotations

import logging

import pytest

from pipeline import ci, persistence, server
from pipeline.ci import SettingsContext

REPO = "github.com/acme/app-a"
LOGGER = "pipeline.ci"


@pytest.fixture(autouse=True)
def plan_dir(tmp_path, monkeypatch):
    plans = tmp_path / "plans"
    plans.mkdir()
    monkeypatch.setattr(server, "PLAN_DIR", plans)
    monkeypatch.setattr(persistence, "PLAN_DIR", plans)
    yield plans


def _seed(scope, scope_id="", repo="", values=None):
    server._store.records().put_settings(
        "default", scope, scope_id, repo, values or {}, [], "ops", None
    )


def _context(team="alpha", repo=REPO):
    return SettingsContext(namespace="default", team=team, repo=repo)


# --- a missed call site fails loudly ---


def test_should_require_settings_context_for_ci_status_once():
    with pytest.raises(TypeError, match="settings"):
        ci._ci_status_once("agent/x", sha="abc")


def test_should_require_settings_context_for_ci_status():
    with pytest.raises(TypeError, match="settings"):
        ci._ci_status("agent/x", sha="abc")


def test_should_require_settings_context_for_reverify_build():
    with pytest.raises(TypeError, match="settings"):
        ci._reverify_build("/tmp/not-a-worktree")


def test_should_require_settings_context_for_reverify_acceptance():
    with pytest.raises(TypeError, match="settings"):
        ci._reverify_acceptance({}, "/tmp/not-a-worktree", "S1")


def test_should_require_settings_context_for_ci_pending_expired():
    with pytest.raises(TypeError, match="settings"):
        ci._ci_pending_expired("2026-11-14T00:00:00+00:00")


# --- gate OFF keeps today's behaviour, and names where OFF came from ---


def test_should_pass_with_ci_gate_disabled_error_when_context_gate_is_off(caplog):
    _seed("team_repo", "alpha", REPO, {"merge_ci_gate": False})

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        result = ci._ci_status_once("agent/x", sha="abc", settings=_context())

    assert result == {"state": ci.PASS, "error": "CI gate disabled"}
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].source == "team_repo"
    assert warnings[0].team == "alpha"
    assert warnings[0].repo == REPO


def test_should_skip_build_when_context_build_gate_is_off():
    _seed("team_repo", "alpha", REPO, {"merge_build_gate": False})

    result = ci._reverify_build("/tmp/not-a-worktree", settings=_context())

    assert result == {"state": "none", "error": "build gate disabled"}


def test_should_expire_pending_wait_on_the_context_timeout():
    _seed("team_repo", "alpha", REPO, {"merge_ci_timeout_s": 60})
    long_ago = "2000-01-01T00:00:00+00:00"

    assert ci._ci_pending_expired(long_ago, settings=_context()) is True


def test_should_not_expire_pending_wait_inside_the_default_timeout():
    recent = "2999-01-01T00:00:00+00:00"

    assert ci._ci_pending_expired(recent, settings=_context()) is False
