"""In-memory reference implementation of the record store contract (ADR 0012).

This is a reference implementation and test double, not durable storage: all
state lives in process memory and disappears when the object does.  It exists so
the shared contract suite in :mod:`pipeline.store_contract` can be exercised
without I/O, and so callers have a cheap stand-in for a real store.

Every valid namespace is served and created on first write; nothing ever spans
namespaces.
"""

from __future__ import annotations

import contextlib
import copy
import json
import math
import threading
import time

from pipeline.concurrency import PLAN_LOCK_RETRY_INTERVAL_SECONDS
from pipeline.store_records import (
    validate_namespace,
    validate_plan_name,
    validate_story_key,
    validate_usage_record,
)


class InMemoryRecordStore:
    """A thread-safe, non-durable :class:`pipeline.store_records.RecordStore`."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._manifests: dict[tuple[str, str], dict] = {}
        self._decisions: dict[tuple[str, str], list[dict]] = {}
        # Append-only usage records, in the order they were appended.
        self._usage: dict[tuple[str, str], list[dict]] = {}
        self._journals: dict[tuple[str, str, str], list[dict]] = {}
        # Journal activity: epoch seconds of the last append per journal.  A
        # high-water mark, so a clock that steps backwards can never report the
        # journal as older than an append it already reported.
        self._journal_updated_at: dict[tuple[str, str, str], float] = {}
        # One re-entrant lock per (namespace, plan_name), created on first use
        # so an unused pair costs nothing.
        self._plan_locks: dict[tuple[str, str], threading.RLock] = {}

    # -- manifests -----------------------------------------------------
    def get_manifest(self, namespace: str, plan_name: str) -> dict | None:
        """Return a copy of the manifest for ``plan_name``, or ``None``."""
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        with self._lock:
            manifest = self._manifests.get((namespace, plan_name))
            if manifest is None:
                return None
            return copy.deepcopy(manifest)

    def get_manifests(self, namespace: str) -> dict[str, dict]:
        """Return a deep copy of every manifest stored in ``namespace``."""
        validate_namespace(namespace)
        with self._lock:
            manifests = {
                plan_name: copy.deepcopy(manifest)
                for (stored_namespace, plan_name), manifest in self._manifests.items()
                if stored_namespace == namespace
            }
        return dict(sorted(manifests.items()))

    def put_manifest(self, namespace: str, plan_name: str, manifest: dict) -> None:
        """Store ``manifest`` as the whole document for ``plan_name``."""
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        if not isinstance(manifest, dict):
            raise TypeError('manifest must be a dict')
        # Serialise before touching any state: a manifest that cannot be stored
        # must leave no trace behind.
        json.dumps(manifest)
        with self._lock:
            self._manifests[(namespace, plan_name)] = copy.deepcopy(manifest)

    def list_plans(self, namespace: str) -> list[str]:
        """Return the sorted plan names stored in ``namespace``."""
        validate_namespace(namespace)
        with self._lock:
            names = {
                plan_name
                for (stored_namespace, plan_name) in self._manifests
                if stored_namespace == namespace
            }
            return sorted(names)

    # -- decisions -----------------------------------------------------
    def get_decisions(self, namespace: str, plan_name: str) -> list[dict]:
        """Return a copy of the decision records for ``plan_name``."""
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        with self._lock:
            return copy.deepcopy(self._decisions.get((namespace, plan_name), []))

    def append_decision(self, namespace: str, plan_name: str, record: dict) -> None:
        """Append one decision record for ``plan_name``."""
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        if not isinstance(record, dict):
            raise TypeError('record must be a dict')
        with self._lock:
            self._decisions.setdefault((namespace, plan_name), []).append(
                copy.deepcopy(record)
            )

    # -- usage records -------------------------------------------------
    def get_usage(self, namespace: str, plan_name: str) -> list[dict]:
        """Return copies of ``plan_name``'s usage records, in written order."""
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        with self._lock:
            records = self._usage.get((namespace, plan_name), ())
            return copy.deepcopy(list(records))

    def append_usage(self, namespace: str, plan_name: str, record: dict) -> None:
        """Append one usage record; usage is append-only, there is no update.

        The record is validated before the lock is taken, so an invalid record
        is refused and the stored history is unchanged.
        """
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        validated = validate_usage_record(plan_name, record)
        with self._lock:
            self._usage.setdefault((namespace, plan_name), []).append(
                copy.deepcopy(validated)
            )

    # -- journals ------------------------------------------------------
    def get_journal(self, namespace: str, plan_name: str, story_key: str) -> list[dict]:
        """Return a copy of the journal records for ``story_key``."""
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        validate_story_key(story_key)
        with self._lock:
            return copy.deepcopy(
                self._journals.get((namespace, plan_name, story_key), [])
            )

    def append_journal(
        self, namespace: str, plan_name: str, story_key: str, record: dict
    ) -> None:
        """Append one journal record for ``story_key``."""
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        validate_story_key(story_key)
        if not isinstance(record, dict):
            raise TypeError('record must be a dict')
        with self._lock:
            self._journals.setdefault((namespace, plan_name, story_key), []).append(
                copy.deepcopy(record)
            )
            key = (namespace, plan_name, story_key)
            now = time.time()
            previous = self._journal_updated_at.get(key)
            # A journal with no tracked activity yet starts at ``now``; an
            # existing one only ever moves forwards.
            self._journal_updated_at[key] = (
                now if previous is None else max(previous, now)
            )

    def clear_journal(
        self, namespace: str, plan_name: str, story_key: str
    ) -> None:
        """Forget the journal for exactly this story in this namespace.

        Only the one ``(namespace, plan_name, story_key)`` entry is removed, so
        other stories and other plans keep their journals.  An absent journal
        is a silent no-op, which is why the entry is popped rather than
        deleted.
        """
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        validate_story_key(story_key)
        with self._lock:
            self._journals.pop((namespace, plan_name, story_key), None)
            self._journal_updated_at.pop((namespace, plan_name, story_key), None)

    def get_journal_updated_at(
        self, namespace: str, plan_name: str, story_key: str
    ) -> float | None:
        """Return the epoch seconds of the last append, or ``None``."""
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        validate_story_key(story_key)
        with self._lock:
            return self._journal_updated_at.get((namespace, plan_name, story_key))

    def list_journal_keys(self, namespace: str, plan_name: str) -> list[str]:
        """Return the sorted story keys holding a journal in ``plan_name``."""
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        with self._lock:
            keys = {
                stored_story_key
                for (stored_namespace, stored_plan, stored_story_key) in self._journals
                if stored_namespace == namespace and stored_plan == plan_name
            }
            return sorted(keys)

    # -- transactions --------------------------------------------------
    @contextlib.contextmanager
    def transaction(self, namespace: str, plan_name: str, wait_seconds: float = 0.0):
        """Acquire this plan's lock, or report it was refused.

        One :class:`threading.RLock` per ``(namespace, plan_name)`` pair,
        created lazily under :attr:`_lock`.  Yields ``True`` when the lock was
        acquired and ``False`` when it was not; callers must skip all work on
        ``False``.  The lock is re-entrant, so a thread that already holds it
        acquires it again instead of deadlocking against itself.

        ``wait_seconds`` is opt-in: the default ``0.0`` is a single
        non-blocking attempt with no sleeping.  A positive budget retries every
        :data:`pipeline.concurrency.PLAN_LOCK_RETRY_INTERVAL_SECONDS` until the
        deadline passes, then yields ``False``; a negative or non-finite budget
        is treated as ``0.0``, exactly as
        :func:`pipeline.concurrency._plan_lock` does.
        """
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        if wait_seconds < 0 or not math.isfinite(wait_seconds):
            wait_seconds = 0.0
        lock = self._plan_lock_for(namespace, plan_name)
        deadline = time.monotonic() + wait_seconds
        while not lock.acquire(blocking=False):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                yield False
                return
            time.sleep(min(PLAN_LOCK_RETRY_INTERVAL_SECONDS, remaining))
        try:
            yield True
        finally:
            lock.release()

    def _plan_lock_for(self, namespace: str, plan_name: str) -> threading.RLock:
        """Return this pair's lock, creating it on first use."""
        with self._lock:
            lock = self._plan_locks.get((namespace, plan_name))
            if lock is None:
                lock = threading.RLock()
                self._plan_locks[(namespace, plan_name)] = lock
            return lock
