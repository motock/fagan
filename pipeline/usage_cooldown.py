"""A short dispatch cooldown for the Claude backend (UC-02).

The usage gate reacts to a *polled* snapshot, and that snapshot lags the real
cap (see `resource_status`'s docstring in app/backend_claude.py, live
2026-10-06). An agent can therefore die on "hit your session limit" before the
poller trips the gate, and the scheduler redispatches into the same wall every
tick. `trip()` lets the caller that observed the cap latch a cooldown;
`active()` is what `ClaudeCliDriver.resource_status` consults.

State is one JSON file, `usage_cooldown.json`, beside `USAGE_STATE_PATH`:
`{"until": "<ISO-8601 UTC>"}`.

Fail-open by design: a missing, corrupt or unparsable file is NOT active, and
neither `trip()` nor `active()` ever raises. A bad file must never block
dispatch forever.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pipeline.paths import USAGE_STATE_PATH

DEFAULT_COOLDOWN_SECONDS = 600
_COOLDOWN_FILENAME = "usage_cooldown.json"
_ENV_VAR = "PIPELINE_USAGE_COOLDOWN_SECONDS"


def _utcnow() -> datetime:
    """The module's single time source, so tests can freeze the clock."""
    return datetime.now(UTC)


def _cooldown_path() -> Path:
    """The latch file, resolved at call time.

    `USAGE_STATE_PATH` is looked up as a module global on every call (never
    cached at import) so tests and callers can repoint it.
    """
    return Path(USAGE_STATE_PATH).parent / _COOLDOWN_FILENAME


def _cooldown_seconds() -> int:
    """The configured cooldown length, read at call time.

    A missing, non-integer or non-positive value falls back to the default
    rather than raising or latching a zero-length (useless) cooldown.
    """
    raw = os.environ.get(_ENV_VAR)
    if raw is None:
        return DEFAULT_COOLDOWN_SECONDS
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_COOLDOWN_SECONDS
    if seconds <= 0:
        return DEFAULT_COOLDOWN_SECONDS
    return seconds


def trip(seconds: int | None = None) -> None:
    """Latch a cooldown of `seconds` (default: the configured length).

    Writes atomically via a sibling temp file + `os.replace`, so a concurrent
    `active()` never reads a half-written file. Never raises: a cooldown that
    cannot be recorded must not break the caller that just saw the cap.
    """
    tmp: Path | None = None
    try:
        if seconds is None:
            seconds = _cooldown_seconds()
        until = _utcnow() + timedelta(seconds=seconds)
        path = _cooldown_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
        tmp.write_text(json.dumps({"until": until.isoformat()}))
        os.replace(tmp, path)
        tmp = None
    except Exception:  # noqa: S110, BLE001 - never raise out of trip()
        pass
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


def active() -> bool:
    """True only when the latch file parses and its `until` is in the future.

    Pure read: no writes, no deletes, no mkdir. Anything unexpected - missing
    file, bad JSON, a non-string or unparsable `until` - is reported as not
    active (fail open).
    """
    try:
        data = json.loads(_cooldown_path().read_text())
        if not isinstance(data, dict):
            return False
        raw_until = data.get("until")
        if not isinstance(raw_until, str):
            return False
        until = datetime.fromisoformat(raw_until)
        if until.tzinfo is None:
            until = until.replace(tzinfo=UTC)
        return until > _utcnow()
    except Exception:  # noqa: BLE001 - fail open on any unreadable state
        return False
