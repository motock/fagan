"""Injected host state and record store for tests of the health and metrics readers.

``health_extras`` and ``metrics_exposition.collect`` take the host state and the
store as arguments. A test that used to point them at a plans directory now
passes :func:`host_at` and :func:`store_at` over the same directory, so the
scheduler snapshot, the spool files, the manifests and the audit chains are
read exactly as before, just through the injected objects.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pipeline.audit_file import FileAuditLog
from pipeline.host_state import FileHostState
from pipeline.store_file_outbox import outbox_summary


def host_at(root: Path | str) -> FileHostState:
    """A host state whose host-local files live directly under *root*."""
    return FileHostState(root=Path(root))


class DirectoryRecords:
    """The record view of a directory: ``<name>.manifest.json`` files and outbox spools."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def list_plans(self, namespace: str) -> list[str]:
        return sorted(
            path.name.removesuffix(".manifest.json")
            for path in self._root.glob("*.manifest.json")
        )

    def get_manifest(self, namespace: str, plan_name: str) -> Any:
        try:
            return json.loads(
                (self._root / f"{plan_name}.manifest.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return None

    def outbox_summary(self, namespace: str) -> dict:
        return outbox_summary(self._root)


class DirectoryStore:
    """A store over a directory: its record view and its file audit chains."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def records(self) -> DirectoryRecords:
        return DirectoryRecords(self._root)

    def audit_log(self) -> FileAuditLog:
        return FileAuditLog(self._root)


def store_at(root: Path | str) -> DirectoryStore:
    """A store whose plans, spools and audit chains live directly under *root*."""
    return DirectoryStore(Path(root))
