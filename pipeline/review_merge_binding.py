"""Refuse to merge a story whose approved review does not cover the branch head.

``review_verdict == "APPROVE"`` says a reviewer approved *a* commit; it does
not say *which* one. ``pipeline.review_verdict_binding.bind_verdict`` records
the reviewed HEAD as ``review_verdict_sha`` before the reviewer runs; this
module is the consumer that enforces it at merge time, so a commit pushed
after the review (agent rework, a human push) can no longer ride the earlier
approval into the default branch.

Two pieces:

``check(story, branch_head_sha)``
    Pure, deny-by-default predicate. Allowed only when the verdict is
    ``APPROVE`` *and* ``review_verdict_sha`` is a non-empty ``str`` equal to
    ``branch_head_sha``. Never raises and never mutates ``story``, so a denied
    call cannot corrupt the binding for the next caller.

``branch_head_sha(branch)``
    The one I/O helper the call sites need: the story branch tip on the
    remote, taken AFTER a fetch and BEFORE the merge-time rebase. It must be
    the remote tip, not the post-rebase sha - the pipeline's own merge-gate
    rebase rewrites the branch, and comparing against the rewritten commit
    would deny every retry tick of known-good work (measured live on
    obs-metrics-rename-and-otel-config story 064def7e).

Expected pipeline post-review commits are handled by re-binding, never by
loosening this check: after its own rebase+force-push the merge gate calls
``bind_verdict`` with the new pushed sha, so the next tick's remote tip
matches. The check always runs first, so a human push is refused before any
re-bind could bless it.
"""

import logging
import subprocess

logger = logging.getLogger(__name__)

#: The instruction appended to every denial: an approval bound to a different
#: commit is only re-earned by reviewing the branch head that will be merged.
RERUN = "re-run review_story"

#: Placeholder reason fragment for a sha that is missing entirely.
_ABSENT = "none"


def _short(sha: object) -> str:
    """First 12 characters of a sha, or ``"none"`` when there is not one.

    Deliberately tolerant: a denial reason must be printable for a missing,
    ``None`` or non-string value without raising.
    """
    return sha[:12] if isinstance(sha, str) and sha else _ABSENT


def check(story: object, branch_head_sha: object) -> dict:
    """Does this story's approval cover ``branch_head_sha``?

    Returns ``{"ok": bool, "reason": str}``. Denies by default: anything other
    than an ``APPROVE`` verdict bound to exactly this head - a missing key, a
    non-string, an empty sha, a mismatch, or an unavailable branch head - is
    not ok. Pure: reads nothing but its arguments and mutates neither.
    """
    try:
        fields = story if isinstance(story, dict) else {}
        verdict = fields.get("review_verdict")
        bound = fields.get("review_verdict_sha")
        head = branch_head_sha

        if verdict != "APPROVE":
            return {
                "ok": False,
                "reason": (
                    f"review verdict is {verdict!r}, not 'APPROVE'; {RERUN}"
                ),
            }
        if not isinstance(bound, str) or not bound:
            return {
                "ok": False,
                "reason": (
                    "the approval is not bound to a reviewed commit "
                    f"(review_verdict_sha={bound!r}) so it cannot cover "
                    f"branch head {_short(head)}; {RERUN}"
                ),
            }
        if not isinstance(head, str) or not head:
            return {
                "ok": False,
                "reason": (
                    f"branch head unavailable (got {head!r}); the approval "
                    f"covers {_short(bound)} and cannot be matched; {RERUN}"
                ),
            }
        if bound != head:
            return {
                "ok": False,
                "reason": (
                    f"approved {_short(bound)} but the branch head is "
                    f"{_short(head)}; the review does not cover the commit "
                    f"being merged; {RERUN}"
                ),
            }
        return {
            "ok": True,
            "reason": f"approval covers branch head {_short(head)}",
        }
    except Exception:
        logger.exception("review/merge binding check failed; denying")
        return {
            "ok": False,
            "reason": f"could not verify the approval binding; {RERUN}",
        }


def branch_head_sha(branch: str, *, repo_root: object = None) -> str | None:
    """The remote tip of ``branch`` after a fetch, or ``None``.

    Never raises. ``None`` (missing branch, unreachable remote, absent git)
    makes ``check`` deny, which is the safe direction: no merge happens and
    the caller can retry. A stale local remote-tracking ref is never used in
    place of a failed fetch - that could allow a head the reviewer never saw.
    """
    if not isinstance(branch, str) or not branch:
        return None
    cwd = str(repo_root) if repo_root else _default_repo_root()
    try:
        fetched = _git(["fetch", "origin"], cwd)
        if fetched is None or fetched.returncode != 0:
            logger.warning(
                "cannot fetch origin to verify the review binding for %s; denying",
                branch,
            )
            return None
        rev = _git(["rev-parse", "--verify", f"refs/remotes/origin/{branch}"], cwd)
        if rev is None or rev.returncode != 0:
            return None
        sha = (rev.stdout or "").strip()
        return sha or None
    except Exception:
        logger.exception("branch head probe failed for %s; denying", branch)
        return None


def _default_repo_root() -> str | None:
    """The repo to probe, read at CALL time.

    ``merge``/``advance_merge`` run inside ``_scoped_repo_root``, which rebinds
    ``pipeline.server.REPO_ROOT`` for the duration of the block. Resolving it
    here (never at import, never cached) is what makes the probe follow that
    rebinding instead of the pipeline's own repo - the exact bug pinned by
    test_approve_merge_scoped_repo_root_push_cwd.
    """
    try:
        from .server import REPO_ROOT

        return str(REPO_ROOT)
    except Exception:
        logger.exception("cannot resolve REPO_ROOT; denying")
        return None


def refuse(
    plan_name: str, story_key: str, story: object, branch_head: object, result: dict
) -> dict:
    """Audit a denial with identifiers only, and build the caller's refusal.

    The record carries the plan, the story and the two short shas - never the
    diff, the review feedback or any other story text.
    """
    fields = story if isinstance(story, dict) else {}
    bound = fields.get("review_verdict_sha")
    try:
        from .audit import record_audit

        record_audit(
            plan_name,
            action="merge.review_binding_denied",
            outcome="denied",
            target={"plan": plan_name, "story": story_key},
            detail={
                "plan": plan_name,
                "story": story_key,
                "review_verdict_sha": _short(bound),
                "branch_head_sha": _short(branch_head),
            },
        )
    except Exception:
        logger.exception(
            "could not audit merge.review_binding_denied for %s/%s",
            plan_name,
            story_key,
        )
    reason = result.get("reason") if isinstance(result, dict) else None
    return {
        "ok": False,
        "error": reason or f"the approval does not cover the branch head; {RERUN}",
        "story_key": story_key,
    }


def deny(
    plan_name: str,
    story_key: str,
    story: dict,
    branch_head: object,
    result: dict,
    summary: dict,
) -> None:
    """Scheduler-side refusal: audit, park the story, tell the human.

    Parking (not the merge gate's retry-then-fail loop) is the right terminal
    state for a stale approval: nothing the scheduler does on a retry can
    re-earn the review, so retrying would only burn ``merge_attempts`` and
    then mark a known-good branch failed. ``review_story`` is the way back.
    """
    refusal = refuse(plan_name, story_key, story, branch_head, result)
    _rv_job = story.pop("merge_reverify", None)
    if _rv_job:
        try:
            from .advance_merge import cancel_merge_reverify

            cancel_merge_reverify(_rv_job)
        except Exception:
            logger.exception("could not cancel the merge re-verify for %s", story_key)
    story["status"] = "parked"
    story["parked_reason"] = refusal["error"]
    story["merge_park_evidence"] = {"pr_checks": story.get("pr_checks")}
    try:
        from datetime import UTC, datetime

        story["merge_parked_at"] = datetime.now(UTC).isoformat()
        from .advance_merge import _notify_user

        _notify_user(
            plan_name,
            f"{story_key} parked: {refusal['error']}"
            + (f" — {story['pr_url']}" if story.get("pr_url") else ""),
            event="story_parked",
            story_key=story_key,
        )
    except Exception:
        logger.exception("could not notify the merge refusal for %s", story_key)
    summary.setdefault("parked", []).append(story_key)


def rebind_after_pipeline_push(story: object, pushed_sha: object) -> bool:
    """Re-bind an APPROVE verdict to the head the pipeline just pushed.

    The merge gate's own rebase and force-push are the one expected way the
    pushed head moves after the review, so the verdict is re-bound HERE - at
    the pipeline's commit point - rather than by loosening ``check`` at merge
    time. Returns True when a re-bind happened.

    Only an existing ``APPROVE`` verdict is re-bound, and only to a non-empty
    sha: this can never manufacture approval for a story that has none.
    """
    fields = story if isinstance(story, dict) else None
    if fields is None or fields.get("review_verdict") != "APPROVE":
        return False
    if not isinstance(pushed_sha, str) or not pushed_sha:
        return False
    if fields.get("review_verdict_sha") == pushed_sha:
        return False
    try:
        from datetime import UTC, datetime

        from .review_verdict_binding import bind_verdict

        bind_verdict(fields, "APPROVE", pushed_sha, datetime.now(UTC))
    except Exception:
        logger.exception("could not re-bind the review verdict after a pipeline push")
        return False
    logger.info(
        "re-bound the review verdict to %s after the merge-gate push", _short(pushed_sha)
    )
    return True


def _git(args: list, cwd: str | None) -> subprocess.CompletedProcess | None:
    """Run one read-only git command, never raising."""
    try:
        return subprocess.run(
            ["git", *args],
            check=False,
            cwd=cwd,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("git %s failed: %s", args[0], e)
        return None


__all__ = [
    "RERUN",
    "branch_head_sha",
    "check",
    "rebind_after_pipeline_push",
    "refuse",
]
