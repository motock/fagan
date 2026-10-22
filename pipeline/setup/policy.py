"""Runtime-policy projection for the guided setup.

The guided setup collects raw answers for the policy section and hands them to
:func:`build_policy_settings`, which returns exactly the key/value pairs that
may be written to ``.pipeline.env``. Everything the policy section can get wrong
*as a group* is caught here, before a single byte reaches the env writer:

* hysteresis — ``PIPELINE_PAUSE_THRESHOLD`` must sit strictly above
  ``PIPELINE_RESUME_THRESHOLD``, otherwise the usage gate would flap;
* escalation pairing — a model without a backend is meaningless, while a
  backend without a model is fine because the backend resolves its own default;
* scope — only the keys this story owns may be emitted, so a transport-only or
  local-agent key can never leak into the policy section of the env file.

:func:`describe_autonomy` is the single home of the autonomy prose, including
the dry-run-to-gated instruction, so the wizard summary and the docs cannot
drift apart.

The module is pure: it reads no environment, touches no filesystem and opens no
network connection, at import time or otherwise.
"""

from __future__ import annotations

from collections.abc import Mapping

from . import schema

__all__ = ["build_policy_settings", "describe_autonomy"]

# The policy keys this story owns, in the order they are written to the env
# file. ``PIPELINE_AUTO_MERGE`` also lives in the schema's policy section but is
# owned by another story, so it is deliberately absent here.
_ALLOWED_KEYS: tuple[str, ...] = (
    "PIPELINE_AUTONOMY",
    "PIPELINE_RISK_THRESHOLD",
    "PIPELINE_MAX_CONCURRENT_AGENTS",
    "PIPELINE_DAILY_REQUEST_THRESHOLD",
    "PIPELINE_WEEKLY_REQUEST_THRESHOLD",
    "PIPELINE_PAUSE_THRESHOLD",
    "PIPELINE_RESUME_THRESHOLD",
    "PIPELINE_ESCALATION_BACKEND",
    "PIPELINE_ESCALATION_MODEL",
    "PIPELINE_SECURITY_AUDIT_EVERY_DAYS",
    "PIPELINE_SECURITY_AUDIT_EVERY_COMMITS",
)

_ALLOWED = frozenset(_ALLOWED_KEYS)

_PAUSE = "PIPELINE_PAUSE_THRESHOLD"
_RESUME = "PIPELINE_RESUME_THRESHOLD"
_ESCALATION_BACKEND = "PIPELINE_ESCALATION_BACKEND"
_ESCALATION_MODEL = "PIPELINE_ESCALATION_MODEL"

_AUTONOMY_MODES = ("dry-run", "gated", "full")

_DRY_RUN_TEXT = (
    "No story will be dispatched in dry-run mode: review a dry run, then re-run "
    "with `--autonomy gated` or set `PIPELINE_AUTONOMY=gated` to let the "
    "pipeline act."
)
_GATED_TEXT = (
    "Stories at or below PIPELINE_RISK_THRESHOLD may merge unattended; "
    "higher-risk work waits for your approval."
)
_FULL_TEXT = (
    "The risk threshold is not consulted: every story may proceed unattended."
)


def build_policy_settings(answers: Mapping[str, object]) -> dict[str, str]:
    """Project raw policy answers onto the writable ``.pipeline.env`` entries.

    ``answers`` is keyed by the policy env-var names. Each key that is present
    and not ``None`` is normalized through :func:`pipeline.setup.schema.validate_value`;
    keys that are absent — or that normalize to the empty string, the schema's
    own encoding of "leave unset" — are omitted from the result entirely, so an
    unset escalation target stays unset instead of being forced to a value.

    Returns a ``dict`` whose keys are a subset of the policy keys this story
    owns and whose values are all normalized strings.

    Raises ``ValueError`` when a value is invalid, when an answer names a key
    outside the allowed set, or when a cross-field rule is broken. Every problem
    found is reported in one message so the operator can fix them in one pass.
    Messages name keys, never values.
    """
    if not isinstance(answers, Mapping):
        raise TypeError("policy answers must be a mapping of key to value")

    errors: list[str] = []

    unknown = sorted(str(key) for key in answers if key not in _ALLOWED)
    if unknown:
        errors.append(
            "unknown policy key(s): "
            + ", ".join(unknown)
            + "; allowed keys are "
            + ", ".join(_ALLOWED_KEYS)
        )

    settings: dict[str, str] = {}
    for key in _ALLOWED_KEYS:
        if key not in answers:
            continue
        value = answers[key]
        if value is None:
            continue
        try:
            raw = value if isinstance(value, str) else str(value)
        except Exception:  # noqa: BLE001 - a hostile __str__ must not escape
            errors.append(f"{key} could not be read as text")
            continue
        ok, normalized = schema.validate_value(key, raw)
        if not ok:
            errors.append(normalized)
            continue
        if not normalized:
            # The schema's "leave unset" encoding; emitting it would write an
            # empty entry rather than leaving the key alone.
            continue
        settings[key] = normalized

    errors.extend(_cross_field_errors(settings))

    if errors:
        raise ValueError("; ".join(errors))
    return settings


def _cross_field_errors(settings: Mapping[str, str]) -> list[str]:
    """Return the cross-field violations in ``settings``, if any."""
    errors: list[str] = []

    pause = settings.get(_PAUSE)
    resume = settings.get(_RESUME)
    if pause is not None and resume is not None and int(pause) <= int(resume):
        errors.append(
            f"{_PAUSE} ({pause}) must be greater than {_RESUME} ({resume}) "
            "so the usage gate does not flap"
        )

    if _ESCALATION_MODEL in settings and _ESCALATION_BACKEND not in settings:
        errors.append(
            f"{_ESCALATION_MODEL} requires {_ESCALATION_BACKEND}; set a backend "
            "or leave both unset"
        )

    return errors


def describe_autonomy(mode: str) -> str:
    """Return the one-sentence description of ``mode``.

    Deterministic and free of secret values, so the wizard summary and the docs
    can render it verbatim. Raises ``ValueError`` for an unknown mode.
    """
    if mode == "dry-run":
        return _DRY_RUN_TEXT
    if mode == "gated":
        return _GATED_TEXT
    if mode == "full":
        return _FULL_TEXT
    raise ValueError(
        f"unknown autonomy mode {mode!r}; valid modes are "
        + ", ".join(_AUTONOMY_MODES)
    )
