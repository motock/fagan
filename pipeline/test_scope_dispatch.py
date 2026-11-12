"""Resolve and record ``test_scope`` for one dispatch.

Glue between the pure resolver (``pipeline.test_scope``) and dispatch: reads
the policy settings and the environment, stamps the result on the story, and
notifies the user when a policy refused a ``scoped`` request.
"""

import os
from datetime import UTC, datetime

from .authorization import DEFAULT_TEAM
from .policy_settings import policy_settings
from .service import _ServerRef
from .test_scope import resolve_test_scope

_notify_user = _ServerRef("_notify_user")
TEST_SCOPE_ENV = "PIPELINE_TEST_SCOPE"
SETTINGS_NAMESPACE = "default"


def _lock_rejected_message(story_key, rejections):
    refused = ", ".join(f"{r['layer']}->{r['by']}" for r in rejections)
    return (
        f"{story_key} test_scope request refused by policy: {refused}; running full"
    )


def stamp_test_scope(plan_name, story_key, manifest, story):
    settings = policy_settings(
        SETTINGS_NAMESPACE, manifest.get("team") or DEFAULT_TEAM
    )
    result = resolve_test_scope(
        settings=settings,
        plan_value=manifest.get("test_scope"),
        story_value=story.get("test_scope"),
        env_value=os.environ.get(TEST_SCOPE_ENV),
    )
    result["resolved_at"] = datetime.now(UTC).isoformat()
    story["test_scope_resolution"] = result
    if result["lock_rejections"]:
        _notify_user(
            plan_name,
            _lock_rejected_message(story_key, result["lock_rejections"]),
            story_key=story_key,
            severity="info",
            event="test_scope_lock_rejected",
            dedup_key=f"test_scope_lock_rejected:{plan_name}:{story_key}",
        )
    return result


def test_scope_event_fields(story):
    resolution = story.get("test_scope_resolution")
    resolved = resolution if isinstance(resolution, dict) else {}
    if story.get("ci_rework"):
        rework = "ci"
    elif story.get("review_feedback"):
        rework = "review"
    else:
        rework = "none"
    return {
        "test_scope": resolved.get("test_scope", "full"),
        "test_scope_source": resolved.get("source", "default"),
        "test_scope_lock_rejected": bool(resolved.get("lock_rejections")),
        "rework": rework,
    }


# Not a test: pytest would otherwise collect this module's `test_*` name.
test_scope_event_fields.__test__ = False


def effective_test_scope(manifest):
    manifest = manifest or {}
    settings = policy_settings(
        SETTINGS_NAMESPACE, manifest.get("team") or DEFAULT_TEAM
    )
    result = resolve_test_scope(
        settings=settings,
        plan_value=manifest.get("test_scope"),
        env_value=os.environ.get(TEST_SCOPE_ENV),
    )
    return {**result, "env": TEST_SCOPE_ENV}
