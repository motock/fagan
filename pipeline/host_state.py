"""Prototype HostState (scratch, outside the repo) for test validation."""
import json
import os
from pathlib import Path
from typing import Protocol

from pipeline.live_ref import LiveRef

__all__ = ["FileHostState", "HostState", "build_host_state"]

_ROOT = LiveRef("PLAN_DIR")


class HostState(Protocol):
    def get_recent_workspaces(self) -> list[str]: ...
    def add_recent_workspace(self, path: str) -> None: ...
    def get_active_workspace(self) -> str | None: ...
    def set_active_workspace(self, path: str | None) -> None: ...


class FileHostState:
    def _root(self) -> Path:
        return Path(os.fspath(_ROOT))

    def get_recent_workspaces(self) -> list[str]:
        path = self._root() / "recent_workspaces.json"
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
        root = self._root()
        recent = [p for p in self.get_recent_workspaces() if p != path]
        recent.insert(0, path)
        recent = recent[:20]
        tmp = root / f"recent_workspaces.json.tmp.{os.getpid()}"
        try:
            tmp.write_text(json.dumps(recent))
            os.replace(tmp, root / "recent_workspaces.json")
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def get_active_workspace(self) -> str | None:
        path = self._root() / "active_workspace.json"
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
        tmp = root / f"active_workspace.json.tmp.{os.getpid()}"
        try:
            tmp.write_text(json.dumps({"path": path}))
            os.replace(tmp, root / "active_workspace.json")
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise


def build_host_state() -> HostState:
    return FileHostState()
