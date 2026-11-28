"""General signed outbound webhook channel type (``type: webhook``).

POSTs a signed JSON notice to a person's own automation. The URL and the HMAC
key are credentials held by reference (``url_ref``, ``secret_ref``) and
resolved through ``app.credentials`` at send time; neither value, nor the
message body, ever reaches a log record. The URL is validated after
resolution and before any socket opens: https only, no userinfo or fragment,
no IP-literal or ``localhost`` host, and every resolved address must be
publicly routable. The connection is pinned to the validated address (the
shared ``app.outbound_guard``) and redirects are never followed.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import math
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.credentials import CredentialRef, resolve
from app.outbound_guard import (
    OutboundRefused,
    is_internal_target,
    pinned_client,
    require_https_url,
    resolve_public_address,
)
from pipeline.notification_channels import (
    ChannelConfigError,
    DeliveryResult,
    DeliveryStatus,
    register_channel_type,
    require_credential_ref,
)
from pipeline.notification_render import render_notification
from pipeline.notification_slack import _secret_text

log = logging.getLogger(__name__)

_CHANNEL_KEYS = frozenset({"type", "url_ref", "secret_ref", "timeout"})
_MIN_TIMEOUT = 1
_MAX_TIMEOUT = 30
_DEFAULT_TIMEOUT = 10


def _client(url: str, address: str, timeout: float) -> httpx.Client:
    """The HTTP boundary: connects to the validated ``address``, no redirects."""
    return pinned_client(url, address, timeout)


def _reveal(ref_name: str) -> str:
    # Slack's ``_secret_text`` is the reviewed reveal point for notification
    # credentials; reuse it rather than widening the reveal allowlist.
    # Imported here: ``pipeline.actor`` pulls in the identity layer, which must
    # not load just to validate a channel.
    from pipeline.actor import current_namespace

    return _secret_text(
        resolve(CredentialRef("notification", ref_name, namespace=current_namespace()))
    ).strip()


def _validated_address(url: str) -> str | None:
    """Apply the URL rules and return the address to pin; None refuses."""
    try:
        require_https_url(url)
        host = (urlsplit(url).hostname or "").casefold().rstrip(".")
    except (OutboundRefused, ValueError):
        return None
    if host == "localhost" or is_internal_target(url):
        return None
    try:
        ipaddress.ip_address(host)
        return None  # IP literal
    except ValueError:
        pass
    return resolve_public_address(url)


def _retry_after(headers: Any) -> float | None:
    raw = headers.get("Retry-After") if headers is not None else None
    if not isinstance(raw, str):
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


def sign(secret: str, timestamp: str, body: str) -> str:
    digest = hmac.new(
        secret.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256
    ).hexdigest()
    return f"sha256={digest}"


class WebhookChannelType:
    """A routable signed webhook instance (``type: webhook``)."""

    type_name = "webhook"

    def __init__(self, name: str, *, url_ref: str, secret_ref: str, timeout: float) -> None:
        self.name = name
        self.outbox_suffix = f".outbox.{name}.jsonl"
        self._url_ref = url_ref
        self._secret_ref = secret_ref
        self._timeout = timeout

    @classmethod
    def from_settings(
        cls, instance_name: str, settings: Mapping[str, Any], env: Mapping[str, str]
    ) -> WebhookChannelType:
        for key in settings:
            if key not in _CHANNEL_KEYS:
                raise ChannelConfigError(f"channel {instance_name!r}: unknown setting {key!r}")
        refs = {}
        for key in ("url_ref", "secret_ref"):
            ref = require_credential_ref(instance_name, key, settings.get(key))
            if ref is None:
                raise ChannelConfigError(
                    f"channel {instance_name!r}: setting {key!r} is required"
                )
            refs[key] = ref
        timeout = settings.get("timeout", _DEFAULT_TIMEOUT)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int | float)
            or (isinstance(timeout, float) and not math.isfinite(timeout))
            or not _MIN_TIMEOUT <= timeout <= _MAX_TIMEOUT
        ):
            raise ChannelConfigError(
                f"channel {instance_name!r}: setting 'timeout' must be between "
                f"{_MIN_TIMEOUT} and {_MAX_TIMEOUT} seconds"
            )
        return cls(
            instance_name,
            url_ref=refs["url_ref"],
            secret_ref=refs["secret_ref"],
            timeout=float(timeout),
        )

    def send(self, record: dict) -> DeliveryResult:
        try:
            return self._send(record)
        except Exception as exc:  # noqa: BLE001 - a channel send must never raise
            log.error(
                "webhook channel send failed: channel=%s error_type=%s",
                self.name,
                type(exc).__name__,
            )
            return DeliveryResult(DeliveryStatus.RETRY)

    def _body(self, record: dict) -> str | None:
        try:
            rendered = render_notification(record)
            if not isinstance(rendered.body, str):
                raise TypeError("body must be a string")
            return json.dumps(
                {
                    "version": 1,
                    "event": rendered.event,
                    "plan": rendered.plan,
                    "story_key": rendered.story_key,
                    "subject": rendered.subject,
                    "body": rendered.body,
                    "link": rendered.link,
                    "sent_at": datetime.now(UTC).isoformat(),
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        except Exception as exc:  # noqa: BLE001 - untrusted spooled record
            log.error(
                "webhook channel rejected record: channel=%s error_type=%s",
                self.name,
                type(exc).__name__,
            )
            return None

    def _send(self, record: dict) -> DeliveryResult:
        body = self._body(record)
        if body is None:
            return DeliveryResult(DeliveryStatus.REJECTED, reason="malformed_record")
        try:
            url = _reveal(self._url_ref)
            secret = _reveal(self._secret_ref)
        except Exception as exc:  # noqa: BLE001 - a failed lookup is a config fault
            log.error(
                "webhook channel credential unavailable: channel=%s error_type=%s",
                self.name,
                type(exc).__name__,
            )
            return DeliveryResult(DeliveryStatus.REJECTED, reason="credential_unavailable")
        if not url or not secret:
            log.error("webhook channel credential unset: channel=%s", self.name)
            return DeliveryResult(DeliveryStatus.REJECTED, reason="credential_unavailable")
        address = _validated_address(url)
        if address is None:
            log.error("webhook channel refused url: channel=%s", self.name)
            return DeliveryResult(DeliveryStatus.REJECTED, reason="url_refused")

        timestamp = str(int(time.time()))
        headers = {
            "Content-Type": "application/json",
            "X-Fagan-Timestamp": timestamp,
            "X-Fagan-Signature": sign(secret, timestamp, body),
            "User-Agent": "fagan-notify/1",
        }
        try:
            with _client(url, address, self._timeout) as client:
                response = client.post(url, content=body.encode(), headers=headers)
        except Exception as exc:  # noqa: BLE001 - timeout/connection: retry later; _failed logs the class name
            return self._failed(exc, DeliveryResult(DeliveryStatus.RETRY))
        return self._http_status(response.status_code, response.headers)

    def _http_status(self, status: int, headers: Any) -> DeliveryResult:
        if 200 <= status < 300:
            log.info("webhook channel sent: channel=%s", self.name)
            return DeliveryResult(DeliveryStatus.DELIVERED)
        exc = RuntimeError()
        if 300 <= status < 400:
            return self._failed(exc, DeliveryResult(DeliveryStatus.REJECTED, reason="redirect"))
        if status in (408, 429):
            return self._failed(
                exc, DeliveryResult(DeliveryStatus.RETRY, retry_after_seconds=_retry_after(headers))
            )
        if status >= 500:
            return self._failed(
                exc, DeliveryResult(DeliveryStatus.RETRY, retry_after_seconds=_retry_after(headers))
            )
        return self._failed(exc, DeliveryResult(DeliveryStatus.REJECTED, reason="http_4xx"))

    def _failed(self, exc: Exception, result: DeliveryResult) -> DeliveryResult:
        log.warning(
            "webhook channel send failed: channel=%s error_type=%s",
            self.name,
            type(exc).__name__,
        )
        return result


register_channel_type(WebhookChannelType)
