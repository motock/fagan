"""The published record-store contract suite (ADR 0012, decision 7).

An out-of-tree store package (for example a Postgres store) runs this suite as
its conformance gate. The package's conftest supplies the ``record_store_factory``
fixture, and a test module collects the suite with
``from pipeline.testing.store_conformance import *``; see
docs/guides/STORE_PLUGINS.md. Every test is written against the ``RecordStore``
Protocol in ``pipeline/store_records.py``, never against a path or a backend.

Namespace policy differs by store and by record kind. A store declares the kinds
it serves in a foreign namespace through ``record_store_serves_other``; every
other kind must refuse it with ``UnknownNamespaceError``. Either way, a foreign
namespace must never see or change the default namespace's records, and a
malformed namespace is refused with ``InvalidRecordKeyError`` before anything is
stored.

Each test declares the protocol names it exercises with ``@covers``;
``test_every_protocol_method_is_covered_by_a_contract_test`` asserts that the
declarations cover every name in ``RECORD_STORE_METHODS``, and
``test_every_covered_name_is_a_protocol_method`` asserts that no declaration
names anything the Protocol lacks.

This module must stay importable without the repo's ``tests/`` tree and without
loading ``pipeline.server``.
"""

import hashlib
import threading
from collections import namedtuple

import pytest

from pipeline.store_memory import InMemoryRecordStore
from pipeline.store_records import (
    DEFAULT_NAMESPACE,
    RECORD_STORE_METHODS,
    InvalidRecordKeyError,
    InvalidUsageRecordError,
    UnknownNamespaceError,
)

PLAN = 'contract-plan'
OTHER_PLAN = 'other-plan'
# A prefix of PLAN's sibling name: 'contract-plan.x' must never leak into PLAN.
LONGER_PLAN = 'contract-plan.x'
STORY = 'S-1'
OTHER_STORY = 'S-2'
FOREIGN = 'foreign'
INVALID_NAMESPACE = '../escape'

M1 = {'schema_version': 1, 'stories': {'S1': {'status': 'pending', 'title': 'T'}}}
M2 = {'schema_version': 1, 'stories': {}}
DECISION = {'decision': 'approve', 'by': 'operator'}
OTHER_DECISION = {'decision': 'reject', 'by': 'operator'}
JOURNAL_RECORD = {'ts': '2026-11-10T09:00:00Z', 'event': 'dispatched'}
OTHER_JOURNAL_RECORD = {'ts': '2026-11-10T09:05:00Z', 'event': 'merged'}
USAGE = {
    'ts': '2026-11-05T10:12:33.412Z',
    'plan': PLAN,
    'story_key': 'STORY-42',
    'attempt': 2,
    'correlation_id': '0f9c4b7e',
    'role': 'dispatch',
    'backend': 'claude',
    'model': 'claude-sonnet-4-5',
    'served_model': 'claude-sonnet-4-5-20250929',
    'input_tokens': 1200,
    'output_tokens': 350,
    'cache_creation_input_tokens': 0,
    'cache_read_input_tokens': 800,
    'cost_usd': 0.0042,
    'duration_ms': 2340,
}
OTHER_USAGE = dict(USAGE, input_tokens=5)
NOTIFICATION = {'ts': '2026-11-10T09:01:00Z', 'message': 'plan ready'}
OTHER_NOTIFICATION = {'ts': '2026-11-10T09:02:00Z', 'message': 'plan merged'}
DRAFT = {'epics': [{'summary': 'e', 'stories': [{'key': 'S-1'}]}]}
OTHER_DRAFT = {'epics': []}
REPORT = 'all stories merged'
OTHER_REPORT = 'retro pending'
CONTENT = 'context body'
OTHER_CONTENT = 'other context body'
META = {'source': 'unit'}


def digest_of(content):
    """The content address the store requires: ``sha256:`` + hex of the body."""
    return 'sha256:' + hashlib.sha256(content.encode('utf-8')).hexdigest()


# -- the coverage registry ----------------------------------------------------

#: Protocol names declared exercised by at least one test in this suite.
COVERED = set()


def covers(*names):
    """Declare that the decorated test exercises the named Protocol methods."""

    def mark(test):
        COVERED.update(names)
        return test

    return mark


# -- backends -----------------------------------------------------------------

Backend = namedtuple('Backend', 'store serves_other known_gaps')


# -- the fixture contract -------------------------------------------------------
#
# An external store package supplies ``record_store_factory`` in its conftest: a
# fixture whose value is a fresh, empty RecordStore for namespace 'default'. Two
# optional fixtures describe the store beyond that: ``record_store_serves_other``
# and ``record_store_known_gaps``. They are looked up by name, never defined here,
# so a conftest override is never shadowed by a default that a test module
# star-imports. Absent, they take the strictest reading of ADR 0012 decision 2:
# a foreign namespace is refused and no defect is excused.

_SERVES_OTHER_DEFAULT = frozenset()
_KNOWN_GAPS_DEFAULT = {}


def _declared(request, name, default):
    """Return the value of the optional fixture ``name``, or ``default``."""
    try:
        return request.getfixturevalue(name)
    except pytest.FixtureLookupError:
        return default


@pytest.fixture
def backend(request, record_store_factory):
    """The store under test with the policy it declares."""
    return Backend(
        record_store_factory,
        _declared(request, 'record_store_serves_other', _SERVES_OTHER_DEFAULT),
        _declared(request, 'record_store_known_gaps', _KNOWN_GAPS_DEFAULT),
    )


# -- record kinds --------------------------------------------------------------
#
# One row per record kind: ``write(store, ns, value)`` stores ``value``,
# ``read(store, ns)`` returns what the kind holds for ``PLAN`` (or the snapshot
# digest), ``empty`` is what an unwritten plan reads as, and ``first``/``second``
# are two distinct values so a leaked write is visible in the read.

Kind = namedtuple('Kind', 'name write read empty first second')

KINDS = (
    Kind(
        'manifest',
        lambda s, ns, v: s.put_manifest(ns, PLAN, v),
        lambda s, ns: s.get_manifest(ns, PLAN),
        None, M1, M2,
    ),
    Kind(
        'decisions',
        lambda s, ns, v: s.append_decision(ns, PLAN, v),
        lambda s, ns: s.get_decisions(ns, PLAN),
        [], DECISION, OTHER_DECISION,
    ),
    Kind(
        'journal',
        lambda s, ns, v: s.append_journal(ns, PLAN, STORY, v),
        lambda s, ns: s.get_journal(ns, PLAN, STORY),
        [], JOURNAL_RECORD, OTHER_JOURNAL_RECORD,
    ),
    Kind(
        'usage',
        lambda s, ns, v: s.append_usage(ns, PLAN, v),
        lambda s, ns: s.get_usage(ns, PLAN),
        [], USAGE, OTHER_USAGE,
    ),
    Kind(
        'notification',
        lambda s, ns, v: s.append_notification(ns, PLAN, v),
        lambda s, ns: s.get_notification_history(ns, PLAN),
        ([], 0), NOTIFICATION, OTHER_NOTIFICATION,
    ),
    Kind(
        'plan_draft',
        lambda s, ns, v: s.put_plan_draft(ns, PLAN, v),
        lambda s, ns: s.get_plan_draft(ns, PLAN),
        None, DRAFT, OTHER_DRAFT,
    ),
    Kind(
        'completion',
        lambda s, ns, v: s.mark_plan_completed(ns, PLAN),
        lambda s, ns: s.is_plan_completed(ns, PLAN),
        False, None, None,
    ),
    Kind(
        'plan_report',
        lambda s, ns, v: s.put_plan_report(ns, PLAN, v),
        lambda s, ns: s.get_plan_report(ns, PLAN),
        None, REPORT, OTHER_REPORT,
    ),
    Kind(
        'context_snapshot',
        lambda s, ns, v: s.put_context_snapshot(ns, digest_of(v), v, META),
        lambda s, ns: s.get_context_snapshot(ns, digest_of(CONTENT)),
        None, CONTENT, OTHER_CONTENT,
    ),
)
KIND_IDS = [kind.name for kind in KINDS]


# -- namespace checks (shared with the pytester run below) ----------------------


def check_foreign_read_is_contained(store, serves_other, kind):
    """A foreign namespace reads nothing written to the default namespace."""
    kind.write(store, DEFAULT_NAMESPACE, kind.first)
    if serves_other:
        assert kind.read(store, FOREIGN) == kind.empty
    else:
        with pytest.raises(UnknownNamespaceError):
            kind.read(store, FOREIGN)


def check_foreign_write_is_contained(store, serves_other, kind):
    """A foreign write never reaches the default namespace."""
    if serves_other:
        kind.write(store, FOREIGN, kind.first)
    else:
        with pytest.raises(UnknownNamespaceError):
            kind.write(store, FOREIGN, kind.first)
    assert kind.read(store, DEFAULT_NAMESPACE) == kind.empty


def check_foreign_write_leaves_default_unchanged(store, serves_other, kind):
    """A foreign write over an existing default record changes nothing."""
    kind.write(store, DEFAULT_NAMESPACE, kind.first)
    before = kind.read(store, DEFAULT_NAMESPACE)
    if serves_other:
        kind.write(store, FOREIGN, kind.second)
    else:
        with pytest.raises(UnknownNamespaceError):
            kind.write(store, FOREIGN, kind.second)
    assert kind.read(store, DEFAULT_NAMESPACE) == before


def check_invalid_namespace_is_refused(store, kind):
    """A malformed namespace is refused on write and read, storing nothing."""
    with pytest.raises(InvalidRecordKeyError):
        kind.write(store, INVALID_NAMESPACE, kind.first)
    with pytest.raises(InvalidRecordKeyError):
        kind.read(store, INVALID_NAMESPACE)
    assert kind.read(store, DEFAULT_NAMESPACE) == kind.empty


# -- manifests -----------------------------------------------------------------


@covers('get_manifest', 'put_manifest')
def test_manifest_round_trips_and_reading_twice_changes_nothing(backend):
    backend.store.put_manifest(DEFAULT_NAMESPACE, PLAN, M1)

    first = backend.store.get_manifest(DEFAULT_NAMESPACE, PLAN)
    second = backend.store.get_manifest(DEFAULT_NAMESPACE, PLAN)

    assert first == M1
    assert second == first


@covers('get_manifest', 'put_manifest')
def test_manifest_put_replaces_the_whole_document(backend):
    backend.store.put_manifest(DEFAULT_NAMESPACE, PLAN, M1)

    backend.store.put_manifest(DEFAULT_NAMESPACE, PLAN, M2)

    assert backend.store.get_manifest(DEFAULT_NAMESPACE, PLAN) == M2


@covers('get_manifest')
def test_missing_manifest_reads_as_none(backend):
    assert backend.store.get_manifest(DEFAULT_NAMESPACE, PLAN) is None


@covers('get_manifest', 'put_manifest')
def test_get_manifest_returns_an_independent_copy(backend):
    backend.store.put_manifest(DEFAULT_NAMESPACE, PLAN, M1)
    got = backend.store.get_manifest(DEFAULT_NAMESPACE, PLAN)

    got['stories'].clear()

    assert backend.store.get_manifest(DEFAULT_NAMESPACE, PLAN) == M1


@covers('get_manifests', 'list_plans', 'put_manifest')
def test_manifests_are_listed_by_plan_name_in_sorted_order(backend):
    backend.store.put_manifest(DEFAULT_NAMESPACE, OTHER_PLAN, M2)
    backend.store.put_manifest(DEFAULT_NAMESPACE, PLAN, M1)

    assert backend.store.list_plans(DEFAULT_NAMESPACE) == sorted([PLAN, OTHER_PLAN])
    manifests = backend.store.get_manifests(DEFAULT_NAMESPACE)
    assert manifests == {PLAN: M1, OTHER_PLAN: M2}
    assert list(manifests) == sorted(manifests)


@covers('get_manifests', 'list_plans')
def test_an_empty_namespace_has_no_manifests(backend):
    assert backend.store.get_manifests(DEFAULT_NAMESPACE) == {}
    assert backend.store.list_plans(DEFAULT_NAMESPACE) == []


@covers('put_manifest', 'get_manifest')
@pytest.mark.parametrize('plan_name', ['../escape', 'host', ''])
def test_malformed_or_reserved_plan_name_is_refused(backend, plan_name):
    with pytest.raises(InvalidRecordKeyError):
        backend.store.put_manifest(DEFAULT_NAMESPACE, plan_name, M1)

    assert backend.store.list_plans(DEFAULT_NAMESPACE) == []


# -- decisions and journals ----------------------------------------------------


@covers('append_decision', 'get_decisions')
def test_decisions_are_kept_in_append_order(backend):
    backend.store.append_decision(DEFAULT_NAMESPACE, PLAN, DECISION)
    backend.store.append_decision(DEFAULT_NAMESPACE, PLAN, OTHER_DECISION)

    assert backend.store.get_decisions(DEFAULT_NAMESPACE, PLAN) == [
        DECISION,
        OTHER_DECISION,
    ]


@covers('append_decision', 'get_decisions')
def test_decisions_are_per_plan(backend):
    backend.store.append_decision(DEFAULT_NAMESPACE, PLAN, DECISION)

    assert backend.store.get_decisions(DEFAULT_NAMESPACE, OTHER_PLAN) == []


@covers('append_journal', 'get_journal')
def test_journal_keeps_append_order_for_one_story(backend):
    backend.store.append_journal(DEFAULT_NAMESPACE, PLAN, STORY, JOURNAL_RECORD)
    backend.store.append_journal(DEFAULT_NAMESPACE, PLAN, STORY, OTHER_JOURNAL_RECORD)

    assert backend.store.get_journal(DEFAULT_NAMESPACE, PLAN, STORY) == [
        JOURNAL_RECORD,
        OTHER_JOURNAL_RECORD,
    ]
    assert backend.store.get_journal(DEFAULT_NAMESPACE, PLAN, OTHER_STORY) == []


@covers('append_journal', 'clear_journal', 'get_journal')
def test_clear_journal_empties_only_that_story(backend):
    backend.store.append_journal(DEFAULT_NAMESPACE, PLAN, STORY, JOURNAL_RECORD)
    backend.store.append_journal(DEFAULT_NAMESPACE, PLAN, OTHER_STORY, JOURNAL_RECORD)

    backend.store.clear_journal(DEFAULT_NAMESPACE, PLAN, STORY)

    assert backend.store.get_journal(DEFAULT_NAMESPACE, PLAN, STORY) == []
    assert backend.store.get_journal(DEFAULT_NAMESPACE, PLAN, OTHER_STORY) == [
        JOURNAL_RECORD
    ]


@covers('get_journal_updated_at', 'append_journal', 'clear_journal')
def test_journal_activity_is_none_until_appended_and_after_clear(backend):
    store = backend.store
    assert store.get_journal_updated_at(DEFAULT_NAMESPACE, PLAN, STORY) is None

    store.append_journal(DEFAULT_NAMESPACE, PLAN, STORY, JOURNAL_RECORD)
    assert isinstance(store.get_journal_updated_at(DEFAULT_NAMESPACE, PLAN, STORY), float)

    store.clear_journal(DEFAULT_NAMESPACE, PLAN, STORY)
    assert store.get_journal_updated_at(DEFAULT_NAMESPACE, PLAN, STORY) is None


@covers('get_journal_updated_at', 'append_journal')
def test_journal_activity_counts_records_that_carry_no_timestamp(backend):
    backend.store.append_journal(DEFAULT_NAMESPACE, PLAN, STORY, {'event': 'bare'})

    assert backend.store.get_journal_updated_at(DEFAULT_NAMESPACE, PLAN, STORY) is not None


@covers('get_journal_updated_at', 'append_journal')
def test_journal_activity_never_decreases_across_appends(backend):
    store = backend.store
    store.append_journal(DEFAULT_NAMESPACE, PLAN, STORY, JOURNAL_RECORD)
    first = store.get_journal_updated_at(DEFAULT_NAMESPACE, PLAN, STORY)

    store.append_journal(DEFAULT_NAMESPACE, PLAN, STORY, OTHER_JOURNAL_RECORD)

    assert store.get_journal_updated_at(DEFAULT_NAMESPACE, PLAN, STORY) >= first


@covers('list_journal_keys', 'append_journal', 'clear_journal')
def test_list_journal_keys_is_sorted_and_skips_cleared_stories(backend):
    store = backend.store
    store.append_journal(DEFAULT_NAMESPACE, PLAN, OTHER_STORY, JOURNAL_RECORD)
    store.append_journal(DEFAULT_NAMESPACE, PLAN, STORY, JOURNAL_RECORD)
    store.clear_journal(DEFAULT_NAMESPACE, PLAN, OTHER_STORY)

    assert store.list_journal_keys(DEFAULT_NAMESPACE, PLAN) == [STORY]
    assert store.list_journal_keys(DEFAULT_NAMESPACE, OTHER_PLAN) == []


@covers('list_journal_keys', 'append_journal', 'get_journal')
def test_a_plan_name_prefix_does_not_leak_another_plans_journals(backend):
    backend.store.append_journal(DEFAULT_NAMESPACE, LONGER_PLAN, STORY, JOURNAL_RECORD)

    assert backend.store.list_journal_keys(DEFAULT_NAMESPACE, PLAN) == []
    assert backend.store.get_journal(DEFAULT_NAMESPACE, PLAN, STORY) == []


# -- usage and notification history ---------------------------------------------


@covers('append_usage', 'get_usage')
def test_usage_records_are_returned_in_write_order(backend):
    backend.store.append_usage(DEFAULT_NAMESPACE, PLAN, USAGE)
    backend.store.append_usage(DEFAULT_NAMESPACE, PLAN, OTHER_USAGE)

    assert backend.store.get_usage(DEFAULT_NAMESPACE, PLAN) == [USAGE, OTHER_USAGE]
    assert backend.store.get_usage(DEFAULT_NAMESPACE, OTHER_PLAN) == []


@covers('append_usage', 'get_usage')
def test_usage_record_with_an_extra_field_is_refused_and_nothing_is_written(backend):
    with pytest.raises(InvalidUsageRecordError):
        backend.store.append_usage(DEFAULT_NAMESPACE, PLAN, dict(USAGE, extra=1))

    assert backend.store.get_usage(DEFAULT_NAMESPACE, PLAN) == []


@covers('append_notification', 'get_notification_history')
def test_notification_history_keeps_written_records_and_counts_no_malformed(backend):
    backend.store.append_notification(DEFAULT_NAMESPACE, PLAN, NOTIFICATION)

    first = backend.store.get_notification_history(DEFAULT_NAMESPACE, PLAN)
    second = backend.store.get_notification_history(DEFAULT_NAMESPACE, PLAN)

    assert first == ([NOTIFICATION], 0)
    assert second == first, 'reading the history must not consume it'


@covers('append_notification', 'get_notification_lines')
def test_notification_lines_render_timestamp_and_message(backend):
    backend.store.append_notification(DEFAULT_NAMESPACE, PLAN, NOTIFICATION)

    assert backend.store.get_notification_lines(DEFAULT_NAMESPACE, PLAN) == [
        f"{NOTIFICATION['ts']} {NOTIFICATION['message']}"
    ]


@covers('append_notification')
@pytest.mark.parametrize(
    'record',
    [
        'not a dict',
        {'message': 'no timestamp'},
        {'ts': '2026-11-10T09:00:00Z', 'message': 42},
    ],
)
def test_malformed_notification_is_refused_before_anything_is_written(backend, record):
    with pytest.raises(TypeError):
        backend.store.append_notification(DEFAULT_NAMESPACE, PLAN, record)

    assert backend.store.get_notification_history(DEFAULT_NAMESPACE, PLAN) == ([], 0)


# -- plan drafts ---------------------------------------------------------------


@covers('put_plan_draft', 'get_plan_draft')
def test_plan_draft_round_trips_and_a_missing_draft_is_none(backend):
    assert backend.store.get_plan_draft(DEFAULT_NAMESPACE, PLAN) is None

    backend.store.put_plan_draft(DEFAULT_NAMESPACE, PLAN, DRAFT)

    assert backend.store.get_plan_draft(DEFAULT_NAMESPACE, PLAN) == DRAFT


@covers('put_plan_draft', 'get_plan_draft')
def test_plan_draft_replaces_the_previous_draft_wholesale(backend):
    backend.store.put_plan_draft(DEFAULT_NAMESPACE, PLAN, dict(DRAFT, extra='x'))

    backend.store.put_plan_draft(DEFAULT_NAMESPACE, PLAN, DRAFT)

    got = backend.store.get_plan_draft(DEFAULT_NAMESPACE, PLAN)
    assert got == DRAFT
    assert 'extra' not in got


@covers('put_plan_draft', 'delete_plan_draft', 'get_plan_draft')
def test_delete_plan_draft_reports_whether_one_was_deleted(backend):
    backend.store.put_plan_draft(DEFAULT_NAMESPACE, PLAN, DRAFT)

    assert backend.store.delete_plan_draft(DEFAULT_NAMESPACE, PLAN) is True
    assert backend.store.delete_plan_draft(DEFAULT_NAMESPACE, PLAN) is False
    assert backend.store.get_plan_draft(DEFAULT_NAMESPACE, PLAN) is None


@covers('put_plan_draft', 'list_plan_drafts')
def test_only_drafts_with_epics_are_listed(backend, request):
    gap = backend.known_gaps.get('draft_epics')
    if gap:
        request.applymarker(pytest.mark.xfail(strict=True, reason=gap))
    backend.store.put_plan_draft(DEFAULT_NAMESPACE, PLAN, DRAFT)
    backend.store.put_plan_draft(DEFAULT_NAMESPACE, OTHER_PLAN, {'name': 'no epics'})

    assert backend.store.list_plan_drafts(DEFAULT_NAMESPACE) == [PLAN]


# -- completion, reports and context snapshots ----------------------------------


@covers('mark_plan_completed', 'is_plan_completed')
def test_completion_marker_is_created_exactly_once(backend):
    store = backend.store
    assert store.is_plan_completed(DEFAULT_NAMESPACE, PLAN) is False

    assert store.mark_plan_completed(DEFAULT_NAMESPACE, PLAN) is True
    assert store.mark_plan_completed(DEFAULT_NAMESPACE, PLAN) is False
    assert store.is_plan_completed(DEFAULT_NAMESPACE, PLAN) is True


@covers('put_plan_report', 'get_plan_report')
def test_plan_report_round_trips_and_replaces_wholesale(backend):
    store = backend.store
    assert store.get_plan_report(DEFAULT_NAMESPACE, PLAN) is None

    store.put_plan_report(DEFAULT_NAMESPACE, PLAN, REPORT)
    store.put_plan_report(DEFAULT_NAMESPACE, PLAN, OTHER_REPORT)

    assert store.get_plan_report(DEFAULT_NAMESPACE, PLAN) == OTHER_REPORT


@covers('put_plan_report', 'get_plan_report')
def test_a_non_string_plan_report_is_refused_and_nothing_is_written(backend):
    with pytest.raises(TypeError):
        backend.store.put_plan_report(DEFAULT_NAMESPACE, PLAN, {'not': 'text'})

    assert backend.store.get_plan_report(DEFAULT_NAMESPACE, PLAN) is None


@covers('put_context_snapshot', 'get_context_snapshot')
def test_context_snapshot_round_trips_under_its_digest(backend):
    store = backend.store
    assert store.get_context_snapshot(DEFAULT_NAMESPACE, digest_of(CONTENT)) is None

    store.put_context_snapshot(DEFAULT_NAMESPACE, digest_of(CONTENT), CONTENT, META)
    got = store.get_context_snapshot(DEFAULT_NAMESPACE, digest_of(CONTENT))

    assert got['content'] == CONTENT
    # The store may add its own keys (digest, size) to the meta it keeps, so
    # the caller's keys must survive rather than the whole dict matching.
    assert META.items() <= got['meta'].items()


@covers('put_context_snapshot', 'get_context_snapshot')
def test_second_put_of_the_same_digest_never_overwrites(backend):
    store = backend.store
    store.put_context_snapshot(DEFAULT_NAMESPACE, digest_of(CONTENT), CONTENT, META)

    store.put_context_snapshot(
        DEFAULT_NAMESPACE, digest_of(CONTENT), CONTENT, {'source': 'other'}
    )

    got = store.get_context_snapshot(DEFAULT_NAMESPACE, digest_of(CONTENT))
    assert got['meta']['source'] == META['source']


@covers('put_context_snapshot')
def test_a_digest_that_does_not_address_its_content_is_refused(backend):
    with pytest.raises(ValueError):
        backend.store.put_context_snapshot(
            DEFAULT_NAMESPACE, digest_of(OTHER_CONTENT), CONTENT, META
        )

    assert backend.store.get_context_snapshot(DEFAULT_NAMESPACE, digest_of(CONTENT)) is None


@covers('put_context_snapshot')
@pytest.mark.parametrize('digest', ['abc', 'sha256:' + 'G' * 64])
def test_a_malformed_digest_is_refused_on_put(backend, digest):
    with pytest.raises(InvalidRecordKeyError):
        backend.store.put_context_snapshot(DEFAULT_NAMESPACE, digest, CONTENT, META)


@covers('get_context_snapshot')
@pytest.mark.parametrize('digest', ['abc', 'sha256:' + 'G' * 64])
def test_a_malformed_digest_is_refused_on_get(backend, request, digest):
    gap = backend.known_gaps.get('malformed_get')
    if gap:
        request.applymarker(pytest.mark.xfail(strict=True, reason=gap))
    with pytest.raises(InvalidRecordKeyError):
        backend.store.get_context_snapshot(DEFAULT_NAMESPACE, digest)


# -- namespace rules -------------------------------------------------------------


@covers('list_plans', 'get_manifests', 'put_manifest')
@pytest.mark.parametrize('namespace', ['', 'Default', '../escape', 'a' * 65])
def test_a_malformed_namespace_is_refused_before_storage(backend, namespace):
    with pytest.raises(InvalidRecordKeyError):
        backend.store.put_manifest(namespace, PLAN, M1)
    with pytest.raises(InvalidRecordKeyError):
        backend.store.list_plans(namespace)

    assert backend.store.list_plans(DEFAULT_NAMESPACE) == []


@pytest.mark.parametrize('kind', KINDS, ids=KIND_IDS)
@covers(
    'put_manifest', 'get_manifest', 'append_decision', 'get_decisions',
    'append_journal', 'get_journal', 'append_usage', 'get_usage',
    'append_notification', 'get_notification_history', 'put_plan_draft',
    'get_plan_draft', 'mark_plan_completed', 'is_plan_completed',
    'put_plan_report', 'get_plan_report', 'put_context_snapshot',
    'get_context_snapshot',
)
def test_a_malformed_namespace_is_refused_for_every_record_kind(backend, kind):
    check_invalid_namespace_is_refused(backend.store, kind)


@pytest.mark.parametrize('kind', KINDS, ids=KIND_IDS)
@covers('get_manifest', 'get_decisions', 'get_journal', 'get_usage',
        'get_notification_history', 'get_plan_draft', 'is_plan_completed',
        'get_plan_report', 'get_context_snapshot')
def test_a_foreign_namespace_reads_no_default_records(backend, kind):
    check_foreign_read_is_contained(backend.store, kind.name in backend.serves_other, kind)


@pytest.mark.parametrize('kind', KINDS, ids=KIND_IDS)
@covers('put_manifest', 'append_decision', 'append_journal', 'append_usage',
        'append_notification', 'put_plan_draft', 'mark_plan_completed',
        'put_plan_report', 'put_context_snapshot')
def test_a_foreign_namespace_write_never_reaches_the_default_namespace(backend, kind):
    check_foreign_write_is_contained(backend.store, kind.name in backend.serves_other, kind)


@pytest.mark.parametrize('kind', KINDS, ids=KIND_IDS)
def test_a_foreign_write_leaves_default_records_unchanged(backend, kind):
    check_foreign_write_leaves_default_unchanged(
        backend.store, kind.name in backend.serves_other, kind
    )


#: Each lister follows the policy of the record kind it lists.
LISTERS = (
    ('manifest', lambda s, ns: s.list_plans(ns), [PLAN]),
    ('journal', lambda s, ns: s.list_journal_keys(ns, PLAN), [STORY]),
    ('plan_draft', lambda s, ns: s.list_plan_drafts(ns), [PLAN]),
)


@pytest.mark.parametrize('kind_name, lister, expected', LISTERS, ids=[row[0] for row in LISTERS])
@covers('list_plans', 'list_journal_keys', 'list_plan_drafts')
def test_a_foreign_namespace_lists_nothing_of_the_default_namespace(
    backend, kind_name, lister, expected
):
    store = backend.store
    store.put_manifest(DEFAULT_NAMESPACE, PLAN, M1)
    store.append_journal(DEFAULT_NAMESPACE, PLAN, STORY, JOURNAL_RECORD)
    store.put_plan_draft(DEFAULT_NAMESPACE, PLAN, DRAFT)

    assert lister(store, DEFAULT_NAMESPACE) == expected
    if kind_name in backend.serves_other:
        assert lister(store, FOREIGN) == []
    else:
        with pytest.raises(UnknownNamespaceError):
            lister(store, FOREIGN)


# -- transactions ----------------------------------------------------------------


@covers('transaction')
def test_uncontended_transaction_yields_true_and_runs_the_block(backend):
    with backend.store.transaction(DEFAULT_NAMESPACE, PLAN) as held:
        assert held is True


@covers('transaction')
def test_transaction_is_reentrant_on_the_holding_thread(backend):
    with backend.store.transaction(DEFAULT_NAMESPACE, PLAN) as outer, \
            backend.store.transaction(DEFAULT_NAMESPACE, PLAN) as inner:
        assert outer is True
        assert inner is True


@covers('transaction')
def test_a_contended_transaction_is_refused_without_blocking(backend):
    acquired = threading.Event()
    release = threading.Event()

    def holder():
        with backend.store.transaction(DEFAULT_NAMESPACE, PLAN) as held:
            assert held is True
            acquired.set()
            release.wait(5)

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert acquired.wait(5)
        with backend.store.transaction(DEFAULT_NAMESPACE, PLAN) as held:
            assert held is False
    finally:
        release.set()
        thread.join(5)


@covers('transaction')
def test_the_lock_is_released_when_the_block_raises(backend):
    with pytest.raises(RuntimeError), backend.store.transaction(DEFAULT_NAMESPACE, PLAN):
        raise RuntimeError('block failed')

    with backend.store.transaction(DEFAULT_NAMESPACE, PLAN) as held:
        assert held is True


@covers('transaction')
def test_a_malformed_namespace_is_refused_before_any_lock_is_taken(backend):
    with pytest.raises(InvalidRecordKeyError), backend.store.transaction(INVALID_NAMESPACE, PLAN):
        pass  # pragma: no cover - the block must never run


@covers('transaction')
def test_a_foreign_namespace_transaction_follows_the_backend_policy(backend):
    # Transactions follow the manifest policy: the lock is per plan, not per kind.
    if 'manifest' not in backend.serves_other:
        with pytest.raises(UnknownNamespaceError), backend.store.transaction(FOREIGN, PLAN):
            pass  # pragma: no cover - refused before the lock
        return
    with backend.store.transaction(FOREIGN, PLAN) as held:
        assert held is True


# -- registry -------------------------------------------------------------------


def test_every_protocol_method_is_covered_by_a_contract_test():
    """Every name in RECORD_STORE_METHODS is declared by at least one test."""
    assert sorted(set(RECORD_STORE_METHODS) - COVERED) == []


def test_every_covered_name_is_a_protocol_method():
    """A declaration naming something the Protocol lacks is a typo, not coverage."""
    assert sorted(COVERED - set(RECORD_STORE_METHODS)) == []


# -- a non-conforming store the namespace cases must reject ----------------------


class LeakyNamespaceStore:
    """A deliberately non-conforming store: it accepts any namespace.

    Every call is folded into the default namespace, so a foreign write lands
    where the default namespace reads it and a malformed namespace is never
    refused. The namespace checks must fail on it; the pytester test below
    asserts that they do.
    """

    def __init__(self):
        self._inner = InMemoryRecordStore()

    def __getattr__(self, name):
        if name not in RECORD_STORE_METHODS:
            raise AttributeError(name)
        method = getattr(self._inner, name)

        def fold_into_default(namespace, *args, **kwargs):
            return method(DEFAULT_NAMESPACE, *args, **kwargs)

        return fold_into_default


NAMESPACE_CHECKS = (
    check_foreign_read_is_contained,
    check_foreign_write_is_contained,
    check_foreign_write_leaves_default_unchanged,
)
