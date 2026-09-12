import json
import logging
from pathlib import Path

from .event_guards import check_precondition
from .events import EventBus, InProcessEventBus
from .notification_outbox import outbox_sink
from .notification_sinks import file_log_sink
from .paths import PLAN_DIR

logger = logging.getLogger(__name__)


def wake_handler(event: dict) -> dict:
    """Handle an agent_done event to potentially advance the pipeline.

    Parameters
    ----------
    event : dict
        Must contain ``plan``, ``story_key`` and ``type`` keys.  Missing keys
        raise KeyError as a programming error.

    Returns
    -------
    dict
        * If the manifest is missing or unparseable: ``{"ok": False, "skipped": "no_manifest"}``
        * If :func:`check_precondition` returns ``{"ok": False, ...}``: that dict unchanged.
        * Otherwise: ``{"ok": True, "woke": <plan>, "result": <advance_pipeline return>}``
    """

    plan_name = event["plan"]
    story_key = event["story_key"]
    event_type = event["type"]

    manifest_path: Path = PLAN_DIR / f"{plan_name}.manifest.json"
    if not manifest_path.exists():
        logger.warning("Manifest missing for plan %s", plan_name)
        return {"ok": False, "skipped": "no_manifest"}
    try:
        manifest_text = manifest_path.read_text()
        manifest = json.loads(manifest_text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to read or parse manifest %s: %s", manifest_path, exc)
        return {"ok": False, "skipped": "no_manifest"}

    precond = check_precondition(manifest, story_key, event_type)
    if not precond.get("ok"):
        # Return the dict unchanged – a stale or redelivered event.
        return precond

    # Lazy import to avoid circular dependency.
    from .server import advance_pipeline

    result = advance_pipeline(plan_name)
    return {"ok": True, "woke": plan_name, "result": result}


def build_bus() -> EventBus:
    bus = InProcessEventBus()
    bus.subscribe("agent_done", wake_handler)
    bus.subscribe("notification", file_log_sink)
    bus.subscribe("notification", outbox_sink)
    return bus

# Process‑level singleton for the event bus.
_BUS = None

def get_bus() -> EventBus:
    """The one bus this process publishes on. Subsequent calls return the same instance.

    The bus is lazily created on first use and cached in the module‑level ``_BUS`` variable.
    It never re‑subscribes handlers; a second call returns the existing bus.
    """
    global _BUS
    if _BUS is None:
        _BUS = build_bus()
    return _BUS
