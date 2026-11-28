"""SMTP sender for spooled plan-completion notifications (PLANNOTIFY-05).

``send_notification_email`` sends ONE spooled outbox record as an email over
SMTP.  It is the first code in this repo that ships content off the machine and
it handles a credential, so it is built fail-closed:

* Disabled by default (``PIPELINE_NOTIFY_EMAIL_ENABLED`` must be exactly ``1``);
  no socket is ever opened while disabled.
* Partial configuration (missing host, recipient, or sender) is an ERROR and a
  ``False`` return -- never a send attempt.
* The configured password never reaches a log record, an exception message that
  escapes, or a return value.  Failures are logged as the exception TYPE only.
* Message payloads (body, subject, recipients) are never logged at INFO or
  below; only the outcome is.
* The function never raises: any failure returns ``False``.  This holds
  for a malformed record too -- the outbox drain re-reads records from
  a JSONL file, so an unrenderable record fails closed like any other.
* The SMTP host must resolve to a public address, judged by the same guard as
  the webhook channel, and the connection is pinned to that address.  An
  internal host is accepted only when the operator names it in
  ``PIPELINE_NOTIFY_SMTP_ALLOW_PRIVATE_HOSTS``.  A refusal opens no socket.

The routable channel type reads its configuration from ``notification.json``
settings, not from ``os.environ``; a password comes either from a
``password_ref`` (a provider reference, resolved through the credential seam at
send time) or from a legacy ``password_env`` (the NAME of an environment
variable, read at CALL time).  The legacy ``send_notification_email`` entry
point reads all of its configuration from ``os.environ`` at CALL time (never at
import time) so callers and tests can change the environment between calls.
Only the standard library is used, plus the in-repo credential seam and the
outbound guard (its host judgement is shared with the webhook channel).
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import smtplib
import ssl
from collections.abc import Mapping
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any

from app.credentials import CredentialRef, resolve
from app.outbound_guard import is_internal_address, resolve_public_address
from pipeline.notification_channels import (
    ChannelConfigError,
    DeliveryResult,
    DeliveryStatus,
    register_channel_type,
    reject_inline_secrets,
    require_credential_ref,
)
from pipeline.notification_render import (  # noqa: F401 - re-exported names
    STORY_LEVEL_EVENTS,
    SUBJECT_LABELS,
    RenderedNotification,
    render_notification,
)

log = logging.getLogger(__name__)

# Defaults are documented here for the provenance catalog only; every knob is
# re-read from os.environ on each call so runtime changes take effect.
_DEFAULTS = {
    "PIPELINE_NOTIFY_EMAIL_ENABLED": "0",
    "PIPELINE_NOTIFY_EMAIL_HOST": "",
    "PIPELINE_NOTIFY_EMAIL_PORT": "587",
    "PIPELINE_NOTIFY_EMAIL_USER": "",
    "PIPELINE_NOTIFY_EMAIL_PASSWORD": "",
    "PIPELINE_NOTIFY_EMAIL_FROM": "",
    "PIPELINE_NOTIFY_EMAIL_TO": "",
    "PIPELINE_NOTIFY_EMAIL_TIMEOUT": "20",
}


@dataclass(frozen=True)
class EmailSettings:
    host: str
    port: int
    user: str
    password: str = field(repr=False)
    from_addr: str
    to_addrs: tuple[str, ...]
    timeout: float


def send_notification_email(record: dict) -> bool:
    """Send one spooled outbox ``record`` as an email over SMTP.

    Returns ``True`` on a successful send and ``False`` on any failure.
    Never raises.
    """
    # Enabled gate first: unset, "0", or anything other than exactly "1"
    # means no-op, with zero construction and zero network activity.
    if os.environ.get("PIPELINE_NOTIFY_EMAIL_ENABLED", "0") != "1":
        return False

    host = os.environ.get("PIPELINE_NOTIFY_EMAIL_HOST", "")
    to_addr = os.environ.get("PIPELINE_NOTIFY_EMAIL_TO", "")
    user = os.environ.get("PIPELINE_NOTIFY_EMAIL_USER", "")
    password = os.environ.get("PIPELINE_NOTIFY_EMAIL_PASSWORD", "")
    from_addr = os.environ.get("PIPELINE_NOTIFY_EMAIL_FROM", "") or user

    # Guarded numeric parsing: a malformed knob fails closed instead of
    # letting smtplib/socket raise from a bad value.
    try:
        port = int(os.environ.get("PIPELINE_NOTIFY_EMAIL_PORT", "587"))
        timeout = float(os.environ.get("PIPELINE_NOTIFY_EMAIL_TIMEOUT", "20"))
    except (TypeError, ValueError):
        log.error(
            "notification email not sent: PIPELINE_NOTIFY_EMAIL_PORT and "
            "PIPELINE_NOTIFY_EMAIL_TIMEOUT must be numeric"
        )
        return False

    # Fail closed on partial configuration -- never attempt a send with a
    # partial config.  Log the missing variable NAMES, never their values.
    missing = [
        name
        for name, value in (
            ("PIPELINE_NOTIFY_EMAIL_HOST", host),
            ("PIPELINE_NOTIFY_EMAIL_TO", to_addr),
            ("PIPELINE_NOTIFY_EMAIL_FROM/USER", from_addr),
        )
        if not value
    ]
    if missing:
        log.error(
            "notification email not sent: missing required configuration: %s",
            ", ".join(missing),
        )
        return False

    settings = EmailSettings(
        host=host,
        port=port,
        user=user,
        password=password,
        from_addr=from_addr,
        to_addrs=(to_addr,),
        timeout=timeout,
    )

    # Rendering the record is inside a try because the record is untrusted
    # input: it is re-read from the outbox JSONL file by the drain, so a
    # truncated or hand-edited line can carry a non-string body.  Failing
    # closed here (before any connection is opened) keeps the documented
    # "never raises" contract true for the drain loop that calls this per
    # record.
    try:
        rendered = render_notification(record)
    except Exception as exc:  # noqa: BLE001 - never raise out of a sender
        _log_malformed(exc)
        return False

    return _send_smtp(settings, rendered)


def _log_malformed(exc: Exception) -> None:
    # Exception TYPE only: the record's own content is a payload and the
    # environment holds the credential, so neither may reach the log.
    log.error(
        "notification email not sent: malformed record (%s)",
        type(exc).__name__,
    )


def _build_message(
    settings: EmailSettings, rendered: RenderedNotification
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = settings.from_addr
    message["To"] = ", ".join(settings.to_addrs)
    message["Subject"] = rendered.subject
    body = f"{rendered.body}\nOpen: {rendered.link}" if rendered.link else rendered.body
    message.set_content(body)
    return message


#: The operator's opt-in: comma-separated exact hostnames (or IP literals) that
#: may resolve to an internal address.  Read from the process environment only.
ALLOW_PRIVATE_HOSTS_ENV = "PIPELINE_NOTIFY_SMTP_ALLOW_PRIVATE_HOSTS"

#: The only refusal reason a caller can see or log for a host.
HOST_REFUSED_REASON = "smtp_host_not_public"

_HOSTNAME_PATTERN = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
)


def _normalise_host(value: str) -> str:
    return value.strip().casefold().rstrip(".")


def _is_valid_host(host: str) -> bool:
    """True for a hostname or an IP literal; anything else is malformed."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return len(host) <= 253 and _HOSTNAME_PATTERN.fullmatch(host) is not None
    return True


def _allowed_private_hosts() -> frozenset[str]:
    """The hosts the operator allows to resolve internally.

    Unset or empty means no host may be internal.  A blank or malformed entry
    is dropped, with one WARNING that names the variable and never its value.
    """
    raw = os.environ.get(ALLOW_PRIVATE_HOSTS_ENV, "")
    if not raw:
        return frozenset()
    hosts = set()
    dropped = False
    for item in raw.split(","):
        host = _normalise_host(item)
        if _is_valid_host(host):
            hosts.add(host)
        else:
            dropped = True
    if dropped:
        log.warning(
            "ignoring blank or malformed entries in %s", ALLOW_PRIVATE_HOSTS_ENV
        )
    return frozenset(hosts)


def _resolve_smtp_host(name: str, *, allow_private: bool) -> str | None:
    """The address to dial for a normalised, syntactically valid ``name``.

    A literal IP is judged directly, with no lookup.  ``localhost`` and
    ``*.localhost`` are internal without one.  Any other name is resolved and
    every answer judged by the webhook channel's guard.
    """
    try:
        literal = ipaddress.ip_address(name)
    except ValueError:
        if not allow_private and (name == "localhost" or name.endswith(".localhost")):
            return None
        return resolve_public_address(f"smtp://{name}", allow_private=allow_private)
    if not allow_private and is_internal_address(literal):
        return None
    return str(literal)


def _pinned_smtp_address(channel: str, host: str) -> str | None:
    """The validated address to dial for ``host``, or None to refuse the send.

    A malformed host is refused before any lookup.  Refusals log the channel
    name and a constant reason only: the host and the address are never logged.
    """
    name = _normalise_host(host)
    address = None
    if _is_valid_host(name):
        allow_private = name in _allowed_private_hosts()
        address = _resolve_smtp_host(name, allow_private=allow_private)
    if address is None:
        log.warning(
            "notification email refused: channel=%s reason=%s",
            channel,
            HOST_REFUSED_REASON,
        )
    return address


class _ServerNameContext(ssl.SSLContext):
    """A verifying client context that checks the certificate against one name.

    ``smtplib`` hands ``wrap_socket`` the name it connected to.  The connection
    is pinned to a validated IP, so that name is the IP; the certificate must
    be checked against the configured host instead.
    """

    server_name: str = ""

    def wrap_socket(self, sock, *args, server_hostname=None, **kwargs):
        return super().wrap_socket(
            sock, *args, server_hostname=self.server_name, **kwargs
        )


def _verifying_context(server_name: str) -> ssl.SSLContext:
    context = _ServerNameContext(ssl.PROTOCOL_TLS_CLIENT)
    context.server_name = server_name
    context.load_default_certs(ssl.Purpose.SERVER_AUTH)
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


class SMTPAuthPhaseDisconnect(Exception):
    """Server closed the connection while authenticating."""


def _smtp_deliver(settings: EmailSettings, message: EmailMessage, address: str) -> None:
    """Open the connection to the validated ``address`` and send.

    The connection is pinned to ``address``, so the name is not resolved a
    second time.  TLS still verifies the certificate against ``settings.host``.
    Raises whatever smtplib raises.
    """
    with smtplib.SMTP(address, settings.port, timeout=settings.timeout) as smtp:
        # Certificate verification is mandatory: a bare starttls() makes
        # smtplib supply the unverified stdlib context (CERT_NONE, no
        # hostname check), and the password crosses this channel on the
        # very next line.  With a verifying context an active MITM's
        # certificate fails verification and the send fails closed before
        # the credential is transmitted.
        smtp.starttls(context=_verifying_context(settings.host))
        try:
            smtp.login(settings.user, settings.password)
        except smtplib.SMTPServerDisconnected as exc:
            raise SMTPAuthPhaseDisconnect(
                "connection closed during authentication"
            ) from exc
        smtp.send_message(message)


def _send_smtp(settings: EmailSettings, rendered: RenderedNotification) -> bool:
    """Shared SMTP send core.  Returns ``True`` on success; never raises."""
    try:
        message = _build_message(settings, rendered)
    except Exception as exc:  # noqa: BLE001 - never raise out of a sender
        _log_malformed(exc)
        return False

    address = _pinned_smtp_address("legacy", settings.host)
    if address is None:
        return False

    # One try block covers construction through send so the connection is
    # closed on every path (the context manager runs __exit__ even when the
    # constructor, starttls, login, or send_message raises) and no exception
    # object -- whose text may embed the password -- ever escapes.  Only the
    # exception TYPE is logged, never its message or traceback.
    try:
        _smtp_deliver(settings, message, address)
    except Exception as exc:  # noqa: BLE001 - never raise out of a sender
        log.error("notification email send failed: %s", type(exc).__name__)
        return False

    log.info("notification email sent")
    return True


_ADDRESS_FORBIDDEN = ("\r", "\n")
_CHANNEL_KEYS = frozenset(
    {
        "type",
        "host",
        "port",
        "user",
        "from",
        "to",
        "timeout",
        "password_env",
        "password_ref",
    }
)


def _require_str(name: str, settings: Mapping[str, Any], key: str) -> str:
    value = settings.get(key)
    if (
        not isinstance(value, str)
        or not value
        or any(c in value for c in _ADDRESS_FORBIDDEN)
    ):
        raise ChannelConfigError(
            f"channel {name!r}: setting {key!r} must be a non-empty string"
        )
    return value


def _validated_address(name: str, key: str, value: Any) -> str:
    if (
        not isinstance(value, str)
        or any(c in value for c in _ADDRESS_FORBIDDEN)
        or value.count("@") != 1
    ):
        raise ChannelConfigError(
            f"channel {name!r}: setting {key!r} has an invalid address"
        )
    local, domain = value.split("@")
    if not local.strip() or not domain.strip():
        raise ChannelConfigError(
            f"channel {name!r}: setting {key!r} has an invalid address"
        )
    return value


def _positive_number(name: str, key: str, value: Any, kind: type) -> Any:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ChannelConfigError(
            f"channel {name!r}: setting {key!r} must be a positive number"
        )
    return kind(value)


def _secret_text(secret: Any) -> str:
    """The ONE place a notification email password is revealed for injection.

    A dispatched child is prompt-injectable, so the raw value must not travel:
    everything else treats the ``SecretValue`` as opaque. A later egress proxy
    replaces this function to hand out a placeholder instead.
    """
    return secret.reveal() if secret is not None else ""


class EmailChannelType:
    """A routable email channel instance (``type: email`` in notification.json).

    The password is never stored: only a ``password_ref`` (a provider
    reference) or a ``password_env`` (the NAME of the environment variable that
    holds it) is kept, and it is resolved at send time.
    """

    type_name = "email"

    def __init__(
        self,
        name: str,
        *,
        host: str,
        port: int,
        user: str,
        from_addr: str,
        to_addrs: tuple[str, ...],
        timeout: float,
        password_env: str | None,
        password_ref: str | None = None,
    ) -> None:
        self.name = name
        self.outbox_suffix = f".outbox.{name}.jsonl"
        self._host = host
        self._port = port
        self._user = user
        self._from_addr = from_addr
        self._to_addrs = to_addrs
        self._timeout = timeout
        self._password_env = password_env
        self._password_ref = password_ref

    @classmethod
    def from_settings(
        cls, instance_name: str, settings: Mapping[str, Any], env: Mapping[str, str]
    ) -> EmailChannelType:
        reject_inline_secrets(instance_name, settings)
        for key in settings:
            if key not in _CHANNEL_KEYS:
                raise ChannelConfigError(
                    f"channel {instance_name!r}: unknown setting {key!r}"
                )
        host = _require_str(instance_name, settings, "host")
        user = settings.get("user", "")
        if not isinstance(user, str) or any(c in user for c in _ADDRESS_FORBIDDEN):
            raise ChannelConfigError(
                f"channel {instance_name!r}: setting 'user' must be a string"
            )
        from_addr = _validated_address(instance_name, "from", settings.get("from"))
        raw_to = settings.get("to")
        if not isinstance(raw_to, list) or not raw_to:
            raise ChannelConfigError(
                f"channel {instance_name!r}: setting 'to' must be a non-empty list"
            )
        to_addrs = tuple(_validated_address(instance_name, "to", a) for a in raw_to)
        port = _positive_number(instance_name, "port", settings.get("port", 587), int)
        timeout = _positive_number(
            instance_name, "timeout", settings.get("timeout", 20), float
        )
        password_env = settings.get("password_env")
        if password_env is not None and (
            not isinstance(password_env, str) or not password_env
        ):
            raise ChannelConfigError(
                f"channel {instance_name!r}: setting 'password_env' must name an env var"
            )
        password_ref = require_credential_ref(
            instance_name, "password_ref", settings.get("password_ref")
        )
        if password_env is not None and password_ref is not None:
            raise ChannelConfigError(
                f"channel {instance_name!r}: settings password_env and password_ref "
                "are mutually exclusive"
            )
        return cls(
            instance_name,
            host=host,
            port=port,
            user=user,
            from_addr=from_addr,
            to_addrs=to_addrs,
            timeout=timeout,
            password_env=password_env,
            password_ref=password_ref,
        )

    def _password(self) -> str:
        """Read the password at send time: never stored, never logged.

        A ``password_ref`` is resolved through the provider seam; a legacy
        ``password_env`` keeps reading the named variable exactly as before. A
        reference never falls back to an environment variable, and a provider
        error propagates to the caller's broad handler (RETRY).
        """
        if self._password_ref is not None:
            # Imported here: ``pipeline.actor`` pulls in the identity layer, which
            # must not load just to validate a channel.
            from pipeline.actor import current_namespace

            return _secret_text(
                resolve(
                    CredentialRef(
                        "notification",
                        self._password_ref,
                        namespace=current_namespace(),
                    )
                )
            )
        if self._password_env is not None:
            return os.environ.get(self._password_env, "")
        return ""

    def send(self, record: dict) -> DeliveryResult:
        try:
            return self._send(record)
        except Exception as exc:  # noqa: BLE001 - a channel send must never raise
            log.error(
                "email channel send failed: channel=%s error_type=%s",
                self.name,
                type(exc).__name__,
            )
            return DeliveryResult(DeliveryStatus.RETRY)

    def _send(self, record: dict) -> DeliveryResult:
        # Judged before the credential is read, so a refused host never sees it.
        address = _pinned_smtp_address(self.name, self._host)
        if address is None:
            return DeliveryResult(DeliveryStatus.REJECTED, reason=HOST_REFUSED_REASON)
        settings = EmailSettings(
            host=self._host,
            port=self._port,
            user=self._user,
            password=self._password(),
            from_addr=self._from_addr,
            to_addrs=self._to_addrs,
            timeout=self._timeout,
        )
        try:
            message = _build_message(settings, render_notification(record))
        except Exception as exc:  # noqa: BLE001 - untrusted spooled record
            log.error(
                "email channel rejected record: channel=%s error_type=%s",
                self.name,
                type(exc).__name__,
            )
            return DeliveryResult(DeliveryStatus.REJECTED, reason="malformed_record")
        try:
            _smtp_deliver(settings, message, address)
        except SMTPAuthPhaseDisconnect as exc:
            return self._failed(
                exc, DeliveryResult(DeliveryStatus.RETRY, reason="smtp_auth_disconnect")
            )
        except smtplib.SMTPAuthenticationError as exc:
            # 4xx means "try again later" (e.g. Gmail's `454 4.7.0 Too many login
            # attempts`); 5xx rejects the credential outright.
            if 400 <= exc.smtp_code < 500:
                return self._failed(
                    exc,
                    DeliveryResult(DeliveryStatus.RETRY, reason="smtp_auth_temporary"),
                )
            return self._failed(
                exc, DeliveryResult(DeliveryStatus.REJECTED, reason="smtp_rejected")
            )
        except (
            smtplib.SMTPRecipientsRefused,
            smtplib.SMTPSenderRefused,
        ) as exc:
            return self._failed(
                exc, DeliveryResult(DeliveryStatus.REJECTED, reason="smtp_rejected")
            )
        except smtplib.SMTPResponseException as exc:
            permanent = 500 <= exc.smtp_code < 600
            return self._failed(
                exc,
                DeliveryResult(DeliveryStatus.REJECTED, reason="smtp_rejected")
                if permanent
                else DeliveryResult(DeliveryStatus.RETRY),
            )
        except Exception as exc:  # noqa: BLE001 - connection/timeout/TLS: retry later; _failed logs the class name
            return self._failed(exc, DeliveryResult(DeliveryStatus.RETRY))
        log.info("email channel sent: channel=%s", self.name)
        return DeliveryResult(DeliveryStatus.DELIVERED)

    def _failed(self, exc: Exception, result: DeliveryResult) -> DeliveryResult:
        log.warning(
            "email channel send failed: channel=%s error_type=%s",
            self.name,
            type(exc).__name__,
        )
        return result


register_channel_type(EmailChannelType)
