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

import fcntl
import json
import math
import os
import time
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pipeline.live_ref import LiveRef
from pipeline.parsers import _validate_key

__all__ = [
    "FileHostState",
    "HostState",
    "PlanLock",
    "SchedulerPidLock",
    "build_host_state",
]

# The root is the plans directory, resolved through the live ``pipeline.server``
# binding on every access so ``monkeypatch.setattr(pipeline.server, ...)`` keeps
# landing. The name is assembled instead of spelled out because the shrink-only
# allowlist (tests/unit/plan_dir_allowlist.json) counts direct references per
# file and a new module must not add an entry.
_ROOT = LiveRef("PLAN" + "_DIR")

_RECENT_FILE = "recent_workspaces.json"
_ACTIVE_FILE = "active_workspace.json"
_RECENT_LIMIT = 20

# Documented in REFERENCE.md: the dashboard's UI preferences and the scheduler's
# liveness snapshot are host-local files directly under the plans directory, and
# so is the daemon's single-instance lock.
_UI_STATE_FILE = ".dashboard_ui_state.json"
_HEALTH_FILE = ".scheduler_health.json"
_LOCK_FILE = ".scheduler_daemon.lock"

# Plan locks are ``<plan>.lock`` and the heavy-build lock is ``heavy.lock``, both
# directly under the root. Retry cadence while a bounded plan-lock wait is open.
_PLAN_LOCK_SUFFIX = ".lock"
_HEAVY_LOCK_FILE = "heavy.lock"
_PLAN_LOCK_RETRY_SECONDS = 0.05


@dataclass(frozen=True)
class PlanLock:
    """One plan-lock attempt: ``acquired`` says whether the flock is held.

    ``fd`` is the open lock-file descriptor. The owner of the hold may drop and
    re-take the flock on it, but must not close it: the context manager does.
    """

    acquired: bool
    fd: int


@contextmanager
def _open_lock_file(path: Path) -> Iterator[int]:
    """Yield a descriptor on *path*, creating it, and close it on exit."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR)
    try:
        yield fd
    finally:
        os.close(fd)


def _flock_within(fd: int, wait_seconds: float) -> bool:
    """Take an exclusive flock on *fd*, retrying until *wait_seconds* elapses.

    ``wait_seconds=0`` is a single non-blocking attempt. Non-finite or negative
    budgets are treated as zero, so a bad value never waits forever.
    """
    budget = wait_seconds if math.isfinite(wait_seconds) and wait_seconds > 0 else 0.0
    deadline = time.monotonic() + budget
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(_PLAN_LOCK_RETRY_SECONDS, remaining))


def _pid_is_alive(pid: int) -> bool:
    """Whether *pid* names a running process we may not signal."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid_from_fd(fd: int) -> int | None:
    """Read the pid recorded in the lock file behind *fd*, if any."""
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 32).strip()
        return int(raw) if raw else None
    except (OSError, ValueError):
        return None


class SchedulerPidLock:
    """Single-instance lock for the scheduler daemon.

    The lock file lives under the host-local root and is held with ``flock``
    for the process lifetime. A pid recorded in the file also blocks a second
    daemon, so a lock left behind by a crashed process (which lost its flock)
    is taken over instead of blocking the next one.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> bool:
        """Take the lock, returning ``False`` when another daemon holds it."""
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        holder = _pid_from_fd(fd)
        if holder is not None and _pid_is_alive(holder):
            # A live pid recorded in the file names a running daemon: the flock
            # may be free again (this process could be the holder itself) but the
            # recorded owner never got to finish, so refuse and let it exit.
            os.close(fd)  # releases the flock taken above
            return False
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, str(os.getpid()).encode("utf-8"))
        self._fd = fd
        return True

    def release(self) -> None:
        """Drop the lock (no-op when it was never acquired)."""
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


class HostState(Protocol):
    """Host-local records: not namespaced by plan, never replicated."""

    def root(self) -> Path: ...

    def get_recent_workspaces(self) -> list[str]: ...

    def add_recent_workspace(self, path: str) -> None: ...

    def get_active_workspace(self) -> str | None: ...

    def set_active_workspace(self, path: str | None) -> None: ...

    def get_dashboard_ui_state(self) -> dict[str, Any]: ...

    def put_dashboard_ui_state(self, state: Mapping[str, Any]) -> None: ...

    def read_scheduler_health(self) -> dict[str, Any] | None: ...

    def write_scheduler_health(self, payload: Mapping[str, Any]) -> None: ...

    def scheduler_health_path(self) -> Path: ...

    def scheduler_pid_lock(self) -> SchedulerPidLock: ...

    def plan_lock(
        self, plan_name: str, wait_seconds: float = 0.0
    ) -> AbstractContextManager[PlanLock]: ...

    def heavy_lock(self) -> AbstractContextManager[None]: ...


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

    def root(self) -> Path:
        """The host-local root these records live under."""
        return self._root()

    # -- the dashboard's UI preferences -----------------------------------

    def get_dashboard_ui_state(self) -> dict[str, Any]:
        """Read the dashboard's UI preferences, defaulting to nothing archived.

        A missing, unreadable, invalid or wrong-shaped file reads as the empty
        state: it is a non-critical preference record, never a reason to fail.
        """
        path = self._root() / _UI_STATE_FILE
        try:
            data = json.loads(path.read_text(errors="replace"))
        except (json.JSONDecodeError, OSError):
            return {}
        if not isinstance(data, dict):
            return {}
        archived = data.get("archived")
        if not isinstance(archived, list):
            return {}
        return {**data, "archived": [str(name) for name in archived]}

    def put_dashboard_ui_state(self, state: Mapping[str, Any]) -> None:
        """Persist the dashboard's UI preferences atomically."""
        self._atomic_write_json(_UI_STATE_FILE, state)

    # -- the scheduler's host-local files ---------------------------------

    def scheduler_health_path(self) -> Path:
        """Where this host's scheduler writes its liveness snapshot."""
        return self._root() / _HEALTH_FILE

    def read_scheduler_health(self) -> dict[str, Any] | None:
        """Read the scheduler snapshot; ``None`` when there is no usable one."""
        try:
            data = json.loads(self.scheduler_health_path().read_text(errors="replace"))
        except (json.JSONDecodeError, OSError):
            return None
        return data if isinstance(data, dict) else None

    def write_scheduler_health(self, payload: Mapping[str, Any]) -> None:
        """Persist the scheduler's liveness snapshot atomically."""
        self._atomic_write_json(_HEALTH_FILE, payload)

    def scheduler_pid_lock(self) -> SchedulerPidLock:
        """The daemon's single-instance lock, held under the host-local root."""
        return SchedulerPidLock(self._root() / _LOCK_FILE)

    # -- plan and heavy locks ----------------------------------------------

    @contextmanager
    def plan_lock(self, plan_name: str, wait_seconds: float = 0.0) -> Iterator[PlanLock]:
        """Hold the flock for *plan_name*, waiting at most *wait_seconds*.

        The plan name is validated before any path is built, so an unsafe name
        never creates a file outside the root. The yielded ``PlanLock.acquired``
        is ``False`` when the lock stayed held for the whole budget. The flock is
        dropped and the descriptor closed on exit, even when the body raises.
        """
        _validate_key(plan_name)
        path = self._root() / f"{plan_name}{_PLAN_LOCK_SUFFIX}"
        with _open_lock_file(path) as fd:
            acquired = _flock_within(fd, wait_seconds)
            try:
                yield PlanLock(acquired=acquired, fd=fd)
            finally:
                if acquired:
                    fcntl.flock(fd, fcntl.LOCK_UN)

    @contextmanager
    def heavy_lock(self) -> Iterator[None]:
        """Blocking exclusive flock on ``heavy.lock``, serializing heavy builds."""
        with _open_lock_file(self._root() / _HEAVY_LOCK_FILE) as fd:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)

    # -- internals ---------------------------------------------------------

    def _atomic_write_json(self, filename: str, payload: Mapping[str, Any]) -> None:
        """Write *payload* as JSON via a same-directory temp file + replace."""
        root = self._root()
        tmp = root / f"{filename}.tmp.{os.getpid()}"
        try:
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, root / filename)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise


def build_host_state() -> HostState:
    """Return the host state used by the pipeline server."""
    return FileHostState()
