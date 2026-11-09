"""The per-channel outbox contract every record store implements (ADR 0012).

ADR 0012 decision 3 makes the notification outbox per channel: each
(plan, channel) pair owns its own queue, so one channel retrying never holds up
another.  Decision 9 keeps the queue records in the record store, next to the
state they are derived from, so a store can commit an outbox append with the
state change that produced it.

This module only declares the contract.  Every store (memory, file, sqlite)
implements the four methods; the behaviour they must share is exercised through
the public API by ``tests/unit/_store_outbox_helpers.py``.

Semantics
---------
``append_outbox(namespace, plan_name, channel, record) -> None``
    Queue one record.  The store assigns it an opaque string id.  Appending
    raises ``TypeError`` for a non-dict record and never writes a record it
    cannot serialise.

``read_outbox(namespace, plan_name, channel, limit=None) -> list[dict]``
    Return ``{'id': str, 'record': dict}`` items oldest first, at most ``limit``
    of them (``limit=0`` is empty).  Reading never removes anything and returns
    deep copies, so mutating a result cannot corrupt the queue.

``ack_outbox(namespace, plan_name, channel, ids) -> int``
    Acknowledge ids.  Acking an id removes exactly one pending entry carrying
    it; an unknown id is ignored; the return value is the number removed.

``list_outboxes(namespace) -> list[tuple[str, str]]``
    The sorted ``(plan, channel)`` pairs holding at least one entry.

Channel keys are validated by ``pipeline.store_records.validate_channel``: only
the legacy ``_legacy`` e-mail spool or an instance name matching the
notification channel rule is accepted.
"""

from __future__ import annotations


class OutboxContract:
    """Mixin declaring the per-channel outbox queue API of a record store."""

    def append_outbox(self, namespace, plan_name, channel, record):
        """Append ``record`` to the ``(plan_name, channel)`` outbox queue."""
        raise NotImplementedError

    def read_outbox(self, namespace, plan_name, channel, limit=None):
        """Return the queue's entries oldest first, without removing them."""
        raise NotImplementedError

    def ack_outbox(self, namespace, plan_name, channel, ids):
        """Remove one pending entry per acked id and return how many went."""
        raise NotImplementedError

    def list_outboxes(self, namespace):
        """Return the sorted ``(plan, channel)`` pairs holding entries."""
        raise NotImplementedError
