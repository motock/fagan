"""Dispatch-phase external context resolution carved out of ``pipeline.dispatch``.

Resolves the plan's ``dispatch`` context sources for a story, persists the
recorded ``context_digests``, and turns a required-source failure into a
blocked-dispatch result that leaves the story's status unchanged.
"""

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .context_sources import ContextSourceError, resolve_dispatch_context
from .service import _ServerRef

_atomic_write_json = _ServerRef("_atomic_write_json")
_notify_user = _ServerRef("_notify_user")


def _step_resolve_context(
    plan_name: str,
    story_key: str,
    story: dict[str, Any],
    manifest: dict[str, Any],
    manifest_path: Path,
) -> tuple[str, dict[str, Any] | None]:
    """Return ``(context_block, blocked_result)``; a non-None result must be returned as-is."""
    try:
        block, missing = resolve_dispatch_context(
            manifest.get("context"), story,
            datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    except ContextSourceError as exc:
        reason = f"required context source failed: {exc}"
        story["dispatch_block_reason"] = reason
        _atomic_write_json(manifest_path, manifest)
        logging.getLogger("pipeline").warning(
            "dispatch blocked by required context source",
            extra={"plan": plan_name, "story_key": story_key},
        )
        return "", {"ok": False, "error": reason, "story_key": story_key}
    for name, _reason in missing:
        _notify_user(
            plan_name,
            f"optional context source {name} unavailable for {story_key}; "
            "dispatching without it",
            story_key=story_key,
            severity="warning",
            event="context_missing",
            dedup_key=f"context_missing:{story_key}:{name}",
        )
    if story.get("context_digests"):
        _atomic_write_json(manifest_path, manifest)
    return block, None
