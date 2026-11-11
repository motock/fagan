"""Stage C proof that the b3 record kinds are covered on the sqlite backend.

Notification history and usage records are Protocol names in
RECORD_STORE_METHODS and are declared by the record-store contract suite. The
outbox queue API is a separate mixin (OutboxContract) whose behaviour suite is
run against SqliteRecords in tests/unit, so it is checked by subclassing
rather than by the Protocol registry.
"""

from __future__ import annotations

from pipeline.store_contract_outbox import OutboxContract
from pipeline.store_records import RECORD_STORE_METHODS
from pipeline.store_sqlite import SqliteRecords
from tests.contract.test_record_store_contract import COVERED
from tests.unit import test_store_sqlite_records
from tests.unit._store_outbox_helpers import OutboxBehaviour

PROTOCOL_B3_NAMES = (
    "append_notification",
    "get_notification_history",
    "append_usage",
    "get_usage",
)
OUTBOX_NAMES = ("append_outbox", "ack_outbox", "list_outboxes")


def uncovered(names, registry, covered):
    """Return the names missing from the Protocol registry or the covered set."""
    return [name for name in names if name not in registry or name not in covered]


def test_should_cover_b3_protocol_names_in_the_contract_suite():
    assert uncovered(PROTOCOL_B3_NAMES, RECORD_STORE_METHODS, COVERED) == []


def test_should_declare_outbox_names_on_the_outbox_mixin():
    assert [n for n in OUTBOX_NAMES if not hasattr(OutboxContract, n)] == []


def test_should_run_the_outbox_behaviour_suite_against_sqlite_records():
    assert issubclass(
        test_store_sqlite_records.TestSqliteRecordsOutbox, OutboxBehaviour
    )


def test_should_run_the_atomic_outbox_suite_against_sqlite_records():
    assert issubclass(
        test_store_sqlite_records.TestSqliteRecordsOutboxAtomic, OutboxBehaviour
    )


def test_should_report_sqlite_records_as_atomic():
    assert SqliteRecords.atomic_transactions is True


def test_should_report_an_unregistered_name_as_uncovered():
    assert uncovered(["append_outbox_v0"], RECORD_STORE_METHODS, COVERED) == [
        "append_outbox_v0"
    ]
