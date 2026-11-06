"""`_story_sizing_warning` must apply the count cap by strength class.

`pipeline.ingest._story_sizing_warning` used to decide the dispatch tier from a
bare backend-name tuple, so a provider whose model entry declares
``"class": "capable"`` still got the 2-production-file warning that only small
on-device models need. It now asks `pipeline.sizing_class.needs_small_story_caps`,
which defers to `pipeline.strength_class` -- the same classifier the auto-route
and plan-review paths use. The 1000-line file-size cap is unchanged for both
classes: it keeps firing for a capable story, only the file-COUNT reason is
lifted.
"""

from __future__ import annotations

import pytest

import pipeline.server  # noqa: F401  (must load before pipeline.ingest)
from app import role_registry
from pipeline.ingest import _sizing_view, _story_sizing_warning

_SIZING_PREFIX = (
    "story sizing risk (see .claude/rules/agent-dispatch-story-sizing.md): "
)
THREE_FILES = ["a.py", "b.py", "c.py"]


@pytest.fixture(autouse=True)
def _isolate_dispatch_env(monkeypatch):
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_MODEL_DEFAULT", raising=False)


@pytest.fixture
def sizing_repo(tmp_path):
    """Three small production files plus one over the 1000-line size cap."""
    for name in THREE_FILES:
        (tmp_path / name).write_text("x = 1\n")
    (tmp_path / "big.py").write_text("\n".join(f"line {i}" for i in range(1200)))
    return str(tmp_path)


def _stub_registry(monkeypatch, payload):
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: payload)


def _registry(
    provider: str, declared_class: str | None = None, tag: str = "m:latest"
) -> dict:
    """A registry with the class where REFERENCE.md documents it: per model."""
    model: dict = {"tag": tag}
    if declared_class is not None:
        model["class"] = declared_class
    return {"providers": {provider: {"models": {"m": model}}}}


def _story(backend: str, files: list[str], model: str = "m") -> dict:
    return {"backend": backend, "model": model, "files": files}


def test_capable_provider_with_three_production_files_does_not_warn(
    monkeypatch, sizing_repo
):
    _stub_registry(monkeypatch, _registry("ollama", "capable"))

    assert _story_sizing_warning(_story("ollama", THREE_FILES), sizing_repo) is None


def test_capable_provider_reached_through_the_plan_role_config_does_not_warn(
    monkeypatch, sizing_repo
):
    _stub_registry(monkeypatch, _registry("ollama", "capable"))
    story = {"files": list(THREE_FILES), "model": "m"}

    view = _sizing_view(story, {"dispatch": {"provider": "ollama"}})

    assert view["backend"] == "ollama"
    assert _story_sizing_warning(view, sizing_repo) is None


def test_litellm_keeps_the_old_warning_from_the_base_hook(monkeypatch, sizing_repo):
    """Derived capability is handled by `class_aware_sizing_warning`, not here.

    This hook only reads an EXPLICIT per-model ``class``; the ingest path's
    wrapper lifts the count cap for a derived-capable target. Pinned by the
    pre-existing `test_ingest_story_sizing_warning` parametrisation.
    """
    _stub_registry(monkeypatch, _registry("litellm"))

    warning = _story_sizing_warning(_story("litellm", THREE_FILES), sizing_repo)

    assert warning is not None
    assert "names 3 production files" in warning


def test_an_ollama_cloud_tag_keeps_the_old_warning_from_the_base_hook(
    monkeypatch, sizing_repo
):
    _stub_registry(monkeypatch, _registry("ollama", tag="glm-5.3-flash:cloud"))

    warning = _story_sizing_warning(_story("ollama", THREE_FILES), sizing_repo)

    assert warning is not None
    assert "names 3 production files" in warning


def test_an_explicit_capable_class_wins_over_a_derived_on_device_provider(
    monkeypatch, sizing_repo
):
    """A `vllm` entry declaring capable lifts the count cap like any other."""
    _stub_registry(monkeypatch, _registry("vllm", "capable"))

    assert _story_sizing_warning(_story("vllm", THREE_FILES), sizing_repo) is None


def test_on_device_provider_with_three_production_files_warns(
    monkeypatch, sizing_repo
):
    _stub_registry(monkeypatch, _registry("ollama", "on-device"))

    warning = _story_sizing_warning(_story("ollama", THREE_FILES), sizing_repo)

    assert warning is not None
    assert warning.startswith(_SIZING_PREFIX)
    assert "names 3 production files" in warning
    assert "over the 2-file cap" in warning


def test_unset_class_on_an_old_backend_keeps_the_old_warning(
    monkeypatch, sizing_repo
):
    """The pre-existing behaviour must survive: no class means the old cap."""
    _stub_registry(monkeypatch, _registry("ollama"))

    warning = _story_sizing_warning(_story("ollama", THREE_FILES), sizing_repo)

    assert warning is not None
    assert "names 3 production files" in warning


def test_unknown_class_string_keeps_the_old_behaviour(monkeypatch, sizing_repo):
    _stub_registry(monkeypatch, _registry("ollama", "mystery"))

    warning = _story_sizing_warning(_story("ollama", THREE_FILES), sizing_repo)

    assert warning is not None
    assert "names 3 production files" in warning


def test_unset_class_on_a_non_old_provider_stays_silent(monkeypatch, sizing_repo):
    """A provider the old gate ignored is not newly sized: the advisory holds."""
    _stub_registry(monkeypatch, _registry("vllm"))

    assert _story_sizing_warning(_story("vllm", THREE_FILES), sizing_repo) is None


@pytest.mark.parametrize(
    "exc", [OSError("missing"), ValueError("bad json"), KeyError("bad key")]
)
def test_unreadable_registry_keeps_the_old_behaviour_and_never_raises(
    monkeypatch, sizing_repo, exc
):
    def _boom(*a, **k):
        raise exc

    monkeypatch.setattr(role_registry, "load_registry", _boom)

    warning = _story_sizing_warning(_story("ollama", THREE_FILES), sizing_repo)

    assert warning is not None
    assert "names 3 production files" in warning


def test_file_size_warning_is_unchanged_for_a_capable_class(
    monkeypatch, sizing_repo
):
    _stub_registry(monkeypatch, _registry("ollama", "capable"))

    warning = _story_sizing_warning(
        _story("ollama", [*THREE_FILES, "big.py"]), sizing_repo
    )

    assert warning is not None
    assert warning.startswith(_SIZING_PREFIX)
    assert "edits big.py (1200 lines)" in warning
    assert "1000-line file-size cap" in warning
    # Only the COUNT reason is lifted for a capable target.
    assert "production files (" not in warning


def test_file_size_warning_is_unchanged_for_an_on_device_class(
    monkeypatch, sizing_repo
):
    _stub_registry(monkeypatch, _registry("ollama", "on-device"))

    warning = _story_sizing_warning(
        _story("ollama", [*THREE_FILES, "big.py"]), sizing_repo
    )

    assert warning is not None
    assert "edits big.py (1200 lines)" in warning
    assert "1000-line file-size cap" in warning
    # The count reason still fires here as well.
    assert "names 4 production files" in warning


def test_file_size_warning_text_is_identical_across_both_classes(
    monkeypatch, sizing_repo
):
    """The size-cap sentence is class-independent; only the count reason varies."""
    files = ["a.py", "big.py"]  # under the count cap, over the size cap

    _stub_registry(monkeypatch, _registry("ollama", "capable"))
    capable = _story_sizing_warning(_story("ollama", files), sizing_repo)

    _stub_registry(monkeypatch, _registry("ollama", "on-device"))
    on_device = _story_sizing_warning(_story("ollama", files), sizing_repo)

    assert capable == on_device
    assert capable is not None
    assert "edits big.py (1200 lines)" in capable
