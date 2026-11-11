"""Runs the published record-store contract suite over this repo's three stores.

The suite itself lives in ``pipeline/testing/store_conformance.py`` (ADR 0012) so
an external store package runs the same tests as its conformance gate. This file
only supplies the repo's own backends (``file``, ``sqlite``, ``memory``) through
the suite's fixture contract, and the pytester self-check that the namespace
cases reject a store which accepts foreign namespaces.
"""

from pathlib import Path

import pytest

pytest_plugins = ('pytester',)

# pipeline.server is imported for its PLAN_DIR binding, which the file backend
# reads through; see ``record_store_factory``.
import pipeline.persistence
import pipeline.server
from pipeline.store import FileStore
from pipeline.store_memory import InMemoryRecordStore
from pipeline.store_sqlite import SqliteStore
from pipeline.testing.store_conformance import *
from pipeline.testing.store_conformance import KINDS, NAMESPACE_CHECKS

BACKENDS = ('file', 'sqlite', 'memory')
#: The record kinds each repo backend serves in a foreign namespace.
SERVES_OTHER = {
    'file': frozenset(),
    'sqlite': frozenset(),
    'memory': frozenset(
        {'manifest', 'decisions', 'journal', 'usage', 'notification', 'context_snapshot'}
    ),
}
#: Known defects the suite pins as strict xfails: a failure is expected, and an
#: unexpected pass fails the run so the marker gets removed with the fix.
KNOWN_GAPS = {
    ('file', 'malformed_get'): 'get_context_snapshot returns None for a malformed '
    'digest; the Protocol requires InvalidRecordKeyError',
    ('sqlite', 'malformed_get'): 'get_context_snapshot returns None for a malformed '
    'digest; the Protocol requires InvalidRecordKeyError',
    ('memory', 'malformed_get'): 'get_context_snapshot returns None for a malformed '
    'digest; the Protocol requires InvalidRecordKeyError',
    ('sqlite', 'draft_epics'): 'sqlite list_plan_drafts lists a draft with no epics '
    'key; the Protocol requires the epics filter',
}


@pytest.fixture(params=BACKENDS)
def backend_kind(request):
    return request.param


@pytest.fixture
def record_store_factory(backend_kind, tmp_path, monkeypatch):
    """A fresh record store of one implementation, isolated per test."""
    plan_dir = tmp_path / 'plans'
    plan_dir.mkdir()
    monkeypatch.setattr(pipeline.server, 'PLAN_DIR', plan_dir)
    monkeypatch.setattr(pipeline.persistence, 'PLAN_DIR', plan_dir)
    if backend_kind == 'file':
        yield FileStore().records()
    elif backend_kind == 'sqlite':
        sqlite_store = SqliteStore(str(tmp_path / 'records.db'))
        try:
            yield sqlite_store.records()
        finally:
            sqlite_store.close()
    else:
        yield InMemoryRecordStore()


@pytest.fixture
def record_store_serves_other(backend_kind):
    return SERVES_OTHER[backend_kind]


@pytest.fixture
def record_store_known_gaps(backend_kind):
    return {
        check: reason
        for (kind, check), reason in KNOWN_GAPS.items()
        if kind == backend_kind
    }


def test_namespace_cases_reject_a_store_that_accepts_foreign_namespaces(pytester):
    """Run the namespace cases against a leaky store and a conforming one."""
    pytester.syspathinsert(Path(__file__).resolve().parent)
    pytester.makepyfile(
        """
        import pytest
        from test_record_store_contract import (
            KINDS,
            KIND_IDS,
            NAMESPACE_CHECKS,
            InMemoryRecordStore,
            LeakyNamespaceStore,
            check_invalid_namespace_is_refused,
            SERVES_OTHER,
        )

        @pytest.mark.parametrize('kind', KINDS, ids=KIND_IDS)
        @pytest.mark.parametrize('check', NAMESPACE_CHECKS, ids=lambda c: c.__name__)
        def test_leaky_store(check, kind):
            check(LeakyNamespaceStore(), False, kind)

        @pytest.mark.parametrize('kind', KINDS, ids=KIND_IDS)
        @pytest.mark.parametrize('check', NAMESPACE_CHECKS, ids=lambda c: c.__name__)
        def test_conforming_store(check, kind):
            check(InMemoryRecordStore(), kind.name in SERVES_OTHER['memory'], kind)

        @pytest.mark.parametrize('kind', KINDS, ids=KIND_IDS)
        def test_leaky_store_invalid_namespace(kind):
            check_invalid_namespace_is_refused(LeakyNamespaceStore(), kind)

        @pytest.mark.parametrize('kind', KINDS, ids=KIND_IDS)
        def test_conforming_store_invalid_namespace(kind):
            check_invalid_namespace_is_refused(InMemoryRecordStore(), kind)
        """
    )

    result = pytester.runpytest()

    kinds = len(KINDS)
    checks = len(NAMESPACE_CHECKS)
    result.assert_outcomes(
        passed=kinds * checks + kinds,
        failed=kinds * checks + kinds,
    )
    assert 'DID NOT RAISE' in result.stdout.str()
