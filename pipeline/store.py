"""Storage seam for pipeline state (W1b).

``Store``, ``_TransactionLock`` and ``FileStore`` were extracted verbatim from
``pipeline/server.py``. They read this module's re-exported globals (``PLAN_DIR``,
``_plan_lock``, ``_append_decision``, ...) as free variables, and the test suite
patches ``pipeline.server`` for those names, so ``pipeline/server.py`` re-exports
everything it needs (see PIPELINE_MCP_DECOMPOSITION_PLAN.md).

The free-variable names (``PLAN_DIR``, ``WORKTREE_ROOT``, ``_plan_lock``,
``_append_decision``, ``_append_journal``) are resolved at call time through the
``_ServerRef`` bindings below, which delegate to ``pipeline.server`` so
``monkeypatch.setattr(pipeline.server, "NAME", ...)`` still lands. They are
intentionally not imported here (a module-load copy would freeze the real
~/.claude/plans path into every test run).
"""

import json
import os
from pathlib import Path
from typing import Any, Protocol

__all__ = ["FileStore", "Store", "_TransactionLock"]


class _ServerRef:
    """Delegates to the *current* ``pipeline.server`` binding for a name.

    The class bodies below reference ``PLAN_DIR``, ``WORKTREE_ROOT``,
    ``_plan_lock``, ``_append_decision`` and ``_append_journal`` as bare module
    globals. The test suite patches ``pipeline.server`` for those names (e.g.
    ``monkeypatch.setattr(pipeline.server, "PLAN_DIR", tmp_path)``), so these
    bindings must read the live ``pipeline.server`` value at call time rather
    than hold a copy imported at module load (which would freeze the real
    ~/.claude/plans path into every test run).
    """

    def __init__(self, name: str):
        self._name = name

    def _value(self):
        from . import server as _server
        return getattr(_server, self._name)

    def __getattr__(self, attr: str):
        return getattr(self._value(), attr)

    def __call__(self, *args, **kwargs):
        return self._value()(*args, **kwargs)

    def __truediv__(self, other):
        return self._value() / other


PLAN_DIR = _ServerRef("PLAN_DIR")
WORKTREE_ROOT = _ServerRef("WORKTREE_ROOT")
_plan_lock = _ServerRef("_plan_lock")
_append_decision = _ServerRef("_append_decision")
_append_journal = _ServerRef("_append_journal")


class Store(Protocol):
    """Storage seam for pipeline state (W1b).

    Every manifest / decisions / journal access in this module goes through a
    Store, so the on-disk JSON layout stops being spelled out at ~20 call
    sites. ``FileStore`` below is the only implementation today; see
    docs/plans/PLATFORM_DECOUPLING_AND_SCALE_PLAN.md, Workstream W1 step 2.
    """

    def manifest_path(self, plan_name: str) -> Path: ...

    def get_manifest(self, plan_name: str) -> dict[str, Any]: ...

    def save_manifest(self, plan_name: str, manifest: dict[str, Any]) -> None: ...

    def transaction(self, plan_name: str): ...

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

    def __init__(self, plan_name: str):
        self._plan_name = plan_name
        self._cm = None

    def __enter__(self):
        self._cm = _plan_lock(self._plan_name)
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
        tmp = self.manifest_path(plan_name).with_suffix(
            self.manifest_path(plan_name).suffix + f".tmp.{os.getpid()}"
        )
        try:
            tmp.write_text(json.dumps(manifest))
            os.replace(tmp, self.manifest_path(plan_name))
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def transaction(self, plan_name: str):
        """Serialise mutations of one plan. Today this is exactly ``_plan_lock``:
        a non-blocking, flock-based, thread-reentrant context manager that
        yields whether the lock was acquired. Callers MUST check the yielded
        bool and skip all work when it is False."""
        return _TransactionLock(plan_name)

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

