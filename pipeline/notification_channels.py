"""Notification channel type contract and registry (stdlib only).

Design rule: one queue per channel INSTANCE (``<plan>.outbox.<name>.jsonl``),
so each instance retries independently and a record is never resent to an
instance that already accepted it.

A channel TYPE is a class with ``type_name`` and a ``from_settings``
classmethod that validates instance settings.  A channel INSTANCE has
``name``, ``outbox_suffix`` and ``send(record) -> DeliveryResult``, which
must never raise.  Secrets never appear in settings: a ``<x>_env`` setting
holds the NAME of an environment variable, resolved through an ``env``
mapping at call time.

The type registry is cumulative: later stories register more types.
"""

from __future__ import annotations

import enum
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pipeline import paths

logger = logging.getLogger(__name__)

_TYPE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_INSTANCE_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_SECRET_KEY_FRAGMENTS = ("password", "token", "webhook_url", "secret", "api_key")
_ENV_SUFFIX = "_env"


class ChannelConfigError(ValueError):
    """Invalid channel instance settings; never carries a secret value."""


class DeliveryStatus(enum.Enum):
    DELIVERED = "delivered"
    RETRY = "retry"
    REJECTED = "rejected"


@dataclass(frozen=True)
class DeliveryResult:
    """Outcome of one send. ``reason`` is a short fixed token, never exception
    text, a URL, or a recipient."""

    status: DeliveryStatus
    retry_after_seconds: float | None = None
    reason: str = ""


class Channel(Protocol):
    name: str
    outbox_suffix: str

    def send(self, record: dict) -> DeliveryResult: ...


_REGISTRY: dict[str, Any] = {}


def register_channel_type(cls: Any) -> None:
    type_name = getattr(cls, "type_name", None)
    if not isinstance(type_name, str) or not _TYPE_NAME_RE.match(type_name):
        raise ValueError("invalid channel type_name")
    if type_name in _REGISTRY:
        raise ValueError(f"channel type already registered: {type_name}")
    _REGISTRY[type_name] = cls


def channel_type(type_name: str) -> Any:
    try:
        return _REGISTRY[type_name]
    except KeyError:
        raise KeyError(type_name) from None


def registered_types() -> list[str]:
    return sorted(_REGISTRY)


def validate_instance_name(name: str) -> None:
    if not isinstance(name, str) or not _INSTANCE_NAME_RE.match(name):
        raise ChannelConfigError(f"invalid channel instance name: {name!r}")


def reject_inline_secrets(instance_name: str, settings: Mapping[str, Any]) -> None:
    """Raise if any setting key looks like a secret held inline."""
    for key in settings:
        lowered = str(key).lower()
        if lowered.endswith(_ENV_SUFFIX):
            continue
        if any(fragment in lowered for fragment in _SECRET_KEY_FRAGMENTS):
            raise ChannelConfigError(
                f"channel {instance_name!r}: setting {key!r} must not hold a secret "
                f"inline; use {key}{_ENV_SUFFIX} naming an environment variable"
            )


def resolve_env(
    instance_name: str, settings: Mapping[str, Any], key: str, env: Mapping[str, str]
) -> str:
    """Resolve the env var NAMED by ``settings[key]`` through ``env``."""
    var_name = settings.get(key)
    if not isinstance(var_name, str) or not var_name:
        raise ChannelConfigError(
            f"channel {instance_name!r}: setting {key!r} is required"
        )
    value = env.get(var_name)
    if not value:
        raise ChannelConfigError(
            f"channel {instance_name!r}: environment variable named by {key!r} is not set"
        )
    return value


def as_sender(channel: Channel) -> Callable[[dict], bool]:
    """Adapt a channel to the ``record -> bool`` sender ``_drain_one_outbox`` needs."""

    def sender(record: dict) -> bool:
        try:
            result = channel.send(record)
        except Exception as exc:  # noqa: BLE001 - a bad channel must not break the drain
            logger.warning(
                "channel send raised: channel=%s error_type=%s",
                channel.name,
                type(exc).__name__,
            )
            return False
        if result.status is DeliveryStatus.DELIVERED:
            return True
        if result.status is DeliveryStatus.REJECTED:
            logger.error(
                "channel permanently rejected record; dropping: channel=%s reason=%s",
                channel.name,
                result.reason,
            )
            return True
        return False

    return sender


def channel_outbox_path(plan: str, channel: Channel) -> Path:
    if not plan or "/" in plan or "\\" in plan or ".." in plan:
        raise ValueError("invalid plan name")
    return paths.PLAN_DIR / f"{plan}{channel.outbox_suffix}"
