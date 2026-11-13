"""The audit log over the pipeline's long-standing JSONL layout (ADR 0012 d7).

One ``<plan>.audit.jsonl`` per plan plus the host-scoped ``.audit.host.jsonl``:
the files the pipeline has always written.  This implementation exists so the
``AuditLog`` protocol has a real backing store with no migration step - a chain
written before the refactor has to verify through the new API, and the bytes
appended after it have to continue that chain rather than restart it.

The streams are resolved through :func:`pipeline.audit.audit_path` and
:func:`pipeline.audit.host_audit_path` at call time, so a test (or an operator)
that redirects the plan directory redirects this log with it, and a host stream
parked somewhere else is still the host stream.

No method rewrites, reorders or deletes a stored record; the tamper tests
bypass the protocol on purpose.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
from pathlib import Path
from typing import Any

from . import audit, audit_chain
from .audit_log import HOST_CHAIN_ID, validate_chain
from .store_records import (
    DEFAULT_NAMESPACE,
    InvalidRecordKeyError,
    UnknownNamespaceError,
    validate_namespace,
    validate_plan_name,
)

logger = logging.getLogger("pipeline.audit")

#: The suffix every plan-scoped stream carries.
SUFFIX = ".audit.jsonl"

#: What the reader says about a stored line that is not a JSON object.  The
#: tamper test writes this literal so a rewritten chain is a realistic break.
MALFORMED_LINE = "not json"


def read_chain_at(path: Path) -> list[dict[str, Any] | None]:
    """Return the entries stored at *path*, ``[]`` when the file is absent.

    Blank lines are skipped; a line that is not a JSON object - unparseable or
    a JSON scalar - is reported as ``None`` at its position so verification can
    name the record that was broken.
    """
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return []
    entries: list[dict[str, Any] | None] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            record = None
        entries.append(record if isinstance(record, dict) else None)
    return entries


def tail_at(path: Path) -> dict[str, Any] | None:
    """Return the last well-formed record at *path*, or ``None``.

    ``None`` covers both a missing file and a final line that cannot be linked
    to: the chain starts at sequence 0 rather than guessing a predecessor.
    """
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return None
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return None
        return record if isinstance(record, dict) else None
    return None


def _resolved(path: Path) -> Path | None:
    """Return *path* resolved, or ``None`` when it cannot be resolved."""
    try:
        return path.resolve()
    except OSError:  # pragma: no cover - a path that cannot be resolved
        return None


class FileAuditLog:
    """The ``AuditLog`` over ``<plan>.audit.jsonl`` and ``.audit.host.jsonl``.

    Only :data:`DEFAULT_NAMESPACE` exists in the file layout, so any other
    namespace is refused before a path is built.  A chain name becomes part of
    a file name, so it is validated before it is turned into a path.

    Without a ``directory`` the log serves the pipeline's own streams, resolved
    through :mod:`pipeline.audit` at call time - that is the real deployment and
    every test that redirects the plan directory.  With a ``directory`` the log
    is bound to one plan directory, which is how a caller (or a contract test)
    asks for a log with state of its own.
    """

    def __init__(self, directory: Path | str | None = None) -> None:
        self._directory = Path(directory) if directory is not None else None

    # -- the streams ---------------------------------------------------
    @staticmethod
    def _require_default(namespace: str) -> None:
        """Refuse a namespace the file layout does not have."""
        validate_namespace(namespace)
        if namespace != DEFAULT_NAMESPACE:
            raise UnknownNamespaceError(
                "the file audit log serves the default namespace only"
            )

    def stream_path(self, chain: str) -> Path:
        """Return the file *chain* lives in - validated, resolved at call time."""
        if self._directory is not None:
            name = audit.HOST_AUDIT_FILE if chain == HOST_CHAIN_ID else f"{chain}{SUFFIX}"
            return self._directory / name
        if chain == HOST_CHAIN_ID:
            return audit.host_audit_path()
        return audit.audit_path(chain)

    def _streams_directory(self) -> Path:
        """Return the directory the plan-scoped streams live in."""
        if self._directory is not None:
            return self._directory
        return audit.audit_path("").parent

    # -- appending -----------------------------------------------------
    def append(self, namespace: str, chain: str, body: dict) -> dict:
        """Append ``body`` to *chain* under an exclusive lock and return it."""
        self._require_default(namespace)
        validate_chain(chain)
        return self._append_at(self.stream_path(chain), body)

    @staticmethod
    def _append_at(path: Path, body: dict[str, Any]) -> dict[str, Any]:
        """Link *body* onto the chain at *path* and write it as one line."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_APPEND, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            record = audit_chain.link(tail_at(path), body)
            line = audit_chain.canonical(record) + "\n"
            os.write(fd, line.encode("utf-8"))
            return record
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    # -- reading -------------------------------------------------------
    def read_chain(self, namespace: str, chain: str) -> list[dict | None]:
        """Return *chain* in storage order, ``[]`` when it is absent."""
        self._require_default(namespace)
        validate_chain(chain)
        return read_chain_at(self.stream_path(chain))

    def head(self, namespace: str, chain: str) -> dict | None:
        """Return the newest record of *chain*, or ``None`` when absent."""
        entries = self.read_chain(namespace, chain)
        return entries[-1] if entries else None

    def list_chains(self, namespace: str) -> list[str]:
        """Return the plan chains in the plan directory, host chain last.

        A file whose resolved path is the host stream is not a plan chain: the
        host stream belongs to no plan wherever it is parked.  A file name that
        is not an addressable plan name is skipped with a warning that names
        neither the file nor the path, because both may be operator data and
        the name may be reserved rather than merely odd.
        """
        try:
            chains, skipped = self._scan(namespace)
        except OSError:  # pragma: no cover - a directory that cannot be listed
            return []
        if skipped:
            logger.warning(
                "skipping audit streams whose plan name is not addressable",
                extra={"skipped": len(skipped)},
            )
        return chains

    def unaddressable_chains(self, namespace: str) -> list[str]:
        """Return the stream names :meth:`list_chains` skips, sorted.

        The names are the stems of ``*.audit.jsonl`` files in the plan directory
        whose name is not an addressable plan name.  Unlike :meth:`list_chains`
        an unlistable directory raises: a caller that reports refusals must not
        read a directory it could not see as one with no refusals.
        """
        _chains, skipped = self._scan(namespace)
        return skipped

    def _scan(self, namespace: str) -> tuple[list[str], list[str]]:
        """Return ``(addressable chains, unaddressable stems)``; raises ``OSError``.

        Uses :func:`os.scandir` rather than :meth:`Path.glob`, which swallows a
        permission error and would report an unlistable directory as empty.
        """
        self._require_default(namespace)
        directory = self._streams_directory()
        host_path = self.stream_path(HOST_CHAIN_ID)
        host = _resolved(host_path)
        try:
            names = sorted(
                entry.name
                for entry in os.scandir(directory)
                if entry.name.endswith(SUFFIX)
            )
        except FileNotFoundError:
            names = []
        chains: list[str] = []
        skipped: list[str] = []
        host_is_a_plan_stream = False
        for file_name in names:
            path = directory / file_name
            if host is not None and _resolved(path) == host:
                host_is_a_plan_stream = True
                continue
            name = file_name.removesuffix(SUFFIX)
            if not name or not self._addressable(name):
                skipped.append(name)
                continue
            chains.append(name)
        if not host_is_a_plan_stream and host_path.is_file():
            chains.append(HOST_CHAIN_ID)
        return chains, skipped

    @staticmethod
    def _addressable(name: str) -> bool:
        """Return whether *name* can be a chain id of this log."""
        if ":" in name:
            return False
        try:
            validate_plan_name(name)
        except InvalidRecordKeyError:
            return False
        return True
