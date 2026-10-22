"""Pure projections for the guided setup's notification answers.

Two public functions (see ``__all__``):

``build_outbox_settings``
    Turns the outbox gate and event selection into the two
    ``PIPELINE_NOTIFY_OUTBOX_*`` env changes. The gate is written as the
    exact string ``"1"`` or ``"0"`` because the sink only enables itself on
    ``"1"``; ``"true"``/``"false"`` would silently disable it.

``build_email_settings``
    Turns the SMTP answers into the eight ``PIPELINE_NOTIFY_EMAIL_*`` env
    changes. A disabled channel tolerates missing host/sender/recipients,
    but every value that *is* supplied is still validated, so a later
    enable cannot inherit a broken port, timeout or address. An enabled
    channel collects every problem and raises one aggregated error instead
    of stopping at the first.

Both functions are pure projections: they return a dict of env changes and
perform no I/O. They read no ``os.environ``, touch no filesystem and open no
network connection. Values are validated through
:func:`pipeline.setup.schema.validate_value`, so the prompt rules live in
exactly one place. Error messages name the offending key (and, for an
invalid event token, the token) but never a secret value: the SMTP password
is returned verbatim and is never validated or echoed.
"""

from __future__ import annotations

from collections.abc import Iterable

from pipeline.notification_outbox import DEFAULT_OUTBOX_EVENTS
from pipeline.setup.schema import validate_value

__all__ = ["build_email_settings", "build_outbox_settings"]

_OUTBOX_ENABLED = "PIPELINE_NOTIFY_OUTBOX_ENABLED"
_OUTBOX_EVENTS = "PIPELINE_NOTIFY_OUTBOX_EVENTS"

_EMAIL_ENABLED = "PIPELINE_NOTIFY_EMAIL_ENABLED"
_EMAIL_HOST = "PIPELINE_NOTIFY_EMAIL_HOST"
_EMAIL_PORT = "PIPELINE_NOTIFY_EMAIL_PORT"
_EMAIL_USER = "PIPELINE_NOTIFY_EMAIL_USER"
_EMAIL_PASSWORD = "PIPELINE_NOTIFY_EMAIL_PASSWORD"
_EMAIL_FROM = "PIPELINE_NOTIFY_EMAIL_FROM"
_EMAIL_TO = "PIPELINE_NOTIFY_EMAIL_TO"
_EMAIL_TIMEOUT = "PIPELINE_NOTIFY_EMAIL_TIMEOUT"


def build_outbox_settings(enabled: bool, events: object = None) -> dict[str, str]:
    """Return the outbox env changes for ``enabled`` and ``events``.

    ``events`` may be a comma-separated string, an iterable of strings, or
    ``None``. ``None`` or an empty selection falls back to
    :data:`pipeline.notification_outbox.DEFAULT_OUTBOX_EVENTS`. Each token is
    normalized through the schema's csv rule; an invalid token raises
    ``ValueError`` naming that token only.
    """
    return {
        _OUTBOX_ENABLED: _gate(enabled),
        _OUTBOX_EVENTS: _normalize_events(events),
    }


def build_email_settings(
    *,
    enabled: bool,
    host: str = "",
    port: str = "587",
    user: str = "",
    password: str = "",
    sender: str = "",
    recipients: object = "",
    timeout: str = "20",
) -> dict[str, str]:
    """Return the eight email env changes for the supplied answers.

    When ``enabled`` is false, a missing host, sender or recipient list is
    accepted because the channel is inert, but any supplied value must still
    pass its schema validator. When ``enabled`` is true, every problem is
    collected and raised as one ``ValueError``. The password is returned
    verbatim and never appears in an error message.
    """
    problems: list[str] = []
    values: dict[str, str] = {_EMAIL_ENABLED: _gate(enabled)}

    def record(key: str, raw: object, *, required: bool = False) -> None:
        text = _text(raw).strip()
        if not text:
            if required:
                problems.append(f"{key} is required when email delivery is enabled")
            values[key] = ""
            return
        ok, normalized = validate_value(key, text)
        if ok:
            values[key] = normalized
        else:
            problems.append(normalized)

    record(_EMAIL_HOST, host, required=enabled)
    record(_EMAIL_PORT, port)
    record(_EMAIL_USER, user)
    values[_EMAIL_PASSWORD] = _text(password)
    record(_EMAIL_FROM, sender, required=enabled)
    values[_EMAIL_TO] = _record_recipients(recipients, required=enabled, problems=problems)
    record(_EMAIL_TIMEOUT, timeout)

    if problems:
        raise ValueError("; ".join(problems))
    return values


def _gate(enabled: bool) -> str:
    """Return the only two gate spellings the pipeline understands."""
    return "1" if enabled else "0"


def _text(raw: object) -> str:
    return raw if isinstance(raw, str) else str(raw)


def _normalize_events(events: object) -> str:
    """Normalize an event selection to a comma-separated string."""
    if events is None:
        return DEFAULT_OUTBOX_EVENTS
    if isinstance(events, str):
        raw = events
    elif isinstance(events, Iterable):
        raw = ",".join(_text(token) for token in events)
    else:
        raw = _text(events)

    tokens = [token.strip() for token in raw.split(",")]
    tokens = [token for token in tokens if token]
    if not tokens:
        return DEFAULT_OUTBOX_EVENTS

    normalized: list[str] = []
    for token in tokens:
        ok, value = validate_value(_OUTBOX_EVENTS, token)
        if not ok:
            raise ValueError(f"{_OUTBOX_EVENTS} entry {token!r} is not a valid event name")
        normalized.append(value)
    return ",".join(normalized)


def _normalize_recipients(recipients: object) -> list[str]:
    """Split one address or a comma-separated list into individual addresses."""
    if isinstance(recipients, str):
        raw = recipients
    elif isinstance(recipients, Iterable):
        raw = ",".join(_text(token) for token in recipients)
    else:
        raw = _text(recipients)

    tokens = [token.strip() for token in raw.split(",")]
    return [token for token in tokens if token]


def _record_recipients(
    recipients: object, *, required: bool, problems: list[str]
) -> str:
    """Validate each recipient address and return the normalized csv."""
    addresses = _normalize_recipients(recipients)
    if not addresses:
        if required:
            problems.append(f"{_EMAIL_TO} is required when email delivery is enabled")
        return ""

    normalized: list[str] = []
    for address in addresses:
        ok, value = validate_value(_EMAIL_TO, address)
        if ok:
            normalized.append(value)
        else:
            problems.append(value)
    return ",".join(normalized)
