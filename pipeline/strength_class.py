"""Classify a dispatch target as "capable" or "on-device".

A *capable* dispatch target is a Claude-class model that does not need the
sizing crutch `.claude/rules/agent-dispatch-story-sizing.md` prescribes for
small on-device models (the 2-production-file cap). An *on-device* target is
everything else -- including anything unrecognised, because the stricter class
is the safe default: capping a capable model costs a little convenience,
failing to cap an on-device one produces an unworkable story.

Derivation, in order of precedence:

1. an explicit ``"class"`` of exactly ``"capable"``/``"on-device"`` on the
   registry's ``providers.<provider>.models.<name>`` entry (any other value is
   ignored and logged once, never guessed at);
2. the provider -- ``claude`` and ``litellm`` are capable;
3. an ``ollama`` model tag ending ``:cloud`` is capable (cloud weights run on
   hardware that is not the local box, mirroring
   ``pipeline.ingest._on_device_route_target``'s definition of on-device);
4. everything else is on-device.
"""
from __future__ import annotations

import logging
import os

CAPABLE = "capable"
ON_DEVICE = "on-device"

_CAPABLE_PROVIDERS = frozenset({"claude", "litellm"})
_CLOUD_TAG_SUFFIX = ":cloud"
_VALID_CLASSES = (CAPABLE, ON_DEVICE)
_ROLE = "dispatch"

# Once-only WARNING for a registry entry carrying an unrecognised class value:
# one bad entry must not spam a log on every story, and the value itself is
# never echoed into the message (it is operator-supplied data, not a fact).
_INVALID_ENTRY_CLASS_WARNED = False

logger = logging.getLogger(__name__)


def strength_class(
    provider: str, model_tag: str | None, entry_class: str | None = None
) -> str:
    """Return the strength class for *provider* dispatching to *model_tag*.

    An explicit *entry_class* of exactly ``"capable"`` or ``"on-device"`` wins
    outright, in both directions. Any other non-None value is ignored (not an
    error) and logged once per process; derivation then proceeds as if it were
    absent. Never raises.
    """
    global _INVALID_ENTRY_CLASS_WARNED

    if entry_class is not None:
        if entry_class in _VALID_CLASSES:
            return str(entry_class)
        if not _INVALID_ENTRY_CLASS_WARNED:
            _INVALID_ENTRY_CLASS_WARNED = True
            logger.warning(
                "model registry entry has an unrecognised strength class; "
                "treating the model as on-device"
            )

    provider_name = str(provider or "").strip().lower()
    if provider_name in _CAPABLE_PROVIDERS:
        return CAPABLE
    tag = str(model_tag or "").strip().lower()
    if provider_name == "ollama" and tag.endswith(_CLOUD_TAG_SUFFIX):
        return CAPABLE
    return ON_DEVICE


def _registry_entry(
    registry: dict, provider: str, model_name_or_tag: str
) -> dict | None:
    """Return ``providers.<provider>.models.<name>`` for *model_name_or_tag*.

    The name may be the model's friendly name (the key under ``models``) or its
    concrete ``tag``. Returns None when the registry is missing/malformed or the
    model is not declared. Never raises.
    """
    if not isinstance(registry, dict) or not isinstance(model_name_or_tag, str):
        return None
    provider_name = str(provider or "").strip().lower()
    if not provider_name:
        return None
    try:
        providers = registry.get("providers")
        if not isinstance(providers, dict):
            return None
        provider_entry = providers.get(provider_name)
        if not isinstance(provider_entry, dict):
            return None
        models = provider_entry.get("models")
        if not isinstance(models, dict):
            return None
        entry = models.get(model_name_or_tag)
        if isinstance(entry, dict):
            return entry
        # Fall back to matching the concrete tag (e.g. "glm-5.3-flash:cloud").
        for candidate in models.values():
            if isinstance(candidate, dict) and candidate.get("tag") == model_name_or_tag:
                return candidate
    except (AttributeError, TypeError, ValueError):
        return None
    return None


def registry_entry_class(
    registry: dict, provider: str, model_name_or_tag: str
) -> str | None:
    """Return the declared ``class`` for a registry model entry, else None.

    Lookup is by friendly name first, then by concrete tag. An entry with no
    ``class`` (or a non-string one) yields None, letting the caller derive the
    class from the provider and tag. Never raises.
    """
    entry = _registry_entry(registry, provider, model_name_or_tag)
    if entry is None:
        return None
    declared = entry.get("class")
    return declared if isinstance(declared, str) else None


def _resolve_model(
    registry: dict, provider: str, model_name_or_tag: str | None
) -> tuple[str | None, str | None]:
    """Resolve *model_name_or_tag* to ``(tag, declared_class)``.

    A friendly name resolves to the entry's concrete ``tag``; an unregistered
    value is used verbatim as a raw tag (a story may pin a tag the registry has
    never heard of, and that must still classify by its ``:cloud`` suffix).
    """
    if model_name_or_tag is None:
        return None, None
    value = str(model_name_or_tag).strip()
    if not value:
        return None, None
    entry = _registry_entry(registry, provider, value)
    if entry is None:
        return value, None
    tag = entry.get("tag")
    declared = entry.get("class")
    return (
        tag if isinstance(tag, str) and tag else value,
        declared if isinstance(declared, str) else None,
    )


def _load_registry() -> dict:
    """Load ``model_registry.json``; any failure degrades to an empty registry."""
    try:
        from app.role_registry import load_registry

        return load_registry() or {}
    except Exception:  # noqa: BLE001 - fail closed to the stricter class
        return {}


def dispatch_strength_class(
    story: dict,
    plan_role_config: dict | None,
    registry: dict | None = None,
    environ: dict | None = None,
) -> str:
    """Return the strength class of the model *story* will actually dispatch to.

    Provider precedence is copied verbatim from
    ``pipeline.ingest._preflight_dispatch_provider`` (not imported, to avoid a
    cycle; keep the two in step):

        story["backend"]        -> plan role_config.dispatch.provider
        -> PIPELINE_BACKEND_DISPATCH env var -> registry roles.dispatch.provider
        -> "claude"

    Model precedence:

        story["model"]          -> plan role_config.dispatch.model
        -> registry roles.dispatch.model -> PIPELINE_LOCAL_MODEL_DEFAULT

    Each model value is a friendly name resolved through the registry when
    declared, else used as a raw tag. ``auto`` is not a capable provider and
    resolves like any other unrecognised string, so ``auto`` with no resolvable
    model is on-device -- the stricter class. Never raises: any lookup failure
    yields on-device.
    """
    try:
        env = os.environ if environ is None else environ
        if not isinstance(env, dict):
            env = {}
        story = story if isinstance(story, dict) else {}
        role_config = plan_role_config if isinstance(plan_role_config, dict) else {}
        dispatch_cfg = role_config.get(_ROLE)
        dispatch_cfg = dispatch_cfg if isinstance(dispatch_cfg, dict) else {}
        reg = registry if isinstance(registry, dict) else _load_registry()
        if not isinstance(reg, dict):
            reg = {}
        roles = reg.get("roles")
        roles = roles if isinstance(roles, dict) else {}
        registry_role = roles.get(_ROLE)
        registry_role = registry_role if isinstance(registry_role, dict) else {}

        provider = ""
        for candidate in (
            story.get("backend"),
            dispatch_cfg.get("provider"),
            env.get("PIPELINE_BACKEND_DISPATCH"),
            registry_role.get("provider"),
        ):
            if candidate:
                provider = str(candidate).strip().lower()
                break
        if not provider:
            provider = "claude"

        model_value = None
        for candidate in (
            story.get("model"),
            dispatch_cfg.get("model"),
            registry_role.get("model"),
            env.get("PIPELINE_LOCAL_MODEL_DEFAULT"),
        ):
            if candidate:
                model_value = candidate
                break

        tag, declared = _resolve_model(reg, provider, model_value)
        return strength_class(provider, tag, declared)
    except Exception:  # noqa: BLE001 - an observability hook must never gate
        logger.debug("strength class derivation failed; assuming on-device")
        return ON_DEVICE
