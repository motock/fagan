"""Tests for the in-memory reference record store (ADR 0012, stage A)."""

from __future__ import annotations

import threading

from pipeline import store_contract
from pipeline.store_contract import MultiNamespaceContract, RecordStoreContract
from pipeline.store_contract_transactions import TransactionContract
from pipeline.store_memory import InMemoryRecordStore
from tests.unit._store_outbox_helpers import MultiNamespaceOutboxBehaviour

THREADS = 8
RECORDS_PER_THREAD_PER_RUN = 25


class TestInMemoryRecordStore(RecordStoreContract, MultiNamespaceContract):
    """The shared behaviour contract, run against the reference store."""

    def make_store(self):
        return InMemoryRecordStore()


class TestInMemoryRecordStoreTransaction(TransactionContract):
    """The transaction lock contract, run against the reference store."""

    # The in-memory store serves every valid namespace, so its lock is taken
    # per (namespace, plan) pair.
    serves_other_namespaces = True

    def make_store(self):
        return InMemoryRecordStore()


class TestInMemoryRecordStoreOutbox(MultiNamespaceOutboxBehaviour):
    """The per-channel outbox contract, run against the reference store."""

    def make_store(self):
        return InMemoryRecordStore()


class TestInMemoryDirect:
    """Behaviour that only the in-memory implementation can show."""

    def test_two_instances_share_no_state(self):
        first = InMemoryRecordStore()
        second = InMemoryRecordStore()
        first.put_manifest('default', 'plan-a', {'version': 1})
        assert second.get_manifest('default', 'plan-a') is None
        assert second.list_plans('default') == []

    def test_concurrent_appends_lose_and_interleave_nothing(self):
        store = InMemoryRecordStore()

        def worker(thread_index):
            for sequence in range(RECORDS_PER_THREAD_PER_RUN):
                store.append_decision(
                    'default',
                    'plan-a',
                    {'thread': thread_index, 'sequence': sequence},
                )

        threads = [
            threading.Thread(target=worker, args=(index,)) for index in range(THREADS)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        records = store.get_decisions('default', 'plan-a')
        assert len(records) == THREADS * RECORDS_PER_THREAD_PER_RUN
        for thread_index in range(THREADS):
            sequences = [
                record['sequence'] for record in records if record['thread'] == thread_index
            ]
            assert sequences == list(range(RECORDS_PER_THREAD_PER_RUN))

    def test_contract_module_does_not_import_pytest(self):
        assert 'pytest' not in vars(store_contract)
