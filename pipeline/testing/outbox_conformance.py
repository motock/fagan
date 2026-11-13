"""The published per-channel outbox suite (ADR 0012 decision 3).

An out-of-tree store package runs this suite as part of its conformance gate,
next to :mod:`pipeline.testing.store_conformance`. The package's conftest
supplies the ``record_store_factory`` fixture (a fresh, empty store for each
test), and a test module collects the suite with
``from pipeline.testing.outbox_conformance import *``.

Two optional fixtures describe the store's namespace policy:
``record_store_serves_other_namespaces`` (``True`` for a store that serves every
valid namespace; absent or ``False`` takes the strict reading, where a foreign
namespace is refused with ``UnknownNamespaceError``).

The behaviour is the one described in :mod:`pipeline.store_contract_outbox`,
graded through the public API only. The assertions are a published copy of
``tests/unit/_store_outbox_helpers.py``, which this package does not ship.

Each test declares the protocol names it exercises with ``@covers``;
``test_outbox_every_protocol_method_is_covered`` asserts that the declarations
cover every outbox method, and ``test_outbox_every_covered_name_is_an_outbox_method``
asserts that no declaration names anything else.

This module must stay importable without the repo's ``tests/`` tree and without
loading ``pipeline.server``.
"""

import pytest

from pipeline.store_contract_outbox import OutboxContract, OutboxSummaryContract
from pipeline.store_records import (
    DEFAULT_NAMESPACE,
    InvalidRecordKeyError,
    UnknownNamespaceError,
)

PLAN = 'plan-a'
OTHER_PLAN = 'plan-b'
LEGACY = '_legacy'
INSTANCE = 'email-a'
OTHER_NAMESPACE = 'acme'
INVALID_NAMESPACE = 'Bad NS'
INVALID_PLAN = '../x'
# Channels that must be refused: empty, upper case, a leading dot, a path
# separator, and one character past the instance-name length limit.
INVALID_CHANNELS = ['Bad', '', '.x', 'a' * 33, 'a/b']
OLD_TS = '2000-01-01T00:00:00Z'

#: Every public method the outbox mixins declare: the source of truth.
OUTBOX_METHODS = tuple(
    sorted(
        name
        for mixin in (OutboxContract, OutboxSummaryContract)
        for name, value in vars(mixin).items()
        if not name.startswith('_') and callable(value)
    )
)

#: Protocol names declared exercised by at least one test in this suite.
COVERED = set()


def covers(*names):
    """Declare that the decorated test exercises the named outbox methods."""

    def mark(test):
        COVERED.update(names)
        return test

    return mark


def _expect(exc_type, fn):
    """Assert that calling ``fn`` raises exactly ``exc_type``."""
    try:
        fn()
    except exc_type:
        return
    raise AssertionError(f'{fn} did not raise {exc_type.__name__}')


def _circular_record():
    """Return a record that no JSON encoder can serialise."""
    record = {'event': 'x'}
    record['self'] = record
    return record


def _serves_other_namespaces(request):
    """Return the store's declared namespace policy; absent means strict."""
    try:
        return request.getfixturevalue('record_store_serves_other_namespaces')
    except pytest.FixtureLookupError:
        return False


# -- append and read ------------------------------------------------------------


@covers('append_outbox', 'read_outbox')
def test_outbox_append_then_read_returns_the_record_with_an_id(record_store_factory):
    store = record_store_factory
    store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'event': 'x'})
    entries = store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)
    assert len(entries) == 1
    entry = entries[0]
    assert set(entry) == {'id', 'record'}
    assert isinstance(entry['id'], str) and entry['id']
    assert entry['record'] == {'event': 'x'}


@covers('append_outbox', 'read_outbox')
def test_outbox_read_returns_the_entries_in_append_order(record_store_factory):
    store = record_store_factory
    for index in range(3):
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': index})
    entries = store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)
    assert [entry['record']['n'] for entry in entries] == [0, 1, 2]


@covers('append_outbox', 'read_outbox')
def test_outbox_read_does_not_remove_the_entries(record_store_factory):
    store = record_store_factory
    store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'event': 'x'})
    first = store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)
    second = store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)
    assert first == second
    assert len(second) == 1


@covers('append_outbox', 'read_outbox')
def test_outbox_mutating_a_returned_entry_changes_nothing_stored(record_store_factory):
    store = record_store_factory
    store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'event': 'x'})
    entry = store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)[0]
    entry['record']['event'] = 'mutated'
    entry['id'] = 'mutated-id'
    again = store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)[0]
    assert again['record'] == {'event': 'x'}
    assert again['id'] != 'mutated-id'


@covers('append_outbox', 'read_outbox')
def test_outbox_mutating_the_appended_dict_changes_nothing_queued(record_store_factory):
    store = record_store_factory
    record = {'event': 'x'}
    store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, record)
    record['event'] = 'mutated'
    entries = store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)
    assert entries[0]['record'] == {'event': 'x'}


@covers('read_outbox')
def test_outbox_read_of_a_queue_never_written_is_empty(record_store_factory):
    store = record_store_factory
    assert store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE) == []


# -- acking ----------------------------------------------------------------------


@covers('append_outbox', 'read_outbox', 'ack_outbox')
def test_outbox_ack_removes_exactly_the_acked_entry_and_returns_one(record_store_factory):
    store = record_store_factory
    store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': 0})
    store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': 1})
    entries = store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)
    removed = store.ack_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, [entries[0]['id']])
    assert removed == 1
    remaining = store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)
    assert [entry['record']['n'] for entry in remaining] == [1]


@covers('append_outbox', 'read_outbox', 'ack_outbox')
def test_outbox_ack_of_an_unknown_id_removes_nothing_and_returns_zero(record_store_factory):
    store = record_store_factory
    store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': 0})
    removed = store.ack_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, ['no-such-id'])
    assert removed == 0
    assert len(store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)) == 1


@covers('append_outbox', 'read_outbox', 'ack_outbox')
def test_outbox_ack_of_an_empty_id_list_removes_nothing(record_store_factory):
    store = record_store_factory
    store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': 0})
    removed = store.ack_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, [])
    assert removed == 0
    assert len(store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)) == 1


@covers('append_outbox', 'read_outbox', 'ack_outbox')
def test_outbox_two_identical_records_are_two_entries_and_one_ack_leaves_one(
    record_store_factory,
):
    store = record_store_factory
    store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': 0})
    store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': 0})
    entries = store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)
    assert len(entries) == 2
    removed = store.ack_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, [entries[0]['id']])
    assert removed == 1
    assert len(store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)) == 1


@covers('append_outbox', 'read_outbox', 'ack_outbox')
def test_outbox_acking_several_ids_removes_one_entry_per_id(record_store_factory):
    store = record_store_factory
    for index in range(3):
        store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': index})
    entries = store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)
    removed = store.ack_outbox(
        DEFAULT_NAMESPACE, PLAN, INSTANCE, [entries[0]['id'], entries[2]['id']]
    )
    assert removed == 2
    remaining = store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)
    assert [entry['record']['n'] for entry in remaining] == [1]


# -- isolation -------------------------------------------------------------------


@covers('append_outbox', 'read_outbox')
def test_outbox_channels_are_isolated_from_each_other(record_store_factory):
    store = record_store_factory
    store.append_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY, {'channel': 'legacy'})
    store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'channel': 'instance'})
    legacy = store.read_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY)
    instance = store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)
    assert [entry['record']['channel'] for entry in legacy] == ['legacy']
    assert [entry['record']['channel'] for entry in instance] == ['instance']


@covers('append_outbox', 'read_outbox')
def test_outbox_an_instance_named_legacy_never_collides_with_the_legacy_queue(
    record_store_factory,
):
    store = record_store_factory
    store.append_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY, {'channel': 'legacy'})
    store.append_outbox(DEFAULT_NAMESPACE, PLAN, 'legacy', {'channel': 'instance'})
    legacy = store.read_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY)
    instance = store.read_outbox(DEFAULT_NAMESPACE, PLAN, 'legacy')
    assert [entry['record']['channel'] for entry in legacy] == ['legacy']
    assert [entry['record']['channel'] for entry in instance] == ['instance']


@covers('append_outbox', 'read_outbox')
def test_outbox_plans_are_isolated_from_each_other(record_store_factory):
    store = record_store_factory
    store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'plan': PLAN})
    store.append_outbox(DEFAULT_NAMESPACE, OTHER_PLAN, INSTANCE, {'plan': OTHER_PLAN})
    first = store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)
    second = store.read_outbox(DEFAULT_NAMESPACE, OTHER_PLAN, INSTANCE)
    assert [entry['record']['plan'] for entry in first] == [PLAN]
    assert [entry['record']['plan'] for entry in second] == [OTHER_PLAN]


@covers('append_outbox', 'read_outbox', 'ack_outbox')
def test_outbox_an_acked_entry_in_one_queue_leaves_the_other_queue_intact(
    record_store_factory,
):
    store = record_store_factory
    store.append_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY, {'n': 0})
    store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': 0})
    legacy_entry = store.read_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY)[0]
    store.ack_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY, [legacy_entry['id']])
    assert len(store.read_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY)) == 0
    assert len(store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)) == 1


# -- listing ---------------------------------------------------------------------


@covers('append_outbox', 'list_outboxes')
def test_outbox_list_outboxes_is_sorted_and_lists_the_queues_with_entries(
    record_store_factory,
):
    store = record_store_factory
    store.append_outbox(DEFAULT_NAMESPACE, OTHER_PLAN, INSTANCE, {'n': 0})
    store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': 0})
    store.append_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY, {'n': 0})
    listed = store.list_outboxes(DEFAULT_NAMESPACE)
    assert listed == sorted(listed)
    assert (PLAN, LEGACY) in listed
    assert (PLAN, INSTANCE) in listed
    assert (OTHER_PLAN, INSTANCE) in listed


@covers('append_outbox', 'read_outbox', 'ack_outbox', 'list_outboxes')
def test_outbox_list_outboxes_omits_a_queue_once_it_is_fully_acked(record_store_factory):
    store = record_store_factory
    store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': 0})
    assert (PLAN, INSTANCE) in store.list_outboxes(DEFAULT_NAMESPACE)
    entry = store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)[0]
    store.ack_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, [entry['id']])
    assert (PLAN, INSTANCE) not in store.list_outboxes(DEFAULT_NAMESPACE)


@covers('list_outboxes')
def test_outbox_list_outboxes_of_an_empty_namespace_is_empty(record_store_factory):
    store = record_store_factory
    assert store.list_outboxes(DEFAULT_NAMESPACE) == []


# -- limits ----------------------------------------------------------------------


@covers('append_outbox', 'read_outbox')
def test_outbox_a_limit_of_zero_returns_no_entries(record_store_factory):
    store = record_store_factory
    store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': 0})
    assert store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, limit=0) == []


@covers('append_outbox', 'read_outbox')
def test_outbox_a_limit_of_one_returns_the_oldest_entry(record_store_factory):
    store = record_store_factory
    store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': 0})
    store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': 1})
    entries = store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, limit=1)
    assert [entry['record']['n'] for entry in entries] == [0]


@covers('append_outbox', 'read_outbox')
def test_outbox_a_limit_larger_than_the_queue_returns_every_entry(record_store_factory):
    store = record_store_factory
    store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': 0})
    assert len(store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, limit=10)) == 1


# -- summary ---------------------------------------------------------------------


@covers('outbox_summary')
def test_outbox_summary_of_an_empty_namespace_counts_nothing(record_store_factory):
    store = record_store_factory
    summary = store.outbox_summary(DEFAULT_NAMESPACE)
    assert summary['files'] == 0
    assert summary['records'] == 0
    assert summary['oldest_age_s'] is None


@covers('append_outbox', 'ack_outbox', 'read_outbox', 'outbox_summary')
def test_outbox_summary_counts_queues_and_records_for_one_plan_or_all(record_store_factory):
    store = record_store_factory
    store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': 0})
    store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'n': 1})
    store.append_outbox(DEFAULT_NAMESPACE, PLAN, LEGACY, {'n': 2})
    store.append_outbox(DEFAULT_NAMESPACE, OTHER_PLAN, INSTANCE, {'n': 3})
    everything = store.outbox_summary(DEFAULT_NAMESPACE)
    assert (everything['files'], everything['records']) == (3, 4)
    one_plan = store.outbox_summary(DEFAULT_NAMESPACE, PLAN)
    assert (one_plan['files'], one_plan['records']) == (2, 3)
    acked = store.read_outbox(DEFAULT_NAMESPACE, OTHER_PLAN, INSTANCE)[0]
    store.ack_outbox(DEFAULT_NAMESPACE, OTHER_PLAN, INSTANCE, [acked['id']])
    after = store.outbox_summary(DEFAULT_NAMESPACE)
    assert (after['files'], after['records']) == (2, 3)


@covers('append_outbox', 'outbox_summary')
def test_outbox_summary_reports_the_age_of_the_oldest_record(record_store_factory):
    store = record_store_factory
    store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'ts': OLD_TS})
    age = store.outbox_summary(DEFAULT_NAMESPACE)['oldest_age_s']
    assert isinstance(age, (int, float)) and not isinstance(age, bool)
    assert age > 0


# -- rejected input --------------------------------------------------------------


@covers('append_outbox', 'read_outbox', 'ack_outbox', 'list_outboxes')
def test_outbox_an_invalid_channel_is_refused_and_writes_nothing(record_store_factory):
    store = record_store_factory
    for channel in INVALID_CHANNELS:
        _expect(
            InvalidRecordKeyError,
            lambda channel=channel: store.append_outbox(
                DEFAULT_NAMESPACE, PLAN, channel, {'n': 0}
            ),
        )
        _expect(
            InvalidRecordKeyError,
            lambda channel=channel: store.read_outbox(DEFAULT_NAMESPACE, PLAN, channel),
        )
        _expect(
            InvalidRecordKeyError,
            lambda channel=channel: store.ack_outbox(
                DEFAULT_NAMESPACE, PLAN, channel, ['x']
            ),
        )
    assert store.list_outboxes(DEFAULT_NAMESPACE) == []


@covers('append_outbox', 'read_outbox')
def test_outbox_a_non_dict_record_raises_type_error_and_writes_nothing(record_store_factory):
    store = record_store_factory
    _expect(
        TypeError,
        lambda: store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, 'nope'),
    )
    _expect(
        TypeError,
        lambda: store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, ['nope']),
    )
    assert store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE) == []


@covers('append_outbox', 'read_outbox', 'list_outboxes')
def test_outbox_a_record_that_is_not_json_serialisable_raises_and_writes_nothing(
    record_store_factory,
):
    store = record_store_factory
    _expect(
        (TypeError, ValueError),
        lambda: store.append_outbox(
            DEFAULT_NAMESPACE, PLAN, INSTANCE, _circular_record()
        ),
    )
    assert store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE) == []
    assert store.list_outboxes(DEFAULT_NAMESPACE) == []


@covers('append_outbox', 'read_outbox', 'ack_outbox', 'list_outboxes', 'outbox_summary')
def test_outbox_an_invalid_namespace_is_refused(record_store_factory):
    store = record_store_factory
    _expect(
        InvalidRecordKeyError,
        lambda: store.append_outbox(INVALID_NAMESPACE, PLAN, INSTANCE, {'n': 0}),
    )
    _expect(
        InvalidRecordKeyError,
        lambda: store.read_outbox(INVALID_NAMESPACE, PLAN, INSTANCE),
    )
    _expect(
        InvalidRecordKeyError,
        lambda: store.ack_outbox(INVALID_NAMESPACE, PLAN, INSTANCE, ['x']),
    )
    _expect(InvalidRecordKeyError, lambda: store.list_outboxes(INVALID_NAMESPACE))
    _expect(InvalidRecordKeyError, lambda: store.outbox_summary(INVALID_NAMESPACE))


@covers('append_outbox', 'read_outbox', 'ack_outbox')
def test_outbox_an_invalid_plan_name_is_refused(record_store_factory):
    store = record_store_factory
    _expect(
        InvalidRecordKeyError,
        lambda: store.append_outbox(DEFAULT_NAMESPACE, INVALID_PLAN, INSTANCE, {'n': 0}),
    )
    _expect(
        InvalidRecordKeyError,
        lambda: store.read_outbox(DEFAULT_NAMESPACE, INVALID_PLAN, INSTANCE),
    )
    _expect(
        InvalidRecordKeyError,
        lambda: store.ack_outbox(DEFAULT_NAMESPACE, INVALID_PLAN, INSTANCE, ['x']),
    )


# -- namespace policy ------------------------------------------------------------


@covers('append_outbox', 'read_outbox', 'ack_outbox', 'list_outboxes', 'outbox_summary')
def test_outbox_a_store_serving_only_default_refuses_another_namespace(
    record_store_factory, request
):
    if _serves_other_namespaces(request):
        pytest.skip('the store serves other namespaces')
    store = record_store_factory
    for call in (
        lambda: store.append_outbox(OTHER_NAMESPACE, PLAN, INSTANCE, {'n': 0}),
        lambda: store.read_outbox(OTHER_NAMESPACE, PLAN, INSTANCE),
        lambda: store.ack_outbox(OTHER_NAMESPACE, PLAN, INSTANCE, ['x']),
        lambda: store.list_outboxes(OTHER_NAMESPACE),
        lambda: store.outbox_summary(OTHER_NAMESPACE),
    ):
        _expect(UnknownNamespaceError, call)
    assert store.list_outboxes(DEFAULT_NAMESPACE) == []


@covers('append_outbox', 'read_outbox', 'list_outboxes')
def test_outbox_a_store_serving_other_namespaces_isolates_them(record_store_factory, request):
    if not _serves_other_namespaces(request):
        pytest.skip('the store serves only the default namespace')
    store = record_store_factory
    store.append_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE, {'ns': 'default'})
    store.append_outbox(OTHER_NAMESPACE, PLAN, INSTANCE, {'ns': 'acme'})
    default_entries = store.read_outbox(DEFAULT_NAMESPACE, PLAN, INSTANCE)
    acme_entries = store.read_outbox(OTHER_NAMESPACE, PLAN, INSTANCE)
    assert [entry['record']['ns'] for entry in default_entries] == ['default']
    assert [entry['record']['ns'] for entry in acme_entries] == ['acme']
    assert store.list_outboxes(DEFAULT_NAMESPACE) == [(PLAN, INSTANCE)]
    assert store.list_outboxes(OTHER_NAMESPACE) == [(PLAN, INSTANCE)]


# -- completeness ----------------------------------------------------------------


def test_outbox_every_protocol_method_is_covered():
    """Every outbox method the mixins declare is exercised by a test here."""
    assert sorted(set(OUTBOX_METHODS) - COVERED) == []


def test_outbox_every_covered_name_is_an_outbox_method():
    """A declaration naming anything else is a typo, not coverage."""
    assert sorted(COVERED - set(OUTBOX_METHODS)) == []
