"""Plan drafts are staged, read and listed through the record store (ADR 0012).

Three call sites of ``PipelineService`` used to reach into ``PLAN_DIR`` for a
plan draft: ``save_plan`` wrote ``<plan>.json`` itself, ``ingest_plan`` (and
``_ingest_plan_impl``) read it back, and the two listings globbed the
directory.  They now go through the record contract
(``records.put_plan_draft`` / ``get_plan_draft`` / ``list_plan_drafts``), so a
plan draft is a *record* and never a path.

What these tests pin, per the story:

* ``save_plan`` stages the draft through the record store and answers with
  ``{"ok": True, "plan": <name>, "staged": True, ...}`` -- there is no
  filesystem path in the answer any more -- and every refusal (bad JSON, a
  non-object, no ``epics``, a server path from a remote caller, a repo root
  outside the allow-list) still leaves *nothing* staged.
* the draft is read back through ``pipeline.plan_draft.read_plan_draft``, which
  answers with the very refusals ``persistence.read_plan_file`` produces, and
  a ``repo_url`` draft still refreshes its clone at ingest time.
* the listings read records: a plan with a draft or a manifest is listed, a
  bookkeeping sidecar never is, and one malformed draft cannot blind a caller.

The record view is reached through the store's own public ``records()``
accessor, and the draft operations are observed by wrapping the *contract's*
methods (``pipeline.store_records.PlanDraftRecords``) so the test never reads
implementation source text.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# pipeline.server must load before pipeline.service / pipeline.ingest (the
# import-cycle note in tests/unit/test_ingest_keyless_reingest.py).
import pipeline.server as p
from app.identity import Principal
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import remote_scope, repo_clone, service
from pipeline.actor import bind_principal
from pipeline.service import PipelineService
from pipeline.store_records import DEFAULT_NAMESPACE, PlanDraftRecords

REMOTE_PRINCIPAL = Principal(subject="remote-alice", auth_method="oidc")
REPO_URL = "https://github.com/acme/widget.git"

_PLAN = {
    "epics": [
        {
            "summary": "E1",
            "stories": [
                {
                    "key": "issue-1",
                    "summary": "S1",
                    "agent_instructions": "Preflight: test fixture - not a real plan\nDo it.",
                }
            ],
        }
    ]
}


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Point every PLAN_DIR binding at ``tmp_path``/plans."""
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


@pytest.fixture
def records():
    """The namespaced record view of the store the service reads and writes."""
    return p._store.records()


def _stage(records, plan_name: str, plan: dict) -> dict:
    """Stage ``plan`` through the record contract (never a file write)."""
    records.put_plan_draft(DEFAULT_NAMESPACE, plan_name, plan)
    return plan


def _observe_put(monkeypatch):
    """Record every ``put_plan_draft`` call and delegate to the real one."""
    calls: list[tuple] = []
    original = PlanDraftRecords.put_plan_draft

    def put(self, namespace, plan_name, plan):
        calls.append((namespace, plan_name, plan))
        return original(self, namespace, plan_name, plan)

    monkeypatch.setattr(PlanDraftRecords, "put_plan_draft", put)
    return calls


def _observe_get(monkeypatch, draft=None):
    """Record every ``get_plan_draft`` call; answer ``draft`` when given."""
    calls: list[tuple] = []
    original = PlanDraftRecords.get_plan_draft

    def get(self, namespace, plan_name):
        calls.append((namespace, plan_name))
        if draft is not None:
            return draft
        return original(self, namespace, plan_name)

    monkeypatch.setattr(PlanDraftRecords, "get_plan_draft", get)
    return calls


# --------------------------------------------------------------------------- #
# (a) save_plan stages the draft through the record store
# --------------------------------------------------------------------------- #
def test_save_plan_stages_the_draft_through_the_record_store(plan_dir, records, monkeypatch):
    calls = _observe_put(monkeypatch)

    result = PipelineService().save_plan("p1", json.dumps(_PLAN))

    assert result["ok"] is True, result
    assert calls == [(DEFAULT_NAMESPACE, "p1", _PLAN)]
    assert records.get_plan_draft(DEFAULT_NAMESPACE, "p1") == _PLAN


def test_save_plan_answers_with_the_plan_name_and_staged(plan_dir):
    result = PipelineService().save_plan("p1", json.dumps(_PLAN))

    assert result["ok"] is True
    assert result["plan"] == "p1"
    assert result["staged"] is True
    assert result["epic_count"] == 1
    assert result["story_count"] == 1


def test_save_plan_answer_carries_no_filesystem_path(plan_dir):
    result = PipelineService().save_plan("p1", json.dumps(_PLAN))

    assert "path" not in result


def test_save_plan_twice_replaces_the_draft_wholesale(plan_dir, records):
    PipelineService().save_plan("p1", json.dumps(_PLAN))
    second = {"epics": [], "note": "second"}

    PipelineService().save_plan("p1", json.dumps(second))

    assert records.get_plan_draft(DEFAULT_NAMESPACE, "p1") == second


@pytest.mark.parametrize(
    ("plan_json", "expected"),
    [
        ("{not json", "Invalid JSON"),
        ("[]", "must be a JSON object"),
        (json.dumps({"name": "p1"}), "'epics'"),
    ],
    ids=["invalid-json", "not-an-object", "no-epics"],
)
def test_save_plan_refusal_stages_nothing(plan_dir, records, plan_json, expected):
    result = PipelineService().save_plan("p1", plan_json)

    assert result["ok"] is False
    assert expected in result["error"]
    assert records.get_plan_draft(DEFAULT_NAMESPACE, "p1") is None


def test_save_plan_refuses_a_server_path_from_a_remote_caller(plan_dir, records):
    plan = dict(_PLAN, repo_root="/srv/repo")

    with bind_principal(REMOTE_PRINCIPAL):
        result = PipelineService().save_plan("p1", json.dumps(plan))

    assert result["ok"] is False
    assert result["error"] == remote_scope.REMOTE_PLAN_PATH_ERROR
    assert records.get_plan_draft(DEFAULT_NAMESPACE, "p1") is None


def test_save_plan_refuses_a_repo_root_outside_the_allow_list(
    plan_dir, records, tmp_path, monkeypatch
):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setenv("PIPELINE_ALLOWED_REPO_ROOTS", str(allowed))
    plan = dict(_PLAN, repo_root=str(tmp_path / "elsewhere"))

    result = PipelineService().save_plan("p1", json.dumps(plan))

    assert result["ok"] is False
    assert result["error"]
    assert records.get_plan_draft(DEFAULT_NAMESPACE, "p1") is None


def test_save_plan_refuses_an_invalid_workspace_and_stages_nothing(
    plan_dir, records, tmp_path
):
    result = PipelineService().save_plan(
        "p1", json.dumps(_PLAN), workspace=str(tmp_path / "nope" / "deeper")
    )

    assert result["ok"] is False
    assert records.get_plan_draft(DEFAULT_NAMESPACE, "p1") is None


# --------------------------------------------------------------------------- #
# (b) the draft is read back through the record store
# --------------------------------------------------------------------------- #
def test_read_plan_draft_returns_the_staged_draft(plan_dir, records):
    from pipeline import plan_draft

    _stage(records, "p1", _PLAN)

    draft, refusal = plan_draft.read_plan_draft("p1")

    assert refusal is None
    assert draft == _PLAN


def test_read_plan_draft_of_a_missing_plan_is_the_legacy_refusal(plan_dir):
    from pipeline import plan_draft

    draft, refusal = plan_draft.read_plan_draft("nope")

    _, expected = ppers.read_plan_file(plan_dir / "nope.json")
    assert refusal == expected
    assert refusal["ok"] is False
    assert draft is None


def test_read_plan_draft_of_a_malformed_plan_is_the_legacy_refusal(plan_dir, records):
    from pipeline import plan_draft

    (plan_dir / "p1.json").write_text("{ not json")
    _, expected = ppers.read_plan_file(plan_dir / "p1.json")

    draft, refusal = plan_draft.read_plan_draft("p1")

    assert refusal == expected
    assert refusal["ok"] is False
    assert draft is None


def test_ingest_reads_the_draft_through_the_record_store(plan_dir, records, monkeypatch):
    calls = _observe_get(monkeypatch)
    monkeypatch.setattr(service, "_ingest_plan_impl", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(service.wake, "after", lambda name, reason, result: result)

    _stage(records, "p1", _PLAN)

    result = PipelineService().ingest_plan("p1")

    assert result["ok"] is True, result
    assert calls == [(DEFAULT_NAMESPACE, "p1")]


def test_ingest_of_a_missing_draft_is_the_legacy_refusal(plan_dir):
    result = PipelineService().ingest_plan("nope")

    assert result["ok"] is False
    assert result["error"] == "No plan named nope"


def test_ingest_of_a_malformed_draft_is_the_legacy_refusal(plan_dir):
    (plan_dir / "p1.json").write_text("{ not json")

    result = PipelineService().ingest_plan("p1")

    _, expected = ppers.read_plan_file(plan_dir / "p1.json")
    assert result["ok"] is False
    assert result["error"] == expected["error"]


def test_ingest_of_a_staged_draft_succeeds(plan_dir, records, monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("hello\n")
    plan = dict(_PLAN, repo_root=str(repo))

    class _Provider:
        def create_epic(self, summary):
            return "epic-1"

        def create_story(self, summary, description, epic_id, agent):
            return "story-1"

    monkeypatch.setattr(p, "get_ticket_provider", lambda: _Provider())
    monkeypatch.setattr(service, "_notify_user", lambda *a, **k: None)
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")
    monkeypatch.setattr(service.wake, "after", lambda name, reason, result: result)
    _stage(records, "p1", plan)

    result = PipelineService().ingest_plan("p1")

    assert result["ok"] is True, result
    assert (plan_dir / "p1.manifest.json").exists()


def test_ingest_refreshes_the_clone_of_a_repo_url_draft(plan_dir, records, monkeypatch):
    cloned: list[str] = []
    monkeypatch.setattr(
        repo_clone, "ensure_clone", lambda url, *a, **k: cloned.append(url) or Path("/tmp/x")
    )
    monkeypatch.setattr(service, "_ingest_plan_impl", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(service.wake, "after", lambda name, reason, result: result)
    _stage(records, "p1", {"repo_url": REPO_URL, "epics": []})

    result = PipelineService().ingest_plan("p1")

    assert result["ok"] is True, result
    assert cloned == [REPO_URL]


def test_ingest_without_a_repo_url_does_not_clone(plan_dir, records, monkeypatch):
    cloned: list[str] = []
    monkeypatch.setattr(
        repo_clone, "ensure_clone", lambda url, *a, **k: cloned.append(url) or Path("/tmp/x")
    )
    monkeypatch.setattr(service, "_ingest_plan_impl", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(service.wake, "after", lambda name, reason, result: result)
    _stage(records, "p1", {"repo_root": "/srv/repo", "epics": []})

    result = PipelineService().ingest_plan("p1")

    assert result["ok"] is True, result
    assert cloned == []


def test_refresh_plan_clone_of_a_plan_without_a_repo_url_does_nothing():
    assert remote_scope.refresh_plan_clone({"epics": []}) is None


def test_refresh_plan_clone_of_no_plan_does_nothing():
    assert remote_scope.refresh_plan_clone(None) is None


def test_refresh_plan_clone_refetches_the_named_repo_url(monkeypatch):
    cloned: list[str] = []
    monkeypatch.setattr(
        repo_clone, "ensure_clone", lambda url, *a, **k: cloned.append(url) or Path("/tmp/x")
    )

    assert remote_scope.refresh_plan_clone({"repo_url": REPO_URL}) is None
    assert cloned == [REPO_URL]


# --------------------------------------------------------------------------- #
# (c) the listings read records
# --------------------------------------------------------------------------- #
def test_a_draft_only_plan_is_listed_by_both_listings(plan_dir, records):
    _stage(records, "p1", _PLAN)

    assert "p1" in PipelineService().list_plans()
    assert "p1" in p._store.list_plans()
    assert PipelineService().list_ingestable_plans() == ["p1"]


def test_a_manifest_only_plan_is_listed_but_not_ingestable(plan_dir):
    (plan_dir / "p1.manifest.json").write_text(json.dumps({"stories": []}))

    assert "p1" in PipelineService().list_plans()
    assert "p1" in p._store.list_plans()
    assert "p1" not in PipelineService().list_ingestable_plans()


def test_a_plan_with_both_a_draft_and_a_manifest_is_not_ingestable(plan_dir, records):
    _stage(records, "p1", _PLAN)
    (plan_dir / "p1.manifest.json").write_text(json.dumps({"stories": []}))

    assert "p1" in PipelineService().list_plans()
    assert PipelineService().list_ingestable_plans() == []


def test_bookkeeping_files_are_never_listed(plan_dir, records):
    _stage(records, "p1", _PLAN)
    (plan_dir / "p1.decisions.json").write_text("[]")
    (plan_dir / "p1.issue-1.journal.json").write_text("[]")
    (plan_dir / "recent_workspaces.json").write_text(json.dumps({"workspaces": []}))

    listed = PipelineService().list_plans()
    assert listed == ["p1"]
    assert "p1.decisions" not in listed
    assert "p1.issue-1.journal" not in listed
    assert "recent_workspaces" not in listed
    assert PipelineService().list_ingestable_plans() == ["p1"]


def test_an_empty_namespace_lists_nothing(plan_dir):
    assert PipelineService().list_plans() == []
    assert PipelineService().list_ingestable_plans() == []
    assert p._store.list_plans() == []


def test_a_malformed_draft_is_skipped_without_raising(plan_dir):
    (plan_dir / "bad.json").write_text("{ not json")

    assert PipelineService().list_ingestable_plans() == []
    assert "bad" not in PipelineService().list_plans()


def test_the_listings_are_sorted(plan_dir, records):
    _stage(records, "beta", _PLAN)
    _stage(records, "alpha", _PLAN)

    assert PipelineService().list_plans() == ["alpha", "beta"]
    assert PipelineService().list_ingestable_plans() == ["alpha", "beta"]


# --------------------------------------------------------------------------- #
# docs contract: the reference and the changelog describe the new shape
# --------------------------------------------------------------------------- #
_REPO_ROOT = Path(__file__).resolve().parents[2]
_REFERENCE = _REPO_ROOT / "REFERENCE.md"
_CHANGELOG = _REPO_ROOT / "CHANGELOG.md"


def _reference_entry(tool: str) -> str:
    """Return the REFERENCE.md bullet that documents ``tool`` and its wraps."""
    lines = _REFERENCE.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if not line.lstrip().startswith(f"- `{tool}"):
            continue
        entry = [line]
        for follow in lines[index + 1:]:
            if not follow.strip() or follow.startswith("- ") or follow.startswith("#"):
                break
            entry.append(follow)
        return "\n".join(entry)
    raise AssertionError(f"REFERENCE.md documents no entry for {tool}")


def test_reference_save_plan_entry_no_longer_advertises_a_written_file():
    entry = _reference_entry("save_plan(")

    assert "~/.claude/plans" not in entry
    assert '"path"' not in entry


def test_reference_save_plan_entry_says_the_plan_is_staged():
    entry = _reference_entry("save_plan(").lower()

    assert "stag" in entry or "draft" in entry or "record" in entry


def test_reference_list_plans_entry_says_a_draft_or_a_manifest():
    entry = _reference_entry("list_plans(").lower()

    assert "draft" in entry or "manifest" in entry


def test_changelog_records_the_save_plan_response_change():
    text = _CHANGELOG.read_text(encoding="utf-8")

    assert "save_plan" in text
    assert "staged" in text.lower()


def test_changelog_records_the_change_in_the_unreleased_section():
    text = _CHANGELOG.read_text(encoding="utf-8")
    start = text.index("## ")
    rest = text[start + 3:].lstrip("\n")
    end = rest.find("\n## ")
    section = rest if end == -1 else rest[:end]

    assert "save_plan" in section
