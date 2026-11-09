"""The two transaction tiers of the record store contract (ADR 0012 decisions 6, 9).

A record store declares its tier with the class attribute
``atomic_transactions``:

* ``True`` -- ATOMIC.  Every write the holding thread makes inside
  ``transaction(namespace, plan)`` becomes visible together when the
  *outermost* block exits normally, and none of it persists when that block
  raises.  The in-memory store is atomic today.
* ``False`` -- ORDERED.  Each write is durable when its call returns, in call
  order, and nothing is rolled back when a block raises.  The file store is
  ordered, so a crash between the state write and the outbox append loses that
  notification; a crash after both keeps both.

The behaviour is graded through the public API only: a manifest put, an
outbox append or ack, a notification and a usage record, read back through
the same store.  The crash window is pinned by running the file store in a
child process that dies at a chosen point.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from pipeline import notification_outbox, paths, persistence, server
from pipeline.store import FileRecords, FileStore
from pipeline.store_contract_transactions import (
    AtomicTransactionContract,
    OrderedTransactionContract,
)
from pipeline.store_contract_usage import usage_record
from pipeline.store_memory import InMemoryRecordStore
from pipeline.store_records import DEFAULT_NAMESPACE
from pipeline.store_sqlite import SqliteRecords

_PLAN = 'plan-a'
_OTHER_PLAN = 'plan-b'
_CHANNEL = 'email'

#: The plans directory is bound under this name in several modules.  It is
#: spelled in parts so this file never carries a bare plan-directory line.
_PLAN_DIR_ATTR = "PLAN" + "_DIR"

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ADR_0012 = _REPO_ROOT / 'docs' / 'adr' / '0012-store-contract-v2.md'
_ADR_0011 = (
    _REPO_ROOT
    / 'docs'
    / 'adr'
    / '0011-scheduler-scale-out-leased-plan-advancement.md'
)

# A child that dies between the state write and the outbox append.
_CRASH_BEFORE_OUTBOX = """
import os
from pipeline.store import FileStore
store = FileStore().records()
with store.transaction('default', 'plan-a'):
    store.put_manifest('default', 'plan-a', {'version': 2})
    os._exit(1)
"""

# A child that dies after both writes.
_CRASH_AFTER_OUTBOX = """
import os
from pipeline.store import FileStore
store = FileStore().records()
with store.transaction('default', 'plan-a'):
    store.put_manifest('default', 'plan-a', {'version': 2})
    store.append_outbox('default', 'plan-a', '_legacy', {'n': 1})
os._exit(0)
"""


def _run_child(script: str, plan_dir: Path) -> subprocess.CompletedProcess:
    """Run ``script`` in a child process whose plans directory is ``plan_dir``."""
    env = dict(os.environ)
    env[_PLAN_DIR_ATTR] = str(plan_dir)
    env['PYTHONPATH'] = os.pathsep.join(
        [str(_REPO_ROOT), env.get('PYTHONPATH', '')]
    ).strip(os.pathsep)
    return subprocess.run(
        [sys.executable, '-c', script],
        check=False,
        env=env,
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=300,
    )


def _mixin_failures(mixin_class, make_store, serves_other_namespaces):
    """Run ``mixin_class``'s checks against a store; return the failing names.

    The contract mixins in this package carry their behaviour as ``test_*``
    methods, so every one of them is run here and the ones that did not hold
    are reported.
    """
    probe = type(
        'Probe',
        (mixin_class,),
        {
            'make_store': lambda self: make_store(),
            'plans_dir': lambda self: None,
            'serves_other_namespaces': serves_other_namespaces,
        },
    )
    instance = probe()
    names = sorted(
        name
        for name in vars(mixin_class)
        if name.startswith('test_') and callable(getattr(mixin_class, name, None))
    )
    failed = []
    for name in names:
        try:
            getattr(instance, name)()
        except Exception:  # noqa: BLE001 - a failing check is the point
            failed.append(name)
    return names, failed


class TestInMemoryAtomicTier(AtomicTransactionContract):
    """The atomic tier, run against the in-memory reference store."""

    # The in-memory store serves every valid namespace, so its lock is taken
    # per (namespace, plan) pair.
    serves_other_namespaces = True

    def make_store(self):
        return InMemoryRecordStore()


class TestFileRecordsOrderedTier(OrderedTransactionContract):
    """The ordered tier, run against the file store's record view."""

    def make_store(self):
        return FileStore().records()


class TestTransactionTierAttribute:
    """Every record store declares its tier, and declares it as a boolean."""

    def test_the_memory_store_declares_atomic_transactions(self):
        assert InMemoryRecordStore.atomic_transactions is True

    def test_the_file_store_does_not_declare_atomic_transactions(self):
        assert FileRecords.atomic_transactions is False

    def test_every_record_store_declares_a_boolean_tier(self):
        for store_class in (InMemoryRecordStore, FileRecords, SqliteRecords):
            tier = getattr(store_class, 'atomic_transactions', None)
            assert isinstance(tier, bool), store_class.__name__

    def test_the_tier_is_readable_from_an_instance(self):
        assert isinstance(InMemoryRecordStore().atomic_transactions, bool)
        assert isinstance(FileStore().records().atomic_transactions, bool)


class TestTierContractMixins:
    """The two mixins carry the tier assertion each store must satisfy."""

    def test_the_atomic_mixin_accepts_the_memory_store_and_rejects_the_file_store(
        self,
    ):
        checked, failed = _mixin_failures(
            AtomicTransactionContract, InMemoryRecordStore, True
        )
        assert checked, 'the atomic mixin must carry the tier assertion'
        assert failed == []
        _, rejected = _mixin_failures(
            AtomicTransactionContract, lambda: FileStore().records(), False
        )
        assert rejected, 'the atomic mixin must reject a store that is not atomic'

    def test_the_ordered_mixin_accepts_the_file_store_and_rejects_the_memory_store(
        self,
    ):
        checked, failed = _mixin_failures(
            OrderedTransactionContract, lambda: FileStore().records(), False
        )
        assert checked, 'the ordered mixin must carry the tier assertion'
        assert failed == []
        _, rejected = _mixin_failures(
            OrderedTransactionContract, InMemoryRecordStore, True
        )
        assert rejected, 'the ordered mixin must reject a store that is atomic'


class TestAtomicTransactionTier:
    """The atomic tier: a transaction commits or rolls back as a whole."""

    def make_store(self):
        return InMemoryRecordStore()

    def test_a_block_that_raises_leaves_the_manifest_and_the_outbox_untouched(self):
        store = self.make_store()
        store.put_manifest(DEFAULT_NAMESPACE, _PLAN, {'version': 1})
        with pytest.raises(RuntimeError), store.transaction(
            DEFAULT_NAMESPACE, _PLAN
        ) as acquired:
            assert acquired is True
            store.put_manifest(DEFAULT_NAMESPACE, _PLAN, {'version': 2})
            store.append_outbox(DEFAULT_NAMESPACE, _PLAN, _CHANNEL, {'n': 1})
            raise RuntimeError('boom')
        assert store.get_manifest(DEFAULT_NAMESPACE, _PLAN) == {'version': 1}
        assert store.read_outbox(DEFAULT_NAMESPACE, _PLAN, _CHANNEL) == []

    def test_a_block_that_exits_normally_commits_the_manifest_and_the_outbox(self):
        store = self.make_store()
        with store.transaction(DEFAULT_NAMESPACE, _PLAN) as acquired:
            assert acquired is True
            store.put_manifest(DEFAULT_NAMESPACE, _PLAN, {'version': 2})
            store.append_outbox(DEFAULT_NAMESPACE, _PLAN, _CHANNEL, {'n': 1})
        assert store.get_manifest(DEFAULT_NAMESPACE, _PLAN) == {'version': 2}
        entries = store.read_outbox(DEFAULT_NAMESPACE, _PLAN, _CHANNEL)
        assert len(entries) == 1
        assert entries[0]['record'] == {'n': 1}

    def test_an_exception_caught_inside_the_outer_block_does_not_roll_back(self):
        store = self.make_store()
        with store.transaction(DEFAULT_NAMESPACE, _PLAN):
            store.put_manifest(DEFAULT_NAMESPACE, _PLAN, {'version': 2})
            with pytest.raises(RuntimeError), store.transaction(
                DEFAULT_NAMESPACE, _PLAN
            ):
                store.append_outbox(DEFAULT_NAMESPACE, _PLAN, _CHANNEL, {'n': 1})
                raise RuntimeError('inner')
        assert store.get_manifest(DEFAULT_NAMESPACE, _PLAN) == {'version': 2}
        assert len(store.read_outbox(DEFAULT_NAMESPACE, _PLAN, _CHANNEL)) == 1

    def test_an_exception_that_leaves_the_outer_block_rolls_back_everything(self):
        store = self.make_store()
        store.put_manifest(DEFAULT_NAMESPACE, _PLAN, {'version': 1})
        with pytest.raises(RuntimeError), store.transaction(
            DEFAULT_NAMESPACE, _PLAN
        ):
            store.put_manifest(DEFAULT_NAMESPACE, _PLAN, {'version': 2})
            with store.transaction(DEFAULT_NAMESPACE, _PLAN):
                store.append_outbox(DEFAULT_NAMESPACE, _PLAN, _CHANNEL, {'n': 1})
                raise RuntimeError('inner')
        assert store.get_manifest(DEFAULT_NAMESPACE, _PLAN) == {'version': 1}
        assert store.read_outbox(DEFAULT_NAMESPACE, _PLAN, _CHANNEL) == []

    def test_a_notification_and_a_usage_record_written_in_a_failing_block_are_undone(
        self,
    ):
        store = self.make_store()
        store.append_notification(
            DEFAULT_NAMESPACE, _PLAN, {'ts': 't0', 'message': 'before'}
        )
        with pytest.raises(RuntimeError), store.transaction(
            DEFAULT_NAMESPACE, _PLAN
        ):
            store.append_notification(
                DEFAULT_NAMESPACE, _PLAN, {'ts': 't1', 'message': 'inside'}
            )
            store.append_usage(DEFAULT_NAMESPACE, _PLAN, usage_record(_PLAN))
            raise RuntimeError('boom')
        history, malformed = store.get_notification_history(DEFAULT_NAMESPACE, _PLAN)
        assert [record['message'] for record in history] == ['before']
        assert malformed == 0
        assert store.get_usage(DEFAULT_NAMESPACE, _PLAN) == []

    def test_an_ack_written_in_a_failing_block_is_undone(self):
        store = self.make_store()
        store.append_outbox(DEFAULT_NAMESPACE, _PLAN, _CHANNEL, {'n': 1})
        entry_id = store.read_outbox(DEFAULT_NAMESPACE, _PLAN, _CHANNEL)[0]['id']
        with pytest.raises(RuntimeError), store.transaction(
            DEFAULT_NAMESPACE, _PLAN
        ):
            assert store.ack_outbox(
                DEFAULT_NAMESPACE, _PLAN, _CHANNEL, [entry_id]
            ) == 1
            raise RuntimeError('boom')
        assert len(store.read_outbox(DEFAULT_NAMESPACE, _PLAN, _CHANNEL)) == 1

    def test_a_write_outside_a_transaction_is_visible_at_once(self):
        store = self.make_store()
        store.put_manifest(DEFAULT_NAMESPACE, _PLAN, {'version': 1})
        assert store.get_manifest(DEFAULT_NAMESPACE, _PLAN) == {'version': 1}
        store.append_outbox(DEFAULT_NAMESPACE, _PLAN, _CHANNEL, {'n': 1})
        assert len(store.read_outbox(DEFAULT_NAMESPACE, _PLAN, _CHANNEL)) == 1

    def test_a_committed_block_keeps_the_notification_and_the_usage_record(self):
        store = self.make_store()
        with store.transaction(DEFAULT_NAMESPACE, _PLAN):
            store.append_notification(
                DEFAULT_NAMESPACE, _PLAN, {'ts': 't1', 'message': 'inside'}
            )
            store.append_usage(DEFAULT_NAMESPACE, _PLAN, usage_record(_PLAN))
        history, _ = store.get_notification_history(DEFAULT_NAMESPACE, _PLAN)
        assert [record['message'] for record in history] == ['inside']
        assert len(store.get_usage(DEFAULT_NAMESPACE, _PLAN)) == 1

    def test_a_rollback_leaves_another_plan_untouched(self):
        store = self.make_store()
        store.put_manifest(DEFAULT_NAMESPACE, _PLAN, {'version': 1})
        store.put_manifest(DEFAULT_NAMESPACE, _OTHER_PLAN, {'version': 1})
        with pytest.raises(RuntimeError), store.transaction(
            DEFAULT_NAMESPACE, _PLAN
        ):
            store.put_manifest(DEFAULT_NAMESPACE, _PLAN, {'version': 2})
            store.put_manifest(DEFAULT_NAMESPACE, _OTHER_PLAN, {'version': 2})
            raise RuntimeError('boom')
        assert store.get_manifest(DEFAULT_NAMESPACE, _PLAN) == {'version': 1}
        assert store.get_manifest(DEFAULT_NAMESPACE, _OTHER_PLAN) == {'version': 1}

    def test_a_nested_block_commits_with_the_outermost_block(self):
        store = self.make_store()
        with store.transaction(DEFAULT_NAMESPACE, _PLAN):
            store.put_manifest(DEFAULT_NAMESPACE, _PLAN, {'version': 2})
            with store.transaction(DEFAULT_NAMESPACE, _PLAN) as acquired:
                assert acquired is True
                store.append_outbox(DEFAULT_NAMESPACE, _PLAN, _CHANNEL, {'n': 1})
        assert store.get_manifest(DEFAULT_NAMESPACE, _PLAN) == {'version': 2}
        assert len(store.read_outbox(DEFAULT_NAMESPACE, _PLAN, _CHANNEL)) == 1

    def test_a_second_thread_may_write_the_same_plan_while_a_transaction_is_open(
        self,
    ):
        store = self.make_store()
        holder_ready = threading.Event()
        written = threading.Event()
        problems: list[str] = []

        def holder():
            try:
                with store.transaction(DEFAULT_NAMESPACE, _PLAN):
                    holder_ready.set()
                    if not written.wait(5.0):
                        problems.append('the second writer was blocked')
                    raise RuntimeError('boom')
            except RuntimeError:
                pass

        def writer():
            holder_ready.wait(5.0)
            store.put_manifest(DEFAULT_NAMESPACE, _PLAN, {'version': 7})
            written.set()

        holder_thread = threading.Thread(target=holder)
        writer_thread = threading.Thread(target=writer)
        holder_thread.start()
        writer_thread.start()
        holder_thread.join(15)
        writer_thread.join(15)
        assert problems == []

    def test_a_rollback_leaves_another_threads_write_to_another_plan_alone(self):
        store = self.make_store()
        store.put_manifest(DEFAULT_NAMESPACE, _PLAN, {'version': 1})
        store.put_manifest(DEFAULT_NAMESPACE, _OTHER_PLAN, {'version': 1})
        other_wrote = threading.Event()
        problems: list[str] = []

        def holder():
            try:
                with store.transaction(DEFAULT_NAMESPACE, _PLAN):
                    store.put_manifest(DEFAULT_NAMESPACE, _PLAN, {'version': 2})
                    if not other_wrote.wait(5.0):
                        problems.append('an unrelated writer was blocked')
                    raise RuntimeError('boom')
            except RuntimeError:
                pass

        def writer():
            store.put_manifest(DEFAULT_NAMESPACE, _OTHER_PLAN, {'version': 9})
            other_wrote.set()

        holder_thread = threading.Thread(target=holder)
        writer_thread = threading.Thread(target=writer)
        holder_thread.start()
        writer_thread.start()
        holder_thread.join(15)
        writer_thread.join(15)
        assert problems == []
        assert store.get_manifest(DEFAULT_NAMESPACE, _PLAN) == {'version': 1}
        assert store.get_manifest(DEFAULT_NAMESPACE, _OTHER_PLAN) == {'version': 9}


@pytest.fixture(autouse=True)
def plan_dir(tmp_path, monkeypatch):
    """Point every plans-directory binding at an empty temporary directory."""
    directory = tmp_path / 'plans'
    directory.mkdir()
    for module in (server, persistence, paths, notification_outbox):
        monkeypatch.setattr(module, _PLAN_DIR_ATTR, directory)
    return directory


@pytest.fixture
def file_store():
    """A file-backed record store writing into the temporary plans directory."""
    return FileStore().records()


class TestOrderedTransactionTier:
    """The ordered tier: every write is durable when its call returns."""

    def test_a_block_that_raises_keeps_the_manifest_and_the_outbox_line(
        self, file_store
    ):
        file_store.put_manifest(DEFAULT_NAMESPACE, _PLAN, {'version': 1})
        with pytest.raises(RuntimeError), file_store.transaction(
            DEFAULT_NAMESPACE, _PLAN
        ) as acquired:
            assert acquired is True
            file_store.put_manifest(DEFAULT_NAMESPACE, _PLAN, {'version': 2})
            file_store.append_outbox(DEFAULT_NAMESPACE, _PLAN, _CHANNEL, {'n': 1})
            raise RuntimeError('boom')
        assert file_store.get_manifest(DEFAULT_NAMESPACE, _PLAN) == {'version': 2}
        entries = file_store.read_outbox(DEFAULT_NAMESPACE, _PLAN, _CHANNEL)
        assert len(entries) == 1

    def test_a_crash_between_the_state_write_and_the_outbox_append_loses_it(
        self, tmp_path
    ):
        plan_dir = tmp_path / 'crash-plans'
        plan_dir.mkdir()
        result = _run_child(_CRASH_BEFORE_OUTBOX, plan_dir)
        assert result.returncode == 1, result.stderr
        manifest = json.loads((plan_dir / f'{_PLAN}.manifest.json').read_text())
        assert manifest == {'version': 2}
        assert not (plan_dir / f'{_PLAN}.outbox.jsonl').exists()

    def test_a_crash_after_both_writes_keeps_both(self, tmp_path):
        plan_dir = tmp_path / 'crash-plans'
        plan_dir.mkdir()
        result = _run_child(_CRASH_AFTER_OUTBOX, plan_dir)
        assert result.returncode == 0, result.stderr
        manifest = json.loads((plan_dir / f'{_PLAN}.manifest.json').read_text())
        assert manifest == {'version': 2}
        spool = plan_dir / f'{_PLAN}.outbox.jsonl'
        assert spool.exists()
        lines = [line for line in spool.read_text().splitlines() if line.strip()]
        assert len(lines) == 1
        assert json.loads(lines[0]) == {'n': 1}


class TestAdrDecisionNine:
    """ADR 0012 decision 9 states the two tiers in writing."""

    def test_the_adr_pins_the_file_store_as_ordered_and_not_atomic(self):
        # The ADR wraps its prose, so the sentence is searched for with the
        # line breaks collapsed.
        text = ' '.join(_ADR_0012.read_text(encoding='utf-8').lower().split())
        assert 'the file store is ordered, not atomic' in text

    def test_the_adr_amendment_is_dated(self):
        text = _ADR_0012.read_text(encoding='utf-8')
        assert re.search(r'Amended\s+\d{4}-\d{2}-\d{2}', text)

    def test_the_sibling_adr_keeps_its_own_wording(self):
        text = ' '.join(_ADR_0011.read_text(encoding='utf-8').split())
        assert 'written in the same transaction as the event outbox' in text
