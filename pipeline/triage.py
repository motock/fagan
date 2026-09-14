# ruff: noqa
"""
This module implements the failure‑triage layer used by the scheduler.

All public functions in this module are fails open: on any error they
return a minimal, well‑formed prompt that contains the TRIAGE QU
and the park‑and‑notify invariant.
"""

import os
import subprocess
from pathlib import Path
from datetime import datetime, timezone
import logging
import json
from .overlord import _invoke_overlord, _load_policy
from .persistence import _notify_user
from .parsers import _parse_ruling
from .parsers import _atomic_write_json
from .persistence import _append_decision, _plan_role_config
from .build_detect import detect_test_command
from .concurrency import _heavy_lock, _is_heavy
from .config import STEP_CAP_FALLBACK_THRESHOLD
from .rebrief import collect_failure_evidence
from .escalation import (_auto_escalation_enabled, _escalate_to_claude, _escalate_to_local_fallback_model)
from .escalation import (_escalate_to_claude as _orig_escalate_to_claude, _escalate_to_local_fallback_model as _orig_escalate_to_local_fallback_model)
from .repo_health import format_findings, classify_repo_health
from .git_ops import _worktree_has_new_commits
TRIAGE_MAX_PER_TICK = 1

# E6/E7: split_story and repo_issue actions are deferred until implemented.
# Refer to docs/plans/OVERLORD_FAILURE_TRIAGE_PLAN.md for implementation details.
# Exported via __all__; handled in execute_ruling.
DEFERRED_ACTIONS = frozenset({"split_story", "repo_issue"})
# ---------------------------------------------------------------------------
# Triage executor helpers
# ---------------------------------------------------------------------------

# from .persistence import _notify_user


def _park(plan_name, story_key, story, reason) -> str:
    """Park a story and notify the user.

    The function sets ``story['status']`` to ``'parked'`` and records the
    ``reason`` in ``story['parked_reason']``.  It then attempts to notify the
    user via :func:`pipeline.persistence._notify_user`.  Any exception raised by
    the notification is swallowed so that the park operation never fails.

    Parameters
    ----------
    plan_name: str
        The name of the plan the story belongs to.
    story_key: str
        The unique key of the story.
    story: dict
        The mutable story dictionary.
    reason: str
        Human‑readable reason for parking.

    Returns
    -------
    str
        ``"park_for_human"`` – the marker used by the scheduler.
    """
    # Mutate the story first – this must happen regardless of notification
    story["status"] = "parked"
    story["parked_reason"] = reason
    try:
        _notify_user(
            plan_name,
            f"{story_key} triage: {reason}",
            event="story_parked",
        )
    except Exception:  # pragma: no cover – notification failures are ignored
        pass
    return "park_for_human"


def execute_ruling(plan_name, story_key, story, ruling, manifest, manifest_path) -> str:
    """Execute a ruling in this slice.

    The current implementation is intentionally minimal – every action
    (recognized or not) results in parking the story.  The rationale is
    truncated to 300 characters to keep the notification concise.
    """
    action = ruling.get("action", "unknown")
    rationale = ruling.get("rationale", "")[:300]
    if action in DEFERRED_ACTIONS:
        story["triage_deferred_action"] = action
        reason = f"triage ruled {action}, which is not implemented yet; parked for a human"
        _park(plan_name, story_key, story, reason)
        try:
            _notify_user(plan_name, f"{story_key} triage: {action} – {rationale}")
        except Exception:
            pass
        return "park_for_human"
    if action == "escalate_model":
        try:
            fallback = manifest.get("local_model_fallback")
            if (
                fallback
                and story.get("model") != fallback
                and not story.get("tried_fallback_model")
                and story.get("backend", "local") == "local"
            ):
                _escalate_to_local_fallback_model(
                    manifest, plan_name, story_key, manifest_path, fallback
                )
                return "escalate_model"
            if _auto_escalation_enabled() and not story.get("escalated"):
                _escalate_to_claude(
                    manifest, plan_name, story_key, manifest_path
                )
                return "escalate_model"
        except Exception as exc:
            reason = f"escalate_model ruled but ladder exhausted: {type(exc).__name__}"
            return _park(plan_name, story_key, story, reason)
        # ladder exhausted
        reason = f"escalate_model ruled but ladder exhausted: {rationale}"
        return _park(plan_name, story_key, story, reason)
    reason = f"unhandled ruling action '{action}': {rationale}"
    return _park(plan_name, story_key, story, reason)
    return _park(plan_name, story_key, story, reason)

def _apply_ruling_for_mode(plan_name, story_key, story, ruling, manifest, manifest_path) -> str:
    """Dispatch a ruling based on the current autonomy mode.

    * ``dry-run`` – the executor is disabled.  The story is left untouched and a
      notification is sent with the recommended action and rationale.
    * ``gated`` or ``full`` – the executor is enabled and the ruling is
      executed via :func:`execute_ruling`.
    """
    # Lazy import to avoid circular dependency
    from .server import PIPELINE_AUTONOMY

    if PIPELINE_AUTONOMY == "dry-run":
        # Notify but do not act
        action = ruling.get("action", "unknown")
        rationale = ruling.get("rationale", "")
        _notify_user(plan_name, f"{story_key} triage dry-run: {action} – {rationale}")
        return "dry-run"
    # Any other mode – execute the ruling
    return execute_ruling(plan_name, story_key, story, ruling, manifest, manifest_path)


# expose subprocess.run for monkeypatching
globals()["subprocess.run"] = subprocess.run

# ---------------------------------------------------------------------------
# Triage loop‑breaker constants and helpers
# ---------------------------------------------------------------------------
TRIAGE_MAX_ATTEMPTS = 2
TRIAGE_MAX_CREATED_STORIES = 3


def _coerce_int(value: object, default: int = 0) -> int:
    return value if isinstance(value, int) else default


def triage_allowed(story: dict) -> tuple[bool, str]:
    attempts = _coerce_int(story.get("triage_attempts", 0))
    if attempts < TRIAGE_MAX_ATTEMPTS:
        return True, ""
    return False, f"triage attempts ({attempts}) at or above cap ({TRIAGE_MAX_ATTEMPTS})"


def action_already_tried(story: dict, action: str) -> bool:
    actions = story.get("triage_actions", [])
    if not isinstance(actions, list):
        return False
    return action in actions


def record_triage_attempt(story: dict, action: str) -> None:
    attempts = _coerce_int(story.get("triage_attempts", 0))
    story["triage_attempts"] = attempts + 1
    actions = story.get("triage_actions")
    if not isinstance(actions, list):
        actions = []
        story["triage_actions"] = actions
    if action not in actions:
        actions.append(action)


def plan_triage_budget_exhausted(manifest: dict) -> bool:
    """Ceiling ships together with the loop breaker so the guard can never be forgotten later.
    The actions that create stories (split_story, repo_issue) are deliberately out of scope.
    """
    created = _coerce_int(manifest.get("triage_created_stories", 0))
    return created >= TRIAGE_MAX_CREATED_STORIES

__all__ = [
    "_auto_triage_enabled",
    "_current_suite_state",
    "_current_git_state",
    "TRIAGE_MAX_ATTEMPTS",
    "TRIAGE_MAX_CREATED_STORIES",
    "TRIAGE_MAX_PER_TICK",
    "run_triage_sweep",
    "action_already_tried",
    "collect_triage_evidence",
    "plan_triage_budget_exhausted",
    "record_triage_attempt",
    "execute_ruling",
    "_apply_ruling_for_mode",
    "rule_on_story",
    "triage_allowed",
    "triage_candidates",
    "DEFERRED_ACTIONS",
    ]
def run_triage_sweep(plan_name: str) -> dict:
    if not _auto_triage_enabled():
        return {"ok": True, "skipped": "disabled"}
    try:
        from .server import PLAN_DIR
        manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text())
        except Exception:
            return {"ok": True, "skipped": "no_manifest"}
        if manifest.get("paused"):
            return {"ok": True, "skipped": "plan_paused"}
        candidates = triage_candidates(manifest.get("stories", {}))
        if not candidates:
            return {"ok": True, "triaged": []}
        triaged_keys = []
        actions = {}
        changed = False
        for key in candidates[:TRIAGE_MAX_PER_TICK]:
            story = manifest["stories"][key]
            allowed, reason = triage_allowed(story)
            if not allowed:
                _park(plan_name, key, story, reason)
                changed = True
                continue
            if plan_triage_budget_exhausted(manifest):
                _park(plan_name, key, story, "plan triage budget exhausted")
                changed = True
                continue
            try:
                findings = classify_repo_health(story, story.get("worktree") or ".")
            except Exception:
                findings = []
            evidence = collect_triage_evidence(story.get("worktree", ""), story, findings)
            ruling = rule_on_story(plan_name, key, story, evidence)
            if action_already_tried(story, ruling["action"]):
                ruling = {"action": "park_for_human", "rationale": f"action {ruling['action']} already tried"}
            record_triage_attempt(story, ruling["action"])
            action = _apply_ruling_for_mode(plan_name, key, story, ruling, manifest, manifest_path)
            triaged_keys.append(key)
            actions[key] = ruling["action"]
            changed = True
        if changed:
            _atomic_write_json(manifest_path, manifest)
        return {"ok": True, "triaged": triaged_keys, "actions": actions}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}

def _auto_triage_enabled() -> bool:
    """Whether the scheduler's failure-triage sweep is enabled.

    Mirrors :func:`pipeline.escalation._auto_escalation_enabled`'s shape so an
    operator who knows one knob knows the other: a truthy value in
    ``("1", "true", "yes", "on")`` enables it, a falsy value in
    ``("0", "false", "no", "off")`` disables it, and any unrecognized value
    fails closed to disabled.

    The one deliberate difference from escalation: escalation falls back to a
    legacy rule (``PIPELINE_BACKEND_DISPATCH == "auto"``) when its flag is
    unset, because that behavior predates the flag and must be preserved.
    Triage has no legacy behavior to preserve, so per CLAUDE.md 'Secure by
    Design / Secure defaults' new features ship disabled and the operator opts
    in: unset means OFF. This knob is independent of both
    ``PIPELINE_BACKEND_DISPATCH`` (dispatch routing) and
    ``PIPELINE_AUTO_ESCALATE`` (the escalation ladder below triage).
    """
    override = os.environ.get("PIPELINE_AUTO_TRIAGE", "").strip().lower()
    if override in ("1", "true", "yes", "on"):
        return True
    if override in ("0", "false", "no", "off"):
        return False
    return False


def triage_candidates(stories: dict) -> list[str]:
    """Return the sorted list of story keys the triage sweep should consider.

    A story is a candidate if its ``status`` is ``"parked"`` or ``"failed"``,
    OR its ``step_cap_streak`` is at or above
    ``STEP_CAP_FALLBACK_THRESHOLD``. A missing or non‑integer
    ``step_cap_streak`` counts as 0, and a story dict with no ``status`` key
    is treated as having no status (never raises).

    ``interrupted`` is deliberately NOT a trigger by itself: it is already in
    the scheduler's ready list (``("todo", "interrupted", "changes_requested")``)
    and auto‑resumes on the next tick, so triaging it would fire continuously
    during normal operation. The step‑cap STREAK is the interrupted‑adjacent
    signal worth acting on, and it is a streak, not a single interrupt.

    The result is ``sorted(...)`` so the sweep is deterministic.
    """
    candidates = []
    for key, story in stories.items():
        status = story.get("status")
        if status in ("parked", "failed"):
            candidates.append(key)
            continue
        streak = story.get("step_cap_streak", 0)
        if not isinstance(streak, int):
            streak = 0
        if streak >= STEP_CAP_FALLBACK_THRESHOLD:
            candidates.append(key)
    return sorted(candidates)

# ---------------------------------------------------------------------------
# Helper: run the real test suite against the worktree's CURRENT HEAD
# ---------------------------------------------------------------------------

def _current_suite_state(worktree: str) -> str:
    """Run the REAL test suite against the worktree's CURRENT HEAD.

    This runs the full test suite against the worktree's current HEAD (unlike
    :func:`collect_failure_evidence`, which only reads cached/stale state) and
    is the fix for the gap found live on story 30e5f9fc-3681-40db-ae54-dd95387dd1e7,
    where a story sat parked with an already‑passing suite because nothing
    ever re‑checked. Never raises. Fails open to ``""`` (silence) on any problem - silence preserves today's behavior exactly, it never asserts something false.
    """
    if not worktree:
        return ""
    raw_timeout = os.environ.get("PIPELINE_TRIAGE_SUITE_TIMEOUT", "").strip()
    try:
        timeout_s = int(raw_timeout)
    except (TypeError, ValueError):
        timeout_s = 240
    if timeout_s <= 0:
        return ""
    try:
        test_dir, test_cmd = detect_test_command(Path(worktree))
        if not test_cmd:
            return ""
        needs_heavy = bool(test_cmd) and _is_heavy(test_cmd)
        if needs_heavy:
            with _heavy_lock():
                r = globals()["subprocess.run"](
                    test_cmd,
                    cwd=test_dir,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=timeout_s,
                )
        else:
            r = globals()["subprocess.run"](
                test_cmd,
                cwd=test_dir,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_s,
            )
        if r.returncode == 0:
            return "CURRENT STATE: full test suite PASSES at the worktree's current HEAD."
        if r.returncode == 5:
            return ""
        return f"CURRENT STATE: full test suite FAILS at the worktree's current HEAD (rc={r.returncode}):\n{(r.stdout + r.stderr)[-500:]}"
    except Exception:  # noqa: BLE001
        return ""

def _current_git_state(worktree: str, story: dict) -> str:
    """Return a live ``GIT STATE:`` section for the story's worktree.

    Mirrors :func:`_current_suite_state`'s fail-open shape: an empty or
    non-string ``worktree`` returns ``""`` immediately (no subprocess is
    spawned), and ANY exception — from the HEAD probe, the base-branch
    resolver or the new-commits helper — returns ``""``. The function never
    raises.

    It reports three facts the overlord must never have to guess from stale
    ``parked_reason`` text (21 of 34 historical parked stories were parked on
    a now-stale "no new commits vs master" reason that live git state
    contradicted):

      (1) the worktree's current HEAD sha;
      (2) whether the story's branch has NEW COMMITS vs the base branch,
          reusing :func:`pipeline.git_ops._worktree_has_new_commits` and
          resolving the base branch the way its existing callers do, via
          :func:`pipeline.server._default_branch`. If the base branch cannot
          be resolved, that fact is reported rather than guessing a name;
      (3) the story's ``pr_url``, when present.
    """
    if not isinstance(worktree, str) or not worktree:
        return ""
    try:
        r = globals()["subprocess.run"](
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        head_sha = (r.stdout or "").strip()
        lines = [f"GIT STATE: HEAD {head_sha}" if head_sha else "GIT STATE: HEAD unknown"]
        base = ""
        try:
            # Lazy import: pipeline.server imports this module at module
            # level, so a module-level import here would be circular.
            from .server import _default_branch

            base = _default_branch()
        except Exception:  # noqa: BLE001
            base = ""
        if base:
            has_new = _worktree_has_new_commits(
                Path(worktree), str(story.get("story_key") or story.get("key") or ""), base,
            )
            lines.append(
                f"BRANCH HAS NEW COMMITS vs {base}: yes"
                if has_new
                else f"BRANCH HAS NO NEW COMMITS vs {base} (0 new commits beyond base)"
            )
        else:
            lines.append("base branch unresolved; new-commits check skipped")
        pr_url = story.get("pr_url")
        if pr_url:
            lines.append(f"PR: {pr_url}")
        return "\n".join(lines)
    except Exception:  # noqa: BLE001
        return ""

# ---------------------------------------------------------------------------
# Main triage evidence collection
# ---------------------------------------------------------------------------

def collect_triage_evidence(worktree: str, story: dict, findings: list | None = None, limit: int = 8000) -> str:
    triage_question = f"TRIAGE QUESTION: this story is terminal (status={story.get('status', '?')}). Decide what to do about it."
    story_state = "\n".join(
        f"{k}: {story.get(k, '-') }"
        for k in [
            "status",
            "parked_reason",
            "backend",
            "model",
            "dispatched_model",
            "escalated",
            "risk",
            "persona",
            "dispatch_attempts",
            "rework_attempts",
            "review_inconclusive_count",
            "step_cap_streak",
            "merge_attempts",
            "triage_attempts",
            "triage_actions",
        ]
    )
    # Section (b2) – current suite state: a LIVE check against the
    # worktree's current HEAD, not cached/stale state (see
    # _current_suite_state's docstring for why this exists).
    current_state_section = ""
    try:
        current_state_section = _current_suite_state(worktree)
    except Exception:  # noqa: BLE001
        current_state_section = ""
    # Section (b3) – live git state: HEAD sha, new-commits-vs-base and the
    # story's pr_url. 21 of 34 historical parked stories were parked on a
    # now-stale "no new commits vs master" reason that live git state
    # contradicted, so the overlord gets the live facts alongside the
    # (possibly stale) parked_reason text.
    git_state_section = ""
    try:
        git_state_section = _current_git_state(worktree, story)
    except Exception:  # noqa: BLE001
        git_state_section = ""
    # Section (c) – repo‑health findings
    findings_section = ""
    if findings:
        try:
            findings_section = format_findings(findings)
        except Exception:  # noqa: BLE001
            findings_section = ""
    # Compute the length of the fixed sections
    fixed_parts = [triage_question, "STORY STATE:\n" + story_state]
    if current_state_section:
        fixed_parts.append(current_state_section)
    if git_state_section:
        fixed_parts.append(git_state_section)
    if findings_section:
        fixed_parts.append(findings_section)
    fixed_text = "\n".join(fixed_parts)
    remaining = max(0, limit - len(fixed_text))
    try:
        failure_evidence = collect_failure_evidence(worktree, story, limit=remaining)
    except Exception:  # noqa: BLE001
        failure_evidence = ""
    if failure_evidence:
        result = (fixed_text + "\n" + failure_evidence)[:limit]
    else:
        result = fixed_text[:limit]
    return result

# ---------------------------------------------------------------------------
# Rule on story – the core triage decision logic
# ---------------------------------------------------------------------------

def rule_on_story(plan_name: str, story_key: str, story: dict, evidence: str) -> dict:
    """Return a ruling for a terminal story.

    The function builds a prompt consisting of the policy text, a triage framing
    that states the story is terminal and asks what to do about it, the supplied
    evidence, and a closing instruction to rule now using the output contract
    exactly, including the ACTION field, and to choose the honest action even if
    it is one the pipeline cannot execute yet.

    The function is fail‑open: any exception during policy load, overlord call
    or parsing results in a default ruling that parks the story for a human
    and logs a warning containing only the exception type.
    """
    try:
        policy = _load_policy()
        # Build prompt
        prompt = f"{policy}\nTRIAGE QUESTION: this story is terminal (status={story.get('status', '?')}). Decide what to do about it.\nEVIDENCE:\n{evidence}\nPlease respond with the following format:\nRULING: ...\nTIER: ...\nRISK: ...\nRATIONALE: ...\nNOTIFY_USER: yes/no\nACTION: ..."
        # Call overlord
        raw = _invoke_overlord(prompt, plan_role_config=_plan_role_config(plan_name))
        # Parse ruling
        ruling = _parse_ruling(raw or "")
    except Exception as exc:  # pragma: no cover - fail open
        ruling = {
            "ruling": "",
            "tier": "",
            "risk": "",
            "rationale": f"triage failed open: {type(exc).__name__}",
            "notify_user": True,
            "action": "park_for_human",
            "failed_open": True,
        }
        logging.getLogger("pipeline").warning(type(exc).__name__)
    else:
        ruling["failed_open"] = False
    # Append decision record
    record = {
        "story_key": story_key,
        "question": "failure triage",
        **ruling,
        "decided_by": "overlord-triage",
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        _append_decision(plan_name, record)
    except Exception:  # pragma: no cover - persistence failure should not crash
        logging.getLogger("pipeline").warning("Failed to append decision for story %s", story_key)
    return ruling
