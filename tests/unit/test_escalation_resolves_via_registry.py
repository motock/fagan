"""TDD suite: escalation must resolve its BASE (provider, model) through
``app.role_registry.resolve_role``, exactly like dispatch does after REG-1.

ROOT CAUSE
----------
``pipeline/escalation.py::_escalation_target`` is the THIRD site that resolves
a dispatch backend by reading the environment directly::

    backend = (os.environ.get("PIPELINE_ESCALATION_BACKEND") or "claude")...
    model = (os.environ.get("PIPELINE_ESCALATION_MODEL") or "").strip() or None

REG-1 moved ``pipeline/dispatch.py`` and ``pipeline/advance.py`` onto
``app.role_registry.resolve_role("dispatch", ...)``. Escalation was left
behind, so an escalated story silently reverts to the old env-only behaviour -
re-introducing exactly the divergence REG-1 fixed, but only on the escalation
path, where it is hardest to notice.

CONTRACT PINNED BY THIS FILE
----------------------------
``pipeline.escalation._escalation_target() -> (backend, model)`` keeps its
zero-argument signature (``pipeline/server.py`` re-exports it and existing
tests call it bare), and:

* ``PIPELINE_ESCALATION_BACKEND`` / ``PIPELINE_ESCALATION_MODEL`` remain the
  escalation-specific override and still win when set - unchanged.
* Otherwise the BASE resolution goes through
  ``role_registry.resolve_role("dispatch", ...)``: the registry's provider and
  model (the resolved driver tag) win over the hardcoded ``("claude", None)``
  default.
* A registry with no usable ``roles.dispatch`` entry (absent, or malformed)
  fails OPEN to today's behaviour: ``("claude", None)`` - never a crash.
* ``story["backend"]`` still wins where it always did: the pin escalation
  writes is honoured by dispatch's own priority order, so a pinned story is
  never re-resolved to the registry.
* The escalation DECISION logic (``_auto_escalation_enabled``) is untouched.

Per ``.claude/rules/testing-config-gates.md`` every test stubs the registry
with a synthetic fixture; nothing asserts against this machine's real
``model_registry``.
"""

from __future__ import annotations

import json

from app import role_registry
from pipeline import dispatch, escalation
from pipeline import server as p
from tests.unit._pipeline_mcp_server_test_helpers import (
    _make_fake_git_run,
)

# Synthetic registry payload. Deliberately NOT this machine's real registry:
# the point is that the *registry* wins, not that any particular tag does.
_PROVIDERS = {
    "claude": {"models": {"sonnet": {"tag": "claude-sonnet-4"}}},
    "ollama": {
        "models": {
            "deepseek-v4.1-flash": {"tag": "deepseek-v4.1-flash:cloud"},
            "glm": {"tag": "glm-5.3-flash:cloud"},
        }
    },
}

_REGISTRY_DISPATCH = {"provider": "ollama", "model": "deepseek-v4.1-flash"}
_REGISTRY_MODEL_TAG = "deepseek-v4.1-flash:cloud"

# The hardcoded pre-registry default escalation used to fall back to.
_OLD_DEFAULT = ("claude", None)

# The story's pre-escalation state: a failed local run.
_LOCAL_MODEL_TAG = "gemma4:26b-a4b-it-qat"


def _install_registry(monkeypatch, tmp_path, roles):
    """Point the registry at a synthetic payload and drop the memo."""
    path = tmp_path / "model_registry.json"
    path.write_text(json.dumps({"providers": _PROVIDERS, "roles": roles}))
    monkeypatch.setenv("PIPELINE_MODEL_REGISTRY_PATH", str(path))
    role_registry.reset_registry_cache()
    return path


def _clear_env(monkeypatch):
    """No escalation override, and no dispatch env var to outrank the registry.

    ``resolve_role``'s provider priority is plan -> ``PIPELINE_BACKEND_<ROLE>``
    env -> registry -> default, so ``PIPELINE_BACKEND_DISPATCH`` must be unset
    for the registry's provider to be the one that wins.
    """
    for name in (
        "PIPELINE_ESCALATION_BACKEND",
        "PIPELINE_ESCALATION_MODEL",
        "PIPELINE_BACKEND_DISPATCH",
        "PIPELINE_LOCAL_MODEL_DEFAULT",
    ):
        monkeypatch.delenv(name, raising=False)


def _target():
    """Call the pinned seam, failing with a clear message if it is absent."""
    fn = getattr(escalation, "_escalation_target", None)
    assert fn is not None, (
        "pipeline.escalation._escalation_target() -> (backend, model) is "
        "missing: escalation must resolve its base target through "
        "app.role_registry.resolve_role('dispatch', ...) instead of reading "
        "PIPELINE_ESCALATION_BACKEND raw and defaulting to 'claude'"
    )
    return fn()


def _manifest(worktree):
    """A failed local story, exactly the shape escalation is handed."""
    return {
        "epics": {},
        "stories": {
            "S1": {
                "summary": "thing",
                "status": "in_progress",
                "pid": 4242,
                "worktree": str(worktree),
                "backend": "local",
                "model": _LOCAL_MODEL_TAG,
                "dispatch_attempts": 1,
                "dispatch_error": "boom",
            }
        },
    }


def _escalate(plan_dir_path, tmp_path, monkeypatch, plan_name, manifest):
    manifest_path = plan_dir_path / f"{plan_name}.manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(p.subprocess, "run", _make_fake_git_run(head_sha="deadbeef"))
    p._escalate_to_claude(manifest, plan_name, "S1", manifest_path)
    return manifest["stories"]["S1"]


# ---------------------------------------------------------------------------
# 1. POSITIVE: the registry wins when no PIPELINE_ESCALATION_* is set
# ---------------------------------------------------------------------------


def test_escalation_target_resolves_registry_provider_and_model(monkeypatch, tmp_path):
    _install_registry(monkeypatch, tmp_path, {"dispatch": dict(_REGISTRY_DISPATCH)})
    _clear_env(monkeypatch)

    backend, model = _target()

    assert backend == "ollama", (
        "with no PIPELINE_ESCALATION_BACKEND set, escalation must resolve its "
        "base provider through role_registry (registry roles.dispatch.provider), "
        "not fall back to the hardcoded 'claude'"
    )
    assert model == _REGISTRY_MODEL_TAG, (
        "escalation must resolve its base MODEL through role_registry too - "
        "today it resolves no model at all and the driver decides"
    )
    assert (backend, model) != _OLD_DEFAULT, (
        "the registry-pinned target must not collapse back to the pre-registry "
        "('claude', None) default"
    )


def test_escalation_target_consults_role_registry_for_dispatch(monkeypatch, tmp_path):
    """MECHANICAL: the base resolution must actually go through resolve_role.

    Patches both plausible import styles (``from app import role_registry`` and
    ``from app.role_registry import resolve_role``) so the assertion holds
    whichever the implementation picks - including delegating to
    ``pipeline.dispatch._resolve_dispatch_target``, which calls it too.
    """
    _install_registry(monkeypatch, tmp_path, {"dispatch": dict(_REGISTRY_DISPATCH)})
    _clear_env(monkeypatch)

    calls: list[str] = []
    real = role_registry.resolve_role

    def spy(role, **kwargs):
        calls.append(role)
        return real(role, **kwargs)

    monkeypatch.setattr(role_registry, "resolve_role", spy)
    if hasattr(escalation, "resolve_role"):
        monkeypatch.setattr(escalation, "resolve_role", spy)

    _target()

    assert "dispatch" in calls, (
        "escalation must resolve its base target through "
        "app.role_registry.resolve_role('dispatch', ...) - the same resolver "
        "dispatch/advance now use - instead of reading the environment directly"
    )


def test_escalate_to_claude_flips_story_to_registry_target(plan_dir, tmp_path, monkeypatch):
    """The resolved registry target is what lands on the story."""
    _install_registry(monkeypatch, tmp_path, {"dispatch": dict(_REGISTRY_DISPATCH)})
    _clear_env(monkeypatch)
    worktree = tmp_path / "wt"
    worktree.mkdir()

    story = _escalate(plan_dir, tmp_path, monkeypatch, "regesc", _manifest(worktree))

    assert story["backend"] == "ollama", (
        "an escalated story must be pinned to the registry-resolved provider, "
        "not the hardcoded 'claude'"
    )
    assert story["model"] == _REGISTRY_MODEL_TAG, (
        "an escalated story must be pinned to the registry-resolved model tag"
    )
    assert story["escalated"] is True
    assert story["status"] == "todo"


# ---------------------------------------------------------------------------
# 2. NEGATIVE CONTROL: PIPELINE_ESCALATION_BACKEND/MODEL still win
# ---------------------------------------------------------------------------


def test_escalation_env_backend_and_model_still_win_over_registry(monkeypatch, tmp_path):
    _install_registry(monkeypatch, tmp_path, {"dispatch": dict(_REGISTRY_DISPATCH)})
    _clear_env(monkeypatch)
    monkeypatch.setenv("PIPELINE_ESCALATION_BACKEND", "lmstudio")
    monkeypatch.setenv("PIPELINE_ESCALATION_MODEL", "some-raw-tag:cloud")

    assert _target() == ("lmstudio", "some-raw-tag:cloud"), (
        "the escalation-specific override must keep winning when set - this "
        "story only changes the BASE resolution underneath it"
    )


def test_escalation_env_backend_without_model_still_yields_none(monkeypatch, tmp_path):
    """Boundary of the override: backend set, model unset -> model None.

    The escalation model is a raw driver tag, never the registry's friendly
    name, so an unset PIPELINE_ESCALATION_MODEL must NOT be back-filled from
    the registry.
    """
    _install_registry(monkeypatch, tmp_path, {"dispatch": dict(_REGISTRY_DISPATCH)})
    _clear_env(monkeypatch)
    monkeypatch.setenv("PIPELINE_ESCALATION_BACKEND", "ollama")

    assert _target() == ("ollama", None)


def test_escalation_env_backend_wins_even_when_it_matches_registry_provider(
    monkeypatch, tmp_path,
):
    """The override wins on the model too, not just the provider."""
    _install_registry(monkeypatch, tmp_path, {"dispatch": dict(_REGISTRY_DISPATCH)})
    _clear_env(monkeypatch)
    monkeypatch.setenv("PIPELINE_ESCALATION_BACKEND", "ollama")
    monkeypatch.setenv("PIPELINE_ESCALATION_MODEL", "glm-5.3-flash:cloud")

    assert _target() == ("ollama", "glm-5.3-flash:cloud"), (
        "an explicit PIPELINE_ESCALATION_MODEL must beat the registry's own "
        "model for the same provider"
    )


# ---------------------------------------------------------------------------
# 3. NEGATIVE CONTROL: story["backend"] already pinned still wins
# ---------------------------------------------------------------------------


def test_pinned_story_backend_still_wins_over_registry(monkeypatch, tmp_path):
    """A story that already pins its backend (how an escalation flip records
    its target) keeps that pin - the registry base resolution must never
    re-route it."""
    _install_registry(monkeypatch, tmp_path, {"dispatch": dict(_REGISTRY_DISPATCH)})
    _clear_env(monkeypatch)

    story = {"backend": "claude"}

    assert dispatch._resolve_dispatch_target(story) == ("claude", None), (
        "story['backend'] is the most specific pin and must still win over the "
        "registry-resolved provider; a claude-routed story must never be handed "
        f"the ollama registry tag {_REGISTRY_MODEL_TAG!r}"
    )
    assert dispatch._resolve_dispatch_backend(story, "ollama") == "claude"


def test_escalation_pin_is_what_dispatch_honours(plan_dir, tmp_path, monkeypatch):
    """The backend escalation writes is the pin dispatch honours on the next
    tick - escalation must not leave the story to be re-resolved to the
    registry."""
    _install_registry(monkeypatch, tmp_path, {"dispatch": dict(_REGISTRY_DISPATCH)})
    _clear_env(monkeypatch)
    worktree = tmp_path / "wt"
    worktree.mkdir()

    story = _escalate(plan_dir, tmp_path, monkeypatch, "regpin", _manifest(worktree))

    assert story["backend"] == "ollama"
    assert dispatch._resolve_dispatch_target(story) == ("ollama", _REGISTRY_MODEL_TAG)


# ---------------------------------------------------------------------------
# 4. BOUNDARY: no usable roles.dispatch entry -> exactly today's behaviour
# ---------------------------------------------------------------------------


def test_registry_without_dispatch_role_falls_back_to_claude_none(monkeypatch, tmp_path):
    _install_registry(monkeypatch, tmp_path, {})
    _clear_env(monkeypatch)

    assert _target() == _OLD_DEFAULT, (
        "a registry with no roles.dispatch entry (a fresh clone) must behave "
        "exactly as today: ('claude', None), never a crash"
    )


def test_malformed_registry_dispatch_entry_fails_open(monkeypatch, tmp_path):
    # roles.dispatch names a model that is not declared under
    # providers.ollama.models -> resolve_role raises RoleRegistryError.
    _install_registry(
        monkeypatch, tmp_path, {"dispatch": {"provider": "ollama", "model": "nope"}}
    )
    _clear_env(monkeypatch)

    assert _target() == _OLD_DEFAULT, (
        "a misconfigured roles.dispatch must fail open to the pre-registry "
        "behaviour rather than crash escalation"
    )


def test_escalate_to_claude_without_registry_entry_keeps_todays_behaviour(
    plan_dir, tmp_path, monkeypatch,
):
    """The pre-registry default: flip to claude, leave the story's model alone."""
    _install_registry(monkeypatch, tmp_path, {})
    _clear_env(monkeypatch)
    worktree = tmp_path / "wt"
    worktree.mkdir()

    story = _escalate(plan_dir, tmp_path, monkeypatch, "regnone", _manifest(worktree))

    assert story["backend"] == "claude"
    assert story.get("model") == _LOCAL_MODEL_TAG, (
        "with no registry model to pin, the story's existing model must be left "
        "untouched - claude dispatch resolves its own model"
    )


# ---------------------------------------------------------------------------
# 5. BOUNDARY: escalating twice does not thrash between two different models
# ---------------------------------------------------------------------------


def test_escalation_target_is_deterministic(monkeypatch, tmp_path):
    _install_registry(monkeypatch, tmp_path, {"dispatch": dict(_REGISTRY_DISPATCH)})
    _clear_env(monkeypatch)

    assert _target() == _target() == ("ollama", _REGISTRY_MODEL_TAG)


def test_escalating_twice_does_not_thrash_between_models(plan_dir, tmp_path, monkeypatch):
    """A second escalation of the same story must resolve the SAME
    (backend, model) as the first - the resolution must not oscillate between
    the registry model and whatever the first escalation wrote onto the
    story."""
    _install_registry(monkeypatch, tmp_path, {"dispatch": dict(_REGISTRY_DISPATCH)})
    _clear_env(monkeypatch)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    manifest = _manifest(worktree)

    first = _escalate(plan_dir, tmp_path, monkeypatch, "regtwice", manifest)
    first_pair = (first["backend"], first["model"])

    second = _escalate(plan_dir, tmp_path, monkeypatch, "regtwice", manifest)
    second_pair = (second["backend"], second["model"])

    assert first_pair == second_pair == ("ollama", _REGISTRY_MODEL_TAG), (
        "escalating twice must be stable: the second escalation must not pick a "
        "different model from the first"
    )


# ---------------------------------------------------------------------------
# PRESERVE: the escalation DECISION logic is untouched
# ---------------------------------------------------------------------------


def test_auto_escalation_enabled_logic_unchanged(monkeypatch):
    monkeypatch.delenv("PIPELINE_AUTO_ESCALATE", raising=False)

    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    assert escalation._auto_escalation_enabled() is True

    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    assert escalation._auto_escalation_enabled() is False

    monkeypatch.setenv("PIPELINE_AUTO_ESCALATE", "1")
    assert escalation._auto_escalation_enabled() is True

    monkeypatch.setenv("PIPELINE_AUTO_ESCALATE", "0")
    assert escalation._auto_escalation_enabled() is False
