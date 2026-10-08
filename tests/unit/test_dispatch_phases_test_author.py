"""Unit tests for the detached test-author decision step
(pipeline/dispatch_phases.py): ``_test_author_detached_enabled`` and
``_step_detached_test_author``.

The only patched boundary is ``pipeline.test_author.start_test_author_phase``
/ ``collect_test_author_phase`` (the true external boundary, imported by the
implementation at call time). The manifest is a REAL tmp_path JSON file, so
persistence behavior is graded on disk. Nothing here calls into dispatch.py:
this story adds decision logic only, wired in by a later story.

Run with the project venv:
    .venv/bin/python -m pytest -q tests/unit/test_dispatch_phases_test_author.py
"""

import json

import pytest

from pipeline import dispatch_phases
from pipeline import server as p
from pipeline import test_author as ptest_author

_STORY_KEY = "S1"
_PHASE = {
    "pid": 4242,
    "started_at": "2026-10-08T00:00:00+00:00",
    "backend": "mlx",
    "model": "test-author-model",
}


class _Recorder:
    """Stand-in for a test-author primitive: records calls, returns a fixed
    result or raises a fixed exception."""

    def __init__(self, result=None, exc=None):
        self.calls = []
        self._result = result
        self._exc = exc

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self._exc is not None:
            raise self._exc
        return self._result


def _patch_primitives(monkeypatch, start=None, collect=None):
    """Patch the primitives at BOTH lazy-resolution points (the
    pipeline.test_author module the implementation imports at call time, and
    the pipeline.server re-export point used by the _ServerRef pattern)."""
    if start is not None:
        monkeypatch.setattr(ptest_author, "start_test_author_phase", start)
        monkeypatch.setattr(p, "start_test_author_phase", start, raising=False)
    if collect is not None:
        monkeypatch.setattr(ptest_author, "collect_test_author_phase", collect)
        monkeypatch.setattr(p, "collect_test_author_phase", collect, raising=False)


def _setup(tmp_path, story, *, backend="ollama", resuming=False, marker=False):
    """Write a real manifest file holding *story* and return the kwargs for
    _step_detached_test_author plus the manifest path."""
    manifest = {"stories": {_STORY_KEY: story}}
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    worktree = tmp_path / "wt"
    worktree.mkdir(exist_ok=True)
    marker_path = worktree / ".tdd_split_test_author_done"
    if marker:
        marker_path.write_text("ok\n")
    kwargs = {
        "story_key": _STORY_KEY,
        "worktree_path": worktree,
        "dispatch_backend": backend,
        "local_model": "deepseek-v4.1-flash",
        "plan_name": "plan-x",
        "plan_role_config": None,
        "manifest": manifest,
        "manifest_path": manifest_path,
        "marker_path": marker_path,
        "resuming": resuming,
    }
    return kwargs, manifest_path


def _disk_story(manifest_path):
    manifest = json.loads(manifest_path.read_text())
    return manifest["stories"][_STORY_KEY]


# ---------- _test_author_detached_enabled: flag parsing ----------


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " on "])
def test_flag_truthy_values_enable(monkeypatch, value):
    monkeypatch.setenv("PIPELINE_TEST_AUTHOR_DETACHED", value)
    assert dispatch_phases._test_author_detached_enabled() is True


@pytest.mark.parametrize("value", ["0", "", "yes please", "off", "2"])
def test_flag_falsy_values_disable(monkeypatch, value):
    monkeypatch.setenv("PIPELINE_TEST_AUTHOR_DETACHED", value)
    assert dispatch_phases._test_author_detached_enabled() is False


def test_flag_unset_disables(monkeypatch):
    monkeypatch.delenv("PIPELINE_TEST_AUTHOR_DETACHED", raising=False)
    assert dispatch_phases._test_author_detached_enabled() is False


# ---------- 'not_applicable': checked first, touches nothing ----------


def test_flag_off_not_applicable_and_primitives_untouched(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("PIPELINE_TEST_AUTHOR_DETACHED", raising=False)
    start, collect = _Recorder(_PHASE), _Recorder(None)
    _patch_primitives(monkeypatch, start=start, collect=collect)
    story = {"title": "t"}
    kwargs, manifest_path = _setup(tmp_path, story)
    before = manifest_path.read_bytes()

    result = dispatch_phases._step_detached_test_author(story, **kwargs)

    assert result == "not_applicable"
    assert start.calls == [] and collect.calls == []
    assert manifest_path.read_bytes() == before
    assert story == {"title": "t"}


def test_claude_backend_not_applicable(monkeypatch, tmp_path):
    monkeypatch.setenv("PIPELINE_TEST_AUTHOR_DETACHED", "1")
    start, collect = _Recorder(_PHASE), _Recorder(None)
    _patch_primitives(monkeypatch, start=start, collect=collect)
    story = {"title": "t"}
    kwargs, manifest_path = _setup(tmp_path, story, backend="claude")
    before = manifest_path.read_bytes()

    result = dispatch_phases._step_detached_test_author(story, **kwargs)

    assert result == "not_applicable"
    assert start.calls == [] and collect.calls == []
    assert manifest_path.read_bytes() == before


def test_resuming_without_phase_not_applicable(monkeypatch, tmp_path):
    monkeypatch.setenv("PIPELINE_TEST_AUTHOR_DETACHED", "1")
    start, collect = _Recorder(_PHASE), _Recorder(None)
    _patch_primitives(monkeypatch, start=start, collect=collect)
    story = {"title": "t"}
    kwargs, manifest_path = _setup(tmp_path, story, resuming=True)
    before = manifest_path.read_bytes()

    result = dispatch_phases._step_detached_test_author(story, **kwargs)

    assert result == "not_applicable"
    assert start.calls == [] and collect.calls == []
    assert manifest_path.read_bytes() == before


def test_marker_already_present_not_applicable(monkeypatch, tmp_path):
    monkeypatch.setenv("PIPELINE_TEST_AUTHOR_DETACHED", "1")
    start, collect = _Recorder(_PHASE), _Recorder(None)
    _patch_primitives(monkeypatch, start=start, collect=collect)
    story = {"title": "t"}
    kwargs, manifest_path = _setup(tmp_path, story, marker=True)
    before = manifest_path.read_bytes()

    result = dispatch_phases._step_detached_test_author(story, **kwargs)

    assert result == "not_applicable"
    assert start.calls == [] and collect.calls == []
    assert manifest_path.read_bytes() == before


# ---------- first entry (not resuming, no phase dict) ----------


def test_first_entry_start_dict_returns_pending_and_persists(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("PIPELINE_TEST_AUTHOR_DETACHED", "1")
    start, collect = _Recorder(_PHASE), _Recorder(None)
    _patch_primitives(monkeypatch, start=start, collect=collect)
    story = {"title": "t"}
    kwargs, manifest_path = _setup(tmp_path, story)

    result = dispatch_phases._step_detached_test_author(story, **kwargs)

    assert result == "pending"
    assert len(start.calls) == 1
    assert collect.calls == []
    disk = _disk_story(manifest_path)
    assert disk["test_author_phase"] == _PHASE
    assert disk["test_author_phase"]["pid"] == _PHASE["pid"]
    assert not kwargs["marker_path"].exists()


def test_first_entry_start_none_fells_open_without_field(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("PIPELINE_TEST_AUTHOR_DETACHED", "1")
    start, collect = _Recorder(None), _Recorder(None)
    _patch_primitives(monkeypatch, start=start, collect=collect)
    story = {"title": "t"}
    kwargs, manifest_path = _setup(tmp_path, story)

    result = dispatch_phases._step_detached_test_author(story, **kwargs)

    assert result == "fell_open"
    assert len(start.calls) == 1
    assert collect.calls == []
    assert "test_author_phase" not in _disk_story(manifest_path)
    assert not kwargs["marker_path"].exists()


# ---------- re-entry (story carries a dict test_author_phase) ----------


def test_reentry_collect_none_pending_and_manifest_byte_identical(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("PIPELINE_TEST_AUTHOR_DETACHED", "1")
    start, collect = _Recorder(_PHASE), _Recorder(None)
    _patch_primitives(monkeypatch, start=start, collect=collect)
    story = {"title": "t", "test_author_phase": dict(_PHASE)}
    kwargs, manifest_path = _setup(tmp_path, story, resuming=True)
    before = manifest_path.read_bytes()

    result = dispatch_phases._step_detached_test_author(story, **kwargs)

    assert result == "pending"
    assert start.calls == []
    assert len(collect.calls) == 1
    assert manifest_path.read_bytes() == before
    assert _disk_story(manifest_path)["test_author_phase"] == _PHASE


def test_reentry_collect_true_authored(monkeypatch, tmp_path):
    monkeypatch.setenv("PIPELINE_TEST_AUTHOR_DETACHED", "1")
    start, collect = _Recorder(_PHASE), _Recorder(True)
    _patch_primitives(monkeypatch, start=start, collect=collect)
    story = {"title": "t", "test_author_phase": dict(_PHASE)}
    kwargs, manifest_path = _setup(tmp_path, story, resuming=True)

    result = dispatch_phases._step_detached_test_author(story, **kwargs)

    assert result == "authored"
    assert len(collect.calls) == 1
    disk = _disk_story(manifest_path)
    assert "test_author_phase" not in disk
    assert disk["tdd_split"] is True
    assert kwargs["marker_path"].read_text() == "ok\n"


def test_reentry_collect_false_fells_open(monkeypatch, tmp_path):
    monkeypatch.setenv("PIPELINE_TEST_AUTHOR_DETACHED", "1")
    start, collect = _Recorder(_PHASE), _Recorder(False)
    _patch_primitives(monkeypatch, start=start, collect=collect)
    story = {"title": "t", "test_author_phase": dict(_PHASE)}
    kwargs, manifest_path = _setup(tmp_path, story, resuming=True)

    result = dispatch_phases._step_detached_test_author(story, **kwargs)

    assert result == "fell_open"
    assert len(collect.calls) == 1
    assert "test_author_phase" not in _disk_story(manifest_path)
    assert not kwargs["marker_path"].exists()


# ---------- never raises ----------


def test_primitive_raising_fells_open_without_propagating(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("PIPELINE_TEST_AUTHOR_DETACHED", "1")
    start = _Recorder(exc=RuntimeError("boom"))
    collect = _Recorder(exc=RuntimeError("boom"))
    _patch_primitives(monkeypatch, start=start, collect=collect)
    story = {"title": "t"}
    kwargs, manifest_path = _setup(tmp_path, story)

    result = dispatch_phases._step_detached_test_author(story, **kwargs)

    assert result == "fell_open"
    assert "test_author_phase" not in _disk_story(manifest_path)
