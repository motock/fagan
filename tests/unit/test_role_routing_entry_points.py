"""A team's ``role_routing`` reaches every runtime role resolution (hpc-08).

``pipeline/review.py`` and ``pipeline/review_orchestrator``'s reviewer helpers
already accept a ``layer_routing`` argument, but the ENTRY POINTS never
supplied it: the orchestrator's ``review_story``, both escalation branches and
the step-cap rebrief all resolved roles with the team's routing silently
dropped.

Every test here stubs the settings Store and the role registry - no live file
is read - and drives the real entry points, asserting the routing VALUE that
reaches the downstream resolver.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from app import role_registry
from pipeline import dispatch_attempt, review_orchestrator, server
from pipeline import escalation as esc
from pipeline import persistence as ppers
from tests.unit._line_limit_policy import assert_within_line_limit

SAFE_DEFAULT_SOURCE = "safe_default"
TEAM_SOURCE = "team"
ROUTING = {"dispatch": {"provider": "openai", "model": "gpt-4o-layer"}}


# --------------------------------------------------------------- stubs


class FakeStore:
    """Stand-in for the settings Store's ``effective_setting``.

    The routing is stored per (namespace, team, repo) scope, so a scope the
    plan does not own resolves to the safe default - exactly as the live
    Store does.
    """

    def __init__(self) -> None:
        self.settings: dict[tuple, tuple] = {}
        self.error: Exception | None = None
        self.calls: list[dict] = []

    def put(
        self,
        value: dict,
        source: str,
        *,
        namespace: str,
        team: str,
        repo: str = "",
    ) -> None:
        self.settings[(namespace, team, repo, "role_routing")] = (value, source)

    def effective_setting(self, key, *, namespace, team, repo, org="", request=None):
        self.calls.append(
            {"key": key, "namespace": namespace, "team": team, "repo": repo}
        )
        if self.error is not None:
            raise self.error
        value, source = self.settings.get(
            (namespace, team, repo, key), ({}, SAFE_DEFAULT_SOURCE)
        )
        return {
            "key": key,
            "value": value,
            "source": source,
            "team": team,
            "repo": repo,
        }

    def role_routing_calls(self) -> list[dict]:
        return [call for call in self.calls if call["key"] == "role_routing"]


@pytest.fixture
def store(monkeypatch):
    from pipeline import settings_store

    fake = FakeStore()
    monkeypatch.setattr(settings_store, "effective_setting", fake.effective_setting)
    return fake


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Pin the plan directory (and the notification sink) to a tmp dir."""
    plans = tmp_path / "plans"
    plans.mkdir()
    monkeypatch.setattr(server, "PLAN_DIR", plans)
    monkeypatch.setattr(ppers, "PLAN_DIR", plans)
    monkeypatch.setattr(server, "_notify_user", lambda *args, **kwargs: None)
    return plans


@pytest.fixture
def registry(monkeypatch):
    """A role registry that records every resolution and never dispatches."""
    seen: list[dict] = []

    def fake_resolve_role(role, **kwargs):
        seen.append({"role": role, **kwargs})
        return types.SimpleNamespace(provider="claude", model=None)

    monkeypatch.setattr(role_registry, "resolve_role", fake_resolve_role)
    return seen


@pytest.fixture
def no_escalation_env(monkeypatch):
    for name in ("PIPELINE_ESCALATION_BACKEND", "PIPELINE_ESCALATION_MODEL"):
        monkeypatch.delenv(name, raising=False)


def _write_plan(plans, plan_name: str, stories: dict, **extra) -> dict:
    manifest = {"epics": {}, "stories": stories}
    manifest.update(extra)
    (plans / f"{plan_name}.manifest.json").write_text(json.dumps(manifest))
    return manifest


def _routing_of(call: dict, positional_index: int) -> object:
    """The layer routing a recorded call carries, however it was passed."""
    if "layer_routing" in call["kwargs"]:
        return call["kwargs"]["layer_routing"]
    args = call["args"]
    if len(args) > positional_index:
        return args[positional_index]
    return None


def _recorder(result=None, raises=None):
    """A callable recording every call and returning ``result``."""
    calls: list[dict] = []

    def fake(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        if raises is not None:
            raise raises
        return result

    return fake, calls


# ------------------------------------------- review_orchestrator.review_story


def _review_story(plan_name: str, story_key: str) -> dict:
    return review_orchestrator.review_story(plan_name, story_key)


def test_review_story_forwards_the_plan_routing_to_the_reviewer(
    store, plan_dir, registry, monkeypatch
):
    """AC: review_story resolves the plan's routing and passes it on."""
    store.put(ROUTING, TEAM_SOURCE, namespace="acme", team="core")
    _write_plan(
        plan_dir,
        "hpc-08",
        {"S1": {"status": "tests_passed", "worktree": "", "risk": "low"}},
        namespace="acme",
        team="core",
    )
    fake, calls = _recorder(result="the reviewer returned no verdict line")
    monkeypatch.setattr(server, "_run_reviewer", fake)

    result = _review_story("hpc-08", "S1")

    assert calls, "the reviewer must be invoked"
    assert _routing_of(calls[0], 4) == ROUTING
    assert result.get("ok") is True


def test_review_story_forwards_the_plan_routing_to_the_security_reviewer(
    store, plan_dir, registry, monkeypatch
):
    """AC: the high-risk security pass gets the routing too."""
    store.put(ROUTING, TEAM_SOURCE, namespace="acme", team="core")
    _write_plan(
        plan_dir,
        "hpc-08",
        {"S1": {"status": "tests_passed", "worktree": "", "risk": "high"}},
        namespace="acme",
        team="core",
    )
    fake, _calls = _recorder(result="VERDICT: APPROVE")
    monkeypatch.setattr(server, "_run_reviewer", fake)
    security_fake, security_calls = _recorder(result="no verdict line here")
    monkeypatch.setattr(server, "_run_security_reviewer", security_fake)

    _review_story("hpc-08", "S1")

    assert security_calls, "the high-risk security pass must be invoked"
    assert _routing_of(security_calls[0], 4) == ROUTING


def test_review_story_resolves_the_routing_once_for_the_plan(
    store, plan_dir, registry, monkeypatch
):
    """AC: the routing is resolved ONCE at the top, in the plan's own scope."""
    store.put(ROUTING, TEAM_SOURCE, namespace="acme", team="core")
    _write_plan(
        plan_dir,
        "hpc-08",
        {"S1": {"status": "tests_passed", "worktree": "", "risk": "low"}},
        namespace="acme",
        team="core",
    )
    fake, _calls = _recorder(result="no verdict line here")
    monkeypatch.setattr(server, "_run_reviewer", fake)

    _review_story("hpc-08", "S1")

    reads = store.role_routing_calls()
    assert len(reads) == 1, "the routing must be resolved once, not per call site"
    assert reads[0]["namespace"] == "acme"
    assert reads[0]["team"] == "core"


def test_review_story_forwards_the_routing_to_every_reviewer_invocation(
    store, plan_dir, registry, monkeypatch
):
    """AC: the transient retry re-invokes the reviewer WITH the routing."""
    store.put(ROUTING, TEAM_SOURCE, namespace="acme", team="core")
    _write_plan(
        plan_dir,
        "hpc-08",
        {"S1": {"status": "tests_passed", "worktree": "", "risk": "low"}},
        namespace="acme",
        team="core",
    )
    fake, calls = _recorder(result="connection reset by peer")
    monkeypatch.setattr(server, "_run_reviewer", fake)

    _review_story("hpc-08", "S1")

    assert len(calls) >= 2, "the transient path must retry the reviewer"
    for call in calls:
        assert _routing_of(call, 4) == ROUTING


def test_review_story_forwards_the_routing_to_the_escalated_reviewer(
    store, plan_dir, registry, monkeypatch
):
    """AC: an escalated story's reviewer call carries the routing as well."""
    store.put(ROUTING, TEAM_SOURCE, namespace="acme", team="core")
    _write_plan(
        plan_dir,
        "hpc-08",
        {
            "S1": {
                "status": "tests_passed",
                "worktree": "",
                "risk": "low",
                "escalated": True,
            }
        },
        namespace="acme",
        team="core",
    )
    fake, calls = _recorder(result="no verdict line here")
    monkeypatch.setattr(server, "_run_reviewer", fake)

    _review_story("hpc-08", "S1")

    assert calls, "the reviewer must be invoked"
    assert _routing_of(calls[0], 4) == ROUTING


def test_review_story_without_team_routing_keeps_the_pre_existing_shape(
    store, plan_dir, registry, monkeypatch
):
    """Survivor: a plan with no role_routing is reviewed exactly as before."""
    _write_plan(
        plan_dir,
        "hpc-08",
        {"S1": {"status": "tests_passed", "worktree": "", "risk": "low"}},
        namespace="acme",
        team="core",
    )
    fake, calls = _recorder(result="no verdict line here")
    monkeypatch.setattr(server, "_run_reviewer", fake)

    result = _review_story("hpc-08", "S1")

    assert calls, "the reviewer must still be invoked"
    assert not _routing_of(calls[0], 4), "no routing may be invented"
    assert result.get("ok") is True


# ----------------------------------------------------------------- escalation


def _escalation_manifest(stories: dict, **extra) -> dict:
    manifest = {"stories": stories}
    manifest.update(extra)
    return manifest


def _escalation_plan(plans, manifest: dict, plan_name: str = "hpc-08") -> dict:
    """Register the escalation's plan so its scope is the plan's own.

    The escalation is handed the manifest directly, but the plan it belongs to
    must resolve to the SAME scope - otherwise a plan-scoped resolution would
    silently read a different team's routing.
    """
    _write_plan(
        plans,
        plan_name,
        manifest.get("stories", {}),
        namespace=manifest.get("namespace"),
        team=manifest.get("team"),
        repo_root=manifest.get("repo_root"),
    )
    return manifest


@pytest.fixture
def fake_git(monkeypatch):
    """Stub the git teardown so no real repository is touched."""
    import subprocess as real_subprocess

    class _FakeSubprocess:
        def run(self, cmd, **kwargs):
            return real_subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(esc, "subprocess", _FakeSubprocess())
    monkeypatch.setattr(esc, "_notify_user", lambda *args, **kwargs: None)


def test_escalate_to_claude_forwards_the_manifest_routing_to_diagnose_failure(
    store, plan_dir, registry, no_escalation_env, fake_git, monkeypatch
):
    """AC: the Claude escalation branch diagnoses with the routing."""
    store.put(
        ROUTING, TEAM_SOURCE, namespace="acme", team="core", repo=str(plan_dir)
    )
    fake, calls = _recorder(result="root cause: the guard was missing")
    monkeypatch.setattr(esc, "diagnose_failure", fake)
    manifest = _escalation_plan(
        plan_dir,
        _escalation_manifest(
            {"S1": {"worktree": "", "status": "failed"}},
            namespace="acme",
            team="core",
            repo_root=str(plan_dir),
        ),
    )

    esc._escalate_to_claude(manifest, "hpc-08", "S1")

    assert calls, "the diagnosis must be requested"
    assert _routing_of(calls[0], 3) == ROUTING


def test_escalate_to_local_fallback_model_forwards_the_manifest_routing(
    store, plan_dir, registry, no_escalation_env, fake_git, monkeypatch
):
    """AC: the local-fallback escalation branch diagnoses with the routing."""
    store.put(
        ROUTING, TEAM_SOURCE, namespace="acme", team="core", repo=str(plan_dir)
    )
    fake, calls = _recorder(result="root cause: the guard was missing")
    monkeypatch.setattr(esc, "diagnose_failure", fake)
    manifest = _escalation_plan(
        plan_dir,
        _escalation_manifest(
            {"S1": {"worktree": "", "status": "failed"}},
            namespace="acme",
            team="core",
            repo_root=str(plan_dir),
        ),
    )

    esc._escalate_to_local_fallback_model(
        manifest, "hpc-08", "S1", "gpt-oss-20b-high:latest"
    )

    assert calls, "the diagnosis must be requested"
    assert _routing_of(calls[0], 3) == ROUTING


def test_escalation_resolves_the_manifest_routing_once(
    store, plan_dir, registry, no_escalation_env, fake_git, monkeypatch
):
    """AC: the routing is resolved ONCE per escalation, in the manifest scope."""
    store.put(
        ROUTING, TEAM_SOURCE, namespace="acme", team="core", repo=str(plan_dir)
    )
    fake, _calls = _recorder(result="root cause: the guard was missing")
    monkeypatch.setattr(esc, "diagnose_failure", fake)
    manifest = _escalation_plan(
        plan_dir,
        _escalation_manifest(
            {"S1": {"worktree": "", "status": "failed"}},
            namespace="acme",
            team="core",
            repo_root=str(plan_dir),
        ),
    )

    esc._escalate_to_claude(manifest, "hpc-08", "S1")

    reads = store.role_routing_calls()
    assert len(reads) == 1, "the routing must be resolved once, not per call site"
    assert reads[0]["namespace"] == "acme"
    assert reads[0]["team"] == "core"


def test_escalation_survives_a_broken_settings_store(
    store, plan_dir, registry, no_escalation_env, fake_git, monkeypatch
):
    """Negative: a Store that raises degrades to "no routing", never a crash."""
    store.error = ConnectionError("store down")
    fake, calls = _recorder(result="root cause: the guard was missing")
    monkeypatch.setattr(esc, "diagnose_failure", fake)
    manifest = _escalation_plan(
        plan_dir,
        _escalation_manifest(
            {"S1": {"worktree": "", "status": "failed"}},
            namespace="acme",
            team="core",
            repo_root=str(plan_dir),
        ),
    )

    esc._escalate_to_claude(manifest, "hpc-08", "S1")

    assert calls, "the diagnosis must still be requested"
    assert not _routing_of(calls[0], 3), "no routing may be invented"
    assert manifest["stories"]["S1"]["status"] == "todo"


def test_escalation_without_team_routing_keeps_the_pre_existing_shape(
    store, plan_dir, registry, no_escalation_env, fake_git, monkeypatch
):
    """Survivor: a plan with no role_routing escalates exactly as before."""
    fake, calls = _recorder(result="root cause: the guard was missing")
    monkeypatch.setattr(esc, "diagnose_failure", fake)
    manifest = _escalation_plan(
        plan_dir,
        _escalation_manifest(
            {"S1": {"worktree": "", "status": "failed"}},
            namespace="acme",
            team="core",
            repo_root=str(plan_dir),
        ),
    )

    esc._escalate_to_claude(manifest, "hpc-08", "S1")

    assert calls, "the diagnosis must still be requested"
    assert not _routing_of(calls[0], 3), "no routing may be invented"
    assert manifest["stories"]["S1"]["status"] == "todo"
    assert manifest["stories"]["S1"]["escalated"] is True


# ------------------------------------------- dispatch_attempt rebrief (step cap)


def test_rebrief_step_cap_struggle_forwards_the_plan_routing(
    store, plan_dir, registry, monkeypatch
):
    """AC: the step-cap rebrief diagnoses with the plan's routing."""
    store.put(ROUTING, TEAM_SOURCE, namespace="acme", team="core")
    _write_plan(plan_dir, "hpc-08", {}, namespace="acme", team="core")
    fake, calls = _recorder(result="root cause: the guard was missing")
    monkeypatch.setattr(server, "diagnose_failure", fake)

    dispatch_attempt._rebrief_step_cap_struggle(
        {"worktree": ""},
        "",
        plan_role_config=None,
        plan_name="hpc-08",
        story_key="S1",
    )

    assert calls, "the diagnosis must be requested"
    assert _routing_of(calls[0], 3) == ROUTING


def test_rebrief_step_cap_struggle_without_a_plan_name_resolves_no_routing(
    store, plan_dir, registry, monkeypatch
):
    """Negative: no plan name means no scope - and never a raised error."""
    store.put(ROUTING, TEAM_SOURCE, namespace="acme", team="core")
    fake, calls = _recorder(result="root cause: the guard was missing")
    monkeypatch.setattr(server, "diagnose_failure", fake)

    dispatch_attempt._rebrief_step_cap_struggle(
        {"worktree": ""},
        "",
        plan_role_config=None,
        plan_name=None,
        story_key="S1",
    )

    assert calls, "the diagnosis must still be requested"
    assert not _routing_of(calls[0], 3), "an unknown scope must not resolve routing"
    assert not store.role_routing_calls(), (
        "no plan name means no scope: the settings Store must not be consulted"
    )


def test_rebrief_step_cap_struggle_for_an_unknown_plan_resolves_no_routing(
    store, plan_dir, registry, monkeypatch
):
    """Negative: an unknown plan resolves to {} instead of raising."""
    store.put(ROUTING, TEAM_SOURCE, namespace="acme", team="core")
    fake, calls = _recorder(result="root cause: the guard was missing")
    monkeypatch.setattr(server, "diagnose_failure", fake)

    dispatch_attempt._rebrief_step_cap_struggle(
        {"worktree": ""},
        "",
        plan_role_config=None,
        plan_name="no-such-plan",
        story_key="S1",
    )

    assert calls, "the diagnosis must still be requested"
    assert not _routing_of(calls[0], 3), "an unknown scope must not resolve routing"


def test_rebrief_step_cap_struggle_survives_a_broken_settings_store(
    store, plan_dir, registry, monkeypatch
):
    """Negative: a Store that raises degrades to "no routing", never a crash."""
    store.error = ConnectionError("store down")
    _write_plan(plan_dir, "hpc-08", {}, namespace="acme", team="core")
    fake, calls = _recorder(result="root cause: the guard was missing")
    monkeypatch.setattr(server, "diagnose_failure", fake)

    dispatch_attempt._rebrief_step_cap_struggle(
        {"worktree": ""},
        "",
        plan_role_config=None,
        plan_name="hpc-08",
        story_key="S1",
    )

    assert calls, "the diagnosis must still be requested"
    assert not _routing_of(calls[0], 3), "no routing may be invented"


# ------------------------------------------------------------- the line ceiling


def _line_limit_gate():
    """The repo's own line-limit checker, loaded as a module."""
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / "scripts" / "check_line_limit.py"
    spec = importlib.util.spec_from_file_location("check_line_limit", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "module",
    [review_orchestrator, esc, dispatch_attempt],
    ids=["review_orchestrator", "escalation", "dispatch_attempt"],
)
def test_the_touched_modules_stay_under_the_line_ceiling(module):
    """The modules this story edits must stay under the 1000-line gate.

    ``review_orchestrator.py`` and ``escalation.py`` sit just under the
    ceiling, so the wiring has to fit without growing them past it (a helper
    moves out instead).
    """
    gate = _line_limit_gate()

    lines = gate.count_lines(Path(module.__file__))

    assert_within_line_limit(lines, gate.DEFAULT_LIMIT, Path(module.__file__).name)
