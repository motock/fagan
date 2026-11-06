"""Decide which story-sizing caps apply, by strength class, not backend name.

`.claude/rules/agent-dispatch-story-sizing.md` prescribes two caps for an
on-device dispatch tier: at most 2 production files in scope, and no production
file over ~1000 lines. The file-COUNT cap is a crutch for small models with a
limited hold-in-head budget; a dispatch target whose registry entry declares
``"class": "capable"`` does not need it. The file-SIZE cap still applies to
every class -- a 1200-line file is a problem for any model.

Until now `pipeline.ingest._story_sizing_warning` decided all of this from a
bare backend-name tuple, so a provider declared capable was still capped to 2
files. This module holds that decision instead:

* ``sizing_applies`` -- is the story sized at all? True for the pre-existing
  backend set (``local``/``ollama``/``lmstudio``/``mlx``/``litellm``/``auto``,
  notably excluding ``claude``) or for any provider declared ``on-device``.
* ``needs_small_story_caps`` -- does the 2-production-file cap apply? False for
  a ``capable`` target, True for an ``on-device`` one, and the old backend-name
  answer when the class is unset or unrecognised.

The class itself is the ``"class"`` field documented for a registry entry
(``REFERENCE.md``, per-model ``class``): an explicit value on the provider's own
entry wins, else the effective dispatch model's entry is consulted. Derivation
by provider name / ``:cloud`` tag is deliberately NOT repeated here -- an unset
class must keep the old backend-name behaviour, not tighten it.

Neither function ever raises: this gates an advisory, non-blocking observability
hook, so an unreadable registry degrades to the old backend-name behaviour.
"""
from __future__ import annotations

import os

from .strength_class import registry_entry_class

CAPABLE = "capable"
ON_DEVICE = "on-device"

# The backend-name gate this decision replaced in
# ``pipeline.ingest._story_sizing_warning``: a story on one of these backends
# was sized, anything else (``claude`` in particular) was left alone.
OLD_SMALL_STORY_BACKENDS = frozenset(
    {"local", "ollama", "lmstudio", "mlx", "litellm", "auto"}
)

_PROVIDER_ENV = "PIPELINE_BACKEND_DISPATCH"
_MODEL_ENV = "PIPELINE_LOCAL_MODEL_DEFAULT"
_ROLE = "dispatch"
# ``pipeline.ingest._preflight_dispatch_provider`` falls back to "claude" when
# nothing declares a provider; mirroring it keeps the two in step.
_DEFAULT_PROVIDER = "claude"


class _UnreadableRegistry(Exception):
    """The registry could not be read; callers degrade to backend names."""


def _normalise(value) -> str:
    """Lower-case and strip a provider/class value; anything odd is empty."""
    return str(value or "").strip().lower()


def _payload(registry) -> dict:
    """Return the registry mapping for *registry*.

    *registry* may be an already-loaded mapping or anything exposing
    ``load_registry()`` (the ``app.role_registry`` module, or ``ingest``'s
    ``_ServerRef`` to it). Raises ``_UnreadableRegistry`` when it is neither, so
    the caller can fall back -- a mapping is never silently ignored.
    """
    if isinstance(registry, dict):
        return registry
    loader = getattr(registry, "load_registry", None)
    if loader is None:
        raise _UnreadableRegistry("no registry loader")
    payload = loader()
    if not isinstance(payload, dict):
        raise _UnreadableRegistry("registry payload is not a mapping")
    return payload


def _dispatch_role(payload: dict) -> dict:
    roles = payload.get("roles")
    role = roles.get(_ROLE) if isinstance(roles, dict) else None
    return role if isinstance(role, dict) else {}


def _effective_provider(story, payload: dict) -> str:
    """Provider precedence copied from ``ingest._preflight_dispatch_provider``.

    ``story["backend"]`` -> ``PIPELINE_BACKEND_DISPATCH`` ->
    ``registry roles.dispatch.provider`` -> ``"claude"``. In the ingest path the
    story's backend has already been stamped with the effective tier by
    ``_sizing_view``; the rest keeps direct calls honest.
    """
    backend = story.get("backend") if isinstance(story, dict) else None
    for candidate in (backend, os.environ.get(_PROVIDER_ENV),
                      _dispatch_role(payload).get("provider")):
        if candidate:
            return _normalise(candidate)
    return _DEFAULT_PROVIDER


def _effective_model(story, payload: dict) -> str | None:
    """Model precedence: ``story["model"]`` -> registry role -> host default."""
    model = story.get("model") if isinstance(story, dict) else None
    for candidate in (model, _dispatch_role(payload).get("model"),
                      os.environ.get(_MODEL_ENV)):
        if candidate:
            return str(candidate)
    return None


def _declared_class(payload: dict, provider: str, model: str | None) -> str | None:
    """Return the class declared for *provider*, else for its effective model.

    An explicit value on the provider's own entry wins outright; otherwise the
    per-model entry documented in ``REFERENCE.md`` is consulted. Anything that
    is not ``"capable"``/``"on-device"`` is returned as its normalised string so
    the caller can treat it as unrecognised. Never raises.
    """
    providers = payload.get("providers")
    entry = providers.get(provider) if isinstance(providers, dict) else None
    if isinstance(entry, dict):
        declared = entry.get("class")
        if isinstance(declared, str):
            return _normalise(declared)
    if model:
        declared = registry_entry_class(payload, provider, model)
        if declared:
            return _normalise(declared)
    return None


def _old_backend(story) -> bool:
    """The pre-existing answer: is the story's own backend in the old set?"""
    backend = story.get("backend") if isinstance(story, dict) else None
    if not _normalise(backend):
        backend = os.environ.get(_PROVIDER_ENV)
    return _normalise(backend) in OLD_SMALL_STORY_BACKENDS


def _decide(story, registry) -> tuple[bool, bool]:
    """Return ``(sizing_applies, needs_small_story_caps)``; may raise."""
    payload = _payload(registry)
    provider = _effective_provider(story, payload)
    old = provider in OLD_SMALL_STORY_BACKENDS
    declared = _declared_class(payload, provider, _effective_model(story, payload))
    if declared == CAPABLE:
        # A capable target loses the count cap but keeps the size cap.
        return old, False
    if declared == ON_DEVICE:
        # An explicit on-device class is sized even on a provider the old
        # backend-name tuple would have ignored.
        return True, True
    # Unset, unrecognised or non-string class: exactly the old behaviour.
    return old, old


def sizing_applies(story: dict, registry) -> bool:
    """Return True when *story* is sized at all; never raises.

    True for a story on one of the old backend names, and for any story whose
    effective provider declares ``"class": "on-device"``. A ``claude``-class
    dispatch target is not sized (only the size cap would ever apply to it and
    the old gate never inspected it), which keeps today's silence intact.
    """
    try:
        return _decide(story, registry)[0]
    except Exception:  # noqa: BLE001 - advisory hook: degrade, never gate
        return _old_backend(story)


def needs_small_story_caps(story: dict, registry) -> bool:
    """Return True when the 2-production-file cap applies; never raises.

    False for a ``"class": "capable"`` dispatch target; True for an
    ``"class": "on-device"`` one; otherwise the old backend-name answer (True
    for ``local``/``ollama``/``lmstudio``/``mlx``/``litellm``/``auto``).
    An unreadable registry or a malformed story degrades to that same old
    answer, so the sizing notice stays advisory.
    """
    try:
        return _decide(story, registry)[1]
    except Exception:  # noqa: BLE001 - advisory hook: degrade, never gate
        return _old_backend(story)
