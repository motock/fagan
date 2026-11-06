"""Decide which story-sizing caps apply, by strength class, not backend name.

`.claude/rules/agent-dispatch-story-sizing.md` prescribes two caps for an
on-device dispatch tier: at most 2 production files in scope, and no production
file over ~1000 lines. The file-COUNT cap is a crutch for small models with a
limited hold-in-head budget; a dispatch target the strength classifier calls
``capable`` (``claude``, ``litellm``, an ``ollama`` model tagged ``:cloud``, or a
registry entry declaring ``"class": "capable"``) does not need it. The file-SIZE
cap still applies to every class -- a 1200-line file is a problem for any model.

The class is read, never re-derived: the ``class`` a
``providers.<name>.models.<model>`` entry declares is looked up with
`pipeline.strength_class`'s ``registry_entry_class`` and interpreted with its
``strength_class``, so there is no second private notion of what a declared
class means. Only an EXPLICIT ``capable``/``on-device`` changes the answer; an
unset (or unrecognised) class keeps the pre-existing backend-name behaviour, so
`pipeline.strength_class`'s name/``:cloud`` derivation of capability does not
silently widen or narrow this advisory. That mirrors
`pipeline.sizing_by_class`, which is where the ingest path lifts the count cap
for a derived-capable target (``class_aware_sizing_warning``): the base hook
this module gates keeps today's message, and the wrapper filters it.

* ``sizing_applies`` -- is the story sized at all? Exactly the pre-existing
  backend set (``local``/``ollama``/``lmstudio``/``mlx``/``litellm``/``auto``,
  notably excluding ``claude``), so nothing that used to be silent starts
  warning.
* ``needs_small_story_caps`` -- does the 2-production-file cap apply? False for
  a ``capable`` class, True for an entry explicitly declaring ``on-device``, and
  the old backend-name answer when the class is unset or unrecognised.

Neither function ever raises: this gates an advisory, non-blocking observability
hook, so an unreadable registry degrades to the old backend-name behaviour.
"""
from __future__ import annotations

import os

from .strength_class import (
    CAPABLE,
    ON_DEVICE,
    registry_entry_class,
    strength_class,
)

# The backend-name gate this decision replaced in
# ``pipeline.ingest._story_sizing_warning``: a story on one of these backends
# was sized, anything else (``claude`` in particular) was left alone.
OLD_SMALL_STORY_BACKENDS = frozenset(
    {"local", "ollama", "lmstudio", "mlx", "litellm", "auto"}
)

_PROVIDER_ENV = "PIPELINE_BACKEND_DISPATCH"
_MODEL_ENV = "PIPELINE_LOCAL_MODEL_DEFAULT"
_ROLE = "dispatch"
# Only an explicit value from this set moves the sizing answer; every other
# value (including a malformed one) falls back to the old backend-name rule.
_DECLARED_CLASSES = frozenset({CAPABLE, ON_DEVICE})
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


def _story_backend(story) -> str:
    """The story's own stamped dispatch backend, normalised (``""`` if none)."""
    return _normalise(story.get("backend") if isinstance(story, dict) else None)


def _old_backend(story) -> bool:
    """The pre-existing answer: is the story's own backend in the old set?"""
    backend = _story_backend(story) or _normalise(os.environ.get(_PROVIDER_ENV))
    return backend in OLD_SMALL_STORY_BACKENDS


def _needs_caps(story, payload: dict) -> bool:
    """Return the 2-file-cap answer for a readable *payload*; may raise."""
    provider = _effective_provider(story, payload)
    old = provider in OLD_SMALL_STORY_BACKENDS
    model = _effective_model(story, payload)
    declared = registry_entry_class(payload, provider, model) if model else None
    if declared in _DECLARED_CLASSES:
        # Interpret the declared class with `pipeline.strength_class`, so there
        # is one definition of what "capable"/"on-device" means. Only an
        # EXPLICIT class moves the answer: a class derived from the provider
        # name or an ollama `:cloud` tag keeps the old backend-name behaviour
        # here, because the ingest notice goes through
        # `pipeline.sizing_by_class.class_aware_sizing_warning`, which lifts the
        # count cap for derived-capable targets. Widening this hook instead
        # would change the message of every existing :cloud story.
        return strength_class(provider, model, declared) == ON_DEVICE
    # Unset or unrecognised class: exactly the old backend-name behaviour.
    return old


def sizing_applies(story: dict, registry) -> bool:
    """Return True when *story* is sized at all; never raises.

    True for a story on one of the old backend names and False otherwise, which
    is the pre-existing gate: ``claude``-class dispatch is not sized, and an
    explicitly on-device provider outside the old set is not newly warned about.
    The file-size cap inside that gate applies to every class.
    """
    backend = _story_backend(story)
    if backend:
        # The ingest path always stamps the story's backend, so the common case
        # needs no registry read at all.
        return backend in OLD_SMALL_STORY_BACKENDS
    try:
        provider = _effective_provider(story, _payload(registry))
    except Exception:  # noqa: BLE001 - advisory hook: degrade, never gate
        return _old_backend(story)
    return provider in OLD_SMALL_STORY_BACKENDS


def needs_small_story_caps(story: dict, registry) -> bool:
    """Return True when the 2-production-file cap applies; never raises.

    False for a ``capable`` class (``claude``, ``litellm``, an ``ollama``
    ``:cloud`` tag, or an explicit ``"class": "capable"`` entry); True for an
    entry explicitly declaring ``"class": "on-device"``; otherwise the old
    backend-name answer (True for
    ``local``/``ollama``/``lmstudio``/``mlx``/``litellm``/``auto``).
    An unreadable registry or a malformed story degrades to that same old
    answer, so the sizing notice stays advisory.
    """
    try:
        return _needs_caps(story, _payload(registry))
    except Exception:  # noqa: BLE001 - advisory hook: degrade, never gate
        return _old_backend(story)
