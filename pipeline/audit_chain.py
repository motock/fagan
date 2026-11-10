"""The audit hash chain: one pure algorithm, no I/O (ADR 0012 decision 10).

Every audit store -- memory, file, SQLite -- must hash the same bytes, or a
chain written by one implementation cannot be verified by another.  So the
algorithm lives here, free of paths, locks and clocks: the callers own the
storage and pass the records in.

A record carries three chain fields besides its body: ``seq`` (0, 1, 2, ...),
``prev_hash`` (the ``hash`` of the record before it, ``""`` for the first) and
``hash`` (SHA-256 over the canonical JSON of every *other* field).

What the chain does not detect: truncation of the tail.  A chain with no
external anchor cannot prove that the newest records were not removed, so the
head hash also has to be shipped somewhere the writer cannot rewrite.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

#: The chain fields the algorithm owns; a body may never carry one.
CHAIN_FIELDS: tuple[str, ...] = ("seq", "prev_hash", "hash")


def canonical(record: dict[str, Any]) -> str:
    """Return the canonical JSON text the hash is computed over."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)


def record_hash(record: dict[str, Any]) -> str:
    """Hash every field except the record's own ``hash``."""
    payload = {key: value for key, value in record.items() if key != "hash"}
    return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()


def link(previous: dict[str, Any] | None, body: dict[str, Any]) -> dict[str, Any]:
    """Return ``body`` plus its ``seq``, ``prev_hash`` and ``hash``.

    ``previous`` is the tail of the chain, or ``None`` for a new chain.  A body
    that already carries one of the chain fields is refused rather than
    silently overwritten: a caller passing its own ``seq`` believes it is
    choosing the chain position, and it is not.  The message names the field
    and never echoes the value, which may be audit content.
    """
    for field in CHAIN_FIELDS:
        if field in body:
            raise ValueError(f"audit body must not carry {field}")
    record: dict[str, Any] = {"seq": 0, "prev_hash": ""}
    if previous is not None:
        record["seq"] = int(previous.get("seq", 0)) + 1
        record["prev_hash"] = previous.get("hash", "")
    record.update(body)
    record["hash"] = record_hash(record)
    return record


def verify_entries(entries: Iterable[dict[str, Any] | None]) -> int | None:
    """Return the index of the first altered entry, or ``None`` when intact.

    Checks all three links a rewrite breaks: the running ``seq``, the
    ``prev_hash`` carried from the previous entry, and the entry's own
    ``hash``.  ``None`` stands for a stored line that is not a JSON object, and
    is reported at its position.  An empty chain is intact: there is no record
    to disagree with.
    """
    previous_hash = ""
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            return index
        if entry.get("seq") != index:
            return index
        if entry.get("prev_hash") != previous_hash:
            return index
        if entry.get("hash") != record_hash(entry):
            return index
        previous_hash = str(entry.get("hash", ""))
    return None
