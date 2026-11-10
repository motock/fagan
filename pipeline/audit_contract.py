"""Reusable behaviour suite for :class:`pipeline.audit_log.AuditLog`.

ADR 0012 decision 10.  A contract that only one implementation passes proves
nothing, so the behaviour lives in mixin classes: an implementation inherits the
mixins that describe what it supports and overrides :meth:`make_log`.

This module imports no test framework on purpose.  It uses plain ``assert``
statements and the small :func:`_expect` helper, so the same suite can be
exercised by any harness.  None of the classes is named ``Test*``: pytest must
never collect a mixin (it would call the unimplemented ``make_log``).

Inherit :class:`AuditLogContract` always, plus exactly one of
:class:`MultiNamespaceContract` (implementations that serve any namespace) or
:class:`SingleNamespaceContract` (implementations that only serve
:data:`DEFAULT_NAMESPACE`).  Add :class:`AuditTamperContract` and
:class:`AuditConcurrencyContract` to hold an implementation to the detection
and serialization guarantees.

Tampering has to bypass the protocol -- that is the point of the protocol -- so
:class:`AuditTamperContract` declares one test-only hook, :meth:`rewrite_raw`,
that the implementing test class provides.
"""

from __future__ import annotations

import threading

from pipeline.audit_chain import record_hash, verify_entries
from pipeline.audit_log import HOST_CHAIN_ID
from pipeline.store_records import (
    DEFAULT_NAMESPACE,
    InvalidRecordKeyError,
    UnknownNamespaceError,
)

_PLAN = "plan-a"
_OTHER_PLAN = "plan-b"
_SECOND_NAMESPACE = "acme"
_INVALID_NAMESPACE = "Bad NS"
_INVALID_CHAINS = ("../x", "", "a/b", "host:x", "host")
_BODY = {"action": "story.merge", "outcome": "allowed"}

#: The number of records a tamper test builds its chain from.
TAMPER_CHAIN_LENGTH = 5

#: The concurrent-append shape: threads x appends per thread.
THREADS = 8
APPENDS_PER_THREAD = 25


def _expect(self, exc_type, fn):
    """Assert that calling ``fn`` raises exactly ``exc_type``.

    Any other exception propagates and fails the test just as loudly; so does
    returning normally instead of raising.
    """
    try:
        fn()
    except exc_type:
        return
    raise AssertionError(f"{fn} did not raise {exc_type.__name__}")


def _seed(log, chain=_PLAN, count=3, namespace=DEFAULT_NAMESPACE):
    """Append *count* records to one chain and return the stored entries."""
    for index in range(count):
        log.append(namespace, chain, {"action": f"a{index}", "outcome": "allowed"})
    return log.read_chain(namespace, chain)


class AuditLogContract:
    """Default-namespace behaviour plus argument validation."""

    def make_log(self):
        """Return a fresh, empty audit log under test."""
        raise NotImplementedError

    # -- appending -----------------------------------------------------
    def test_append_returns_the_record_it_stored(self):
        log = self.make_log()
        record = log.append(DEFAULT_NAMESPACE, _PLAN, dict(_BODY))
        assert log.read_chain(DEFAULT_NAMESPACE, _PLAN) == [record]

    def test_the_first_record_of_a_chain_starts_at_seq_zero(self):
        log = self.make_log()
        record = log.append(DEFAULT_NAMESPACE, _PLAN, dict(_BODY))
        assert (record["seq"], record["prev_hash"]) == (0, "")

    def test_the_sequence_of_a_chain_runs_zero_one_two(self):
        log = self.make_log()
        entries = _seed(log)
        assert [entry["seq"] for entry in entries] == [0, 1, 2]

    def test_each_record_links_to_the_hash_of_the_one_before_it(self):
        log = self.make_log()
        entries = _seed(log)
        assert entries[1]["prev_hash"] == entries[0]["hash"]
        assert entries[2]["prev_hash"] == entries[1]["hash"]

    def test_the_hash_of_a_record_is_the_hash_of_the_record(self):
        log = self.make_log()
        record = log.append(DEFAULT_NAMESPACE, _PLAN, dict(_BODY))
        assert record["hash"] == record_hash(record)

    def test_a_stored_chain_verifies(self):
        log = self.make_log()
        _seed(log, count=3)
        assert verify_entries(log.read_chain(DEFAULT_NAMESPACE, _PLAN)) is None

    def test_read_chain_returns_the_records_in_storage_order(self):
        log = self.make_log()
        entries = _seed(log)
        assert [entry["action"] for entry in entries] == ["a0", "a1", "a2"]

    # -- reading -------------------------------------------------------
    def test_the_head_of_an_absent_chain_is_none(self):
        log = self.make_log()
        assert log.head(DEFAULT_NAMESPACE, _PLAN) is None

    def test_read_chain_of_an_absent_chain_is_an_empty_list(self):
        log = self.make_log()
        assert log.read_chain(DEFAULT_NAMESPACE, _PLAN) == []

    def test_the_head_is_the_newest_record_of_the_chain(self):
        log = self.make_log()
        log.append(DEFAULT_NAMESPACE, _PLAN, dict(_BODY))
        newest = log.append(DEFAULT_NAMESPACE, _PLAN, dict(_BODY))
        assert log.head(DEFAULT_NAMESPACE, _PLAN) == newest

    def test_the_head_of_one_chain_is_not_the_head_of_another(self):
        log = self.make_log()
        log.append(DEFAULT_NAMESPACE, _PLAN, dict(_BODY))
        assert log.head(DEFAULT_NAMESPACE, _OTHER_PLAN) is None

    def test_list_chains_of_an_empty_namespace_is_an_empty_list(self):
        log = self.make_log()
        assert log.list_chains(DEFAULT_NAMESPACE) == []

    def test_list_chains_lists_the_plan_chains_sorted(self):
        log = self.make_log()
        log.append(DEFAULT_NAMESPACE, "zeta", dict(_BODY))
        log.append(DEFAULT_NAMESPACE, "alpha", dict(_BODY))
        assert log.list_chains(DEFAULT_NAMESPACE) == ["alpha", "zeta"]

    def test_list_chains_puts_the_host_chain_after_the_plan_chains(self):
        log = self.make_log()
        log.append(DEFAULT_NAMESPACE, "zeta", dict(_BODY))
        log.append(DEFAULT_NAMESPACE, HOST_CHAIN_ID, dict(_BODY))
        log.append(DEFAULT_NAMESPACE, "alpha", dict(_BODY))
        assert log.list_chains(DEFAULT_NAMESPACE) == ["alpha", "zeta", HOST_CHAIN_ID]

    def test_the_host_chain_is_a_chain_of_its_own(self):
        log = self.make_log()
        log.append(DEFAULT_NAMESPACE, HOST_CHAIN_ID, dict(_BODY))
        assert len(log.read_chain(DEFAULT_NAMESPACE, HOST_CHAIN_ID)) == 1
        assert log.read_chain(DEFAULT_NAMESPACE, _PLAN) == []

    # -- no shared state -----------------------------------------------
    def test_mutating_a_returned_record_does_not_change_the_stored_record(self):
        log = self.make_log()
        record = log.append(DEFAULT_NAMESPACE, _PLAN, {"detail": {"sha": "abc"}})
        record["detail"]["sha"] = "tampered"
        record["seq"] = 99
        stored = log.read_chain(DEFAULT_NAMESPACE, _PLAN)[0]
        assert stored["detail"] == {"sha": "abc"}
        assert stored["seq"] == 0

    def test_mutating_the_body_after_the_append_changes_nothing(self):
        log = self.make_log()
        body = {"action": "a", "outcome": "allowed", "detail": {"sha": "abc"}}
        log.append(DEFAULT_NAMESPACE, _PLAN, body)
        body["detail"]["sha"] = "tampered"
        assert log.read_chain(DEFAULT_NAMESPACE, _PLAN)[0]["detail"] == {"sha": "abc"}

    def test_mutating_a_returned_record_does_not_change_the_head(self):
        log = self.make_log()
        record = log.append(DEFAULT_NAMESPACE, _PLAN, {"detail": {"sha": "abc"}})
        record["detail"]["sha"] = "tampered"
        assert log.head(DEFAULT_NAMESPACE, _PLAN)["detail"] == {"sha": "abc"}

    def test_two_logs_share_no_state(self):
        first = self.make_log()
        second = self.make_log()
        first.append(DEFAULT_NAMESPACE, _PLAN, dict(_BODY))
        assert second.read_chain(DEFAULT_NAMESPACE, _PLAN) == []
        assert second.list_chains(DEFAULT_NAMESPACE) == []

    # -- validation ----------------------------------------------------
    def test_an_invalid_namespace_is_refused_and_writes_nothing(self):
        log = self.make_log()
        _expect(
            self,
            InvalidRecordKeyError,
            lambda: log.append(_INVALID_NAMESPACE, _PLAN, dict(_BODY)),
        )
        assert log.list_chains(DEFAULT_NAMESPACE) == []
        assert log.read_chain(DEFAULT_NAMESPACE, _PLAN) == []

    def test_an_invalid_chain_is_refused_and_writes_nothing(self):
        log = self.make_log()
        for chain in _INVALID_CHAINS:
            _expect(
                self,
                InvalidRecordKeyError,
                lambda chain=chain: log.append(DEFAULT_NAMESPACE, chain, dict(_BODY)),
            )
        assert log.list_chains(DEFAULT_NAMESPACE) == []
        assert log.read_chain(DEFAULT_NAMESPACE, _PLAN) == []

    def test_an_invalid_chain_is_refused_on_read_too(self):
        log = self.make_log()
        for chain in _INVALID_CHAINS:
            _expect(self, InvalidRecordKeyError, lambda chain=chain: log.head(DEFAULT_NAMESPACE, chain))

    def test_a_body_that_carries_the_sequence_is_refused_and_writes_nothing(self):
        log = self.make_log()
        _expect(
            self,
            ValueError,
            lambda: log.append(DEFAULT_NAMESPACE, _PLAN, dict(_BODY, seq=7)),
        )
        assert log.read_chain(DEFAULT_NAMESPACE, _PLAN) == []

    def test_a_body_that_carries_the_previous_hash_is_refused_and_writes_nothing(self):
        log = self.make_log()
        _expect(
            self,
            ValueError,
            lambda: log.append(DEFAULT_NAMESPACE, _PLAN, dict(_BODY, prev_hash="abc")),
        )
        assert log.read_chain(DEFAULT_NAMESPACE, _PLAN) == []

    def test_a_body_that_carries_a_hash_is_refused_and_writes_nothing(self):
        log = self.make_log()
        _expect(
            self,
            ValueError,
            lambda: log.append(DEFAULT_NAMESPACE, _PLAN, dict(_BODY, hash="abc")),
        )
        assert log.read_chain(DEFAULT_NAMESPACE, _PLAN) == []


class MultiNamespaceContract:
    """Behaviour of logs that serve every well-formed namespace."""

    def make_log(self):
        """Return a fresh, empty audit log under test."""
        raise NotImplementedError

    def test_should_keep_namespaces_apart(self):
        log = self.make_log()
        log.append(_SECOND_NAMESPACE, _PLAN, dict(_BODY))
        assert log.read_chain(DEFAULT_NAMESPACE, _PLAN) == []
        assert log.list_chains(DEFAULT_NAMESPACE) == []

    def test_should_report_the_chains_of_each_namespace_separately(self):
        log = self.make_log()
        log.append(_SECOND_NAMESPACE, _PLAN, dict(_BODY))
        assert log.list_chains(_SECOND_NAMESPACE) == [_PLAN]
        assert log.list_chains(DEFAULT_NAMESPACE) == []

    def test_should_keep_a_separate_chain_of_the_same_name_per_namespace(self):
        log = self.make_log()
        log.append(_SECOND_NAMESPACE, _PLAN, dict(_BODY))
        log.append(DEFAULT_NAMESPACE, _PLAN, dict(_BODY))
        assert len(log.read_chain(_SECOND_NAMESPACE, _PLAN)) == 1
        assert len(log.read_chain(DEFAULT_NAMESPACE, _PLAN)) == 1

    def test_should_start_the_sequence_of_each_namespace_at_zero(self):
        log = self.make_log()
        log.append(_SECOND_NAMESPACE, _PLAN, dict(_BODY))
        record = log.append(DEFAULT_NAMESPACE, _PLAN, dict(_BODY))
        assert (record["seq"], record["prev_hash"]) == (0, "")

    def test_should_not_report_another_namespaces_head(self):
        log = self.make_log()
        log.append(_SECOND_NAMESPACE, _PLAN, dict(_BODY))
        assert log.head(DEFAULT_NAMESPACE, _PLAN) is None


class SingleNamespaceContract:
    """Behaviour of logs that only serve :data:`DEFAULT_NAMESPACE`."""

    def make_log(self):
        """Return a fresh, empty audit log under test."""
        raise NotImplementedError

    def test_should_refuse_an_unknown_namespace(self):
        log = self.make_log()
        log.append(DEFAULT_NAMESPACE, _PLAN, dict(_BODY))
        _expect(self, UnknownNamespaceError, lambda: log.list_chains(_SECOND_NAMESPACE))
        assert log.list_chains(DEFAULT_NAMESPACE) == [_PLAN]

    def test_should_refuse_an_unknown_namespace_on_read_chain(self):
        log = self.make_log()
        _expect(self, UnknownNamespaceError, lambda: log.read_chain(_SECOND_NAMESPACE, _PLAN))

    def test_should_refuse_an_unknown_namespace_on_head(self):
        log = self.make_log()
        _expect(self, UnknownNamespaceError, lambda: log.head(_SECOND_NAMESPACE, _PLAN))

    def test_should_refuse_an_unknown_namespace_on_append_without_storing(self):
        log = self.make_log()
        _expect(
            self,
            UnknownNamespaceError,
            lambda: log.append(_SECOND_NAMESPACE, _PLAN, dict(_BODY)),
        )
        assert log.list_chains(DEFAULT_NAMESPACE) == []


class AuditTamperContract:
    """What the chain must notice when stored entries are rewritten.

    The protocol has no mutator, so the test reaches past it through
    :meth:`rewrite_raw`, which replaces the stored entries of one chain
    verbatim.  Every case is judged with
    :func:`pipeline.audit_chain.verify_entries`, the same function an operator's
    verification uses.
    """

    def make_log(self):
        """Return a fresh, empty audit log under test."""
        raise NotImplementedError

    def rewrite_raw(self, log, namespace, chain, entries):
        """Replace the stored entries of one chain verbatim (test-only hook)."""
        raise NotImplementedError

    # -- helpers -------------------------------------------------------
    def seeded(self):
        """Return ``(log, entries)`` for a chain of ``TAMPER_CHAIN_LENGTH``."""
        log = self.make_log()
        entries = _seed(log, count=TAMPER_CHAIN_LENGTH)
        assert len(entries) == TAMPER_CHAIN_LENGTH
        return log, entries

    def reported(self, entries):
        """Rewrite the chain with *entries* and return the first bad index."""
        log, _ = self.seeded()
        self.rewrite_raw(log, DEFAULT_NAMESPACE, _PLAN, entries)
        return verify_entries(log.read_chain(DEFAULT_NAMESPACE, _PLAN))

    # -- the cases -----------------------------------------------------
    def test_an_intact_chain_verifies(self):
        log, entries = self.seeded()
        assert verify_entries(log.read_chain(DEFAULT_NAMESPACE, _PLAN)) is None
        assert verify_entries(entries) is None

    def test_an_edited_record_is_reported_at_its_own_position(self):
        _, entries = self.seeded()
        entries[2] = dict(entries[2], detail={"sha": "tampered"})
        assert self.reported(entries) == 2

    def test_an_edited_and_rehashed_record_is_reported_at_the_record_after_it(self):
        _, entries = self.seeded()
        edited = dict(entries[2], detail={"sha": "tampered"})
        edited["hash"] = record_hash(edited)
        entries[2] = edited
        assert self.reported(entries) == 3

    def test_a_deleted_middle_record_is_reported_at_its_own_position(self):
        _, entries = self.seeded()
        del entries[2]
        assert self.reported(entries) == 2

    def test_reordered_records_are_reported_at_the_first_moved_one(self):
        _, entries = self.seeded()
        entries[1], entries[2] = entries[2], entries[1]
        assert self.reported(entries) == 1

    def test_a_duplicated_record_is_reported_at_the_copy(self):
        _, entries = self.seeded()
        entries.insert(3, dict(entries[2]))
        assert self.reported(entries) == 3

    def test_a_gap_in_the_sequence_is_reported_at_the_gap(self):
        _, entries = self.seeded()
        entries[3] = dict(entries[3], seq=4)
        assert self.reported(entries) == 3

    def test_a_malformed_entry_is_reported_at_its_own_position(self):
        _, entries = self.seeded()
        entries[1] = None
        assert self.reported(entries) == 1

    def test_truncating_the_tail_verifies_clean(self):
        """Only the anchor catches a truncation; the chain itself cannot.

        This is the documented limit of an unanchored hash chain: dropping the
        newest records leaves a shorter chain that is still internally
        consistent.  The head hash has to be anchored somewhere the writer
        cannot rewrite (``pipeline/audit_anchor.py``) for that to be detected.
        """
        _, entries = self.seeded()
        del entries[4]
        assert self.reported(entries) is None

    def test_an_append_after_a_tamper_links_to_the_current_tail(self):
        _, entries = self.seeded()
        entries[2] = dict(entries[2], detail={"sha": "tampered"})
        log, _ = self.seeded()
        self.rewrite_raw(log, DEFAULT_NAMESPACE, _PLAN, entries)
        tail = log.head(DEFAULT_NAMESPACE, _PLAN)
        appended = log.append(DEFAULT_NAMESPACE, _PLAN, {"action": "after"})
        assert appended["prev_hash"] == tail["hash"]
        assert appended["seq"] == tail["seq"] + 1
        # The tamper is still what verification reports first.
        assert verify_entries(log.read_chain(DEFAULT_NAMESPACE, _PLAN)) == 2


class AuditConcurrencyContract:
    """Concurrent appends must not share a sequence number."""

    def make_log(self):
        """Return a fresh, empty audit log under test."""
        raise NotImplementedError

    def test_concurrent_appends_to_one_chain_are_serialized(self):
        log = self.make_log()
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                for _ in range(APPENDS_PER_THREAD):
                    log.append(DEFAULT_NAMESPACE, _PLAN, {"action": "a", "outcome": "ok"})
            except BaseException as error:  # noqa: BLE001 - reported below
                errors.append(error)

        threads = [threading.Thread(target=worker) for _ in range(THREADS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        entries = log.read_chain(DEFAULT_NAMESPACE, _PLAN)
        assert len(entries) == THREADS * APPENDS_PER_THREAD
        assert [entry["seq"] for entry in entries] == list(range(THREADS * APPENDS_PER_THREAD))
        assert verify_entries(entries) is None

    def test_concurrent_appends_to_two_chains_do_not_share_sequences(self):
        log = self.make_log()
        errors: list[BaseException] = []

        def worker(chain: str) -> None:
            try:
                for _ in range(APPENDS_PER_THREAD):
                    log.append(DEFAULT_NAMESPACE, chain, {"action": "a", "outcome": "ok"})
            except BaseException as error:  # noqa: BLE001 - reported below
                errors.append(error)

        threads = [
            threading.Thread(target=worker, args=(chain,))
            for chain in (_PLAN, _OTHER_PLAN)
            for _ in range(THREADS // 2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        expected = (THREADS // 2) * APPENDS_PER_THREAD
        for chain in (_PLAN, _OTHER_PLAN):
            entries = log.read_chain(DEFAULT_NAMESPACE, chain)
            assert [entry["seq"] for entry in entries] == list(range(expected))
            assert verify_entries(entries) is None
