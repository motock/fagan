"""Consult the repository access hook before a dispatch, a merge, a read and a plan.

``plan_refusal`` runs when a plan naming a ``repo_url`` is saved, drafted or
ingested, before anything is cloned. ``refuse_dispatch`` runs once a story is known and before any worktree or git
work. ``merge_decision`` wraps the merge gate's decision so a refused story is
parked before that decision is computed. ``pipeline.repo_read_access`` asks the
same hook before a repository read. With ``PIPELINE_REPO_ACCESS_PLUGIN`` unset
there is no hook, and every entry point changes nothing.

The hook is a security seam. It loads only through the plugin allowlist, and
every failure to consult it (an exception, a malformed result, a bad spec)
refuses. A refusal parks the story with ``access refused: <reason token>`` and
appends one host audit record carrying the action and the reason token. It
never carries the plan, the story, the repository or the hook's free text.

External dependency references have their own hook, ``PIPELINE_REFERENCE_PLUGIN``,
which is the opposite way round: with it unset every reference is refused.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, cast

from app.plugin_loader import (
    load_plugin,
    plugin_spec,
    require_allowlist,
    require_methods,
)
from pipeline import bound_records
from pipeline.actor import current_namespace
from pipeline.audit import append_host_audit

logger = logging.getLogger("pipeline")

ACCESS_HOOK_PLUGIN_ENV = "PIPELINE_REPO_ACCESS_PLUGIN"
REFERENCE_HOOK_PLUGIN_ENV = "PIPELINE_REFERENCE_PLUGIN"
REFUSAL_PREFIX = "access refused: "

_SEAM = "repo_access"
_REFERENCE_SEAM = "reference"
_HOOK_ERROR = "hook_error"
_NO_REFERENCE_PLUGIN = "no_reference_plugin"
_REFERENCE_DENIED = "reference_denied"
_UNSPECIFIED = "unspecified"
REPOSITORY_UNIDENTIFIED = "repository_unidentified"
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
    bound_records.put_manifest(store, plan_name, manifest)
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


def merge_refusal(plan_name: str, story: dict[str, Any]) -> dict[str, Any] | None:
    """Return the error result when the hook refuses a manual merge, else None.

    Unlike ``refuse_dispatch`` this parks nothing: the caller returns the error
    and the story, its branch and its PR stay exactly as they were.
    """
    reason = _refusal_reason("merge", story, lambda: _plan_repo(plan_name))
    if reason is None:
        return None
    return {"ok": False, "error": REFUSAL_PREFIX + reason}


def plan_refusal(repo: str) -> str | None:
    """Return the refusal reason token for a plan naming ``repo``, else None.

    The question is whether the bound namespace could dispatch into ``repo``, so
    the existing ``dispatch`` action answers it and no hook needs a new one.
    """
    return _refusal_reason("dispatch", {"risk": "low"}, lambda: repo)


def unidentified_plan_refusal() -> str | None:
    """Return the refusal for a plan whose repository identity cannot be derived.

    Only a configured hook refuses. With none, the clone path reports the bad
    URL as it always has.
    """
    if plugin_spec(_SEAM) is None:
        return None
    return _record_refusal("dispatch", REPOSITORY_UNIDENTIFIED)


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
    # plugin_spec() above is not None, so load_plugin returns a factory.
    hook = cast(Callable, load_plugin(_SEAM))()
    require_methods(hook, ("check",), "repo access hook")
    return hook


class ReferenceHook(Protocol):
    def allow(self, from_namespace: str, to_namespace: str) -> bool: ...


def reference_refusal(from_namespace: str, to_namespace: str) -> str | None:
    """Return the refusal reason token for an external reference, else None.

    Ingest calls this once per external reference, and a refusal is audited.
    With no ``PIPELINE_REFERENCE_PLUGIN`` selected, every reference is refused.
    """
    reason = _reference_reason(from_namespace, to_namespace)
    if reason is None:
        return None
    return _record_refusal("reference", reason)


def reference_allowed(from_namespace: str, to_namespace: str) -> bool:
    """Return True only when the reference hook allows the reference right now.

    Readiness calls this on every check, so it does not audit: a refusal is
    audited once, at ingest.
    """
    return _reference_reason(from_namespace, to_namespace) is None


def _reference_reason(from_namespace: str, to_namespace: str) -> str | None:
    """Return the refusal reason token, or None only when the hook returns True."""
    try:
        hook = _reference_hook()
        if hook is None:
            return _NO_REFERENCE_PLUGIN
        allowed = hook.allow(from_namespace, to_namespace)
    except Exception as exc:  # noqa: BLE001 - any failure to consult must refuse
        logger.error(
            "reference hook failed; refusing",
            extra={"error_type": type(exc).__name__},
        )
        return _HOOK_ERROR
    if allowed is True:
        return None
    return _REFERENCE_DENIED


def _reference_hook() -> ReferenceHook | None:
    """Return the configured reference hook, or None when no plugin is selected.

    Unlike the repo access hook, the absence of a plugin is not a pass-through:
    ``_reference_reason`` turns a None here into a refusal.
    """
    if plugin_spec(_REFERENCE_SEAM) is None:
        return None
    require_allowlist(_REFERENCE_SEAM)
    # plugin_spec() above is not None, so load_plugin returns a factory.
    hook = cast(Callable, load_plugin(_REFERENCE_SEAM))()
    require_methods(hook, ("allow",), "reference hook")
    return hook


def _plan_repo(plan_name: str) -> str:
    from .ci import settings_for_plan

    return settings_for_plan(plan_name).repo


def _manifest_repo(manifest: dict[str, Any]) -> str:
    from .ci import context_from_manifest

    return context_from_manifest(manifest).repo


# ---------- Admission hook (dispatch, spend, notification) ----------
#
# A second, independent seam. ``admit`` is consulted by the spend gate and the
# notification sink, and is the single decision point the dispatch path will
# call. With ``PIPELINE_ADMISSION_PLUGIN`` unset every call admits and changes
# nothing. A refusal is audited with its reason token. A failure to consult the
# hook is decided per caller by ``fail_open``: a notification passes, because a
# lost alert is worse than a sent one; every other kind fails closed.

ADMISSION_HOOK_PLUGIN_ENV = "PIPELINE_ADMISSION_PLUGIN"
ADMISSION_KINDS = frozenset({"dispatch", "spend", "notification"})

_ADMISSION_SEAM = "admission"


@dataclass(frozen=True)
class AdmissionDecision:
    admitted: bool
    reason: str


class AdmissionHook(Protocol):
    def admit(
        self, kind: str, namespace: str, context: dict[str, Any]
    ) -> AdmissionDecision: ...


def admit(kind: str, context: dict[str, Any], *, fail_open: bool = False) -> bool:
    """Return whether ``kind`` may proceed.

    ``context`` carries only counts or percentages the caller already holds,
    never secrets or payloads. ``fail_open`` is the answer when the hook cannot
    be consulted: the caller states it, because the safe side differs per kind.
    """
    if kind not in ADMISSION_KINDS:
        raise ValueError(f"unknown admission kind {kind!r}")
    try:
        hook = _admission_hook()
        if hook is None:
            return True
        decision = hook.admit(kind, current_namespace(), dict(context))
    except Exception as exc:  # noqa: BLE001 - any failure to consult takes the caller's safe side
        logger.error(
            "admission hook failed",
            extra={"admission_kind": kind, "error_type": type(exc).__name__},
        )
        return _hook_failed(kind, fail_open=fail_open)
    if not isinstance(decision, AdmissionDecision) or not isinstance(
        decision.admitted, bool
    ):
        return _hook_failed(kind, fail_open=fail_open)
    if decision.admitted:
        return True
    is_token = isinstance(decision.reason, str) and _TOKEN_RE.match(decision.reason)
    token = decision.reason if is_token else _UNSPECIFIED
    _audit_admission("admission_refused", "refused", kind, token)
    return False


def _hook_failed(kind: str, *, fail_open: bool) -> bool:
    outcome = "admitted" if fail_open else "refused"
    _audit_admission("admission_hook_failed", outcome, kind, _HOOK_ERROR)
    return fail_open


def _audit_admission(action: str, outcome: str, kind: str, reason: str) -> None:
    try:
        append_host_audit(
            action=action,
            outcome=outcome,
            detail={"admission_kind": kind, "reason": reason},
        )
    except Exception:  # noqa: BLE001 - a failed audit append never changes the decision
        logger.warning("admission audit append failed", extra={"admission_kind": kind})


def admit_dispatch(story: dict[str, Any]) -> bool:
    """Return whether ``story`` may take a dispatch slot this tick.

    A refusal or a hook failure returns False, and the caller defers the story:
    it stays dispatch-eligible, is not parked, and claims no lease or slot. The
    refusal itself is audited by ``admit``.
    """
    attempts = int(story.get("dispatch_attempts") or 0)
    return admit("dispatch", {"dispatch_attempts": attempts})


def _admission_hook() -> AdmissionHook | None:
    """Return the configured admission hook, or None when no plugin is selected.

    Same fail-closed loading as the repo access hook: the allowlist is checked
    before the plugin is imported, and any error propagates to ``admit``.
    """
    if plugin_spec(_ADMISSION_SEAM) is None:
        return None
    require_allowlist(_ADMISSION_SEAM)
    # plugin_spec() above is not None, so load_plugin returns a factory.
    hook = cast(Callable, load_plugin(_ADMISSION_SEAM))()
    require_methods(hook, ("admit",), "admission hook")
    return hook
