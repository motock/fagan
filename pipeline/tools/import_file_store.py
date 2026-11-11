"""One-time import of the file store's records into the SQLite store (ADR 0012).

Run once by an operator, against the database a ``PIPELINE_STORE=sqlite``
deployment will read::

    python -m pipeline.tools.import_file_store --db /absolute/path/store.db [--dry-run]

Every record kind is read through the file store and written through the SQLite
store for the default namespace (:mod:`pipeline.store_import_records`).  The
audit chains are imported by :func:`pipeline.store_import_audit.import_audit_chains`
and this command adds no audit logic of its own: its per-chain report is copied
into the summary under ``"audit"``.

The command is idempotent -- a second run imports nothing -- and a dry run
reports what a real run would do and writes no record.  Source files are never
modified or deleted.

The summary is one JSON object on stdout.  The exit status is ``0`` when every
record kind and every audit chain was imported or already present, and ``1``
when any of them was refused.  A refusal never stops the rest of the import.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from pipeline import store_import_records
from pipeline.audit_file import FileAuditLog
from pipeline.store import FileStore, build_store
from pipeline.store_import_audit import REFUSED, import_audit_chains
from pipeline.store_records import DEFAULT_NAMESPACE
from pipeline.store_sqlite import SqliteStore

#: The operator-facing command, quoted by REFERENCE.md and the CHANGELOG.
COMMAND = "python -m pipeline.tools.import_file_store --db <absolute path> [--dry-run]"


def _absolute(value: str) -> str:
    """Accept only an absolute database path: a relative one would resolve per process."""
    if not Path(value).is_absolute():
        raise argparse.ArgumentTypeError("--db must be an absolute path")
    return value


def run_import(source: FileStore, target: SqliteStore, dry_run: bool) -> dict[str, Any]:
    """Import every record kind, then the audit chains, and return the summary."""
    record_kinds = store_import_records.import_record_kinds(source, target, dry_run)
    audit = import_audit_chains(
        FileAuditLog(),
        target.audit_log(),
        namespace=DEFAULT_NAMESPACE,
        dry_run=dry_run,
    )
    return {"dry_run": dry_run, "record_kinds": record_kinds, "audit": audit}


def _any_refused(summary: dict[str, Any]) -> bool:
    kind_refused = any(tally["refused"] for tally in summary["record_kinds"].values())
    audit_refused = any(entry["status"] == REFUSED for entry in summary["audit"])
    return kind_refused or audit_refused


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.tools.import_file_store",
        description="One-time import of the file store's records into the SQLite store.",
    )
    parser.add_argument(
        "--db",
        required=True,
        type=_absolute,
        help="absolute path of the SQLite database to import into",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be imported; write no record",
    )
    args = parser.parse_args(argv)

    target = SqliteStore(args.db)
    try:
        # The source is the file layout whatever PIPELINE_STORE selects, so the
        # factory is asked for it explicitly rather than given the environment.
        summary = run_import(build_store({"PIPELINE_STORE": "file"}), target, args.dry_run)
    finally:
        target.close()
    print(json.dumps(summary, indent=2))
    return 1 if _any_refused(summary) else 0


if __name__ == "__main__":
    sys.exit(main())
