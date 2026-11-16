"""The DEFAULT_NAMESPACE ratchet, and the audit layer's request namespace.

Two things are pinned here.

The ratchet counts the production modules that name ``DEFAULT_NAMESPACE``
and holds each count to an allowlist written from the real counts. A module
that gains a use, or a new module that uses it, fails; a module that loses
uses passes, and its allowlist entry should then be lowered in the same
change.

The routing tests show that the audit layer writes and reads the chain of the
namespace bound for the request, that with nothing bound it reads and writes
the default chain exactly as before, and that a store which cannot hold the
bound namespace refuses rather than falling back to the default.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

import pipeline.server
from pipeline import audit, plan_draft, security_audit
from pipeline.actor import bind_namespace, current_namespace
from pipeline.audit_file import FileAuditLog
from pipeline.audit_log import HOST_CHAIN_ID
from pipeline.service import PipelineService
from pipeline.service_levels import read_story_timeline
from pipeline.store_memory import InMemoryRecordStore
from pipeline.store_records import DEFAULT_NAMESPACE, UnknownNamespaceError
from pipeline.store_sqlite import SqliteStore

REPO_ROOT = Path(__file__).resolve().parents[2]
TOKEN = "DEFAULT_NAMESPACE"
SCANNED_DIRECTORIES = ("pipeline", "app")
EXCLUDED_PREFIXES = ("pipeline/testing/",)

NAMESPACE = "a/b"
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
SHA = "a" * 40

#: Production modules that name ``DEFAULT_NAMESPACE`` at this commit, with the
#: number of lines naming it. A module absent from this map may name it zero
#: times. Lower an entry when its module loses uses; never raise one.
ALLOWLIST: dict[str, int] = {
    "pipeline/actor.py": 3,
    "pipeline/audit_anchor.py": 8,
    "pipeline/audit_file.py": 3,
    "pipeline/events_nats_consumer.py": 2,
    "pipeline/health_freshness.py": 2,
    "pipeline/ci.py": 3,
    "pipeline/manifest_source.py": 3,
    "pipeline/metrics_exposition.py": 4,
    "pipeline/namespace_resolver.py": 2,
    "pipeline/persistence.py": 2,
    "pipeline/plan_draft.py": 3,
    "pipeline/settings_tools.py": 5,
    "pipeline/store.py": 5,
    "pipeline/store_contract.py": 101,
    "pipeline/store_contract_notifications.py": 3,
    "pipeline/store_contract_transactions.py": 27,
    "pipeline/store_contract_usage.py": 21,
    "pipeline/store_import_audit.py": 2,
    "pipeline/store_memory.py": 3,
    "pipeline/store_records.py": 6,
    "pipeline/store_sqlite.py": 4,
    "pipeline/store_sqlite_notifications.py": 4,
    "pipeline/store_sqlite_records.py": 7,
    "pipeline/telemetry.py": 2,
    "pipeline/tools/backfill_plan_source.py": 6,
    "pipeline/tools/import_file_store.py": 3,
    "pipeline/audit_contract.py": 69,
}

#: Why each allowlisted module may name ``DEFAULT_NAMESPACE``: one line per entry.
#: An allowlist entry without a reason, or whose module no longer names the
#: constant, fails ``test_every_allowlist_entry_has_a_reason_and_a_live_use``.
ALLOWLIST_REASONS: dict[str, str] = {
    "pipeline/actor.py": "request-namespace fallback: current_namespace() resolves to the default when none is bound",
    "pipeline/audit_anchor.py": "audit anchoring is default-only: it reads and publishes the default chains",
    "pipeline/audit_file.py": "file audit layout holds only the default namespace; other namespaces are refused",
    "pipeline/ci.py": "merge CI gate and manifest namespace plumbing; routing the gate is a separate change",
    "pipeline/manifest_source.py": "file fallback reads the default partition; routing it is a separate change",
    "pipeline/metrics_exposition.py": "process-wide metrics reads; no request namespace is bound",
    "pipeline/namespace_resolver.py": "built-in resolver maps a request with no namespace plugin to the default namespace",
    "pipeline/persistence.py": "file-layout refusal check: legacy notification and decision writers hold only the default partition",
    "pipeline/plan_draft.py": "draft list helpers default to the default namespace; bound draft reads are refused upstream",
    "pipeline/settings_tools.py": "settings plumbing: settings are read and written in the default namespace",
    "pipeline/store.py": "store-layer parameter defaults and the legacy FileStore surface, which is default-only",
    "pipeline/store_contract.py": "record-store contract definition: the contract names the default namespace",
    "pipeline/store_contract_notifications.py": "notification contract definition: the namespace its cases exercise",
    "pipeline/store_contract_transactions.py": "transaction contract definition: default-namespace contract cases",
    "pipeline/store_contract_usage.py": "usage contract definition: default-namespace contract cases",
    "pipeline/store_import_audit.py": "audit import tool: the default namespace is the import target",
    "pipeline/store_memory.py": "in-memory store: default-only legacy surface that refuses other namespaces",
    "pipeline/store_records.py": "defines DEFAULT_NAMESPACE and the namespace validators that check against it",
    "pipeline/store_sqlite.py": "SQLite store legacy surface: default-only, refuses other namespaces",
    "pipeline/store_sqlite_notifications.py": "SQLite notification legacy reads and writes use the default namespace",
    "pipeline/store_sqlite_records.py": "SQLite record legacy decision and journal helpers use the default namespace",
    "pipeline/telemetry.py": "telemetry's own process-wide label for the default namespace, not a request scope",
    "pipeline/tools/backfill_plan_source.py": "backfill tool: moves file-layout plans into the default namespace",
    "pipeline/tools/import_file_store.py": "import tool: file-layout data is imported into the default namespace",
    "pipeline/events_nats_consumer.py": "background consumer: reads the default partition; no request is bound",
    "pipeline/health_freshness.py": "background consumer: outbox probe reads the default partition; no request is bound",
    "pipeline/audit_contract.py": "audit contract definition: the contract names the default chain namespace",
}


def count_uses(root: Path) -> dict[str, int]:
    """Return ``{repo-relative path: lines naming DEFAULT_NAMESPACE}``.

    Only production ``.py`` files under the scanned directories count; the
    ``pipeline/testing`` package is test support and is excluded.
    """
    counts: dict[str, int] = {}
    for directory in SCANNED_DIRECTORIES:
        for path in sorted((root / directory).rglob("*.py")):
            relative = path.relative_to(root).as_posix()
            if relative.startswith(EXCLUDED_PREFIXES):
                continue
            text = path.read_text(encoding="utf-8")
            uses = sum(TOKEN in line for line in text.splitlines())
            if uses:
                counts[relative] = uses
    return counts


def ratchet_violations(counts: dict[str, int], allowlist: dict[str, int]) -> list[str]:
    """Return one message per module whose count exceeds its allowlisted number."""
    violations = []
    for relative, uses in sorted(counts.items()):
        allowed = allowlist.get(relative, 0)
        if uses > allowed:
            violations.append(
                f"{relative}: {uses} uses of {TOKEN}, allowlist permits {allowed}; "
                "lower the number by selecting the namespace with current_namespace()"
            )
    return violations


def allowlist_problems(
    counts: dict[str, int], allowlist: dict[str, int], reasons: dict[str, str]
) -> list[str]:
    """Return one message per allowlist entry that lacks a reason or has no use.

    A reason must be a single non-empty line, and every reason must belong to
    an allowlist entry: a reason left behind by a removed entry is also a
    problem.
    """
    problems = []
    for relative in sorted(allowlist):
        reason = reasons.get(relative, "")
        if not reason.strip() or "\n" in reason:
            problems.append(f"{relative}: allowlist entry needs a one-line reason")
        if counts.get(relative, 0) == 0:
            problems.append(f"{relative}: names {TOKEN} zero times; remove its entry")
    for relative in sorted(reasons.keys() - allowlist.keys()):
        problems.append(
            f"{relative}: reason given for a module that is not allowlisted"
        )
    return problems


def _write_module(root: Path, relative: str, uses: int) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"X{index} = {TOKEN}\n" for index in range(uses)))


# -- the ratchet -------------------------------------------------------------


def test_ratchet_passes_on_the_real_tree() -> None:
    """Every production module is at or below its allowlisted count."""
    assert ratchet_violations(count_uses(REPO_ROOT), ALLOWLIST) == []


def test_ratchet_fails_when_a_new_module_names_the_default_namespace(
    tmp_path: Path,
) -> None:
    """A module the allowlist does not know may not name the default namespace."""
    _write_module(tmp_path, "pipeline/new_module.py", uses=1)

    violations = ratchet_violations(count_uses(tmp_path), {})

    assert len(violations) == 1
    assert violations[0].startswith("pipeline/new_module.py: 1 uses")
    assert "lower the number" in violations[0]


def test_ratchet_fails_when_a_listed_module_gains_a_use(tmp_path: Path) -> None:
    """A higher count than the allowlisted one fails, naming the module."""
    _write_module(tmp_path, "pipeline/listed.py", uses=2)

    violations = ratchet_violations(count_uses(tmp_path), {"pipeline/listed.py": 1})

    assert len(violations) == 1
    assert "pipeline/listed.py: 2 uses" in violations[0]
    assert "allowlist permits 1" in violations[0]


def test_ratchet_passes_when_a_listed_module_loses_a_use(tmp_path: Path) -> None:
    """A lower count than the allowlisted one passes."""
    _write_module(tmp_path, "pipeline/listed.py", uses=1)

    assert ratchet_violations(count_uses(tmp_path), {"pipeline/listed.py": 2}) == []


def test_ratchet_counts_app_modules(tmp_path: Path) -> None:
    """The ratchet covers ``app/`` as well as ``pipeline/``."""
    _write_module(tmp_path, "app/new_app_module.py", uses=1)

    assert ratchet_violations(count_uses(tmp_path), {}) != []


def test_ratchet_ignores_the_testing_package(tmp_path: Path) -> None:
    """``pipeline/testing/`` is test support, not production code."""
    _write_module(tmp_path, "pipeline/testing/fixture_support.py", uses=4)

    assert count_uses(tmp_path) == {}


# -- the allowlist reasons ----------------------------------------------------


def test_every_allowlist_entry_has_a_reason_and_a_live_use() -> None:
    """Each allowlisted module has a one-line reason and still names the constant."""
    problems = allowlist_problems(count_uses(REPO_ROOT), ALLOWLIST, ALLOWLIST_REASONS)

    assert problems == []


def test_allowlist_entry_without_a_reason_is_reported() -> None:
    """An entry with no reason fails, naming the module."""
    problems = allowlist_problems(
        {"pipeline/listed.py": 1}, {"pipeline/listed.py": 1}, {}
    )

    assert problems == ["pipeline/listed.py: allowlist entry needs a one-line reason"]


def test_allowlist_reason_spanning_two_lines_is_reported() -> None:
    """A reason is one line; a reason with a line break fails."""
    problems = allowlist_problems(
        {"pipeline/listed.py": 1},
        {"pipeline/listed.py": 1},
        {"pipeline/listed.py": "first line\nsecond line"},
    )

    assert problems == ["pipeline/listed.py: allowlist entry needs a one-line reason"]


def test_allowlist_entry_for_a_module_without_the_constant_is_reported() -> None:
    """An entry whose module no longer names the constant fails, even with a reason."""
    problems = allowlist_problems(
        {},
        {"pipeline/listed.py": 1},
        {"pipeline/listed.py": "was a background consumer"},
    )

    assert problems == [
        "pipeline/listed.py: names DEFAULT_NAMESPACE zero times; remove its entry"
    ]


def test_reason_for_a_module_not_on_the_allowlist_is_reported() -> None:
    """A reason left behind by a removed entry fails."""
    problems = allowlist_problems({}, {}, {"pipeline/gone.py": "removed module"})

    assert problems == [
        "pipeline/gone.py: reason given for a module that is not allowlisted"
    ]


# -- the request namespace ---------------------------------------------------


@pytest.fixture
def sqlite_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SqliteStore:
    """Hand the audit layer a SQLite store of its own for one test."""
    store = SqliteStore(tmp_path / "pipeline.db")
    monkeypatch.setattr(pipeline.server, "_store", store)
    return store


def test_nothing_bound_writes_and_reads_the_default_chain(
    sqlite_store: SqliteStore,
) -> None:
    """With no namespace bound the plan chain is the default namespace's chain."""
    assert current_namespace() == DEFAULT_NAMESPACE

    audit.append_audit("plan-x", action="story.merge", outcome="allowed")

    log = sqlite_store.audit_log()
    assert len(log.read_chain(DEFAULT_NAMESPACE, "plan-x")) == 1
    assert audit.verify_audit_chain("plan-x") is None


def test_bound_namespace_receives_the_plan_append(sqlite_store: SqliteStore) -> None:
    """With ``a/b`` bound the append lands in that namespace's chain only."""
    with bind_namespace(NAMESPACE):
        audit.append_audit("plan-x", action="story.merge", outcome="allowed")

    log = sqlite_store.audit_log()
    assert len(log.read_chain(NAMESPACE, "plan-x")) == 1
    assert log.read_chain(DEFAULT_NAMESPACE, "plan-x") == []


def test_bound_namespace_is_the_one_verified(sqlite_store: SqliteStore) -> None:
    """Verification lists and checks the chains of the bound namespace only."""
    audit.append_audit("only-default", action="story.merge", outcome="allowed")
    with bind_namespace(NAMESPACE):
        audit.append_audit("only-bound", action="story.merge", outcome="allowed")

    with bind_namespace(NAMESPACE):
        plans, _host = audit.verify_audit_log()
    unbound_plans, _unbound_host = audit.verify_audit_log()

    assert set(plans) == {"only-bound"}
    assert set(unbound_plans) == {"only-default"}


def test_bound_namespace_receives_the_host_append(sqlite_store: SqliteStore) -> None:
    """The host-scoped chain follows the bound namespace too."""
    with bind_namespace(NAMESPACE):
        audit.append_host_audit(action="settings.change", outcome="allowed")

    log = sqlite_store.audit_log()
    assert len(log.read_chain(NAMESPACE, HOST_CHAIN_ID)) == 1
    assert log.read_chain(DEFAULT_NAMESPACE, HOST_CHAIN_ID) == []


def test_file_store_refuses_a_bound_namespace_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file layout has no other namespace, so the append is refused, not redirected."""

    class _FileBackedStore:
        def audit_log(self) -> FileAuditLog:
            return FileAuditLog(tmp_path)

    monkeypatch.setattr(pipeline.server, "_store", _FileBackedStore())

    with bind_namespace(NAMESPACE), pytest.raises(UnknownNamespaceError):
        audit.append_audit("plan-x", action="story.merge", outcome="allowed")

    assert list(tmp_path.iterdir()) == []


def test_nothing_bound_keeps_the_security_audit_state_in_the_default_namespace() -> (
    None
):
    """Unbound, the audit state is recorded under the default namespace."""
    records = InMemoryRecordStore()

    security_audit.record_audit(records, "/repo", SHA, NOW)

    key = security_audit.repo_key("/repo")
    assert records.get_security_audit_state(DEFAULT_NAMESPACE, key) is not None
    assert security_audit.read_audit_state(records, "/repo") is not None


def test_bound_namespace_holds_the_security_audit_state() -> None:
    """Bound, the audit state is written to and read from the bound namespace only."""
    records = InMemoryRecordStore()

    with bind_namespace(NAMESPACE):
        security_audit.record_audit(records, "/repo", SHA, NOW)
        bound_state = security_audit.read_audit_state(records, "/repo")

    key = security_audit.repo_key("/repo")
    assert bound_state is not None
    assert records.get_security_audit_state(NAMESPACE, key) is not None
    assert records.get_security_audit_state(DEFAULT_NAMESPACE, key) is None


# -- plan paths under the request namespace ----------------------------------

DRAFT = {"epics": []}


class _NullAuditLog:
    """An audit log that accepts appends and keeps none, for paths that audit."""

    def append(self, namespace: str, chain: str, body: dict) -> dict:
        return {}


class _RecordStore:
    """A store stand-in whose record view is one in-memory record store."""

    def __init__(self, records: InMemoryRecordStore) -> None:
        self._records = records

    def records(self) -> InMemoryRecordStore:
        return self._records

    def audit_log(self) -> _NullAuditLog:
        return _NullAuditLog()


class _NamespaceRecordingLog:
    """An audit log that records each namespace it is asked to read."""

    def __init__(self) -> None:
        self.namespaces: list[str] = []

    def list_chains(self, namespace: str) -> list[str]:
        self.namespaces.append(namespace)
        return []

    def read_chain(self, namespace: str, chain: str) -> list[dict]:
        self.namespaces.append(namespace)
        return []


class _RefusingLog(_NamespaceRecordingLog):
    """An audit log that refuses every namespace but the default one."""

    def list_chains(self, namespace: str) -> list[str]:
        raise UnknownNamespaceError("unknown namespace")


@pytest.fixture
def memory_records(monkeypatch: pytest.MonkeyPatch) -> InMemoryRecordStore:
    """Hand the plan paths an in-memory record store of its own for one test."""
    records = InMemoryRecordStore()
    monkeypatch.setattr(pipeline.server, "_store", _RecordStore(records))
    return records


def test_bound_save_plan_is_refused_and_writes_no_default_draft(
    memory_records: InMemoryRecordStore,
) -> None:
    """A bound save is refused with an explicit denial; the default draft is untouched."""
    plan_json = json.dumps(DRAFT)

    with bind_namespace(NAMESPACE):
        result = PipelineService().save_plan("plan-x", plan_json)

    assert result == {"ok": False, "error": "unknown namespace"}
    assert memory_records.get_plan_draft(DEFAULT_NAMESPACE, "plan-x") is None


def test_unbound_save_plan_stages_the_default_draft(
    memory_records: InMemoryRecordStore,
) -> None:
    """Unbound, save_plan stages the draft in the default namespace, as before."""
    result = PipelineService().save_plan("plan-x", json.dumps(DRAFT))

    assert result["ok"] is True
    assert memory_records.get_plan_draft(DEFAULT_NAMESPACE, "plan-x") == DRAFT


def test_bound_draft_read_is_refused_not_redirected(
    memory_records: InMemoryRecordStore,
) -> None:
    """Drafts are default-only, so a bound draft read is refused, never read from default."""
    memory_records.put_plan_draft(DEFAULT_NAMESPACE, "plan-x", DRAFT)

    with bind_namespace(NAMESPACE):
        plan, refusal = plan_draft.read_plan_draft("plan-x")

    assert plan is None
    assert refusal == {"ok": False, "error": "unknown namespace"}


def test_unbound_draft_read_serves_the_default_draft(
    memory_records: InMemoryRecordStore,
) -> None:
    """Unbound, the draft read returns the default namespace's draft, as before."""
    memory_records.put_plan_draft(DEFAULT_NAMESPACE, "plan-x", DRAFT)

    plan, refusal = plan_draft.read_plan_draft("plan-x")

    assert plan == DRAFT
    assert refusal is None


def test_bound_ingest_is_refused_before_it_writes_the_default_partition(
    memory_records: InMemoryRecordStore,
) -> None:
    """Ingest under a bound namespace refuses at the draft read and writes nothing."""
    memory_records.put_plan_draft(DEFAULT_NAMESPACE, "plan-x", DRAFT)

    with bind_namespace(NAMESPACE):
        result = pipeline.server.ingest_plan("plan-x")

    assert result == {"ok": False, "error": "unknown namespace"}
    assert memory_records.get_manifest(DEFAULT_NAMESPACE, "plan-x") is None
    assert memory_records.get_manifest(NAMESPACE, "plan-x") is None


def test_bound_story_timeline_reads_only_the_bound_namespace() -> None:
    """With ``a/b`` bound the timeline asks the audit log for that namespace only."""
    log = _NamespaceRecordingLog()

    with bind_namespace(NAMESPACE):
        read_story_timeline(log)

    assert set(log.namespaces) == {NAMESPACE}


def test_unbound_story_timeline_reads_the_default_namespace() -> None:
    """Unbound, the timeline asks the audit log for the default namespace, as before."""
    log = _NamespaceRecordingLog()

    read_story_timeline(log)

    assert set(log.namespaces) == {DEFAULT_NAMESPACE}


def test_bound_story_timeline_propagates_a_refused_namespace() -> None:
    """A log that refuses the bound namespace fails the timeline, never empties it."""
    with bind_namespace(NAMESPACE), pytest.raises(UnknownNamespaceError):
        read_story_timeline(_RefusingLog())
