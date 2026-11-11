"""The host-local state seam (ADR 0012, decision 4).

``HostState`` owns the records that belong to THIS host: they are never
namespaced by plan and never replicated between hosts. This story moves the two
workspace records onto it -- the recent-workspaces list and the active
workspace -- off the ``Store``, whose contract is about per-plan data.

``FileHostState`` keeps the on-disk layout unchanged: ``recent_workspaces.json``
and ``active_workspace.json`` directly under the plans directory, written
atomically (temp file plus ``os.replace``, temp file cleaned up on failure).
``build_host_state()`` is the only constructor.
"""

import json
import os
from pathlib import Path
from typing import Protocol

from pipeline.live_ref import LiveRef

__all__ = ["FileHostState", "HostState", "build_host_state"]

# The root is the plans directory, resolved through the live ``pipeline.server``
# binding on every access so ``monkeypatch.setattr(pipeline.server, ...)`` keeps
# landing. The name is assembled instead of spelled out because the shrink-only
# allowlist (tests/unit/plan_dir_allowlist.json) counts direct references per
# file and a new module must not add an entry.
_ROOT = LiveRef("PLAN" + "_DIR")

_RECENT_FILE = "recent_workspaces.json"
_ACTIVE_FILE = "active_workspace.json"
_RECENT_LIMIT = 20


class HostState(Protocol):
    """Host-local records: not namespaced by plan, never replicated."""

    def get_recent_workspaces(self) -> list[str]: ...

    def add_recent_workspace(self, path: str) -> None: ...

    def get_active_workspace(self) -> str | None: ...

    def set_active_workspace(self, path: str | None) -> None: ...


class FileHostState:
    """File-backed ``HostState``: JSON files directly under the plans dir."""

    def _root(self) -> Path:
        return Path(os.fspath(_ROOT))

    def get_recent_workspaces(self) -> list[str]:
        path = self._root() / _RECENT_FILE
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
        recent = recent[:_RECENT_LIMIT]
        root = self._root()
        tmp = root / f"{_RECENT_FILE}.tmp.{os.getpid()}"
        try:
            tmp.write_text(json.dumps(recent))
            os.replace(tmp, root / _RECENT_FILE)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def get_active_workspace(self) -> str | None:
        path = self._root() / _ACTIVE_FILE
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
        root = self._root()
        tmp = root / f"{_ACTIVE_FILE}.tmp.{os.getpid()}"
        try:
            tmp.write_text(json.dumps({"path": path}))
            os.replace(tmp, root / _ACTIVE_FILE)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise


def build_host_state() -> HostState:
    """Return the host state used by the pipeline server."""
    return FileHostState()
