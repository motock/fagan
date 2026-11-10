"""The in-memory reference audit log (ADR 0012 decision 10).

This is the reference implementation of :class:`pipeline.audit_log.AuditLog`:
append-only, per-namespace and hash-chained, with everything in process memory.
It exists so the contract suite has something to hold, and so the file and
SQLite implementations have a working model to be compared against.

Two properties are worth stating because they are easy to lose:

* nothing leaves or enters the log by reference -- both the body handed to
  :meth:`append` and every record handed back are deep copies, so a caller
  cannot mutate stored history by keeping a reference to it;
* one lock covers "read the tail, link, store", so concurrent appends cannot
  claim the same ``seq`` or the same ``prev_hash``.
"""

from __future__ import annotations

import copy
import threading
from typing import Any

from .audit_chain import link
from .audit_log import HOST_CHAIN_ID, validate_chain
from .store_records import validate_namespace


class InMemoryAuditLog:
    """An append-only, hash-chained audit log held in memory."""

    def __init__(self) -> None:
        # namespace -> chain -> entries in storage order
        self._chains: dict[str, dict[str, list[dict[str, Any]]]] = {}
        self._lock = threading.Lock()

    def append(self, namespace: str, chain: str, body: dict) -> dict:
        """Append ``body`` to the chain and return the record as stored."""
        validate_namespace(namespace)
        validate_chain(chain)
        with self._lock:
            entries = self._chains.setdefault(namespace, {}).setdefault(chain, [])
            record = link(entries[-1] if entries else None, copy.deepcopy(body))
            entries.append(record)
            return copy.deepcopy(record)

    def read_chain(self, namespace: str, chain: str) -> list[dict | None]:
        """Return the chain in storage order, ``[]`` when it does not exist."""
        validate_namespace(namespace)
        validate_chain(chain)
        with self._lock:
            return copy.deepcopy(self._chains.get(namespace, {}).get(chain, []))

    def head(self, namespace: str, chain: str) -> dict | None:
        """Return the newest record of the chain, or ``None`` when absent."""
        validate_namespace(namespace)
        validate_chain(chain)
        with self._lock:
            entries = self._chains.get(namespace, {}).get(chain, [])
            return copy.deepcopy(entries[-1]) if entries else None

    def list_chains(self, namespace: str) -> list[str]:
        """Return the plan chains sorted, then the host chain when present."""
        validate_namespace(namespace)
        with self._lock:
            names = list(self._chains.get(namespace, {}))
        host = [HOST_CHAIN_ID] if HOST_CHAIN_ID in names else []
        return sorted(name for name in names if name != HOST_CHAIN_ID) + host
