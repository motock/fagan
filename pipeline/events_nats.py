"""NATS JetStream-backed event bus (selected by ``PIPELINE_EVENT_BUS=nats``).

ADR 0010 makes the bus the pipeline's delivery contract: a publisher must know
whether a durable subscriber will pick an event up (``EventBus.delivers``) and
must not block forever when the broker cannot acknowledge. This module provides
that bus.

Design notes
------------
* **Fail closed at construction.** :class:`NatsEventBus` validates every
  ``PIPELINE_NATS_*`` variable before it opens a socket and refuses to run
  against a stream it was not told how to create (ADR 0004). A refused
  configuration therefore never leaves a half-open connection behind.
* **Synchronous facade, private loop.** The pipeline publishes from ordinary
  synchronous code. Every broker operation is scheduled onto one private
  asyncio loop running on a daemon thread. The loop belongs to the process that
  created it: after ``fork()`` the child drops the inherited references and
  builds its own, because a thread does not survive ``fork`` while its
  ``asyncio`` loop object does (ADR 0010 consequences).
* **Broker and subscribers both.** Like ``DispatchingJsonlEventBus``, this bus
  persists *and* delivers: it publishes to JetStream and then dispatches the
  event to its own subscribers. Selecting a bus that only persisted would stop
  every handler in the publishing process from running. The one exception is a
  publish the stream reports as a duplicate -- that event was already
  delivered, so dispatching it again would double the work the message id
  exists to suppress.
* **No ack bookkeeping here.** ``nats-py`` tracks outstanding PubAcks on its own
  JetStream context. This module keeps only the loop/connection state and the
  handler registry; a per-publish map would go stale the moment a publish timed
  out and a retry reused the same ``Nats-Msg-Id``.
* **Duplicate suppression is the broker's job.** An event that carries an
  ``event_id`` or an ``idempotency_key`` is published with a ``Nats-Msg-Id``
  header, so a republished event is deduplicated by the stream instead of by
  process-local memory.
* **Logs and errors name the variable or the event type only.** A URL may embed
  a password and a payload may hold anything at all, so neither is ever
  formatted into a message.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import json
import logging
import math
import os
import ssl
import stat
import threading
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .events import EventBus, JsonlEventBus

__all__ = ["EventPublishError", "NatsEventBus", "nats_settings_from_env"]

logger = logging.getLogger(__name__)

#: ``PIPELINE_NATS_URL`` -- the server to publish to (required).
ENV_NATS_URL = "PIPELINE_NATS_URL"
#: ``PIPELINE_NATS_CREDS_FILE`` -- optional NATS user-credentials file.
ENV_NATS_CREDS_FILE = "PIPELINE_NATS_CREDS_FILE"
#: ``PIPELINE_NATS_TLS_CA_FILE`` -- CA bundle that makes a plaintext-looking
#: remote URL acceptable, and that the connection is verified against.
ENV_NATS_TLS_CA_FILE = "PIPELINE_NATS_TLS_CA_FILE"
#: ``PIPELINE_NATS_STREAM`` -- JetStream stream name (default ``PIPELINE``).
ENV_NATS_STREAM = "PIPELINE_NATS_STREAM"
#: ``PIPELINE_EVENT_PUBLISH_TIMEOUT_S`` -- upper bound on one publish (default 5).
ENV_PUBLISH_TIMEOUT_S = "PIPELINE_EVENT_PUBLISH_TIMEOUT_S"
#: ``PIPELINE_NATS_MANAGE_STREAM`` -- ``1`` lets the bus create the stream.
ENV_MANAGE_STREAM = "PIPELINE_NATS_MANAGE_STREAM"
#: ``PIPELINE_NATS_DURABLE_TYPES`` -- comma-separated event types a durable
#: consumer in another process subscribes to.
ENV_DURABLE_TYPES = "PIPELINE_NATS_DURABLE_TYPES"

DEFAULT_STREAM = "PIPELINE"
DEFAULT_PUBLISH_TIMEOUT_S = 5.0
#: Event types the long-running consumer subscribes to when the environment
#: does not say otherwise.
DEFAULT_DURABLE_TYPES = frozenset({"notification", "agent_done"})
#: Subjects the managed stream captures: every pipeline event.
STREAM_SUBJECTS = ["pipeline.>"]
#: How long the managed stream retains an event.
STREAM_MAX_AGE_S = 7 * 24 * 60 * 60

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_HEADER_MSG_ID = "Nats-Msg-Id"
_CREDS_MODE = 0o600
_CLOSE_TIMEOUT_S = 2.0
#: How long a cancelled broker call is given to unwind before its future is
#: abandoned: cancellation that lands after the loop stops raises from the
#: loop's own callback handling, which no caller can act on.
_CANCEL_SETTLE_S = 2.0


class EventPublishError(RuntimeError):
    """An event could not be published (broker unreachable, unacknowledged...).

    The message names the event type and the failure class only: an event
    payload is never part of it.
    """


@dataclass(frozen=True)
class _NatsSettings:
    """Validated ``PIPELINE_NATS_*`` configuration for one bus instance."""

    url: str
    stream: str
    publish_timeout_s: float
    manage_stream: bool
    creds_file: str | None = None
    tls_ca_file: str | None = None
    durable_types: frozenset[str] = field(default_factory=frozenset)


def _refuse(variable: str, reason: str) -> ValueError:
    """A :class:`ValueError` that names the offending *variable*, never its value."""
    return ValueError(f"{variable} {reason}")


def _validate_url_setting(url: str, tls_ca_file: str | None) -> None:
    """Refuse a URL that cannot be trusted to carry pipeline events.

    A loopback server may be plain text (that is how ``nats-server -js`` runs
    in CI). Anything else must be encrypted: an explicit ``tls://`` URL, or a
    ``nats://`` URL *plus* the CA bundle the connection is verified against.
    """
    parts = urlsplit(url)
    if parts.scheme not in {"nats", "tls"} or not parts.hostname:
        raise _refuse(ENV_NATS_URL, "must be a nats:// or tls:// URL with a host")

    if parts.hostname in _LOOPBACK_HOSTS or parts.scheme == "tls":
        return

    if not (tls_ca_file or "").strip():
        raise _refuse(
            ENV_NATS_URL,
            "points at a remote host over plain text; use a tls:// URL or set "
            f"{ENV_NATS_TLS_CA_FILE}",
        )


def _validate_creds_file(raw: str) -> str:
    """Refuse a credentials file that is missing or readable by anyone else."""
    path = Path(raw)
    if not path.is_file():
        raise _refuse(ENV_NATS_CREDS_FILE, "must name an existing file")
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        raise _refuse(ENV_NATS_CREDS_FILE, "must name a readable file") from None
    if mode != _CREDS_MODE:
        raise _refuse(
            ENV_NATS_CREDS_FILE, "must be readable by its owner only (mode 600)"
        )
    return raw


def _parse_publish_timeout(raw: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise _refuse(ENV_PUBLISH_TIMEOUT_S, "must be a positive number") from None
    if not math.isfinite(value) or value <= 0:
        raise _refuse(ENV_PUBLISH_TIMEOUT_S, "must be a positive number")
    return value


def _parse_durable_types(raw: str | None) -> frozenset[str]:
    """Durable types, under-claiming on an ambiguous value.

    Unset means "the daemon's default set". Set-but-blank means "nothing is
    consumed durably": that is the fail-closed direction, because a publisher
    that believes a type is consumed elsewhere stops doing the work inline.
    """
    if raw is None:
        return DEFAULT_DURABLE_TYPES
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def nats_settings_from_env() -> _NatsSettings:
    """Read and validate the ``PIPELINE_NATS_*`` configuration.

    Returns
    -------
    _NatsSettings
        The validated settings.

    Raises
    ------
    ValueError
        Naming the offending variable (never its value) when a setting is
        missing or unusable.
    """
    raw_url = (os.environ.get(ENV_NATS_URL) or "").strip()
    if not raw_url:
        raise _refuse(ENV_NATS_URL, "must be set to the NATS server URL")

    tls_ca_file = (os.environ.get(ENV_NATS_TLS_CA_FILE) or "").strip() or None
    _validate_url_setting(raw_url, tls_ca_file)

    raw_creds = (os.environ.get(ENV_NATS_CREDS_FILE) or "").strip()
    creds_file = _validate_creds_file(raw_creds) if raw_creds else None

    raw_timeout = os.environ.get(ENV_PUBLISH_TIMEOUT_S)
    timeout = (
        DEFAULT_PUBLISH_TIMEOUT_S
        if raw_timeout is None
        else _parse_publish_timeout(raw_timeout)
    )

    stream = (os.environ.get(ENV_NATS_STREAM) or "").strip() or DEFAULT_STREAM
    manage_stream = (os.environ.get(ENV_MANAGE_STREAM) or "").strip() == "1"

    return _NatsSettings(
        url=raw_url,
        stream=stream,
        publish_timeout_s=timeout,
        manage_stream=manage_stream,
        creds_file=creds_file,
        tls_ca_file=tls_ca_file,
        durable_types=_parse_durable_types(os.environ.get(ENV_DURABLE_TYPES)),
    )


def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Thread target: make *loop* this thread's loop and run it forever."""
    asyncio.set_event_loop(loop)
    try:
        loop.run_forever()
    finally:
        loop.close()


async def _drain_pending_tasks() -> None:
    """Cancel every other task on the bus's loop and let them unwind.

    The client keeps a protocol flusher (and, once consuming, a subscription
    reader) pending on this loop. Closing the loop with those still alive
    raises from the loop's own callback handling, which no caller can act on,
    so teardown cancels them and waits for the cancellation to land first.
    """
    current = asyncio.current_task()
    pending = [task for task in asyncio.all_tasks() if task is not current]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


class NatsEventBus(EventBus):
    """Publish pipeline events to NATS JetStream.

    Every setting comes from the ``PIPELINE_NATS_*`` environment; see
    :func:`nats_settings_from_env`.

    Raises
    ------
    ValueError
        If a setting is missing or unusable, or the stream does not exist and
        ``PIPELINE_NATS_MANAGE_STREAM`` is not ``1``. Nothing is dialled before
        every setting has been validated.
    """

    def __init__(self) -> None:
        # (1) Pure validation: no thread, no socket, no `nats` import yet.
        settings = nats_settings_from_env()
        self._settings = settings
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._pid: int | None = None
        self._nc = None
        self._js = None
        self._stream_ready = False
        self._handlers: dict[str, list] = {}
        self._durable_types: frozenset[str] = settings.durable_types

        # (2) Only a configuration that passed validation may connect. Every
        # setting is already validated above, so a broker that does not answer
        # yet is not a configuration error: it is reported by the first
        # `publish` as EventPublishError, which is the failure shape call sites
        # already tolerate. A reachable broker that refuses the stream is a
        # configuration error and refuses construction here.
        try:
            loop = self._ensure_loop()
            self._ensure_connection(loop)
        except ValueError:
            self._shutdown()
            raise
        except Exception:  # noqa: BLE001 - a silent broker is not a bad setting
            self._shutdown()
            logger.warning(
                "the NATS broker named by %s did not answer; the first publish "
                "retries the connection",
                ENV_NATS_URL,
            )

    # -- public EventBus API -------------------------------------------------
    def subscribe(self, event_type: str, handler) -> None:
        """Record *handler* for *event_type*.

        Recorded handlers run in this process after a successful publish, and
        are the same handlers the durable consumer process serves.
        """
        self._handlers.setdefault(event_type, []).append(handler)

    def delivers(self, event_type: str) -> bool:
        """Whether *event_type* reaches a subscriber, local or durable."""
        return bool(self._handlers.get(event_type)) or (
            event_type in self._durable_types
        )

    def publish(self, event: dict) -> None:
        """Publish *event* to ``pipeline.<type>.<plan>`` and await its PubAck.

        Raises
        ------
        ValueError
            If the event has no usable ``type``/``plan``, or the plan is not a
            valid plan identifier (the same rules ``JsonlEventBus`` applies).
        EventPublishError
            If JetStream does not acknowledge the event within
            ``PIPELINE_EVENT_PUBLISH_TIMEOUT_S``, or the broker refuses it.
        """
        event_type = self._event_type(event)
        subject = self._subject(event_type, self._event_plan(event))
        headers = self._headers(event)
        payload = json.dumps(event, separators=(",", ":"), default=str).encode()

        try:
            loop = self._ensure_loop()
            self._ensure_connection(loop)
        except ValueError:
            raise
        except Exception:  # noqa: BLE001 - a dead broker becomes one typed error
            raise EventPublishError(
                f"publishing a {event_type} event could not reach the NATS broker"
            ) from None

        future = asyncio.run_coroutine_threadsafe(
            self._publish_async(subject, payload, headers), loop
        )
        try:
            ack = future.result(self._settings.publish_timeout_s)
        except TimeoutError:
            future.cancel()
            raise EventPublishError(
                f"publishing a {event_type} event timed out after "
                f"{self._settings.publish_timeout_s}s"
            ) from None
        except concurrent.futures.CancelledError:
            raise EventPublishError(
                f"publishing a {event_type} event was cancelled"
            ) from None
        except EventPublishError:
            raise
        except Exception:  # noqa: BLE001 - broker failures become one typed error
            raise EventPublishError(
                f"publishing a {event_type} event to JetStream failed"
            ) from None

        if not getattr(ack, "duplicate", False):
            self._dispatch(event_type, event)

    def close(self) -> None:
        """Stop the private loop and the thread that runs it (idempotent)."""
        self._shutdown()

    # -- event field extraction ---------------------------------------------
    @staticmethod
    def _event_type(event: dict) -> str:
        if not isinstance(event, dict):
            raise ValueError("Event must be a mapping")  # noqa: TRY004
        event_type = event.get("type")
        if not isinstance(event_type, str) or not event_type.strip():
            raise ValueError("Event must contain a non-empty 'type' key")
        return event_type.strip()

    @staticmethod
    def _event_plan(event: dict) -> str:
        plan = event.get("plan")
        if not plan:
            raise ValueError("Event must contain a non-empty 'plan' key")
        # Reuse the JSONL bus's rules rather than restating them here, so the
        # two buses can never disagree about what a plan identifier is.
        # ``JsonlEventBus._validate_plan`` reads no instance state -- it is a
        # pure function of the plan string -- so the unbound function is called
        # directly: a broker bus owns no JSONL append-log and must not build one
        # just to validate a name. The reuse is pinned by
        # test_publish_reuses_the_jsonl_bus_plan_rules.
        return JsonlEventBus._validate_plan(None, plan)

    @staticmethod
    def _subject(event_type: str, plan: str) -> str:
        return f"pipeline.{event_type}.{plan}"

    @staticmethod
    def _headers(event: dict) -> dict[str, str]:
        """The ``Nats-Msg-Id`` header, present only when the event has an id.

        The stream deduplicates on this value, so it must be *stable*: derived
        from the event, never minted per publish.
        """
        message_id = event.get("event_id") or event.get("idempotency_key")
        if not message_id:
            return {}
        return {_HEADER_MSG_ID: str(message_id)}

    def _dispatch(self, event_type: str, event: dict) -> None:
        """Run this process's handlers for a freshly acknowledged event."""
        for handler in self._handlers.get(event_type, []):
            try:
                handler(event)
            except Exception as exc:  # noqa: BLE001 - one handler must not silence others
                # The type and the exception's class, never the payload or the
                # exception text: a handler failure must be diagnosable without
                # leaking event contents.
                error_type = type(exc).__name__
                logger.error(
                    "Event handler failed for event type %s (%s)",
                    event_type,
                    error_type,
                    extra={"error_type": error_type},
                )

    # -- private loop lifecycle ---------------------------------------------
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        """Return this process's private loop, building one when needed.

        A loop inherited across ``fork()`` is unusable: the thread that ran it
        does not exist in the child. The pid is compared *before* it is
        rewritten -- assigning ``self._pid`` first would make the check always
        true and leave the child driving a dead loop.
        """
        pid = os.getpid()
        if (
            self._loop is not None
            and self._thread is not None
            and self._thread.is_alive()
            and self._pid == pid
            and not self._loop.is_closed()
        ):
            return self._loop

        # Drop the inherited references; never close or join them, because in a
        # forked child they describe a thread that does not exist. The stream
        # context goes with them: it is bound to the loop that is being left.
        self._loop = None
        self._thread = None
        self._nc = None
        self._js = None
        self._stream_ready = False

        loop = asyncio.new_event_loop()
        thread = threading.Thread(
            target=_run_loop, args=(loop,), name="pipeline-events-nats", daemon=True
        )
        thread.start()
        self._loop = loop
        self._thread = thread
        self._pid = pid
        return loop

    def _shutdown(self) -> None:
        """Tear down the connection, the loop and its thread (idempotent)."""
        loop, thread, nc = self._loop, self._thread, self._nc
        self._loop = None
        self._thread = None
        self._pid = None
        self._nc = None
        self._js = None
        self._stream_ready = False
        if loop is None:
            return
        if loop.is_running():
            if nc is not None:
                try:
                    asyncio.run_coroutine_threadsafe(nc.close(), loop).result(
                        _CLOSE_TIMEOUT_S
                    )
                except Exception:  # noqa: BLE001 - best-effort teardown
                    logger.debug("closing the NATS connection failed")
            try:
                asyncio.run_coroutine_threadsafe(
                    _drain_pending_tasks(), loop
                ).result(_CANCEL_SETTLE_S)
            except Exception:  # noqa: BLE001 - best-effort teardown
                logger.debug("draining the NATS loop's pending tasks failed")
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread.is_alive():
            thread.join(timeout=_CLOSE_TIMEOUT_S)

    # -- broker operations (async; every `nats` import stays local) ----------
    def _ensure_connection(self, loop: asyncio.AbstractEventLoop) -> None:
        """Connect and verify the stream once per process, on a fixed budget.

        Dialling and the stream check share the publish timeout as their
        budget: neither may outlast a publish, so a broker that answers slowly
        (or not at all) fails the publish instead of wedging the caller for as
        long as the client's own reconnect loop feels like retrying.
        """
        if self._nc is not None and self._stream_ready:
            return
        budget = self._settings.publish_timeout_s
        nc = self._run(loop, self._connect_async(), budget)
        self._nc = nc
        self._js = nc.jetstream()
        try:
            self._run(loop, self._verify_stream_async(), budget)
        except BaseException:
            self._nc = None
            self._js = None
            raise
        self._stream_ready = True

    def _run(self, loop: asyncio.AbstractEventLoop, coro, timeout: float | None = None):
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        if timeout is None:
            return future.result()
        try:
            return future.result(timeout)
        except TimeoutError:
            # Abandon the attempt and let the cancellation settle here, while
            # the loop is still running: a dial still in flight must not keep
            # the loop busy, nor land a connection nobody will use.
            future.cancel()
            with contextlib.suppress(BaseException):
                future.result(_CANCEL_SETTLE_S)
            raise

    async def _connect_async(self):
        import nats  # local import: keeps this module importable without nats-py

        options: dict = {
            "servers": [self._settings.url],
            "name": "pipeline",
            # One refusal is enough to know the broker is not there: the
            # client's own retry loop would otherwise spend its default
            # attempts before reporting it.
            "max_reconnect_attempts": 1,
            "reconnect_time_wait": 0.1,
            "connect_timeout": self._settings.publish_timeout_s,
        }
        if self._settings.creds_file:
            options["user_credentials"] = self._settings.creds_file
        if self._settings.tls_ca_file:
            options["tls"] = ssl.create_default_context(
                cafile=self._settings.tls_ca_file
            )
        return await nats.connect(**options)

    async def _verify_stream_async(self) -> None:
        from nats.js.errors import NotFoundError

        stream = self._settings.stream
        try:
            await self._js.stream_info(stream)
            return
        except NotFoundError:
            if not self._settings.manage_stream:
                raise _refuse(
                    ENV_NATS_STREAM,
                    f"names stream {stream!r}, which does not exist; set "
                    f"{ENV_MANAGE_STREAM}=1 to create it",
                ) from None

        await self._js.add_stream(
            name=stream,
            subjects=list(STREAM_SUBJECTS),
            max_age=STREAM_MAX_AGE_S,
            storage=self._file_storage(),
        )

    @staticmethod
    def _file_storage():
        """File-backed storage, spelled the way the installed client expects."""
        try:
            from nats.js.api import StorageType
        except ImportError:
            return "file"
        return StorageType.FILE

    async def _publish_async(self, subject: str, payload: bytes, headers: dict):
        kwargs: dict = {"payload": payload}
        if headers:
            kwargs["headers"] = headers
        return await self._js.publish(subject, **kwargs)
