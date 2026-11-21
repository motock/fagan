"""General signed outbound webhook channel type (``type: webhook``).

POSTs a signed JSON notice to a person's own automation. The URL and the HMAC
key are credentials held by reference (``url_ref``, ``secret_ref``) and
resolved through ``app.credentials`` at send time; neither value, nor the
message body, ever reaches a log record. The URL is validated after
resolution and before any socket opens: https only, no userinfo or fragment,
no IP-literal or ``localhost`` host, and every resolved address must be
publicly routable. Redirects are never followed.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import math
import socket
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from app.credentials import CredentialRef, resolve
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


class _RedirectBlocked(Exception):
    """A 30x response was received; it is never followed."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise _RedirectBlocked


def _build_opener() -> urllib.request.OpenerDirector:
    """The HTTP boundary: verifying TLS context, redirects raise."""
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        _NoRedirectHandler(),
    )


def _reveal(ref_name: str) -> str:
    # Slack's ``_secret_text`` is the reviewed reveal point for notification
    # credentials; reuse it rather than widening the reveal allowlist.
    return _secret_text(resolve(CredentialRef("notification", ref_name))).strip()


def _is_public_address(text: str) -> bool:
    try:
        addr = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return False
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def _url_is_allowed(url: str) -> bool:
    """Apply the URL rules; any doubt refuses."""
    if not url or any(c.isspace() or ord(c) < 32 for c in url):
        return False
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError:
        return False
    if parts.scheme != "https" or not host:
        return False
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return False
    if parts.fragment or "#" in url:
        return False
    if host.lower().rstrip(".") == "localhost":
        return False
    try:
        ipaddress.ip_address(host)
        return False  # IP literal
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, parts.port or 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    addresses = [info[4][0] for info in infos]
    return bool(addresses) and all(_is_public_address(a) for a in addresses)


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
        if not _url_is_allowed(url):
            log.error("webhook channel refused url: channel=%s", self.name)
            return DeliveryResult(DeliveryStatus.REJECTED, reason="url_refused")

        timestamp = str(int(time.time()))
        request = urllib.request.Request(
            url,
            data=body.encode(),
            headers={
                "Content-Type": "application/json",
                "X-Fagan-Timestamp": timestamp,
                "X-Fagan-Signature": sign(secret, timestamp, body),
                "User-Agent": "fagan-notify/1",
            },
            method="POST",
        )
        try:
            with _build_opener().open(request, timeout=self._timeout) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            return self._http_status(exc.code, exc.headers, exc)
        except _RedirectBlocked as exc:
            return self._failed(exc, DeliveryResult(DeliveryStatus.REJECTED, reason="redirect"))
        except Exception as exc:  # noqa: BLE001 - timeout/DNS/connection: retry later
            return self._failed(exc, DeliveryResult(DeliveryStatus.RETRY))
        return self._http_status(status, None, None)

    def _http_status(self, status: int, headers: Any, exc: Exception | None) -> DeliveryResult:
        if 200 <= status < 300:
            log.info("webhook channel sent: channel=%s", self.name)
            return DeliveryResult(DeliveryStatus.DELIVERED)
        if exc is None:
            exc = RuntimeError()
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
