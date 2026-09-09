"""Regression test for PP-02 review Blocking Finding 1.

`_effective_dispatch_check` (pipeline/preflight.py) reports the dispatch
role's resolution via `app.role_registry.resolve_role`, which falls through
to model_registry.json's `roles.dispatch` entry when PIPELINE_BACKEND_
DISPATCH is unset. But real per-story dispatch execution NEVER consults
that registry key — every real call site (pipeline/dispatch.py:291-294,
advance.py:311-312, escalation.py:272) resolves the backend by reading
PIPELINE_BACKEND_DISPATCH directly, defaulting to "claude" when unset, and
only consults the registry (a *different* key, routing.dispatch, via a
*different* function, _route_dispatch_backend in pipeline/usage.py) when the
env var is literally "auto".

So: env unset + registry routes dispatch to ollama + ollama CLI present +
claude CLI absent must still report "claude" as the backend that will
actually run, and it must FAIL (claude CLI missing) — never green-light
ollama, which will never be dispatched to in this scenario.

This test pins that exact false-green scenario from the review. It must
fail on the current (pre-fix) code, which reports status "ok" naming
"ollama" instead.
"""

from __future__ import annotations

from app import role_registry
from pipeline import preflight


def _registry_routing_dispatch_to_ollama():
    """Synthetic registry payload: roles.dispatch points at ollama."""
    return {
        "providers": {
            "claude": {"models": {"sonnet": {"tag": "claude-sonnet-4"}}},
            "ollama": {
                "models": {"glm-5.3-flash:cloud": {"tag": "glm-5.3-flash:cloud"}}
            },
        },
        "roles": {
            "overlord": {"provider": "claude", "model": "sonnet"},
            "dispatch": {"provider": "ollama", "model": "glm-5.3-flash:cloud"},
        },
    }


def _find_dispatch(results):
    matches = [
        check
        for check in results
        if "dispatch" in str(check.get("name", "")).lower()
    ]
    assert matches, (
        f"no dispatch check in names {[check.get('name') for check in results]}"
    )
    return matches[0]


def test_env_unset_registry_routes_to_ollama_but_real_dispatch_uses_claude(
    tmp_path, monkeypatch
):
    """The scenario from the review: registry says ollama, but claude is
    what pipeline/dispatch.py will actually try to run (env unset -> default
    "claude"). Preflight must fail naming claude, not pass naming ollama.
    """
    monkeypatch.setattr(
        role_registry,
        "load_registry",
        _registry_routing_dispatch_to_ollama,
    )
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)

    def which(name):
        # ollama's CLI is present on this host; claude's is not.
        if name == "ollama":
            return "/fake/bin/ollama"
        return None

    results = preflight.run_preflight(plan_dir=tmp_path, which=which)
    check = _find_dispatch(results)

    # Real dispatch execution (pipeline/dispatch.py:291-294) will resolve
    # env_backend="claude" here (env unset -> default), and the claude CLI
    # is missing, so this must be a fail naming claude -- never an "ok" for
    # ollama, which real dispatch will never actually invoke.
    assert check["status"] == "fail", (
        "preflight reported a false green: it approved 'ollama' (from the "
        "registry) while real dispatch execution will actually try 'claude' "
        f"(env unset -> default) and find it missing. Got: {check!r}"
    )
    assert "claude" in check["message"].lower()
