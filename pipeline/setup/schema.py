"""Declarative schema for every key the guided setup offers.

One ``KeySpec`` per configuration key: which section it belongs to, what a
fresh install should write, how a raw string typed at a prompt is validated
and normalized, and whether the value must never be echoed back.

The module is pure. It reads no environment, touches no filesystem and opens
no network connection at import time, so it is safe to import from anywhere
(including a test that asserts exactly that).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

__all__ = [
    "SCHEMA",
    "SECTIONS",
    "KeySpec",
    "default_for",
    "keys_for_section",
    "validate_value",
]

SECTIONS = ("core", "routing", "policy", "notifications", "integrations")

# Backends the pipeline can dispatch to. ``auto`` is only meaningful for the
# per-role routing keys; escalation must name a concrete backend.
_BACKENDS = ("claude", "local", "ollama", "lmstudio", "mlx", "litellm")
_ROUTING_CHOICES = _BACKENDS + ("auto",)
_AUTONOMY_CHOICES = ("dry-run", "gated", "full")
_RISK_CHOICES = ("low", "medium", "high")
_TICKET_CHOICES = ("auto", "none", "plane")

# Mirrors pipeline.notification_outbox.DEFAULT_OUTBOX_EVENTS. Kept as a literal
# so this module imports nothing outside the stdlib.
_DEFAULT_OUTBOX_EVENTS = (
    "plan_completed,story_parked,dispatch_failed,tests_failed,agent_gave_up"
)

_BOOL_TRUE = frozenset({"1", "true", "yes", "on"})
_BOOL_FALSE = frozenset({"0", "false", "no", "off"})

_CSV_TOKEN = re.compile(r"[a-z][a-z0-9_]*")
_DIGITS = re.compile(r"[0-9]+")

# Inclusive integer bounds per key. A missing upper bound means "no maximum".
_INT_BOUNDS: dict[str, tuple[int, int | None]] = {
    "PIPELINE_MAX_CONCURRENT_AGENTS": (1, None),
    "PIPELINE_DAILY_REQUEST_THRESHOLD": (1, None),
    "PIPELINE_WEEKLY_REQUEST_THRESHOLD": (1, None),
    "PIPELINE_PAUSE_THRESHOLD": (0, 100),
    "PIPELINE_RESUME_THRESHOLD": (0, 100),
    "PIPELINE_SECURITY_AUDIT_EVERY_DAYS": (0, None),
    "PIPELINE_SECURITY_AUDIT_EVERY_COMMITS": (0, None),
    "PIPELINE_NOTIFY_EMAIL_PORT": (1, 65535),
    "PIPELINE_NOTIFY_EMAIL_TIMEOUT": (1, None),
}

# Enum keys that also accept the empty string, meaning "leave unset".
_EMPTY_ALLOWED = frozenset({"PIPELINE_ESCALATION_BACKEND"})

# Free-form keys that must still carry an http(s) scheme when set.
_URL_KEYS = frozenset({"PLANE_BASE"})


@dataclass(frozen=True)
class KeySpec:
    """One configuration key offered by the guided setup."""

    name: str
    section: str
    default: str | None
    kind: str
    choices: tuple[str, ...] = ()
    secret: bool = False
    prompt: str = ""


SCHEMA: tuple[KeySpec, ...] = (
    # -- core ---------------------------------------------------------------
    KeySpec(
        name="REPO_ROOT",
        section="core",
        default=None,
        kind="path",
        prompt="Absolute path to the repository the pipeline manages",
    ),
    KeySpec(
        name="PLAN_DIR",
        section="core",
        default="~/.claude/plans",
        kind="path",
        prompt="Directory holding plan files",
    ),
    KeySpec(
        name="WORKTREE_ROOT",
        section="core",
        default="~/.claude/worktrees",
        kind="path",
        prompt="Directory holding per-story git worktrees",
    ),
    KeySpec(
        name="AGENTS_DIR",
        section="core",
        default="~/.claude/agents",
        kind="path",
        prompt="Directory holding agent persona files",
    ),
    KeySpec(
        name="PIPELINE_MODEL_REGISTRY_PATH",
        section="core",
        default=None,
        kind="path",
        prompt="Optional path to a model registry JSON file",
    ),
    # -- routing ------------------------------------------------------------
    KeySpec(
        name="PIPELINE_BACKEND_OVERLORD",
        section="routing",
        default=None,
        kind="enum",
        choices=_ROUTING_CHOICES,
        prompt="Backend for the overlord role (blank keeps the code default)",
    ),
    KeySpec(
        name="PIPELINE_BACKEND_PLANNER",
        section="routing",
        default=None,
        kind="enum",
        choices=_ROUTING_CHOICES,
        prompt="Backend for the planner role (blank keeps the code default)",
    ),
    KeySpec(
        name="PIPELINE_BACKEND_DISPATCH",
        section="routing",
        default=None,
        kind="enum",
        choices=_ROUTING_CHOICES,
        prompt="Backend for the dispatch role (blank keeps the code default)",
    ),
    KeySpec(
        name="PIPELINE_BACKEND_REVIEW",
        section="routing",
        default=None,
        kind="enum",
        choices=_ROUTING_CHOICES,
        prompt="Backend for the review role (blank keeps the code default)",
    ),
    KeySpec(
        name="PIPELINE_BACKEND_DECOMPOSE",
        section="routing",
        default=None,
        kind="enum",
        choices=_ROUTING_CHOICES,
        prompt="Backend for the decompose role (blank keeps the code default)",
    ),
    KeySpec(
        name="PIPELINE_BACKEND_TEST_AUTHOR",
        section="routing",
        default=None,
        kind="enum",
        choices=_ROUTING_CHOICES,
        prompt="Backend for the test-author role (blank keeps the code default)",
    ),
    KeySpec(
        name="PIPELINE_BACKEND_DIAGNOSIS",
        section="routing",
        default=None,
        kind="enum",
        choices=_ROUTING_CHOICES,
        prompt="Backend for the diagnosis role (blank keeps the code default)",
    ),
    KeySpec(
        name="PIPELINE_BACKEND_SECURITY",
        section="routing",
        default=None,
        kind="enum",
        choices=_ROUTING_CHOICES,
        prompt="Backend for the security role (blank keeps the code default)",
    ),
    KeySpec(
        name="PIPELINE_BACKEND_CHAT",
        section="routing",
        default=None,
        kind="enum",
        choices=_ROUTING_CHOICES,
        prompt="Backend for the chat role (blank keeps the code default)",
    ),
    # -- policy -------------------------------------------------------------
    KeySpec(
        name="PIPELINE_AUTONOMY",
        section="policy",
        default="dry-run",
        kind="enum",
        choices=_AUTONOMY_CHOICES,
        prompt="Autonomy level: dry-run plans only, gated acts up to the risk "
        "threshold, full acts on everything",
    ),
    KeySpec(
        name="PIPELINE_RISK_THRESHOLD",
        section="policy",
        default="low",
        kind="enum",
        choices=_RISK_CHOICES,
        prompt="Highest story risk the overlord may act on unattended",
    ),
    KeySpec(
        name="PIPELINE_MAX_CONCURRENT_AGENTS",
        section="policy",
        default="3",
        kind="int",
        prompt="Maximum number of agents running at once",
    ),
    KeySpec(
        name="PIPELINE_DAILY_REQUEST_THRESHOLD",
        section="policy",
        default="3000",
        kind="int",
        prompt="Daily request budget before the usage gate pauses work",
    ),
    KeySpec(
        name="PIPELINE_WEEKLY_REQUEST_THRESHOLD",
        section="policy",
        default="15000",
        kind="int",
        prompt="Weekly request budget before the usage gate pauses work",
    ),
    KeySpec(
        name="PIPELINE_PAUSE_THRESHOLD",
        section="policy",
        default="90",
        kind="int",
        prompt="Usage percentage that pauses new work",
    ),
    KeySpec(
        name="PIPELINE_RESUME_THRESHOLD",
        section="policy",
        default="70",
        kind="int",
        prompt="Usage percentage that resumes paused work",
    ),
    KeySpec(
        name="PIPELINE_ESCALATION_BACKEND",
        section="policy",
        default=None,
        kind="enum",
        choices=_BACKENDS,
        prompt="Backend for escalations (blank derives it from the dispatch role)",
    ),
    KeySpec(
        name="PIPELINE_ESCALATION_MODEL",
        section="policy",
        default=None,
        kind="str",
        prompt="Model for escalations (blank derives it from the dispatch role)",
    ),
    KeySpec(
        name="PIPELINE_SECURITY_AUDIT_EVERY_DAYS",
        section="policy",
        default="0",
        kind="int",
        prompt="Run a security audit every N days (0 disables)",
    ),
    KeySpec(
        name="PIPELINE_SECURITY_AUDIT_EVERY_COMMITS",
        section="policy",
        default="0",
        kind="int",
        prompt="Run a security audit every N commits (0 disables)",
    ),
    KeySpec(
        name="PIPELINE_AUTO_MERGE",
        section="policy",
        default="1",
        kind="bool",
        prompt="Merge reviewed pull requests automatically (0 parks every PR "
        "for a human merge)",
    ),
    # -- notifications ------------------------------------------------------
    KeySpec(
        name="PIPELINE_NOTIFY_OUTBOX_ENABLED",
        section="notifications",
        default="0",
        kind="bool",
        prompt="Write pipeline events to the notification outbox",
    ),
    KeySpec(
        name="PIPELINE_NOTIFY_OUTBOX_EVENTS",
        section="notifications",
        default=_DEFAULT_OUTBOX_EVENTS,
        kind="csv",
        prompt="Comma-separated event names to record in the outbox",
    ),
    KeySpec(
        name="PIPELINE_NOTIFY_EMAIL_ENABLED",
        section="notifications",
        default="0",
        kind="bool",
        prompt="Send notification emails",
    ),
    KeySpec(
        name="PIPELINE_NOTIFY_EMAIL_HOST",
        section="notifications",
        default="",
        kind="str",
        prompt="SMTP host (blank disables email delivery)",
    ),
    KeySpec(
        name="PIPELINE_NOTIFY_EMAIL_PORT",
        section="notifications",
        default="587",
        kind="int",
        prompt="SMTP port",
    ),
    KeySpec(
        name="PIPELINE_NOTIFY_EMAIL_USER",
        section="notifications",
        default="",
        kind="str",
        prompt="SMTP username",
    ),
    KeySpec(
        name="PIPELINE_NOTIFY_EMAIL_PASSWORD",
        section="notifications",
        default="",
        kind="str",
        secret=True,
        prompt="SMTP password (never echoed back)",
    ),
    KeySpec(
        name="PIPELINE_NOTIFY_EMAIL_FROM",
        section="notifications",
        default="",
        kind="email",
        prompt="From address for notification emails",
    ),
    KeySpec(
        name="PIPELINE_NOTIFY_EMAIL_TO",
        section="notifications",
        default="",
        kind="email",
        prompt="Recipient address for notification emails",
    ),
    KeySpec(
        name="PIPELINE_NOTIFY_EMAIL_TIMEOUT",
        section="notifications",
        default="20",
        kind="int",
        prompt="SMTP timeout in seconds",
    ),
    # -- integrations -------------------------------------------------------
    KeySpec(
        name="PLANE_BASE",
        section="integrations",
        default="",
        kind="str",
        prompt="Plane base URL, http:// or https:// (blank disables Plane)",
    ),
    KeySpec(
        name="PLANE_API_KEY",
        section="integrations",
        default="",
        kind="str",
        secret=True,
        prompt="Plane API key (never echoed back)",
    ),
    KeySpec(
        name="PLANE_WORKSPACE",
        section="integrations",
        default="",
        kind="str",
        prompt="Plane workspace slug",
    ),
    KeySpec(
        name="PLANE_PROJECT",
        section="integrations",
        default="",
        kind="str",
        prompt="Plane project identifier",
    ),
    KeySpec(
        name="PIPELINE_TICKET_PROVIDER",
        section="integrations",
        default="none",
        kind="enum",
        choices=_TICKET_CHOICES,
        prompt="Ticket provider: auto, none or plane",
    ),
)

_BY_NAME: dict[str, KeySpec] = {spec.name: spec for spec in SCHEMA}


def keys_for_section(section: str) -> tuple[KeySpec, ...]:
    """Return the specs in ``section``, in ``SCHEMA`` order.

    Raises ``ValueError`` naming the valid sections when ``section`` is not one
    of them.
    """
    if section not in SECTIONS:
        raise ValueError(
            f"unknown setup section {section!r}; valid sections are "
            + ", ".join(SECTIONS)
        )
    return tuple(spec for spec in SCHEMA if spec.section == section)


def default_for(key: str) -> str | None:
    """Return the raw fresh-install default for ``key``.

    ``None`` means "leave the key unset" so the code's own default applies.
    Raises ``ValueError`` for a key the schema does not know.
    """
    return _spec(key).default


def validate_value(key: str, raw: str) -> tuple[bool, str]:
    """Validate and normalize ``raw`` for ``key``.

    Returns ``(True, normalized)`` or ``(False, actionable_message)``. Never
    raises, whatever the input: an unknown key, a non-string, an empty value or
    a multi-megabyte one all come back as a rejection with a message. Messages
    never contain the submitted value, so a secret is never echoed back.
    """
    spec = _BY_NAME.get(key) if isinstance(key, str) else None
    if spec is None:
        return False, f"unknown setup key {key!r}"
    try:
        return _validate(spec, raw)
    except Exception:  # noqa: BLE001 - validate_value must never raise
        return False, f"{spec.name} could not be validated; check the value and try again"


def _spec(key: str) -> KeySpec:
    try:
        return _BY_NAME[key]
    except KeyError:
        raise ValueError(f"unknown setup key {key!r}") from None


def _validate(spec: KeySpec, raw: str) -> tuple[bool, str]:
    if not isinstance(raw, str):
        return False, f"{spec.name} must be entered as text"
    text = raw.strip()
    kind = spec.kind
    if kind == "bool":
        return _validate_bool(spec, text)
    if kind == "int":
        return _validate_int(spec, text)
    if kind == "enum":
        return _validate_enum(spec, text)
    if kind == "path":
        return _validate_path(spec, text)
    if kind == "csv":
        return _validate_csv(spec, text)
    if kind == "email":
        return _validate_email(spec, text)
    return _validate_str(spec, text)


def _validate_bool(spec: KeySpec, text: str) -> tuple[bool, str]:
    lowered = text.lower()
    if lowered in _BOOL_TRUE:
        return True, "1"
    if lowered in _BOOL_FALSE:
        return True, "0"
    return False, (
        f"{spec.name} must be one of 1, 0, true, false, yes, no, on, off"
    )


def _validate_int(spec: KeySpec, text: str) -> tuple[bool, str]:
    if not _DIGITS.fullmatch(text):
        return False, f"{spec.name} must be a whole number"
    try:
        value = int(text)
    except ValueError:
        # Python refuses to convert absurdly long digit strings; treat that as
        # out of range rather than letting it escape.
        return False, f"{spec.name} is out of range"
    low, high = _INT_BOUNDS.get(spec.name, (0, None))
    if value < low:
        return False, f"{spec.name} must be at least {low}"
    if high is not None and value > high:
        return False, f"{spec.name} must be at most {high}"
    return True, str(value)


def _validate_enum(spec: KeySpec, text: str) -> tuple[bool, str]:
    lowered = text.lower()
    if not lowered and spec.name in _EMPTY_ALLOWED:
        return True, ""
    if lowered in spec.choices:
        return True, lowered
    return False, f"{spec.name} must be one of " + ", ".join(spec.choices)


def _validate_path(spec: KeySpec, text: str) -> tuple[bool, str]:
    if not text:
        return False, f"{spec.name} must be a path"
    expanded = os.path.expanduser(text)
    if not os.path.isabs(expanded):
        return False, f"{spec.name} must be an absolute path (or start with ~)"
    return True, expanded


def _validate_csv(spec: KeySpec, text: str) -> tuple[bool, str]:
    tokens = [token.strip() for token in text.split(",")]
    tokens = [token for token in tokens if token]
    if not tokens:
        return False, f"{spec.name} must list at least one name"
    for token in tokens:
        if not _CSV_TOKEN.fullmatch(token):
            return False, (
                f"{spec.name} entries must be lowercase names like "
                "plan_completed, separated by commas"
            )
    return True, ",".join(tokens)


def _validate_email(spec: KeySpec, text: str) -> tuple[bool, str]:
    if not text:
        return True, ""
    if any(char.isspace() for char in text):
        return False, f"{spec.name} must not contain spaces"
    if text.count("@") != 1:
        return False, f"{spec.name} must contain exactly one @"
    local, _, domain = text.partition("@")
    if not local:
        return False, f"{spec.name} must have something before the @"
    if "." not in domain:
        return False, f"{spec.name} must have a domain with a dot, like example.com"
    return True, text


def _validate_str(spec: KeySpec, text: str) -> tuple[bool, str]:
    if (
        spec.name in _URL_KEYS
        and text
        and not text.lower().startswith(("http://", "https://"))
    ):
        return False, f"{spec.name} must start with http:// or https://"
    return True, text
