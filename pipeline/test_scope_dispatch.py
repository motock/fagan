"""Resolve and record ``test_scope`` for one dispatch.

Glue between the pure resolver (``pipeline.test_scope``) and dispatch: reads
the policy settings and the environment, stamps the result on the story, and
notifies the user when a policy refused a ``scoped`` request.
"""

import logging
import os
from datetime import UTC, datetime

from .authorization import DEFAULT_TEAM
from .file_size_policy_dispatch import stamp_file_size_policy
from .forge import get_forge
from .policy_settings import policy_settings
from .service import _ServerRef
from .test_scope import resolve_test_scope

_notify_user = _ServerRef("_notify_user")
TEST_SCOPE_ENV = "PIPELINE_TEST_SCOPE"
REWORK_ON_CI_FAIL_ENV = "PIPELINE_REWORK_ON_CI_FAIL"
SETTINGS_NAMESPACE = "default"


def rework_on_ci_fail_enabled() -> bool:
    return os.environ.get(REWORK_ON_CI_FAIL_ENV, "0") == "1"


def ci_unavailable_reason(manifest: dict) -> str | None:
    from . import ci as _ci

    if not _ci.PIPELINE_MERGE_CI_GATE:
        return "ci_gate_disabled"
    if not rework_on_ci_fail_enabled():
        return "rework_on_ci_fail_off"
    repo_root = manifest.get("repo_root")
    if not isinstance(repo_root, str) or not repo_root:
        return "no_ci_config"
    try:
        has_ci = get_forge(repo_root).has_ci_config(repo_root)
    except Exception as exc:  # noqa: BLE001 - fail closed to full, never raise
        logging.getLogger("pipeline").warning(
            "CI configuration unreadable; treating test_scope as full",
            extra={"error_type": type(exc).__name__},
        )
        return "no_ci_config"
    return None if has_ci else "no_ci_config"


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
    if result["test_scope"] == "scoped":
        reason = ci_unavailable_reason(manifest)
        if reason is not None:
            result = {
                **result,
                "test_scope": "full",
                "degraded_from": "scoped",
                "degraded_reason": reason,
            }
            _notify_user(
                plan_name,
                f"{story_key} test_scope scoped degraded to full: {reason}",
                story_key=story_key,
                severity="warning",
                event="test_scope_degraded",
                dedup_key=f"test_scope_degraded:{plan_name}:{reason}",
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
    stamp_file_size_policy(plan_name, story_key, settings, story)
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
