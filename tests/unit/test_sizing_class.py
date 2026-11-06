"""Tests for the strength-class decision behind the ingest sizing caps.

`pipeline.sizing_class.needs_small_story_caps` is the tier decision that used to
be a bare backend-name tuple inside `pipeline.ingest._story_sizing_warning`. The
count cap is a crutch for small on-device models, so a provider whose model entry
explicitly declares ``"class": "capable"`` is not capped on file count, while one
declaring ``"class": "on-device"`` is. An unset or unrecognised class keeps the
pre-existing backend-name behaviour: the declared value is read and interpreted
through `pipeline.strength_class`, never re-derived here, so this hook cannot
diverge from it about what a declared class *means*.

`pipeline.sizing_class.sizing_applies` is the wider scope question, narrowed to
the pre-existing gate: only the local/cloud-open backends are sized at all, so
no provider that used to be silent starts emitting sizing warnings.

Nothing here may ever raise: this gates an advisory observability hook, so an
unreadable registry degrades to the old backend-name behaviour.
"""

from __future__ import annotations

import pytest

from app import role_registry
from pipeline.sizing_class import (
    CAPABLE,
    ON_DEVICE,
    needs_small_story_caps,
    sizing_applies,
)
from pipeline.strength_class import strength_class

OLD_BACKEND = "ollama"
NON_OLD_BACKEND = "vllm"

THREE_PRODUCTION_FILES = ["app/a.py", "app/b.py", "app/c.py"]


@pytest.fixture(autouse=True)
def _isolate_dispatch_env(monkeypatch):
    """Keep a developer's exported dispatch env out of these tests."""
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_MODEL_DEFAULT", raising=False)


def _registry(
    provider: str, declared_class: str | None = None, tag: str = "m:latest"
) -> dict:
    """A synthetic registry declaring *provider* with one model entry.

    ``class`` goes where `REFERENCE.md` documents it: on the
    ``providers.<name>.models.<model>`` entry.
    """
    model: dict = {"tag": tag}
    if declared_class is not None:
        model["class"] = declared_class
    return {"providers": {provider: {"models": {"m": model}}}}


def _story(backend: str | None, model: str = "m") -> dict:
    story = {"model": model, "files": list(THREE_PRODUCTION_FILES)}
    if backend is not None:
        story["backend"] = backend
    return story


@pytest.mark.parametrize(
    "backend,declared_class,expected",
    [
        # A capable class lifts the count cap, whatever the provider is called.
        (OLD_BACKEND, CAPABLE, False),
        (NON_OLD_BACKEND, CAPABLE, False),
        # An explicit on-device class caps even a provider the old tuple ignored.
        (OLD_BACKEND, ON_DEVICE, True),
        (NON_OLD_BACKEND, ON_DEVICE, True),
        # Unset class: exactly the old backend-name behaviour.
        (OLD_BACKEND, None, True),
        (NON_OLD_BACKEND, None, False),
        # An unrecognised class string is ignored, not guessed at: old behaviour.
        (OLD_BACKEND, "mystery", True),
        (NON_OLD_BACKEND, "mystery", False),
    ],
)
def test_needs_small_story_caps_truth_table(backend, declared_class, expected):
    registry = _registry(backend, declared_class)

    assert needs_small_story_caps(_story(backend), registry) is expected


@pytest.mark.parametrize("backend", [OLD_BACKEND, NON_OLD_BACKEND])
@pytest.mark.parametrize("declared_class", [CAPABLE, ON_DEVICE])
def test_an_explicitly_capable_class_is_never_capped(backend, declared_class):
    """No second, divergent tier decision: an explicit `capable` is uncapped.

    The declared value is interpreted with `pipeline.strength_class`, so the two
    can never disagree about what a declared class means. The converse does not
    hold, by design: an unset class on a provider outside the old backend set
    derives as on-device yet keeps the old (uncapped) answer, so nothing that
    used to be silent starts warning.
    """
    registry = _registry(backend, declared_class)

    shared = strength_class(backend, "m:latest", declared_class)
    capped = needs_small_story_caps(_story(backend), registry)

    assert (shared == CAPABLE) is (capped is False)


@pytest.mark.parametrize(
    "provider,expected",
    [
        # `claude` is outside the old backend set, so it was never sized at all.
        ("claude", False),
        # `litellm` IS in the old set and declares no class, so the base hook
        # keeps its pre-existing capped answer; the ingest path's
        # `class_aware_sizing_warning` is what lifts the count cap for it.
        ("litellm", True),
    ],
)
def test_derived_capable_providers_keep_the_old_answer(provider, expected):
    """This hook only reads an EXPLICIT class; derivation stays where it is.

    `claude`/`litellm` are capable by provider name in `pipeline.strength_class`,
    but the ingest notice's count cap is lifted for them by
    `pipeline.sizing_by_class.class_aware_sizing_warning` (which the ingest
    manifest write goes through), not by this advisory hook -- see the
    pre-existing `test_ingest_story_sizing_warning` pins for `litellm` and for a
    `:cloud` model.
    """
    assert (
        needs_small_story_caps(_story(provider), _registry(provider)) is expected
    ), provider


def test_an_ollama_cloud_tag_keeps_the_old_answer_for_this_hook():
    """`:cloud` capability is derived, so this hook keeps the old behaviour."""
    registry = _registry("ollama", tag="glm-5.3-flash:cloud")

    assert needs_small_story_caps(_story("ollama"), registry) is True


def test_sizing_applies_is_the_pre_existing_backend_gate():
    """Only the six old backend names are sized; nothing new starts warning."""
    old_backends = ("local", "ollama", "lmstudio", "mlx", "litellm", "auto")
    for backend in old_backends:
        assert sizing_applies(_story(backend), _registry(backend)) is True, backend
    for backend in ("claude", NON_OLD_BACKEND):
        assert sizing_applies(_story(backend), _registry(backend)) is False, backend


@pytest.mark.parametrize(
    "backend,declared_class",
    [
        (NON_OLD_BACKEND, ON_DEVICE),
        (NON_OLD_BACKEND, CAPABLE),
        (NON_OLD_BACKEND, None),
        ("claude", ON_DEVICE),
        ("claude", None),
    ],
)
def test_sizing_applies_does_not_widen_beyond_the_old_backend_set(
    backend, declared_class
):
    """A provider the old gate ignored stays unsized, whatever it declares."""
    assert sizing_applies(_story(backend), _registry(backend, declared_class)) is False


def test_unset_class_with_every_old_backend_name_is_capped():
    """The tuple that moved out of ingest.py keeps all six of its names.

    None of them declares a class, so all keep the pre-existing answer --
    including `litellm`, whose derived capability is handled by
    `pipeline.sizing_by_class.class_aware_sizing_warning` in the ingest path.
    """
    old_backends = ("local", "ollama", "lmstudio", "mlx", "litellm", "auto")
    for backend in old_backends:
        assert (
            needs_small_story_caps(_story(backend), _registry(backend)) is True
        ), backend
    for backend in ("claude", NON_OLD_BACKEND):
        assert needs_small_story_caps(_story(backend), _registry(backend)) is False, backend


def test_missing_backend_and_model_resolve_through_the_registry_dispatch_role():
    registry = _registry("ollama", CAPABLE)
    registry["roles"] = {"dispatch": {"provider": "ollama", "model": "m"}}

    assert needs_small_story_caps({"files": list(THREE_PRODUCTION_FILES)}, registry) is False

    registry["providers"]["ollama"]["models"]["m"].pop("class")
    assert needs_small_story_caps({"files": list(THREE_PRODUCTION_FILES)}, registry) is True


def test_missing_backend_and_no_dispatch_role_is_not_capped():
    """No provider anywhere resolves to claude, which the old gate never sized."""
    registry = {"providers": {}, "roles": {}}

    assert needs_small_story_caps({"files": list(THREE_PRODUCTION_FILES)}, registry) is False
    assert sizing_applies({"files": list(THREE_PRODUCTION_FILES)}, registry) is False


class _BrokenRegistry:
    """A registry loader that fails the way a corrupt/missing file can."""

    def __init__(self, exc: BaseException):
        self.exc = exc

    def load_registry(self):
        raise self.exc


@pytest.mark.parametrize(
    "exc",
    [OSError("missing"), ValueError("bad json"), KeyError("bad key"), TypeError("bad")],
)
def test_unreadable_registry_falls_back_to_the_old_behaviour_without_raising(exc):
    broken = _BrokenRegistry(exc)

    assert needs_small_story_caps(_story(OLD_BACKEND), broken) is True
    assert needs_small_story_caps(_story(NON_OLD_BACKEND), broken) is False
    assert sizing_applies(_story(OLD_BACKEND), broken) is True
    assert sizing_applies(_story("claude"), broken) is False


@pytest.mark.parametrize("registry", [None, "not a registry", [], _registry("ollama")])
def test_a_registry_without_a_loader_is_never_consulted_implicitly(registry):
    """A mapping is used as-is; anything unreadable degrades to old behaviour."""
    assert needs_small_story_caps(_story(OLD_BACKEND), registry) is True
    assert needs_small_story_caps(_story(NON_OLD_BACKEND), registry) is False


def test_the_live_registry_module_is_read_through_load_registry(monkeypatch):
    monkeypatch.setattr(
        role_registry,
        "load_registry",
        lambda *a, **k: _registry(OLD_BACKEND, CAPABLE),
    )

    assert needs_small_story_caps(_story(OLD_BACKEND), role_registry) is False


def test_the_live_registry_module_failing_degrades_to_old_behaviour(monkeypatch):
    def _boom(*a, **k):
        raise OSError("registry unreadable")

    monkeypatch.setattr(role_registry, "load_registry", _boom)

    assert needs_small_story_caps(_story(OLD_BACKEND), role_registry) is True
    assert needs_small_story_caps(_story(NON_OLD_BACKEND), role_registry) is False


@pytest.mark.parametrize(
    "story",
    [None, [], {}, {"backend": None}, {"backend": 7}, {"backend": "  "},
     {"backend": "OLLAMA "}, {"files": "not-a-list"}],
)
def test_malformed_stories_never_raise(story):
    assert isinstance(needs_small_story_caps(story, _registry(OLD_BACKEND)), bool)
    assert isinstance(sizing_applies(story, _registry(OLD_BACKEND)), bool)


def test_malformed_registry_payloads_degrade_to_the_old_behaviour():
    for payload in ({}, {"providers": None}, {"providers": {"ollama": "nope"}},
                    {"providers": {"ollama": {"models": None}}}):
        assert needs_small_story_caps(_story(OLD_BACKEND), payload) is True, payload
        assert needs_small_story_caps(_story(NON_OLD_BACKEND), payload) is False, payload


@pytest.mark.parametrize("declared", [" Capable ", "CAPABLE", "", "on_device"])
def test_unrecognised_class_values_keep_the_old_behaviour(declared):
    """Only exactly `capable`/`on-device` are honoured; anything else is old.

    `pipeline.strength_class` logs and ignores an unrecognised value, so the
    sizing decision must fall back to the backend name, never guess.
    """
    registry = _registry(OLD_BACKEND, declared)

    assert needs_small_story_caps(_story(OLD_BACKEND), registry) is True
    assert needs_small_story_caps(_story(NON_OLD_BACKEND), registry) is False
