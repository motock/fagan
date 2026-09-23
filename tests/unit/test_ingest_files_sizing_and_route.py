"""Tests for `files`-based story sizing and the on-device auto-route.

Two related ingest-time behaviours are pinned here:

1. ``_story_sizing_warning`` prefers a story's declared ``files`` list over
   the backtick-quoted-path regex in ``agent_instructions``. When ``files``
   is a list it is authoritative: test paths (anything under ``tests/`` or
   whose basename matches ``test_*.py`` / ``*_test.py`` / ``conftest.py``)
   are ignored entirely, the production-file COUNT cap counts non-test,
   non-``.md`` paths (docs travel with code), and the file-SIZE cap checks
   EVERY non-test path -- ``.md`` files and repo-root files such as
   ``README.md`` or ``pyproject.toml`` included. When ``files`` is absent
   the regex path is unchanged.

2. ``_on_device_route_target`` decides whether an oversized on-device story
   should be auto-routed to the host's ``:cloud`` default model, and
   ``_ingest_plan_impl`` performs that route: it rewrites the story's
   ``backend``/``model``, records ``sizing_auto_routed``, notifies once with
   the ``sizing_auto_routed`` event, and skips the plain advisory warning for
   that story so it is never double-notified.

These tests are RED until the implementation lands.
"""

from __future__ import annotations

import inspect
import json
import logging
from pathlib import Path

import pytest

# pipeline.server must load before pipeline.ingest: pipeline.ingest's module
# body imports pipeline.build_detect, which imports pipeline.server, which
# imports _ingest_plan_impl back from pipeline.ingest -- so importing
# pipeline.ingest first re-enters a partially-initialized pipeline.server.
import pipeline.server as p
from pipeline import concurrency as pcon
from pipeline import ingest as ingest_mod
from pipeline import persistence as ppers
from pipeline import ticketing as pt
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _clear_caches,
    _fake_plane,
    _isolate_usage_state,
    _plane_configured,
    _story,
)

_SIZING_RULE = "agent-dispatch-story-sizing.md"
_CLOUD_TAG = "glm-5.3-flash:cloud"
_LOCAL_TAG = "gpt-oss-20b-high:latest"
_SKIP_SUFFIX = (
    "; auto-route skipped: PIPELINE_LOCAL_MODEL_DEFAULT is not a :cloud tag"
)
_PREFLIGHT = "Preflight: test fixture - not a real plan\n"
_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def sizing_plan_dir(tmp_path, monkeypatch):
    """Local copy of the shared ``plan_dir`` fixture.

    Importing the shared fixture and using it as a same-named test parameter
    is the standard pytest pattern, but ruff's F811 false positive is only
    ignored for the ``test_pipeline_mcp_server_*.py`` file pattern, so this
    file keeps its own copy under a distinct name.
    """
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    # pipeline_persistence and pipeline_concurrency import PLAN_DIR from
    # pipeline_paths at module load and read it as a free var, so patches
    # must land on their own bindings too.
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


def _write_lines(path, count):
    """Create ``path`` (parents included) holding exactly ``count`` lines."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x\n" * count)
    return path


def _capture_notices(monkeypatch):
    """Replace the notification seam and record ``(plan, msg, event)``."""
    notices = []

    def _fake_notify(plan, msg, **kwargs):
        notices.append((plan, msg, kwargs.get("event")))

    monkeypatch.setattr(ingest_mod, "_notify_user", _fake_notify)
    return notices


def _plan(repo_root, stories):
    return {
        "repo_root": str(repo_root),
        "epics": [{"summary": "E1", "stories": stories}],
    }


def _oversized_story(**over):
    """A todo on-device story whose ``files`` list trips the count cap."""
    base = {
        "backend": "ollama",
        "model": _LOCAL_TAG,
        "files": ["pipeline/a.py", "pipeline/b.py", "pipeline/c.py"],
        "agent_instructions": _PREFLIGHT + "Touch three production files.",
    }
    base.update(over)
    return _story(**base)


# ---------------------------------------------------------------------------
# Signatures (the brief keeps both public shapes stable)
# ---------------------------------------------------------------------------


def test_story_sizing_warning_signature_is_unchanged():
    params = list(inspect.signature(ingest_mod._story_sizing_warning).parameters)
    assert params == ["story", "repo_root"]


def test_on_device_route_target_signature():
    params = list(inspect.signature(ingest_mod._on_device_route_target).parameters)
    assert params == ["story"]


# ---------------------------------------------------------------------------
# `files`-based production-file-count cap
# ---------------------------------------------------------------------------


def test_files_three_production_files_warns(tmp_path):
    story = {
        "backend": "ollama",
        "files": ["pipeline/a.py", "pipeline/b.py", "pipeline/c.py"],
    }
    warning = ingest_mod._story_sizing_warning(story, str(tmp_path))
    assert warning is not None
    assert "production files" in warning
    assert "3" in warning
    for path in ("pipeline/a.py", "pipeline/b.py", "pipeline/c.py"):
        assert path in warning
    assert _SIZING_RULE in warning


def test_files_exactly_two_production_files_is_fine(tmp_path):
    """The cap is exclusive-of-2: 2 files is the documented allowance."""
    story = {"backend": "ollama", "files": ["pipeline/a.py", "pipeline/b.py"]}
    assert ingest_mod._story_sizing_warning(story, str(tmp_path)) is None


def test_files_only_tests_no_warning(tmp_path):
    story = {
        "backend": "ollama",
        "files": [
            "tests/unit/test_a.py",
            "tests/unit/test_b.py",
            "tests/unit/test_c.py",
        ],
    }
    assert ingest_mod._story_sizing_warning(story, str(tmp_path)) is None


def test_files_two_production_plus_three_md_no_count_warning(tmp_path):
    """Docs travel with code: `.md` paths never count toward the 2-file cap."""
    story = {
        "backend": "ollama",
        "files": [
            "pipeline/a.py",
            "pipeline/b.py",
            "REFERENCE.md",
            "docs/a.md",
            "docs/b.md",
        ],
    }
    assert ingest_mod._story_sizing_warning(story, str(tmp_path)) is None


def test_files_md_does_not_count_toward_count_cap(tmp_path):
    story = {
        "backend": "ollama",
        "files": ["pipeline/a.py", "pipeline/b.py", "REFERENCE.md"],
    }
    assert ingest_mod._story_sizing_warning(story, str(tmp_path)) is None


@pytest.mark.parametrize(
    "path",
    [
        "pipeline/test_foo.py",
        "pipeline/foo_test.py",
        "pipeline/conftest.py",
        "tests/helpers.py",
    ],
)
def test_files_test_basenames_are_ignored(tmp_path, path):
    """A test path is ignored entirely, even when it is over the size cap."""
    _write_lines(tmp_path / path, 1200)
    story = {"backend": "ollama", "files": [path]}
    assert ingest_mod._story_sizing_warning(story, str(tmp_path)) is None


def test_files_test_path_over_size_cap_is_ignored(tmp_path):
    _write_lines(tmp_path / "tests" / "unit" / "test_big.py", 1200)
    story = {"backend": "ollama", "files": ["tests/unit/test_big.py"]}
    assert ingest_mod._story_sizing_warning(story, str(tmp_path)) is None


def test_files_test_paths_do_not_count_toward_the_count_cap(tmp_path):
    """Two production files plus a test file is still within the 2-file cap."""
    story = {
        "backend": "ollama",
        "files": ["pipeline/a.py", "pipeline/b.py", "pipeline/test_c.py"],
    }
    assert ingest_mod._story_sizing_warning(story, str(tmp_path)) is None


def _base_warning(warning):
    """The documented warning text, without any appended auto-route suffix."""
    return warning.split("; auto-route skipped:")[0]


def test_files_count_warning_keeps_the_documented_message_format(tmp_path):
    story = {
        "backend": "ollama",
        "files": ["pipeline/a.py", "pipeline/b.py", "pipeline/c.py"],
    }
    warning = ingest_mod._story_sizing_warning(story, str(tmp_path))
    assert _base_warning(warning) == (
        "story sizing risk (see .claude/rules/agent-dispatch-story-sizing.md): "
        "names 3 production files (pipeline/a.py, pipeline/b.py, pipeline/c.py), "
        "over the 2-file cap for a non-Claude dispatch tier"
    )


def test_files_size_warning_keeps_the_documented_message_format(tmp_path):
    _write_lines(tmp_path / "REFERENCE.md", 1200)
    story = {"backend": "ollama", "files": ["REFERENCE.md"]}
    warning = ingest_mod._story_sizing_warning(story, str(tmp_path))
    assert _base_warning(warning) == (
        "story sizing risk (see .claude/rules/agent-dispatch-story-sizing.md): "
        "edits REFERENCE.md (1200 lines), over the 1000-line file-size cap for "
        "the weakest dispatch tier"
    )


def test_files_absent_regex_warning_keeps_the_documented_message_format(tmp_path):
    story = {
        "backend": "ollama",
        "agent_instructions": "Touch `app/a.py`, `app/b.py` and `app/c.py`.",
    }
    warning = ingest_mod._story_sizing_warning(story, str(tmp_path))
    assert _base_warning(warning) == (
        "story sizing risk (see .claude/rules/agent-dispatch-story-sizing.md): "
        "names 3 production files (app/a.py, app/b.py, app/c.py), over the "
        "2-file cap for a non-Claude dispatch tier"
    )


def test_files_readme_at_repo_root_over_size_cap_warns(tmp_path):
    _write_lines(tmp_path / "README.md", 1200)
    story = {"backend": "ollama", "files": ["README.md"]}
    warning = ingest_mod._story_sizing_warning(story, str(tmp_path))
    assert warning is not None
    assert "size cap" in warning
    assert "README.md" in warning


def test_files_count_and_size_reasons_are_both_reported(tmp_path):
    _write_lines(tmp_path / "pipeline" / "big.py", 1200)
    story = {
        "backend": "ollama",
        "files": ["pipeline/big.py", "pipeline/a.py", "pipeline/b.py"],
    }
    warning = ingest_mod._story_sizing_warning(story, str(tmp_path))
    assert warning is not None
    assert "production files" in warning
    assert "size cap" in warning
    assert "pipeline/big.py" in warning


def test_files_backend_gate_is_case_and_whitespace_insensitive(tmp_path):
    story = {
        "backend": "  OLLAMA ",
        "files": ["pipeline/a.py", "pipeline/b.py", "pipeline/c.py"],
    }
    assert ingest_mod._story_sizing_warning(story, str(tmp_path)) is not None


def test_files_unreadable_repo_root_degrades_to_none(tmp_path):
    """A missing repo_root must not raise: the size check is skipped."""
    story = {"backend": "ollama", "files": ["pipeline/a.py"]}
    assert (
        ingest_mod._story_sizing_warning(story, str(tmp_path / "nope")) is None
    )


# ---------------------------------------------------------------------------
# `files`-based file-size cap (every non-test path, .md and root included)
# ---------------------------------------------------------------------------


def test_files_md_file_over_size_cap_warns(tmp_path):
    _write_lines(tmp_path / "REFERENCE.md", 1200)
    story = {"backend": "ollama", "files": ["REFERENCE.md"]}
    warning = ingest_mod._story_sizing_warning(story, str(tmp_path))
    assert warning is not None
    assert "size cap" in warning
    assert "REFERENCE.md" in warning
    assert "1200" in warning


def test_files_repo_root_file_over_size_cap_warns(tmp_path):
    _write_lines(tmp_path / "pyproject.toml", 1200)
    story = {"backend": "ollama", "files": ["pyproject.toml"]}
    warning = ingest_mod._story_sizing_warning(story, str(tmp_path))
    assert warning is not None
    assert "size cap" in warning
    assert "pyproject.toml" in warning


def test_files_file_of_exactly_the_cap_is_fine(tmp_path):
    """Boundary: the cap is ``> 1000``, so exactly 1000 lines is allowed."""
    _write_lines(tmp_path / "pipeline" / "big.py", 1000)
    story = {"backend": "ollama", "files": ["pipeline/big.py"]}
    assert ingest_mod._story_sizing_warning(story, str(tmp_path)) is None


def test_files_nonexistent_file_counts_toward_cap_but_not_size(tmp_path):
    """A brand-new file has no line count yet: it still counts toward the
    production-file cap but is skipped for the size check."""
    story = {
        "backend": "ollama",
        "files": ["pipeline/new.py", "pipeline/a.py", "pipeline/b.py"],
    }
    warning = ingest_mod._story_sizing_warning(story, str(tmp_path))
    assert warning is not None
    assert "production files" in warning
    assert "size cap" not in warning


# ---------------------------------------------------------------------------
# `files` present vs. absent: the list wins, the regex is the fallback
# ---------------------------------------------------------------------------


def test_files_present_overrides_the_backtick_regex(tmp_path):
    """When `files` is a list it is authoritative: the regex is not consulted."""
    story = {
        "backend": "ollama",
        "files": ["pipeline/a.py"],
        "agent_instructions": (
            "Touch `pipeline/a.py`, `pipeline/b.py` and `pipeline/c.py`."
        ),
    }
    assert ingest_mod._story_sizing_warning(story, str(tmp_path)) is None


def test_files_empty_list_uses_the_list_not_the_regex(tmp_path):
    """An empty list is still a list: no production files, so no warning."""
    story = {
        "backend": "ollama",
        "files": [],
        "agent_instructions": (
            "Touch `pipeline/a.py`, `pipeline/b.py` and `pipeline/c.py`."
        ),
    }
    assert ingest_mod._story_sizing_warning(story, str(tmp_path)) is None


def test_files_absent_uses_the_regex_path(tmp_path):
    story = {
        "backend": "ollama",
        "agent_instructions": (
            "Touch `pipeline/a.py`, `pipeline/b.py` and `pipeline/c.py`."
        ),
    }
    warning = ingest_mod._story_sizing_warning(story, str(tmp_path))
    assert warning is not None
    assert "production files" in warning


def test_files_none_falls_back_to_the_regex(tmp_path):
    story = {
        "backend": "ollama",
        "files": None,
        "agent_instructions": (
            "Touch `pipeline/a.py`, `pipeline/b.py` and `pipeline/c.py`."
        ),
    }
    warning = ingest_mod._story_sizing_warning(story, str(tmp_path))
    assert warning is not None
    assert "production files" in warning


def test_files_non_list_falls_back_to_the_regex(tmp_path):
    """Only a real ``list`` is authoritative; anything else uses the regex."""
    story = {
        "backend": "ollama",
        "files": "pipeline/a.py",
        "agent_instructions": (
            "Touch `pipeline/a.py`, `pipeline/b.py` and `pipeline/c.py`."
        ),
    }
    warning = ingest_mod._story_sizing_warning(story, str(tmp_path))
    assert warning is not None
    assert "production files" in warning


# ---------------------------------------------------------------------------
# Backend gate (unchanged)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["claude", None, "", "auto"])
def test_files_backend_claude_class_is_never_warned(tmp_path, backend):
    story = {
        "files": ["pipeline/a.py", "pipeline/b.py", "pipeline/c.py"],
    }
    if backend is not None:
        story["backend"] = backend
    assert ingest_mod._story_sizing_warning(story, str(tmp_path)) is None


# ---------------------------------------------------------------------------
# `_on_device_route_target`
# ---------------------------------------------------------------------------


def _route_story(**over):
    base = {"status": "todo", "backend": "ollama", "model": _LOCAL_TAG}
    base.update(over)
    return base


def test_on_device_route_target_returns_the_default_tag(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", _CLOUD_TAG)
    assert ingest_mod._on_device_route_target(_route_story()) == _CLOUD_TAG


@pytest.mark.parametrize(
    "backend", ["local", "ollama", "lmstudio", "mlx", "litellm"]
)
def test_on_device_route_target_accepts_every_local_backend(monkeypatch, backend):
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", _CLOUD_TAG)
    assert (
        ingest_mod._on_device_route_target(_route_story(backend=backend))
        == _CLOUD_TAG
    )


@pytest.mark.parametrize("backend", ["claude", "auto", "", None, "  Claude  "])
def test_on_device_route_target_rejects_non_local_backends(monkeypatch, backend):
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", _CLOUD_TAG)
    assert ingest_mod._on_device_route_target(_route_story(backend=backend)) is None


@pytest.mark.parametrize(
    "status", ["in_progress", "done", "parked", "blocked", None]
)
def test_on_device_route_target_requires_todo_status(monkeypatch, status):
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", _CLOUD_TAG)
    assert ingest_mod._on_device_route_target(_route_story(status=status)) is None


def test_on_device_route_target_none_when_model_already_cloud(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", _CLOUD_TAG)
    assert ingest_mod._on_device_route_target(_route_story(model=_CLOUD_TAG)) is None


def test_on_device_route_target_none_when_default_not_cloud(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", "devstral:24b")
    assert ingest_mod._on_device_route_target(_route_story()) is None


def test_on_device_route_target_none_when_default_unset(monkeypatch):
    monkeypatch.delenv("PIPELINE_LOCAL_MODEL_DEFAULT", raising=False)
    assert ingest_mod._on_device_route_target(_route_story()) is None


@pytest.mark.parametrize("model", [None, ""])
def test_on_device_route_target_none_when_model_absent(monkeypatch, model):
    """With no per-story model the effective model IS the default, which
    already ends in ``:cloud`` -- so there is nothing to route."""
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", _CLOUD_TAG)
    assert ingest_mod._on_device_route_target(_route_story(model=model)) is None


# ---------------------------------------------------------------------------
# Ingest-level wiring
# ---------------------------------------------------------------------------


def test_ingest_auto_routes_an_oversized_on_device_story(
    sizing_plan_dir, monkeypatch, tmp_path, caplog
):
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", _CLOUD_TAG)
    notices = _capture_notices(monkeypatch)
    story = _oversized_story()
    plan = _plan(tmp_path, [story])
    (sizing_plan_dir / "route1.json").write_text(json.dumps(plan))

    with caplog.at_level(logging.WARNING, logger="pipeline"):
        result = p.ingest_plan("route1")

    assert result["ok"] is True
    manifest = json.loads((sizing_plan_dir / "route1.manifest.json").read_text())
    persisted = manifest["stories"]["issue-1"]
    assert persisted["backend"] == "ollama"
    assert persisted["model"] == _CLOUD_TAG

    expected_warning = ingest_mod._story_sizing_warning(story, str(tmp_path))
    assert expected_warning is not None
    assert persisted["sizing_auto_routed"] == {
        "from_backend": "ollama",
        "from_model": _LOCAL_TAG,
        "to_model": _CLOUD_TAG,
        "reason": expected_warning,
    }

    routed = [n for n in notices if n[2] == "sizing_auto_routed"]
    assert len(routed) == 1
    plan_name, msg, _event = routed[0]
    assert plan_name == "route1"
    assert msg == f"issue-1: auto-routed to {_CLOUD_TAG}: {expected_warning}"
    # The routed story is not double-notified with a plain sizing warning.
    assert len(notices) == 1

    assert any(
        rec.levelno == logging.WARNING and "auto-routed to" in rec.getMessage()
        for rec in caplog.records
    )
    # Exactly one warning for the story: the route notice replaces the plain
    # advisory sizing warning rather than joining it.
    story_warnings = [
        rec
        for rec in caplog.records
        if rec.levelno == logging.WARNING and "issue-1" in rec.getMessage()
    ]
    assert len(story_warnings) == 1
    assert "auto-routed to" in story_warnings[0].getMessage()


def test_ingest_rewrites_a_non_ollama_local_backend_to_ollama(
    sizing_plan_dir, monkeypatch, tmp_path
):
    """The route pins the story to the ollama driver, recording the origin."""
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", _CLOUD_TAG)
    _capture_notices(monkeypatch)
    plan = _plan(tmp_path, [_oversized_story(backend="lmstudio")])
    (sizing_plan_dir / "route10.json").write_text(json.dumps(plan))

    result = p.ingest_plan("route10")

    assert result["ok"] is True
    manifest = json.loads((sizing_plan_dir / "route10.manifest.json").read_text())
    persisted = manifest["stories"]["issue-1"]
    assert persisted["backend"] == "ollama"
    assert persisted["model"] == _CLOUD_TAG
    assert persisted["sizing_auto_routed"]["from_backend"] == "lmstudio"
    assert persisted["sizing_auto_routed"]["from_model"] == _LOCAL_TAG
    assert persisted["sizing_auto_routed"]["to_model"] == _CLOUD_TAG


def test_ingest_does_not_route_when_default_is_not_cloud(
    sizing_plan_dir, monkeypatch, tmp_path
):
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", "devstral:24b")
    notices = _capture_notices(monkeypatch)
    plan = _plan(tmp_path, [_oversized_story()])
    (sizing_plan_dir / "route2.json").write_text(json.dumps(plan))

    result = p.ingest_plan("route2")

    assert result["ok"] is True
    manifest = json.loads((sizing_plan_dir / "route2.manifest.json").read_text())
    persisted = manifest["stories"]["issue-1"]
    assert persisted["backend"] == "ollama"
    assert persisted["model"] == _LOCAL_TAG
    assert "sizing_auto_routed" not in persisted
    assert len(notices) == 1
    _plan_name, msg, event = notices[0]
    assert event is None
    assert "production files" in msg
    assert msg.endswith(_SKIP_SUFFIX)


def test_ingest_routes_a_regex_sized_story_with_an_explicit_model(
    sizing_plan_dir, monkeypatch, tmp_path
):
    """The route depends on the sizing warning, not on how it was derived."""
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", _CLOUD_TAG)
    notices = _capture_notices(monkeypatch)
    story = _story(
        backend="ollama",
        model=_LOCAL_TAG,
        agent_instructions=(
            _PREFLIGHT
            + "Touch `pipeline/a.py`, `pipeline/b.py` and `pipeline/c.py`."
        ),
    )
    plan = _plan(tmp_path, [story])
    (sizing_plan_dir / "route8.json").write_text(json.dumps(plan))

    result = p.ingest_plan("route8")

    assert result["ok"] is True
    manifest = json.loads((sizing_plan_dir / "route8.manifest.json").read_text())
    persisted = manifest["stories"]["issue-1"]
    assert persisted["backend"] == "ollama"
    assert persisted["model"] == _CLOUD_TAG
    assert persisted["sizing_auto_routed"]["to_model"] == _CLOUD_TAG
    assert len([n for n in notices if n[2] == "sizing_auto_routed"]) == 1


def test_ingest_routes_only_the_oversized_story(
    sizing_plan_dir, monkeypatch, tmp_path
):
    """A well-scoped sibling is neither routed nor notified."""
    issue_ids = iter(["issue-1", "issue-2"])
    monkeypatch.setattr(
        pt,
        "plane_request",
        lambda method, path, **kw: (
            _fake_plane(method, path, **kw)
            if not path.endswith("/work-items/")
            else {"id": next(issue_ids)}
        ),
    )
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", _CLOUD_TAG)
    notices = _capture_notices(monkeypatch)
    plan = _plan(
        tmp_path,
        [
            _oversized_story(key="S1"),
            _story(
                key="S2",
                backend="ollama",
                model=_LOCAL_TAG,
                files=["pipeline/a.py"],
                agent_instructions=_PREFLIGHT + "Touch one file.",
            ),
        ],
    )
    (sizing_plan_dir / "route9.json").write_text(json.dumps(plan))

    result = p.ingest_plan("route9")

    assert result["ok"] is True
    manifest = json.loads((sizing_plan_dir / "route9.manifest.json").read_text())
    stories = manifest["stories"]
    assert stories["issue-1"]["model"] == _CLOUD_TAG
    assert "sizing_auto_routed" in stories["issue-1"]
    assert stories["issue-2"]["model"] == _LOCAL_TAG
    assert "sizing_auto_routed" not in stories["issue-2"]
    routed = [n for n in notices if n[2] == "sizing_auto_routed"]
    assert len(routed) == 1
    assert routed[0][1].startswith("issue-1: auto-routed to ")


def test_ingest_does_not_route_when_default_is_unset(
    sizing_plan_dir, monkeypatch, tmp_path
):
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    monkeypatch.delenv("PIPELINE_LOCAL_MODEL_DEFAULT", raising=False)
    notices = _capture_notices(monkeypatch)
    plan = _plan(tmp_path, [_oversized_story()])
    (sizing_plan_dir / "route3.json").write_text(json.dumps(plan))

    result = p.ingest_plan("route3")

    assert result["ok"] is True
    manifest = json.loads((sizing_plan_dir / "route3.manifest.json").read_text())
    persisted = manifest["stories"]["issue-1"]
    assert persisted["model"] == _LOCAL_TAG
    assert "sizing_auto_routed" not in persisted
    assert len(notices) == 1
    _plan_name, msg, event = notices[0]
    assert event is None
    assert msg.endswith(_SKIP_SUFFIX)


def test_ingest_warns_but_does_not_route_when_model_is_already_cloud(
    sizing_plan_dir, monkeypatch, tmp_path
):
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", _CLOUD_TAG)
    notices = _capture_notices(monkeypatch)
    plan = _plan(tmp_path, [_oversized_story(model=_CLOUD_TAG)])
    (sizing_plan_dir / "route4.json").write_text(json.dumps(plan))

    result = p.ingest_plan("route4")

    assert result["ok"] is True
    manifest = json.loads((sizing_plan_dir / "route4.manifest.json").read_text())
    persisted = manifest["stories"]["issue-1"]
    assert persisted["model"] == _CLOUD_TAG
    assert "sizing_auto_routed" not in persisted
    assert len(notices) == 1
    _plan_name, msg, event = notices[0]
    assert event is None
    assert "production files" in msg
    # The default IS a :cloud tag, so the skip suffix does not apply.
    assert "auto-route skipped" not in msg


def test_ingest_never_warns_or_routes_a_claude_backend_story(
    sizing_plan_dir, monkeypatch, tmp_path
):
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", _CLOUD_TAG)
    notices = _capture_notices(monkeypatch)
    plan = _plan(tmp_path, [_oversized_story(backend="claude")])
    (sizing_plan_dir / "route5.json").write_text(json.dumps(plan))

    result = p.ingest_plan("route5")

    assert result["ok"] is True
    manifest = json.loads((sizing_plan_dir / "route5.manifest.json").read_text())
    persisted = manifest["stories"]["issue-1"]
    assert persisted["backend"] == "claude"
    assert persisted["model"] == _LOCAL_TAG
    assert "sizing_auto_routed" not in persisted
    assert notices == []


def test_ingest_never_routes_a_running_story_on_reingest(
    sizing_plan_dir, monkeypatch, tmp_path
):
    """A re-ingest of an already-running story must not be re-routed."""
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", "devstral:24b")
    notices = _capture_notices(monkeypatch)
    plan = _plan(tmp_path, [{**_oversized_story(), "key": "S1"}])
    (sizing_plan_dir / "route6.json").write_text(json.dumps(plan))

    assert p.ingest_plan("route6")["ok"] is True

    manifest_path = sizing_plan_dir / "route6.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["stories"]["issue-1"]["status"] = "in_progress"
    manifest_path.write_text(json.dumps(manifest))

    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", _CLOUD_TAG)
    notices.clear()

    result = p.ingest_plan("route6")

    assert result["ok"] is True
    manifest = json.loads(manifest_path.read_text())
    persisted = manifest["stories"]["issue-1"]
    assert persisted["status"] == "in_progress"
    assert persisted["backend"] == "ollama"
    assert persisted["model"] == _LOCAL_TAG
    assert "sizing_auto_routed" not in persisted
    assert [n for n in notices if n[2] == "sizing_auto_routed"] == []


def test_ingest_files_absent_behaviour_is_unchanged(
    sizing_plan_dir, monkeypatch, tmp_path
):
    """With no `files` field the regex path still warns, and a story with no
    per-story model is never routed (its effective model is the :cloud
    default)."""
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", _CLOUD_TAG)
    notices = _capture_notices(monkeypatch)
    story = _story(
        backend="ollama",
        agent_instructions=(
            _PREFLIGHT
            + "Touch `pipeline/a.py`, `pipeline/b.py` and `pipeline/c.py`."
        ),
    )
    plan = _plan(tmp_path, [story])
    (sizing_plan_dir / "route7.json").write_text(json.dumps(plan))

    result = p.ingest_plan("route7")

    assert result["ok"] is True
    manifest = json.loads((sizing_plan_dir / "route7.manifest.json").read_text())
    persisted = manifest["stories"]["issue-1"]
    assert "files" not in persisted
    assert "sizing_auto_routed" not in persisted
    assert len(notices) == 1
    _plan_name, msg, event = notices[0]
    assert event is None
    assert "production files" in msg


# ---------------------------------------------------------------------------
# REFERENCE.md documentation
# ---------------------------------------------------------------------------


def _reference_section(heading):
    text = (_REPO_ROOT / "REFERENCE.md").read_text()
    start = text.index(heading)
    rest = text[start + len(heading):]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def test_reference_documents_files_sizing_and_the_auto_route():
    section = _reference_section("## Story file scope (`files`)")
    assert "sizing_auto_routed" in section
    assert "auto-route" in section.lower()
    assert "PIPELINE_LOCAL_MODEL_DEFAULT" in section
    assert "size" in section.lower()
    assert ".md" in section
    assert "files" in section

    # The new paragraph is appended at the END of the section (the section is
    # cumulative, so only this paragraph's placement is asserted).
    body = section.rstrip()
    if body.endswith("---"):
        body = body[:-3].rstrip()
    paragraphs = [para.strip() for para in body.split("\n\n") if para.strip()]
    assert "sizing_auto_routed" in paragraphs[-1]
    assert "PIPELINE_LOCAL_MODEL_DEFAULT" in paragraphs[-1]
