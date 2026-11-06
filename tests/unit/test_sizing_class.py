"""Tests for the strength-class decision behind the ingest sizing caps.

`pipeline.sizing_class.needs_small_story_caps` is the tier decision that used to
be a bare backend-name tuple inside `pipeline.ingest._story_sizing_warning`. The
count cap is a crutch for small on-device models, so a dispatch target declared
``"class": "capable"`` is not capped on file count; ``"class": "on-device"`` is
capped whatever its provider is called; and an unset or unrecognised class keeps
the pre-existing backend-name behaviour.

`pipeline.sizing_class.sizing_applies` is the wider scope question: today only
the local/cloud-open backends are sized at all, and a story is sized when its
effective provider is one of those OR its declared class is ``on-device``.

Nothing here may ever raise: this gates an advisory observability hook, so an
unreadable registry degrades to the old backend-name behaviour.
"""

from __future__ import annotations

import pytest

from app import role_registry
from pipeline.sizing_class import needs_small_story_caps, sizing_applies

OLD_BACKEND = "ollama"
NON_OLD_BACKEND = "vllm"
CAPABLE = "capable"
ON_DEVICE = "on-device"

THREE_PRODUCTION_FILES = ["app/a.py", "app/b.py", "app/c.py"]


@pytest.fixture(autouse=True)
def _isolate_dispatch_env(monkeypatch):
    """Keep a developer's exported dispatch env out of these tests."""
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_MODEL_DEFAULT", raising=False)


def _registry(provider: str, declared_class: str | None = None) -> dict:
    """A synthetic registry declaring *provider* (and optionally its class)."""
    entry: dict = {"models": {"m": {"tag": "m:latest"}}}
    if declared_class is not None:
        entry["class"] = declared_class
    return {"providers": {provider: entry}}


def _story(backend: str | None) -> dict:
    story = {"model": "m", "files": list(THREE_PRODUCTION_FILES)}
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


@pytest.mark.parametrize(
    "backend,declared_class,expected",
    [
        (OLD_BACKEND, CAPABLE, True),
        (OLD_BACKEND, ON_DEVICE, True),
        (OLD_BACKEND, None, True),
        (NON_OLD_BACKEND, ON_DEVICE, True),
        # A capable provider the old tuple never sized stays unsized: only the
        # size cap would apply to it and today it is not inspected at all.
        (NON_OLD_BACKEND, CAPABLE, False),
        (NON_OLD_BACKEND, None, False),
        ("claude", None, False),
    ],
)
def test_sizing_applies_covers_old_backends_and_on_device_classes(
    backend, declared_class, expected
):
    registry = _registry(backend, declared_class)

    assert sizing_applies(_story(backend), registry) is expected


def test_unset_class_with_every_old_backend_name_is_capped():
    """The tuple that moved out of ingest.py keeps all six of its names."""
    old_backends = ("local", "ollama", "lmstudio", "mlx", "litellm", "auto")
    for backend in old_backends:
        assert needs_small_story_caps(_story(backend), _registry(backend)) is True, backend
    for backend in ("claude", NON_OLD_BACKEND):
        assert needs_small_story_caps(_story(backend), _registry(backend)) is False, backend


def test_missing_backend_resolves_through_the_registry_dispatch_role():
    registry = _registry("ollama", CAPABLE)
    registry["roles"] = {"dispatch": {"provider": "ollama"}}

    assert needs_small_story_caps({"files": list(THREE_PRODUCTION_FILES)}, registry) is False

    registry["providers"]["ollama"].pop("class")
    assert needs_small_story_caps({"files": list(THREE_PRODUCTION_FILES)}, registry) is True


def test_missing_backend_and_no_dispatch_role_is_not_capped():
    """No provider anywhere resolves to claude, which the old gate never sized."""
    registry = {"providers": {}, "roles": {}}

    assert needs_small_story_caps({"files": list(THREE_PRODUCTION_FILES)}, registry) is False
    assert sizing_applies({"files": list(THREE_PRODUCTION_FILES)}, registry) is False


def test_a_model_entry_class_is_honoured_without_a_provider_level_one():
    """REFERENCE.md documents `class` on the providers.<name>.models.<model> entry."""
    registry = {
        "providers": {"ollama": {"models": {"m": {"tag": "m:latest", "class": CAPABLE}}}}
    }

    assert needs_small_story_caps(_story(OLD_BACKEND), registry) is False


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


def test_provider_class_is_normalised_and_unknown_values_are_ignored():
    registry = _registry(OLD_BACKEND, " Capable ")

    assert needs_small_story_caps(_story(OLD_BACKEND), registry) is False
    assert needs_small_story_caps(_story(OLD_BACKEND), _registry(OLD_BACKEND, "CAPABLE")) is False
    assert needs_small_story_caps(_story(OLD_BACKEND), _registry(OLD_BACKEND, "")) is True
