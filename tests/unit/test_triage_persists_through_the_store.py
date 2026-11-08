"""The triage writers persist through the record store (ADR 0012, stage B1c).

Every triage site that used to read/write ``<plan>.manifest.json`` by path now
goes through ``Store.records()``: ``get_manifest`` for a fresh read and
``put_manifest`` for the write. A fake store records the calls and can mutate
an unrelated story between the two reads, which is the only way to prove the
sweep merges onto the *fresh* record instead of clobbering a concurrent edit.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from pipeline import plan_conflict_ruling as pcr
from pipeline import server, store_records, triage
from pipeline import triage_scope_actions as tsa
from pipeline.store_records import InvalidRecordKeyError

TENANT = "default"


def _validate(plan_name):
    """Apply the real store's plan-name rule: an illegal key raises a ValueError."""
    validator = getattr(store_records, "validate_plan_name", None)
    assert validator is not None, "store_records must expose validate_plan_name"
    return validator(plan_name)


class FakeRecordStore:
    """Minimal record view: fresh deep copies, call counting, one-shot hook."""

    def __init__(self, manifests: dict[str, dict] | None = None) -> None:
        self._manifests = {k: copy.deepcopy(v) for k, v in (manifests or {}).items()}
        self.get_calls: list[tuple[str, str]] = []
        self.put_calls: list[tuple[str, str, dict]] = []
        self.on_second_read = None  # hook(store) run between the 1st and 2nd read

    def get_manifest(self, namespace, plan_name):
        _validate(plan_name)
        assert namespace == TENANT
        self.get_calls.append((namespace, plan_name))
        snapshot = copy.deepcopy(self._manifests.get(plan_name))
        if len(self.get_calls) == 1 and self.on_second_read is not None:
            hook, self.on_second_read = self.on_second_read, None
            hook(self)
        return snapshot

    def put_manifest(self, namespace, plan_name, record):
        _validate(plan_name)
        assert namespace == TENANT
        self.put_calls.append((namespace, plan_name, copy.deepcopy(record)))
        self._manifests[plan_name] = copy.deepcopy(record)

    # test helpers -----------------------------------------------------
    def mutate_story(self, plan_name, key, **fields):
        self._manifests[plan_name]["stories"][key].update(fields)


class FakeStore:
    def __init__(self, records: FakeRecordStore) -> None:
        self._records = records

    def manifest_path(self, plan_name: str) -> Path:
        return Path("/nonexistent-plans") / f"{plan_name}.manifest.json"

    def records(self) -> FakeRecordStore:
        return self._records


@pytest.fixture
def fake_store(monkeypatch):
    """Install a fake store as the process-wide store, plus quiet notifications."""
    def _install(manifests=None):
        records = FakeRecordStore(manifests)
        monkeypatch.setattr(server, "_store", FakeStore(records))
        monkeypatch.setattr(triage, "_notify_user", lambda *a, **k: None)
        monkeypatch.setattr(tsa, "_notify_user", lambda *a, **k: None)
        monkeypatch.setattr(pcr, "_notify_user", lambda *a, **k: None)
        monkeypatch.setenv("PIPELINE_AUTO_TRIAGE", "1")
        return records

    return _install


# ---- run_triage_sweep ------------------------------------------------------


def test_should_merge_sweep_changes_onto_the_fresh_record(fake_store):
    records = fake_store(
        {
            "demo": {
                "stories": {
                    # At the attempt cap: the sweep parks it without an LLM call.
                    "S1": {"status": "failed", "triage_attempts": 999},
                    "S2": {"status": "todo"},
                }
            }
        }
    )
    # Between the sweep's initial read and its re-read, a concurrent writer
    # finishes S2. The sweep never saw that change.
    records.on_second_read = lambda store: store.mutate_story(
        "demo", "S2", status="done"
    )

    result = triage.run_triage_sweep("demo")

    assert result.get("ok") is True
    assert result.get("triaged") == []
    assert len(records.put_calls) == 1
    namespace, plan_name, written = records.put_calls[0]
    assert (namespace, plan_name) == (TENANT, "demo")
    assert written["stories"]["S1"]["status"] == "parked"
    # The concurrent edit survives: only the sweep's own diff is applied.
    assert written["stories"]["S2"]["status"] == "done"

    # Only a follow-up read catches a merge onto the stale baseline.
    stored = records.get_manifest(TENANT, "demo")
    assert stored["stories"]["S1"]["status"] == "parked"
    assert stored["stories"]["S2"]["status"] == "done"


def test_should_skip_with_no_manifest_when_the_store_has_none(fake_store, tmp_path):
    records = fake_store({})

    result = triage.run_triage_sweep("demo")

    assert result == {"ok": True, "skipped": "no_manifest"}
    assert records.put_calls == []


def test_should_skip_a_paused_plan(fake_store, monkeypatch, tmp_path):
    paused = {"paused": True, "stories": {"S1": {"status": "todo"}}}
    records = fake_store({"demo": copy.deepcopy(paused)})
    # The legacy plan directory holds the same record, so this guard holds
    # before and after the store migration.
    monkeypatch.setattr(server, "PLAN_DIR", tmp_path)
    (tmp_path / "demo.manifest.json").write_text(json.dumps(paused))

    result = triage.run_triage_sweep("demo")

    assert result == {"ok": True, "skipped": "plan_paused"}
    assert records.put_calls == []


# ---- _execute_amend_scope --------------------------------------------------


def _amend_story(**over):
    story = {
        "status": "parked",
        "risk": "low",
        "files": ["pipeline/foo.py"],
        "worktree": "/tmp/does-not-matter",
        "scope_conflict": {"paths": ["pipeline/bar.py"], "reasons": {}},
    }
    story.update(over)
    return story


def test_should_persist_an_amended_scope_through_the_record_store(
    fake_store, monkeypatch, tmp_path
):
    story = _amend_story()
    records = fake_store({"demo": {"stories": {"S": copy.deepcopy(story)}}})
    monkeypatch.setattr(tsa, "_is_green_at_head", lambda s: True)

    result = tsa._execute_amend_scope(
        "demo",
        "S",
        story,
        {"action": "amend_scope", "rationale": "because"},
        {"stories": {"S": story}},
        tmp_path / "demo.manifest.json",
    )

    assert result == "amend_scope"
    assert len(records.put_calls) == 1
    namespace, plan_name, written = records.put_calls[0]
    assert (namespace, plan_name) == (TENANT, "demo")
    assert written["stories"]["S"]["files"] == ["pipeline/foo.py", "pipeline/bar.py"]
    assert written["stories"]["S"]["status"] == "tests_passed"

    # A later read sees this write: the amendment went to the record store,
    # not to a snapshot.
    stored = records.get_manifest(TENANT, "demo")
    assert stored["stories"]["S"]["files"] == ["pipeline/foo.py", "pipeline/bar.py"]
    assert stored["stories"]["S"]["scope_amendments"][0]["path"] == "pipeline/bar.py"


# ---- _plan_conflict_intercept ---------------------------------------------


def _patch_conflict_seams(monkeypatch, ruling):
    monkeypatch.setattr(pcr, "failed_node_ids", lambda stdout: ["tests/test_x.py::t"])
    monkeypatch.setattr(pcr, "failing_test_files", lambda node_ids: ["tests/test_x.py"])
    monkeypatch.setattr(pcr, "_first_review_base", lambda worktree: "base")
    monkeypatch.setattr(pcr, "branch_file_sets", lambda worktree, base: (set(), set()))
    monkeypatch.setattr(pcr, "_production_diff", lambda worktree, base, paths: "")
    monkeypatch.setattr(pcr, "rule_on_plan_conflict", lambda *a, **k: dict(ruling))


class _Result:
    stdout = "FAILED tests/test_x.py::t"


def test_should_persist_a_plan_conflict_ruling_through_the_record_store(
    fake_store, monkeypatch
):
    ruling = {
        "ruling": "PREAUTHORIZE_TEST_EDIT",
        "file": "tests/test_x.py",
        "test": "t",
        "before": "x = 1",
        "after": "x = 2",
        "replacement_assertion": "assert x == 2",
        "justification": "pre-existing",
    }
    story = {"status": "failed", "rework_attempts": 2}
    records = fake_store({"demo": {"stories": {"S": copy.deepcopy(story)}}})
    _patch_conflict_seams(monkeypatch, ruling)

    out = pcr._plan_conflict_intercept(
        "demo",
        "S",
        story,
        _Result(),
        "/wt",
        {"stories": {"S": story}, "role_config": {"overlord": "m"}},
        Path("/nonexistent-plans/demo.manifest.json"),
        4242,
    )

    assert out == {"status": "changes_requested", "pid": 4242, "plan_conflict": "preauthorized"}
    assert len(records.put_calls) == 1
    namespace, plan_name, written = records.put_calls[0]
    assert (namespace, plan_name) == (TENANT, "demo")
    assert written["stories"]["S"]["plan_conflict_ruling"]["files"] == ["tests/test_x.py"]

    stored = records.get_manifest(TENANT, "demo")
    assert stored["stories"]["S"]["plan_conflict_ruling"]["ruling"] == "PREAUTHORIZE_TEST_EDIT"


# ---- fail-closed on a key the store rejects -------------------------------


def test_should_fail_closed_on_an_invalid_plan_name(fake_store, monkeypatch):
    records = fake_store({"plan-ok": {"stories": {}}})
    _patch_conflict_seams(
        monkeypatch,
        {
            "ruling": "PREAUTHORIZE_TEST_EDIT",
            "file": "tests/test_x.py",
            "test": "t",
            "before": "a",
            "after": "b",
            "replacement_assertion": "assert 1",
            "justification": "j",
        },
    )

    # The sweep reports the rejected key instead of silently skipping it and
    # writes nothing at all.
    result = triage.run_triage_sweep("-plan")
    assert result == {"ok": False, "error": "InvalidRecordKeyError"}
    assert records.put_calls == []

    # The amend-scope site raises before touching anything.
    story = _amend_story()
    with pytest.raises(InvalidRecordKeyError):
        tsa._execute_amend_scope(
            "-plan",
            "S",
            story,
            {"action": "amend_scope", "rationale": "because"},
            {"stories": {"S": story}},
            Path("/nonexistent-plans/-plan.manifest.json"),
        )
    assert records.put_calls == []

    # The plan-conflict site fails open (returns None) without writing.
    assert (
        pcr._plan_conflict_intercept(
            "-plan",
            "S",
            {"status": "failed", "rework_attempts": 1},
            _Result(),
            "/wt",
            {"stories": {}, "role_config": {}},
            Path("/nonexistent-plans/-plan.manifest.json"),
            4242,
        )
        is None
    )
    assert records.put_calls == []

    # A rejected key is never mistaken for "no manifest", and the neighbour
    # record is untouched.
    with pytest.raises(InvalidRecordKeyError):
        records.get_manifest(TENANT, "-plan")
    assert records.get_manifest(TENANT, "plan-ok") == {"stories": {}}
