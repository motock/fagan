"""px-05: per-namespace advance in the scheduler, and namespace binding in children.

The scheduler tick advances the default namespace exactly as before, then each
other namespace that holds plans, inside ``bind_namespace``. A worker or review
job receives the namespace it must run under as an explicit argument; a request
for a namespace the plan does not declare is refused and audited, never run.

Default-only installs take the unchanged path and must return the same result.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import pipeline.server
from pipeline import namespace_guard, scheduler_namespaces
from pipeline.actor import current_namespace
from pipeline.store_memory import InMemoryRecordStore

SENTINEL = {"ok": True, "plans": {"default-plan": {"ok": True}}}


class _FakeStore:
    """Stands in for ``server._store``: exposes a namespaced record view."""

    def __init__(self, records: object | None) -> None:
        if records is not None:
            self.records = lambda: records


class _Namespaced(InMemoryRecordStore):
    """A record view whose enumeration capability can be made to report junk."""

    def __init__(self, reported: list[str] | None = None) -> None:
        super().__init__()
        self._reported = reported

    def list_namespaces(self) -> list[str]:
        if self._reported is not None:
            return list(self._reported)
        return super().list_namespaces()


@pytest.fixture
def audits(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    recorded: list[dict] = []

    def fake_append(**kwargs):
        recorded.append(kwargs)
        return {}

    monkeypatch.setattr(namespace_guard, "append_host_audit", fake_append)
    return recorded


@pytest.fixture
def advance_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Record each in-process advance with the namespace bound while it ran."""
    calls: list[tuple[str, str]] = []

    def fake_advance(plan_name: str) -> dict:
        calls.append((current_namespace(), plan_name))
        return {"ok": True, "plan": plan_name}

    monkeypatch.setattr(pipeline.server, "advance_pipeline", fake_advance)
    monkeypatch.setattr(pipeline.server, "advance_all_plans", lambda: dict(SENTINEL))
    return calls


def _install(monkeypatch: pytest.MonkeyPatch, store: object) -> None:
    monkeypatch.setattr(pipeline.server, "_store", store)


# ---------------------------------------------------------------------------
# scheduler tick: namespaces advance independently
# ---------------------------------------------------------------------------


def test_two_namespaces_advance_their_own_plans_and_see_only_their_records(
    monkeypatch, advance_calls, audits
) -> None:
    records = _Namespaced()
    records.put_manifest("alpha", "p-alpha", {"namespace": "alpha", "stories": {}})
    records.put_manifest("beta", "p-beta", {"namespace": "beta", "stories": {}})
    _install(monkeypatch, _FakeStore(records))

    visible: dict[str, list[str]] = {}

    def fake_advance(plan_name: str) -> dict:
        namespace = current_namespace()
        visible[namespace] = records.list_plans(namespace)
        advance_calls.append((namespace, plan_name))
        return {"ok": True}

    monkeypatch.setattr(pipeline.server, "advance_pipeline", fake_advance)

    result = scheduler_namespaces.advance_all_namespaces()

    assert advance_calls == [("alpha", "p-alpha"), ("beta", "p-beta")]
    assert visible == {"alpha": ["p-alpha"], "beta": ["p-beta"]}
    assert result["namespaces"] == {
        "alpha": {"ok": True, "plans": {"p-alpha": {"ok": True}}},
        "beta": {"ok": True, "plans": {"p-beta": {"ok": True}}},
    }
    assert audits == []


def test_a_plan_declaring_another_namespace_is_refused_and_audited(
    monkeypatch, advance_calls, audits
) -> None:
    records = _Namespaced()
    records.put_manifest("alpha", "plan-zeta", {"namespace": "beta", "stories": {}})
    _install(monkeypatch, _FakeStore(records))

    result = scheduler_namespaces.advance_all_namespaces()

    assert advance_calls == []
    assert result["namespaces"]["alpha"]["plans"]["plan-zeta"] == {
        "ok": False,
        "error": "namespace_mismatch",
    }
    assert len(audits) == 1
    assert audits[0]["detail"] == {"reason": "namespace_mismatch", "correlation_id": ""}
    assert "plan-zeta" not in json.dumps(audits)


# ---------------------------------------------------------------------------
# default-only installs: the unchanged path
# ---------------------------------------------------------------------------


def test_default_only_records_return_the_unchanged_default_result(
    monkeypatch, advance_calls
) -> None:
    records = _Namespaced()
    records.put_manifest("default", "p", {"stories": {}})
    _install(monkeypatch, _FakeStore(records))

    result = scheduler_namespaces.advance_all_namespaces()

    assert result == SENTINEL
    assert "namespaces" not in result
    assert advance_calls == []


def test_a_store_without_a_record_view_returns_the_unchanged_default_result(
    monkeypatch, advance_calls
) -> None:
    _install(monkeypatch, _FakeStore(None))

    result = scheduler_namespaces.advance_all_namespaces()

    assert result == SENTINEL
    assert advance_calls == []


# ---------------------------------------------------------------------------
# negative: the enumeration capability reports an invalid namespace
# ---------------------------------------------------------------------------


def test_an_invalid_reported_namespace_is_not_skipped_silently(
    monkeypatch, advance_calls
) -> None:
    records = _Namespaced(reported=["Bad Name!"])
    records.put_manifest("alpha", "p", {"namespace": "alpha", "stories": {}})
    _install(monkeypatch, _FakeStore(records))

    result = scheduler_namespaces.advance_all_namespaces()

    assert advance_calls == []
    assert result["namespace_error"] == "namespace_enumeration_failed"
    assert "namespaces" not in result


# ---------------------------------------------------------------------------
# advance worker: the request names a namespace
# ---------------------------------------------------------------------------


def _worker():
    import pipeline.advance_worker

    return pipeline.advance_worker


@pytest.fixture
def manifest(monkeypatch: pytest.MonkeyPatch) -> dict:
    """The manifest the child reads for its plan; tests set its contents."""
    state: dict = {}
    monkeypatch.setattr(
        "pipeline.persistence._plan_manifest", lambda name: state.get("manifest")
    )
    return state


def test_worker_refuses_a_tampered_namespace_without_advancing(
    manifest, advance_calls, audits
) -> None:
    manifest["manifest"] = {"stories": {}}

    result = _worker()._advance("demo-plan", "other")

    assert result == {"ok": False, "error": "namespace_mismatch"}
    assert advance_calls == []
    assert [a["detail"]["reason"] for a in audits] == ["namespace_mismatch"]


def test_worker_refuses_a_malformed_requested_namespace(
    manifest, advance_calls, audits
) -> None:
    manifest["manifest"] = {"stories": {}}

    result = _worker()._advance("demo-plan", "../escape")

    assert result == {"ok": False, "error": "namespace_malformed"}
    assert advance_calls == []


def test_worker_binds_the_requested_namespace_when_the_plan_declares_it(
    manifest, advance_calls, audits
) -> None:
    manifest["manifest"] = {"namespace": "alpha", "stories": {}}

    result = _worker()._advance("demo-plan", "alpha")

    assert result == {"ok": True, "plan": "demo-plan"}
    assert advance_calls == [("alpha", "demo-plan")]
    assert audits == []


def test_worker_without_a_requested_namespace_is_unchanged(
    manifest, advance_calls, audits
) -> None:
    manifest["manifest"] = None

    result = _worker()._advance("demo-plan")

    assert result == {"ok": True, "plan": "demo-plan"}
    assert advance_calls == [("default", "demo-plan")]
    assert audits == []


# ---------------------------------------------------------------------------
# advance_one: optional trailing namespace argument
# ---------------------------------------------------------------------------


def _advance_one():
    import pipeline.advance_one

    return pipeline.advance_one


def test_advance_one_refuses_a_mismatched_namespace_argument_with_an_audit(
    tmp_path: Path, manifest, advance_calls, audits
) -> None:
    manifest["manifest"] = {"namespace": "alpha", "stories": {}}
    result_path = tmp_path / "out.json"

    assert _advance_one().main(["demo-plan", str(result_path), "beta"]) == 1

    assert advance_calls == []
    assert json.loads(result_path.read_text()) == {
        "ok": False,
        "error": "namespace_mismatch",
    }
    assert [a["detail"]["reason"] for a in audits] == ["namespace_mismatch"]


def test_advance_one_rejects_more_than_one_namespace_argument(tmp_path: Path) -> None:
    assert _advance_one().main(
        ["demo-plan", str(tmp_path / "out.json"), "alpha", "extra"]
    ) == 2


# ---------------------------------------------------------------------------
# review job: optional trailing namespace argument
# ---------------------------------------------------------------------------


def _review_job():
    import pipeline.review_job

    return pipeline.review_job


def test_review_job_refuses_a_mismatched_namespace_without_reviewing(
    tmp_path: Path, monkeypatch, manifest, audits
) -> None:
    manifest["manifest"] = {"namespace": "alpha", "stories": {}}
    calls: list[str] = []
    monkeypatch.setattr(
        pipeline.server, "review_story", lambda *a: calls.append("ran") or {}
    )
    result_path = tmp_path / "out.json"

    assert _review_job().main(["P", "S-1", str(result_path), "beta"]) == 1

    assert calls == []
    assert json.loads(result_path.read_text()) == {
        "state": "error",
        "error": "namespace_mismatch",
    }
    assert [a["detail"]["reason"] for a in audits] == ["namespace_mismatch"]


def test_review_job_reviews_under_the_matching_namespace(
    tmp_path: Path, monkeypatch, manifest, audits
) -> None:
    manifest["manifest"] = {"namespace": "alpha", "stories": {}}
    seen: list[str] = []

    def fake_review(plan, story):
        seen.append(current_namespace())
        return {"status": "pass"}

    monkeypatch.setattr(pipeline.server, "review_story", fake_review)
    result_path = tmp_path / "out.json"

    assert _review_job().main(["P", "S-1", str(result_path), "alpha"]) == 0

    assert seen == ["alpha"]
    assert json.loads(result_path.read_text())["state"] == "done"


def test_review_job_rejects_more_than_one_namespace_argument(tmp_path: Path) -> None:
    assert _review_job().main(
        ["P", "S-1", str(tmp_path / "out.json"), "alpha", "extra"]
    ) == 2
