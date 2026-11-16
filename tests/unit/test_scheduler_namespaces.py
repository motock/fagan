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


def _tick() -> dict:
    """Run one scheduler tick, resolving the default advance at call time."""
    return scheduler_namespaces.advance_all_namespaces(pipeline.server.advance_all_plans)


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

    result = _tick()

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

    result = _tick()

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

    result = _tick()

    assert result == SENTINEL
    assert "namespaces" not in result
    assert advance_calls == []


def test_a_store_without_a_record_view_returns_the_unchanged_default_result(
    monkeypatch, advance_calls
) -> None:
    _install(monkeypatch, _FakeStore(None))

    result = _tick()

    assert result == SENTINEL
    assert advance_calls == []


def test_the_default_namespace_advance_is_the_function_passed_in(
    monkeypatch, advance_calls
) -> None:
    records = _Namespaced()
    records.put_manifest("default", "p", {"stories": {}})
    _install(monkeypatch, _FakeStore(records))
    passed_in: list[str] = []

    def default_advance() -> dict:
        passed_in.append(current_namespace())
        return dict(SENTINEL)

    result = scheduler_namespaces.advance_all_namespaces(default_advance)

    assert result == SENTINEL
    assert passed_in == ["default"]
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

    result = _tick()

    assert advance_calls == []
    assert result["namespace_error"] == "namespace_enumeration_failed"
    assert "namespaces" not in result


# ---------------------------------------------------------------------------
# advance worker: the request names a namespace
# ---------------------------------------------------------------------------


def _worker():
    import pipeline.advance_worker

    return pipeline.advance_worker


def _install_plan(
    monkeypatch: pytest.MonkeyPatch, stored_in: str, plan_name: str, declared: str | None = None
) -> _Namespaced:
    """Install a real record store holding *plan_name* under *stored_in*.

    The manifest declares *declared* when given; reads are never patched, so a
    child's lookup in the requested namespace is exercised for real.
    """
    records = _Namespaced()
    manifest = {"stories": {}} if declared is None else {"namespace": declared, "stories": {}}
    records.put_manifest(stored_in, plan_name, manifest)
    _install(monkeypatch, _FakeStore(records))
    return records


@pytest.fixture
def manifest(monkeypatch: pytest.MonkeyPatch) -> dict:
    """The manifest the child reads for its plan; tests set its contents."""
    state: dict = {}
    monkeypatch.setattr(
        "pipeline.persistence._plan_manifest", lambda name: state.get("manifest")
    )
    return state


def test_worker_refuses_a_tampered_namespace_without_advancing(
    monkeypatch, advance_calls, audits
) -> None:
    _install_plan(monkeypatch, "beta", "demo-plan", declared="alpha")

    result = _worker()._advance("demo-plan", "beta")

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
    monkeypatch, advance_calls, audits
) -> None:
    _install_plan(monkeypatch, "alpha", "demo-plan", declared="alpha")

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
    tmp_path: Path, monkeypatch, advance_calls, audits
) -> None:
    _install_plan(monkeypatch, "beta", "demo-plan", declared="alpha")
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
    tmp_path: Path, monkeypatch, audits
) -> None:
    _install_plan(monkeypatch, "beta", "P", declared="alpha")
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
    tmp_path: Path, monkeypatch, audits
) -> None:
    _install_plan(monkeypatch, "alpha", "P", declared="alpha")
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


# ---------------------------------------------------------------------------
# children read the manifest from the namespace the parent requested
# ---------------------------------------------------------------------------


@pytest.fixture
def alpha_only_store(monkeypatch: pytest.MonkeyPatch) -> _Namespaced:
    """A real record store holding plans under ``alpha`` only; reads are unpatched."""
    records = _Namespaced()
    records.put_manifest("alpha", "demo-plan", {"stories": {}})
    records.put_manifest("alpha", "P", {"stories": {}})
    _install(monkeypatch, _FakeStore(records))
    return records


def test_worker_binds_a_plan_stored_only_under_the_requested_namespace(
    alpha_only_store, advance_calls, audits
) -> None:
    result = _worker()._advance("demo-plan", "alpha")

    assert result == {"ok": True, "plan": "demo-plan"}
    assert advance_calls == [("alpha", "demo-plan")]
    assert audits == []


def test_advance_one_binds_a_plan_stored_only_under_the_requested_namespace(
    tmp_path: Path, alpha_only_store, advance_calls, audits
) -> None:
    result_path = tmp_path / "out.json"

    assert _advance_one().main(["demo-plan", str(result_path), "alpha"]) == 0

    assert advance_calls == [("alpha", "demo-plan")]
    assert json.loads(result_path.read_text()) == {"ok": True, "plan": "demo-plan"}
    assert audits == []


def test_review_job_reviews_a_plan_stored_only_under_the_requested_namespace(
    tmp_path: Path, monkeypatch, alpha_only_store, audits
) -> None:
    seen: list[str] = []

    def fake_review(plan, story):
        seen.append(current_namespace())
        return {"status": "pass"}

    monkeypatch.setattr(pipeline.server, "review_story", fake_review)
    result_path = tmp_path / "out.json"

    assert _review_job().main(["P", "S-1", str(result_path), "alpha"]) == 0

    assert seen == ["alpha"]
    assert audits == []


def test_worker_refuses_a_plan_the_requested_namespace_does_not_hold(
    alpha_only_store, advance_calls, audits
) -> None:
    result = _worker()._advance("demo-plan", "beta")

    assert result == {"ok": False, "error": "namespace_mismatch"}
    assert advance_calls == []
    assert [a["detail"]["reason"] for a in audits] == ["namespace_mismatch"]


def test_worker_refuses_a_non_string_namespace_and_audits_it(
    manifest, advance_calls, audits
) -> None:
    manifest["manifest"] = {"stories": {}}

    result = _worker()._advance("demo-plan", 7)

    assert result == {"ok": False, "error": "namespace_malformed"}
    assert advance_calls == []
    assert [a["detail"]["reason"] for a in audits] == ["namespace_malformed"]


def test_worker_refuses_a_namespace_the_store_cannot_serve(
    monkeypatch, advance_calls, audits
) -> None:
    _install(monkeypatch, _FakeStore(None))

    result = _worker()._advance("demo-plan", "alpha")

    assert result == {"ok": False, "error": "namespace_unavailable"}
    assert advance_calls == []
    assert [a["detail"]["reason"] for a in audits] == ["namespace_unavailable"]


# ---------------------------------------------------------------------------
# ss-01: PIPELINE_SCHEDULER_NAMESPACES_ONLY / _SKIP scope the tick
# ---------------------------------------------------------------------------


class _Spied(_Namespaced):
    """Records every per-namespace read so exclusion can be asserted."""

    def __init__(self) -> None:
        super().__init__()
        self.reads: list[tuple[str, str]] = []

    def list_plans(self, namespace: str) -> list[str]:
        self.reads.append(("list_plans", namespace))
        return super().list_plans(namespace)

    def get_manifest(self, namespace: str, plan_name: str):
        self.reads.append(("get_manifest", namespace))
        return super().get_manifest(namespace, plan_name)


def _three_namespace_records() -> _Spied:
    records = _Spied()
    records.put_manifest("default", "p-default", {"stories": {}})
    records.put_manifest("alpha", "p-alpha", {"namespace": "alpha", "stories": {}})
    records.put_manifest("beta", "p-beta", {"namespace": "beta", "stories": {}})
    return records


def test_unset_scope_returns_the_same_result_as_before(
    monkeypatch, advance_calls, audits
) -> None:
    monkeypatch.delenv("PIPELINE_SCHEDULER_NAMESPACES_ONLY", raising=False)
    monkeypatch.delenv("PIPELINE_SCHEDULER_NAMESPACES_SKIP", raising=False)
    records = _three_namespace_records()
    _install(monkeypatch, _FakeStore(records))

    result = _tick()

    assert result["ok"] is True
    assert result["plans"] == SENTINEL["plans"]
    assert set(result["namespaces"]) == {"alpha", "beta"}
    assert "namespace_error" not in result
    assert advance_calls == [("alpha", "p-alpha"), ("beta", "p-beta")]


def test_only_one_namespace_advances_only_it_and_not_default(
    monkeypatch, advance_calls, audits
) -> None:
    monkeypatch.setenv("PIPELINE_SCHEDULER_NAMESPACES_ONLY", "alpha")
    records = _three_namespace_records()
    _install(monkeypatch, _FakeStore(records))
    default_calls: list[str] = []

    def default_advance() -> dict:
        default_calls.append("called")
        return dict(SENTINEL)

    result = scheduler_namespaces.advance_all_namespaces(default_advance)

    assert default_calls == []
    assert advance_calls == [("alpha", "p-alpha")]
    assert set(result["namespaces"]) == {"alpha"}
    assert "beta" not in result["namespaces"]
    assert "namespace_error" not in result


def test_only_containing_default_advances_default_and_only_the_listed(
    monkeypatch, advance_calls, audits
) -> None:
    monkeypatch.setenv("PIPELINE_SCHEDULER_NAMESPACES_ONLY", "default,beta")
    records = _three_namespace_records()
    _install(monkeypatch, _FakeStore(records))
    default_calls: list[str] = []

    def default_advance() -> dict:
        default_calls.append("called")
        return dict(SENTINEL)

    result = scheduler_namespaces.advance_all_namespaces(default_advance)

    assert default_calls == ["called"]
    assert advance_calls == [("beta", "p-beta")]
    assert set(result["namespaces"]) == {"beta"}


def test_skip_excludes_one_namespace_and_still_advances_the_rest(
    monkeypatch, advance_calls, audits
) -> None:
    monkeypatch.setenv("PIPELINE_SCHEDULER_NAMESPACES_SKIP", "beta")
    records = _three_namespace_records()
    _install(monkeypatch, _FakeStore(records))

    result = _tick()

    assert result["plans"] == SENTINEL["plans"]
    assert advance_calls == [("alpha", "p-alpha")]
    assert set(result["namespaces"]) == {"alpha"}


def test_skip_naming_default_skips_the_default_advance(
    monkeypatch, advance_calls, audits
) -> None:
    monkeypatch.setenv("PIPELINE_SCHEDULER_NAMESPACES_SKIP", "default")
    records = _three_namespace_records()
    _install(monkeypatch, _FakeStore(records))
    default_calls: list[str] = []

    def default_advance() -> dict:
        default_calls.append("called")
        return dict(SENTINEL)

    scheduler_namespaces.advance_all_namespaces(default_advance)

    assert default_calls == []
    assert advance_calls == [("alpha", "p-alpha"), ("beta", "p-beta")]


def test_a_skipped_namespace_is_never_read_or_advanced(
    monkeypatch, advance_calls, audits
) -> None:
    monkeypatch.setenv("PIPELINE_SCHEDULER_NAMESPACES_SKIP", "beta")
    records = _three_namespace_records()
    _install(monkeypatch, _FakeStore(records))

    _tick()

    assert ("list_plans", "beta") not in records.reads
    assert ("get_manifest", "beta") not in records.reads
    assert all(call[0] != "beta" for call in advance_calls)


def test_an_only_scope_never_reads_an_excluded_namespace(
    monkeypatch, advance_calls, audits
) -> None:
    monkeypatch.setenv("PIPELINE_SCHEDULER_NAMESPACES_ONLY", "alpha")
    records = _three_namespace_records()
    _install(monkeypatch, _FakeStore(records))

    _tick()

    assert ("list_plans", "beta") not in records.reads
    assert ("get_manifest", "beta") not in records.reads
    assert all(call[0] != "beta" for call in advance_calls)


def test_both_variables_set_refuses_and_advances_nothing(
    monkeypatch, advance_calls, audits
) -> None:
    monkeypatch.setenv("PIPELINE_SCHEDULER_NAMESPACES_ONLY", "alpha")
    monkeypatch.setenv("PIPELINE_SCHEDULER_NAMESPACES_SKIP", "beta")
    records = _three_namespace_records()
    _install(monkeypatch, _FakeStore(records))
    default_calls: list[str] = []

    def default_advance() -> dict:
        default_calls.append("called")
        return dict(SENTINEL)

    result = scheduler_namespaces.advance_all_namespaces(default_advance)

    assert result == {"ok": False, "plans": {}, "namespace_error": "scheduler_scope_invalid"}
    assert default_calls == []
    assert advance_calls == []
    assert records.reads == []


@pytest.mark.parametrize("value", ["alpha,,beta", "Bad Name!", "a/b/c"])
def test_malformed_scope_entry_refuses_and_advances_nothing(
    monkeypatch, advance_calls, audits, value
) -> None:
    monkeypatch.setenv("PIPELINE_SCHEDULER_NAMESPACES_SKIP", value)
    records = _three_namespace_records()
    _install(monkeypatch, _FakeStore(records))
    default_calls: list[str] = []

    def default_advance() -> dict:
        default_calls.append("called")
        return dict(SENTINEL)

    result = scheduler_namespaces.advance_all_namespaces(default_advance)

    assert result == {"ok": False, "plans": {}, "namespace_error": "scheduler_scope_invalid"}
    assert default_calls == []
    assert advance_calls == []
    assert records.reads == []


def test_refusal_logs_the_error_class_and_no_namespace_names(
    monkeypatch, advance_calls, audits, caplog
) -> None:
    monkeypatch.setenv("PIPELINE_SCHEDULER_NAMESPACES_ONLY", "secret-team")
    monkeypatch.setenv("PIPELINE_SCHEDULER_NAMESPACES_SKIP", "beta")
    _install(monkeypatch, _FakeStore(_three_namespace_records()))

    with caplog.at_level("ERROR", logger="pipeline.scheduler_namespaces"):
        _tick()

    assert "scheduler scope refused" in caplog.text
    assert "secret-team" not in caplog.text
    assert "beta" not in caplog.text
