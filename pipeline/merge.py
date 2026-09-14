"""Merge adjudication helpers extracted from pipeline/server.py.

These functions were moved verbatim from the server module. Tests monkeypatch
module globals on ``pipeline.server`` (e.g. ``p._ci_status_once``,
``p.PIPELINE_AUTONOMY``), so every external name these functions read is
resolved via a lazy ``from .server import ...`` inside the function body at
call time - the same circular-avoidance pattern used by pipeline/rebase.py and
pipeline/pr.py. This keeps the re-exported bindings on ``pipeline.server`` the
single source of truth that monkeypatches land on.
"""

import fcntl
import logging
import os
import re
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_lock_state = {}


def _merge_gate_ci_status(branch: str, *, sha: str) -> dict[str, str]:
    """Single-poll CI status for the merge adjudication phase.

    Always uses the non-blocking ``_ci_status_once`` (so a pending result
    yields the tick instead of sleeping). ``_ci_status_once`` performs exactly
    one query and returns immediately; the blocking ``_ci_status`` poller is no
    longer used by the merge gate, so the S5 non-blocking CI-pending behaviour
    is the production default rather than a test-only code path.
    """
    from .server import _ci_status_once

    return _ci_status_once(branch, sha=sha)


def _rebase_and_push_for_merge(plan_name, key, branch, worktree) -> tuple[str, str]:
    from .pr import _resolve_story_branch
    from .server import (
        REPO_ROOT,
        _default_branch,
        _notify_user,
        _rebase_onto_master,
        _store,
    )

    # Resolve the worktree's ACTUAL HEAD branch before touching git. A rework
    # round can leave the worktree checked out on an alias branch
    # agent/<key>-<suffix> (e.g. agent/s10-2); _open_pr/_merge_pr already
    # resolve it, and this gate must operate on the SAME branch or the story
    # can never merge: pushing the convention name here fails with
    # "src refspec agent/<key> does not match any" once a prior _merge_pr has
    # squash-merged and `git branch -D`-ed it, or - if a stale local
    # convention branch survived - pushes that stale code while the CI poll
    # queries the worktree-HEAD SHA, a SHA never pushed to the polled branch.
    # The caller-passed ``branch`` (the pre-alias convention name from
    # advance.py) is kept in the signature for compatibility with the existing
    # test fakes but must not be trusted for push/CI identity.
    # Resolve only when there is a worktree to probe: a missing/anomalous
    # worktree must degrade to the caller's convention branch without paying
    # a subprocess (the rebase helper below applies the same guard).
    resolved = branch
    if worktree and Path(worktree).is_dir():
        resolved = _resolve_story_branch(worktree, key)

    rb = _rebase_onto_master(worktree, resolved)
    if rb.get("auto_resolved"):
        # W4L-04: stamp the story's correlation_id onto this merge-gate
        # notice. This helper receives only plan+story key (no story dict),
        # so the id is looked up from the manifest the same way neighboring
        # code reads story fields; older manifests without the field keep
        # the legacy record shape (absent key, not null).
        _story = _store.get_manifest(plan_name)["stories"].get(key) or {}
        _cid = _story.get("correlation_id")
        _notify_user(
            plan_name,
            f"{key} rebase auto-resolved an "
            f"additive-import conflict against "
            f"origin/{_default_branch()}.",
            **({"correlation_id": _cid} if _cid else {}),
            event="rebase_auto_resolved",
        )
    if not rb["ok"]:
        return (
            f"rebase: {rb['error']}",
            "",
        )
    pushed_sha = ""
    if Path(worktree).is_dir():
        push = subprocess.run(
            ["git", "push", "--force-with-lease", "origin", resolved],
            check=False,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        if push.returncode != 0:
            return (
                f"push: {(push.stderr or push.stdout).strip()[:200]}",
                "",
            )
        rev = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=False,
            cwd=worktree,
            capture_output=True,
            text=True,
        )
        pushed_sha = rev.stdout.strip()
    return ("", pushed_sha)


@contextmanager
def _try_acquire_git_lock(repo_root: Path):
    """Non-blocking advisory flock guarding .git-mutating dispatch.

    Reentrant per call stack: flock() is scoped to the open file
    description, not the process, so a nested call must recognize the
    lock is already held by an ancestor frame rather than re-flocking
    (which would fail against its own outer acquisition).
    """
    lock_path = repo_root / ".git" / ".pipeline-git-lock"
    key = str(lock_path)
    if _lock_state.get(key, 0) > 0:
        _lock_state[key] += 1
        try:
            yield True
        finally:
            _lock_state[key] -= 1
        return
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    except OSError:
        yield True
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_path.write_text(str(repo_root))
        except OSError:
            yield False
            return
        _lock_state[key] = 1
        try:
            yield True
        finally:
            _lock_state[key] -= 1
            if _lock_state[key] <= 0:
                del _lock_state[key]
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _parse_merge_ruling(raw: str) -> dict[str, str] | None:
    """Parse the overlord's merge ruling from its reply.

    Expected format (one line each, order-insensitive)::

        RULING: proceed|park
        RATIONALE: <one line>

    Returns ``{"ruling": ..., "rationale": ...}`` or ``None`` when the reply
    does not carry a parseable RULING line (callers fail closed to the hold).
    """
    match = re.search(r"^\s*RULING:\s*(proceed|park)\s*$", raw or "", re.MULTILINE | re.IGNORECASE)
    if match is None:
        return None
    rationale_match = re.search(r"^\s*RATIONALE:\s*(.+)$", raw or "", re.MULTILINE | re.IGNORECASE)
    return {
        "ruling": match.group(1).lower(),
        "rationale": (rationale_match.group(1).strip() if rationale_match else ""),
    }


def _adjudicate_high_risk_merge(story: dict[str, Any]) -> dict[str, str]:
    """Ask the overlord to rule on a high-risk merge (full autonomy only).

    Sends the merge context - PR checks state, review verdict,
    security-review verdict, risk and story summary - with an instruction to
    rule ``proceed`` or ``park`` with a rationale. The ruling is recorded in
    the decisions log (``decided_by: 'overlord'``) with the story's prior
    state captured before any mutation. Fail closed: an overlord failure or
    an unparseable reply parks with the standing high-risk hold reason.
    """
    from .overlord import _invoke_overlord
    from .persistence import _append_decision, _plan_role_config

    hold = {"action": "park", "reason": "high risk held for human review"}
    plan_name = story.get("plan") or ""
    # Prior state captured before any mutation: the caller parks/merges only
    # after this returns, so the story dict still holds its pre-gate state.
    prior_status = story.get("status")
    prior_parked_reason = story.get("parked_reason")

    prompt = (
        "MERGE ADJUDICATION: this story's merge gate is high-risk and the "
        "pipeline is running in full autonomy, so you rule where a human "
        "otherwise would. Rule 'proceed' to let the merge go ahead or 'park' "
        "to hold it for human review, with a rationale.\n"
        f"STORY: {story.get('key') or '?'}\n"
        f"SUMMARY: {story.get('summary') or '(none)'}\n"
        f"RISK: {story.get('risk') or 'unknown'}\n"
        f"REVIEW VERDICT: {story.get('review_verdict') or '(none)'}\n"
        f"SECURITY-REVIEW VERDICT: "
        f"{story.get('security_review_verdict') or '(none)'}\n"
        f"PR CHECKS: {story.get('pr_checks') or '(none)'}\n"
        "Respond in this exact format:\n"
        "RULING: proceed|park\n"
        "RATIONALE: <one line>\n"
    )
    try:
        raw = _invoke_overlord(prompt, plan_role_config=_plan_role_config(plan_name))
        parsed = _parse_merge_ruling(raw or "")
    except Exception as exc:  # noqa: BLE001 - overlord failure fails closed
        parsed = None
        logging.getLogger("pipeline").warning(
            "overlord merge adjudication failed for %s: %s",
            story.get("key"),
            type(exc).__name__,
        )
    if parsed is None:
        record = {
            "story_key": story.get("key"),
            "question": "high-risk merge adjudication",
            "ruling": "",
            "rationale": "unparseable overlord reply; failed closed to the hold",
            "prior_status": prior_status,
            "prior_parked_reason": prior_parked_reason,
            "decided_by": "overlord",
            "decided_at": datetime.now(timezone.utc).isoformat(),
        }
    else:
        record = {
            "story_key": story.get("key"),
            "question": "high-risk merge adjudication",
            "ruling": parsed["ruling"],
            "rationale": parsed["rationale"],
            "prior_status": prior_status,
            "prior_parked_reason": prior_parked_reason,
            "decided_by": "overlord",
            "decided_at": datetime.now(timezone.utc).isoformat(),
        }
    try:
        _append_decision(plan_name, record)
    except Exception:  # noqa: BLE001 - audit write must never break the gate
        logging.getLogger("pipeline").warning(
            "failed to append merge adjudication record for %s", story.get("key")
        )
    if parsed is not None and parsed["ruling"] == "proceed":
        return {"action": "merge", "reason": "overlord ruled proceed"}
    return hold


def _merge_decision(story: dict[str, Any]) -> dict[str, str]:
    """Pure decision: may a reviewed (pr_open) story merge unattended?

    Honors PIPELINE_AUTONOMY and PIPELINE_RISK_THRESHOLD. In dry-run and
    gated a high-risk story is always parked for human review; in full
    autonomy the hold becomes an overlord adjudication whose ruling is
    recorded in the decisions log and followed (fail closed to the hold).
    """
    from .server import _RISK_ORDER, PIPELINE_AUTONOMY, PIPELINE_RISK_THRESHOLD

    if story.get("review_verdict") != "APPROVE":
        return {"action": "park", "reason": "not approved"}
    if PIPELINE_AUTONOMY == "dry-run":
        return {"action": "park", "reason": "dry-run"}

    risk_rank = _RISK_ORDER.get(
        (story.get("risk") or "low").lower(), _RISK_ORDER["high"]
    )
    if risk_rank >= _RISK_ORDER["high"]:
        if PIPELINE_AUTONOMY == "full":
            return _adjudicate_high_risk_merge(story)
        return {"action": "park", "reason": "high risk held for human review"}
    if PIPELINE_AUTONOMY == "full":
        return {"action": "merge", "reason": "autonomy=full"}

    threshold = _RISK_ORDER.get(PIPELINE_RISK_THRESHOLD, _RISK_ORDER["low"])
    if risk_rank <= threshold:
        return {
            "action": "merge",
            "reason": f"risk <= threshold {PIPELINE_RISK_THRESHOLD}",
        }
    return {
        "action": "park",
        "reason": f"risk above threshold {PIPELINE_RISK_THRESHOLD}",
    }


def _approve_merge_impl(plan_name: str, story_key: str) -> dict[str, Any]:
    from .pr import _resolve_story_branch
    from .server import (
        REPO_ROOT,
        _atomic_write_json,
        _ci_rerun,
        _ci_status,
        _default_branch,
        _mark_plane_done,
        _mcp_restart_notice,
        _mcp_self_source_touched,
        _merge_pr,
        _notify_user,
        _rebase_onto_master,
        _reverify_acceptance,
        _reverify_build,
        _scoped_repo_root,
        _store,
        _validate_key,
    )

    _validate_key(plan_name)
    _validate_key(story_key)
    manifest_path = _store.manifest_path(plan_name)

    with _store.transaction(plan_name) as acquired:
        if not acquired:
            return {
                "ok": False,
                "error": "plan busy (scheduler tick in progress); retry",
                "retriable": True,
            }
        # Re-read the manifest from disk INSIDE the lock so we merge against
        # the freshest on-disk state, not a pre-lock stale copy. A scheduler
        # tick may have changed the story's status or verdict while we waited
        # to acquire the lock.
        manifest = _store.get_manifest(plan_name)
        story = manifest["stories"].get(story_key)
        if not story:
            return {"ok": False, "error": f"No such story {story_key}"}
        if story["status"] not in ("parked", "pr_open"):
            return {
                "ok": False,
                "error": f"Story is {story['status']}, not parked/pr_open",
            }
        if story.get("review_verdict") != "APPROVE":
            return {"ok": False, "error": "Story was never reviewer-approved"}

        try:
            with _scoped_repo_root(plan_name):
                # Mode 9 gate applies here too: even an explicit human merge must
                # not land a conflicting or CI-red PR. Disable via
                # PIPELINE_MERGE_CI_GATE=0 only if you intentionally accept that.
                worktree = story.get("worktree", "")
                # Resolve the worktree's ACTUAL HEAD branch (a rework round can
                # leave it on an alias agent/<key>-<suffix>) so rebase -> push
                # -> CI -> _merge_pr all operate on the one branch _merge_pr
                # merges. The hardcoded convention name previously pushed a
                # branch a prior _merge_pr had already deleted ("src refspec
                # does not match any") or a stale twin, and CI-polled a SHA
                # that was never pushed to it. Resolve only when there is a
                # worktree to probe; a missing/anomalous worktree degrades to
                # the convention branch without paying a subprocess.
                branch = f"agent/{story_key.lower()}"
                if worktree and Path(worktree).is_dir():
                    branch = _resolve_story_branch(worktree, story_key)
                rb = _rebase_onto_master(worktree, branch)
                if rb.get("auto_resolved"):
                    # W4L-04: stamp the story's persisted correlation_id
                    # (omitted entirely for older manifests - absent, not
                    # null).
                    _cid = story.get("correlation_id")
                    _notify_user(
                        plan_name,
                        f"{story_key} rebase auto-resolved an "
                        f"additive-import conflict against "
                        f"origin/{_default_branch()}.",
                        **({"correlation_id": _cid} if _cid else {}),
                        event="rebase_auto_resolved",
                    )
                if not rb["ok"]:
                    return {
                        "ok": False,
                        "error": f"rebase failed: {rb['error']}",
                        "story_key": story_key,
                    }
                pushed_sha = ""
                if Path(worktree).is_dir():
                    push = subprocess.run(
                        ["git", "push", "--force-with-lease", "origin", branch],
                        check=False,
                        cwd=REPO_ROOT,
                        capture_output=True,
                        text=True,
                    )
                    if push.returncode != 0:
                        return {
                            "ok": False,
                            "error": f"push failed: {(push.stderr or push.stdout).strip()[:200]}",
                            "story_key": story_key,
                        }
                    # Mode 26: pin to the exact commit that was just pushed,
                    # not the branch name - see the matching comment in
                    # advance_pipeline's own merge adjudication above.
                    rev = subprocess.run(
                        ["git", "rev-parse", "HEAD"],
                        check=False,
                        cwd=worktree,
                        capture_output=True,
                        text=True,
                    )
                    pushed_sha = rev.stdout.strip()
                ci = _ci_status(branch, sha=pushed_sha)
                if ci["state"] == "cancelled" and not story.get("ci_rerun_attempted"):
                    # Same one-shot auto-rerun as the scheduler's merge gate:
                    # a queue-delay cancellation carries no code-quality
                    # signal, so give it one automatic retry before failing.
                    story["ci_rerun_attempted"] = True
                    _ci_rerun(pushed_sha)
                    ci = _ci_status(branch, sha=pushed_sha)
                if ci["state"] in ("fail", "cancelled"):
                    return {
                        "ok": False,
                        "error": f"CI failing: {ci['error']}",
                        "story_key": story_key,
                    }
                if ci["state"] == "pending":
                    return {
                        "ok": False,
                        "error": f"CI still pending: {ci['error']}",
                        "story_key": story_key,
                    }
                acc = _reverify_acceptance(story, worktree, story_key)
                if acc["state"] == "fail":
                    return {
                        "ok": False,
                        "error": f"acceptance reverify fail: {acc['error']}",
                        "story_key": story_key,
                    }
                build = _reverify_build(worktree)
                if build["state"] == "fail":
                    return {
                        "ok": False,
                        "error": f"build reverify fail: {build['error']}",
                        "story_key": story_key,
                    }
                # _merge_pr removes the worktree and deletes the branch, so the
                # self-source diff must be taken BEFORE the merge, not after.
                mcp_touched = _mcp_self_source_touched(
                    worktree, f"origin/{_default_branch()}"
                )
                _merge_pr(story.get("worktree", ""), story_key)
        except Exception as e:  # noqa: BLE001 (surface the gh/git failure to the human, don't raise)
            return {"ok": False, "error": str(e), "story_key": story_key}
        # Final write INSIDE the lock, using the manifest re-read inside the
        # lock (not a pre-lock copy). Clear parked_reason on leaving 'parked'.
        story["status"] = "done"
        story.pop("parked_reason", None)
        story.pop("ci_rerun_attempted", None)
        _atomic_write_json(manifest_path, manifest)

        from .plan_completion import notify_if_plan_completed

        try:
            notify_if_plan_completed(plan_name, manifest)
        except Exception:
            logging.getLogger(__name__).exception(
                "notify_if_plan_completed failed for %s", plan_name
            )
        _maybe_record_retro(plan_name, manifest)
    _mark_plane_done(story_key, plan_name)
    if mcp_touched:
        _notify_user(plan_name, _mcp_restart_notice(mcp_touched))
    return {"ok": True, "story_key": story_key, "status": "done"}


def _maybe_record_retro(plan_name: str, manifest: dict) -> None:
    from .server import PIPELINE_SELF_REPO_ROOT, _record_retro_pending

    stories = manifest.get("stories", {})
    if not stories or not all(s.get("status") == "done" for s in stories.values()):
        return
    if manifest.get("repo_root") != str(PIPELINE_SELF_REPO_ROOT):
        return
    _record_retro_pending(plan_name, len(stories))


def _set_plan_paused(plan_name: str, paused: bool) -> dict[str, Any]:
    from .server import _store

    with _store.transaction(plan_name) as acquired:
        if not acquired:
            return {
                "ok": True,
                "skipped": "locked",
                "reason": "an advance_pipeline tick is already running for this plan",
            }
        if not _store.manifest_path(plan_name).exists():
            return {"ok": False, "error": f"No manifest for {plan_name}"}
        manifest = _store.get_manifest(plan_name)
        manifest["paused"] = paused
        _store.save_manifest(plan_name, manifest)
        return {"ok": True, "plan_name": plan_name, "paused": paused}