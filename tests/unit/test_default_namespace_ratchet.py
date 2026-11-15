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

from datetime import UTC, datetime
from pathlib import Path

import pytest

import pipeline.server
from pipeline import audit, security_audit
from pipeline.actor import bind_namespace, current_namespace
from pipeline.audit_file import FileAuditLog
from pipeline.audit_log import HOST_CHAIN_ID
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
    "pipeline/context_sources.py": 2,
    "pipeline/decision_records.py": 3,
    "pipeline/dispatch_context.py": 2,
    "pipeline/events_nats_consumer.py": 2,
    "pipeline/health_freshness.py": 2,
    "pipeline/ci.py": 3,
    "pipeline/ingest.py": 7,
    "pipeline/manifest_source.py": 3,
    "pipeline/merge.py": 2,
    "pipeline/metrics_exposition.py": 4,
    "pipeline/notification_outbox.py": 6,
    "pipeline/persistence.py": 2,
    "pipeline/plan_draft.py": 4,
    "pipeline/preflight.py": 2,
    "pipeline/review.py": 2,
    "pipeline/service.py": 3,
    "pipeline/service_levels.py": 3,
    "pipeline/settings_tools.py": 5,
    "pipeline/store.py": 5,
    "pipeline/store_contract.py": 101,
    "pipeline/store_contract_notifications.py": 3,
    "pipeline/store_contract_transactions.py": 27,
    "pipeline/store_contract_usage.py": 21,
    "pipeline/store_import_audit.py": 2,
    "pipeline/store_memory.py": 3,
    "pipeline/store_plugin.py": 2,
    "pipeline/store_records.py": 6,
    "pipeline/store_sqlite.py": 4,
    "pipeline/store_sqlite_notifications.py": 4,
    "pipeline/store_sqlite_records.py": 7,
    "pipeline/tick_checks.py": 2,
    "pipeline/telemetry.py": 2,
    "pipeline/tools/backfill_plan_source.py": 6,
    "pipeline/tools/import_file_store.py": 3,
    "pipeline/usage_record.py": 2,
    "pipeline/audit_contract.py": 69,
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
