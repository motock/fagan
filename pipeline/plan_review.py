"""Per-story review warnings for a plan manifest.

``review_warnings`` answers the question an operator asks before dispatch:
"which advisory warnings would ingest raise for each of these stories right
now?" Every check is delegated to the helper that owns it
(``pipeline.ingest`` and ``pipeline.build_detect``) rather than
re-implemented here, so a warning ingest would raise is exactly a warning
reported by this preview.

The ``pipeline.ingest`` import is deliberately lazy: ingest imports modules
that import this one, so a module-level import would form a cycle.
"""

import logging

logger = logging.getLogger(__name__)


def review_warnings(manifest: dict) -> dict[str, list[str]]:
    """Return per-story advisory review warnings, keyed by story id.

    Stories are visited in ``manifest["stories"]`` insertion order. A story
    that raises no warnings maps to ``[]``. Boundary shapes (a non-dict
    manifest, or a missing/non-dict ``stories``) yield ``{}``, and a non-dict
    story entry is skipped instead of raising.

    A helper that raises for one story records ``["warning check failed"]``
    for that story — logged by exception type only, never by message or path
    — and never aborts the remaining stories.
    """
    if not isinstance(manifest, dict):
        return {}
    stories = manifest.get("stories")
    if not isinstance(stories, dict):
        return {}

    from . import build_detect, ingest

    role_config = manifest.get("role_config")
    repo_root = manifest.get("repo_root") or ""
    warnings: dict[str, list[str]] = {}
    for story_id, story in stories.items():
        if not isinstance(story, dict):
            continue
        found: list[str] = []
        try:
            sizing = ingest._story_sizing_warning(
                ingest._sizing_view(story, role_config), repo_root
            )
            if sizing:
                found.append(sizing)
            if ingest._preflight_dispatch_provider(story, role_config) != "claude":
                status = ingest._preflight_status(story.get("agent_instructions"))
                if status != "ok":
                    found.append(f"preflight: {status}")
            security = ingest._security_review_unenforced_warning(story)
            if security:
                found.append(security)
            isolation = build_detect._isolation_only_acceptance_warning(story)
            if isolation:
                found.append(isolation)
        except Exception as exc:  # noqa: BLE001 - one bad story must not abort the rest
            logger.warning("plan review warning check failed: %s", type(exc).__name__)
            found = ["warning check failed"]
        warnings[story_id] = found
    return warnings
