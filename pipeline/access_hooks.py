"""Consult the repository access hook before a dispatch and before a merge.

``refuse_dispatch`` runs once a story is known and before any worktree or git
work. ``merge_decision`` wraps the merge gate's decision so a refused story is
parked before that decision is computed. With ``PIPELINE_REPO_ACCESS_PLUGIN``
unset there is no hook, and both entry points change nothing.

The hook is a security seam. It loads only through the plugin allowlist, and
every failure to consult it (an exception, a malformed result, a bad spec)
refuses. A refusal parks the story with ``access refused: <reason token>`` and
appends one host audit record carrying the action and the reason token. It
never carries the plan, the story, the repository or the hook's free text.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from app.plugin_loader import (
    load_plugin,
    plugin_spec,
    require_allowlist,
    require_methods,
)
from pipeline.actor import current_namespace
from pipeline.audit import append_host_audit

logger = logging.getLogger("pipeline")

ACCESS_HOOK_PLUGIN_ENV = "PIPELINE_REPO_ACCESS_PLUGIN"
REFUSAL_PREFIX = "access refused: "

_SEAM = "repo_access"
_HOOK_ERROR = "hook_error"
_UNSPECIFIED = "unspecified"
_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    reason: str


class RepoAccessHook(Protocol):
    def check(
        self, namespace: str, repo: str, action: str, risk: str
    ) -> AccessDecision: ...


def refuse_dispatch(
    store: Any, plan_name: str, story_key: str, manifest: dict[str, Any]
) -> dict[str, Any] | None:
    """Park ``story_key`` and return a refusal when the hook refuses its dispatch."""
    story = manifest["stories"][story_key]
    reason = _refusal_reason("dispatch", story, lambda: _manifest_repo(manifest))
    if reason is None:
        return None
    story["status"] = "parked"
    story["parked_reason"] = REFUSAL_PREFIX + reason
    store.records().put_manifest("default", plan_name, manifest)
    return {"ok": False, "error": REFUSAL_PREFIX + reason, "story_key": story_key}


def merge_decision(
    decide: Callable[[dict[str, Any]], dict[str, str]],
    plan_name: str,
    story: dict[str, Any],
) -> dict[str, str]:
    """Return a park decision when the hook refuses the merge, else ``decide(story)``."""
    reason = _refusal_reason("merge", story, lambda: _plan_repo(plan_name))
    if reason is not None:
        return {"action": "park", "reason": REFUSAL_PREFIX + reason}
    return decide(story)


def merge_refusal(plan_name: str, story: dict[str, Any]) -> dict[str, str] | None:
    """Return the error result when the hook refuses a manual merge, else None.

    Unlike ``refuse_dispatch`` this parks nothing: the caller returns the error
    and the story, its branch and its PR stay exactly as they were.
    """
    reason = _refusal_reason("merge", story, lambda: _plan_repo(plan_name))
    if reason is None:
        return None
    return {"ok": False, "error": REFUSAL_PREFIX + reason}


def _refusal_reason(
    action: str, story: dict[str, Any], resolve_repo: Callable[[], str]
) -> str | None:
    """Return the refusal reason token, or None when ``action`` may proceed."""
    try:
        hook = _hook()
        if hook is None:
            return None
        decision = hook.check(
            current_namespace(),
            resolve_repo(),
            action,
            str(story.get("risk") or "unknown"),
        )
    except Exception as exc:  # noqa: BLE001 - any failure to consult must refuse
        logger.error(
            "repo access hook failed; refusing",
            extra={"access_action": action, "error_type": type(exc).__name__},
        )
        return _record_refusal(action, _HOOK_ERROR)
    if not isinstance(decision, AccessDecision) or not isinstance(
        decision.allowed, bool
    ):
        return _record_refusal(action, _HOOK_ERROR)
    if decision.allowed:
        return None
    is_token = isinstance(decision.reason, str) and _TOKEN_RE.match(decision.reason)
    token = decision.reason if is_token else _UNSPECIFIED
    return _record_refusal(action, token)


def _record_refusal(action: str, reason: str) -> str:
    try:
        append_host_audit(
            action="repo_access_refused",
            outcome="refused",
            detail={"access_action": action, "reason": reason},
        )
    except Exception:  # noqa: BLE001 - a failed audit append never changes the decision
        logger.warning(
            "repo access refusal audit append failed", extra={"access_action": action}
        )
    return reason


def _hook() -> RepoAccessHook | None:
    """Return the configured hook, or None when no plugin is selected.

    The allowlist is checked before the plugin is imported, so a refused spec
    never runs. Any error here propagates to ``_refusal_reason``, which refuses.
    """
    if plugin_spec(_SEAM) is None:
        return None
    require_allowlist(_SEAM)
    hook = load_plugin(_SEAM)()
    require_methods(hook, ("check",), "repo access hook")
    return hook


def _plan_repo(plan_name: str) -> str:
    from .ci import settings_for_plan

    return settings_for_plan(plan_name).repo


def _manifest_repo(manifest: dict[str, Any]) -> str:
    from .ci import context_from_manifest

    return context_from_manifest(manifest).repo
