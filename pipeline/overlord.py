"""Overlord / decision helpers for the pipeline MCP server.

_load_policy concatenates the global decision policy (POLICY_PATH) with any
per-repo override (REPO_ROOT/.overlord-policy.md). _invoke_overlord runs the
overlord persona headless via the configured Backend.

Both read server module-level globals (POLICY_PATH, REPO_ROOT) that tests
patch via p.<name>; the moved code reads them via lazy imports from the
server module (circular-avoidance - the server imports pipeline_overlord at
top level). Tests that patch p._invoke_overlord keep working because the
server call site (request_decision) uses the bare name, which resolves
through the re-export to the patched binding.
"""

from app import backend, role_registry

from .persona import _persona_body, _persona_default_model
from .role_routing_scope import layer_routing_kwargs


def _load_policy() -> str:
    """Concatenate the global decision policy with any per-repo override."""
    # Lazy import: POLICY_PATH / REPO_ROOT are server module-level globals
    # patched by tests via p.<name>; reading them here at call time sees the
    # patched value. The server imports this module at top level, so a
    # module-load import would cycle.
    from .server import POLICY_PATH, REPO_ROOT
    parts = []
    if POLICY_PATH.exists():
        parts.append(POLICY_PATH.read_text())
    override = REPO_ROOT / ".overlord-policy.md"
    if override.exists():
        parts.append("\n\n## Per-repository override\n\n" + override.read_text())
    return "\n".join(parts)


def _invoke_overlord(
    prompt: str,
    plan_role_config: dict | None = None,
    layer_routing: dict | None = None,
) -> str:
    """Run the overlord persona headless and return its raw stdout.

    External boundary: delegates to the configured Backend. Tests mock this
    function. Provider/model fall through role_registry (PIPELINE_BACKEND_
    OVERLORD / a plan's role_config / model_registry.json's "overlord"
    entry), falling back to the persona's declared tier ("opus") when none
    of those apply - so an unconfigured install resolves identically to
    before role_registry existed. Passing name=resolution.provider
    explicitly (rather than relying on get_backend's own internal env
    lookup, as before) is required so a registry/plan-configured provider
    actually takes effect.

    ``layer_routing`` is the team's ``role_routing`` value for the caller's
    (namespace, team, repo) scope. The CALLER resolves it once per call (via
    ``pipeline.role_routing_scope``) and hands it here; the overlord only
    forwards it to ``resolve_role`` as its own argument - it is never looked up
    here and never merged into ``plan_role_config``, whose pairing rules differ.
    An unset scope adds no keyword at all, so the everyday call keeps its exact
    pre-existing shape.
    """
    system = _persona_body("overlord")
    resolution = role_registry.resolve_role(
        "overlord", plan_role_config=plan_role_config,
        model_fallback=lambda: _persona_default_model("overlord") or "opus",
        **layer_routing_kwargs(layer_routing),
    )
    return backend.get_backend("overlord", name=resolution.provider).complete(
        prompt, system=system, model=resolution.model, allowed_tools="Read",
    )


__all__ = [
    "_invoke_overlord",
    "_load_policy",
]