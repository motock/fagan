"""Regression test for PP-02 review Blocking Finding 1 - re-pinned by REG-3.

HISTORY - this file used to pin the OPPOSITE of what it now asserts.
`_effective_dispatch_check` in pipeline/preflight.py originally reported the
dispatch role's resolution via `app.role_registry.resolve_role`, which falls
through to model_registry.json's `roles.dispatch` entry when
PIPELINE_BACKEND_DISPATCH is unset. At the time, real per-story dispatch
execution read PIPELINE_BACKEND_DISPATCH directly and NEVER consulted that
registry key, so preflight green-lit a provider dispatch would never invoke.
The PP-02 review resolved that divergence by changing the REPORTER to match
the broken resolution: preflight read the env var raw, and this file pinned
that - asserting that env unset + registry routing dispatch to ollama must
still report "claude" and FAIL.

The two prerequisite stories (REG-1, REG-2) made that premise false: dispatch
and escalation now resolve through `role_registry.resolve_role` and DO
consult the registry (see pipeline/dispatch.py's `_resolve_dispatch_target`).
The original instinct - report via resolve_role - was right; only the
resolution underneath it was broken, and that is now fixed. So this file is
re-pinned rather than deleted: the same scenario now reports OLLAMA (the
backend that will actually run) and PASSES the CLI check.

It is still a parity guard, and a stronger one than before: the assertions
below compare preflight's reported backend against pipeline/dispatch.py's OWN
resolver for the same inputs instead of hardcoding a provider name, so
preflight and real dispatch can never drift apart again without this file
going red.
"""

from __future__ import annotations

from app import role_registry
from pipeline import dispatch, preflight


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


def test_env_unset_registry_routes_to_ollama_reports_ollama_and_passes(
    tmp_path, monkeypatch
):
    """The scenario from the review, re-pinned: env unset, registry routes
    dispatch to ollama, ollama's CLI present, claude's CLI absent.

    Preflight must report OLLAMA - the backend real dispatch will actually
    run - and PASS the CLI check. It previously asserted "claude" and a FAIL,
    because dispatch used to ignore the registry; that premise is gone.
    """
    monkeypatch.setattr(
        role_registry,
        "load_registry",
        _registry_routing_dispatch_to_ollama,
    )
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)

    probed = []

    def which(name):
        # ollama's CLI is present on this host; claude's is not.
        probed.append(name)
        if name == "ollama":
            return "/fake/bin/ollama"
        return None

    results = preflight.run_preflight(plan_dir=tmp_path, which=which)
    check = _find_dispatch(results)

    # Parity anchor: what does real dispatch resolve for this same config?
    # (Sanity-check the scenario itself, so this test cannot go vacuous if
    # dispatch's resolver ever changes shape.)
    expected_provider, _expected_model = dispatch._resolve_dispatch_target({}, None)
    assert expected_provider == "ollama", (
        "scenario drift: pipeline/dispatch.py no longer resolves this config "
        f"to ollama (got {expected_provider!r}); the review's scenario is no "
        "longer the one being pinned"
    )

    assert check["status"] == "ok", (
        "preflight did not pass the CLI check for the backend real dispatch "
        f"will run ({expected_provider!r}). Got: {check!r}"
    )
    assert expected_provider in check["message"].lower(), (
        f"preflight reported {check['message']!r} but pipeline/dispatch.py "
        f"resolves this story to {expected_provider!r}"
    )
    assert "ollama" in probed, probed
    assert "claude" not in probed, (
        "preflight probed claude's CLI, which real dispatch will never "
        f"invoke here (probed {probed!r})"
    )
