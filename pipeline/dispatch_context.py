"""Resolve a plan's dispatch-phase external context sources for a story.

Lives outside ``dispatch.py`` because that module is held under the
1000-line limit enforced by the line-limit tests.
"""

import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import context_sources as ctxsrc
from . import paths
from .service import _ServerRef

_atomic_write_json = _ServerRef("_atomic_write_json")
_notify_user = _ServerRef("_notify_user")


def _load_recorded_context(source: dict[str, Any], recorded: dict[str, Any]) -> dict[str, Any]:
    """Return the snapshot for a previously recorded digest, verifying it."""
    digest = recorded["digest"]
    hexdigest = digest.removeprefix("sha256:")
    path = paths.PLAN_DIR / "context" / hexdigest / "content.txt"
    try:
        text = path.read_text(encoding="utf-8") if hexdigest.isalnum() else None
    except OSError:
        text = None
    if text is None:
        # Snapshot lost: refetch, but only accept byte-identical content.
        text = ctxsrc.resolve_dir_source(source)["text"]
        ctxsrc.snapshot_context(text)
    if "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest() != digest:
        raise ctxsrc.ContextSourceError(
            f"source {source['name']!r}: context no longer matches recorded digest {digest}"
        )
    return {"text": text, "digest": digest, "bytes": recorded["bytes"]}


def _fetch_context(source: dict[str, Any], story: dict[str, Any], now: str) -> dict[str, Any]:
    resolved = ctxsrc.resolve_dir_source(source)
    ctxsrc.snapshot_context(resolved["text"])
    story["context_digests"][source["name"]] = {
        "provider": source["provider"],
        "uri": source["uri"],
        "digest": resolved["digest"],
        "bytes": resolved["bytes"],
        "files": resolved["files"],
        "truncated": resolved["truncated"],
        "fetched_at": now,
    }
    return resolved


def _step_resolve_context(
    plan_name: str,
    story_key: str,
    story: dict[str, Any],
    manifest: dict[str, Any],
    manifest_path: Path,
) -> tuple[str, dict[str, Any] | None]:
    """Resolve the plan's dispatch-phase context sources for a story.

    Returns ``(rendered_block, blocked_result)``; a non-None result is a
    required-source failure that must be returned as-is (status unchanged).
    Recorded ``context_digests`` are reused on rework/resume, never refetched.
    """
    context = manifest.get("context")
    sources = ctxsrc.sources_for_phase(context["sources"], "dispatch") if context else []
    if not sources:
        return "", None
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    digests = story.setdefault("context_digests", {})
    resolved = []
    for source in sources:
        try:
            recorded = digests.get(source["name"])
            loaded = (
                _load_recorded_context(source, recorded)
                if recorded
                else _fetch_context(source, story, now)
            )
        except ctxsrc.ContextSourceError as exc:
            if source.get("required", True):
                reason = f"required context source failed: {exc}"
                story["dispatch_block_reason"] = reason
                _atomic_write_json(manifest_path, manifest)
                logging.getLogger("pipeline").warning(
                    "dispatch blocked by required context source",
                    extra={"plan": plan_name, "story_key": story_key},
                )
                return "", {"ok": False, "error": reason, "story_key": story_key}
            _notify_user(
                plan_name,
                f"optional context source {source['name']} unavailable for "
                f"{story_key}; dispatching without it",
                story_key=story_key,
                severity="warning",
                event="context_missing",
                dedup_key=f"context_missing:{story_key}:{source['name']}",
            )
            continue
        resolved.append({**source, **{k: loaded[k] for k in ("text", "digest", "bytes")}})
    if story["context_digests"]:
        _atomic_write_json(manifest_path, manifest)
    max_total = context.get("max_total_chars", ctxsrc.DEFAULT_MAX_TOTAL_CHARS)
    return ctxsrc.render_context_block(resolved, max_total_chars=max_total), None
