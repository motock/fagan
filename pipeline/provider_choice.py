"""Pure provider-choice model for the pipeline's role routing.

This module holds NO side effects: no prompting, no console output, no file
writes, no network.  It is a pure projection over two arguments:

``effective_config``
    The dict ``PipelineService`` reports for the current configuration.  Its
    ``"roles"`` key holds one entry per role in the shape
    ``pipeline.config_provenance.resolve_role_provenance`` emits
    (``role``, ``provider``, ``model``, ``provider_source``,
    ``model_source``, ``restart_required``, ``error``).

``registry``
    The dict ``app.role_registry`` loads from ``model_registry.json``:
    ``{"providers": {name: {"models": {logical_name: {...}}}}, "roles": {...}}``.

The single public function, :func:`build_choice_model`, returns one dict per
pipeline role (in :data:`PIPELINE_ROLES` order) with:

``role``
    The role name.
``current``
    ``{"provider": ..., "model": ..., "source": ...}`` as the effective
    configuration resolved it, or all-``None`` when the role has no entry.
``options``
    Every provider/model pair the registry argument declares, sorted by
    provider name then model name.
``error``
    The inline misconfiguration message the effective configuration carried
    for the role, or ``None``.

Both inputs are arguments only: this module never reads configuration or the
model registry itself, so callers (and tests) pass synthetic fixtures.
"""
from __future__ import annotations

from pipeline.config_provenance import PIPELINE_ROLES


def _collect_options(registry: dict) -> list[dict]:
    """Every declared provider/model pair, sorted by (provider, model).

    Tolerates a missing, empty, or non-mapping ``providers`` block, a
    provider entry without a ``models`` mapping, and an empty ``models``
    mapping - each contributes no options rather than raising.
    """
    providers = registry.get("providers") if isinstance(registry, dict) else None
    if not isinstance(providers, dict):
        return []
    options: list[dict] = []
    for provider_name, spec in providers.items():
        models = spec.get("models") if isinstance(spec, dict) else None
        if not isinstance(models, dict):
            continue
        for model_name in models:
            options.append({"provider": provider_name, "model": model_name})
    options.sort(key=lambda option: (option["provider"], option["model"]))
    return options


def build_choice_model(effective_config: dict, registry: dict) -> list[dict]:
    """One dict per pipeline role: its current routing plus declared options.

    Iterates :data:`PIPELINE_ROLES` (never the effective-config argument's
    keys) so the output order is stable regardless of how the caller built
    the input.  A role absent from the effective-config argument still gets
    an entry, with an all-``None`` ``current``; an inline ``error`` on an
    entry is surfaced on the returned dict, not raised.
    """
    entries_by_role: dict = {}
    roles = effective_config.get("roles") if isinstance(effective_config, dict) else None
    if isinstance(roles, (list, tuple)):
        for entry in roles:
            if isinstance(entry, dict) and entry.get("role") is not None:
                entries_by_role[entry["role"]] = entry

    options = _collect_options(registry)

    result: list[dict] = []
    for role in PIPELINE_ROLES:
        entry = entries_by_role.get(role)
        if entry is None:
            current = {"provider": None, "model": None, "source": None}
            error = None
        else:
            current = {
                "provider": entry.get("provider"),
                "model": entry.get("model"),
                "source": entry.get("provider_source") or entry.get("model_source"),
            }
            error = entry.get("error")
        result.append(
            {
                "role": role,
                "current": current,
                "options": list(options),
                "error": error,
            }
        )
    return result