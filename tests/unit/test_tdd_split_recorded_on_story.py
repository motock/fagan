"""Tests for recording `tdd_split` on the story when the always-on
test-author phase runs and succeeds (pipeline/dispatch.py).

The test-author phase used to be opt-in via the per-story `tdd_split`
field; it is now unconditional for local-family dispatch, but nothing
ever recorded that it ran. `pipeline/ci.py:_acceptance_tampered` branches
on `story["tdd_split"]`, so a permanently-False field silently kept every
story on the lenient "a pure append to the oracle is allowed" path even
when the split had run and the oracle should have been strictly
read-only. These tests pin the field to the phase's success branch.

Fixtures are copied locally per this repo's convention - no shared
conftest for dispatch tests.
"""
import hashlib
import json
import re
from pathlib import Path

import pytest

from pipeline import persistence as ppers
from pipeline import persona as pper
from pipeline import server as p
from pipeline import ticketing as pt

MARKER = ".tdd_split_test_author_done"
ROOT = Path(__file__).resolve().parents[2]

_BASE_STORY = {
    "summary": "Do thing",
    "agent_instructions": "Build it.",
    "status": "todo",
    "dependencies": [],
}


# ---------- Fixtures (copied from test_dispatch_acceptance_overwrite.py) ----------
@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    d = tmp_path / "agents"
    d.mkdir()
    (d / "overlord.md").write_text(
        '---\nname: "overlord"\nmodel: opus\nmemory: user\n---\n\n'
        "You are the Overlord body text.\n"
    )
    (d / "software-engineer.md").write_text(
        '---\nname: "software-engineer"\nmodel: sonnet\n---\n\nEngineer body.\n'
    )
    (d / "code-reviewer.md").write_text(
        '---\nname: "code-reviewer"\nmodel: sonnet\n---\n\nReviewer body.\n'
    )
    (d / "product-analyst.md").write_text(
        '---\nname: "product-analyst"\nmodel: opus\n---\n\nAnalyst body.\n'
    )
    monkeypatch.setattr(p, "AGENTS_DIR", d)
    monkeypatch.setattr(pper, "AGENTS_DIR", d)
    return d


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    return d


@pytest.fixture
def worktree_root(tmp_path, monkeypatch):
    d = tmp_path / "worktrees"
    d.mkdir()
    monkeypatch.setattr(p, "WORKTREE_ROOT", d)
    return d


@pytest.fixture(autouse=True)
def _plane_configured(monkeypatch):
    monkeypatch.setattr(pt, "PLANE_API_KEY", "test-key")
    monkeypatch.setattr(pt, "PLANE_WORKSPACE", "test-ws")
    monkeypatch.setattr(pt, "PLANE_PROJECT", "test-proj")


# ---------- Helpers ----------
def _write_manifest(plan_path, plan_name, stories):
    (plan_path / f"{plan_name}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": stories}, indent=2)
    )


def _stub_dispatch_externals(monkeypatch):
    """Stub the external boundaries dispatch_story touches so it can run to
    completion without git/gh/Plane/subprocess."""
    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)


def _manifest(plan_path, plan_name):
    return json.loads((plan_path / f"{plan_name}.manifest.json").read_text())


def _story(plan_path, plan_name, key="S1"):
    return _manifest(plan_path, plan_name)["stories"][key]


def _spy(monkeypatch, result):
    """Replace the test-author phase with a recording stub returning
    `result`, so a test can prove the phase ran (or did not) and control
    its success/failure verdict."""
    calls = []

    def _fake(*args, **kwargs):
        calls.append((args, kwargs))
        return result

    monkeypatch.setattr(p, "_run_test_author_phase", _fake)
    return calls


# ---------- positive: phase ran and succeeded ----------
def test_phase_success_records_tdd_split_true_on_manifest_story(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """The headline behaviour: after a dispatch in which the test-author
    phase runs and succeeds, the story dict persisted to the manifest has
    `tdd_split is True` (not merely truthy, and not left absent)."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "tddrec", {"S1": dict(_BASE_STORY)})
    calls = _spy(monkeypatch, True)
    _stub_dispatch_externals(monkeypatch)

    result = p.dispatch_story("tddrec", "S1")

    assert result["ok"] is True
    assert len(calls) == 1
    # The marker proves the success branch actually ran.
    assert (worktree_root / "S1" / MARKER).exists()
    story = _story(plan_dir, "tddrec")
    assert story["tdd_split"] is True


def test_recorded_tdd_split_tightens_acceptance_tamper_gate(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """End-to-end: the value dispatch persists is the value
    `ci._acceptance_tampered` reads. With `tdd_split` now True on the
    persisted story, a PURE APPEND to the oracle is tampering (strict
    read-only) instead of the lenient append-allowed path."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "tddgate", {"S1": dict(_BASE_STORY)})
    _spy(monkeypatch, True)
    _stub_dispatch_externals(monkeypatch)

    assert p.dispatch_story("tddgate", "S1")["ok"] is True

    story = _story(plan_dir, "tddgate")
    assert story["tdd_split"] is True

    original = "def test_oracle():\n    assert True\n"
    worktree = worktree_root / "S1"
    (worktree / "oracle.py").write_text(
        original + "\n\ndef test_appended():\n    assert True\n"
    )
    story["acceptance"] = [{"path": "oracle.py", "source": original}]
    story["acceptance_digests"] = {
        "oracle.py": hashlib.sha256(original.encode("utf-8")).hexdigest(),
    }

    from pipeline import ci

    assert ci._acceptance_tampered(story, str(worktree)) == ["oracle.py"]


# ---------- negative: phase skipped, story left exactly as it was ----------
def test_phase_skipped_non_local_backend_leaves_tdd_split_absent(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A non-local-family backend never runs the phase, so the key must
    not be created at all (not written as False)."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")
    _write_manifest(plan_dir, "tddclaude", {"S1": dict(_BASE_STORY)})
    calls = _spy(monkeypatch, True)
    _stub_dispatch_externals(monkeypatch)

    result = p.dispatch_story("tddclaude", "S1")

    assert result["ok"] is True
    assert calls == []
    assert "tdd_split" not in _story(plan_dir, "tddclaude")


def test_phase_skipped_when_resuming_leaves_tdd_split_absent(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A resumed dispatch acts on the tests it already has and never gets
    a fresh test-authoring pass, so the field must stay untouched."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "tddresume", {"S1": dict(_BASE_STORY)})
    (worktree_root / "S1").mkdir()
    calls = _spy(monkeypatch, True)
    _stub_dispatch_externals(monkeypatch)

    result = p.dispatch_story("tddresume", "S1")

    assert result["ok"] is True
    assert calls == []
    assert "tdd_split" not in _story(plan_dir, "tddresume")


def test_phase_skipped_when_marker_present_leaves_tdd_split_absent(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """An existing `.tdd_split_test_author_done` marker means the phase
    already ran on a prior dispatch; this dispatch must not touch the
    field (and must not call the phase)."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "tddmarker", {"S1": dict(_BASE_STORY)})
    worktree = worktree_root / "S1"
    worktree.mkdir()
    (worktree / MARKER).write_text("ok\n")
    calls = _spy(monkeypatch, True)
    _stub_dispatch_externals(monkeypatch)

    result = p.dispatch_story("tddmarker", "S1")

    assert result["ok"] is True
    assert calls == []
    assert "tdd_split" not in _story(plan_dir, "tddmarker")


def test_phase_failure_does_not_set_tdd_split_true(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """The documented fail-open cases (role unconfigured, dispatch
    failure, timeout, no commit produced) all surface as a False return
    from `_run_test_author_phase`. The phase ran but did not succeed, so
    `tdd_split` must NOT be set True."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _write_manifest(plan_dir, "tddfalse", {"S1": dict(_BASE_STORY)})
    calls = _spy(monkeypatch, False)
    _stub_dispatch_externals(monkeypatch)

    result = p.dispatch_story("tddfalse", "S1")

    assert result["ok"] is True
    assert len(calls) == 1
    assert not (worktree_root / "S1" / MARKER).exists()
    assert "tdd_split" not in _story(plan_dir, "tddfalse")


# ---------- boundary: never write False over an existing value ----------
def test_phase_skipped_does_not_overwrite_existing_true(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A story that already carries `tdd_split: True` (e.g. recorded by an
    earlier dispatch) must keep it when this dispatch skips the phase."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")
    _write_manifest(plan_dir, "tddkeep", {"S1": dict(_BASE_STORY, tdd_split=True)})
    calls = _spy(monkeypatch, True)
    _stub_dispatch_externals(monkeypatch)

    result = p.dispatch_story("tddkeep", "S1")

    assert result["ok"] is True
    assert calls == []
    assert _story(plan_dir, "tddkeep")["tdd_split"] is True


def test_phase_skipped_does_not_overwrite_existing_false(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """An explicit `tdd_split: False` (the ingest default) must survive a
    skipped phase unchanged -- still False, not deleted."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")
    _write_manifest(plan_dir, "tddkeepfalse", {"S1": dict(_BASE_STORY, tdd_split=False)})
    calls = _spy(monkeypatch, True)
    _stub_dispatch_externals(monkeypatch)

    result = p.dispatch_story("tddkeepfalse", "S1")

    assert result["ok"] is True
    assert calls == []
    assert _story(plan_dir, "tddkeepfalse")["tdd_split"] is False


# ---------- source-level guards on the surrounding contract ----------
def test_dispatch_source_sets_tdd_split_true_in_phase_success_branch():
    """Mechanical check of the edit site: the assignment must sit in the
    same region as the `_run_test_author_phase(...)` call and the
    `test_author_marker.write_text(...)` marker write -- i.e. inside the
    phase's success branch, not somewhere else in the function."""
    src = (ROOT / "pipeline" / "dispatch.py").read_text()
    match = re.search(r'story\["tdd_split"\]\s*=\s*True', src)
    assert match is not None, "dispatch.py never sets story['tdd_split'] = True"
    window = src[max(0, match.start() - 800): match.end() + 800]
    assert "_run_test_author_phase(" in window
    assert "test_author_marker.write_text(" in window


def test_ingest_still_declares_tdd_split_default_false():
    """Out of scope for this change: ingest.py keeps the key and its
    False default."""
    src = (ROOT / "pipeline" / "ingest.py").read_text()
    assert '"tdd_split": bool(story.get("tdd_split", False))' in src


def test_ci_acceptance_tampered_still_branches_on_story_tdd_split():
    """Out of scope for this change: ci.py's gate keeps reading the story
    field (it was being fed a wrong input, not mis-branching)."""
    src = (ROOT / "pipeline" / "ci.py").read_text()
    assert 'tdd_split = bool(story.get("tdd_split", False))' in src
