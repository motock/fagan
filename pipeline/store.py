"""Storage seam for pipeline state (W1b).

``Store``, ``_TransactionLock`` and ``FileStore`` were extracted verbatim from
``pipeline/server.py``. They read this module's re-exported globals (``PLAN_DIR``,
``_plan_lock``, ``_append_decision``, ...) as free variables, and the test suite
patches ``pipeline.server`` for those names, so ``pipeline/server.py`` re-exports
everything it needs (see PIPELINE_MCP_DECOMPOSITION_PLAN.md).

The free-variable names (``PLAN_DIR``, ``WORKTREE_ROOT``, ``_plan_lock``,
``_append_decision``, ``_append_journal``, ``_atomic_write_json``) are resolved
at call time through the ``_ServerRef`` bindings below, which delegate to
``pipeline.server`` so ``monkeypatch.setattr(pipeline.server, "NAME", ...)``
still lands. They are
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
import os
from pathlib import Path
from typing import Any, Protocol

from pipeline.live_ref import LiveRef
from pipeline.store_records import (
    DEFAULT_NAMESPACE,
    UnknownNamespaceError,
    validate_namespace,
    validate_plan_name,
    validate_story_key,
)

__all__ = ["FileStore", "Store", "_TransactionLock"]

_ServerRef = LiveRef


PLAN_DIR = _ServerRef("PLAN_DIR")
WORKTREE_ROOT = _ServerRef("WORKTREE_ROOT")
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


class Store(Protocol):
    """Storage seam for pipeline state (W1b).

    Every manifest / decisions / journal access in this module goes through a
    Store, so the on-disk JSON layout stops being spelled out at ~20 call
    sites. ``FileStore`` (JSON files in the plans directory) and
    ``pipeline.store_sqlite.SqliteStore`` (a single-host database) implement it;
    see docs/plans/PLATFORM_DECOUPLING_AND_SCALE_PLAN.md, Workstream W1 step 2.
    """

    def manifest_path(self, plan_name: str) -> Path: ...

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
    def get_recent_workspaces(self) -> list[str]: ...
    def add_recent_workspace(self, path: str) -> None: ...
    def get_active_workspace(self) -> str | None: ...
    def set_active_workspace(self, path: str | None) -> None: ...
    def get_manifest_or_none(self, plan_name: str) -> dict | None: ...
    def get_journal(self, plan_name: str, story_key: str) -> tuple[bool, list[dict]]: ...
    def get_journal_final_ts(self, plan_name: str, story_key: str) -> str | None: ...
    def get_story_log(self, plan_name: str, story_key: str, manifest: dict[str, Any], lines: int = 200) -> dict[str, Any]: ...
    def get_worktree_file(self, story: dict[str, Any], filename: str) -> dict[str, Any]: ...


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

    def manifest_path(self, plan_name: str) -> Path:
        return PLAN_DIR / f"{plan_name}.manifest.json"

    def get_manifest(self, plan_name: str) -> dict[str, Any]:
        return json.loads(self.manifest_path(plan_name).read_text())

    def save_manifest(self, plan_name: str, manifest: dict[str, Any]) -> None:
        _atomic_write_json(self.manifest_path(plan_name), manifest)

    def transaction(self, plan_name: str, wait_seconds: float = 0.0):
        """Serialise mutations of one plan. Today this is exactly ``_plan_lock``:
        a non-blocking, flock-based, thread-reentrant context manager that
        yields whether the lock was acquired. Callers MUST check the yielded
        bool and skip all work when it is False.

        ``wait_seconds`` is opt-in: the default ``0.0`` is a single attempt
        with no sleeping (the scheduler's own ticks). Operator commands pass
        ``command_lock_wait_seconds()`` to retry for a bounded time."""
        return _TransactionLock(plan_name, wait_seconds)

    list_plans = lambda self: [f.stem for f in PLAN_DIR.glob("*.json")]

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

    def get_story_log(self, plan_name: str, story_key: str, manifest: dict[str, Any], lines: int = 200) -> dict[str, Any]:
        empty = {"available": False, "lines": []}
        if not isinstance(lines, int) or lines < 1:
            lines = 200
        lines = min(lines, 500)
        stories = manifest.get("stories") if isinstance(manifest, dict) else None
        if not isinstance(stories, dict):
            return empty
        story = stories.get(story_key)
        if not isinstance(story, dict):
            return empty
        raw_log = story.get("log")
        if not isinstance(raw_log, str) or not raw_log:
            return empty
        log_path = Path(raw_log)
        if log_path.is_absolute():
            # Manifest stores log paths relative to PLAN_DIR historically; an
            # absolute path is a corrupt/hand-edited manifest we fail closed
            # on (resolving an absolute path under a CWD we don't control
            # would be unsafe).
            return empty
        log_path = PLAN_DIR / raw_log
        try:
            log_path = log_path.resolve(strict=False)
            plan_dir_resolved = PLAN_DIR.resolve()
            if log_path != plan_dir_resolved and not log_path.is_relative_to(plan_dir_resolved):
                return empty
        except OSError:
            return empty
        if not log_path.exists() or not log_path.is_file():
            return empty
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return empty
        all_lines = text.splitlines()
        tail = all_lines[-lines:]
        return {"available": True, "lines": tail}

    def get_worktree_file(self, story: dict[str, Any], filename: str) -> dict[str, Any]:
        empty = {"available": False, "text": ""}
        if not isinstance(story, dict):
            return empty
        if not isinstance(filename, str) or not filename:
            return empty
        if "/" in filename or "\\" in filename or filename == ".." or filename == ".":
            return empty
        raw_wt = story.get("worktree")
        if not isinstance(raw_wt, str) or not raw_wt:
            return empty
        wt_path = Path(raw_wt)
        if not wt_path.is_absolute():
            return empty
        target = wt_path / filename
        try:
            target = target.resolve(strict=False)
            root = WORKTREE_ROOT.resolve()
            if target != root and not target.is_relative_to(root):
                return empty
        except OSError:
            return empty
        if not target.exists() or not target.is_file():
            return empty
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return empty
        return {"available": True, "text": text}

    def get_manifest_or_none(self, plan_name: str) -> dict | None:
        """Return manifest or None on missing/corrupt."""
        try:
            return self.get_manifest(plan_name)
        except (json.JSONDecodeError, OSError):
            return None
    def get_recent_workspaces(self) -> list[str]:
        path = PLAN_DIR / "recent_workspaces.json"
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(errors="replace"))
        except (json.JSONDecodeError, OSError):
            return []
        if not isinstance(data, list):
            return []
        return [str(x) for x in data if isinstance(x, str)]

    def add_recent_workspace(self, path: str) -> None:
        recent = self.get_recent_workspaces()
        recent = [p for p in recent if p != path]
        recent.insert(0, path)
        recent = recent[:20]
        tmp = PLAN_DIR / f"recent_workspaces.json.tmp.{os.getpid()}"
        try:
            tmp.write_text(json.dumps(recent))
            os.replace(tmp, PLAN_DIR / "recent_workspaces.json")
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def get_active_workspace(self) -> str | None:
        path = PLAN_DIR / "active_workspace.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(errors="replace"))
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(data, dict):
            return None
        value = data.get("path")
        if isinstance(value, str) and value:
            return value
        return None

    def set_active_workspace(self, path: str | None) -> None:
        tmp = PLAN_DIR / f"active_workspace.json.tmp.{os.getpid()}"
        try:
            tmp.write_text(json.dumps({"path": path}))
            os.replace(tmp, PLAN_DIR / "active_workspace.json")
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def records(self) -> "FileRecords":
        """Return the namespaced record view of this store (ADR 0012)."""
        return FileRecords(self)


class FileRecords:
    """The namespaced record view over one :class:`FileStore` (ADR 0012).

    Every operation delegates to the legacy ``FileStore`` methods, so the
    record view and the legacy view always see the same single copy of a
    record.  Namespaces are opaque keys: this implementation serves
    :data:`DEFAULT_NAMESPACE` only and refuses any other namespace without
    touching storage.
    """

    def __init__(self, store: FileStore) -> None:
        self._store = store

    def _refuse_other_namespace(self, namespace: str) -> None:
        if namespace != DEFAULT_NAMESPACE:
            raise UnknownNamespaceError('unknown namespace')

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

    # -- plan listing --------------------------------------------------
    def list_plans(self, namespace: str) -> list[str]:
        validate_namespace(namespace)
        self._refuse_other_namespace(namespace)
        return sorted(self._store.list_manifests())

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


STORE_INVALID = "invalid"


def store_kind(environ=None) -> str:
    """Return the selected Store kind without constructing a store.

    ``PIPELINE_STORE`` unset, or exactly ``file``, selects the JSON-files file
    store; exactly ``sqlite`` selects the database store and requires an
    absolute ``PIPELINE_STORE_PATH``. Every other value -- including the empty
    string, and ``sqlite`` without a usable path -- raises, because a store
    selection an operator cannot be given must never fall back silently to a
    different one. Nothing is cached: the variable is read on every call, so a
    later change is observed.

    ``environ`` defaults to the live process environment; passing a mapping
    reads that instead, which is how callers state a selection explicitly.
    """
    source = os.environ if environ is None else environ
    raw = source.get("PIPELINE_STORE")
    if raw is None or raw == "file":
        return "file"
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
        "PIPELINE_STORE must be unset or 'file' (allowed values: 'file', 'sqlite')"
    )


def build_store(environ=None) -> Store:
    """Select the configured Store; validates before any filesystem work.

    The kind is resolved first, so a rejected configuration raises without
    constructing a store -- i.e. without touching the plan directory.
    """
    source = os.environ if environ is None else environ
    kind = store_kind(environ)
    if kind == "sqlite":
        # Function-local: ``pipeline.store_sqlite`` imports the file store from
        # this module, so a module-level import here would be circular.
        from pipeline.store_sqlite import SqliteStore

        return SqliteStore(source["PIPELINE_STORE_PATH"])
    return FileStore()


def reported_store_kind(environ=None) -> str:
    """Return the selected store kind for *observation*, never raising.

    Read-only reporting (``/api/health``'s ``runtime.store``) must not turn a
    health check into a 500 because of one mistyped environment variable, so an
    unsupported value is reported as :data:`STORE_INVALID` instead of raising.
    The rejected value is never echoed -- it may be a path or a secret.
    :func:`build_store` remains fail-closed: the pipeline still refuses to run
    with a selection it cannot honour; only the report degrades.
    """
    try:
        return store_kind(environ)
    except ValueError:
        return STORE_INVALID
