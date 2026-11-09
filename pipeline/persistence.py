"""Plan-file persistence helpers for the pipeline MCP server.

Reads/writes the small JSON files that live alongside a plan's manifest in
PLAN_DIR: notifications log, decisions log, per-story checkpoint journal,
and a plan's role_config block. All read that binding as a free variable -
it is imported from pipeline_paths at module load, and tests patch
pipeline_persistence.PLAN_DIR directly (the Option B pattern from
PIPELINE_MCP_DECOMPOSITION_PLAN.md §4).
"""

import collections
import datetime
import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from .events import EventBus
from .parsers import _atomic_write_json
from .paths import PLAN_DIR
from .store_records import (
    LEGACY_OUTBOX_CHANNEL,
    InvalidRecordKeyError,
    validate_channel,
)

# Retention/rotation policy for the per-plan notification sinks (the JSONL
# sidecar written by _write_notification_record and the free-text .log written
# by notification_sinks.file_log_sink). Both writers share one rotation helper
# (_rotate_if_needed) so the two cannot drift. A cap <= 0 disables rotation
# (append-only, the historical behavior).
NOTIFICATIONS_MAX_BYTES = int(os.environ.get("PIPELINE_NOTIFICATIONS_MAX_BYTES", str(2 * 1024 * 1024)))
NOTIFICATIONS_KEEP_N = int(os.environ.get("PIPELINE_NOTIFICATIONS_KEEP", "3"))

# Module-level constant for notification severities
NOTIFY_SEVERITIES = frozenset({"info", "warning", "error"})

# Logger for this module
logger = logging.getLogger(__name__)



def _decisions_path(plan_name: str) -> Path:
    return PLAN_DIR / f"{plan_name}.decisions.json"


def _append_decision(plan_name: str, record: dict[str, Any]) -> None:
    path = _decisions_path(plan_name)
    log = json.loads(path.read_text()) if path.exists() else []
    log.append(record)
    _atomic_write_json(path, log)

# ---------- Checkpoint journal ----------

def _journal_path(plan_name: str, story_key: str) -> Path:
    return PLAN_DIR / f"{plan_name}.{story_key}.journal.json"


def _append_journal(plan_name: str, story_key: str, record: dict[str, Any]) -> None:
    path = _journal_path(plan_name, story_key)
    log = json.loads(path.read_text()) if path.exists() else []
    log.append(record)
    _atomic_write_json(path, log)


def _read_journal(plan_name: str, story_key: str) -> list[dict[str, Any]]:
    path = _journal_path(plan_name, story_key)
    return json.loads(path.read_text()) if path.exists() else []


def _plan_manifest(plan_name: str) -> dict[str, Any] | None:
    """Read one plan's manifest, record store first, plan directory second.

    Delegates to :func:`pipeline.manifest_source.read_plan_manifest`, the one
    store-first read (ADR 0012 stage B), so this module cannot drift from
    ``pipeline.checkpoint`` / ``pipeline.worktree_patch``.

    ``fallback_on_error=True``, stated explicitly: a role_config read is never a
    gate, so a broken store or an unreadable file degrades to ``None`` ("no
    override") instead of raising. A plan neither source knows is ``None`` too.

    ``plans_root`` is this module's own patchable binding (see the module
    docstring) and is passed in rather than looked up, so patching it - or the
    server's, which the fixture mirrors - is honoured.

    A manifest a source does yield is returned as the answer: a store manifest
    without a role_config block means "no override", never "ask the file".
    The read is a pure read (the caller only reads the returned snapshot).
    """
    from .manifest_source import read_plan_manifest

    return read_plan_manifest(
        plan_name, fallback_on_error=True, plans_root=PLAN_DIR
    )


def _plan_role_config(plan_name: str) -> dict:
    """A plan's role_config block (per-role provider/model overrides, set at
    save_plan/ingest_plan time - see role_registry.py's resolve_role()),
    or {} if the plan/manifest doesn't exist, doesn't set one, or the
    manifest is unreadable. Read fresh each call through _plan_manifest
    above - never a gate, so any read failure degrades to "no override"
    rather than raising, and anything that is not a mapping is ignored.
    """
    manifest = _plan_manifest(plan_name)
    if manifest is None:
        return {}
    role_config = manifest.get("role_config")
    return role_config if isinstance(role_config, dict) else {}

# ---------- Notification JSONL helpers ----------

def _notifications_jsonl_path(plan_name: str) -> Path:
    """Return the path to the notifications.jsonl file for a plan."""
    return PLAN_DIR / f"{plan_name}.notifications.jsonl"


def _notification_record(
    plan_name,
    message,
    story_key,
    severity,
    event,
    dedup_key,
    ts,
    correlation_id=None,
    attempt=None,
    role=None,
    provider=None,
    model=None,
) -> dict:
    """Build a structured notification record.

    Parameters are passed in the same order as used by _notify_user. The
    function is pure and does not modify any global state.

    The optional correlation/context fields (correlation_id, attempt, role,
    provider, model) are included in the returned record ONLY when not None,
    so the legacy record shape is unchanged when none are set.
    """
    # Validate severity; fall back to "info" if invalid.
    if not isinstance(severity, str) or severity not in NOTIFY_SEVERITIES:
        logger.warning(
            "Invalid notification severity %r for plan %s; falling back to 'info'",
            severity,
            plan_name,
        )
        severity = "info"
    record = {
        "ts": ts,
        "plan": plan_name,
        "message": message,
        "story_key": story_key,
        "severity": severity,
        "event": event,
        "dedup_key": dedup_key,
    }
    for name, value in (
        ("correlation_id", correlation_id),
        ("attempt", attempt),
        ("role", role),
        ("provider", provider),
        ("model", model),
    ):
        if value is not None:
            record[name] = value
    return record


def _rotate_if_needed(path: Path, max_bytes: int, keep: int) -> None:
    """Rotate ``path`` to numbered generations when it exceeds ``max_bytes``.

    Shared by both notification writers (the JSONL sidecar and the free-text
    .log) so their retention policies cannot drift. Best-effort: any OSError
    (missing file, unreadable dir, failed rename) is swallowed so the caller
    falls back to a plain append.

    Policy:
    * ``max_bytes <= 0`` disables rotation entirely (append-only).
    * ``keep <= 0`` retains no generations: the deletion loop starts at
      ``keep + 1``, so with ``keep=0`` the just-rotated generation is deleted
      immediately (truncate-on-overflow, not "disabled").
    * Rotation happens only when the current size EXCEEDS ``max_bytes``.
    * The active file becomes ``<path>.1``; existing generations shift up
      (``.1`` -> ``.2``, ...) and generations beyond ``keep`` are deleted.
    """
    if max_bytes <= 0:
        return
    try:
        size = os.path.getsize(path)
    except OSError:
        # Missing (or unreadable) file: nothing to rotate, and stat'ing must
        # not create the file.
        return
    if size <= max_bytes:
        return
    try:
        # Shift existing generations up FIRST so the active file's rename to
        # .1 cannot clobber a generation that still needs to move.
        for i in range(keep - 1, 0, -1):
            if os.path.exists(f"{path}.{i}"):
                os.replace(f"{path}.{i}", f"{path}.{i + 1}")
        os.replace(path, f"{path}.1")
        # Delete generations beyond keep (e.g. keep was lowered since an
        # older run left more generations behind).
        i = keep + 1
        while os.path.exists(f"{path}.{i}"):
            os.remove(f"{path}.{i}")
            i += 1
    except OSError:
        # Rotation is best-effort; the caller falls back to a plain append.
        return


def _write_notification_record(plan_name: str, record: dict) -> None:
    """Append a JSONL line to the plan's notifications.jsonl file.

    Errors from opening or writing the file are swallowed and logged at ERROR
    level; callers should not see these exceptions.
    """
    try:
        path = _notifications_jsonl_path(plan_name)
        _rotate_if_needed(path, NOTIFICATIONS_MAX_BYTES, NOTIFICATIONS_KEEP_N)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as exc:  # pragma: no cover - exercised by tests
        logger.error("Failed to write notification record for plan %s: %s", plan_name, exc)

def _notifications_log_path(plan_name: str) -> Path:
    """Return the path to the notifications.log file for a plan.

    Derived from the JSONL sidecar's path so this module keeps a single
    plans-directory binding (the same free variable every other plan-file
    writer here reads), and the two notification sidecars can never be looked
    up in different directories.
    """
    return _notifications_jsonl_path(plan_name).with_suffix(".log")


def _write_notification(plan_name: str, record: dict) -> None:
    """Write both of a notification's on-disk representations.

    One record, two representations: the free-text ``<plan>.notifications.log``
    line first, then the structured ``<plan>.notifications.jsonl`` line. The
    bytes and the rotation are the ones
    ``notification_sinks.file_log_sink`` and ``_notify_user._write_directly``
    already produce, so a notification written through the record store and one
    written through the bus sink are byte-identical.
    """
    log_path = _notifications_log_path(plan_name)
    try:
        _rotate_if_needed(log_path, NOTIFICATIONS_MAX_BYTES, NOTIFICATIONS_KEEP_N)
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(f"{record['ts']} {record['message']}\n")
    except OSError as exc:  # pragma: no cover - defensive
        logger.error(
            "Failed to write notification log for plan %s: %s", plan_name, exc
        )
    _write_notification_record(plan_name, record)


def _usage_jsonl_path(plan_name: str) -> Path:
    return PLAN_DIR / f"{plan_name}.usage.jsonl"


def _write_usage_record(plan_name: str, record: dict) -> None:
    """Append one usage record as a JSONL line (same retention as the notifications sidecar)."""
    try:
        path = _usage_jsonl_path(plan_name)
        _rotate_if_needed(path, NOTIFICATIONS_MAX_BYTES, NOTIFICATIONS_KEEP_N)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as exc:
        logger.error("Failed to write usage record for plan %s: %s", plan_name, type(exc).__name__)


# Reimplement _notify_user with keyword-only parameters and JSONL sidecar.

def _notify_user(
    plan_name: str,
    message: str,
    *,
    story_key=None,
    severity="info",
    event=None,
    dedup_key=None,
    correlation_id=None,
    attempt=None,
    role=None,
    provider=None,
    model=None,
) -> None:
    """Durably record a notice for the user with structured JSONL sidecar.

    The function keeps its original two-positional-argument contract while
    adding keyword-only parameters for structured data. It writes the free-text
    log line first, then appends a JSONL record.

    The optional correlation/context kwargs are forwarded to the record
    builder and the bus-event payload; when omitted (the default) the record
    and payload shapes are unchanged.
    """
    ts = datetime.datetime.now(datetime.UTC).isoformat()
    record = _notification_record(
        plan_name,
        message,
        story_key,
        severity,
        event,
        dedup_key,
        ts,
        correlation_id=correlation_id,
        attempt=attempt,
        role=role,
        provider=provider,
        model=model,
    )

    def _write_directly() -> None:
        path = PLAN_DIR / f"{plan_name}.notifications.log"
        _rotate_if_needed(path, NOTIFICATIONS_MAX_BYTES, NOTIFICATIONS_KEEP_N)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{ts} {message}\n")
        _write_notification_record(plan_name, record)

    try:
        # Lazy import to avoid cycle: event_wiring imports notification_sinks, which imports persistence
        from .event_wiring import get_bus
        from .events import make_event
        bus = get_bus()
        payload = {
            "message": message,
            "story_key": story_key,
            "severity": record["severity"],
            "event": event,
            "dedup_key": dedup_key,
            "ts": ts,
        }
        # Forward the optional dispatch context so sinks persisting from the
        # bus payload produce the same record as the direct-write path.
        for name, value in (
            ("correlation_id", correlation_id),
            ("attempt", attempt),
            ("role", role),
            ("provider", provider),
            ("model", model),
        ):
            if value is not None:
                payload[name] = value
        evt = make_event(
            "notification",
            plan_name,
            story_key=story_key,
            payload=payload,
        )
        evt["ts"] = ts
        # Enqueue the durable outbox record BEFORE publishing: the event is
        # transport only, so a broker outage (or a crash between the two) must
        # never lose the outbox entry.  The lazy import keeps the existing
        # cycle rule (event_wiring -> notification_sinks -> persistence); a
        # failure here is logged by type only and never stops the history
        # write or the publish below.
        try:
            from . import notification_outbox

            notification_outbox.outbox_sink(evt)
        except Exception as exc:  # noqa: BLE001 - never break the notification
            logger.error(
                "Failed to enqueue notification outbox entry; continuing: %s",
                type(exc).__name__,
            )
        bus.publish(evt)
        # InProcessEventBus.publish swallows handler exceptions internally, so a
        # sink that fails to persist never raises here and can't be caught below.
        # Instead, ask the bus whether anything -- a local sink or a durable
        # remote consumer -- will receive "notification": if nothing delivers,
        # write directly so the notification is never silently dropped; when
        # something delivers (the normal, wired bus), it already persisted the
        # record, so skip to avoid a double write.
        delivers = getattr(bus, "delivers", None)
        if delivers is None:
            will_deliver = EventBus.delivers(bus, "notification")
        else:
            will_deliver = delivers("notification")
        if not will_deliver:
            try:
                _write_directly()
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to write notification directly: %s", exc)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to publish notification event; falling back: %s", exc)
        try:
            _write_directly()
        except Exception as exc2:  # noqa: BLE001
            logger.error("Failed to write notification directly: %s", exc2)

# -- per-channel outbox queues (ADR 0012 decision 3) ---------------------
#
# A notification channel owns its own append-only JSONL spool so one channel
# retrying never holds up another.  The legacy e-mail spool keeps its historic
# ``<plan>.outbox.jsonl`` name in the directory the legacy writer resolves,
# while an instance channel gets ``<plan>.outbox.<channel>.jsonl`` in the one
# the channel writer resolves.  Both bindings are resolved AT CALL TIME because
# that is what today's writers use and what the tests patch.


def _outbox_dirs() -> tuple[Path, Path]:
    """Return the ``(legacy, instance)`` outbox directories, resolved now."""
    from . import notification_outbox, paths

    return notification_outbox.PLAN_DIR, paths.PLAN_DIR


def _outbox_path(plan_name: str, channel: str) -> Path:
    """Return the spool file holding ``channel``'s queue for ``plan_name``.

    The canonical location is the binding today's writer resolves: the legacy
    spool's directory for :data:`LEGACY_OUTBOX_CHANNEL`, the channel writer's
    directory for an instance channel.  A queue that already exists under the
    OTHER binding is honoured in place -- read, acked and appended to where it
    is -- so an existing spool is never shadowed by an empty canonical file and
    read, ack and append always agree on which file they mean.
    """
    legacy_dir, instance_dir = _outbox_dirs()
    if channel == LEGACY_OUTBOX_CHANNEL:
        canonical = legacy_dir / f"{plan_name}.outbox.jsonl"
        alternative = instance_dir / canonical.name
    else:
        canonical = instance_dir / f"{plan_name}.outbox.{channel}.jsonl"
        alternative = legacy_dir / canonical.name
    if not canonical.exists() and alternative.exists():
        return alternative
    return canonical


def _outbox_line_id(raw_line: str) -> str:
    """Return the opaque id of one raw spool line (sha256 hex of its text)."""
    return hashlib.sha256(raw_line.encode("utf-8")).hexdigest()


def _outbox_parse(raw_line: str) -> dict[str, Any] | None:
    """Return the record a spool line carries, or ``None`` if it is unusable.

    A blank line, malformed JSON and a JSON value that is not an object are all
    "unusable": the queue skips them on read and drops them on the next ack.
    """
    if not raw_line.strip():
        return None
    try:
        record = json.loads(raw_line)
    except json.JSONDecodeError:
        return None
    return record if isinstance(record, dict) else None


def _warn_unusable_outbox_lines(plan_name: str, channel: str, count: int) -> None:
    """Log one WARNING about unusable spool lines, naming only plan + channel.

    The line content is never logged: a spooled notification can hold a
    recipient, a subject or a URL.
    """
    logger.warning(
        "Skipping %d malformed outbox line(s): plan=%s channel=%s",
        count,
        plan_name,
        channel,
    )


def _replace_outbox_lines(path: Path, lines: list[str]) -> None:
    """Rewrite ``path`` with ``lines`` atomically, never truncating first.

    A temp file in the same directory then :func:`os.replace` over the
    original: a crash mid-rewrite leaves the original intact.  Best-effort, so
    a failed rewrite never raises into the caller.
    """
    tmp_path: Path | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
        )
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_fh:
            for line in lines:
                tmp_fh.write(line + "\n")
        os.replace(tmp_path, path)
        tmp_path = None
    except OSError:
        logger.exception(
            "Failed to rewrite outbox %s atomically; retaining records", path.name
        )
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:  # pragma: no cover - best-effort cleanup
                pass


def _outbox_append(plan_name: str, channel: str, record: dict) -> None:
    """Append one record to the ``(plan_name, channel)`` spool.

    The line is exactly ``json.dumps(record, default=str)`` so the file is
    byte-identical to what the outbox sink has always written, and it is built
    BEFORE the file is opened: a record that cannot be serialised writes
    nothing.
    """
    line = json.dumps(record, default=str)
    path = _outbox_path(plan_name, channel)
    # The spool directory is the queue's own state, so it is created on first
    # append rather than assumed: a queue written into a freshly made (or
    # re-made) directory must not fail with FileNotFoundError, and an append
    # must never land in some other process's directory.
    path.parent.mkdir(parents=True, exist_ok=True)
    _rotate_if_needed(path, NOTIFICATIONS_MAX_BYTES, NOTIFICATIONS_KEEP_N)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _outbox_read(
    plan_name: str, channel: str, limit: int | None = None
) -> list[dict[str, Any]]:
    """Return the queue's ``{'id', 'record'}`` entries, oldest first.

    Reading never removes or creates anything: a missing spool file is an empty
    queue.  Unusable lines are skipped (one WARNING per read) and the records
    returned are freshly parsed, so a caller cannot mutate the stored queue.
    """
    path = _outbox_path(plan_name, channel)
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except OSError:
        logger.warning(
            "Could not read outbox: plan=%s channel=%s", plan_name, channel
        )
        return []
    entries: list[dict[str, Any]] = []
    unusable = 0
    for raw_line in raw_lines:
        record = _outbox_parse(raw_line)
        if record is None:
            unusable += 1
            continue
        entries.append({'id': _outbox_line_id(raw_line), 'record': record})
    if unusable:
        _warn_unusable_outbox_lines(plan_name, channel, unusable)
    if limit is not None:
        entries = entries[: max(0, limit)]
    return entries


def _outbox_ack(plan_name: str, channel: str, ids) -> int:
    """Ack ``ids``: remove one pending entry per id, return how many went.

    An id removes exactly one entry carrying it (so two identical lines need
    two acks), an unknown id is ignored, and unusable lines are dropped.  Every
    other raw line is kept byte-for-byte.  The file is re-read immediately
    before the atomic rewrite, so a record appended while the ack was working
    survives.  An ack with no ids and no unusable lines leaves the file
    untouched.
    """
    path = _outbox_path(plan_name, channel)
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return 0
    except OSError:
        logger.warning(
            "Could not read outbox: plan=%s channel=%s", plan_name, channel
        )
        return 0
    wanted = collections.Counter(str(acked) for acked in ids)
    retained: list[str] = []
    removed = 0
    unusable = 0
    for raw_line in raw_lines:
        if _outbox_parse(raw_line) is None:
            unusable += 1
            continue
        line_id = _outbox_line_id(raw_line)
        if wanted[line_id] > 0:
            wanted[line_id] -= 1
            removed += 1
            continue
        retained.append(raw_line)
    if unusable:
        _warn_unusable_outbox_lines(plan_name, channel, unusable)
    if not removed and not unusable:
        return 0
    try:
        current_lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        current_lines = raw_lines
    appended_since_snapshot = current_lines[len(raw_lines):]
    _replace_outbox_lines(path, retained + appended_since_snapshot)
    return removed


def _outbox_queues() -> list[tuple[str, str]]:
    """Return the sorted ``(plan, channel)`` pairs that hold at least one entry."""
    found: set[tuple[str, str]] = set()
    for directory in _outbox_dirs():
        for path in Path(directory).glob("*.outbox.jsonl"):
            plan = path.name[: -len(".outbox.jsonl")]
            if plan:
                found.add((plan, LEGACY_OUTBOX_CHANNEL))
        for path in Path(directory).glob("*.outbox.*.jsonl"):
            stem = path.name[: -len(".jsonl")]
            plan, _, channel = stem.rpartition(".outbox.")
            if not plan or not channel:
                continue
            try:
                validate_channel(channel)
            except InvalidRecordKeyError:
                continue
            found.add((plan, channel))
    return sorted(
        (plan, channel)
        for plan, channel in found
        if _outbox_read(plan, channel)
    )


__all__ = [
    "NOTIFICATIONS_KEEP_N",
    "NOTIFICATIONS_MAX_BYTES",
    "NOTIFY_SEVERITIES",
    "_append_decision",
    "_append_journal",
    "_decisions_path",
    "_journal_path",
    "_notification_record",
    "_notifications_jsonl_path",
    "_notify_user",
    "_plan_role_config",
    "_read_journal",
    "_write_notification_record",
]


def read_plan_file(path: Path) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Return ``(plan, refusal)`` for the plan JSON at *path*.

    A missing file, an unreadable one, malformed JSON, or a top-level value that
    is not an object are each a structured refusal rather than an exception.
    Ingest calls this on a file any caller can write, so a truncated manifest --
    a disk-full write, an interrupted atomic replace -- must become ingest's
    usual ``{"ok": False}`` instead of aborting with a traceback.
    """
    name = path.stem
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}, {"ok": False, "error": f"No plan named {name}"}
    except OSError as exc:
        return {}, {
            "ok": False,
            "error": f"plan {name!r} is unreadable: {exc.strerror or exc}",
        }
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, {"ok": False, "error": f"plan {name!r} is unreadable: {exc}"}
    if not isinstance(data, dict):
        return {}, {
            "ok": False,
            "error": f"plan {name!r} is unreadable: not a JSON object",
        }
    return data, None
