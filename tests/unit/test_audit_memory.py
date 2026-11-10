"""Behaviour and contract tests for the in-memory audit log (ADR 0012).

The audit log is append-only: the application may insert a record and read
one back, and there is no operation that rewrites history.  These tests hold
the in-memory reference implementation to that promise -- the shared contract
mixins in :mod:`pipeline.audit_contract`, plus the same behaviour asserted
directly, so a hollow contract cannot pass.
"""

from __future__ import annotations

import copy
import threading

import pytest

from pipeline import audit_chain, audit_contract, audit_log, audit_memory
from pipeline.store_records import (
    DEFAULT_NAMESPACE,
    InvalidRecordKeyError,
    UnknownNamespaceError,
    validate_namespace,
)

PLAN = "plan-a"
OTHER_PLAN = "plan-b"
SECOND_NAMESPACE = "acme"
INVALID_NAMESPACE = "Bad NS"
INVALID_CHAINS = ("../x", "", "a/b", "host:x", "host")
BODY = {"action": "story.merge", "outcome": "allowed"}

THREADS = 8
APPENDS_PER_THREAD = 25


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _seed_chain(log, namespace=DEFAULT_NAMESPACE, chain=PLAN, count=5):
    """Append *count* records to one chain and return the stored entries."""
    for index in range(count):
        log.append(namespace, chain, {"action": f"a{index}", "outcome": "allowed"})
    return log.read_chain(namespace, chain)


def _attribute_roots(log):
    """Return the dicts and lists the log keeps its state in."""
    roots = []
    for name in dir(log):
        if name.startswith("__"):
            continue
        try:
            value = getattr(log, name)
        except AttributeError:
            continue
        if isinstance(value, (dict, list, tuple)):
            roots.append((name, value))
    return roots


def _walk(value, path):
    """Yield ``(path, value)`` for every value reachable under *path*."""
    yield path, value
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _walk(item, path + (key,))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _walk(item, path + (index,))


def _looks_like_entries(candidate) -> bool:
    """Return whether *candidate* looks like a stored chain of audit records."""
    return bool(candidate) and all(
        item is None or (isinstance(item, dict) and "seq" in item and "hash" in item)
        for item in candidate
    )


def _descend(current, element):
    """Return the value *element* names inside *current*."""
    if isinstance(current, (dict, list, tuple)):
        return current[element]
    return getattr(current, element)


def _find_entry_slot(log, namespace, chain):
    """Return the path to the stored entries of one chain.

    The memory log keeps its chains in whatever shape it likes, so the slot is
    found by looking for the place that maps a chain name to its records.
    """
    candidates = []
    for name, root in _attribute_roots(log):
        for path, value in _walk(root, (name,)):
            if not isinstance(value, (list, tuple)):
                continue
            last = path[-1]
            if (
                last == chain
                or last == (namespace, chain)
                or _looks_like_entries(value)
            ):
                candidates.append(path)
    named = [
        path for path in candidates if path[-1] == chain or path[-1] == (namespace, chain)
    ]
    if len(named) > 1:
        scoped = [path for path in named if namespace in path[:-1]]
        named = scoped or named
    if len(named) == 1:
        return named[0]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _replace_at(log, path, value) -> None:
    """Store *value* at *path* inside *log*."""
    parent = log
    for element in path[:-1]:
        parent = _descend(parent, element)
    last = path[-1]
    if isinstance(parent, (dict, list)):
        parent[last] = value
    else:
        setattr(parent, last, value)


def _patch_reader(log, namespace, chain, entries) -> None:
    """Make the log report *entries* for one chain (last-resort test seam)."""
    original = log.read_chain

    def read_chain(reader_namespace, reader_chain):
        if reader_namespace == namespace and reader_chain == chain:
            return [entry if isinstance(entry, dict) else None for entry in entries]
        return original(reader_namespace, reader_chain)

    log.read_chain = read_chain


def _rewrite_raw(log, namespace, chain, entries) -> None:
    """Replace the stored entries of one chain verbatim (test-only seam).

    The protocol has no mutator, so a tamper test has to reach past it: the
    stored entries are found in the log's own state, whatever shape the
    implementation keeps them in.
    """
    replacement = copy.deepcopy(list(entries))
    path = _find_entry_slot(log, namespace, chain)
    if path is None:
        _patch_reader(log, namespace, chain, replacement)
        return
    stored = _descend(log, path[0])
    for element in path[1:-1]:
        stored = _descend(stored, element)
    if isinstance(stored, list):
        stored[:] = replacement
    else:
        _replace_at(log, path, replacement)

def _mixin(*names):
    """Return the first mixin *names* names, failing when there is none."""
    for name in names:
        found = getattr(audit_contract, name, None)
        if found is not None:
            return found
    raise AssertionError(f"pipeline.audit_contract exposes none of {names}")


# --------------------------------------------------------------------------
# The shared contract, run against the reference implementation
# --------------------------------------------------------------------------


class TestInMemoryAuditLog(
    audit_contract.AuditLogContract, _mixin("MultiNamespaceContract")
):
    """The shared audit-log contract, run against the in-memory log."""

    def make_log(self):
        return audit_memory.InMemoryAuditLog()


class TestInMemoryAuditLogTamper(audit_contract.AuditTamperContract):
    """The tamper-detection contract, run against the in-memory log."""

    def make_log(self):
        return audit_memory.InMemoryAuditLog()

    def rewrite_raw(self, log, namespace, chain, entries):
        _rewrite_raw(log, namespace, chain, entries)


class TestInMemoryAuditLogConcurrency(audit_contract.AuditConcurrencyContract):
    """The concurrent-append contract, run against the in-memory log."""

    def make_log(self):
        return audit_memory.InMemoryAuditLog()


# --------------------------------------------------------------------------
# The behaviour itself
# --------------------------------------------------------------------------


class TestInMemoryAuditLogBehaviour:
    """What an append-only audit log must do."""

    def make_log(self):
        return audit_memory.InMemoryAuditLog()

    def test_append_returns_the_record_it_stored(self):
        log = self.make_log()
        record = log.append(DEFAULT_NAMESPACE, PLAN, dict(BODY))
        assert log.read_chain(DEFAULT_NAMESPACE, PLAN) == [record]

    def test_the_first_record_of_a_chain_starts_at_seq_zero(self):
        log = self.make_log()
        record = log.append(DEFAULT_NAMESPACE, PLAN, dict(BODY))
        assert record["seq"] == 0
        assert record["prev_hash"] == ""

    def test_the_sequence_of_a_chain_runs_zero_one_two(self):
        log = self.make_log()
        for _ in range(3):
            log.append(DEFAULT_NAMESPACE, PLAN, dict(BODY))
        assert [entry["seq"] for entry in log.read_chain(DEFAULT_NAMESPACE, PLAN)] == [
            0,
            1,
            2,
        ]

    def test_each_record_links_to_the_hash_of_the_one_before_it(self):
        log = self.make_log()
        entries = _seed_chain(log, count=3)
        assert entries[1]["prev_hash"] == entries[0]["hash"]
        assert entries[2]["prev_hash"] == entries[1]["hash"]

    def test_the_hash_of_a_record_is_the_hash_of_the_record(self):
        log = self.make_log()
        record = log.append(DEFAULT_NAMESPACE, PLAN, dict(BODY))
        assert record["hash"] == audit_chain.record_hash(record)

    def test_read_chain_returns_the_records_in_storage_order(self):
        log = self.make_log()
        entries = _seed_chain(log, count=3)
        assert [entry["action"] for entry in entries] == ["a0", "a1", "a2"]

    def test_the_head_of_an_absent_chain_is_none(self):
        log = self.make_log()
        assert log.head(DEFAULT_NAMESPACE, PLAN) is None

    def test_read_chain_of_an_absent_chain_is_an_empty_list(self):
        log = self.make_log()
        assert log.read_chain(DEFAULT_NAMESPACE, PLAN) == []

    def test_list_chains_of_an_empty_namespace_is_an_empty_list(self):
        log = self.make_log()
        assert log.list_chains(DEFAULT_NAMESPACE) == []

    def test_the_head_is_the_newest_record_of_the_chain(self):
        log = self.make_log()
        log.append(DEFAULT_NAMESPACE, PLAN, dict(BODY))
        newest = log.append(DEFAULT_NAMESPACE, PLAN, dict(BODY))
        assert log.head(DEFAULT_NAMESPACE, PLAN) == newest

    def test_a_record_of_one_namespace_is_invisible_in_another(self):
        log = self.make_log()
        log.append(SECOND_NAMESPACE, PLAN, dict(BODY))
        assert log.read_chain(DEFAULT_NAMESPACE, PLAN) == []
        assert log.list_chains(DEFAULT_NAMESPACE) == []

    def test_list_chains_of_one_namespace_covers_only_its_own_namespace(self):
        log = self.make_log()
        log.append(SECOND_NAMESPACE, PLAN, dict(BODY))
        assert log.list_chains(SECOND_NAMESPACE) == [PLAN]
        assert log.list_chains(DEFAULT_NAMESPACE) == []

    def test_two_namespaces_keep_two_separate_chains_of_the_same_name(self):
        log = self.make_log()
        log.append(SECOND_NAMESPACE, PLAN, dict(BODY))
        log.append(DEFAULT_NAMESPACE, PLAN, dict(BODY))
        assert len(log.read_chain(SECOND_NAMESPACE, PLAN)) == 1
        assert len(log.read_chain(DEFAULT_NAMESPACE, PLAN)) == 1

    def test_list_chains_lists_the_plan_chains_sorted_and_the_host_chain_last(self):
        log = self.make_log()
        log.append(DEFAULT_NAMESPACE, "zeta", dict(BODY))
        log.append(DEFAULT_NAMESPACE, "alpha", dict(BODY))
        log.append(DEFAULT_NAMESPACE, audit_log.HOST_CHAIN_ID, dict(BODY))
        assert log.list_chains(DEFAULT_NAMESPACE) == [
            "alpha",
            "zeta",
            audit_log.HOST_CHAIN_ID,
        ]

    def test_the_host_chain_is_a_chain_of_its_own(self):
        log = self.make_log()
        log.append(DEFAULT_NAMESPACE, audit_log.HOST_CHAIN_ID, dict(BODY))
        assert len(log.read_chain(DEFAULT_NAMESPACE, audit_log.HOST_CHAIN_ID)) == 1
        assert log.read_chain(DEFAULT_NAMESPACE, PLAN) == []

    def test_mutating_a_returned_record_does_not_change_the_stored_record(self):
        log = self.make_log()
        record = log.append(DEFAULT_NAMESPACE, PLAN, {"detail": {"sha": "abc"}})
        record["detail"]["sha"] = "tampered"
        record["seq"] = 99
        stored = log.read_chain(DEFAULT_NAMESPACE, PLAN)[0]
        assert stored["detail"] == {"sha": "abc"}
        assert stored["seq"] == 0

    def test_mutating_the_body_after_the_append_changes_nothing(self):
        log = self.make_log()
        body = {"action": "a", "outcome": "allowed", "detail": {"sha": "abc"}}
        log.append(DEFAULT_NAMESPACE, PLAN, body)
        body["detail"]["sha"] = "tampered"
        assert log.read_chain(DEFAULT_NAMESPACE, PLAN)[0]["detail"] == {"sha": "abc"}

    def test_two_logs_share_no_state(self):
        first = self.make_log()
        second = self.make_log()
        first.append(DEFAULT_NAMESPACE, PLAN, dict(BODY))
        assert second.read_chain(DEFAULT_NAMESPACE, PLAN) == []
        assert second.list_chains(DEFAULT_NAMESPACE) == []

    def test_an_invalid_namespace_is_refused(self):
        log = self.make_log()
        with pytest.raises(InvalidRecordKeyError):
            log.append(INVALID_NAMESPACE, PLAN, dict(BODY))

    @pytest.mark.parametrize("chain", INVALID_CHAINS)
    def test_an_invalid_chain_is_refused(self, chain):
        log = self.make_log()
        with pytest.raises(InvalidRecordKeyError):
            log.append(DEFAULT_NAMESPACE, chain, dict(BODY))

    def test_a_refused_append_writes_nothing(self):
        log = self.make_log()
        for chain in INVALID_CHAINS:
            with pytest.raises(InvalidRecordKeyError):
                log.append(DEFAULT_NAMESPACE, chain, dict(BODY))
        assert log.list_chains(DEFAULT_NAMESPACE) == []
        assert log.read_chain(DEFAULT_NAMESPACE, PLAN) == []

    @pytest.mark.parametrize("field", ["seq", "prev_hash", "hash"])
    def test_a_body_that_carries_a_chain_field_is_refused(self, field):
        log = self.make_log()
        with pytest.raises(ValueError):
            log.append(DEFAULT_NAMESPACE, PLAN, dict(BODY, **{field: "x"}))
        assert log.read_chain(DEFAULT_NAMESPACE, PLAN) == []


# --------------------------------------------------------------------------
# Tamper detection
# --------------------------------------------------------------------------


class TestInMemoryAuditLogTamperBehaviour:
    """What the chain must notice when the stored entries are rewritten."""

    def tampered(self, entries):
        log = audit_memory.InMemoryAuditLog()
        _seed_chain(log)
        _rewrite_raw(log, DEFAULT_NAMESPACE, PLAN, entries)
        return audit_chain.verify_entries(log.read_chain(DEFAULT_NAMESPACE, PLAN))

    def test_an_intact_chain_verifies(self):
        log = audit_memory.InMemoryAuditLog()
        entries = _seed_chain(log)
        assert audit_chain.verify_entries(entries) is None

    def test_an_edited_record_is_reported_at_its_own_position(self):
        log = audit_memory.InMemoryAuditLog()
        entries = _seed_chain(log)
        entries[2] = dict(entries[2], detail={"sha": "tampered"})
        assert self.tampered(entries) == 2

    def test_an_edited_and_rehashed_record_is_reported_at_the_next_record(self):
        log = audit_memory.InMemoryAuditLog()
        entries = _seed_chain(log)
        edited = dict(entries[2], detail={"sha": "tampered"})
        edited["hash"] = audit_chain.record_hash(edited)
        entries[2] = edited
        assert self.tampered(entries) == 3

    def test_a_deleted_middle_record_is_reported_at_its_own_position(self):
        log = audit_memory.InMemoryAuditLog()
        entries = _seed_chain(log)
        del entries[2]
        assert self.tampered(entries) == 2

    def test_reordered_records_are_reported_at_the_first_moved_one(self):
        log = audit_memory.InMemoryAuditLog()
        entries = _seed_chain(log)
        entries[1], entries[2] = entries[2], entries[1]
        assert self.tampered(entries) == 1

    def test_a_duplicated_record_is_reported_at_the_copy(self):
        log = audit_memory.InMemoryAuditLog()
        entries = _seed_chain(log)
        entries.insert(3, entries[2])
        assert self.tampered(entries) == 3

    def test_a_gap_in_the_sequence_is_reported_at_the_gap(self):
        log = audit_memory.InMemoryAuditLog()
        entries = _seed_chain(log)
        entries[3] = dict(entries[3], seq=4)
        assert self.tampered(entries) == 3

    def test_a_malformed_entry_is_reported_at_its_own_position(self):
        log = audit_memory.InMemoryAuditLog()
        entries = _seed_chain(log)
        entries[1] = None
        assert self.tampered(entries) == 1

    def test_an_entry_that_is_not_an_object_is_read_as_none(self):
        log = audit_memory.InMemoryAuditLog()
        entries = _seed_chain(log)
        entries[1] = None
        _rewrite_raw(log, DEFAULT_NAMESPACE, PLAN, entries)
        stored = log.read_chain(DEFAULT_NAMESPACE, PLAN)
        assert stored[1] is None
        assert stored[0] == entries[0]

    def test_a_truncated_tail_verifies(self):
        """Only the external anchor detects a truncated tail, never the chain."""
        log = audit_memory.InMemoryAuditLog()
        entries = _seed_chain(log)
        del entries[4]
        assert self.tampered(entries) is None

    def test_an_append_after_a_tamper_links_to_the_current_tail(self):
        log = audit_memory.InMemoryAuditLog()
        entries = _seed_chain(log)
        entries[2] = dict(entries[2], detail={"sha": "tampered"})
        _rewrite_raw(log, DEFAULT_NAMESPACE, PLAN, entries)
        appended = log.append(DEFAULT_NAMESPACE, PLAN, dict(BODY))
        assert appended["seq"] == 5
        assert appended["prev_hash"] == entries[-1]["hash"]

    def test_an_append_after_a_tamper_does_not_repair_the_chain(self):
        log = audit_memory.InMemoryAuditLog()
        entries = _seed_chain(log)
        entries[2] = dict(entries[2], detail={"sha": "tampered"})
        _rewrite_raw(log, DEFAULT_NAMESPACE, PLAN, entries)
        log.append(DEFAULT_NAMESPACE, PLAN, dict(BODY))
        stored = log.read_chain(DEFAULT_NAMESPACE, PLAN)
        assert audit_chain.verify_entries(stored) == 2


# --------------------------------------------------------------------------
# Concurrent appends
# --------------------------------------------------------------------------


class TestInMemoryAuditLogConcurrencyBehaviour:
    """What the log must do when several threads append at once."""

    def test_concurrent_appends_to_one_chain_claim_every_seq_once(self):
        log = audit_memory.InMemoryAuditLog()

        def worker():
            for _ in range(APPENDS_PER_THREAD):
                log.append(DEFAULT_NAMESPACE, PLAN, dict(BODY))

        threads = [threading.Thread(target=worker) for _ in range(THREADS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        entries = log.read_chain(DEFAULT_NAMESPACE, PLAN)
        assert len(entries) == THREADS * APPENDS_PER_THREAD
        assert sorted(entry["seq"] for entry in entries) == list(
            range(THREADS * APPENDS_PER_THREAD)
        )
        assert audit_chain.verify_entries(entries) is None

    def test_two_chains_appended_concurrently_keep_their_own_sequences(self):
        log = audit_memory.InMemoryAuditLog()

        def worker(chain):
            for _ in range(APPENDS_PER_THREAD):
                log.append(DEFAULT_NAMESPACE, chain, dict(BODY))

        threads = [
            threading.Thread(target=worker, args=(chain,))
            for chain in (PLAN, OTHER_PLAN)
            for _ in range(THREADS)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        for chain in (PLAN, OTHER_PLAN):
            entries = log.read_chain(DEFAULT_NAMESPACE, chain)
            assert len(entries) == THREADS * APPENDS_PER_THREAD
            assert sorted(entry["seq"] for entry in entries) == list(
                range(THREADS * APPENDS_PER_THREAD)
            )
            assert audit_chain.verify_entries(entries) is None


# --------------------------------------------------------------------------
# The contract module itself
# --------------------------------------------------------------------------


class TestAuditContractModule:
    """The shape of the reusable contract suite."""

    def test_the_contract_module_names_its_mixins(self):
        for name in (
            "AuditLogContract",
            "AuditTamperContract",
            "AuditConcurrencyContract",
        ):
            assert hasattr(audit_contract, name), name

    def test_the_contract_module_offers_both_namespace_variants(self):
        assert _mixin("MultiNamespaceContract", "AuditMultiNamespaceContract")
        assert _mixin("SingleNamespaceContract", "AuditSingleNamespaceContract")

    def test_no_mixin_is_named_like_a_test_class(self):
        assert not [name for name in vars(audit_contract) if name.startswith("Test")]

    def test_the_contract_module_imports_no_test_framework(self):
        assert "pytest" not in vars(audit_contract)

    def test_the_contract_mixin_takes_its_log_from_the_test_class(self):
        assert hasattr(audit_contract.AuditLogContract, "make_log")


class _DefaultNamespaceOnlyLog:
    """A minimal audit log that serves the default namespace only."""

    def __init__(self):
        self._chains: dict[str, list[dict]] = {}

    def _check(self, namespace) -> None:
        validate_namespace(namespace)
        if namespace != DEFAULT_NAMESPACE:
            raise UnknownNamespaceError("unknown namespace")

    def append(self, namespace, chain, body):
        self._check(namespace)
        chain = audit_log.validate_chain(chain)
        entries = self._chains.setdefault(chain, [])
        previous = entries[-1] if entries else None
        record = audit_chain.link(previous, copy.deepcopy(body))
        entries.append(record)
        return copy.deepcopy(record)

    def read_chain(self, namespace, chain):
        self._check(namespace)
        chain = audit_log.validate_chain(chain)
        return copy.deepcopy(self._chains.get(chain, []))

    def head(self, namespace, chain):
        self._check(namespace)
        chain = audit_log.validate_chain(chain)
        entries = self._chains.get(chain, [])
        return copy.deepcopy(entries[-1]) if entries else None

    def list_chains(self, namespace):
        self._check(namespace)
        return sorted(self._chains)


class TestSingleNamespaceContract(
    _mixin("SingleNamespaceContract", "AuditSingleNamespaceContract")
):
    """The single-namespace contract, run against a default-namespace log."""

    def make_log(self):
        return _DefaultNamespaceOnlyLog()
