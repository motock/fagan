"""
Outbox enqueue entry point that spools *selected* ``notification`` events to a
per-plan outbox queue for a later out-of-band send.

This module is NOT a bus subscriber.  :func:`pipeline.persistence._notify_user`
calls :func:`outbox_sink` itself, on the caller's thread, BEFORE it publishes
the event: the notifier enqueues the outbox entry before it publishes
(ADR 0012 decision 9 -- "events are transport; the outbox is the durable
record").  A bus subscription would therefore write every record twice.

The outbox is a queue per ``(plan, channel)`` held by the process's record
store -- with the file store, one JSONL sidecar per queue:

* the legacy e-mail queue – ``<plan>.outbox.jsonl``, one JSON line per record.

The sink itself performs NO network I/O.  :func:`drain_outbox` (below) reads
the queued records and hands each to an injected ``sender`` — in production
``pipeline.notification_email.send_notification_email`` (PLANNOTIFY-05) — so
spooling can never block the sequential pipeline tick on a network call; only
the scheduler's own drain phase (PLANNOTIFY-06) pays that cost, on its own
schedule.

Two distinct concepts are called *event* here, mirroring
:mod:`pipeline.notification_sinks`:

* ``event["type"] == "notification"`` – the **bus-level** event type.
* ``event["payload"]["event"]`` – the **notification's own** structured event
  name, e.g. ``"plan_completed"``.  Selection uses ONLY this structured field;
  message text is never matched, so transient notices such as dispatch retries
  are never queued for delivery.

Selection rules:

* Disabled by default.  The sink writes nothing unless the environment
  variable ``PIPELINE_NOTIFY_OUTBOX_ENABLED`` is exactly ``"1"`` (the strings
  ``"0"``, ``"true"``, ``"01"`` … all leave it disabled).
* Event allowlist.  ``PIPELINE_NOTIFY_OUTBOX_EVENTS`` is a comma-separated
  list of structured event names (default
  ``"plan_completed,story_parked,dispatch_failed,tests_failed,agent_gave_up"``,
  i.e. the completion notice plus the parked/failed story events that need a
  human); only notifications whose ``payload["event"]`` is in that set are
  spooled.
* Retention is delegated to :func:`pipeline.persistence._rotate_if_needed`
  with :data:`pipeline.persistence.NOTIFICATIONS_MAX_BYTES` and
  :data:`pipeline.persistence.NOTIFICATIONS_KEEP_N` — the same policy as
  ``file_log_sink``, so it cannot drift.

Per-instance queues (notification channels): when a notification config file
exists, each event is also appended to the outbox of every instance its routes
deliver to, and :func:`drain_channel_outboxes` drains those through the
instances.  The routes are the allowlist there (``PIPELINE_NOTIFY_OUTBOX_EVENTS``
only filters the legacy file).  Files of instances removed from the config are
left untouched, not deleted.

Like every sink, this one never raises into the bus: any failure is logged at
ERROR level with ``exc_info=True`` and swallowed, so a notification failure
can never break a pipeline tick.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import persistence
from .actor import current_namespace
from .live_ref import LiveRef as _ServerRef
from .notification_audience import members_for
from .notification_channels import as_sender
from .notification_routing import (
    NotificationConfigError,
    namespace_notification_config,
    parse_layer_routes,
    safe_load_notification_config,
)
from .store_namespaces import namespaces_with_plans
from .store_records import LEGACY_OUTBOX_CHANNEL

logger = logging.getLogger(__name__)

# The process's shared record store, resolved lazily through
# ``pipeline.server``: every outbox read and acknowledgement goes through
# ``_store.records()`` so the drain can never see a second copy of a queue.
_store = _ServerRef("_store")

OUTBOX_ENABLED_ENV = "PIPELINE_NOTIFY_OUTBOX_ENABLED"
OUTBOX_EVENTS_ENV = "PIPELINE_NOTIFY_OUTBOX_EVENTS"
DEFAULT_OUTBOX_EVENTS = "plan_completed,story_parked,dispatch_failed,tests_failed,agent_gave_up"

# Suffix shared by the sink (which writes it) and the drain (which reads it) —
# one constant so the two can never drift apart.
OUTBOX_SUFFIX = ".outbox.jsonl"

# Wildcard plan name for :func:`drain_outbox`: drain EVERY plan's outbox file
# in one call.  The scheduler tick is plan-agnostic (its scan and reconcile
# phases cover all plans at once), so its drain phase passes this instead of a
# single plan name.
ALL_PLANS = "*"

# Maximum age of a spooled record, in seconds (7 days).  A channel that cannot
# deliver keeps every record forever, so a recovery would flush weeks of stale
# notifications at once; the drain drops anything older than this instead.
OUTBOX_MAX_AGE_S = 7 * 24 * 3600


def _queue_identity(path: Path) -> tuple[str, str]:
    """Return ``(plan, channel)`` for a queue file name.

    The names are fixed by :func:`pipeline.persistence._outbox_path`:
    ``<plan>.outbox.jsonl`` for the legacy queue and
    ``<plan>.outbox.<channel>.jsonl`` for an instance queue.
    """
    stem = path.name.removesuffix(".jsonl")
    plan, _, channel = stem.partition(".outbox")
    return plan, channel.lstrip(".") or LEGACY_OUTBOX_CHANNEL


def _append_rotating(path: Path, line: str) -> None:
    """Append one JSON ``line`` to the queue whose file is ``path``.

    Every queue write funnels through here -- tests patch this name to
    simulate a failing channel -- and delegates to the shared store's
    ``append_outbox`` so the record is durable, goes through the store's own
    retention/rotation policy, and lands in the caller's transaction when one
    is held.
    """
    plan, channel = _queue_identity(path)
    _store.records().append_outbox(
        current_namespace(), plan, channel, json.loads(line)
    )


def _enqueue(plan: str, channel: str, record: dict[str, Any]) -> None:
    """Enqueue ``record`` on ``channel``'s queue for ``plan``.

    The line is exactly ``json.dumps(record, default=str)`` -- the record the
    bus subscriber always wrote -- so the queue file is byte-identical to what
    it used to be.
    """
    path = persistence._outbox_path(plan, channel)
    _append_rotating(path, json.dumps(record, default=str))


def _spool_legacy(plan: str, evt_name: str | None, record: dict[str, Any]) -> None:
    if os.environ.get(OUTBOX_ENABLED_ENV, "0") != "1":
        # Secure default: outbound sinks ship disabled.  Read per call so
        # tests (and operators) can flip it without a process restart.
        return
    raw = os.environ.get(OUTBOX_EVENTS_ENV, DEFAULT_OUTBOX_EVENTS)
    allowed = {s.strip() for s in raw.split(",") if s.strip()}
    if evt_name not in allowed:
        # Not allowlisted (or no structured event name): silently skip.  This
        # gate selects the LEGACY channel only: the instance queues are
        # selected by the routing config, so a miss here must still route.
        return
    _enqueue(plan, LEGACY_OUTBOX_CHANNEL, record)


def _read_manifest(plan: str) -> dict[str, Any] | None:
    """Return the plan manifest, or ``None`` when it cannot be read.

    Read once per spool call so a route can be owner-scoped and the plan's
    team layers consulted. Any failure to read the manifest (missing file, bad
    JSON, unreadable) means "unknown": the plan then matches only routes
    without an ``owner`` glob and no layer routes, which is the deny-by-default
    direction. Only the error type is logged -- a manifest can hold paths, so
    nothing from the failure or the manifest leaks into the log.
    """
    try:
        # Read through the process's SHARED store: a second store connection
        # (``store.build_store()``) would contend for the write lock the caller
        # may be holding while it enqueues its outbox entry.
        manifest = _store.records().get_manifest(current_namespace(), plan)
    except Exception as exc:  # noqa: BLE001 - an unreadable manifest means "unknown"
        logger.warning(
            "Could not read plan owner for notification routing: error_type=%s",
            type(exc).__name__,
        )
        return None
    return manifest if isinstance(manifest, dict) else None


def _layer_routes_for(manifest: dict[str, Any] | None):
    """Return a ``read_layer_routes`` callable for the plan's settings context.

    An unreadable manifest yields no layer routes (deny by default). The
    namespace is the one the manifest was read from.
    """

    def read(namespace: str) -> Any:
        if manifest is None:
            return None
        # Lazy: pipeline.ci imports the settings resolver (import cycle).
        from .ci import context_from_manifest
        from .settings_store import effective_setting

        context = context_from_manifest(manifest)
        org = manifest.get("org")
        return effective_setting(
            "notification_routes",
            namespace=namespace,
            team=context.team,
            repo=context.repo,
            org=org if isinstance(org, str) else "",
        )["value"]

    return read


def _spool_instances(plan: str, evt_name: str | None, record: dict[str, Any]) -> None:
    """Append ``record`` to the queue of each instance the config routes it to.

    A config file's routes, merged with the plan's resolved team/org/repo
    ``notification_routes``, are the allowlist for these queues (the legacy
    ``PIPELINE_NOTIFY_OUTBOX_EVENTS`` is not consulted), and the file's
    presence (or a layer value) is the opt-in.  Neither means nothing here.
    One instance's write failure is logged (type only) and never stops the
    others or the legacy write.
    """
    if "PYTEST_CURRENT_TEST" in os.environ:
        _default_plans = (Path.home() / ".claude" / "plans").resolve()
        if persistence.legacy_outbox_dir().resolve() == _default_plans:
            logger.warning(
                "refusing to spool instance notifications into the live plan directory under pytest"
            )
            return
    try:
        manifest = _read_manifest(plan)
        try:
            config = namespace_notification_config(
                _layer_routes_for(manifest), current_namespace()
            )
        except NotificationConfigError as exc:
            logger.error("notification config invalid; delivering nothing: error=%s", exc)
            return
        if config is None:
            return
        owner = manifest.get("owner") if manifest else None
        owner = owner if isinstance(owner, str) else ""
        members = members_for(current_namespace(), plan, owner)
        for name in config.resolve(plan, evt_name, owner, members):
            try:
                _enqueue(plan, name, record)
            except Exception as exc:  # noqa: BLE001 - isolate one instance's failure
                logger.error(
                    "Failed to spool notification to instance outbox: instance=%s error_type=%s",
                    name,
                    type(exc).__name__,
                )
    except Exception as exc:  # noqa: BLE001 - the sink never raises into the bus
        logger.error(
            "Failed to route notification to instance outboxes: error_type=%s",
            type(exc).__name__,
        )


def outbox_sink(event: dict[str, Any]) -> None:
    """Enqueue a ``notification`` event on the outboxes that select it.

    This is the public enqueue entry point.  :func:`pipeline.persistence.
    _notify_user` calls it directly, on the caller's thread, BEFORE it
    publishes the event; the function is NOT a bus subscriber (a subscriber
    would enqueue every record a second time).

    Parameters
    ----------
    event:
        The notification event.  It must contain a truthy ``plan`` key and may
        include a ``payload`` dict whose optional ``event`` key carries the
        structured notification name used for allowlist matching.  The record
        queued is exactly ``dict(event)``, so the spooled line is
        ``json.dumps(event, default=str)`` -- identical to what the bus
        subscriber used to write.

    Returns
    -------
    None
        The function always returns ``None``; every failure is logged at ERROR
        (error TYPE only) and never raised.  One channel's failure never stops
        another channel's write.
    """
    try:
        plan = event.get("plan")
        if not plan:
            # Checked BEFORE the enabled gate so a malformed event is reported
            # even when the sink is disabled.
            logger.error(
                "Missing or falsy 'plan' in notification event: %s", event
            )
            return

        payload = event.get("payload")
        evt_name = payload.get("event") if isinstance(payload, dict) else None
        record = dict(event)

        # Each channel is isolated separately: one failing queue must never
        # stop the others.  The legacy gate and the routing config select
        # DISJOINT channel sets, so both writes are attempted on every call.
        try:
            _spool_legacy(plan, evt_name, record)
        except Exception as exc:  # isolate the legacy channel
            logger.exception(
                "Failed to spool notification to the legacy outbox: error_type=%s",
                type(exc).__name__,
            )
        _spool_instances(plan, evt_name, record)
    except Exception as exc:  # the entry point never raises
        logger.exception(
            "Failed to spool notification to outbox: error_type=%s",
            type(exc).__name__,
        )
        return


def _is_expired(record: dict, now: datetime) -> bool:
    """Return True when ``record`` is older than :data:`OUTBOX_MAX_AGE_S`.

    The record's top-level ``"ts"`` is the ISO 8601 string the sink spooled
    (for example ``2026-11-04T00:24:53.872989+00:00``).  A naive timestamp is
    treated as UTC.  A missing key, a non-string value, or an unparseable
    string returns False, so the record is KEPT: an odd timestamp must fail
    towards delivery, never towards silent loss.
    """
    raw = record.get("ts")
    if not isinstance(raw, str):
        return False
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (now - ts).total_seconds() > OUTBOX_MAX_AGE_S


def _drain_one_outbox(path: Path, sender) -> int:
    """Drain the single outbox file ``path``; return the sent-record count.

    Records the ``sender`` accepted (returned ``True``) are removed; records
    it rejected (``False``) or that made it RAISE are retained verbatim for
    the next drain.  Records older than :data:`OUTBOX_MAX_AGE_S` are dropped
    unsent — removed from the spool and logged at ``WARNING`` with the file
    name only — so a channel that recovers after a long outage does not flush
    weeks of stale notifications.  The rewrite is atomic — a temp file in the
    same directory, then :func:`os.replace` over the original — so a crash
    mid-rewrite leaves the original intact: at-least-once delivery for
    everything the age limit retains, never a silently lost notification.
    Never raises.
    """
    sent = 0
    try:
        if not path.exists():
            # A read must not create state: no outbox file means nothing was
            # ever spooled for this plan, so return 0 without creating one.
            return 0

        with open(path, "r", encoding="utf-8") as fh:
            raw_lines = fh.read().splitlines()

        # One clock reading for the whole drain so a slow sender cannot make
        # the expiry cutoff drift from record to record.
        now = datetime.now(UTC)
        # Retained lines keep their ORIGINAL raw text (never a re-serialized
        # dict) so a drain that rejects everything leaves the file
        # byte-for-byte unchanged.
        retained_lines: list[str] = []
        for raw_line in raw_lines:
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                # One corrupt line must not block the rest of the file — and
                # it is dropped from the rewrite entirely, so it can neither
                # be sent nor re-WARN on every future drain.
                logger.warning(
                    "Skipping malformed outbox line in %s (not valid JSON)",
                    path.name,
                )
                continue
            if isinstance(record, dict) and _is_expired(record, now):
                # Stale beyond OUTBOX_MAX_AGE_S: drop it instead of delivering
                # weeks-old notifications on a channel that just recovered.
                # Nothing from the record is logged.  It is not added to
                # retained_lines, so the rewrite removes it from the file.
                logger.warning(
                    "Dropping expired outbox record: file=%s", path.name
                )
                continue
            try:
                accepted = sender(record)
            except Exception:  # a raising sender is treated exactly like a False
                logger.exception(
                    "Outbox sender raised for a spooled record; retaining it"
                )
                retained_lines.append(raw_line)
                continue
            if accepted:
                sent += 1
            else:
                retained_lines.append(raw_line)

        # Re-read immediately before the replace: the outbox is append-only
        # (only outbox_sink ever appends), so our original snapshot is always
        # a prefix of the current file. A record the sink spooled WHILE this
        # drain was busy sending (e.g. a slow SMTP loop racing a freshly
        # abandoned worker's completion notice) lives past that prefix and
        # would otherwise be silently discarded by the replace below. Merge
        # it in unprocessed — it is picked up, parsed, and sent on the next
        # drain — so "never a lost notification" holds under concurrency too.
        try:
            with open(path, "r", encoding="utf-8") as fh:
                current_lines = fh.read().splitlines()
        except OSError:
            current_lines = raw_lines
        appended_since_snapshot = current_lines[len(raw_lines):]

        # The file existed, so always rewrite it — even when nothing is
        # retained, which leaves it empty (the pinned choice; the file is
        # never removed).
        tmp_path: Path | None = None
        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
            )
            tmp_path = Path(tmp_name)
            with os.fdopen(fd, "w", encoding="utf-8") as tmp_fh:
                for retained in retained_lines + appended_since_snapshot:
                    tmp_fh.write(retained + "\n")
            # Never delete or truncate the original before the replace: a
            # failed replace must leave every record in place.
            os.replace(tmp_path, path)
            tmp_path = None  # replaced; nothing left to clean up
        except Exception:  # keep going, never raise
            logger.exception(
                "Failed to rewrite outbox %s atomically; retaining records",
                path.name,
            )
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:  # pragma: no cover - best-effort cleanup
                    pass
        return sent
    except Exception:  # the drain never raises into the tick
        logger.exception("Failed to drain notification outbox %s", path.name)
        return sent


def _queues_by_channel() -> dict[str, list[str]]:
    """Return the plans holding an entry, keyed by channel.

    Enumerated through the store so the drain sees exactly the queues the
    sink wrote, in the order the store reports them.  A queue that is empty
    (or was never created) is not listed: there is nothing to drain.
    """
    queues: dict[str, list[str]] = {}
    for plan, channel in _store.records().list_outboxes(current_namespace()):
        plans = queues.setdefault(channel, [])
        if plan not in plans:
            plans.append(plan)
    return queues


def _drain_queue(plan_name: str, channel: str, sender) -> int:
    """Drain one ``(plan_name, channel)`` queue; return the accepted count.

    Records the ``sender`` accepted (returned ``True``) are acknowledged and
    removed; records it rejected (``False``) or that made it RAISE are
    retained verbatim for the next drain.  A record older than
    :data:`OUTBOX_MAX_AGE_S` is dropped UNSENT and reported at ``WARNING``
    naming only the plan and the channel -- a record can hold a recipient, a
    subject or a URL, so its content is never logged.

    The queue is acknowledged exactly once, with every id collected, even
    when nothing was collected: the file store uses that call to drop
    malformed lines, and acknowledging only the ids this drain actually saw
    keeps a record appended between the read and the ack.
    """
    entries = _store.records().read_outbox(current_namespace(), plan_name, channel)
    now = datetime.now(UTC)
    acked: list[str] = []
    delivered = 0
    for entry in entries:
        entry_id = entry.get("id")
        record = entry.get("record") or {}
        if _is_expired(record, now):
            logger.warning(
                "Dropping expired outbox record: plan=%s channel=%s",
                plan_name,
                channel,
            )
            if entry_id is not None:
                acked.append(entry_id)
            continue
        try:
            accepted = bool(sender(record))
        except Exception as exc:  # noqa: BLE001 - a raising sender means "retain"
            logger.error(
                "Failed to deliver outbox record: plan=%s channel=%s error_type=%s",
                plan_name,
                channel,
                type(exc).__name__,
            )
            continue
        if accepted:
            delivered += 1
            if entry_id is not None:
                acked.append(entry_id)
    _store.records().ack_outbox(current_namespace(), plan_name, channel, acked)
    return delivered


def drain_outbox(plan_name: str, sender) -> int:
    """Drain spooled outbox records for ``plan_name`` through ``sender``.

    ``sender`` is an explicit argument (in production
    ``pipeline.notification_email.send_notification_email``), never imported
    here, so callers — and tests — can inject any callable that takes one
    record dict and returns a bool.

    Parameters
    ----------
    plan_name:
        The plan whose legacy queue should be drained, or :data:`ALL_PLANS`
        (``"*"``) to drain every plan's legacy queue in one call — the shape
        the scheduler tick uses, since a tick is plan-agnostic.
    sender:
        Callable taking one parsed record dict and returning ``True`` when the
        record was delivered (remove it) or ``False`` when it was not (retain
        it for the next drain).  A sender that RAISES is treated exactly like
        a ``False`` return.

    Returns
    -------
    int
        The count of records the sender accepted.  Never raises: a failing
        store, a raising sender or an expired record is logged and the drain
        moves on.
    """
    total = 0
    try:
        if plan_name == ALL_PLANS:
            plans = _queues_by_channel().get(LEGACY_OUTBOX_CHANNEL, [])
        else:
            plans = [plan_name]
        for plan in plans:
            try:
                total += _drain_queue(plan, LEGACY_OUTBOX_CHANNEL, sender)
            except Exception as exc:  # noqa: BLE001 - one plan must not block another
                logger.error(
                    "Failed to drain outbox: plan=%s error_type=%s",
                    plan,
                    type(exc).__name__,
                )
    except Exception as exc:  # noqa: BLE001 - the drain never raises into the tick
        logger.error("Failed to drain outbox: error_type=%s", type(exc).__name__)
        return 0
    return total


def _layer_instances(namespace: str) -> dict[str, Any]:
    """Return the instances ``namespace``'s layer ``notification_routes`` define.

    An instance name two layers define with different settings is dropped and
    logged (namespace and name only): sending through the wrong team's
    credential is worse than not sending. An invalid layer value is logged by
    type and skipped; the namespace's other layers still apply.
    """
    found: dict[str, tuple[Any, Any]] = {}
    conflicts: set[str] = set()
    for settings_record in _store.records().list_settings(namespace):
        values = settings_record.get("values")
        value = values.get("notification_routes") if isinstance(values, dict) else None
        if value is None or value == {}:
            continue
        try:
            parsed = parse_layer_routes(value, namespace)
        except Exception as exc:  # noqa: BLE001 - one bad layer must not stop the rest
            logger.error(
                "Invalid layer notification routes; skipping layer: namespace=%s error_type=%s",
                namespace,
                type(exc).__name__,
            )
            continue
        raw_instances = value.get("instances") or {}
        for name, channel in parsed.instances.items():
            if name not in found:
                found[name] = (channel, raw_instances.get(name))
            elif found[name][1] != raw_instances.get(name):
                conflicts.add(name)
    for name in sorted(conflicts):
        logger.warning(
            "Layer instance defined with differing settings; not delivering: "
            "namespace=%s instance=%s",
            namespace,
            name,
        )
    return {name: pair[0] for name, pair in found.items() if name not in conflicts}


def _drain_namespace(plan_name: str, namespace: str) -> dict[str, int]:
    """Drain ``namespace``'s instance queues, under that namespace."""
    from .actor import bind_namespace

    delivered: dict[str, int] = {}
    with bind_namespace(namespace):
        deployment = safe_load_notification_config()
        # A deployment instance is never shadowed by a layer instance.
        instances = {**_layer_instances(namespace)}
        instances.update(deployment.instances if deployment is not None else {})
        if deployment is None and not instances:
            return delivered
        if plan_name == ALL_PLANS:
            plans_by_channel = _queues_by_channel()
        else:
            plans_by_channel = {name: [plan_name] for name in instances}
        for name, channel in instances.items():
            count = 0
            try:
                sender = as_sender(channel)
                for plan in plans_by_channel.get(name, []):
                    count += _drain_queue(plan, name, sender)
            except Exception as exc:  # noqa: BLE001 - one instance must not block another
                logger.error(
                    "Failed to drain instance outbox: instance=%s error_type=%s",
                    name,
                    type(exc).__name__,
                )
            delivered[name] = count
    return delivered


def drain_channel_outboxes(plan_name: str = ALL_PLANS) -> dict[str, int]:
    """Drain every namespace's instance queues through their instances.

    The caller's namespace and every namespace holding a plan are drained,
    each under its own binding with the deployment
    instances plus the instances its layers' ``notification_routes`` define.
    Returns delivered counts keyed by instance name, summed when a name appears
    in several namespaces.  A queue belonging to an instance no longer in the
    config is left untouched (not drained, not acknowledged, not deleted).  No
    config file and no layer instances means nothing happens.  One namespace's
    failure is logged (type only) and never stops another.  Never raises.
    """
    delivered: dict[str, int] = {}
    try:
        # The caller's own namespace is always drained, plans or not, as before.
        namespaces = sorted({current_namespace(), *namespaces_with_plans(_store.records())})
    except Exception as exc:  # noqa: BLE001 - the drain never raises into the tick
        logger.error(
            "Failed to drain channel outboxes: error_type=%s", type(exc).__name__
        )
        return {}
    for namespace in namespaces:
        try:
            counts = _drain_namespace(plan_name, namespace)
        except Exception as exc:  # noqa: BLE001 - one namespace must not block another
            logger.error(
                "Failed to drain namespace channel outboxes: namespace=%s error_type=%s",
                namespace,
                type(exc).__name__,
            )
            continue
        for name, count in counts.items():
            delivered[name] = delivered.get(name, 0) + count
    return delivered
