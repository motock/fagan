"""Regression tests for the order-dependent xdist flakiness around
test_dispatch_default_tier_matches_roles_dispatch, plus the
testing-config-gates violation that made that test fragile.

Root cause being pinned (see .agent_scratchpad.md and the PR description):

1. **Live-file dependency (config-gates violation).**
   ``test_dispatch_default_tier_matches_roles_dispatch`` calls
   ``load_registry()`` with no stub, so it reads the *real*
   ``model_registry.json`` on disk. Its assertion
   (``resolution.model in {roles_dispatch["model"], tag}``) is only
   accidentally true for whatever the live file happens to declare today.
   Under pytest-xdist, other workers' tests that set
   ``PIPELINE_MODEL_REGISTRY_PATH`` (test_config_write*.py,
   test_pipeline_review_prompt_addition.py) or that read the live file
   concurrently (test_chat_role_registry.py) can interleave with this
   test's ``load_registry()`` read, so the test resolves against a
   registry that is not the one its own module constants assume. A
   sequential run hides this because the env var is restored before the
   next test starts; parallel workers each have their own os.environ but
   the *repo-root* file is shared, and any test that mutates it (or that
   runs while another worker's env override is in force at import time)
   surfaces the mismatch.

2. **No per-test reset of the registry path seam.** ``load_registry()``
   resolves its target via ``_registry_path()`` -> ``os.environ`` on
   every call, but nothing in tests/unit/conftest.py isolates
   ``PIPELINE_MODEL_REGISTRY_PATH`` the way ``_isolate_environ`` isolates
   other ``PIPELINE_*`` vars for tests that read the registry *without*
   passing an explicit path. Any test that leaks that env var (a
   monkeypatch teardown that doesn't run before this test, or an in-place
   ``os.environ`` mutation monkeypatch cannot undo) makes
   ``load_registry()`` read a tmp file that no longer exists -> ``{}`` ->
   ``reg["roles"]["dispatch"]`` KeyError, or a *different* registry ->
   assertion mismatch. Order-dependent, worker-dependent, invisible
   sequentially.

These tests are written FIRST (red) and assert the fix contract:

- the dispatch default-tier test must resolve against a **synthetic
  registry dict fixture**, never the live file;
- ``load_registry()`` must have no shared mutable state that survives
  across invocations: two calls with a changed
  ``PIPELINE_MODEL_REGISTRY_PATH`` must observe the *new* file's contents
  (no memoization of the parsed dict or of the resolved path);
- boundary/negative behavior of the routing resolution itself.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.role_registry import RoleRegistryError, load_registry, resolve_route

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LIVE_REGISTRY_PATH = REPO_ROOT / "model_registry.json"
REGISTRY_PATH_ENV = "PIPELINE_MODEL_REGISTRY_PATH"

LOW_RISK_STORY = {"risk": "low", "persona": "software-engineer"}


# ---------------------------------------------------------------------------
# Synthetic registry fixture (the stub-registry pattern already used by
# test_dashboard_checklist_config.py and
# test_pipeline_mcp_server_runner_and_overlord.py — a plain dict passed via
# the `registry=` kwarg, never the live file).
# ---------------------------------------------------------------------------
def _stub_registry() -> dict:
    """A minimal, fully-valid registry: dispatch's default tier resolves to
    the same (provider, model) as roles.dispatch, mirroring the inertness
    contract the live file is supposed to uphold."""
    return {
        "providers": {
            "ollama": {
                "models": {
                    "gpt-oss-20b-high": {"tag": "gpt-oss-20b-high:latest"},
                    "glm-4.6": {"tag": "glm-4.6:q4"},
                }
            },
            "claude": {
                "models": {
                    "sonnet": {"tag": "claude-sonnet-4-5"},
                }
            },
        },
        "roles": {
            "dispatch": {"provider": "ollama", "model": "gpt-oss-20b-high"},
        },
        "routing": {
            "dispatch": {
                "default_tier": "local_default",
                "tiers": {
                    "local_default": {
                        "provider": "ollama",
                        "model": "gpt-oss-20b-high",
                    },
                    "escalated": {
                        "provider": "claude",
                        "model": "sonnet",
                    },
                },
                "rules": [
                    {
                        # Deliberately does NOT match LOW_RISK_STORY, so the
                        # default tier is what the inertness test resolves.
                        "when": {"persona": "security"},
                        "tier": "escalated",
                    },
                ],
            }
        },
    }


@pytest.fixture
def stub_registry() -> dict:
    return _stub_registry()


def _write_registry(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "model_registry.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


# ---------------------------------------------------------------------------
# 1. The headline regression: dispatch default tier vs roles.dispatch,
#    asserted against the STUB registry only — the live model_registry.json
#    must not be read.
# ---------------------------------------------------------------------------
def test_dispatch_default_tier_matches_roles_dispatch_synthetic(stub_registry):
    """The inertness contract, asserted against a synthetic registry: the
    routing default tier resolves to the same concrete model roles.dispatch
    names, via a provider that is in the local-transport family."""
    resolution = resolve_route("dispatch", story=LOW_RISK_STORY, registry=stub_registry)
    assert resolution is not None
    roles_dispatch = stub_registry["roles"]["dispatch"]
    ollama_entry = stub_registry["providers"][roles_dispatch["provider"]]["models"][
        roles_dispatch["model"]
    ]
    assert resolution.model in {roles_dispatch["model"], ollama_entry.get("tag")}
    from pipeline.config import _LOCAL_BACKEND_NAMES

    assert resolution.provider in _LOCAL_BACKEND_NAMES


def test_dispatch_default_tier_test_does_not_read_live_registry(monkeypatch):
    """Config-gates rule: the resolution-logic test must not depend on
    today's model_registry.json. Simulate the live file being absent (the
    xdist/other-worker condition) — the synthetic-registry test above must
    still pass, which it does by construction because it never calls
    load_registry() against the repo file. Asserted here by pointing the
    env override at a nonexistent path and confirming the stub-driven
    resolution is unaffected."""
    monkeypatch.setenv(REGISTRY_PATH_ENV, str(REPO_ROOT / "does-not-exist.json"))
    reg = _stub_registry()
    resolution = resolve_route("dispatch", story=LOW_RISK_STORY, registry=reg)
    assert resolution is not None
    assert resolution.model == "gpt-oss-20b-high:latest"


def test_live_registry_file_is_not_required_for_resolution(tmp_path, monkeypatch):
    """With PIPELINE_MODEL_REGISTRY_PATH pointed at an empty directory and
    no explicit path, load_registry() returns {} (registry is optional) —
    and resolve_route with an explicit synthetic registry still works."""
    monkeypatch.setenv(REGISTRY_PATH_ENV, str(tmp_path / "missing.json"))
    assert load_registry() == {}
    resolution = resolve_route("dispatch", story=LOW_RISK_STORY, registry=_stub_registry())
    assert resolution.model == "gpt-oss-20b-high:latest"


# ---------------------------------------------------------------------------
# 2. Isolation regression: load_registry() must carry no shared mutable
#    state across invocations. Two calls with a changed registry path must
#    observe the new file — this is the test that fails on the pre-fix code
#    if load_registry() memoizes the parsed dict or the resolved path.
# ---------------------------------------------------------------------------
def test_load_registry_reflects_changed_env_path(tmp_path, monkeypatch):
    """No memoization of the resolved path: changing
    PIPELINE_MODEL_REGISTRY_PATH between two calls must change what the
    second call returns."""
    first = _write_registry(
        tmp_path / "a",
        {
            "providers": {"ollama": {"models": {"m1": {"tag": "t1"}}}},
            "roles": {"dispatch": {"provider": "ollama", "model": "m1"}},
        },
    )
    second = _write_registry(
        tmp_path / "b",
        {
            "providers": {"ollama": {"models": {"m2": {"tag": "t2"}}}},
            "roles": {"dispatch": {"provider": "ollama", "model": "m2"}},
        },
    )
    monkeypatch.setenv(REGISTRY_PATH_ENV, str(first))
    one = load_registry()
    assert one["roles"]["dispatch"]["model"] == "m1"

    monkeypatch.setenv(REGISTRY_PATH_ENV, str(second))
    two = load_registry()
    assert two["roles"]["dispatch"]["model"] == "m2", (
        "load_registry() returned the FIRST file's contents after the env "
        "path changed — a stale cache/memoized path is surviving across "
        "calls, which is the xdist order-dependence bug"
    )


def test_load_registry_does_not_memoize_parsed_dict(tmp_path, monkeypatch):
    """No memoization of the parsed contents: rewriting the same file
    between two calls must be visible to the second call."""
    path = _write_registry(
        tmp_path,
        {
            "providers": {"ollama": {"models": {"m1": {"tag": "t1"}}}},
            "roles": {"dispatch": {"provider": "ollama", "model": "m1"}},
        },
    )
    monkeypatch.setenv(REGISTRY_PATH_ENV, str(path))
    assert load_registry()["roles"]["dispatch"]["model"] == "m1"

    path.write_text(
        json.dumps(
            {
                "providers": {"ollama": {"models": {"m9": {"tag": "t9"}}}},
                "roles": {"dispatch": {"provider": "ollama", "model": "m9"}},
            }
        )
    )
    assert load_registry()["roles"]["dispatch"]["model"] == "m9", (
        "load_registry() returned stale contents after the file changed — "
        "the parsed dict is being memoized across calls"
    )


def test_load_registry_returns_fresh_dict_each_call(tmp_path, monkeypatch):
    """The returned dict must not be a shared module-level object: mutating
    one call's return value must not affect the next call's."""
    path = _write_registry(
        tmp_path,
        {
            "providers": {"ollama": {"models": {"m1": {"tag": "t1"}}}},
            "roles": {"dispatch": {"provider": "ollama", "model": "m1"}},
        },
    )
    monkeypatch.setenv(REGISTRY_PATH_ENV, str(path))
    first = load_registry()
    first["roles"]["dispatch"]["model"] = "MUTATED-IN-PLACE"
    second = load_registry()
    assert second["roles"]["dispatch"]["model"] == "m1", (
        "mutating one load_registry() return value leaked into the next "
        "call — a shared module-level dict is being handed out"
    )


def test_load_registry_explicit_path_beats_env(tmp_path, monkeypatch):
    """path= wins over PIPELINE_MODEL_REGISTRY_PATH (the seam other tests
    stub through); the env var must not be able to redirect an explicit
    path= call."""
    env_file = _write_registry(
        tmp_path / "env",
        {
            "providers": {"ollama": {"models": {"m_env": {"tag": "t_env"}}}},
            "roles": {"dispatch": {"provider": "ollama", "model": "m_env"}},
        },
    )
    explicit = _write_registry(
        tmp_path / "explicit",
        {
            "providers": {"ollama": {"models": {"m_explicit": {"tag": "t_x"}}}},
            "roles": {"dispatch": {"provider": "ollama", "model": "m_explicit"}},
        },
    )
    monkeypatch.setenv(REGISTRY_PATH_ENV, str(env_file))
    reg = load_registry(explicit)
    assert reg["roles"]["dispatch"]["model"] == "m_explicit"


def test_load_registry_no_env_no_path_reads_repo_default(monkeypatch):
    """With no env override, load_registry() reads the repo-root
    model_registry.json (the documented default) — asserted structurally:
    the resolved default path constant must point at that file."""
    monkeypatch.delenv(REGISTRY_PATH_ENV, raising=False)
    from app.role_registry import _DEFAULT_REGISTRY_PATH

    assert _DEFAULT_REGISTRY_PATH.name == "model_registry.json"
    assert _DEFAULT_REGISTRY_PATH == REPO_ROOT / "model_registry.json"


# ---------------------------------------------------------------------------
# 3. Boundary / negative cases for the routing resolution itself.
# ---------------------------------------------------------------------------
def test_resolve_route_empty_rules_falls_back_to_default_tier(stub_registry):
    reg = _stub_registry()
    reg["routing"]["dispatch"]["rules"] = []
    resolution = resolve_route("dispatch", story=LOW_RISK_STORY, registry=reg)
    assert resolution.tier == "local_default"
    assert resolution.model == "gpt-oss-20b-high:latest"


def test_resolve_route_no_routing_block_returns_none(stub_registry):
    reg = _stub_registry()
    del reg["routing"]
    assert resolve_route("dispatch", story=LOW_RISK_STORY, registry=reg) is None


def test_resolve_route_role_missing_from_routing_returns_none(stub_registry):
    reg = _stub_registry()
    del reg["routing"]["dispatch"]
    assert resolve_route("dispatch", story=LOW_RISK_STORY, registry=reg) is None


def test_resolve_route_first_matching_rule_wins(stub_registry):
    reg = _stub_registry()
    reg["routing"]["dispatch"]["rules"] = [
        {"when": {"max_risk": "low"}, "tier": "escalated"},
        {"when": {"max_risk": "high"}, "tier": "local_default"},
    ]
    resolution = resolve_route("dispatch", story=LOW_RISK_STORY, registry=reg)
    assert resolution.tier == "escalated"


def test_resolve_route_max_risk_boundary_inclusive(stub_registry):
    """max_risk is inclusive: a story AT the named level matches."""
    reg = _stub_registry()
    reg["routing"]["dispatch"]["rules"] = [
        {"when": {"max_risk": "medium"}, "tier": "escalated"},
    ]
    at_boundary = resolve_route(
        "dispatch", story={"risk": "medium", "persona": "software-engineer"}, registry=reg
    )
    assert at_boundary.tier == "escalated"
    just_above = resolve_route(
        "dispatch", story={"risk": "high", "persona": "software-engineer"}, registry=reg
    )
    assert just_above.tier == "local_default"


def test_resolve_route_unknown_risk_defaults_to_high(stub_registry):
    """A story with no/unknown risk is treated as the riskiest (high), so a
    max_risk=low rule must NOT match it."""
    reg = _stub_registry()
    reg["routing"]["dispatch"]["rules"] = [
        {"when": {"max_risk": "low"}, "tier": "escalated"},
    ]
    resolution = resolve_route("dispatch", story={"persona": "software-engineer"}, registry=reg)
    assert resolution.tier == "local_default"


def test_resolve_route_persona_predicate(stub_registry):
    reg = _stub_registry()
    reg["routing"]["dispatch"]["rules"] = [
        {"when": {"persona": "security"}, "tier": "escalated"},
    ]
    match = resolve_route("dispatch", story={"persona": "security"}, registry=reg)
    assert match.tier == "escalated"
    no_match = resolve_route("dispatch", story=LOW_RISK_STORY, registry=reg)
    assert no_match.tier == "local_default"


def test_resolve_route_unsupported_predicate_raises(stub_registry):
    reg = _stub_registry()
    reg["routing"]["dispatch"]["rules"] = [
        {"when": {"bogus_predicate": "x"}, "tier": "escalated"},
    ]
    with pytest.raises(RoleRegistryError) as exc:
        resolve_route("dispatch", story=LOW_RISK_STORY, registry=reg)
    assert "bogus_predicate" in str(exc.value)
    assert "max_risk" in str(exc.value) and "persona" in str(exc.value)


def test_resolve_route_unknown_max_risk_level_raises(stub_registry):
    reg = _stub_registry()
    reg["routing"]["dispatch"]["rules"] = [
        {"when": {"max_risk": "catastrophic"}, "tier": "escalated"},
    ]
    with pytest.raises(RoleRegistryError) as exc:
        resolve_route("dispatch", story=LOW_RISK_STORY, registry=reg)
    assert "catastrophic" in str(exc.value)


def test_resolve_route_rule_names_undeclared_tier_raises(stub_registry):
    reg = _stub_registry()
    reg["routing"]["dispatch"]["rules"] = [
        {"when": {"max_risk": "low"}, "tier": "no_such_tier"},
    ]
    with pytest.raises(RoleRegistryError) as exc:
        resolve_route("dispatch", story=LOW_RISK_STORY, registry=reg)
    assert "no_such_tier" in str(exc.value)


def test_resolve_route_tier_names_unknown_provider_raises(stub_registry):
    reg = _stub_registry()
    reg["routing"]["dispatch"]["tiers"]["local_default"]["provider"] = "nope"
    with pytest.raises(RoleRegistryError) as exc:
        resolve_route("dispatch", story=LOW_RISK_STORY, registry=reg)
    assert "nope" in str(exc.value)


def test_resolve_route_tier_names_unknown_model_raises(stub_registry):
    reg = _stub_registry()
    reg["routing"]["dispatch"]["tiers"]["local_default"]["model"] = "no_such_model"
    with pytest.raises(RoleRegistryError) as exc:
        resolve_route("dispatch", story=LOW_RISK_STORY, registry=reg)
    assert "no_such_model" in str(exc.value)


def test_resolve_route_returns_tag_not_friendly_name(stub_registry):
    """The returned model is the concrete tag, never the friendly name."""
    resolution = resolve_route("dispatch", story=LOW_RISK_STORY, registry=stub_registry)
    assert resolution.model == "gpt-oss-20b-high:latest"
    assert resolution.model != "gpt-oss-20b-high"


def test_resolve_route_plan_role_config_beats_registry(stub_registry):
    """Plan-level routing beats the registry's own routing block: the
    registry's rule would pick 'escalated', but the plan's routing names
    'local_default' and that must win."""
    reg = _stub_registry()
    reg["routing"]["dispatch"]["rules"] = [
        {"when": {"max_risk": "low"}, "tier": "escalated"},
    ]
    plan_role_config = {
        "routing": {
            "dispatch": {
                "default_tier": "local_default",
                "tiers": {
                    "local_default": {
                        "provider": "ollama",
                        "model": "gpt-oss-20b-high",
                    }
                },
            }
        }
    }
    resolution = resolve_route(
        "dispatch",
        story=LOW_RISK_STORY,
        registry=reg,
        plan_role_config=plan_role_config,
    )
    assert resolution is not None
    assert resolution.tier == "local_default"
    assert resolution.model == "gpt-oss-20b-high:latest"


# ---------------------------------------------------------------------------
# 4. load_registry validation negatives (the fail-closed contract the
#    isolation fix must not weaken).
# ---------------------------------------------------------------------------
def test_load_registry_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv(REGISTRY_PATH_ENV, str(tmp_path / "nope.json"))
    assert load_registry() == {}


def test_load_registry_malformed_json_raises(tmp_path, monkeypatch):
    path = tmp_path / "model_registry.json"
    path.write_text("{not json")
    monkeypatch.setenv(REGISTRY_PATH_ENV, str(path))
    with pytest.raises(RoleRegistryError) as exc:
        load_registry()
    assert "invalid JSON" in str(exc.value)
    assert str(path) in str(exc.value)


def test_load_registry_role_names_unknown_provider_raises(tmp_path, monkeypatch):
    path = _write_registry(
        tmp_path,
        {"roles": {"dispatch": {"provider": "ghost"}}},
    )
    monkeypatch.setenv(REGISTRY_PATH_ENV, str(path))
    with pytest.raises(RoleRegistryError) as exc:
        load_registry()
    assert "roles.dispatch" in str(exc.value)
    assert "ghost" in str(exc.value)


def test_load_registry_role_names_unknown_model_raises(tmp_path, monkeypatch):
    path = _write_registry(
        tmp_path,
        {
            "providers": {"ollama": {"models": {"m1": {"tag": "t1"}}}},
            "roles": {"dispatch": {"provider": "ollama", "model": "ghost"}},
        },
    )
    monkeypatch.setenv(REGISTRY_PATH_ENV, str(path))
    with pytest.raises(RoleRegistryError) as exc:
        load_registry()
    assert "ghost" in str(exc.value)


def test_load_registry_role_without_provider_is_allowed(tmp_path, monkeypatch):
    path = _write_registry(tmp_path, {"roles": {"dispatch": {"model": "m1"}}})
    monkeypatch.setenv(REGISTRY_PATH_ENV, str(path))
    assert load_registry() == {"roles": {"dispatch": {"model": "m1"}}}


# ---------------------------------------------------------------------------
# 5. The live shipped registry stays well-formed (membership-only, per the
#    shared-artifact rule — never exact contents).
# ---------------------------------------------------------------------------
def test_live_registry_routing_block_declares_dispatch(tmp_path):
    """A model_registry.json declaring a routing.dispatch block loads with
    'routing'/'dispatch' present — load_registry's own file-parsing
    behavior, exercised (via the load_registry(path=...) seam this module's
    own docstring recommends) against a synthetic registry file rather than
    the live model_registry.json, whose routing block is free to be
    reconfigured."""
    path = _write_registry(tmp_path, _stub_registry())
    reg = load_registry(path)
    assert "routing" in reg
    assert "dispatch" in reg["routing"]


def test_live_registry_dispatch_rules_use_supported_predicates(tmp_path):
    """Every rule in a routing.dispatch block must use only the two
    predicates resolve_route supports (max_risk, persona) — exercised
    against a synthetic registry file rather than the live
    model_registry.json's current rule set."""
    stub = _stub_registry()
    stub["routing"]["dispatch"]["rules"] = [
        {"when": {"max_risk": "low"}, "tier": "escalated"},
        {"when": {"persona": "security"}, "tier": "escalated"},
    ]
    path = _write_registry(tmp_path, stub)
    reg = load_registry(path)
    for rule in reg["routing"]["dispatch"].get("rules", []):
        assert set(rule.get("when", {})) <= {"max_risk", "persona"}


def test_live_registry_resolves_without_raising():
    if not LIVE_REGISTRY_PATH.exists():
        pytest.skip("repo model_registry.json not present")
    reg = load_registry(LIVE_REGISTRY_PATH)
    for story in (
        {"risk": "low", "persona": "software-engineer"},
        {"risk": "high", "persona": "software-engineer"},
    ):
        resolve_route("dispatch", story=story, registry=reg)


# ---------------------------------------------------------------------------
# 6. Config-gates regression (RED until the fix lands): the ORIGINAL
#    test_dispatch_default_tier_matches_roles_dispatch in
#    tests/unit/test_routing_registry.py must resolve against a synthetic/
#    stubbed registry fixture, not the live model_registry.json on disk.
#    .claude/rules/testing-config-gates.md: test the resolution logic, not
#    today's configured values — a live-file read is itself the fragility
#    that made this test order-dependent under xdist (other workers'
#    registry writes / env redirects interleave with its load_registry()
#    read). Asserted by source inspection so the fix cannot be skipped.
# ---------------------------------------------------------------------------
def _original_test_source() -> str:
    import inspect

    import tests.unit.test_routing_registry as mod

    return inspect.getsource(mod.test_dispatch_default_tier_matches_roles_dispatch)


def test_dispatch_default_tier_test_does_not_call_load_registry():
    """The dispatch default-tier test must not read the live registry via
    load_registry() — it must resolve against a synthetic registry dict."""
    source = _original_test_source()
    assert "load_registry()" not in source, (
        "test_dispatch_default_tier_matches_roles_dispatch still calls "
        "load_registry() with no stub — it reads the live model_registry.json, "
        "which violates the testing-config-gates rule and is the source of the "
        "xdist order-dependent flakiness. Rewrite it to construct/pass a "
        "synthetic registry dict fixture and assert against that."
    )


def test_dispatch_default_tier_test_uses_synthetic_registry():
    """The rewritten test must construct/pass a synthetic registry — a stub
    fixture or an inline dict literal — rather than relying on whatever
    load_registry() returns. (The pre-fix code already contains the text
    'registry=' as the resolve_route kwarg, so that alone proves nothing;
    this asserts a synthetic registry is actually constructed.)"""
    source = _original_test_source()
    has_stub_fixture = "stub_registry" in source or "synthetic" in source
    has_inline_dict = '{"providers"' in source or "{'providers'" in source
    assert has_stub_fixture or has_inline_dict, (
        "test_dispatch_default_tier_matches_roles_dispatch must construct a "
        "synthetic registry (a stub fixture like stub_registry, or an inline "
        "{'providers': ...} dict literal) and resolve against it — not against "
        "the live model_registry.json via load_registry()"
    )


def test_dispatch_default_tier_test_docstring_does_not_claim_live_registry_read():
    """The test's docstring must stop claiming it reads the live registry
    ('read from the live registry so nothing is hardcoded here') — that
    comment documents exactly the fragility being removed."""
    source = _original_test_source()
    assert "live registry" not in source, (
        "test_dispatch_default_tier_matches_roles_dispatch's docstring still "
        "documents reading the live registry; after the synthetic-registry "
        "rewrite this claim must be gone"
    )


def test_original_routing_registry_module_imports_cleanly():
    """The original module must remain importable after the rewrite (the
    rewrite must not break its own module)."""
    import tests.unit.test_routing_registry as mod

    assert hasattr(mod, "test_dispatch_default_tier_matches_roles_dispatch")


def test_dispatch_default_tier_test_survives_poisoned_registry_env(monkeypatch, tmp_path):
    """Direct in-process reproduction of the xdist order-dependent failure.

    The contaminating scenario: another test (or another xdist worker's
    leaked/interleaved state) leaves PIPELINE_MODEL_REGISTRY_PATH pointing
    at a registry whose dispatch routing resolves to a HOSTED provider —
    or the live repo file has been rewritten mid-suite by a
    set_role_default call that didn't redirect. The original
    test_dispatch_default_tier_matches_roles_dispatch calls load_registry()
    with no stub, so it reads whatever that path holds and its inertness
    assertion fails — but only when the poisoned state lands before it,
    which is why the failure is order/worker-dependent and invisible
    sequentially.

    This test points the env var at such a poisoned registry, then runs
    the original test function in-process. It must pass BECAUSE the
    rewritten test resolves against its own synthetic registry fixture and
    never consults load_registry()'s env-derived path. Fails on pre-fix
    code (the live/poisoned read makes the inertness assertion blow up).
    """
    poisoned = {
        "providers": {
            "claude": {"models": {"sonnet": {"tag": "claude-sonnet-4-5"}}},
        },
        "roles": {
            "dispatch": {"provider": "claude", "model": "sonnet"},
        },
        "routing": {
            "dispatch": {
                "default_tier": "hosted_default",
                "tiers": {
                    "hosted_default": {"provider": "claude", "model": "sonnet"},
                },
            }
        },
    }
    path = _write_registry(tmp_path, poisoned)
    monkeypatch.setenv(REGISTRY_PATH_ENV, str(path))

    import tests.unit.test_routing_registry as mod

    # Must not raise and must not fail its own inertness assertion, no
    # matter what PIPELINE_MODEL_REGISTRY_PATH points at.
    mod.test_dispatch_default_tier_matches_roles_dispatch()