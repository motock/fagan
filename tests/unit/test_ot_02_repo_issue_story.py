"""Guard for the ``OT-02-repo-issue`` follow-up story record.

``OT-02`` was parked with a stale ``parked_reason``. The work was committed and
pushed at ``f8453e5`` on ``agent/ot-02`` (PR #1347 open) while the pipeline
recorded "no new commit", because an unmeasurable new-commits check collapsed to
``False``. Two pipeline defects caused that churn:

(a) ``pipeline.git_ops._worktree_new_commits`` returns ``("unknown", base)`` when
    no base branch resolves, ``pipeline.git_ops._worktree_has_new_commits``
    reduces that tri-state to ``state == "yes"`` -> ``False``, and
    ``pipeline.story_status`` parks the story as "no new commits";
(b) dispatched sessions cannot call ``request_decision`` (``app/backend_claude.py``
    runs ``--strict-mcp-config`` with no ``--mcp-config``), so the agent had to
    self-report ``SCOPE-CONFLICT``.

This test asserts the record is *storable* in the story store the pipeline
actually reads (``pipeline.triage_repo_issue._execute_repo_issue``) and that its
brief names the real code paths, not invented state. It asserts symbols and
behaviour, never sentences, so the story can be edited without breaking tests.
"""

import json
import re
from pathlib import Path

import pytest

STORY_KEY = "OT-02-repo-issue"
STORY_PATH = "stories/OT-02-repo-issue.md"


@pytest.fixture
def repo_root():
    """The checkout under test; this file lives at tests/unit/<name>.py."""
    return Path(__file__).resolve().parents[2]

# Documented story schema (.claude/rules/pipeline-story-schema.md). Runtime-only
# keys written by the dispatcher are intentionally absent: a hand-authored
# record must not set them.
SCHEMA_KEYS = {
    "key",
    "summary",
    "description",
    "agent_instructions",
    "acceptance",
    "dependencies",
    "persona",
    "model",
    "risk",
    "backend",
    "files",
    "blocked_by",
    # Manifest lifecycle field the executor sets when it files the child.
    "status",
}
FORBIDDEN_KEYS = {"id", "title", "acceptance_criteria", "depends_on"}

FENCE = re.compile(r"^```json\s*$(.*?)^```\s*$", re.DOTALL | re.MULTILINE)


def _records(root):
    out = []
    for path in sorted((root / "stories").glob("*.md")):
        match = FENCE.search(path.read_text(encoding="utf-8"))
        if match is not None:
            out.append((path, json.loads(match.group(1))))
    return out


def _story(repo_root):
    path = repo_root / STORY_PATH
    assert path.is_file(), f"{STORY_KEY} story record missing: {path}"
    text = path.read_text(encoding="utf-8")
    match = FENCE.search(text)
    assert match is not None, f"{path} carries no ```json story record"
    return text, json.loads(match.group(1))


def test_store_holds_exactly_one_record_for_the_key(repo_root):
    matches = [r for _, r in _records(repo_root) if r.get("key") == STORY_KEY]
    assert len(matches) == 1, (
        f"expected exactly 1 {STORY_KEY} record, found {len(matches)}: "
        "the store write must be idempotent, not append/suffixed"
    )


def test_record_is_valid_against_the_story_schema(repo_root):
    _, record = _story(repo_root)
    assert set(record) <= SCHEMA_KEYS, f"non-schema keys: {sorted(set(record) - SCHEMA_KEYS)}"
    assert not set(record) & FORBIDDEN_KEYS
    assert record["key"] == STORY_KEY
    assert record["summary"].startswith(STORY_KEY)
    assert record["status"] == "todo"  # the executor writes "todo", not "open"
    assert record["persona"] and record["risk"] in {"low", "medium", "high"}
    assert isinstance(record["files"], list) and record["files"]
    assert isinstance(record["agent_instructions"], str)
    assert len(record["agent_instructions"]) > 500, "the brief must be substantive"


def test_brief_names_the_real_park_path_and_never_invents_state(repo_root):
    text, record = _story(repo_root)
    brief = record["agent_instructions"]
    for symbol in (
        "pipeline/git_ops.py",
        "_worktree_new_commits",
        "_worktree_has_new_commits",
        "story_status",
        "unknown",
    ):
        assert symbol in brief, f"brief does not name {symbol}"
    # The unmeasurable state must escalate, not park as "no new commit".
    assert "escalate" in brief
    # Guard against the misdirection this record previously carried: no
    # persistent high-water mark exists in pipeline/, so none may be mandated.
    assert "high_water" not in text and "high-water mark must not" not in brief


def test_brief_names_the_missing_request_decision_tool(repo_root):
    _, record = _story(repo_root)
    brief = record["agent_instructions"]
    for symbol in (
        "request_decision",
        "SCOPE-CONFLICT",
        "--strict-mcp-config",
        "--mcp-config",
        "app/backend_claude.py",
    ):
        assert symbol in brief, f"brief does not name {symbol}"


def test_brief_records_the_hold_and_parent_evidence(repo_root):
    _, record = _story(repo_root)
    brief = record["agent_instructions"]
    for token in ("f8453e5", "agent/ot-02", "1347"):
        assert token in brief, f"brief does not cite {token}"
    assert "do NOT merge PR #1347" in brief
    assert "do NOT redispatch OT-02" in brief
    assert "do NOT mark OT-02 done" in brief


def test_brief_records_the_scope_adjudication(repo_root):
    _, record = _story(repo_root)
    brief = record["agent_instructions"]
    assert "IN SCOPE" in brief
    assert "PipelineService.dispatch_story" in brief
    assert "_adjudicate_merges" in brief
    assert "forbidden" in brief


def test_brief_requires_a_clean_main_diff_before_merge(repo_root):
    _, record = _story(repo_root)
    brief = record["agent_instructions"]
    assert "clean main" in brief
    for stray in ("app/oidc_login.py", "pipeline/authorization.py", "deploy/*"):
        assert stray in brief, f"pre-merge note does not flag {stray}"


def test_filing_into_the_plan_manifest_is_an_idempotent_upsert(repo_root, monkeypatch):
    """The real executor must add the child once and never duplicate it."""
    triage = pytest.importorskip("pipeline.triage")
    execute = pytest.importorskip("pipeline.triage_repo_issue")._execute_repo_issue

    monkeypatch.setattr(triage, "plan_triage_budget_exhausted", lambda manifest: False)
    monkeypatch.setattr(triage, "_park", lambda *args, **kwargs: "parked")

    manifest = {"stories": {}}
    parent = {"key": "OT-02", "summary": "OT-02", "status": "failed"}
    for _ in range(2):
        execute("observability-otel", "OT-02", parent, {"ruling": "hold"}, manifest, repo_root / "m.json")

    assert list(manifest["stories"]) == [STORY_KEY]
    child = manifest["stories"][STORY_KEY]
    assert child["status"] == "todo"
    assert set(child) <= SCHEMA_KEYS


def test_repo_record_can_be_filed_without_losing_its_brief(repo_root):
    """The in-repo copy and the manifest record are the same schema shape."""
    _, record = _story(repo_root)
    assert set(record) <= SCHEMA_KEYS
    assert record["agent_instructions"].strip()
    assert "ACCEPTANCE CRITERIA" in record["agent_instructions"]
    assert "acceptance" not in record  # none is claimed; no empty-list stub
