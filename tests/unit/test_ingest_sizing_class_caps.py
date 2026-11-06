"""`_story_sizing_warning` must apply the count cap by strength class.

`pipeline.ingest._story_sizing_warning` used to decide the dispatch tier from a
bare backend-name tuple, so a provider whose registry entry declares
``"class": "capable"`` still got the 2-production-file warning that only small
on-device models need. It now asks `pipeline.sizing_class.needs_small_story_caps`
instead. The 1000-line file-size cap is unchanged for both classes: it keeps
firing for a capable story, only the file-COUNT reason is lifted.
"""

from __future__ import annotations

import pytest

import pipeline.server  # noqa: F401  (must load before pipeline.ingest)
from app import role_registry
from pipeline.ingest import _sizing_view, _story_sizing_warning

_SIZING_PREFIX = "story sizing risk (see .claude/rules/agent-dispatch-story-sizing.md): "
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


def _registry(provider: str, declared_class: str | None = None) -> dict:
    entry: dict = {"models": {"m": {"tag": "m:latest"}}}
    if declared_class is not None:
        entry["class"] = declared_class
    return {"providers": {provider: entry}}


def _story(backend: str, files: list[str]) -> dict:
    return {"backend": backend, "model": "m", "files": files}


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


def test_on_device_provider_with_three_production_files_warns(monkeypatch, sizing_repo):
    _stub_registry(monkeypatch, _registry("vllm", "on-device"))

    warning = _story_sizing_warning(_story("vllm", THREE_FILES), sizing_repo)

    assert warning is not None
    assert warning.startswith(_SIZING_PREFIX)
    assert "names 3 production files (a.py, b.py, c.py)" in warning
    assert "2-file cap" in warning


def test_on_device_class_on_an_old_backend_still_warns(monkeypatch, sizing_repo):
    _stub_registry(monkeypatch, _registry("ollama", "on-device"))

    warning = _story_sizing_warning(_story("ollama", THREE_FILES), sizing_repo)

    assert warning is not None
    assert "3 production files" in warning


def test_unset_class_on_an_old_backend_keeps_the_old_warning(monkeypatch, sizing_repo):
    _stub_registry(monkeypatch, _registry("ollama"))

    warning = _story_sizing_warning(_story("ollama", THREE_FILES), sizing_repo)

    assert warning is not None
    assert "3 production files" in warning


def test_unset_class_on_a_non_old_provider_stays_silent(monkeypatch, sizing_repo):
    _stub_registry(monkeypatch, _registry("vllm"))

    assert _story_sizing_warning(_story("vllm", THREE_FILES), sizing_repo) is None


@pytest.mark.parametrize("backend", ["ollama", "vllm"])
def test_unknown_class_string_keeps_the_old_behaviour(monkeypatch, sizing_repo, backend):
    _stub_registry(monkeypatch, _registry(backend, "mystery"))

    warning = _story_sizing_warning(_story(backend, THREE_FILES), sizing_repo)

    if backend == "ollama":
        assert warning is not None
        assert "3 production files" in warning
    else:
        assert warning is None


def test_unreadable_registry_keeps_the_old_behaviour_and_never_raises(
    monkeypatch, sizing_repo
):
    def _boom(*_a, **_k):
        raise OSError("model_registry.json is unreadable")

    monkeypatch.setattr(role_registry, "load_registry", _boom)

    assert _story_sizing_warning(_story("ollama", THREE_FILES), sizing_repo) is not None
    assert _story_sizing_warning(_story("claude", THREE_FILES), sizing_repo) is None


def test_file_size_warning_is_unchanged_for_a_capable_class(monkeypatch, sizing_repo):
    _stub_registry(monkeypatch, _registry("ollama", "capable"))

    warning = _story_sizing_warning(_story("ollama", [*THREE_FILES, "big.py"]), sizing_repo)

    assert warning is not None
    assert warning.startswith(_SIZING_PREFIX)
    assert "edits big.py (1200 lines)" in warning
    assert "1000-line file-size cap" in warning
    # Only the COUNT reason is lifted for a capable target.
    assert "production files (" not in warning


def test_file_size_warning_is_unchanged_for_an_on_device_class(monkeypatch, sizing_repo):
    _stub_registry(monkeypatch, _registry("ollama", "on-device"))

    warning = _story_sizing_warning(_story("ollama", [*THREE_FILES, "big.py"]), sizing_repo)

    assert warning is not None
    assert "edits big.py (1200 lines)" in warning
    assert "1000-line file-size cap" in warning
    assert "4 production files (" in warning


def test_file_size_warning_text_is_identical_across_both_classes(
    monkeypatch, sizing_repo
):
    """The size reason itself is byte-identical whichever class applies."""
    _stub_registry(monkeypatch, _registry("ollama", "on-device"))
    on_device = _story_sizing_warning(_story("ollama", ["big.py"]), sizing_repo)
    _stub_registry(monkeypatch, _registry("ollama", "capable"))
    capable = _story_sizing_warning(_story("ollama", ["big.py"]), sizing_repo)

    assert on_device == capable
    assert on_device is not None
