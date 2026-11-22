"""The deployment-only ``host_tests_skipped`` lock for CI-gated ``scoped`` stories.

When the deployment layer sets ``host_tests_skipped`` to exactly ``True`` and a
story resolved ``scoped`` (CI gates the merge), the pipeline host runs no test
command for that story. A story degraded to ``full`` keeps its host tests.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from app.identity import Principal
from pipeline import authorization, dispatch_baseline, persistence, settings_tools
from pipeline import server as p
from pipeline import test_scope_dispatch as tsd
from pipeline.actor import bind_principal
from pipeline.settings_registry import REGISTRY
from pipeline.test_scope_run import NO_TESTS_CMD, grade_test_cmd, is_no_tests_cmd

KEY = "host_tests_skipped"
REPO = "github.com/acme/widgets"
HUMAN = Principal("user:alice", auth_method="oidc")
BASE_TEST_CMD = [sys.executable, "-m", "pytest", "-q"]
ACCEPTANCE = [{"path": "tests/acceptance_x.py", "source": ""}]
SCOPED_RESOLUTION = {"test_scope": "scoped", "host_tests": "skipped"}


class _Allow:
    def allow(self, principal, action, resource=None):
        return True


class _CiForgeStub:
    def has_ci_config(self, repo_root):
        return True


@pytest.fixture
def store(tmp_path, monkeypatch):
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", directory)
    monkeypatch.setattr(persistence, "PLAN_DIR", directory)
    monkeypatch.setattr(settings_tools, "provider_configured", lambda: False)
    monkeypatch.setattr(authorization, "default_authorizer", lambda: _Allow())
    monkeypatch.delenv(authorization.ENFORCE_ENV, raising=False)
    return p._store


def _set(**overrides):
    args = {
        "scope": "deployment",
        "scope_id": "",
        "repo": "",
        "key": KEY,
        "value": True,
        "expected_version": None,
    }
    args.update(overrides)
    with bind_principal(HUMAN):
        return settings_tools.set_setting(**args)


def _stamp(story=None, manifest=None):
    manifest = {
        "repo_root": str(Path.cwd()),
        "repo_url": "https://github.com/acme/app.git",
        "test_scope": "scoped",
        **(manifest or {}),
    }
    return tsd.stamp_test_scope("plan-a", "S1", manifest, story if story is not None else {})


def _gate_on_ci(seed_merge_gate, monkeypatch):
    seed_merge_gate(rework_on_ci_fail=True)
    monkeypatch.setattr("pipeline.test_scope_dispatch.get_forge", lambda root: _CiForgeStub())


@pytest.fixture
def notifications(monkeypatch):
    sent = []
    monkeypatch.setattr(tsd, "_notify_user", lambda plan, message, **kwargs: sent.append(message))
    return sent


@pytest.fixture
def default_main(monkeypatch):
    monkeypatch.setattr("pipeline.server._default_branch", lambda: "main")
    monkeypatch.setattr("pipeline.test_scope_run.changed_test_paths", lambda worktree, base: [])


# --- registry: deployment-only lock -----------------------------------------


def test_the_key_is_a_deployment_only_bool_lock_with_safe_value_off():
    key = REGISTRY[KEY]
    assert (key.kind, key.type, key.safe_value, key.allowed_layers) == (
        "lock",
        "bool",
        False,
        ("deployment",),
    )


@pytest.mark.parametrize(
    "scope, scope_id, repo",
    [
        ("org", "acme", ""),
        ("team", "team-1", ""),
        ("team_repo", "team-1", REPO),
    ],
)
def test_set_setting_refuses_the_key_below_the_deployment_scope(store, scope, scope_id, repo):
    refused = _set(scope=scope, scope_id=scope_id, repo=repo)
    assert refused["ok"] is False
    assert refused["reason"] == "lock_key"


def test_set_setting_accepts_the_key_at_deployment(store):
    assert _set()["ok"] is True


# --- stamping: the flag is recorded only for a scoped, CI-gated story -------


def test_stamp_records_host_tests_skipped_for_a_scoped_ci_gated_story(
    store, seed_merge_gate, monkeypatch, notifications
):
    assert _set()["ok"] is True
    _gate_on_ci(seed_merge_gate, monkeypatch)
    story = {}
    _stamp(story=story)
    assert story["test_scope_resolution"]["test_scope"] == "scoped"
    assert story["test_scope_resolution"]["host_tests"] == "skipped"


def test_stamp_degraded_to_full_keeps_host_tests_and_has_no_host_tests_field(
    store, seed_merge_gate, monkeypatch, notifications
):
    assert _set()["ok"] is True
    seed_merge_gate(merge_ci_gate=False)
    story = {}
    _stamp(story=story)
    assert story["test_scope_resolution"]["test_scope"] == "full"
    assert "host_tests" not in story["test_scope_resolution"]


def test_stamp_full_request_has_no_host_tests_field_even_when_flag_is_set(
    store, seed_merge_gate, monkeypatch, notifications
):
    assert _set()["ok"] is True
    _gate_on_ci(seed_merge_gate, monkeypatch)
    story = {}
    _stamp(story=story, manifest={"test_scope": "full"})
    assert story["test_scope_resolution"]["test_scope"] == "full"
    assert "host_tests" not in story["test_scope_resolution"]


@pytest.mark.parametrize("stored", ["true", 1, None])
def test_stamp_treats_a_non_bool_stored_value_as_false(stored, seed_merge_gate, monkeypatch, notifications):
    _gate_on_ci(seed_merge_gate, monkeypatch)
    layers = {"deployment": {KEY: stored}}
    monkeypatch.setattr(tsd, "read_layers", lambda *args, **kwargs: layers)
    story = {}
    _stamp(story=story)
    assert story["test_scope_resolution"]["test_scope"] == "scoped"
    assert "host_tests" not in story["test_scope_resolution"]


# --- grading: the host runs no test command when the flag applies -----------


def test_grade_with_flag_off_keeps_the_scoped_command_for_targets(default_main, tmp_path):
    story = {"test_scope_resolution": {"test_scope": "scoped"}, "acceptance": ACCEPTANCE}
    cmd = grade_test_cmd(story, "S1", tmp_path, Path("tests"), BASE_TEST_CMD, ["full"])
    assert not is_no_tests_cmd(cmd)
    assert cmd[-1].endswith("tests/acceptance_x.py")


def test_grade_with_flag_on_and_scoped_runs_no_tests(default_main, tmp_path):
    story = {"test_scope_resolution": dict(SCOPED_RESOLUTION), "acceptance": ACCEPTANCE}
    cmd = grade_test_cmd(story, "S1", tmp_path, Path("tests"), BASE_TEST_CMD, ["full"])
    assert cmd == list(NO_TESTS_CMD)


def test_grade_with_flag_on_and_scoped_records_the_no_tests_grade(default_main, tmp_path):
    story = {"test_scope_resolution": dict(SCOPED_RESOLUTION), "acceptance": ACCEPTANCE}
    grade_test_cmd(story, "S1", tmp_path, Path("tests"), BASE_TEST_CMD, ["full"])
    grade = story["test_scope_grade"]
    assert grade["targets"] == []
    assert grade["no_tests"] is True
    assert grade["host_tests"] == "skipped"
    assert isinstance(grade["ts"], str)


def test_grade_with_flag_on_and_full_runs_the_full_command(default_main, tmp_path):
    story = {"test_scope_resolution": {"test_scope": "full", "host_tests": "skipped"}}
    cmd = grade_test_cmd(story, "S1", tmp_path, Path("tests"), BASE_TEST_CMD, ["full"])
    assert cmd == ["full"]


def test_grade_with_flag_on_but_degraded_runs_the_full_command(default_main, tmp_path):
    story = {"test_scope_resolution": {"test_scope": "full", "degraded_from": "scoped"}}
    cmd = grade_test_cmd(story, "S1", tmp_path, Path("tests"), BASE_TEST_CMD, ["full"])
    assert cmd == ["full"]
    assert "host_tests" not in story["test_scope_resolution"]


# --- baseline: no host run when the flag applies ----------------------------


def _forbid_run(monkeypatch):
    def fail_run(cmd, **kwargs):
        raise AssertionError(f"subprocess.run must not be called: {cmd!r}")

    monkeypatch.setattr(dispatch_baseline.subprocess, "run", fail_run)


def test_baseline_snapshot_is_none_and_runs_nothing_when_host_tests_are_skipped(
    monkeypatch, tmp_path
):
    _forbid_run(monkeypatch)
    story = {"test_scope_resolution": dict(SCOPED_RESOLUTION), "acceptance": ACCEPTANCE}
    snapshot = dispatch_baseline.baseline_snapshot_for(story, lambda _wt: {"full": True})
    assert snapshot(tmp_path) is None


def test_baseline_snapshot_still_runs_the_scoped_command_when_flag_is_off(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(dispatch_baseline, "detect_test_command", lambda _wt: (tmp_path, BASE_TEST_CMD))
    monkeypatch.setattr(dispatch_baseline.subprocess, "run", fake_run)
    story = {"test_scope_resolution": {"test_scope": "scoped"}, "acceptance": ACCEPTANCE}
    snapshot = dispatch_baseline.baseline_snapshot_for(story, lambda _wt: {"full": True})
    assert snapshot(tmp_path) is not None
    assert len(calls) == 1
