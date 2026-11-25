"""``export_plan`` reads the plan from the namespace bound to the call.

A remote client calls ``export_plan`` inside ``pipeline.actor.bind_namespace``
for its team. Before the fix the read named the ``default`` namespace literally,
so a team's plan answered ``Plan not found`` even though ``list_plans`` (which
is namespace-bound) listed it.

The file store's records refuse every non-default namespace, so the namespaced
cases use the in-memory record store (``pipeline/store_memory.py``) behind a
minimal stand-in for ``pipeline.server._store``. The no-binding case uses the
same stand-in so that all five cases read the same backend.
"""

from __future__ import annotations

import pytest

import pipeline.server as p
from pipeline.actor import bind_namespace
from pipeline.service import PipelineService
from pipeline.store_memory import InMemoryRecordStore

PLAN = "update-spring-boot"


class _StoreStandIn:
    """Exposes ``records()`` over one in-memory record store, as ``_store`` does."""

    def __init__(self, records: InMemoryRecordStore) -> None:
        self._records = records

    def records(self) -> InMemoryRecordStore:
        return self._records


def _manifest(summary: str) -> dict:
    return {
        "repo_root": "/srv/repos/spring",
        "plan_epics": ["Bumps"],
        "stories": {
            "sb-bump-parent": {
                "summary": summary,
                "description": "Bump the parent version.",
                "agent_instructions": "Bump the parent pom to the next release.",
                "epic": "Bumps",
                "persona": "software-engineer",
                "model": "sonnet",
                "risk": "low",
                "status": "done",
            }
        },
    }


@pytest.fixture
def records(monkeypatch: pytest.MonkeyPatch) -> InMemoryRecordStore:
    store = InMemoryRecordStore()
    monkeypatch.setattr(p, "_store", _StoreStandIn(store))
    return store


def test_manifest_in_the_bound_namespace_is_exported(records):
    records.put_manifest("team-a", PLAN, _manifest("Bump parent"))

    with bind_namespace("team-a"):
        result = PipelineService().export_plan(PLAN)

    assert result.get("ok") is True, result
    summaries = [
        story["summary"]
        for epic in result["plan"]["epics"]
        for story in epic["stories"]
    ]
    assert summaries == ["Bump parent"]


def test_manifest_in_another_namespace_is_not_exported(records):
    records.put_manifest("team-a", PLAN, _manifest("Bump parent"))

    with bind_namespace("team-b"):
        result = PipelineService().export_plan(PLAN)

    assert result == {"ok": False, "error": "Plan not found"}


def test_unbound_call_exports_a_manifest_in_the_default_namespace(records):
    records.put_manifest("default", PLAN, _manifest("Bump parent"))

    result = PipelineService().export_plan(PLAN)

    assert result.get("ok") is True, result
    assert result["plan"]["epics"][0]["stories"][0]["summary"] == "Bump parent"


def test_default_namespace_plan_is_not_exported_to_a_bound_team(records):
    records.put_manifest("default", PLAN, _manifest("Bump parent"))

    with bind_namespace("team-a"):
        result = PipelineService().export_plan(PLAN)

    assert result == {"ok": False, "error": "Plan not found"}


def test_invalid_plan_name_is_refused_before_any_namespaced_read(
    records, monkeypatch: pytest.MonkeyPatch
):
    def _boom(*args, **kwargs):
        raise AssertionError("the manifest was read before the plan name was validated")

    monkeypatch.setattr(records, "get_manifest", _boom)

    with bind_namespace("team-a"):
        try:
            result = PipelineService().export_plan("../escape")
        except ValueError as error:
            message = str(error)
        else:
            assert isinstance(result, dict), result
            assert result.get("ok") is False, result
            message = str(result.get("error", ""))

    assert "invalid" in message.lower() or "escape" in message
