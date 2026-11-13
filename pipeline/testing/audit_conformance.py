"""The published audit-log suite (ADR 0012 decision 10).

An out-of-tree store package runs this suite as part of its conformance gate.
The package's conftest supplies:

* ``record_store_factory`` -- a fresh, empty store for each test. The suite
  grades ``store.audit_log()``, the log that store exposes.
* ``audit_rewrite_raw`` -- REQUIRED. The tamper hook: a callable
  ``(log, namespace, chain, entries)`` that replaces the stored entries of one
  chain verbatim. It bypasses the protocol on purpose, because the protocol has
  no mutator. A store that cannot provide one cannot claim tamper detection, so
  a missing fixture is an error, not a skip.
* ``record_store_serves_other_namespaces`` -- optional. ``True`` for a store that
  serves every valid namespace; absent or ``False`` takes the strict reading,
  where a foreign namespace is refused with ``UnknownNamespaceError``.

The test classes are the mixins of :mod:`pipeline.audit_contract`, collected
here so a consumer only imports this module:
``from pipeline.testing.audit_conformance import *``.

Each test class declares the :class:`pipeline.audit_log.AuditLog` Protocol names
it exercises with ``@covers``; ``test_audit_every_protocol_method_is_covered``
asserts that the declarations cover every name in the Protocol, and
``test_audit_every_covered_name_is_an_audit_method`` asserts that no declaration
names anything the Protocol lacks.

This module must stay importable without the repo's ``tests/`` tree and without
loading ``pipeline.server``.
"""

import pytest

from pipeline.audit_contract import (
    AuditConcurrencyContract,
    AuditLogContract,
    AuditTamperContract,
    MultiNamespaceContract,
    SingleNamespaceContract,
)
from pipeline.audit_log import AuditLog
from pipeline.store_records import DEFAULT_NAMESPACE

#: The chain the tamper cases seed. The mixin uses one fixed name for it.
_TAMPER_CHAIN = 'plan-a'

#: Every public method of the AuditLog Protocol: the source of truth.
AUDIT_METHODS = tuple(
    sorted(
        name
        for name, value in vars(AuditLog).items()
        if not name.startswith('_') and callable(value)
    )
)

#: Protocol names declared exercised by at least one test in this suite.
COVERED = set()


def covers(*names):
    """Declare that the decorated test class exercises the named Protocol methods."""

    def mark(test):
        COVERED.update(names)
        return test

    return mark


def _serves_other_namespaces(request):
    """Return the store's declared namespace policy; absent means strict."""
    try:
        return request.getfixturevalue('record_store_serves_other_namespaces')
    except pytest.FixtureLookupError:
        return False


class _AuditStoreBinding:
    """Grades the audit log of the store the consumer's factory supplies.

    ``serves_other_namespaces`` is set by the namespace-policy subclasses; the
    fixture skips a class whose policy does not match the store's declared one.
    ``None`` means the class applies to every policy.
    """

    serves_other_namespaces = None

    @pytest.fixture(autouse=True)
    def _bind_store(self, request, record_store_factory):
        if (
            self.serves_other_namespaces is not None
            and _serves_other_namespaces(request) != self.serves_other_namespaces
        ):
            pytest.skip('the store does not declare this namespace policy')
        self._store = record_store_factory

    def make_log(self):
        """Return the audit log of the store under test."""
        return self._store.audit_log()


@covers('append', 'read_chain', 'head', 'list_chains')
class TestAuditLog(_AuditStoreBinding, AuditLogContract):
    """Default-namespace behaviour and argument validation, for every store."""

    def test_two_logs_share_no_state(self):
        """Not graded here: a store has one audit log, the same object every call.

        The mixin's case builds two independent logs. Through a store there is
        only one, so the store's own guarantee (``audit_log()`` returns the same
        object) is what applies, and no second log can be made to compare.
        """
        pytest.skip('a store has one audit log; the mixin needs two independent logs')


class TestAuditLogMultiNamespace(_AuditStoreBinding, MultiNamespaceContract):
    """Namespace isolation, for a store that serves every valid namespace."""

    serves_other_namespaces = True


class TestAuditLogSingleNamespace(_AuditStoreBinding, SingleNamespaceContract):
    """Foreign-namespace refusal, for a store that serves only the default."""

    serves_other_namespaces = False


class TestAuditConcurrency(_AuditStoreBinding, AuditConcurrencyContract):
    """Concurrent appends are serialized, for every store."""


class TestAuditTamper(_AuditStoreBinding, AuditTamperContract):
    """Rewritten history is detected; the tamper hook is a required fixture."""

    @pytest.fixture(autouse=True)
    def _bind_rewrite_raw(self, audit_rewrite_raw):
        self._rewrite_raw = audit_rewrite_raw

    def rewrite_raw(self, log, namespace, chain, entries):
        """Replace the stored entries of one chain verbatim, via the fixture."""
        self._rewrite_raw(log, namespace, chain, entries)

    def make_log(self):
        """Return an empty log, cleared through the hook.

        A store has one audit log, so a second "fresh" log in the same test is
        an empty chain made with the hook. The seeding helper appends to the
        log and asserts the chain it reads back.
        """
        log = super().make_log()
        self.rewrite_raw(log, DEFAULT_NAMESPACE, _TAMPER_CHAIN, [])
        return log


# -- completeness ----------------------------------------------------------------


def test_audit_every_protocol_method_is_covered():
    """Every AuditLog Protocol method is declared by a test class here."""
    assert sorted(set(AUDIT_METHODS) - COVERED) == []


def test_audit_every_covered_name_is_an_audit_method():
    """A declaration naming anything else is a typo, not coverage."""
    assert sorted(COVERED - set(AUDIT_METHODS)) == []
