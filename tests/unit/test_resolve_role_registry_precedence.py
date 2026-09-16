"""Precedence tests for app.role_registry.resolve_role (REG-4).

MAINTAINER DECISION (2026-09-16): model_registry.json is the single source of
truth for role routing. The role-specific PIPELINE_BACKEND_<ROLE> env var is
demoted to the EMPTY-STATE fallback: it is consulted only when the registry
has no entry for that role, so a fresh clone (whose shipped
model_registry.json has no `roles` block at all) still boots.

New provider order, top to bottom:

    story-level override (handled by callers)
    -> plan_role_config[role]["provider"]
    -> registry["roles"][role]["provider"]
    -> PIPELINE_BACKEND_<ROLE> env var
    -> default_provider

Every test here passes synthetic `registry` and `environ` dicts straight into
resolve_role (both are explicit keyword arguments) — os.environ is never
monkeypatched, and no file on disk is read.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.role_registry import RoleRegistryError, resolve_role

# A minimal, self-contained provider catalogue: every model name used below is
# declared here, so a RoleRegistryError can only come from the typo guard.
_PROVIDERS = {
    "claude": {"models": {"sonnet": {"tag": "claude-sonnet-4-5"}}},
    "ollama": {"models": {"llama3": {"tag": "llama3:8b"}}},
    "mlx": {"models": {"qwen": {"tag": "mlx-community/Qwen2.5"}}},
}


def _registry(roles: dict | None = None) -> dict:
    """A synthetic registry. `roles=None` reproduces the shipped fresh-clone
    shape: a providers block with no `roles` key at all."""
    reg: dict = {"providers": _PROVIDERS}
    if roles is not None:
        reg["roles"] = roles
    return reg


# ---------------------------------------------------------------------------
# 1. POSITIVE: the registry outranks the role env var
# ---------------------------------------------------------------------------

def test_registry_provider_outranks_role_env_var():
    """Registry pins planner -> ollama while PIPELINE_BACKEND_PLANNER names
    claude: the REGISTRY wins (this is the whole point of REG-4)."""
    reg = _registry(roles={"planner": {"provider": "ollama", "model": "llama3"}})
    res = resolve_role(
        "planner",
        registry=reg,
        environ={"PIPELINE_BACKEND_PLANNER": "claude"},
    )
    assert res.provider == "ollama", (
        "registry['roles']['planner']['provider'] must outrank "
        "PIPELINE_BACKEND_PLANNER"
    )


def test_registry_provider_outranks_env_var_for_every_role():
    """The swap is in the shared chain, not special-cased per role."""
    reg = _registry(
        roles={
            "planner": {"provider": "ollama"},
            "dispatch": {"provider": "mlx"},
            "review": {"provider": "ollama"},
        }
    )
    environ = {
        "PIPELINE_BACKEND_PLANNER": "claude",
        "PIPELINE_BACKEND_DISPATCH": "claude",
        "PIPELINE_BACKEND_REVIEW": "claude",
    }
    assert resolve_role(
        "planner", registry=reg, environ=environ, model_fallback=lambda: "llama3"
    ).provider == "ollama"
    assert resolve_role(
        "dispatch", registry=reg, environ=environ, model_fallback=lambda: "qwen"
    ).provider == "mlx"
    assert resolve_role(
        "review", registry=reg, environ=environ, model_fallback=lambda: "llama3"
    ).provider == "ollama"


# ---------------------------------------------------------------------------
# 2. BOUNDARY (empty state): env var is the fallback when the registry is silent
# ---------------------------------------------------------------------------

def test_env_var_wins_when_registry_has_no_roles_block():
    """Fresh-clone path: the shipped model_registry.json has no `roles` block
    at all, so PIPELINE_BACKEND_<ROLE> must still be honored."""
    reg = _registry()  # no "roles" key whatsoever
    res = resolve_role(
        "planner",
        registry=reg,
        environ={"PIPELINE_BACKEND_PLANNER": "ollama"},
        model_fallback=lambda: "llama3",
    )
    assert res.provider == "ollama"


def test_env_var_wins_when_registry_roles_block_lacks_the_role():
    """Same empty-state path, but the roles block exists and simply has no
    entry for this role."""
    reg = _registry(roles={"dispatch": {"provider": "mlx"}})
    res = resolve_role(
        "planner",
        registry=reg,
        environ={"PIPELINE_BACKEND_PLANNER": "ollama"},
        model_fallback=lambda: "llama3",
    )
    assert res.provider == "ollama"


def test_env_var_wins_when_registry_role_entry_has_no_provider():
    """A roles entry that only pins a model leaves the provider slot empty,
    so the env var still supplies the provider."""
    reg = _registry(roles={"planner": {"model": "llama3"}})
    res = resolve_role(
        "planner",
        registry=reg,
        environ={"PIPELINE_BACKEND_PLANNER": "ollama"},
    )
    assert res.provider == "ollama"
    assert res.model == "llama3:8b"


# ---------------------------------------------------------------------------
# 3. BOUNDARY: neither registry nor env -> default_provider
# ---------------------------------------------------------------------------

def test_default_provider_when_neither_registry_nor_env_names_one():
    res = resolve_role(
        "planner",
        registry=_registry(),
        environ={},
        default_provider="mlx",
        model_fallback=lambda: "qwen",
    )
    assert res.provider == "mlx"


def test_default_provider_is_used_when_registry_role_entry_has_no_provider():
    res = resolve_role(
        "planner",
        registry=_registry(roles={"planner": {"model": "qwen"}}),
        environ={},
        default_provider="mlx",
    )
    assert res.provider == "mlx"
    assert res.model == "mlx-community/Qwen2.5"


def test_default_provider_is_normalized():
    res = resolve_role(
        "planner",
        registry=_registry(),
        environ={},
        default_provider="  MLX  ",
        model_fallback=lambda: "qwen",
    )
    assert res.provider == "mlx"


# ---------------------------------------------------------------------------
# 4. NEGATIVE CONTROL: plan role_config still beats the registry
# ---------------------------------------------------------------------------

def test_plan_role_config_still_outranks_registry():
    reg = _registry(roles={"planner": {"provider": "ollama", "model": "llama3"}})
    res = resolve_role(
        "planner",
        plan_role_config={"planner": {"provider": "claude"}},
        registry=reg,
        environ={"PIPELINE_BACKEND_PLANNER": "mlx"},
        model_fallback=lambda: "sonnet",
    )
    assert res.provider == "claude", "plan role_config must stay top of the chain"
    # The registry's model belongs to the registry's own (ollama) provider, so
    # it must NOT be applied to the plan-overridden claude provider.
    assert res.model == "sonnet"


def test_plan_role_config_model_outranks_registry_model():
    reg = _registry(roles={"planner": {"provider": "ollama", "model": "llama3"}})
    res = resolve_role(
        "planner",
        plan_role_config={"planner": {"provider": "ollama", "model": "llama3"}},
        registry=reg,
        environ={},
    )
    assert res.provider == "ollama"
    assert res.model == "llama3:8b"


# ---------------------------------------------------------------------------
# 5. The registry's MODEL is honored when its provider wins
# ---------------------------------------------------------------------------

def test_registry_model_is_honored_when_registry_provider_wins():
    reg = _registry(roles={"planner": {"provider": "ollama", "model": "llama3"}})
    res = resolve_role(
        "planner",
        registry=reg,
        environ={"PIPELINE_BACKEND_PLANNER": "claude"},
        model_fallback=lambda: "sonnet",
    )
    assert res.provider == "ollama"
    assert res.model == "llama3:8b", (
        "the registry's model must be resolved through "
        "providers.ollama.models['llama3'].tag when its provider wins"
    )


def test_registry_model_is_honored_when_env_supplies_the_provider():
    """Registry pins only a model; the env var supplies the provider. The
    registry model is still applied (its provider slot was empty)."""
    reg = _registry(roles={"planner": {"model": "llama3"}})
    res = resolve_role(
        "planner",
        registry=reg,
        environ={"PIPELINE_BACKEND_PLANNER": "ollama"},
        model_fallback=lambda: "sonnet",
    )
    assert res.provider == "ollama"
    assert res.model == "llama3:8b"


# ---------------------------------------------------------------------------
# 6. The typo guard survives the swap
# ---------------------------------------------------------------------------

def test_registry_model_not_declared_under_its_provider_raises():
    reg = _registry(roles={"planner": {"provider": "ollama", "model": "llama3-typo"}})
    with pytest.raises(RoleRegistryError) as excinfo:
        resolve_role("planner", registry=reg, environ={})
    message = str(excinfo.value)
    assert "llama3-typo" in message
    assert "providers.ollama.models" in message


def test_registry_model_typo_raises_even_when_env_names_another_provider():
    """The registry provider wins, so the registry model is validated against
    the registry provider — the env var cannot mask the typo."""
    reg = _registry(roles={"planner": {"provider": "ollama", "model": "llama3-typo"}})
    with pytest.raises(RoleRegistryError) as excinfo:
        resolve_role(
            "planner",
            registry=reg,
            environ={"PIPELINE_BACKEND_PLANNER": "claude"},
        )
    assert "providers.ollama.models" in str(excinfo.value)


def test_registry_model_typo_raises_when_env_supplies_the_provider():
    """Registry pins only a (typo'd) model; the env-won provider must still be
    validated against it."""
    reg = _registry(roles={"planner": {"model": "llama3-typo"}})
    with pytest.raises(RoleRegistryError) as excinfo:
        resolve_role(
            "planner",
            registry=reg,
            environ={"PIPELINE_BACKEND_PLANNER": "ollama"},
        )
    assert "providers.ollama.models" in str(excinfo.value)


def test_plan_model_typo_raises_against_the_winning_provider():
    reg = _registry()
    with pytest.raises(RoleRegistryError) as excinfo:
        resolve_role(
            "planner",
            plan_role_config={"planner": {"provider": "claude", "model": "llama3"}},
            registry=reg,
            environ={},
        )
    assert "providers.claude.models" in str(excinfo.value)


# ---------------------------------------------------------------------------
# The resolve_role docstring must document the NEW order (docs/code agreement)
# ---------------------------------------------------------------------------

def test_resolve_role_docstring_states_new_provider_order():
    doc = resolve_role.__doc__ or ""
    assert "Provider priority:" in doc, "resolve_role must keep documenting its chain"
    start = doc.index("Provider priority:")
    end = doc.index("Model priority:", start)
    paragraph = doc[start:end]
    i_plan = paragraph.index("plan_role_config")
    i_registry = paragraph.index("registry")
    i_env = paragraph.index("PIPELINE_BACKEND_")
    i_default = paragraph.index("default_provider")
    assert i_plan < i_registry < i_env < i_default, (
        "resolve_role's 'Provider priority:' paragraph must read "
        "plan role_config -> registry roles -> PIPELINE_BACKEND_<ROLE> env -> "
        f"default_provider; got:\n{paragraph}"
    )


def test_resolve_role_docstring_no_longer_claims_env_outranks_registry():
    """The paragraph explaining when the registry's model pairing is ignored
    must describe the new order: only plan role_config can now outrank the
    registry's provider, so the stale '(plan/env)' phrasing must be gone."""
    doc = resolve_role.__doc__ or ""
    assert "(plan/env)" not in doc, (
        "resolve_role's docstring still claims the env var outranks the "
        "registry's provider; with the new order only plan role_config can "
        "override the registry's provider"
    )
    assert "Model priority:" in doc, (
        "resolve_role must keep documenting its model chain"
    )
    pairing_paragraph = doc[doc.index("Model priority:"):]
    assert "plan" in pairing_paragraph, (
        "the paragraph explaining when the registry's model pairing is "
        "ignored must still name plan role_config as the overriding source"
    )


def test_registry_provider_wins_with_no_registry_model_uses_fallback():
    """Registry pins only a provider; the caller's model_fallback supplies the
    model, resolved against the registry-won provider."""
    reg = _registry(roles={"planner": {"provider": "ollama"}})
    res = resolve_role(
        "planner",
        registry=reg,
        environ={"PIPELINE_BACKEND_PLANNER": "claude"},
        model_fallback=lambda: "llama3",
    )
    assert res.provider == "ollama"
    assert res.model == "llama3:8b"


# ---------------------------------------------------------------------------
# The documented chain must match the code (README + REFERENCE)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_README = _REPO_ROOT / "README.md"
_REFERENCE = _REPO_ROOT / "REFERENCE.md"


def _section_body(text: str, heading: str) -> str:
    """Body of the first heading line whose text is `heading` (any level),
    through the line before the next heading of the same-or-higher level."""
    lines = text.splitlines()
    level = None
    start = None
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") and stripped.lstrip("#").strip() == heading:
            level = len(stripped) - len(stripped.lstrip("#"))
            start = idx
            break
    assert start is not None, f"expected a heading {heading!r}"
    for idx in range(start + 1, len(lines)):
        stripped = lines[idx].strip()
        if stripped.startswith("#") and (len(stripped) - len(stripped.lstrip("#"))) <= level:
            return "\n".join(lines[start:idx])
    return "\n".join(lines[start:])


def _numbered_list_items(section_body: str) -> list[str]:
    """The first contiguous `1.`/`2.`/... list in `section_body`, one string
    per item (continuation lines folded in)."""
    items: list[str] = []
    current: list[str] = []
    started = False
    for line in section_body.splitlines():
        if re.match(r"^\s*\d+\.\s", line):
            if current:
                items.append(" ".join(current))
            current = [line.strip()]
            started = True
        elif started and line.strip():
            current.append(line.strip())
        elif started and not line.strip():
            break
    if current:
        items.append(" ".join(current))
    return items


def test_readme_documents_registry_before_env_var():
    """README's 'Provider selection & authorization' list must present the
    registry `roles` block above the PIPELINE_BACKEND_<ROLE> env var, with
    plan role_config still first and the caller's fallback last."""
    body = _section_body(_README.read_text(), "Provider selection & authorization")
    items = _numbered_list_items(body)
    assert len(items) >= 4, (
        "expected README's 'Provider selection & authorization' section to "
        f"keep its numbered resolution-order list; got {items!r}"
    )

    def _index_of(*needles: str) -> int:
        for i, item in enumerate(items):
            if any(n in item for n in needles):
                return i
        return -1

    i_plan = _index_of("role_config", "role config")
    i_registry = _index_of("roles", "registry")
    i_env = _index_of("PIPELINE_BACKEND_")
    i_fallback = _index_of("fallback")
    assert -1 not in (i_plan, i_registry, i_env, i_fallback), (
        "expected all four resolution-order markers (role_config, registry "
        "roles, PIPELINE_BACKEND_<ROLE>, fallback) in README's numbered list; "
        f"got items={items!r}"
    )
    assert i_plan < i_registry < i_env < i_fallback, (
        "README's documented order must be plan role_config -> registry roles "
        "-> PIPELINE_BACKEND_<ROLE> env -> fallback; got "
        f"plan={i_plan} registry={i_registry} env={i_env} "
        f"fallback={i_fallback} in {items!r}"
    )


def test_reference_documents_registry_before_env_var():
    """REFERENCE.md's 'Per-role provider/model configuration' resolution
    priority list must present the registry above the env var."""
    body = _section_body(_REFERENCE.read_text(), "Per-role provider/model configuration")
    items = _numbered_list_items(body)
    assert len(items) >= 4, (
        "expected REFERENCE.md's 'Per-role provider/model configuration' "
        f"section to keep its numbered resolution-priority list; got {items!r}"
    )

    def _index_of(*needles: str) -> int:
        for i, item in enumerate(items):
            if any(n in item for n in needles):
                return i
        return -1

    i_plan = _index_of("role_config")
    i_registry = _index_of("roles.", "roles<", "registry")
    i_env = _index_of("PIPELINE_BACKEND_")
    i_fallback = _index_of("default", "fallback")
    assert -1 not in (i_plan, i_registry, i_env, i_fallback), (
        "expected all four resolution-order markers (role_config, registry "
        "roles, PIPELINE_BACKEND_<ROLE>, default/fallback) in REFERENCE.md's "
        f"numbered list; got items={items!r}"
    )
    assert i_plan < i_registry < i_env < i_fallback, (
        "REFERENCE.md's documented order must be plan role_config -> registry "
        "roles -> PIPELINE_BACKEND_<ROLE> env -> default/fallback; got "
        f"plan={i_plan} registry={i_registry} env={i_env} "
        f"fallback={i_fallback} in {items!r}"
    )
