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
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

from pipeline.audit_anchor import AnchorUnreadable, anchored_heads
from pipeline.audit_file import FileAuditLog
from pipeline.plan_approval import canonical_digest
from pipeline.store import FileStore, build_store
from pipeline.store_import_audit import REFUSED, import_audit_chains
from pipeline.store_records import (
    DEFAULT_NAMESPACE,
    context_address,
    verify_context_digest,
)
from pipeline.store_sqlite import SqliteStore
from pipeline.store_sqlite_records import JOURNAL_OPTIONAL_FIELDS

#
# :func:`import_record_kinds` reads every record kind of the default namespace
# through the file store's record view and writes it through the SQLite store's,
# one kind at a time, and returns one tally per kind:
#
# ``{"imported": n, "already_present": n, "refused": [{"name", "reason"}]}``
#
# Nothing is ever overwritten.  Each kind is decided against what the database
# already holds, and only two shapes are possible:
#
# * a single value (a manifest, a draft, a report, a snapshot) is written when the
#   database has none, counted ``already_present`` when it is identical, and
#   refused ``conflict`` when it differs;
# * an append-only log (decisions, usage, journals, outbox queues, notification
#   history) is written only past the records the database already holds -- the
#   database's records must be a prefix of the file's, or the log is refused
#   ``conflict``.  A second run therefore appends nothing.
#
# A manifest is verified: its canonical digest is read back from the database
# and must equal the file's, or the write is rolled back and the plan refused
# ``digest_mismatch``.  A source file that cannot be read is refused
# ``unreadable`` and the rest is imported.
#
# Reads of the database use the row-only views where a read-time adoption would
# otherwise write (a manifest's row is checked with ``_stored_document``), so a
# dry run writes nothing.
# """

NS = DEFAULT_NAMESPACE

#: The kinds, in the order they are imported and reported.
KINDS = (
    "manifests",
    "plan_drafts",
    "plan_completions",
    "decisions",
    "journals",
    "usage",
    "outbox",
    "notification_history",
    "context_snapshots",
)

#: What :func:`_fetch` returns once it has refused a read.  Not ``None``, which
#: is a legitimate answer ("there is no such value") from the record views.
_FAILED = object()


class _DigestMismatch(Exception):
    """A manifest read back from the database does not digest as the file did."""


def _tally() -> dict[str, Any]:
    return {"imported": 0, "already_present": 0, "refused": []}


def _refuse(tally: dict, name: str, reason: str) -> None:
    tally["refused"].append({"name": name, "reason": reason})


def _fetch(tally: dict, name: str, call: Callable[..., Any], *args: Any) -> Any:
    """Return ``call(*args)``, or refuse *name* as unreadable and return ``_FAILED``."""
    try:
        return call(*args)
    except (OSError, ValueError):
        _refuse(tally, name, "unreadable")
        return _FAILED


def _plan_names(source: Any) -> list[str]:
    """Return every plan the file store can name: manifests, drafts, queues."""
    records = source.records()
    names = set(records.list_plans(NS))
    names.update(records.list_plan_drafts(NS))
    names.update(plan for plan, _channel in records.list_outboxes(NS))
    return sorted(names)


def import_record_kinds(source: Any, target: Any, dry_run: bool = False) -> dict:
    """Import every record kind from *source* (a ``FileStore``) into *target*.

    Returns the tally of each kind, keyed by :data:`KINDS`.  The source is only
    read.  A dry run reports the same tallies and writes no record.
    """
    plans = _plan_names(source)
    importers = {
        "manifests": partial(_manifests, source, target, plans, dry_run),
        "plan_drafts": partial(_drafts, source, target, dry_run),
        "plan_completions": partial(_completions, source, target, plans, dry_run),
        "decisions": partial(_decisions, source, target, plans, dry_run),
        "journals": partial(_journals, source, target, plans, dry_run),
        "usage": partial(_usage, source, target, plans, dry_run),
        "outbox": partial(_outbox, source, target, dry_run),
        "notification_history": partial(_history, source, target, plans, dry_run),
        "context_snapshots": partial(_snapshots, source, target, dry_run),
    }
    return {kind: importers[kind]() for kind in KINDS}


# -- single-value kinds ------------------------------------------------------
def _write_manifest(target: Any, plan: str, manifest: dict) -> str | None:
    """Write *manifest* in one transaction and verify it; return a refusal reason."""
    records = target.records()
    try:
        with records.transaction(NS, plan) as acquired:
            if not acquired:
                return "busy"
            records.put_manifest(NS, plan, manifest)
            written = records.get_manifest(NS, plan)
            if canonical_digest(written) != canonical_digest(manifest):
                raise _DigestMismatch()
    except _DigestMismatch:
        # The transaction rolled back, so the database holds no trace of it.
        return "digest_mismatch"
    return None


def _manifests(source: Any, target: Any, plans: list[str], dry_run: bool) -> dict:
    tally = _tally()
    records = source.records()
    for plan in plans:
        manifest = _fetch(tally, plan, records.get_manifest, NS, plan)
        if manifest is _FAILED:
            continue
        if manifest is None:
            # ``get_manifest`` answers None for a damaged file too, so a
            # manifest file that exists but reads as nothing is unreadable.
            if plan in source.records().list_plans("default"):
                _refuse(tally, plan, "unreadable")
            continue
        stored = target._stored_document(plan)
        if stored == manifest:
            tally["already_present"] += 1
        elif stored is not None:
            _refuse(tally, plan, "conflict")
        elif dry_run:
            tally["imported"] += 1
        else:
            reason = _write_manifest(target, plan, manifest)
            if reason is None:
                tally["imported"] += 1
            else:
                _refuse(tally, plan, reason)
    return tally


def _single_value(
    tally: dict, name: str, stored: Any, value: Any, write: Callable[[], None], dry_run: bool
) -> None:
    """Write *value* when nothing is stored; otherwise count it or refuse it."""
    if stored == value:
        tally["already_present"] += 1
    elif stored is not None:
        _refuse(tally, name, "conflict")
    else:
        if not dry_run:
            write()
        tally["imported"] += 1


def _drafts(source: Any, target: Any, dry_run: bool) -> dict:
    tally = _tally()
    src = source.records()
    dst = target.records()
    for plan in src.list_plan_drafts(NS):
        draft = _fetch(tally, plan, src.get_plan_draft, NS, plan)
        if draft is _FAILED:
            continue
        if draft is None:
            # Listed as a draft, yet nothing reads back: a damaged file.
            _refuse(tally, plan, "unreadable")
            continue
        _single_value(
            tally, plan, dst.get_plan_draft(NS, plan), draft,
            partial(dst.put_plan_draft, NS, plan, draft), dry_run,
        )
    return tally


def _completions(source: Any, target: Any, plans: list[str], dry_run: bool) -> dict:
    tally = _tally()
    src = source.records()
    dst = target.records()
    for plan in plans:
        if src.is_plan_completed(NS, plan):
            if dst.is_plan_completed(NS, plan):
                tally["already_present"] += 1
            else:
                if not dry_run:
                    dst.mark_plan_completed(NS, plan)
                tally["imported"] += 1
        report = _fetch(tally, plan, src.get_plan_report, NS, plan)
        if report is not _FAILED and report is not None:
            _single_value(
                tally, plan, dst.get_plan_report(NS, plan), report,
                partial(dst.put_plan_report, NS, plan, report), dry_run,
            )
    return tally


def _snapshots(source: Any, target: Any, dry_run: bool) -> dict:
    tally = _tally()
    src = source.records()
    dst = target.records()
    root = src._snapshots_root()
    if not root.is_dir():
        return tally
    for folder in sorted(root.iterdir()):
        if not folder.is_dir():
            continue
        try:
            digest = "sha256:" + context_address(folder.name)
            snapshot = src.get_context_snapshot(NS, digest)
            if snapshot is None:
                raise ValueError("snapshot folder has no content")
            verify_context_digest(digest, snapshot["content"])
        except (OSError, ValueError):
            _refuse(tally, folder.name, "unreadable")
            continue
        stored = dst.get_context_snapshot(NS, digest)
        _single_value(
            tally, folder.name,
            None if stored is None else stored["content"],
            snapshot["content"],
            partial(
                dst.put_context_snapshot, NS, digest, snapshot["content"], snapshot["meta"]
            ),
            dry_run,
        )
    return tally


# -- append-only kinds -------------------------------------------------------
def _append_missing(
    tally: dict,
    name: str,
    stored: list,
    records: list,
    append: Callable[[Any], None],
    dry_run: bool,
) -> None:
    """Append the records past *stored*, which must be a prefix of *records*."""
    if records[: len(stored)] != stored:
        _refuse(tally, name, "conflict")
        return
    tally["already_present"] += len(stored)
    try:
        for record in records[len(stored):]:
            if not dry_run:
                append(record)
            tally["imported"] += 1
    except (ValueError, TypeError):
        _refuse(tally, name, "invalid_record")


def _decisions(source: Any, target: Any, plans: list[str], dry_run: bool) -> dict:
    tally = _tally()
    src = source.records()
    dst = target.records()
    for plan in plans:
        records = _fetch(tally, plan, src.get_decisions, NS, plan)
        if records is _FAILED:
            continue
        _append_missing(
            tally, plan, dst.get_decisions(NS, plan), records,
            partial(dst.append_decision, NS, plan), dry_run,
        )
    return tally


def _journal_view(entry: dict) -> dict:
    """Return *entry* as the database serves it: optional fields left out when unset."""
    return {
        key: value
        for key, value in entry.items()
        if value is not None or key not in JOURNAL_OPTIONAL_FIELDS
    }


def _journals(source: Any, target: Any, plans: list[str], dry_run: bool) -> dict:
    tally = _tally()
    src = source.records()
    dst = target.records()
    for plan in plans:
        keys = _fetch(tally, plan, src.list_journal_keys, NS, plan)
        if keys is _FAILED:
            continue
        for key in keys:
            name = f"{plan}/{key}"
            entries = _fetch(tally, name, src.get_journal, NS, plan, key)
            if entries is _FAILED:
                continue
            _append_missing(
                tally,
                name,
                [_journal_view(entry) for entry in dst.get_journal(NS, plan, key)],
                [_journal_view(entry) for entry in entries],
                partial(dst.append_journal, NS, plan, key),
                dry_run,
            )
    return tally


def _usage(source: Any, target: Any, plans: list[str], dry_run: bool) -> dict:
    tally = _tally()
    src = source.records()
    dst = target.records()
    for plan in plans:
        records = _fetch(tally, plan, src.get_usage, NS, plan)
        if records is _FAILED:
            continue
        _append_missing(
            tally, plan, dst.get_usage(NS, plan), records,
            partial(dst.append_usage, NS, plan), dry_run,
        )
    return tally


def _outbox(source: Any, target: Any, dry_run: bool) -> dict:
    tally = _tally()
    src = source.records()
    dst = target.records()
    for plan, channel in src.list_outboxes(NS):
        name = f"{plan}/{channel}"
        entries = _fetch(tally, name, src.read_outbox, NS, plan, channel)
        if entries is _FAILED:
            continue
        stored = [entry["record"] for entry in dst.read_outbox(NS, plan, channel)]
        _append_missing(
            tally, name, stored, [entry["record"] for entry in entries],
            partial(dst.append_outbox, NS, plan, channel), dry_run,
        )
    return tally


def _history(source: Any, target: Any, plans: list[str], dry_run: bool) -> dict:
    tally = _tally()
    src = source.records()
    dst = target.records()
    for plan in plans:
        read = _fetch(tally, plan, src.get_notification_history, NS, plan)
        if read is _FAILED:
            continue
        records, malformed = read
        if malformed:
            # Skipping the damaged lines would import a history with holes in it.
            _refuse(tally, plan, "malformed")
            continue
        stored, _ = dst.get_notification_history(NS, plan)
        _append_missing(
            tally, plan, stored, records,
            partial(dst.append_notification, NS, plan), dry_run,
        )
    return tally


#: The operator-facing command, quoted by REFERENCE.md and the CHANGELOG.
COMMAND = "python -m pipeline.tools.import_file_store --db <absolute path> [--dry-run]"


def _absolute(value: str) -> str:
    """Accept only an absolute database path: a relative one would resolve per process."""
    if not Path(value).is_absolute():
        raise argparse.ArgumentTypeError("--db must be an absolute path")
    return value


def run_import(source: FileStore, target: SqliteStore, dry_run: bool) -> dict[str, Any]:
    """Import every record kind, then the audit chains, and return the summary."""
    record_kinds = import_record_kinds(source, target, dry_run)
    try:
        anchors, anchors_unreadable = anchored_heads(), False
    except AnchorUnreadable:
        # A configured anchor that cannot be read must not allow any chain in.
        anchors, anchors_unreadable = None, True
    audit = import_audit_chains(
        FileAuditLog(),
        target.audit_log(),
        namespace=DEFAULT_NAMESPACE,
        dry_run=dry_run,
        anchors=anchors,
        anchors_unreadable=anchors_unreadable,
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
