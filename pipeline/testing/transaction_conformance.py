"""The published transaction suite (ADR 0012 decisions 6 and 9).

An out-of-tree store package runs this suite as part of its conformance gate.
The package's conftest supplies:

* ``record_store_factory`` -- a fresh, empty store for each test.
* ``record_store_transaction_tier`` -- REQUIRED. ``'atomic'`` for a store whose
  outermost ``transaction()`` block commits or rolls back every write it made;
  ``'ordered'`` for a store whose writes are durable when each call returns. The
  suite runs the matching tier's cases and skips the other. Any other value is
  an error.
* ``record_store_serves_other_namespaces`` -- optional. ``True`` for a store that
  serves every valid namespace; absent or ``False`` takes the strict reading.
* ``record_store_plans_dir`` -- optional. The directory a file-backed store locks
  in. Given, the suite proves a rejected key takes no lock file; absent, that
  assertion is skipped.

Every test class is a mixin of :mod:`pipeline.store_contract_transactions`,
collected here so a consumer only imports this module:
``from pipeline.testing.transaction_conformance import *``.

Each test declares the protocol names it exercises with ``@covers``;
``test_transaction_every_protocol_method_is_covered`` asserts that the declarations
cover every transaction method, and
``test_transaction_every_covered_name_is_a_transaction_method`` asserts that no
declaration names anything else.

This module must stay importable without the repo's ``tests/`` tree and without
loading ``pipeline.server``.
"""

import pytest

from pipeline.store_contract_transactions import (
    AtomicTransactionContract,
    OrderedTransactionContract,
    TransactionContract,
)

TIERS = ('atomic', 'ordered')

#: The RecordStore Protocol method this suite grades.
TRANSACTION_METHODS = ('transaction',)

#: Protocol names declared exercised by at least one test in this suite.
COVERED = set()


def covers(*names):
    """Declare that the decorated test class exercises the named Protocol methods."""

    def mark(test):
        COVERED.update(names)
        return test

    return mark


def _tier(request):
    """Return the declared transaction tier, refusing any other value."""
    tier = request.getfixturevalue('record_store_transaction_tier')
    if tier not in TIERS:
        raise ValueError(
            f"record_store_transaction_tier must be 'atomic' or 'ordered', not {tier!r}"
        )
    return tier


def _optional(request, name):
    """Return the value of the optional fixture ``name``, or ``None``."""
    try:
        return request.getfixturevalue(name)
    except pytest.FixtureLookupError:
        return None


class _TransactionStoreBinding:
    """Grades the store the consumer's factory supplies.

    ``tier`` is set by the tier subclasses; the fixture skips a class whose tier
    is not the store's declared one. ``None`` means the class applies to both.
    """

    tier = None

    @pytest.fixture(autouse=True)
    def _bind_store(self, request, record_store_factory):
        tier = _tier(request)
        if self.tier is not None and tier != self.tier:
            pytest.skip(f'the store declares the {tier} tier')
        self._store = record_store_factory
        self._plans_dir = _optional(request, 'record_store_plans_dir')
        self.serves_other_namespaces = bool(
            _optional(request, 'record_store_serves_other_namespaces')
        )

    def make_store(self):
        """Return the store under test."""
        return self._store

    def plans_dir(self):
        """Return the directory a file-backed store locks in, or ``None``."""
        return self._plans_dir


@covers('transaction')
class TestTransactionLock(_TransactionStoreBinding, TransactionContract):
    """The lock contract every store shows, whatever its tier."""


class TestAtomicTransactions(_TransactionStoreBinding, AtomicTransactionContract):
    """Rollback of a raising block, for a store that declares the atomic tier."""

    tier = 'atomic'


class TestOrderedTransactions(_TransactionStoreBinding, OrderedTransactionContract):
    """Durable writes in a raising block, for a store that declares the ordered tier."""

    tier = 'ordered'


# -- completeness ----------------------------------------------------------------


def test_transaction_every_protocol_method_is_covered():
    """Every transaction Protocol method is declared by a test class here."""
    assert sorted(set(TRANSACTION_METHODS) - COVERED) == []


def test_transaction_every_covered_name_is_a_transaction_method():
    """A declaration naming anything else is a typo, not coverage."""
    assert sorted(COVERED - set(TRANSACTION_METHODS)) == []
