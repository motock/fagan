"""The audit-log contract: append and read, never rewrite (ADR 0012 decision 10).

An audit record is a row ``(namespace, chain, seq, prev_hash, record, hash)``
with ``(namespace, chain, seq)`` unique.  The application has INSERT and SELECT
only: the surface below is the whole vocabulary an implementation offers, and
it deliberately contains no way to change or drop history.  A rewrite is
therefore not something the code can do politely -- it has to bypass the
protocol, which is exactly what a tamper test does.

This module holds the contract only.  It stores nothing and imports no storage.
"""

from __future__ import annotations

from typing import Protocol

from .store_records import validate_plan_name

#: The host-scoped chain: events that have no plan to belong to.  The leading
#: ``@`` cannot appear in a plan name (``store_records._KEY_RE``), so the host
#: chain can never alias a plan chain inside one namespace.
HOST_CHAIN_ID = "@host"

#: The complete surface of an append-only audit log.
AUDIT_LOG_METHODS: tuple[str, ...] = (
    "append",
    "read_chain",
    "head",
    "list_chains",
)


def validate_chain(chain: str) -> str:
    """Return ``chain`` when it is :data:`HOST_CHAIN_ID` or a valid plan name.

    A chain name becomes an anchor key and, in the file implementation, part of
    a path, so it follows the plan-name rule.  The refusal is deliberately
    generic and never echoes the offending value, which may be a path.
    """
    if chain == HOST_CHAIN_ID:
        return HOST_CHAIN_ID
    return validate_plan_name(chain)


class AuditLog(Protocol):
    """An append-only, per-namespace set of hash-chained record streams.

    There is no method that rewrites, reorders, truncates or deletes a stored
    record, and no attribute that exposes the storage for mutation.  Anything
    that claims to be an audit log offers exactly these four operations.
    """

    def append(self, namespace: str, chain: str, body: dict) -> dict:
        """Append ``body`` to the chain and return the record as stored."""
        ...

    def read_chain(self, namespace: str, chain: str) -> list[dict | None]:
        """Return the chain in storage order (``[]`` when it is absent)."""
        ...

    def head(self, namespace: str, chain: str) -> dict | None:
        """Return the newest record of the chain, or ``None`` when it is absent."""
        ...

    def list_chains(self, namespace: str) -> list[str]:
        """Return the chain names in the namespace, plan chains first, sorted."""
        ...
