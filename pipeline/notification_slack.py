"""Slack incoming-webhook channel type (NC-07).

The webhook URL is a credential: instance settings hold either the NAME of the
environment variable (``webhook_url_env``) or a credential reference
(``webhook_url_ref``) that the provider seam resolves at send time.  The
URL never reaches a log record, an exception message that escapes, or a
``DeliveryResult``; failures are logged as the exception TYPE only.  The URL
must be ``https`` and redirects are never followed, so a 30x can neither
downgrade the transport nor re-send the body to another host.
"""

from __future__ import annotations

import json
import logging
import math
import os
import ssl
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from app.credentials import CredentialRef, resolve
from pipeline.notification_channels import (
    ChannelConfigError,
    DeliveryResult,
    DeliveryStatus,
    register_channel_type,
    reject_inline_secrets,
    require_credential_ref,
)
from pipeline.notification_render import render_notification

log = logging.getLogger(__name__)

MAX_TEXT_CHARS = 3000
TRUNCATION_MARKER = "[... truncated ...]"
_CHANNEL_KEYS = frozenset(
    {"type", "webhook_url_env", "webhook_url_ref", "timeout"}
)
_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}
_PARTIAL_ENTITY_TAIL = ("&amp", "&lt", "&gt", "&am", "&l", "&g", "&a", "&")


class _RedirectBlocked(Exception):
    """A 30x response was received; it is never followed."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise _RedirectBlocked


def _secret_text(secret: Any) -> str:
    """The ONE place a Slack webhook credential is revealed for injection.

    A dispatched child is prompt-injectable, so the raw value must not travel:
    everything else treats the ``SecretValue`` as opaque. A later egress proxy
    replaces this function to hand out a placeholder instead.
    """
    return secret.reveal() if secret is not None else ""


def _build_opener() -> urllib.request.OpenerDirector:
    """The HTTP boundary: verifying TLS context, redirects raise."""
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        _NoRedirectHandler(),
    )


def _escape(text: str) -> str:
    return "".join(_ESCAPES.get(c, c) for c in text)


def _slack_text(subject: str, body: str) -> str:
    """Escape ``& < >`` and cap at ``MAX_TEXT_CHARS`` with a visible marker."""
    text = _escape(f"{subject}\n{body}")
    if len(text) <= MAX_TEXT_CHARS:
        return text
    kept = text[: MAX_TEXT_CHARS - len(TRUNCATION_MARKER) - 1]
    # Do not leave half an escape sequence ("&a") at the cut.
    for tail in _PARTIAL_ENTITY_TAIL:
        if kept.endswith(tail) and not kept.endswith(("&amp;", "&lt;", "&gt;")):
            kept = kept[: -len(tail)]
            break
    return f"{kept}\n{TRUNCATION_MARKER}"


def _is_valid_webhook_url(url: str) -> bool:
    if any(c.isspace() or ord(c) < 32 for c in url):
        return False
    try:
        parts = urlsplit(url)
        return parts.scheme == "https" and bool(parts.hostname)
    except ValueError:
        return False


_MAX_TIMEOUT = 3600  # also keeps a huge JSON integer out of float()


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


class SlackChannelType:
    """A routable Slack channel instance (``type: slack`` in notification.json)."""

    type_name = "slack"

    def __init__(
        self,
        name: str,
        *,
        webhook_url_env: str | None,
        webhook_url_ref: str | None,
        timeout: float,
    ) -> None:
        self.name = name
        self.outbox_suffix = f".outbox.{name}.jsonl"
        self._webhook_url_env = webhook_url_env
        self._webhook_url_ref = webhook_url_ref
        self._timeout = timeout

    @classmethod
    def from_settings(
        cls, instance_name: str, settings: Mapping[str, Any], env: Mapping[str, str]
    ) -> SlackChannelType:
        reject_inline_secrets(instance_name, settings)
        for key in settings:
            if key not in _CHANNEL_KEYS:
                raise ChannelConfigError(f"channel {instance_name!r}: unknown setting {key!r}")
        env_name = settings.get("webhook_url_env")
        # ``None`` means "not given"; an empty string is still an error, so the
        # existing message and behaviour for a blank env name are preserved.
        if env_name is not None and (not isinstance(env_name, str) or not env_name):
            raise ChannelConfigError(
                f"channel {instance_name!r}: setting 'webhook_url_env' must name an env var"
            )
        ref_name = require_credential_ref(
            instance_name, "webhook_url_ref", settings.get("webhook_url_ref")
        )
        if env_name is not None and ref_name is not None:
            raise ChannelConfigError(
                f"channel {instance_name!r}: settings webhook_url_env and "
                "webhook_url_ref are mutually exclusive"
            )
        if env_name is None and ref_name is None:
            raise ChannelConfigError(
                f"channel {instance_name!r}: setting 'webhook_url_env' must name an env var"
            )
        timeout = settings.get("timeout", 20)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int | float)
            or timeout <= 0
            or (isinstance(timeout, float) and not math.isfinite(timeout))
            or timeout > _MAX_TIMEOUT
        ):
            raise ChannelConfigError(
                f"channel {instance_name!r}: setting 'timeout' must be a positive number"
            )
        return cls(
            instance_name,
            webhook_url_env=env_name,
            webhook_url_ref=ref_name,
            timeout=float(timeout),
        )

    def send(self, record: dict) -> DeliveryResult:
        try:
            return self._send(record)
        except Exception as exc:  # noqa: BLE001 - a channel send must never raise
            log.error(
                "slack channel send failed: channel=%s error_type=%s",
                self.name,
                type(exc).__name__,
            )
            return DeliveryResult(DeliveryStatus.RETRY)

    def _send(self, record: dict) -> DeliveryResult:
        try:
            rendered = render_notification(record)
            if not isinstance(rendered.body, str):
                raise TypeError("body must be a string")
            payload = json.dumps({"text": _slack_text(rendered.subject, rendered.body)}).encode()
        except Exception as exc:  # noqa: BLE001 - untrusted spooled record
            log.error(
                "slack channel rejected record: channel=%s error_type=%s",
                self.name,
                type(exc).__name__,
            )
            return DeliveryResult(DeliveryStatus.REJECTED, reason="malformed_record")

        if self._webhook_url_ref is not None:
            url = _secret_text(
                resolve(CredentialRef("notification", self._webhook_url_ref))
            ).strip()
            source = self._webhook_url_ref
        else:
            url = os.environ.get(self._webhook_url_env, "").strip()
            source = self._webhook_url_env
        if not url or not _is_valid_webhook_url(url):
            # Name the variable or reference only: its value is the credential.
            log.error(
                "slack channel misconfigured: channel=%s env_var=%s must hold an https URL",
                self.name,
                source,
            )
            return DeliveryResult(DeliveryStatus.RETRY, reason="misconfigured")

        try:
            request = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with _build_opener().open(request, timeout=self._timeout) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            return self._http_status(exc.code, exc.headers, exc)
        except Exception as exc:  # noqa: BLE001 - timeout/DNS/connection/redirect: retry later
            return self._failed(exc, DeliveryResult(DeliveryStatus.RETRY))
        return self._http_status(status, None, None)

    def _http_status(self, status: int, headers: Any, exc: Exception | None) -> DeliveryResult:
        if 200 <= status < 300:
            log.info("slack channel sent: channel=%s", self.name)
            return DeliveryResult(DeliveryStatus.DELIVERED)
        if exc is None:
            exc = RuntimeError()
        if status == 429:
            return self._failed(
                exc, DeliveryResult(DeliveryStatus.RETRY, retry_after_seconds=_retry_after(headers))
            )
        if 400 <= status < 500:
            return self._failed(exc, DeliveryResult(DeliveryStatus.REJECTED, reason="http_4xx"))
        return self._failed(exc, DeliveryResult(DeliveryStatus.RETRY))

    def _failed(self, exc: Exception, result: DeliveryResult) -> DeliveryResult:
        log.warning(
            "slack channel send failed: channel=%s error_type=%s",
            self.name,
            type(exc).__name__,
        )
        return result


register_channel_type(SlackChannelType)
