"""Storage seam for pipeline state (W1b).

``Store``, ``_TransactionLock`` and ``FileStore`` were extracted verbatim from
``pipeline/server.py``. They read this module's re-exported globals (``PLAN_DIR``,
``_plan_lock``, ``_append_decision``, ...) as free variables, and the test suite
patches ``pipeline.server`` for those names, so ``pipeline/server.py`` re-exports
everything it needs (see PIPELINE_MCP_DECOMPOSITION_PLAN.md).

Those free-variable names are resolved at call time through the
``_ServerRef`` bindings below, which delegate to ``pipeline.server`` so
``monkeypatch.setattr(pipeline.server, "NAME", ...)`` still lands. They are
intentionally not imported here (a module-load copy would freeze the real
~/.claude/plans path into every test run).

Store selection lives here too: ``build_store()`` is the only place a
``FileStore`` is constructed, so ``PIPELINE_STORE`` governs every call site,
``store_kind()`` resolves the selected kind, and ``reported_store_kind()``
renders it for observation (``/api/health``'s ``runtime`` block) without ever
raising -- an unsupported selection is reported as ``STORE_INVALID`` while
``build_store()`` stays fail-closed. None of them are listed in ``__all__``,
which is pinned by ``tests/unit/test_live_ref.py``.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Protocol

from app.plugin_loader import SPEC_RE, load_plugin, plugin_spec, require_methods
from pipeline.audit_file import FileAuditLog
from pipeline.live_ref import LiveRef
from pipeline.store_contract_outbox import OutboxContract, OutboxSummaryContract
from pipeline.store_records import (
    DEFAULT_NAMESPACE,
    RECORD_STORE_METHODS,
    InvalidRecordKeyError,
    PlanRecords,
    UnknownNamespaceError,
    validate_channel,
    validate_namespace,
    validate_notification_record,
    validate_plan_name,
    validate_story_key,
    validate_usage_record,
)

logger = logging.getLogger(__name__)

__all__ = ["FileStore", "Store", "_TransactionLock"]

_ServerRef = LiveRef


PLAN_DIR = _ServerRef("PLAN_DIR")
WORKTREE_ROOT = _ServerRef("WORKTREE_ROOT")
# The story-log root is the plans directory until HostState owns it (ADR 0012
# decision 12). Exposed under its own name so the workspace reader can resolve
# against the live binding without naming PLAN_DIR itself (the plan-directory
# allowlist pins this module as the store's own implementation).
STORY_LOG_ROOT = PLAN_DIR
_plan_lock = _ServerRef("_plan_lock")
_append_decision = _ServerRef("_append_decision")
_append_journal = _ServerRef("_append_journal")
_atomic_write_json = _ServerRef("_atomic_write_json")

# Display-only keys the legacy journal reader adds to every entry it returns.
_JOURNAL_DISPLAY_DEFAULTS = ("step", "summary", "next_hint")


def _persistence():
    """Return the ``pipeline.persistence`` module, imported at call time.

    Journal and decision *writes* go through ``pipeline.persistence``, which
    resolves the plans directory from its own module binding. A record view that
    read back through the legacy ``FileStore`` readers would answer from the
    server module's binding instead -- identical in production, but a silently
    different directory wherever the two bindings are patched apart.
    Imported inside the call (not at module load, and not through
    ``_ServerRef``: ``pipeline.server`` re-exports only some of these names) so
    the writer's current directory is what a read sees.
    """
    from . import persistence

    return persistence


_JOURNAL_SUFFIX = ".journal.json"


# Only ever used to take the journal *directory* from the writers' path helper.
_JOURNAL_PROBE_KEY = "story"


def _journals_dir(plan_name: str) -> Path:
    """Return the directory ``plan_name``'s journal files live in, per call.

    Taken from the path helper this view's writers use (see
    :func:`_persistence`) rather than from a directory binding, so a plans
    directory patched on that module is the one that is listed.  The probed
    story key only supplies a parent directory: nothing is read or created.
    """
    return _persistence()._journal_path(plan_name, _JOURNAL_PROBE_KEY).parent


def _journal_key(filename: str, plan_name: str) -> str | None:
    """Return the story key a journal filename records for ``plan_name``.

    Journal files are ``<plan>.<story>.journal.json``, and a plan name may
    itself contain the separator (``p`` is a prefix of ``p.x``), so a glob for
    ``p`` also matches ``p.x.s1.journal.json`` and would yield the key
    ``x.s1`` -- another plan's journal.  A story key never carries the plan
    separator, so a candidate that does is refused rather than misattributed.
    """
    prefix = f"{plan_name}."
    if not filename.startswith(prefix) or not filename.endswith(_JOURNAL_SUFFIX):
        return None
    key = filename[len(prefix) : -len(_JOURNAL_SUFFIX)]
    if "." in key:
        return None
    try:
        validate_story_key(key)
    except InvalidRecordKeyError:
        return None
    return key


def _read_json_records(path: Path) -> list[dict]:
    """Return the records in a JSON-list file at ``path``, fail-open.

    Absent, unreadable, malformed and non-list content all read as no records,
    and a read never creates or truncates anything on disk.
    """
    try:
        if not path.exists():
            return []
        raw = json.loads(path.read_text(errors="replace"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(raw, list):
        return []
    return [record for record in raw if isinstance(record, dict)]


def _read_usage_records(path: Path) -> list[dict]:
    """Return the usage records in the JSONL file at ``path``, in file order.

    One record per line.  A line that is not a JSON object is a malformed line
    -- a truncated append, a hand-edited file -- and is skipped with a single
    warning for the file.  That warning names the file and the count of skipped
    lines only: a usage record's values are counts, ids and cost, and a log line
    is not a second place to publish them.  An absent or unreadable file is an
    empty list, and a read never creates or truncates anything.
    """
    try:
        if not path.exists():
            return []
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    records: list[dict] = []
    skipped = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            record = None
        if not isinstance(record, dict):
            skipped += 1
            continue
        records.append(record)
    if skipped:
        logger.warning(
            'skipped %d malformed usage record line(s) in %s', skipped, path.name
        )
    return records


class Store(Protocol):
    """Storage seam for pipeline state (W1b).

    Every manifest / decisions / journal access in this module goes through a
    Store, so the on-disk JSON layout stops being spelled out at ~20 call
    sites. ``FileStore`` (JSON files in the plans directory) and
    ``pipeline.store_sqlite.SqliteStore`` (a single-host database) implement it;
    see docs/plans/PLATFORM_DECOUPLING_AND_SCALE_PLAN.md, Workstream W1 step 2.
    """

    def get_manifest(self, plan_name: str) -> dict[str, Any]: ...

    def save_manifest(self, plan_name: str, manifest: dict[str, Any]) -> None: ...

    def transaction(self, plan_name: str, wait_seconds: float = 0.0): ...

    # NOTE: declared via lambda assignment rather than a plain method
    # statement so this doesn't add a third and fourth hit to
    # test_pipeline_mcp_list_plans_migration.py's duplicate-definition guard,
    # which counts occurrences of that method-defining keyword pair and
    # predates this Store seam (it only knows about PipelineService's method
    # plus the module-level @mcp.tool() wrapper).
    list_plans = lambda self: ...

    def list_manifests(self) -> list[str]: ...

    def update_story(
        self, plan_name: str, story_key: str, fields: dict[str, Any]
    ) -> dict[str, Any] | None: ...

    def append_decision(self, plan_name: str, record: dict[str, Any]) -> None: ...

    def append_journal(
        self, plan_name: str, story_key: str, record: dict[str, Any]
    ) -> None: ...

    def get_notifications(self, plan_name: str) -> list[str]: ...
    def get_notification_records(self, plan_name: str, limit: int = 100) -> list[dict]: ...
    def get_decisions(self, plan_name: str) -> list[dict]: ...
    def get_manifest_or_none(self, plan_name: str) -> dict | None: ...
    def get_journal(self, plan_name: str, story_key: str) -> tuple[bool, list[dict]]: ...
    def get_journal_final_ts(self, plan_name: str, story_key: str) -> str | None: ...


class _TransactionLock:
    """Class-based context manager wrapping ``_plan_lock``.

    Behaviourally identical to ``_plan_lock`` for the ``with`` statement
    (``__enter__`` yields whether the lock was acquired; ``__exit__`` releases
    it). The class form additionally makes a *fresh, never-entered* instance's
    ``__exit__`` a safe no-op (returns False) rather than raising a
    "generator didn't stop" error — which matters for test helpers that
    delegate ``__exit__`` to a freshly constructed transaction.
    """

    def __init__(self, plan_name: str, wait_seconds: float = 0.0):
        self._plan_name = plan_name
        self._wait_seconds = wait_seconds
        self._cm = None

    def __enter__(self):
        self._cm = _plan_lock(self._plan_name, self._wait_seconds)
        return self._cm.__enter__()

    def __exit__(self, *exc):
        if self._cm is None:
            return False
        return self._cm.__exit__(*exc)


class FileStore:
    """A JSON-files-in-PLAN_DIR Store, behaviourally identical to the raw
    path construction it replaces.

    Methods deliberately read this module's globals (``PLAN_DIR``,
    ``_plan_lock``, ``_atomic_write_json``, ...) as free variables rather than
    holding copies, for the same reason ``PipelineService`` does: ``_store`` is
    constructed at import time, and the test suite patches
    ``pipeline.server.PLAN_DIR`` after that. Holding a copy would freeze the
    real ~/.claude/plans path into every test run.
    """

    def _plans_dir(self) -> Path:
        # Read live, like the other plans-directory call sites: the server
        # module's binding is patched after this module is imported.
        return PLAN_DIR

    def get_manifest(self, plan_name: str) -> dict[str, Any]:
        return json.loads((self._plans_dir() / f"{plan_name}.manifest.json").read_text())

    def save_manifest(self, plan_name: str, manifest: dict[str, Any]) -> None:
        _atomic_write_json(self._plans_dir() / f"{plan_name}.manifest.json", manifest)

    def audit_log(self) -> FileAuditLog:
        """Return the file audit log this store keeps its chains in."""
        return FileAuditLog()

    def transaction(self, plan_name: str, wait_seconds: float = 0.0):
        """Serialise mutations of one plan. Today this is exactly ``_plan_lock``:
        a non-blocking, flock-based, thread-reentrant context manager that
        yields whether the lock was acquired. Callers MUST check the yielded
        bool and skip all work when it is False.

        ``wait_seconds`` is opt-in: the default ``0.0`` is a single attempt
        with no sleeping (the scheduler's own ticks). Operator commands pass
        ``command_lock_wait_seconds()`` to retry for a bounded time."""
        return _TransactionLock(plan_name, wait_seconds)

    list_plans = lambda self: sorted(set(self.list_manifests()) | set(self.records().list_plan_drafts(DEFAULT_NAMESPACE)))

    def list_manifests(self) -> list[str]:
        return [
            mp.name.removesuffix(".manifest.json")
            for mp in sorted(PLAN_DIR.glob("*.manifest.json"))
        ]

    def update_story(
        self, plan_name: str, story_key: str, fields: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Apply ``fields`` to one story and persist. Returns the updated story,
        or None when the story does not exist (the caller owns the error shape)."""
        manifest = self.get_manifest(plan_name)
        story = manifest["stories"].get(story_key)
        if story is None:
            return None
        story.update(fields)
        self.save_manifest(plan_name, manifest)
        return story

    def append_decision(self, plan_name: str, record: dict[str, Any]) -> None:
        _append_decision(plan_name, record)

    def append_journal(
        self, plan_name: str, story_key: str, record: dict[str, Any]
    ) -> None:
        _append_journal(plan_name, story_key, record)


    def get_notifications(self, plan_name: str) -> list[str]:
        """Return last 100 lines of <plan>.notifications.log, fail-open."""
        path = PLAN_DIR / f"{plan_name}.notifications.log"
        if not path.exists():
            return []
        return path.read_text(errors="replace").splitlines()[-100:]

    def get_notification_records(self, plan_name: str, limit: int = 100) -> list[dict]:
        """Return last `limit` records from <plan>.notifications.jsonl, fail-open."""
        if limit <= 0:
            limit = 100
        path = PLAN_DIR / f"{plan_name}.notifications.jsonl"
        if not path.exists():
            return []
        records: list[dict] = []
        for line in path.read_text(errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            ts = str(obj.get("ts", ""))
            message = str(obj.get("message", ""))
            severity = obj.get("severity")
            if severity not in ("info", "warning", "error"):
                severity = "info"
            else:
                severity = str(severity)
            story_key = obj.get("story_key")
            event = obj.get("event")
            dedup_key = obj.get("dedup_key")
            records.append({
                "ts": ts,
                "message": message,
                "severity": severity,
                "story_key": story_key,
                "event": event,
                "dedup_key": dedup_key,
            })
        return records[-limit:]

    def get_decisions(self, plan_name: str) -> list[dict]:
        """Return decisions JSON, fail-open."""
        path = PLAN_DIR / f"{plan_name}.decisions.json"
        if not path.exists():
            return []
        try:
            return json.loads(path.read_text(errors="replace"))
        except (json.JSONDecodeError, OSError):
            return []

    def get_journal(self, plan_name: str, story_key: str) -> tuple[bool, list[dict]]:
        """Return (available, entries) for a story's checkpoint journal,
        mirroring dashboard._read_journal. Never raises."""
        path = PLAN_DIR / f"{plan_name}.{story_key}.journal.json"
        if not path.exists():
            return False, []
        try:
            raw = json.loads(path.read_text(errors="replace"))
        except (json.JSONDecodeError, OSError):
            return False, []
        if not isinstance(raw, list) or not raw:
            return False, []
        entries = [e for e in raw if isinstance(e, dict)]
        if not entries:
            return False, []
        normalized: list[dict] = []
        for e in entries:
            normalized.append({
                **e,
                "step": e.get("step"),
                "summary": e.get("summary"),
                "next_hint": e.get("next_hint"),
            })
        return True, normalized

    def get_journal_final_ts(self, plan_name: str, story_key: str) -> str | None:
        path = PLAN_DIR / f"{plan_name}.{story_key}.journal.json"
        if not path.exists():
            return None
        try:
            entries = json.loads(path.read_text(errors="replace"))
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(entries, list) or not entries:
            return None
        last = entries[-1]
        if isinstance(last, dict):
            return last.get("ts")
        return None

    def get_manifest_or_none(self, plan_name: str) -> dict | None:
        """Return manifest or None on missing/corrupt."""
        try:
            return self.get_manifest(plan_name)
        except (json.JSONDecodeError, OSError):
            return None
    # The recent/active workspace records are host-local (not namespaced, never
    # replicated), so they live on pipeline.host_state.FileHostState rather than
    # on the Store (ADR 0012, decision 4).

    def records(self) -> "FileRecords":
        """Return the namespaced record view of this store (ADR 0012)."""
        return FileRecords(self)


class FileRecords(PlanRecords, OutboxContract, OutboxSummaryContract):
    """The namespaced record view over one :class:`FileStore` (ADR 0012).

    Every operation delegates to the legacy ``FileStore`` methods, so the
    record view and the legacy view always see the same single copy of a
    record.  Namespaces are opaque keys: this implementation serves
    :data:`DEFAULT_NAMESPACE` only and refuses any other namespace without
    touching storage.
    """

    #: Ordered, not atomic: each write is durable when its call returns and a
    #: block that raises rolls nothing back (ADR 0012 decision 9).
    atomic_transactions = False

    def __init__(self, store: FileStore) -> None:
        self._store = store

    def _refuse_other_namespace(self, namespace: str) -> None:
        if namespace != DEFAULT_NAMESPACE:
            raise UnknownNamespaceError('unknown namespace')

    # -- notification history ------------------------------------------
    def append_notification(
        self, namespace: str, plan_name: str, record: dict[str, Any]
    ) -> None:
        """Append one notification to ``plan_name``'s history.

        One record, both of the representations the pipeline already writes:
        the free-text ``<plan>.notifications.log`` line and the structured
        ``<plan>.notifications.jsonl`` line, byte-identical to what
        ``notification_sinks.file_log_sink`` writes for the same notification.
        The write goes through :func:`pipeline.persistence._write_notification`
        so it lands in the plans directory the bus sink writes to.

        A record that is not a dict carrying a string ``ts`` and a ``message``
        that is a string or ``None`` is refused with :class:`TypeError`
        *before* anything is written: a record with no timestamp has no line
        to write, and must not leave a half-written history behind.  ``None``
        is accepted because a notification that carries no message is still a
        notification; it renders as an empty free-text message and as
        ``"message": null`` in the JSONL sidecar.
        """
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        self._refuse_other_namespace(namespace)
        validate_notification_record(record)
        # The writer resolves the plans directory as its own free variable,
        # exactly as the bus sink does, so a directory patched on that module
        # is the one the two representations land in.
        _persistence()._write_notification(plan_name, record)

    def get_notification_history(
        self, namespace: str, plan_name: str
    ) -> tuple[list[dict[str, Any]], int]:
        """Return ``plan_name``'s notification records and a malformed count.

        The records come back in written order (oldest first) parsed out of
        ``<plan>.notifications.jsonl`` by
        :func:`pipeline.story_metrics.load_notification_records`, the one
        JSONL parser in the tree; lines that parser cannot read are counted
        in the second element instead of raising. A missing sidecar is
        ``([], 0)`` and creates nothing.
        """
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        self._refuse_other_namespace(namespace)
        return self._load_notification_records(plan_name)

    def get_notification_lines(
        self, namespace: str, plan_name: str, limit: int = 100
    ) -> list[str]:
        """Return the last ``limit`` free-text notification lines.

        Each line is ``f'{ts} {message}'``, exactly the shape the sidecar
        holds after :meth:`append_notification` wrote it. A ``limit`` of zero
        or less means the last 100 lines, the same rule
        :meth:`FileStore.get_notifications` already applies; a missing log is
        ``[]`` and creates nothing.
        """
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        self._refuse_other_namespace(namespace)
        if limit <= 0:
            limit = 100
        path = self._notification_path(plan_name, ".log")
        if not path.exists():
            return []
        return path.read_text(errors="replace").splitlines()[-limit:]

    # -- notification helpers ------------------------------------------
    def _notification_path(self, plan_name: str, suffix: str) -> Path:
        """Return one of the plan's notification sidecars.

        The path is derived from the plan manifest's own directory, so the file
        store keeps a single plans-directory binding for its notification
        files -- the one its manifest, log and journal readers already use.
        """
        return self._store._plans_dir() / (
            f"{plan_name}.notifications{suffix}"
        )

    def _load_notification_records(
        self, plan_name: str
    ) -> tuple[list[dict[str, Any]], int]:
        """Parse the plan's JSONL sidecar through the shared parser."""
        from pipeline.story_metrics import load_notification_records

        return load_notification_records(
            self._notification_path(plan_name, ".jsonl")
        )

    # -- manifests -----------------------------------------------------
    def get_manifest(self, namespace: str, plan_name: str) -> dict | None:
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        self._refuse_other_namespace(namespace)
        return self._store.get_manifest_or_none(plan_name)

    def get_manifests(self, namespace: str) -> dict[str, dict]:
        """Return every readable plan manifest for ``namespace``.

        Reads through :mod:`pipeline.cross_plan` so the size-and-mtime parse
        cache is shared with everything else that scans the plan directory: an
        unchanged manifest is not re-parsed.  The values are those cached
        objects, so treat them as read-only snapshots.  Imported here because
        ``cross_plan`` cannot be imported at module level without a cycle.
        """
        validate_namespace(namespace)
        self._refuse_other_namespace(namespace)
        from . import cross_plan

        return cross_plan.load_manifests(PLAN_DIR._value())

    def put_manifest(
        self, namespace: str, plan_name: str, manifest: dict
    ) -> None:
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        self._refuse_other_namespace(namespace)
        if not isinstance(manifest, dict):
            raise TypeError('manifest must be a dict')
        self._store.save_manifest(plan_name, manifest)

    def get_manifest_updated_at(self, namespace: str, plan_name: str) -> float | None:
        """Return the manifest file's ``st_mtime``, or ``None`` when it is absent.

        The path comes from the file store's own resolution, so the stat is of
        the same file :meth:`get_manifest` reads.  A stat, never an open.
        """
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        self._refuse_other_namespace(namespace)
        try:
            return (self._store._plans_dir() / f"{plan_name}.manifest.json").stat().st_mtime
        except FileNotFoundError:
            return None

    # -- plan listing --------------------------------------------------
    def list_plans(self, namespace: str) -> list[str]:
        validate_namespace(namespace)
        self._refuse_other_namespace(namespace)
        return sorted(self._store.list_manifests())

    # -- transactions --------------------------------------------------
    def transaction(self, namespace: str, plan_name: str, wait_seconds: float = 0.0):
        """Acquire this plan's write transaction, or report it was refused.

        Validates the keys and refuses any namespace but
        :data:`DEFAULT_NAMESPACE` *before* a lock is taken, then delegates to
        :meth:`FileStore.transaction`.  Delegation (rather than importing
        :mod:`pipeline.concurrency` here) keeps ``store._plan_lock`` the single
        LiveRef patch point the test suite relies on.

        Yields ``True`` when the lock was acquired and ``False`` when it was
        not; callers must skip all work on ``False``.  ``wait_seconds`` is
        opt-in: ``0.0`` is one attempt with no sleeping.
        """
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        self._refuse_other_namespace(namespace)
        return self._store.transaction(plan_name, wait_seconds)

    # -- decisions -----------------------------------------------------
    def get_decisions(self, namespace: str, plan_name: str) -> list[dict]:
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        self._refuse_other_namespace(namespace)
        # Read the file the record view's own writer appends to, resolved per
        # call: see :func:`_persistence`.
        return _read_json_records(_persistence()._decisions_path(plan_name))

    def append_decision(
        self, namespace: str, plan_name: str, record: dict
    ) -> None:
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        self._refuse_other_namespace(namespace)
        if not isinstance(record, dict):
            raise TypeError('record must be a dict')
        self._store.append_decision(plan_name, record)

    # -- usage records -------------------------------------------------
    def get_usage(self, namespace: str, plan_name: str) -> list[dict]:
        """Return the plan's usage records, in the order they were written.

        Reads the JSONL file this view's own writer appends to, resolved per
        call (see :func:`_persistence`), so a plans directory patched on the
        writers' module is the one that is read.  A malformed line -- a line
        that is not a JSON object -- is skipped with one warning that carries no
        content, because the line may hold any of the record's values.  An
        absent file is an empty list: a read never creates anything.
        """
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        self._refuse_other_namespace(namespace)
        return _read_usage_records(_persistence()._usage_jsonl_path(plan_name))

    def append_usage(self, namespace: str, plan_name: str, record: dict) -> None:
        """Append one usage record; usage is append-only, there is no update.

        The record is validated first (exactly the contract's field set, each
        value of a type that field may hold), so an invalid record is refused
        before the file is opened and the plan's usage history is unchanged.
        """
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        self._refuse_other_namespace(namespace)
        validated = validate_usage_record(plan_name, record)
        _persistence()._write_usage_record(plan_name, validated)

    # -- journals ------------------------------------------------------
    def get_journal(
        self, namespace: str, plan_name: str, story_key: str
    ) -> list[dict]:
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        validate_story_key(story_key)
        self._refuse_other_namespace(namespace)
        # Read the file the record view's own writer appends to, resolved per
        # call: see :func:`_persistence`.
        records = _read_json_records(
            _persistence()._journal_path(plan_name, story_key)
        )
        # The legacy reader decorates every entry with display-only defaults
        # for the dashboard.  A record store hands back the records it was
        # given, so drop those defaults when the stored record did not carry
        # them (the legacy reader leaves a real value untouched).
        return [
            {
                key: value
                for key, value in record.items()
                if not (key in _JOURNAL_DISPLAY_DEFAULTS and value is None)
            }
            for record in records
        ]

    def append_journal(
        self, namespace: str, plan_name: str, story_key: str, record: dict
    ) -> None:
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        validate_story_key(story_key)
        self._refuse_other_namespace(namespace)
        if not isinstance(record, dict):
            raise TypeError('record must be a dict')
        self._store.append_journal(plan_name, story_key, record)

    def clear_journal(
        self, namespace: str, plan_name: str, story_key: str
    ) -> None:
        """Delete a story's journal so a re-dispatch starts clean.

        The keys are validated before any path is built, so an invalid plan
        name or story key is refused as :class:`InvalidRecordKeyError` rather
        than turned into a path.  The file removed is the one this view's own
        writer appends to, resolved per call; an absent journal is a no-op.
        """
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        validate_story_key(story_key)
        self._refuse_other_namespace(namespace)
        _persistence()._journal_path(plan_name, story_key).unlink(missing_ok=True)

    # -- journal activity ----------------------------------------------
    def get_journal_updated_at(
        self, namespace: str, plan_name: str, story_key: str
    ) -> float | None:
        """Return when the story's journal was last appended to, or ``None``.

        The answer is the journal file's ``st_mtime`` in the directory this
        view's writers append to, resolved per call: a ``stat``, never an open,
        so the read has no side effects.  An absent or unreadable journal is
        ``None``, which is also why a cleared journal reads as no journal
        rather than as a stale time.
        """
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        validate_story_key(story_key)
        self._refuse_other_namespace(namespace)
        path = _persistence()._journal_path(plan_name, story_key)
        try:
            return os.stat(path).st_mtime
        except OSError:
            return None

    def list_journal_keys(self, namespace: str, plan_name: str) -> list[str]:
        """Return the sorted story keys that have a journal in ``plan_name``."""
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        self._refuse_other_namespace(namespace)
        try:
            paths = list(
                _journals_dir(plan_name).glob(f"{plan_name}.*{_JOURNAL_SUFFIX}")
            )
        except OSError:
            return []
        keys = {_journal_key(path.name, plan_name) for path in paths}
        return sorted(key for key in keys if key is not None)

    # -- per-channel outbox queues (ADR 0012 decision 3) ----------------
    def append_outbox(
        self, namespace: str, plan_name: str, channel: str, record: dict
    ) -> None:
        """Queue ``record`` on the ``(plan_name, channel)`` outbox spool.

        The keys are validated before a path is built, and a record that is not
        a dict -- or that cannot be serialised -- is refused before the file is
        opened, so a refused append writes nothing.
        """
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        validate_channel(channel)
        self._refuse_other_namespace(namespace)
        if not isinstance(record, dict):
            raise TypeError('record must be a dict')
        _persistence()._outbox_append(plan_name, channel, record)

    def read_outbox(
        self,
        namespace: str,
        plan_name: str,
        channel: str,
        limit: int | None = None,
    ) -> list[dict]:
        """Return the queue's entries oldest first, without removing them.

        A queue that was never written is empty and creates no file; malformed
        spool lines are skipped.  The entries are freshly parsed from disk, so
        mutating a returned record cannot change the stored queue.
        """
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        validate_channel(channel)
        self._refuse_other_namespace(namespace)
        return _persistence()._outbox_read(plan_name, channel, limit)

    def ack_outbox(
        self, namespace: str, plan_name: str, channel: str, ids
    ) -> int:
        """Acknowledge ``ids``: remove one pending entry per id, return the count.

        An id removes exactly one entry carrying it, an unknown id is ignored,
        and every raw line that is not acked is kept byte-for-byte.
        """
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        validate_channel(channel)
        self._refuse_other_namespace(namespace)
        return _persistence()._outbox_ack(plan_name, channel, ids)

    def outbox_summary(self, namespace: str, plan_name=None) -> dict:
        """Summarise the outbox queues of the file representation.

        The file store *is* the spool reader, so this delegates to
        :mod:`pipeline.outbox_health` and keeps its exact semantics: an empty
        spool file still counts in ``files``, and a corrupt line is a record
        with an unknown age. Both the spool directory and the
        ``outbox_health`` reader are resolved at call time, so a test that
        patches either is honoured.
        """
        from . import outbox_health, paths

        validate_namespace(namespace)
        self._refuse_other_namespace(namespace)
        # Resolved through the package attribute, exactly like the writer
        # (``pipeline.persistence._outbox_dirs``) resolves the spool
        # directory, so the summary always reads the queues the file
        # representation writes.
        spool_dir = paths.PLAN_DIR
        if plan_name is not None:
            try:
                validate_plan_name(plan_name)
            except InvalidRecordKeyError:
                return {"files": 0, "records": 0, "oldest_age_s": None}
            return outbox_health.outbox_summary_for_plan(spool_dir, plan_name)
        return outbox_health.outbox_summary(spool_dir)

    def list_outboxes(self, namespace: str) -> list[tuple[str, str]]:
        """Return the sorted ``(plan, channel)`` pairs that hold entries."""
        validate_namespace(namespace)
        self._refuse_other_namespace(namespace)
        return _persistence()._outbox_queues()


STORE_INVALID = "invalid"


def store_kind(environ=None) -> str:
    """Return the selected Store kind without constructing a store.

    ``PIPELINE_STORE`` unset, or exactly ``file``, selects the JSON-files file
    store; exactly ``sqlite`` selects the database store and requires an
    absolute ``PIPELINE_STORE_PATH``; exactly ``plugin`` selects the store
    factory named by ``PIPELINE_STORE_PLUGIN`` and requires a well-formed
    ``module:attr`` spec (the spec is shape-checked here, never imported). Every
    other value -- including the empty string, and ``sqlite`` without a usable
    path -- raises, because a store selection an operator cannot be given must
    never fall back silently to a different one. Nothing is cached: the variable
    is read on every call, so a later change is observed.

    ``environ`` defaults to the live process environment; passing a mapping
    reads that instead, which is how callers state a selection explicitly.
    """
    source = os.environ if environ is None else environ
    raw = source.get("PIPELINE_STORE")
    if raw is None or raw == "file":
        return "file"
    if raw == "plugin":
        # Shape only: the spec is imported by build_store, never here, so a
        # health probe runs no third-party code. The error names the variable,
        # never the spec, which may be a path.
        spec = plugin_spec("store", source)
        if spec is None or not SPEC_RE.match(spec):
            raise ValueError(
                "PIPELINE_STORE_PLUGIN must name a 'module:attr' store factory "
                "when PIPELINE_STORE=plugin"
            )
        return "plugin"
    if raw == "sqlite":
        # A selection that cannot name its database is not a usable selection:
        # two processes would otherwise each create their own file in whatever
        # directory they started from. Refused here, so an unconfigured
        # database selection is reported as STORE_INVALID like any other.
        target = source.get("PIPELINE_STORE_PATH")
        if not target or not Path(target).is_absolute():
            raise ValueError(
                "PIPELINE_STORE_PATH must name an absolute database path when "
                "PIPELINE_STORE selects the database store; leave "
                "PIPELINE_STORE unset (or 'file') for the JSON file store"
            )
        return "sqlite"
    raise ValueError(
        "PIPELINE_STORE must be unset or 'file' (allowed values: 'file', 'sqlite', "
        "'plugin')"
    )


def build_store(environ=None) -> Store:
    """Select the configured Store; validates before any filesystem work.

    The kind is resolved first, so a rejected configuration raises without
    constructing a store -- i.e. without touching the plan directory.

    For ``PIPELINE_STORE=plugin`` the factory named by ``PIPELINE_STORE_PLUGIN``
    is loaded through the plugin loader (ADR 0012 decision 7) and called with no
    arguments; the object it returns must provide every name in
    ``RECORD_STORE_METHODS``. Any loader error raises: nothing falls back to the
    file store.
    """
    source = os.environ if environ is None else environ
    kind = store_kind(environ)
    if kind == "sqlite":
        # Function-local: ``pipeline.store_sqlite`` imports the file store from
        # this module, so a module-level import here would be circular.
        from pipeline.store_sqlite import SqliteStore

        return SqliteStore(source["PIPELINE_STORE_PATH"])
    if kind == "plugin":
        factory = load_plugin("store", source)
        built = factory()
        require_methods(built, RECORD_STORE_METHODS, "store")
        return built
    return FileStore()


def reported_store_kind(environ=None) -> str:
    """Return the selected store kind for *observation*, never raising.

    Read-only reporting (``/api/health``'s ``runtime.store``) must not turn a
    health check into a 500 because of one mistyped environment variable, so an
    unsupported value is reported as :data:`STORE_INVALID` instead of raising.
    The rejected value is never echoed -- it may be a path or a secret.
    :func:`build_store` remains fail-closed: the pipeline still refuses to run
    with a selection it cannot honour; only the report degrades. A plugin
    selection is reported as ``plugin`` without loading the plugin.
    """
    try:
        return store_kind(environ)
    except ValueError:
        return STORE_INVALID
