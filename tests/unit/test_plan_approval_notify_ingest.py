"""The ingest emit site for ``plan_pending_approval`` (ENT-LC-01).

``pipeline/plan_approval_notice.py`` words and delivers the plan-approval
notices; ``pipeline/ingest.py`` is the emit site for a plan that has just been
left *pending approval*. The contract pinned here:

* the notice is emitted **only** while ``PIPELINE_PLAN_APPROVAL`` is on;
* it is emitted **once per (plan, digest)**: a re-ingest of unchanged content
  is silent, a re-ingest of edited content notifies again, and a plan that is
  already decided (``approved`` with a matching digest) is silent;
* it is emitted **after** the manifest is written, so a notifier that raises
  can never lose the manifest or fail the ingest;
* ``pending_notice_due`` is a pure predicate -- it must not mutate the
  approval records it compares.

The dedup rule, stated as a rule: a notice is due when the new record is
``pending_approval`` *and* there is no previous approval record, or the
previous status was not ``pending_approval``, or the previous digest differs
from the new digest.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from pipeline import ingest as ingest_mod
from pipeline import persistence
from pipeline import plan_approval as pa
from pipeline import plan_approval_notice as notice
from pipeline import server as p
from pipeline import ticketing as pt
from pipeline.plan_approval_notice import pending_notice_due
from pipeline.service import PipelineService

PENDING = "plan_pending_approval"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(persistence, "PLAN_DIR", d)
    monkeypatch.setattr(
        pt, "plane_request", lambda *a, **kw: pytest.fail("Plane must not be called")
    )
    return d


@pytest.fixture
def notices(monkeypatch):
    """Record every ``_notify_user`` call; the real one is never invoked."""
    records: list[dict] = []

    def recorder(
        plan_name,
        message,
        *,
        story_key=None,
        severity="info",
        event=None,
        dedup_key=None,
        correlation_id=None,
        attempt=None,
        role=None,
        provider=None,
        model=None,
    ):
        records.append(
            {
                "plan_name": plan_name,
                "message": message,
                "event": event,
                "dedup_key": dedup_key,
                "story_key": story_key,
            }
        )

    monkeypatch.setattr(p, "_notify_user", recorder)
    return records


def _pending(records):
    return [r for r in records if r["event"] == PENDING]


def _write_plan(plan_dir, tmp_path, name="ap", **story_extra):
    story = {"summary": "Do it", "key": "S1", **story_extra}
    plan = {"repo_root": str(tmp_path), "epics": [{"summary": "E", "stories": [story]}]}
    (plan_dir / f"{name}.json").write_text(json.dumps(plan))


def _manifest_path(plan_dir, name="ap"):
    return plan_dir / f"{name}.manifest.json"


def _read_manifest(plan_dir, name="ap"):
    return json.loads(_manifest_path(plan_dir, name).read_text())


# ---------------------------------------------------------------------------
# pending_notice_due(): the dedup rule as a pure predicate
# ---------------------------------------------------------------------------


def test_should_be_due_when_there_is_no_previous_record():
    assert pending_notice_due(None, {"status": "pending_approval", "digest": "a"})
    assert pending_notice_due(
        "not-a-dict", {"status": "pending_approval", "digest": "a"}
    )


def test_should_be_due_when_the_previous_record_was_approved():
    previous = {"status": "approved", "digest": "b"}
    record = {"status": "pending_approval", "digest": "b"}

    assert pending_notice_due(previous, record) is True


def test_should_not_be_due_when_the_previous_record_was_pending_with_the_same_digest():
    previous = {"status": "pending_approval", "digest": "a"}
    record = {"status": "pending_approval", "digest": "a"}

    assert pending_notice_due(previous, record) is False


def test_should_be_due_when_the_previous_pending_record_has_another_digest():
    previous = {"status": "pending_approval", "digest": "a"}
    record = {"status": "pending_approval", "digest": "b"}

    assert pending_notice_due(previous, record) is True


def test_should_be_due_when_the_previous_pending_record_has_no_digest():
    """A legacy record without ``digest`` must not raise ``KeyError``."""
    previous = {"status": "pending_approval"}
    record = {"status": "pending_approval", "digest": "a"}

    assert pending_notice_due(previous, record) is True


def test_should_not_be_due_when_the_new_record_is_not_pending():
    previous = {"status": "pending_approval", "digest": "a"}
    record = {"status": "approved", "digest": "a"}

    assert pending_notice_due(previous, record) is False


@pytest.mark.parametrize(
    "previous, record",
    [
        (None, {"status": "pending_approval", "digest": "a"}),
        ({"status": "approved", "digest": "b"}, {"status": "pending_approval", "digest": "b"}),
        ({"status": "pending_approval", "digest": "a"}, {"status": "pending_approval", "digest": "a"}),
        ({"status": "pending_approval", "digest": "a"}, {"status": "pending_approval", "digest": "b"}),
        ({"status": "pending_approval"}, {"status": "approved", "digest": "x"}),
    ],
)
def test_should_not_mutate_its_arguments(previous, record):
    previous_before = copy.deepcopy(previous)
    record_before = copy.deepcopy(record)

    pending_notice_due(previous, record)

    assert record == record_before
    assert previous == previous_before


# ---------------------------------------------------------------------------
# Ingest emit site
# ---------------------------------------------------------------------------


def test_should_emit_one_notice_on_the_first_enabled_ingest(
    plan_dir, tmp_path, monkeypatch, notices
):
    monkeypatch.setenv(pa.APPROVAL_ENV, "on")
    _write_plan(plan_dir, tmp_path)

    assert p.ingest_plan("ap")["ok"] is True

    emitted = _pending(notices)
    assert len(emitted) == 1
    assert emitted[0]["plan_name"] == "ap"
    assert emitted[0]["dedup_key"].startswith(f"{PENDING}:ap:")
    assert _read_manifest(plan_dir)["approval"]["status"] == "pending_approval"


def test_should_not_emit_a_notice_for_an_identical_reingest(
    plan_dir, tmp_path, monkeypatch, notices
):
    monkeypatch.setenv(pa.APPROVAL_ENV, "on")
    _write_plan(plan_dir, tmp_path)
    assert p.ingest_plan("ap")["ok"] is True
    assert len(_pending(notices)) == 1

    assert p.ingest_plan("ap")["ok"] is True

    assert len(_pending(notices)) == 1


def test_should_emit_a_new_notice_when_the_authored_content_changes(
    plan_dir, tmp_path, monkeypatch, notices
):
    monkeypatch.setenv(pa.APPROVAL_ENV, "on")
    _write_plan(plan_dir, tmp_path, agent_instructions="A")
    assert p.ingest_plan("ap")["ok"] is True
    assert len(_pending(notices)) == 1
    first_digest = _read_manifest(plan_dir)["approval"]["digest"]

    _write_plan(plan_dir, tmp_path, agent_instructions="B")
    assert p.ingest_plan("ap")["ok"] is True

    emitted = _pending(notices)
    assert len(emitted) == 2
    second_digest = _read_manifest(plan_dir)["approval"]["digest"]
    assert second_digest != first_digest
    assert emitted[1]["dedup_key"].startswith(f"{PENDING}:ap:")
    assert emitted[0]["dedup_key"] != emitted[1]["dedup_key"]


def test_should_not_emit_a_notice_after_approval_and_an_identical_reingest(
    plan_dir, tmp_path, monkeypatch, notices
):
    monkeypatch.setenv(pa.APPROVAL_ENV, "on")
    _write_plan(plan_dir, tmp_path)
    assert p.ingest_plan("ap")["ok"] is True
    assert len(_pending(notices)) == 1

    decided = PipelineService().approve_plan("ap")
    assert decided["ok"] is True
    assert decided["status"] == "approved"

    assert p.ingest_plan("ap")["ok"] is True

    assert len(_pending(notices)) == 1
    approval = _read_manifest(plan_dir)["approval"]
    assert approval["status"] == "approved"
    assert approval["digest"] == decided["digest"]


@pytest.mark.parametrize("value", [None, "off"])
def test_should_not_emit_a_notice_when_the_gate_is_off(
    plan_dir, tmp_path, monkeypatch, notices, value
):
    if value is None:
        monkeypatch.delenv(pa.APPROVAL_ENV, raising=False)
    else:
        monkeypatch.setenv(pa.APPROVAL_ENV, value)
    _write_plan(plan_dir, tmp_path)

    assert p.ingest_plan("ap")["ok"] is True

    assert _pending(notices) == []
    assert "approval" not in _read_manifest(plan_dir)


def test_should_still_write_the_manifest_when_the_notifier_raises(
    plan_dir, tmp_path, monkeypatch
):
    monkeypatch.setenv(pa.APPROVAL_ENV, "on")
    _write_plan(plan_dir, tmp_path)

    def boom(plan_name, message, *, event=None, **kwargs):
        if event == PENDING:
            raise RuntimeError("transport down")

    monkeypatch.setattr(p, "_notify_user", boom)

    result = p.ingest_plan("ap")

    assert result["ok"] is True
    manifest = _read_manifest(plan_dir)
    assert manifest["approval"]["status"] == "pending_approval"


def test_should_keep_on_ingest_imported_into_the_ingest_module():
    """The gate call must stay name-bound in ``pipeline.ingest``.

    ``tests/unit/test_plan_approval.py`` monkeypatches
    ``pipeline.ingest.on_ingest``; a call moved into another module would
    silently bypass it.
    """
    assert callable(ingest_mod.on_ingest)


# ---------------------------------------------------------------------------
# Documentation pin
# ---------------------------------------------------------------------------

SPEC = Path(__file__).resolve().parents[2] / "docs" / "specs" / "PLAN_APPROVAL.md"


def test_should_document_the_pending_notice_and_the_link_variable():
    text = SPEC.read_text(encoding="utf-8")

    assert PENDING in text
    assert notice.PUBLIC_URL_ENV in text
