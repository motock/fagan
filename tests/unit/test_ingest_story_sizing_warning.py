"""Tests for the ingest-time story-sizing warning.

``.claude/rules/agent-dispatch-story-sizing.md`` documents two mechanically
checkable caps for a story dispatched to a non-Claude executor: no more than
2 production files in scope, and no production file over ~1000 lines in
scope. Neither was checked anywhere -- they were enforced only by a human
plan author remembering to apply them by hand, which is how story
``146753bb`` (chat-ux-improvements) ended up needing 3 dispatch attempts and
an escalation to a stronger model.

``_story_sizing_warning`` is the ingest-time observability hook that closes
that gap. Like the "multi-model concurrent dispatch" / "ollama serving
parallelism" warnings in ``pipeline/dispatch.py``, it is purely advisory: it
never blocks or rejects an ingest, and a filesystem read failure degrades to
``None`` rather than raising.
"""

import json

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


# ---------- production-file-count cap ----------


def test_three_production_files_warns(tmp_path):
    story = {
        "backend": "ollama",
        "agent_instructions": "Touch `app/a.py`, `app/b.py` and `app/c.py`.",
    }
    warning = ingest_mod._story_sizing_warning(story, str(tmp_path))
    assert warning is not None
    assert "production files" in warning
    assert _SIZING_RULE in warning


def test_exactly_two_production_files_is_fine(tmp_path):
    """The cap is exclusive-of-2: 2 files is the documented allowance."""
    story = {
        "backend": "ollama",
        "agent_instructions": "Touch `app/a.py` and `app/b.py`.",
    }
    assert ingest_mod._story_sizing_warning(story, str(tmp_path)) is None


def test_one_production_file_is_fine(tmp_path):
    story = {"backend": "ollama", "agent_instructions": "Touch `app/a.py`."}
    assert ingest_mod._story_sizing_warning(story, str(tmp_path)) is None


def test_test_and_doc_paths_do_not_count_toward_the_cap(tmp_path):
    """Only production files count: a test file and a markdown doc alongside
    two production files must not push the story over the 2-file cap."""
    story = {
        "backend": "ollama",
        "agent_instructions": (
            "Touch `app/a.py`, `app/b.py`, `tests/unit/test_a.py` and `docs/notes.md`."
        ),
    }
    assert ingest_mod._story_sizing_warning(story, str(tmp_path)) is None


# ---------- file-size cap ----------


def test_oversized_file_warns(tmp_path):
    _write_lines(tmp_path / "static" / "big.css", 1001)
    story = {"backend": "ollama", "agent_instructions": "Edit `static/big.css`."}
    warning = ingest_mod._story_sizing_warning(story, str(tmp_path))
    assert warning is not None
    assert "size cap" in warning
    assert "static/big.css" in warning


def test_file_of_exactly_the_cap_is_fine(tmp_path):
    """Boundary: the cap is ``> 1000``, so exactly 1000 lines is allowed."""
    _write_lines(tmp_path / "static" / "big.css", 1000)
    story = {"backend": "ollama", "agent_instructions": "Edit `static/big.css`."}
    assert ingest_mod._story_sizing_warning(story, str(tmp_path)) is None


# ---------- backend gate ----------


@pytest.mark.parametrize("backend", ["claude", None, "", "auto"])
def test_claude_class_backends_are_never_flagged(tmp_path, backend):
    """Claude-class dispatch doesn't need this crutch, so the same oversized
    story must stay silent for it (mirrors the backend gate the
    test-author/planner phases already use)."""
    _write_lines(tmp_path / "static" / "big.css", 1001)
    story = {
        "agent_instructions": ("Edit `static/big.css`, `app/a.py` and `app/b.py`."),
    }
    if backend is not None:
        story["backend"] = backend
    assert ingest_mod._story_sizing_warning(story, str(tmp_path)) is None


@pytest.mark.parametrize("backend", ["local", "ollama", "lmstudio", "mlx", "litellm"])
def test_every_non_claude_tier_is_checked(tmp_path, backend):
    story = {
        "backend": backend,
        "agent_instructions": "Touch `app/a.py`, `app/b.py` and `app/c.py`.",
    }
    assert ingest_mod._story_sizing_warning(story, str(tmp_path)) is not None


def test_backend_is_case_and_whitespace_insensitive(tmp_path):
    story = {
        "backend": "  OLLAMA ",
        "agent_instructions": "Touch `app/a.py`, `app/b.py` and `app/c.py`.",
    }
    assert ingest_mod._story_sizing_warning(story, str(tmp_path)) is not None


# ---------- well-scoped / degenerate input ----------


def test_well_scoped_story_is_silent(tmp_path):
    _write_lines(tmp_path / "app" / "a.py", 5)
    _write_lines(tmp_path / "app" / "b.py", 5)
    story = {
        "backend": "ollama",
        "agent_instructions": "Touch `app/a.py` and `app/b.py`.",
    }
    assert ingest_mod._story_sizing_warning(story, str(tmp_path)) is None


def test_missing_agent_instructions_is_silent(tmp_path):
    assert (
        ingest_mod._story_sizing_warning({"backend": "ollama"}, str(tmp_path)) is None
    )


def test_empty_agent_instructions_is_silent(tmp_path):
    story = {"backend": "ollama", "agent_instructions": ""}
    assert ingest_mod._story_sizing_warning(story, str(tmp_path)) is None


def test_instructions_without_backtick_paths_is_silent(tmp_path):
    story = {
        "backend": "ollama",
        "agent_instructions": "Fix the thing in the app directory, no paths here.",
    }
    assert ingest_mod._story_sizing_warning(story, str(tmp_path)) is None


def test_non_production_backtick_paths_are_silent(tmp_path):
    """Backtick-quoted paths outside the repo-path prefixes (e.g. a bare
    filename or a command) are not file-scope claims."""
    story = {
        "backend": "ollama",
        "agent_instructions": "Run `pytest -q` and edit `README`.",
    }
    assert ingest_mod._story_sizing_warning(story, str(tmp_path)) is None


# ---------- nonexistent files ----------


def test_nonexistent_file_counts_toward_cap_but_not_size(tmp_path):
    """A brand-new file the story is about to CREATE has no line count yet:
    it must be skipped for the size check (never raise, never "oversized")
    while still counting toward the production-file cap."""
    _write_lines(tmp_path / "app" / "a.py", 5)
    _write_lines(tmp_path / "app" / "b.py", 5)
    story = {
        "backend": "ollama",
        "agent_instructions": "Create `app/new.py`, touch `app/a.py` and `app/b.py`.",
    }
    warning = ingest_mod._story_sizing_warning(story, str(tmp_path))
    assert warning is not None
    assert "production files" in warning
    assert "size cap" not in warning


def test_nonexistent_repo_root_never_raises(tmp_path):
    story = {
        "backend": "ollama",
        "agent_instructions": "Edit `static/big.css`.",
    }
    assert ingest_mod._story_sizing_warning(story, str(tmp_path / "nope")) is None


# ---------- ingest-time wiring ----------


def _plan(repo_root, instructions):
    return {
        "repo_root": str(repo_root),
        "epics": [
            {
                "summary": "E1",
                "stories": [_story(backend="ollama", agent_instructions=instructions)],
            }
        ],
    }


def test_ingest_notifies_once_for_an_oversized_story(
    sizing_plan_dir, monkeypatch, tmp_path
):
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    notices = []
    monkeypatch.setattr(
        ingest_mod, "_notify_user", lambda plan, msg: notices.append((plan, msg))
    )
    plan = _plan(
        tmp_path,
        "Touch `app/a.py`, `app/b.py` and `app/c.py`.",
    )
    (sizing_plan_dir / "sizing.json").write_text(json.dumps(plan))

    result = p.ingest_plan("sizing")

    assert result["ok"] is True
    assert len(notices) == 1
    plan_name, msg = notices[0]
    assert plan_name == "sizing"
    assert "issue-1" in msg
    assert _SIZING_RULE in msg
    assert "production files" in msg


def test_ingest_is_silent_for_a_well_scoped_story(
    sizing_plan_dir, monkeypatch, tmp_path
):
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    notices = []
    monkeypatch.setattr(
        ingest_mod, "_notify_user", lambda plan, msg: notices.append((plan, msg))
    )
    plan = _plan(tmp_path, "Touch `app/a.py`.")
    (sizing_plan_dir / "sizing2.json").write_text(json.dumps(plan))

    result = p.ingest_plan("sizing2")

    assert result["ok"] is True
    assert notices == []


def test_ingest_still_persists_the_story_unchanged(
    sizing_plan_dir, monkeypatch, tmp_path
):
    """The warning is advisory: it must not alter what lands in the manifest
    or block the ingest."""
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    monkeypatch.setattr(ingest_mod, "_notify_user", lambda plan, msg: None)
    instructions = "Touch `app/a.py`, `app/b.py` and `app/c.py`."
    plan = _plan(tmp_path, instructions)
    (sizing_plan_dir / "sizing3.json").write_text(json.dumps(plan))

    result = p.ingest_plan("sizing3")

    assert result["ok"] is True
    manifest = json.loads((sizing_plan_dir / "sizing3.manifest.json").read_text())
    assert manifest["stories"]["issue-1"]["agent_instructions"] == instructions
    assert manifest["stories"]["issue-1"]["backend"] == "ollama"
