"""Shared behaviour suite for the per-channel outbox queues (ADR 0012).

ADR 0012 decision 3 gives every record store a per-channel outbox queue, so a
notification channel retries independently of every other channel.  This module
holds the behaviour every implementation must show; the concrete test classes
live in ``test_store_memory.py``, ``test_store_file_records.py`` and
``test_store_sqlite_records.py``.

The suite is deliberately store-agnostic: it only calls the public record store
API (``append_outbox`` / ``read_outbox`` / ``ack_outbox`` / ``list_outboxes``),
never a private helper, so an implementation may lay its storage out however it
likes.

Like the other contract modules it imports no test framework: plain ``assert``
statements and a small ``_expect`` helper, so the same cases run under any
harness.  No class here is named ``Test*`` -- pytest must never collect the
mixin itself (it would call the unimplemented ``make_store``).
"""

from __future__ import annotations

from pipeline.store_records import (
    DEFAULT_NAMESPACE,
    InvalidRecordKeyError,
    UnknownNamespaceError,
)

PLAN = 'plan-a'
OTHER_PLAN = 'plan-b'
LEGACY = '_legacy'
INSTANCE = 'email-a'

# Channels that must be refused: empty, upper case, a leading dot, a path
# separator, and one character past the instance-name length limit.
INVALID_CHANNELS = ['Bad', '', '.x', 'a' * 33, 'a/b']


def _expect(exc_type, fn):
    """Assert that calling ``fn`` raises exactly ``exc_type``."""
    try:
        fn()
    except exc_type:
        return
    raise AssertionError(f'{fn} did not raise {exc_type.__name__}')


def _circular_record():
    """Return a record that no JSON encoder can serialise."""
    record: dict = {'event': 'x'}
    record['self'] = record
    return record


class OutboxBehaviour:
    """The per-channel outbox behaviour every record store must show."""

    def make_store(self):
        """Return a fresh, empty store under test."""
        raise NotImplementedError

    # -- append and read -----------------------------------------------
    def test_append_then_read_returns_the_record_with_an_id(self):
        store = self.make_store()
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'event': 'x'})
        entries = store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)
        assert len(entries) == 1
        entry = entries[0]
        assert set(entry) == {'id', 'record'}
        assert isinstance(entry['id'], str) and entry['id']
        assert entry['record'] == {'event': 'x'}

    def test_read_returns_the_entries_in_append_order(self):
        store = self.make_store()
        for index in range(3):
            store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': index})
        entries = store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)
        assert [entry['record']['n'] for entry in entries] == [0, 1, 2]

    def test_read_does_not_remove_the_entries(self):
        store = self.make_store()
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'event': 'x'})
        first = store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)
        second = store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)
        assert first == second
        assert len(second) == 1

    def test_mutating_a_returned_record_does_not_change_the_stored_one(self):
        store = self.make_store()
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'event': 'x'})
        entry = store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)[0]
        entry['record']['event'] = 'mutated'
        entry['id'] = 'mutated-id'
        again = store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)[0]
        assert again['record'] == {'event': 'x'}
        assert again['id'] != 'mutated-id'

    def test_read_of_a_queue_that_was_never_written_is_empty(self):
        store = self.make_store()
        assert store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE) == []

    # -- acking --------------------------------------------------------
    def test_ack_removes_exactly_the_acked_entry_and_returns_one(self):
        store = self.make_store()
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': 0})
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': 1})
        entries = store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)
        removed = store.ack_outbox(
            DEFAULT_NAMESPACE, PLAN, INSTANCE, [entries[0]['id']]
        )
        assert removed == 1
        remaining = store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)
        assert [entry['record']['n'] for entry in remaining] == [1]

    def test_ack_of_an_unknown_id_removes_nothing_and_returns_zero(self):
        store = self.make_store()
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': 0})
        removed = store.ack_outbox(
            DEFAULT_NAMESPACE, PLAN, INSTANCE, ['no-such-id']
        )
        assert removed == 0
        assert len(store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)) == 1

    def test_ack_of_an_empty_id_list_removes_nothing(self):
        store = self.make_store()
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': 0})
        removed = store.ack_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, [])
        assert removed == 0
        assert len(store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)) == 1

    def test_two_identical_records_are_two_entries_and_one_ack_leaves_one(self):
        store = self.make_store()
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': 0})
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': 0})
        entries = store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)
        assert len(entries) == 2
        removed = store.ack_outbox(
            DEFAULT_NAMESPACE, PLAN, INSTANCE, [entries[0]['id']]
        )
        assert removed == 1
        assert len(store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)) == 1

    def test_acking_several_ids_removes_one_entry_per_id(self):
        store = self.make_store()
        for index in range(3):
            store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': index})
        entries = store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)
        removed = store.ack_outbox(
            DEFAULT_NAMESPACE,
            PLAN,
            INSTANCE,
            [entries[0]['id'], entries[2]['id']],
        )
        assert removed == 2
        remaining = store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)
        assert [entry['record']['n'] for entry in remaining] == [1]

    # -- isolation -----------------------------------------------------
    def test_channels_are_isolated_from_each_other(self):
        store = self.make_store()
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY, {'channel': 'legacy'})
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'channel': 'instance'})
        legacy = store.read_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY)
        instance = store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)
        assert [entry['record']['channel'] for entry in legacy] == ['legacy']
        assert [entry['record']['channel'] for entry in instance] == ['instance']

    def test_an_instance_named_legacy_never_collides_with_the_legacy_queue(self):
        store = self.make_store()
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY, {'channel': 'legacy'})
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, 'legacy', {'channel': 'instance'})
        legacy = store.read_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY)
        instance = store.read_outbox(DEFAULT_NAMESPACE, PLAN, 'legacy')
        assert [entry['record']['channel'] for entry in legacy] == ['legacy']
        assert [entry['record']['channel'] for entry in instance] == ['instance']

    def test_plans_are_isolated_from_each_other(self):
        store = self.make_store()
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'plan': PLAN})
        store.append_outbox(DEFAULT_NAMESPACE, OTHER_PLAN, INSTANCE, {'plan': OTHER_PLAN})
        first = store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)
        second = store.read_outbox(DEFAULT_NAMESPACE, OTHER_PLAN, INSTANCE)
        assert [entry['record']['plan'] for entry in first] == [PLAN]
        assert [entry['record']['plan'] for entry in second] == [OTHER_PLAN]

    def test_an_acked_entry_in_one_queue_leaves_the_other_queue_intact(self):
        store = self.make_store()
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY, {'n': 0})
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': 0})
        legacy_entry = store.read_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY)[0]
        store.ack_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY, [legacy_entry['id']])
        assert len(store.read_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY)) == 0
        assert len(store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)) == 1

    # -- listing -------------------------------------------------------
    def test_list_outboxes_is_sorted_and_lists_the_queues_with_entries(self):
        store = self.make_store()
        store.append_outbox(DEFAULT_NAMESPACE, OTHER_PLAN, INSTANCE, {'n': 0})
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': 0})
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY, {'n': 0})
        listed = store.list_outboxes(DEFAULT_NAMESPACE)
        assert listed == sorted(listed)
        assert (PLAN, LEGACY) in listed
        assert (PLAN, INSTANCE) in listed
        assert (OTHER_PLAN, INSTANCE) in listed

    def test_list_outboxes_omits_a_queue_once_it_is_fully_acked(self):
        store = self.make_store()
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': 0})
        assert (PLAN, INSTANCE) in store.list_outboxes(DEFAULT_NAMESPACE)
        entry = store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)[0]
        store.ack_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, [entry['id']])
        assert (PLAN, INSTANCE) not in store.list_outboxes(DEFAULT_NAMESPACE)

    def test_list_outboxes_of_an_empty_namespace_is_empty(self):
        store = self.make_store()
        assert store.list_outboxes(DEFAULT_NAMESPACE) == []

    # -- limits --------------------------------------------------------
    def test_a_limit_of_zero_returns_no_entries(self):
        store = self.make_store()
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': 0})
        assert store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, limit=0) == []

    def test_a_limit_of_one_returns_the_oldest_entry(self):
        store = self.make_store()
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': 0})
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': 1})
        entries = store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, limit=1)
        assert [entry['record']['n'] for entry in entries] == [0]

    def test_a_limit_larger_than_the_queue_returns_every_entry(self):
        store = self.make_store()
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': 0})
        entries = store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, limit=10)
        assert len(entries) == 1

    # -- rejected input ------------------------------------------------
    def test_an_invalid_channel_is_refused_and_writes_nothing(self):
        store = self.make_store()
        for channel in INVALID_CHANNELS:
            _expect(
                InvalidRecordKeyError,
                lambda channel=channel: store.append_outbox(
                    DEFAULT_NAMESPACE, PLAN, channel, {'n': 0}
                ),
            )
            _expect(
                InvalidRecordKeyError,
                lambda channel=channel: store.read_outbox(
                    DEFAULT_NAMESPACE, PLAN, channel
                ),
            )
            _expect(
                InvalidRecordKeyError,
                lambda channel=channel: store.ack_outbox(
                    DEFAULT_NAMESPACE, PLAN, channel, ['x']
                ),
            )
        assert store.list_outboxes(DEFAULT_NAMESPACE) == []

    def test_a_non_dict_record_raises_type_error(self):
        store = self.make_store()
        _expect(
            TypeError,
            lambda: store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, 'nope'),
        )
        _expect(
            TypeError,
            lambda: store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, ['nope']),
        )
        assert store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE) == []

    def test_a_record_that_is_not_json_serialisable_raises_and_writes_nothing(self):
        store = self.make_store()
        _expect(
            (TypeError, ValueError),
            lambda: store.append_outbox(
                DEFAULT_NAMESPACE, PLAN, INSTANCE, _circular_record()
            ),
        )
        assert store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE) == []
        assert store.list_outboxes(DEFAULT_NAMESPACE) == []

    def test_an_invalid_namespace_is_refused(self):
        store = self.make_store()
        _expect(
            InvalidRecordKeyError,
            lambda: store.append_outbox('Bad NS', PLAN, INSTANCE, {'n': 0}),
        )
        _expect(
            InvalidRecordKeyError,
            lambda: store.read_outbox('Bad NS', PLAN, INSTANCE),
        )
        _expect(
            InvalidRecordKeyError,
            lambda: store.ack_outbox('Bad NS', PLAN, INSTANCE, ['x']),
        )
        _expect(
            InvalidRecordKeyError,
            lambda: store.list_outboxes('Bad NS'),
        )

    def test_an_invalid_plan_name_is_refused(self):
        store = self.make_store()
        _expect(
            InvalidRecordKeyError,
            lambda: store.append_outbox(DEFAULT_NAMESPACE, '../x', INSTANCE, {'n': 0}),
        )
        _expect(
            InvalidRecordKeyError,
            lambda: store.read_outbox(DEFAULT_NAMESPACE, '../x', INSTANCE),
        )
        _expect(
            InvalidRecordKeyError,
            lambda: store.ack_outbox(DEFAULT_NAMESPACE, '../x', INSTANCE, ['x']),
        )


class SingleNamespaceOutboxBehaviour(OutboxBehaviour):
    """Outbox behaviour for a store that serves only ``default``."""

    def test_another_namespace_is_refused(self):
        store = self.make_store()
        for call in (
            lambda: store.append_outbox('acme', PLAN, INSTANCE, {'n': 0}),
            lambda: store.read_outbox('acme', PLAN, INSTANCE),
            lambda: store.ack_outbox('acme', PLAN, INSTANCE, ['x']),
            lambda: store.list_outboxes('acme'),
        ):
            _expect(UnknownNamespaceError, call)
        assert store.list_outboxes(DEFAULT_NAMESPACE) == []


class MultiNamespaceOutboxBehaviour(OutboxBehaviour):
    """Outbox behaviour for a store that serves every valid namespace."""

    def test_namespaces_are_isolated_from_each_other(self):
        store = self.make_store()
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'ns': 'default'})
        store.append_outbox('acme', PLAN, INSTANCE, {'ns': 'acme'})
        default_entries = store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)
        acme_entries = store.read_outbox('acme', PLAN, INSTANCE)
        assert [entry['record']['ns'] for entry in default_entries] == ['default']
        assert [entry['record']['ns'] for entry in acme_entries] == ['acme']

    def test_list_outboxes_only_lists_the_requested_namespace(self):
        store = self.make_store()
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': 0})
        store.append_outbox('acme', PLAN, INSTANCE, {'n': 0})
        assert store.list_outboxes(DEFAULT_NAMESPACE) == [(PLAN, INSTANCE)]
        assert store.list_outboxes('acme') == [(PLAN, INSTANCE)]
