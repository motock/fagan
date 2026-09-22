"""TDD tests for the story-level ``files`` field (production file scope).

A story may declare the exact repo-relative paths of the PRODUCTION files
(including docs) it is allowed to change. ``ingest_plan`` must:

1. VALIDATE ``story["files"]`` up front, before any side effect (no manifest
   write, no epic/story creation), via a single helper
   ``pipeline.ingest._files_field_error(story) -> str | None``. ``None`` when
   ``files`` is absent or ``None``; otherwise the value must be a list whose
   every entry is a non-empty ``str`` that is repo-relative POSIX: it must not
   start with ``"/"``, must contain no ``"\\"``, and must have no ``".."`` path
   component. Duplicates are rejected. The error names the story summary and
   the offending entry, and the caller returns ``{"ok": False, "error": <it>}``.
2. PERSIST ``files`` verbatim (as a fresh list) in the manifest story, and
   omit the key entirely when the plan story has no ``files`` (or ``None``),
   so existing strict-equality assertions on persisted stories are unaffected.
3. RE-INGEST: ``"files"`` is the only authored field allowed to be ABSENT from
   a re-ingested story. Present -> copied; absent -> the key is removed from
   the merged story (dropping ``files`` from the plan clears it). Every other
   authored field keeps its existing copy-on-re-ingest behavior.

These tests drive the REAL ``_ingest_plan_impl`` through ``p.ingest_plan`` with
a tmp ``PLAN_DIR``, following the fixture pattern of
``tests/unit/test_ingest_plan_risk_lock.py``. They are RED until the
implementation lands.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Import pipeline.server FIRST: pipeline.ingest -> build_detect -> server ->
# ingest is a circular import, so importing a pipeline submodule standalone can
# raise ImportError. Importing the server module first breaks the cycle.
import pipeline.server  # noqa: F401
from pipeline import ingest as ingest_mod
from pipeline import server as p
from pipeline import ticketing as pt

REPO_ROOT = Path(__file__).resolve().parents[2]
REFERENCE = REPO_ROOT / "REFERENCE.md"
STORY_SCHEMA_RULE = REPO_ROOT / ".claude" / "rules" / "pipeline-story-schema.md"

# A non-Claude dispatch provider is gated on a real `Preflight:` line
# (.claude/rules/local-dispatch-preflight.md), so every fixture story carries
# one as its first line. This keeps the `files` validation the failure under
# test rather than the preflight gate.
_PREFLIGHT = "Preflight: test fixture — not a real plan"


def _explode_plane(*a, **kw):
    raise AssertionError("Plane should not be called - PLANE_* env is unset in tests")


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    return d


def _story(**extra):
    story = {
        "summary": "Do the thing",
        "key": "S1",
        "agent_instructions": _PREFLIGHT + "\nImplement it.",
    }
    story.update(extra)
    return story


def _plan(tmp_path, story):
    return {
        "repo_root": str(tmp_path),
        "epics": [{"summary": "E1", "stories": [story]}],
    }


def _write_plan(plan_dir, plan_name, plan):
    (plan_dir / f"{plan_name}.json").write_text(json.dumps(plan))


def _manifest_path(plan_dir, plan_name):
    return plan_dir / f"{plan_name}.manifest.json"


def _read_manifest(plan_dir, plan_name):
    return json.loads(_manifest_path(plan_dir, plan_name).read_text())


# ---------------------------------------------------------------------------
# 1. The helper: _files_field_error
# ---------------------------------------------------------------------------
def test_files_field_error_absent_is_none():
    assert ingest_mod._files_field_error({"summary": "S1"}) is None


def test_files_field_error_none_value_is_none():
    assert ingest_mod._files_field_error({"summary": "S1", "files": None}) is None


def test_files_field_error_empty_list_is_none():
    assert ingest_mod._files_field_error({"summary": "S1", "files": []}) is None


def test_files_field_error_valid_list_is_none():
    assert (
        ingest_mod._files_field_error(
            {"summary": "S1", "files": ["pipeline/foo.py", "REFERENCE.md"]}
        )
        is None
    )


# (files value, fragment the error must name as the offending entry or None)
_INVALID_FILES = [
    ("pipeline/foo.py", "pipeline/foo.py"),
    ([""], None),
    (["/abs/path.py"], "/abs/path.py"),
    (["../x.py"], "../x.py"),
    (["pipeline/../x.py"], "pipeline/../x.py"),
    (["pipeline\\x.py"], "pipeline\\x.py"),
    ([1], "1"),
    (["pipeline/foo.py", "pipeline/foo.py"], "pipeline/foo.py"),
]


@pytest.mark.parametrize("files, fragment", _INVALID_FILES)
def test_files_field_error_rejects_invalid_values(files, fragment):
    err = ingest_mod._files_field_error({"summary": "Do the thing", "files": files})
    assert isinstance(err, str) and err, f"expected an error for files={files!r}"
    # The error names the story summary ...
    assert "Do the thing" in err
    # ... and the offending entry.
    if fragment is not None:
        assert fragment in err


# ---------------------------------------------------------------------------
# 2. Persistence: files is stored verbatim; absent/None -> no key.
# ---------------------------------------------------------------------------
def test_files_persisted_as_given(plan_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    files = ["pipeline/foo.py", "REFERENCE.md"]
    _write_plan(plan_dir, "persist", _plan(tmp_path, _story(files=files)))

    result = p.ingest_plan("persist")

    assert result["ok"] is True
    manifest = _read_manifest(plan_dir, "persist")
    assert manifest["stories"]["S1"]["files"] == files


def test_absent_files_writes_no_key(plan_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    _write_plan(plan_dir, "absent", _plan(tmp_path, _story()))

    result = p.ingest_plan("absent")

    assert result["ok"] is True
    manifest = _read_manifest(plan_dir, "absent")
    assert "files" not in manifest["stories"]["S1"]


def test_none_files_writes_no_key(plan_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    _write_plan(plan_dir, "nonefiles", _plan(tmp_path, _story(files=None)))

    result = p.ingest_plan("nonefiles")

    assert result["ok"] is True
    manifest = _read_manifest(plan_dir, "nonefiles")
    assert "files" not in manifest["stories"]["S1"]


def test_empty_files_list_is_accepted_and_persisted(plan_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    _write_plan(plan_dir, "emptyfiles", _plan(tmp_path, _story(files=[])))

    result = p.ingest_plan("emptyfiles")

    assert result["ok"] is True
    manifest = _read_manifest(plan_dir, "emptyfiles")
    assert manifest["stories"]["S1"]["files"] == []


# ---------------------------------------------------------------------------
# 3. Re-ingest: a changed list replaces; dropping files clears the key.
# ---------------------------------------------------------------------------
def test_reingest_changed_files_list_replaces(plan_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    plan = _plan(tmp_path, _story(files=["pipeline/a.py"]))
    _write_plan(plan_dir, "replace", plan)
    assert p.ingest_plan("replace")["ok"] is True

    plan["epics"][0]["stories"][0]["files"] = ["pipeline/b.py", "docs/x.md"]
    _write_plan(plan_dir, "replace", plan)
    result = p.ingest_plan("replace")

    assert result["ok"] is True
    manifest = _read_manifest(plan_dir, "replace")
    assert manifest["stories"]["S1"]["files"] == ["pipeline/b.py", "docs/x.md"]


def test_reingest_dropping_files_removes_the_key(plan_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    plan = _plan(tmp_path, _story(files=["pipeline/a.py"]))
    _write_plan(plan_dir, "drop", plan)
    assert p.ingest_plan("drop")["ok"] is True
    assert _read_manifest(plan_dir, "drop")["stories"]["S1"]["files"] == [
        "pipeline/a.py"
    ]

    # Re-ingest the same key with `files` removed from the plan story.
    del plan["epics"][0]["stories"][0]["files"]
    _write_plan(plan_dir, "drop", plan)
    result = p.ingest_plan("drop")

    assert result["ok"] is True
    manifest = _read_manifest(plan_dir, "drop")
    assert "files" not in manifest["stories"]["S1"]


# ---------------------------------------------------------------------------
# 4. "files" is an authored story field refreshed on re-ingest.
# ---------------------------------------------------------------------------
def test_files_is_an_authored_story_field():
    # Membership only: the tuple is cumulative and later stories extend it.
    assert "files" in p._INGEST_AUTHORED_STORY_FIELDS


# ---------------------------------------------------------------------------
# 5. Validation is up front: every rejection writes no manifest.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("files, fragment", _INVALID_FILES)
def test_invalid_files_rejected_without_writing_a_manifest(
    plan_dir, monkeypatch, tmp_path, files, fragment
):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    _write_plan(plan_dir, "badfiles", _plan(tmp_path, _story(files=files)))

    result = p.ingest_plan("badfiles")

    assert result["ok"] is False
    assert "Do the thing" in result["error"]
    if fragment is not None:
        assert fragment in result["error"]
    # Validation runs before any side effect: no manifest is written.
    assert not _manifest_path(plan_dir, "badfiles").exists()


# ---------------------------------------------------------------------------
# 6. Docs: REFERENCE.md gains a "Story file scope (`files`)" section.
# ---------------------------------------------------------------------------
def test_reference_has_story_file_scope_section_before_per_role_config():
    text = REFERENCE.read_text()
    heading = "## Story file scope (`files`)"
    anchor = "## Per-role provider/model configuration"
    assert heading in text, "REFERENCE.md must document the story `files` field"
    assert anchor in text
    assert text.index(heading) < text.index(anchor)

    body = text[text.index(heading) + len(heading):text.index(anchor)]
    assert body.strip(), "the new section must have a body"
    assert "repo-relative" in body
    assert "test" in body.lower()


def test_story_schema_rule_documents_files_field():
    text = STORY_SCHEMA_RULE.read_text()
    # The JSON example's story object gains the field after "backend".
    assert '"files": ["pipeline/foo.py", "REFERENCE.md"],' in text

    lines = text.splitlines()
    backend_line = next(
        i for i, line in enumerate(lines) if line.startswith("- `backend`")
    )
    files_lines = [
        i for i, line in enumerate(lines) if line.startswith("- ") and "`files`" in line
    ]
    assert files_lines, "the rule file must document the `files` field"
    assert min(files_lines) > backend_line, "the `files` bullet must follow `backend`"
