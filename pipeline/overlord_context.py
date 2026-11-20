"""Recorded context sources as REFERENCE DATA for the overlord's rulings.

The overlord sees only the snapshots recorded on the story at dispatch
(``context_digests``); this module never fetches. A source therefore reaches
the overlord only when it lists the ``dispatch`` phase as well as the overlord
phase. The block guides the ruling. It never grants an action: the authority
matrix, ``ALLOWED_ACTIONS`` and the merge gate decide what executes.
"""

from __future__ import annotations

import logging

from . import bound_records
from . import context_sources as ctxsrc
from .review import _read_recorded_snapshot

OVERLORD_PHASES = ("triage", "merge_adjudication")

_UNVERIFIED_SNAPSHOT_LINE = (
    "[context source {name} was recorded for this story but its snapshot is "
    "unavailable; this review cannot verify it]"
)


def overlord_context(plan_name: str, story_key: str, phase: str) -> tuple[str, list[str]]:
    """Return ``(block, digests)`` for an overlord ``phase`` ruling.

    ``block`` is "" or a rendered block that starts and ends with a newline,
    so callers can splice it between existing prompt lines. ``digests`` are
    the digests of the sources the block rendered, in render order. Any
    failure (manifest unreadable, nothing rendered) returns ``("", [])`` and
    logs one WARNING naming the plan, story and phase, never the exception
    message: the ruling must still happen.
    """
    if phase not in OVERLORD_PHASES:
        raise ValueError(f"unsupported overlord context phase {phase!r}")
    try:
        from .server import _store

        manifest = bound_records.get_manifest(_store, plan_name)
        body, digests = _render(manifest or {}, story_key, phase)
    except Exception as exc:  # noqa: BLE001 - the ruling must still happen
        _warn_unavailable(plan_name, story_key, phase, type(exc).__name__)
        return "", []
    if not body:
        _warn_unavailable(plan_name, story_key, phase, "EmptyContextBlock")
        return "", []
    return "\n" + body.strip("\n") + "\n", digests


def _render(manifest: dict, story_key: str, phase: str) -> tuple[str, list[str]]:
    """Render the story's recorded snapshots for ``phase`` and their digests.

    Mirrors ``pipeline.review.build_phase_context_block`` (same selection,
    digest check and warning line) in one pass, so the digests returned are
    exactly those of the sources whose text the block carries.
    """
    context = manifest.get("context")
    story = (manifest.get("stories") or {}).get(story_key) or {}
    recorded_all = story.get("context_digests") or {}
    if not context or not recorded_all:
        return "", []
    resolved: list[dict] = []
    warnings: list[str] = []
    for source in ctxsrc.sources_for_phase(context["sources"], phase):
        recorded = recorded_all.get(source["name"])
        if not recorded:
            continue
        text = _read_recorded_snapshot(recorded)
        if text is None:
            warnings.append(_UNVERIFIED_SNAPSHOT_LINE.format(name=source["name"]))
            continue
        resolved.append(
            {**source, "text": text, "digest": recorded["digest"], "bytes": recorded.get("bytes", len(text))}
        )
    max_total = context.get("max_total_chars", ctxsrc.DEFAULT_MAX_TOTAL_CHARS)
    block, digests = ctxsrc.render_context_block_with_digests(resolved, max_total_chars=max_total)
    return "\n".join(part for part in (block, *warnings) if part), digests


def _warn_unavailable(plan_name: str, story_key: str, phase: str, reason: str) -> None:
    logging.getLogger("pipeline").warning(
        "overlord context unavailable: plan=%s story=%s phase=%s reason=%s",
        plan_name,
        story_key,
        phase,
        reason,
    )
