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
from pipeline.store_contract_outbox import OutboxContract, OutboxSummaryContract
from pipeline.store_records import (
    DEFAULT_NAMESPACE,
    InvalidRecordKeyError,
    UnknownNamespaceError,
    UnreadableRecordError,
    snapshot_meta,
    context_address,
    validate_channel,
    validate_namespace,
    validate_notification_record,
    validate_plan_name,
    validate_story_key,
    validate_usage_record,
    verify_context_digest,
)

#: Sentinel recorded in the undo log for a key that held no value before the
#: transaction wrote it, so a rollback knows to delete rather than restore.
_ABSENT = object()


class InMemoryRecordStore(OutboxContract, OutboxSummaryContract):
    """A thread-safe, non-durable :class:`pipeline.store_records.RecordStore`.

    Atomic tier (ADR 0012 decision 9): while a thread holds the outermost
    ``transaction()`` block it records an undo log of every structure it
    writes, and an exception that leaves that block restores exactly those
    keys.  Nested blocks commit with the outermost one.
    """

    #: Every write inside ``transaction()`` commits or rolls back as a whole.
    atomic_transactions = True

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # Per-thread transaction state: ``{'depth': int, 'undo': [...]}``.  It
        # must be thread-local, so one thread's block never rolls back or
        # commits another thread's writes.
        self._transactions = threading.local()
        self._manifests: dict[tuple[str, str], dict] = {}
        # The transient plan drafts (ADR 0012 decision 3), per (namespace,
        # plan), held as a deep copy so a caller's later mutation of the dict
        # it passed never changes what is stored.
        self._drafts: dict[tuple[str, str], dict] = {}
        # The plan-completion marker and report (ADR 0012 decision 3): the
        # plans whose marker is set, and the report text per plan.  Both are
        # dicts keyed by (namespace, plan), so the transaction undo log can
        # restore them like every other structure.
        self._completed: dict[tuple[str, str], bool] = {}
        self._reports: dict[tuple[str, str], str] = {}
        # The content-addressed context snapshots (ADR 0012 decision 11), per
        # (namespace, digest), held as a deep copy: a snapshot is immutable, so
        # a caller's later mutation of the meta it passed never reaches storage.
        self._snapshots: dict[tuple[str, str], dict] = {}
        self._decisions: dict[tuple[str, str], list[dict]] = {}
        # Append-only usage records, in the order they were appended.
        self._usage: dict[tuple[str, str], list[dict]] = {}
        self._journals: dict[tuple[str, str, str], list[dict]] = {}
        # The notification history, per (namespace, plan).  An instance
        # attribute created here, never a class attribute and never a
        # mutable default, so two stores never share one plan's history.
        self._notifications: dict[tuple[str, str], list[dict]] = {}
        # Journal activity: epoch seconds of the last append per journal.  A
        # high-water mark, so a clock that steps backwards can never report the
        # journal as older than an append it already reported.
        self._journal_updated_at: dict[tuple[str, str, str], float] = {}
        # Per-channel outbox queues: (namespace, plan, channel) -> [(id, record)]
        # in append order, oldest first.  Ids come from a per-store counter, so
        # they are opaque and never reused inside one store's lifetime.
        self._outboxes: dict[tuple[str, str, str], list[tuple[str, dict]]] = {}
        self._outbox_id_counter = 0
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
            self._record_undo(self._manifests, (namespace, plan_name))
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

    # -- plan drafts (ADR 0012 decision 3) ------------------------------
    def _draft_path(self, plan_name: str):
        """Return ``<plans dir>/<plan>.json``, resolved per call.

        The draft is a *transient file* the file store stages (``save_plan``),
        not durable store state, so the reference store reads a draft it does
        not hold itself from the plans directory the writers use, through
        :mod:`pipeline.persistence` -- the reader ingest already refuses with --
        rather than constructing a file store of its own.  The directory comes
        from the writer's own path helper, so a plans directory patched on that
        module is the one that is read.
        """
        from pipeline import persistence

        return persistence._journal_path(plan_name, 'story').parent / (
            f'{plan_name}.json'
        )

    def _draft_namespace(self, namespace: str) -> None:
        """Validate ``namespace`` and refuse anything but ``'default'``.

        A plan draft is the staged document ``save_plan`` writes and ingest
        consumes, and both core paths serve ``DEFAULT_NAMESPACE`` only: this
        reference store therefore refuses any other namespace for the four
        draft operations, before any state is touched, exactly as the file and
        sqlite record views do.
        """
        validate_namespace(namespace)
        if namespace != DEFAULT_NAMESPACE:
            raise UnknownNamespaceError('unknown namespace')

    def put_plan_draft(self, namespace: str, plan_name: str, plan: dict) -> None:
        """Store a copy of ``plan`` as ``plan_name``'s draft, wholesale."""
        self._draft_namespace(namespace)
        validate_plan_name(plan_name)
        if not isinstance(plan, dict):
            raise TypeError('plan must be a dict')
        # Serialise before touching any state: a document that cannot be
        # stored must leave no trace behind.
        json.dumps(plan)
        with self._lock:
            self._record_undo(self._drafts, (namespace, plan_name))
            self._drafts[(namespace, plan_name)] = copy.deepcopy(plan)

    def get_plan_draft(self, namespace: str, plan_name: str) -> dict | None:
        """Return a copy of ``plan_name``'s draft, or ``None`` when absent.

        A draft this store holds itself answers from memory.  A draft this store
        does not hold is read from the plans directory through
        :func:`pipeline.persistence.read_plan_file`, the reader ``save_plan``
        stages it for and ingest refuses with -- the same transitional fallback
        ``SqliteRecords`` makes until stage C.  A malformed file therefore raises
        :class:`UnreadableRecordError` carrying that reader's reason text, never
        a bare ``JSONDecodeError``.
        """
        self._draft_namespace(namespace)
        validate_plan_name(plan_name)
        with self._lock:
            plan = self._drafts.get((namespace, plan_name))
            if plan is not None:
                return copy.deepcopy(plan)
        path = self._draft_path(plan_name)
        if not path.is_file():
            return None
        from pipeline import persistence

        plan, refusal = persistence.read_plan_file(path)
        if refusal is not None:
            raise UnreadableRecordError(refusal['error'])
        return plan

    def delete_plan_draft(self, namespace: str, plan_name: str) -> bool:
        """Delete ``plan_name``'s draft; return whether one was deleted."""
        self._draft_namespace(namespace)
        validate_plan_name(plan_name)
        with self._lock:
            if (namespace, plan_name) in self._drafts:
                self._record_undo(self._drafts, (namespace, plan_name))
                del self._drafts[(namespace, plan_name)]
                return True
        try:
            self._draft_path(plan_name).unlink()
        except FileNotFoundError:
            return False
        return True

    def list_plan_drafts(self, namespace: str) -> list[str]:
        """Return the sorted plans in ``namespace`` that have an ingestable draft."""
        self._draft_namespace(namespace)
        with self._lock:
            names = {
                plan_name
                for (stored_namespace, plan_name), plan in self._drafts.items()
                if stored_namespace == namespace and 'epics' in plan
            }
        for path in self._draft_path('probe').parent.glob('*.json'):
            if '.' in path.stem:
                continue
            try:
                document = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            if isinstance(document, dict) and 'epics' in document:
                names.add(path.stem)
        return sorted(names)

    # -- context snapshots (ADR 0012 decision 11) -----------------------
    def put_context_snapshot(
        self, namespace: str, digest: str, content: str, meta: dict
    ) -> None:
        """Store ``content`` under ``digest`` once; never overwrite it.

        The digest is checked against the content before any state is touched,
        and a digest that is already stored is left exactly as it is -- content
        and meta alike -- because a snapshot is immutable.
        """
        verify_context_digest(digest, content)
        validate_namespace(namespace)
        payload = snapshot_meta(meta, digest, content)
        with self._lock:
            key = (namespace, digest)
            if key in self._snapshots:
                return
            self._record_undo(self._snapshots, key)
            self._snapshots[key] = {'content': content, 'meta': payload}

    def get_context_snapshot(self, namespace: str, digest: str) -> dict | None:
        """Return a copy of ``{'content', 'meta'}`` for ``digest``, or ``None``."""
        address = context_address(digest)
        validate_namespace(namespace)
        with self._lock:
            found = self._snapshots.get((namespace, digest))
            if found is None:
                found = self._snapshots.get((namespace, address))
            return None if found is None else copy.deepcopy(found)

    # -- plan completion marker and report (ADR 0012 decision 3) --------
    def mark_plan_completed(self, namespace: str, plan_name: str) -> bool:
        """Create ``plan_name``'s completion marker; True only if created.

        The check-and-set runs under the store lock, so two racing callers
        cannot both report completion.
        """
        self._draft_namespace(namespace)
        validate_plan_name(plan_name)
        with self._lock:
            if (namespace, plan_name) in self._completed:
                return False
            self._record_undo(self._completed, (namespace, plan_name))
            self._completed[(namespace, plan_name)] = True
            return True

    def is_plan_completed(self, namespace: str, plan_name: str) -> bool:
        """Whether ``plan_name``'s completion marker exists."""
        self._draft_namespace(namespace)
        validate_plan_name(plan_name)
        with self._lock:
            return (namespace, plan_name) in self._completed

    def put_plan_report(self, namespace: str, plan_name: str, text: str) -> None:
        """Store ``plan_name``'s report, replacing the previous one wholesale."""
        self._draft_namespace(namespace)
        validate_plan_name(plan_name)
        if not isinstance(text, str):
            raise TypeError('plan report must be a str')
        with self._lock:
            self._record_undo(self._reports, (namespace, plan_name))
            self._reports[(namespace, plan_name)] = text

    def get_plan_report(self, namespace: str, plan_name: str) -> str | None:
        """Return ``plan_name``'s report, or ``None`` when it has none."""
        self._draft_namespace(namespace)
        validate_plan_name(plan_name)
        with self._lock:
            return self._reports.get((namespace, plan_name))

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
            self._record_undo(self._decisions, (namespace, plan_name))
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
            self._record_undo(self._usage, (namespace, plan_name))
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
            self._record_undo(self._journals, (namespace, plan_name, story_key))
            self._record_undo(
                self._journal_updated_at, (namespace, plan_name, story_key)
            )
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
            self._record_undo(self._journals, (namespace, plan_name, story_key))
            self._record_undo(
                self._journal_updated_at, (namespace, plan_name, story_key)
            )
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

    # -- per-channel outbox queues (ADR 0012 decision 3) ----------------
    def append_outbox(
        self, namespace: str, plan_name: str, channel: str, record: dict
    ) -> None:
        """Queue a deep copy of ``record`` on the ``(plan, channel)`` queue.

        The record is copied on the way in, so a caller that keeps mutating its
        dict cannot change a queue it already appended to.  A record that is
        not a dict is a ``TypeError``; one that no JSON encoder can serialise
        is refused BEFORE anything is stored, so a refused append leaves no
        entry behind.
        """
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        validate_channel(channel)
        if not isinstance(record, dict):
            raise TypeError('record must be a dict')
        json.dumps(record, default=str)
        with self._lock:
            self._record_undo(self._outboxes, (namespace, plan_name, channel))
            entry_ids = self._outboxes.setdefault(
                (namespace, plan_name, channel), []
            )
            self._outbox_id_counter += 1
            entry_ids.append((f'outbox-{self._outbox_id_counter}', copy.deepcopy(record)))

    def read_outbox(
        self,
        namespace: str,
        plan_name: str,
        channel: str,
        limit: int | None = None,
    ) -> list[dict]:
        """Return copies of the queue's entries oldest first, without removing."""
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        validate_channel(channel)
        with self._lock:
            entries = [
                {'id': entry_id, 'record': copy.deepcopy(record)}
                for entry_id, record in self._outboxes.get(
                    (namespace, plan_name, channel), []
                )
            ]
        if limit is not None:
            entries = entries[: max(0, limit)]
        return entries

    def ack_outbox(
        self, namespace: str, plan_name: str, channel: str, ids
    ) -> int:
        """Remove one entry per acked id; return how many were removed."""
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        validate_channel(channel)
        wanted = [str(acked) for acked in ids]
        with self._lock:
            self._record_undo(self._outboxes, (namespace, plan_name, channel))
            queue = self._outboxes.get((namespace, plan_name, channel), [])
            retained: list[tuple[str, dict]] = []
            removed = 0
            for entry_id, record in queue:
                if entry_id in wanted:
                    wanted.remove(entry_id)
                    removed += 1
                    continue
                retained.append((entry_id, record))
            self._outboxes[(namespace, plan_name, channel)] = retained
            return removed

    def outbox_summary(self, namespace: str, plan_name=None) -> dict:
        """Summarise the outbox queues this store holds."""
        from datetime import UTC, datetime

        from .outbox_health import _age_seconds

        validate_namespace(namespace)
        if plan_name is not None:
            try:
                validate_plan_name(plan_name)
            except InvalidRecordKeyError:
                return {"files": 0, "records": 0, "oldest_age_s": None}
        now = datetime.now(UTC)
        files = 0
        records = 0
        oldest = None
        with self._lock:
            queues = list(self._outboxes.items())
        for (ns, plan, _channel), entries in queues:
            if ns != namespace:
                continue
            if plan_name is not None and plan != plan_name:
                continue
            if not entries:
                continue
            files += 1
            records += len(entries)
            for _entry_id, record in entries:
                age = _age_seconds(record.get("ts") if isinstance(record, dict) else None, now)
                if age is not None and (oldest is None or age > oldest):
                    oldest = age
        return {"files": files, "records": records, "oldest_age_s": oldest}

    def list_outboxes(self, namespace: str) -> list[tuple[str, str]]:
        """Return the sorted ``(plan, channel)`` pairs that hold at least one entry."""
        validate_namespace(namespace)
        with self._lock:
            pairs = {
                (plan_name, channel)
                for (stored_namespace, plan_name, channel), queue in self._outboxes.items()
                if stored_namespace == namespace and queue
            }
        return sorted(pairs)

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

        Atomicity (ADR 0012 decision 9): the outermost block for this thread
        opens an undo log, and an exception that leaves it restores exactly the
        keys this thread wrote and re-raises; a normal exit commits.  A nested
        block never rolls back -- it re-raises so the enclosing block decides --
        and therefore commits with the outermost one.  The store lock is taken
        per operation, never around the ``yield``, so another thread writing the
        same plan is only ever delayed by one operation.
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
        state = self._transaction_state()
        state['depth'] += 1
        outermost = state['depth'] == 1
        try:
            yield True
        except BaseException:
            if outermost:
                self._rollback(state)
            raise
        else:
            if outermost:
                state['undo'].clear()
        finally:
            state['depth'] -= 1
            if state['depth'] == 0:
                state['undo'].clear()
            lock.release()

    # -- notification history ------------------------------------------
    def append_notification(
        self, namespace: str, plan_name: str, record: dict
    ) -> None:
        """Append one notification record to ``plan_name``'s history."""
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        validate_notification_record(record)
        with self._lock:
            self._record_undo(self._notifications, (namespace, plan_name))
            self._notifications.setdefault((namespace, plan_name), []).append(
                copy.deepcopy(record)
            )

    def get_notification_history(
        self, namespace: str, plan_name: str
    ) -> tuple[list[dict], int]:
        """Return ``plan_name``'s notification records and a malformed count.

        A memory store writes records, never a JSONL sidecar, so there is
        never a malformed line to report: the count is always ``0``.
        """
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        with self._lock:
            records = self._notifications.get((namespace, plan_name), ())
            return [copy.deepcopy(record) for record in records], 0

    def get_notification_lines(
        self, namespace: str, plan_name: str, limit: int = 100
    ) -> list[str]:
        """Return the last ``limit`` notification lines, each ``f'{ts} {message}'``."""
        records, _ = self.get_notification_history(namespace, plan_name)
        if limit <= 0:
            limit = 100
        return [
            f"{record['ts']} {record['message']}" for record in records[-limit:]
        ]

    def _plan_lock_for(self, namespace: str, plan_name: str) -> threading.RLock:
        """Return this pair's lock, creating it on first use."""
        with self._lock:
            lock = self._plan_locks.get((namespace, plan_name))
            if lock is None:
                lock = threading.RLock()
                self._plan_locks[(namespace, plan_name)] = lock
            return lock

    # -- transaction undo log ------------------------------------------
    def _transaction_state(self) -> dict:
        """Return this thread's transaction state, creating it on first use."""
        state = getattr(self._transactions, 'state', None)
        if state is None:
            state = {'depth': 0, 'undo': []}
            self._transactions.state = state
        return state

    def _record_undo(self, structure: dict, key) -> None:
        """Remember ``key``'s current value before this thread overwrites it.

        Called as the first action of every write, under :attr:`_lock`, while
        this thread holds a transaction.  The *first* write in a block wins: a
        key already in the log keeps its original value, so two puts inside one
        block still roll back to the value the block found.  A write made
        outside any transaction is its own atomic write and records nothing.
        """
        state = getattr(self._transactions, 'state', None)
        if state is None or state['depth'] <= 0:
            return
        for logged_structure, logged_key, _ in state['undo']:
            if logged_structure is structure and logged_key == key:
                return
        previous = structure.get(key, _ABSENT)
        state['undo'].append(
            (
                structure,
                key,
                _ABSENT if previous is _ABSENT else copy.deepcopy(previous),
            )
        )

    def _rollback(self, state: dict) -> None:
        """Restore every logged key, deleting the ones recorded absent."""
        undo = state['undo']
        state['undo'] = []
        with self._lock:
            for structure, key, previous in undo:
                if previous is _ABSENT:
                    structure.pop(key, None)
                else:
                    structure[key] = previous
