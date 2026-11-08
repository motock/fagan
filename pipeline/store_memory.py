"""In-memory reference implementation of the record store contract (ADR 0012).

This is a reference implementation and test double, not durable storage: all
state lives in process memory and disappears when the object does.  It exists so
the shared contract suite in :mod:`pipeline.store_contract` can be exercised
without I/O, and so callers have a cheap stand-in for a real store.

Every valid namespace is served and created on first write; nothing ever spans
namespaces.
"""

from __future__ import annotations

import copy
import json
import threading

from pipeline.store_records import (
    validate_namespace,
    validate_plan_name,
    validate_story_key,
)


class InMemoryRecordStore:
    """A thread-safe, non-durable :class:`pipeline.store_records.RecordStore`."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._manifests: dict[tuple[str, str], dict] = {}
        self._decisions: dict[tuple[str, str], list[dict]] = {}
        self._journals: dict[tuple[str, str, str], list[dict]] = {}

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
