"""Durable NATS consumers for the scheduler daemon's ``agent_done`` handlers.

When ``PIPELINE_EVENT_BUS=nats``, :class:`~pipeline.events_nats.NatsEventBus`
publishes events to JetStream but only dispatches them to handlers in the
*publishing* process. An event published by a short-lived MCP server or a
dashboard action therefore has nobody to run its handler: the handler lives in
the long-running scheduler daemon. This module closes that gap with one durable
pull consumer per event type the bus declares as durable
(``PIPELINE_NATS_DURABLE_TYPES``).

Delivery contract (ADR 0010)
---------------------------
* **At least once.** A message is acknowledged only after every recorded
  handler for its type has returned. A handler that raises leaves the message
  unacked, so the broker redelivers it.
* **Exponential backoff.** A failed attempt is nacked with
  ``_BACKOFF_BASE_S * 2 ** (num_delivered - 1)`` seconds -- one second for the
  first retry, then two, four, eight -- and the consumer itself is created with
  the same schedule, so a message abandoned by a crashed process is retried on
  the same curve.
* **Dead-lettering is terminal and written once.** At the delivery limit
  (``PIPELINE_EVENT_MAX_DELIVER``, default 5) the raw message is republished to
  ``pipeline.deadletter.<type>.<plan>`` and an ``event_dead_lettered`` record is
  written *directly* to the plan's sidecar through
  :mod:`pipeline.persistence`. It is never written through ``_notify_user``:
  that path publishes on the event bus, which is the component whose delivery
  just failed. The record is keyed on the event id, so a lost ack cannot leave
  a second row. After that the message is acked; anything the broker redelivers
  at a higher attempt count is acked without publishing or recording again.
* **A handler refusal is a normal return.** ``wake_handler`` answers a stale or
  duplicate event with the guard's refusal and ``advance_pipeline`` answers a
  locked plan with ``skipped: locked``. Those are decisions, not failures, so
  the message is acked and never retried.

Ownership
---------
Only the scheduler daemon calls :func:`start_consumers`, and only for a
:class:`~pipeline.events_nats.NatsEventBus` -- MCP servers and the dashboard
publish without consuming. The fetch loops run on the bus's own asyncio loop
thread, so a consumer shares the connection the bus already owns; no extra
thread or loop is created here.

Logs name the event type and the handler only. A payload, a plan path or an
exception's text could carry anything at all, so none of them is formatted into
a message (Secure-by-Design).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import json
import logging
import os
from dataclasses import dataclass

from . import persistence
from .events import JsonlEventBus
from .events_nats import NatsEventBus

__all__ = [
    "durable_types",
    "max_deliver",
    "running_consumer_names",
    "start_consumers",
    "stop_consumers",
]

logger = logging.getLogger(__name__)

#: ``PIPELINE_EVENT_MAX_DELIVER`` -- delivery attempts before dead-lettering.
ENV_MAX_DELIVER = "PIPELINE_EVENT_MAX_DELIVER"
#: Delivery attempts before dead-lettering when the variable is unset.
DEFAULT_MAX_DELIVER = 5
#: Seconds before the first retry; doubled for every further failed attempt.
_BACKOFF_BASE_S = 1.0
#: Subject prefix a dead-lettered event is republished under.
DEAD_LETTER_PREFIX = "pipeline.deadletter"
#: Sidecar ``event`` value of the record written when an event is dead-lettered.
DEAD_LETTER_RECORD_EVENT = "event_dead_lettered"
#: Messages requested per pull; keeps a burst from starving the ack loop.
_FETCH_BATCH = 16
#: Upper bound on one pull request, so a stop request is noticed promptly.
_FETCH_TIMEOUT_S = 1.0
#: Pause after a fetch failure that is not an idle timeout.
_FETCH_ERROR_PAUSE_S = 1.0
#: How long starting one consumer may take before it is reported as failed.
_START_TIMEOUT_S = 15.0
#: How long an in-flight delivery may take to finish during shutdown.
_STOP_GRACE_S = 10.0
#: How long shutdown may take in total before it gives up on the loop.
_STOP_TIMEOUT_S = 20.0


@dataclass
class _Consumer:
    """One durable consumer's delivery state, owned by the bus's loop."""

    event_type: str
    durable: str
    subscription: object
    stop: asyncio.Event
    bus: NatsEventBus
    task: asyncio.Task | None = None


#: Consumers this process started, in start order.
_consumers: list[_Consumer] = []


def durable_types(bus: NatsEventBus) -> tuple[str, ...]:
    """The event types *bus* asks a durable consumer for, sorted.

    The names come from ``PIPELINE_NATS_DURABLE_TYPES``, which the bus parsed
    and validated at construction; sorting keeps consumer creation -- and the
    tests that pin it -- deterministic.
    """
    return tuple(sorted(bus._durable_types))


def max_deliver() -> int:
    """Delivery attempts before an event is dead-lettered.

    Read per delivery, not cached at start: an operator can change
    ``PIPELINE_EVENT_MAX_DELIVER`` and the next attempt honours it. An unusable
    value is reported and replaced by :data:`DEFAULT_MAX_DELIVER` rather than
    silently turning into zero attempts.
    """
    raw = os.environ.get(ENV_MAX_DELIVER)
    if raw is None or raw.strip() == "":
        return DEFAULT_MAX_DELIVER
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s is not an integer; using %d", ENV_MAX_DELIVER, DEFAULT_MAX_DELIVER)
        return DEFAULT_MAX_DELIVER
    if value < 1:
        logger.warning(
            "%s must be at least 1; using %d", ENV_MAX_DELIVER, DEFAULT_MAX_DELIVER
        )
        return DEFAULT_MAX_DELIVER
    return value


def running_consumer_names() -> tuple[str, ...]:
    """The durable consumer names this process currently runs."""
    return tuple(consumer.durable for consumer in _consumers)


def start_consumers(bus) -> None:
    """Start one durable consumer per durable type of *bus*.

    A bus that is not a :class:`~pipeline.events_nats.NatsEventBus` gets
    nothing: durable consumption belongs to the broker, and the scheduler
    daemon is the only process that starts it.

    A broker that cannot be reached leaves no consumer behind and does not
    abort the daemon; the events stay in the stream and a later start consumes
    them.
    """
    if not isinstance(bus, NatsEventBus):
        return

    loop = bus._ensure_loop()
    try:
        bus._ensure_connection(loop)
    except Exception:  # noqa: BLE001 - an unreachable broker is not fatal here
        logger.warning(
            "durable consumers were not started: the NATS broker named by "
            "PIPELINE_NATS_URL is unreachable"
        )
        return

    already = set(running_consumer_names())
    for event_type in durable_types(bus):
        durable = _durable_name(event_type)
        if durable in already:
            continue
        try:
            consumer = asyncio.run_coroutine_threadsafe(
                _start_one(bus, event_type), loop
            ).result(_START_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - one type must not stop the others
            logger.warning(
                "the durable consumer %s was not started", durable
            )
            continue
        _consumers.append(consumer)


def stop_consumers() -> None:
    """Stop every consumer this process started and forget it.

    The stop flag is set first, so a loop that is between fetches exits before
    it takes another message, and the in-flight handler still finishes and acks
    exactly as it would have -- cancelling it mid-delivery would leave a
    message the broker has to redeliver. Only a loop that outlives
    :data:`_STOP_GRACE_S` is cancelled.
    """
    while _consumers:
        consumer = _consumers.pop()
        loop = consumer.bus._loop
        if loop is None or loop.is_closed() or not loop.is_running():
            _abandon(consumer)
            continue
        try:
            asyncio.run_coroutine_threadsafe(_stop_one(consumer), loop).result(
                _STOP_TIMEOUT_S
            )
        except Exception:  # noqa: BLE001 - shutdown must finish regardless
            _abandon(consumer)
            logger.warning("the durable consumer %s did not stop cleanly", consumer.durable)


def _abandon(consumer: _Consumer) -> None:
    """Drop a consumer whose loop can no longer run it."""
    consumer.stop.set()
    consumer.task = None


def _durable_name(event_type: str) -> str:
    """The broker-side name of *event_type*'s durable consumer."""
    return f"pipeline-{event_type}"


def _consumer_config(limit: int):
    """The JetStream consumer configuration for a delivery *limit*."""
    from nats.js.api import AckPolicy, ConsumerConfig

    return ConsumerConfig(
        ack_policy=AckPolicy.EXPLICIT,
        max_deliver=limit,
        # Attempt n costs the broker 2 ** (n - 1) seconds before redelivery,
        # the same curve a nak from this process asks for.
        backoff=[_backoff_delay(attempt) for attempt in range(1, limit)],
    )


def _backoff_delay(attempts: int) -> float:
    """Seconds to wait before retrying an event delivered *attempts* times."""
    return _BACKOFF_BASE_S * (2 ** max(attempts - 1, 0))


async def _start_one(bus: NatsEventBus, event_type: str) -> _Consumer:
    """Create the durable consumer for *event_type* and its fetch loop."""
    durable = _durable_name(event_type)
    subscription = await bus._js.pull_subscribe(
        f"pipeline.{event_type}.>",
        durable=durable,
        stream=bus._settings.stream,
        config=_consumer_config(max_deliver()),
    )
    stop = asyncio.Event()
    consumer = _Consumer(
        event_type=event_type,
        durable=durable,
        subscription=subscription,
        stop=stop,
        bus=bus,
    )
    consumer.task = asyncio.create_task(_fetch_loop(consumer))
    return consumer


async def _stop_one(consumer: _Consumer) -> None:
    """Signal *consumer* to stop and wait for its fetch loop to finish."""
    consumer.stop.set()
    task = consumer.task
    consumer.task = None
    if task is None:
        return
    try:
        await asyncio.wait_for(asyncio.shield(task), _STOP_GRACE_S)
    except TimeoutError:
        # The handler outlived the grace period; a message cancelled before it
        # was acked stays on the consumer and is redelivered on the next start.
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001 - the fetch loop logged the cause
        logger.debug("the durable consumer %s ended with a fetch failure", consumer.durable)


async def _fetch_loop(consumer: _Consumer) -> None:
    """Pull batches of messages and deliver them until stopped."""
    while not consumer.stop.is_set():
        try:
            messages = await consumer.subscription.fetch(
                _FETCH_BATCH, timeout=_FETCH_TIMEOUT_S
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad pull must not end the loop
            if _is_fetch_timeout(exc):
                continue
            logger.warning(
                "the durable consumer %s could not fetch; retrying", consumer.durable
            )
            await asyncio.sleep(_FETCH_ERROR_PAUSE_S)
            continue
        for message in messages:
            await _deliver(consumer, message)


def _is_fetch_timeout(exc: BaseException) -> bool:
    """Whether *exc* is an idle pull (no message), not a broker failure."""
    try:
        from nats.errors import TimeoutError as nats_timeout
    except Exception:  # noqa: BLE001 - without nats-py only the builtin exists
        nats_timeout = TimeoutError
    return isinstance(exc, (TimeoutError, nats_timeout))


async def _deliver(consumer: _Consumer, message) -> None:
    """Run the handlers for one message and ack, nak or dead-letter it."""
    attempts = _num_delivered(message)
    try:
        event = json.loads(message.data)
    except Exception:  # noqa: BLE001 - a payload this module cannot read
        event = None
    if not isinstance(event, dict):
        # Nothing can be routed without an envelope, and no handler can act on
        # it: dropping it is the only option that does not loop forever.
        logger.warning(
            "the durable consumer %s received an unreadable message; dropping it",
            consumer.durable,
        )
        await message.ack()
        return

    failed_handler = await asyncio.to_thread(_run_handlers, consumer, event)
    if failed_handler is None:
        await message.ack()
        return

    limit = max_deliver()
    if attempts > limit:
        # A redelivery after the final ack (a lost ack): the dead-letter message
        # and the record already exist, so only the ack is repeated.
        await message.ack()
        return
    if attempts == limit:
        await _dead_letter(consumer, message, event, attempts, failed_handler)
        await message.ack()
        return
    await message.nak(delay=_backoff_delay(attempts))


def _run_handlers(consumer: _Consumer, event: dict) -> str | None:
    """Run every recorded handler in order; name the one that failed.

    Handlers run off the bus's loop (the caller awaits a worker thread), so a
    slow handler cannot stall the connection the consumer is fetching from.
    Returns ``None`` when all of them returned -- including a handler that
    refused the event, which is a decision, not a failure.
    """
    for handler in list(consumer.bus._handlers.get(consumer.event_type, ())):
        try:
            handler(event)
        except Exception:  # noqa: BLE001 - the broker redelivers the message
            return _handler_name(handler)
    return None


def _handler_name(handler) -> str:
    """A handler's name for a log line, never its arguments or result."""
    return getattr(handler, "__name__", None) or type(handler).__name__


async def _dead_letter(
    consumer: _Consumer,
    message,
    event: dict,
    attempts: int,
    failed_handler: str,
) -> None:
    """Republish the raw message and record the dead letter exactly once."""
    event_type = consumer.event_type
    plan = event.get("plan")
    event_id = _event_id(event)
    subject = _dead_letter_subject(event_type, plan)

    if subject is None:
        logger.warning(
            "an %s event could not be dead-lettered: its envelope names no usable plan",
            event_type,
        )
    else:
        try:
            await consumer.bus._js.publish(subject, message.data)
        except Exception:  # noqa: BLE001 - the record is still written below
            logger.warning(
                "an %s event could not be republished to its dead-letter subject",
                event_type,
            )

    if subject is not None:
        _write_dead_letter_record(event_type, plan, event, event_id, attempts)

    logger.warning(
        "dead-lettering a %s event: handler %s failed", event_type, failed_handler
    )


def _dead_letter_subject(event_type: str, plan) -> str | None:
    """The dead-letter subject for *plan*, or ``None`` when it is unusable."""
    if not isinstance(plan, str) or not plan.strip():
        return None
    try:
        # The same rules the JSONL bus applies, so a name the rest of the
        # pipeline rejects can never become a subject (or a wildcard).
        validated = JsonlEventBus._validate_plan(None, plan)
    except ValueError:
        return None
    return f"{DEAD_LETTER_PREFIX}.{event_type}.{validated}"


def _event_id(event: dict):
    """The event's stable identity, as published in ``Nats-Msg-Id``."""
    return event.get("event_id") or event.get("idempotency_key")


def _write_dead_letter_record(
    event_type: str,
    plan: str,
    event: dict,
    event_id,
    attempts: int,
) -> None:
    """Append the ``event_dead_lettered`` record to the plan's sidecar.

    Written through :mod:`pipeline.persistence`'s direct writer, never through
    ``_notify_user``: that path publishes on the event bus, and the bus is the
    component whose delivery failed.
    """
    if event_id is not None and _record_exists(plan, event_id):
        return
    record = persistence._notification_record(
        plan,
        f"Event {event_type} was dead-lettered after {attempts} delivery attempts",
        event.get("story_key"),
        "error",
        DEAD_LETTER_RECORD_EVENT,
        event_id,
        datetime.datetime.now(datetime.UTC).isoformat(),
        attempt=attempts,
    )
    persistence._write_notification_record(plan, record)


def _record_exists(plan: str, event_id) -> bool:
    """Whether the dead-letter record for *event_id* is already on disk."""
    path = persistence._notifications_jsonl_path(plan)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if (
            isinstance(record, dict)
            and record.get("event") == DEAD_LETTER_RECORD_EVENT
            and record.get("dedup_key") == event_id
        ):
            return True
    return False


def _num_delivered(message) -> int:
    """How many times the broker has delivered *message*, at least once."""
    try:
        return max(int(message.metadata.num_delivered), 1)
    except Exception:  # noqa: BLE001 - a message without metadata is a first try
        return 1
