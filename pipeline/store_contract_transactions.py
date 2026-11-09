"""Reusable behaviour suite for ``RecordStore.transaction`` (ADR 0012 decision 6).

ADR 0012 decision 6 says a record store's ``transaction(namespace, plan)`` stays
non-blocking, and this story makes that true for every implementation.  The lock
contract only is covered here: atomicity of the writes made inside the block is
a separate story.

Like :mod:`pipeline.store_contract`, this module imports no test framework: it
uses plain ``assert`` statements and the small helpers below, so the same cases
run under any harness.  None of the classes is named ``Test*`` -- pytest must
never collect a mixin (it would call the unimplemented ``make_store``).

Inherit :class:`TransactionContract` in a concrete test class alongside the
behaviour mixins from :mod:`pipeline.store_contract`.  A store that serves every
namespace (the in-memory reference store) sets
``serves_other_namespaces = True``; a single-namespace store leaves it alone.
"""

from __future__ import annotations

import math
import threading
import time

from pipeline.store_records import (
    DEFAULT_NAMESPACE,
    InvalidRecordKeyError,
    UnknownNamespaceError,
)

_PLAN = 'plan-a'
_OTHER_NAMESPACE = 'acme'
_INVALID_KEY = '../x'
_INVALID_NAMESPACE = 'Bad NS'

# How long a refused (non-blocking) acquire may take to answer.  Generous
# enough for a loaded CI box, tight enough that a store which actually blocks
# fails instead of hanging the suite.
_REFUSAL_BUDGET_SECONDS = 1.0


def _expect(exc_type, fn):
    """Assert that calling ``fn`` raises exactly ``exc_type``."""
    try:
        fn()
    except exc_type:
        return
    raise AssertionError(f'{fn} did not raise {exc_type.__name__}')


def _enter(store, namespace, plan_name, wait_seconds=0.0):
    """Enter and leave the transaction, returning the yielded bool.

    Entering is what a caller does, so this covers a store that validates
    eagerly (at the call) and one that validates when the block is entered.
    """
    with store.transaction(namespace, plan_name, wait_seconds) as acquired:
        return acquired


class TransactionContract:
    """Behaviour every :class:`pipeline.store_records.RecordStore` must show."""

    #: Set to ``True`` by stores that serve every valid namespace.
    serves_other_namespaces = False

    def make_store(self):
        """Return a fresh, empty store under test."""
        raise NotImplementedError

    def plans_dir(self):
        """Return the directory a file-backed store locks in, or ``None``.

        Only used to prove that a rejected key is refused *before* any lock is
        taken: no ``<plan>.lock`` file may appear.  A store that keeps no lock
        file returns ``None`` and that assertion is skipped.
        """

    # -- helpers -------------------------------------------------------
    @staticmethod
    def _hold_in_thread(store, plan_name, wait_seconds=0.0):
        """Acquire ``plan_name`` in a daemon thread and hold it until released.

        Returns ``(result, release)``: ``result`` is a one-slot list that gains
        the yielded bool, ``release`` is an :class:`threading.Event` the test
        sets to let the holder exit its block.
        """
        release = threading.Event()
        result = []

        def holder():
            with store.transaction(DEFAULT_NAMESPACE, plan_name, wait_seconds) as acquired:
                result.append(acquired)
                if not acquired:
                    return
                release.wait(5.0)

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5.0
        while not result and time.monotonic() < deadline:
            time.sleep(0.005)
        assert result == [True], 'the holder thread could not acquire the lock'
        return result, release

    # -- uncontended ---------------------------------------------------
    def test_uncontended_transaction_yields_true_and_runs_the_block(self):
        store = self.make_store()
        ran = []
        with store.transaction(DEFAULT_NAMESPACE, _PLAN) as acquired:
            assert acquired is True
            ran.append('body')
        assert ran == ['body']

    # -- contention ----------------------------------------------------
    def test_a_second_thread_is_refused_without_blocking(self):
        store = self.make_store()
        _, release = self._hold_in_thread(store, _PLAN)
        try:
            ran = []
            started = time.monotonic()
            with store.transaction(DEFAULT_NAMESPACE, _PLAN) as acquired:
                # The caller's half of the contract: skip all work on False.
                if acquired:
                    ran.append('body')
            elapsed = time.monotonic() - started
            assert acquired is False
            assert ran == [], 'the refused thread must skip the block body'
            assert elapsed < _REFUSAL_BUDGET_SECONDS, elapsed
        finally:
            release.set()

    def test_a_waiter_acquires_when_the_holder_releases_inside_the_budget(self):
        store = self.make_store()
        _, release = self._hold_in_thread(store, _PLAN)
        threading.Timer(0.1, release.set).start()
        with store.transaction(DEFAULT_NAMESPACE, _PLAN, 0.3) as acquired:
            assert acquired is True

    def test_a_waiter_gives_up_when_the_holder_never_releases(self):
        store = self.make_store()
        _, release = self._hold_in_thread(store, _PLAN)
        try:
            started = time.monotonic()
            with store.transaction(DEFAULT_NAMESPACE, _PLAN, 0.3) as acquired:
                assert acquired is False
            elapsed = time.monotonic() - started
            assert elapsed < _REFUSAL_BUDGET_SECONDS, elapsed
        finally:
            release.set()

    # -- re-entrance ---------------------------------------------------
    def test_re_entry_on_the_holding_thread_yields_true(self):
        store = self.make_store()
        with store.transaction(DEFAULT_NAMESPACE, _PLAN) as outer:
            assert outer is True
            with store.transaction(DEFAULT_NAMESPACE, _PLAN) as inner:
                assert inner is True

    # -- release on raise ----------------------------------------------
    def test_the_lock_is_released_when_the_block_raises(self):
        store = self.make_store()

        class Boom(Exception):
            pass

        try:
            with store.transaction(DEFAULT_NAMESPACE, _PLAN) as acquired:
                assert acquired is True
                raise Boom('boom')
        except Boom as exc:
            assert str(exc) == 'boom'
        else:  # pragma: no cover - the raise above must propagate
            raise AssertionError('the exception did not propagate')

        with store.transaction(DEFAULT_NAMESPACE, _PLAN) as acquired:
            assert acquired is True

    # -- rejected keys are refused before any lock ----------------------
    def test_an_invalid_namespace_is_rejected_before_any_lock_is_taken(self):
        store = self.make_store()
        lock_dir = self.plans_dir()
        _expect(
            InvalidRecordKeyError,
            lambda: _enter(store, _INVALID_NAMESPACE, _PLAN),
        )
        if lock_dir is not None:
            assert not (lock_dir / f'{_PLAN}.lock').exists()

    def test_an_invalid_plan_name_is_rejected_before_any_lock_is_taken(self):
        store = self.make_store()
        lock_dir = self.plans_dir()
        _expect(
            InvalidRecordKeyError,
            lambda: _enter(store, DEFAULT_NAMESPACE, _INVALID_KEY),
        )
        if lock_dir is not None:
            assert not (lock_dir / f'{_PLAN}.lock').exists()

    # -- namespace scope -----------------------------------------------
    def test_an_unknown_namespace_is_refused(self):
        store = self.make_store()
        if self.serves_other_namespaces:
            with store.transaction(_OTHER_NAMESPACE, _PLAN) as acquired:
                assert acquired is True
            return
        _expect(
            UnknownNamespaceError,
            lambda: _enter(store, _OTHER_NAMESPACE, _PLAN),
        )

    def test_another_namespace_holds_an_independent_lock(self):
        store = self.make_store()
        if not self.serves_other_namespaces:
            return
        # Same plan name, two namespaces: holding one must not refuse the other.
        with store.transaction(_OTHER_NAMESPACE, _PLAN) as outer:
            assert outer is True
            with store.transaction(DEFAULT_NAMESPACE, _PLAN) as inner:
                assert inner is True

    # -- degenerate wait budgets ----------------------------------------
    def test_a_negative_wait_behaves_like_zero(self):
        store = self.make_store()
        _, release = self._hold_in_thread(store, _PLAN)
        try:
            started = time.monotonic()
            with store.transaction(DEFAULT_NAMESPACE, _PLAN, -1) as acquired:
                assert acquired is False
            elapsed = time.monotonic() - started
            assert elapsed < _REFUSAL_BUDGET_SECONDS, elapsed
        finally:
            release.set()

    def test_a_non_finite_wait_behaves_like_zero(self):
        store = self.make_store()
        assert math.isnan(float('nan'))
        _, release = self._hold_in_thread(store, _PLAN)
        try:
            started = time.monotonic()
            with store.transaction(DEFAULT_NAMESPACE, _PLAN, float('nan')) as acquired:
                assert acquired is False
            elapsed = time.monotonic() - started
            assert elapsed < _REFUSAL_BUDGET_SECONDS, elapsed
        finally:
            release.set()


class AtomicTransactionContract:
    """Behaviour of a store that declares ``atomic_transactions = True``.

    ADR 0012 decision 9 splits the record stores into two tiers.  An atomic
    store makes every write the holding thread performs inside
    :meth:`transaction` visible together when the *outermost* block exits
    normally, and leaves none of them behind when that block raises.  Nested
    blocks commit with the outermost one.

    Inherit this mixin in a concrete test class that supplies ``make_store``;
    the class is deliberately not named ``Test*``, because pytest must never
    collect a mixin on its own.
    """

    #: Set to ``True`` by stores that serve every valid namespace.
    serves_other_namespaces = False

    def make_store(self):
        """Return a fresh, empty store under test."""
        raise NotImplementedError

    def plans_dir(self):
        """Return the directory a file-backed store locks in, or ``None``."""

    def test_the_store_declares_the_atomic_tier(self):
        store = self.make_store()
        assert store.atomic_transactions is True, (
            f'{type(store).__name__} must declare atomic_transactions = True'
        )

    def test_a_raising_block_rolls_back_the_writes_it_made(self):
        store = self.make_store()
        store.put_manifest(DEFAULT_NAMESPACE, _PLAN, {'version': 1})
        try:
            with store.transaction(DEFAULT_NAMESPACE, _PLAN) as acquired:
                assert acquired is True
                store.put_manifest(DEFAULT_NAMESPACE, _PLAN, {'version': 2})
                raise RuntimeError('boom')
        except RuntimeError:
            pass
        assert store.get_manifest(DEFAULT_NAMESPACE, _PLAN) == {'version': 1}


class OrderedTransactionContract:
    """Behaviour of a store that declares ``atomic_transactions = False``.

    An ordered store makes each write durable when its call returns, in call
    order, and rolls nothing back when a block raises -- so a process that dies
    between the state write and the outbox append loses that notification,
    while a process that dies after both keeps both (ADR 0012 decision 9).

    Inherit this mixin in a concrete test class that supplies ``make_store`` and,
    for a file-backed store, ``plans_dir``.
    """

    #: Set to ``True`` by stores that serve every valid namespace.
    serves_other_namespaces = False

    def make_store(self):
        """Return a fresh, empty store under test."""
        raise NotImplementedError

    def plans_dir(self):
        """Return the directory a file-backed store locks in, or ``None``."""

    def test_the_store_declares_the_ordered_tier(self):
        store = self.make_store()
        assert store.atomic_transactions is False, (
            f'{type(store).__name__} must declare atomic_transactions = False'
        )

    def test_a_raising_block_keeps_the_writes_it_made(self):
        store = self.make_store()
        store.put_manifest(DEFAULT_NAMESPACE, _PLAN, {'version': 1})
        try:
            with store.transaction(DEFAULT_NAMESPACE, _PLAN) as acquired:
                assert acquired is True
                store.put_manifest(DEFAULT_NAMESPACE, _PLAN, {'version': 2})
                raise RuntimeError('boom')
        except RuntimeError:
            pass
        assert store.get_manifest(DEFAULT_NAMESPACE, _PLAN) == {'version': 2}
