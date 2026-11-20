"""Repo-local checks run before the overlord rules on a high-risk merge.

In full autonomy the overlord rules on a high-risk merge with GitHub PR checks
as its only test evidence, which a repo without CI never has. This module runs
the repo's own tests, build and lint on the exact commit BEFORE that ruling -
the same detached job the post-ruling re-verify uses (``pipeline.merge_reverify``),
with lint added - and shows the result to the overlord as a ``LOCAL CHECKS:``
line. A red result parks the merge deterministically, without asking.

The names this module reads from ``pipeline.advance`` / ``pipeline.advance_merge``
are resolved at call time through ``_ModuleRef``, so tests that patch those
modules keep reaching the code here.
"""

from __future__ import annotations

import math
import os
import subprocess
from pathlib import Path
from typing import Any

from . import merge_reverify
from .ci import settings_for_plan
from .host_state import build_host_state
from .module_ref import _ModuleRef

# Default wall-clock budget for one detached check, overridable with
# PIPELINE_MERGE_REVERIFY_TIMEOUT_SECONDS. Re-exported by pipeline.advance_merge.
MERGE_REVERIFY_TIMEOUT_DEFAULT = 1800.0

_reverify_acceptance = _ModuleRef("pipeline.advance", "_reverify_acceptance")
_reverify_build = _ModuleRef("pipeline.advance", "_reverify_build")
_run_lint_gate = _ModuleRef("pipeline.server", "_run_lint_gate")
PIPELINE_AUTONOMY = _ModuleRef("pipeline.advance", "PIPELINE_AUTONOMY")
start_merge_reverify = _ModuleRef("pipeline.advance_merge", "start_merge_reverify")
collect_merge_reverify = _ModuleRef("pipeline.advance_merge", "collect_merge_reverify")
cancel_merge_reverify = _ModuleRef("pipeline.advance_merge", "cancel_merge_reverify")

_JOB_KIND = "merge_local_checks"
_VERDICT_STATES = frozenset({"pass", "fail", "error"})


def reverify_timeout() -> float:
    """Wall-clock budget for one detached check, in seconds.

    The default unless ``PIPELINE_MERGE_REVERIFY_TIMEOUT_SECONDS`` is a finite
    positive number: NaN fails every comparison and inf would hand subprocess an
    unbounded budget.
    """
    timeout = MERGE_REVERIFY_TIMEOUT_DEFAULT
    raw = os.environ.get("PIPELINE_MERGE_REVERIFY_TIMEOUT_SECONDS")
    if raw is not None:
        try:
            value = float(raw)
        except ValueError:
            value = float("nan")
        if math.isfinite(value) and value > 0:
            timeout = value
    return timeout


def adjudication_due(story: dict[str, Any], plan_name: str | None) -> bool:
    """True exactly when ``merge._merge_decision`` would ask the overlord.

    Mirrors that function's checks, in its order, with the same helpers: the
    review must be APPROVE, autonomy must not be dry-run, auto-merge must be on,
    and the risk must rank high (an unrecognised risk counts as high), in full
    autonomy. The parity is pinned by a test; change both together.
    """
    from .autonomy_settings import effective_auto_merge, effective_autonomy
    from .config import PIPELINE_AUTO_MERGE
    from .server import _RISK_ORDER

    context = settings_for_plan(plan_name) if plan_name else None
    autonomy = effective_autonomy(PIPELINE_AUTONOMY._value(), context)
    auto_merge = effective_auto_merge(PIPELINE_AUTO_MERGE, context)

    if story.get("review_verdict") != "APPROVE":
        return False
    if autonomy == "dry-run":
        return False
    if not auto_merge:
        return False
    risk_rank = _RISK_ORDER.get(
        (story.get("risk") or "low").lower(), _RISK_ORDER["high"]
    )
    return risk_rank >= _RISK_ORDER["high"] and autonomy == "full"


def local_checks_gate(
    plan_name: str, key: str, story: dict[str, Any], summary: dict[str, Any]
) -> dict[str, Any] | None:
    """Run or collect the local checks for the branch head before a ruling.

    Returns ``None`` when the overlord should be asked now (the checks passed,
    or are unavailable - evidence the ruling must see), ``{"action": "park", ...}``
    when they are red (no ruling is asked), and ``{"action": "wait"}`` while a
    detached job runs. The result is recorded on ``story["local_checks"]``
    keyed by the commit it graded.
    """
    head = _worktree_head(story)
    if head is None:
        story["local_checks"] = _record(None, "error", {}, "worktree unavailable")
        return None
    if _result_for(story, head) is None:
        if merge_reverify.detached_enabled():
            return _poll(plan_name, key, story, head, summary)
        story["local_checks"] = _inline(plan_name, key, story, head)
    return _verdict(story, head)


def render_local_checks_line(story: dict[str, Any]) -> str:
    """The ``LOCAL CHECKS:`` evidence line for the overlord prompt."""
    result = story.get("local_checks")
    if not isinstance(result, dict):
        return "LOCAL CHECKS: not run"
    state = result.get("state")
    if state == "error":
        return f"LOCAL CHECKS: unavailable ({result.get('error') or 'unknown'})"
    if state not in ("pass", "fail"):
        return "LOCAL CHECKS: not run"
    checks = result.get("checks") or {}
    sha = str(result.get("sha") or "")[:7]
    return (
        f"LOCAL CHECKS: tests {checks.get('tests', 'skipped')}"
        f" / lint {checks.get('lint', 'skipped')}"
        f" / build {checks.get('build', 'skipped')} @ {sha}"
    )


def _worktree_head(story: dict[str, Any]) -> str | None:
    worktree = story.get("worktree") or ""
    if not worktree or not Path(worktree).is_dir():
        return None
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() or None


def _record(sha: str | None, state: str, checks: dict, error: str) -> dict[str, Any]:
    return {"sha": sha, "state": state, "checks": checks, "error": error}


def _result_for(story: dict[str, Any], head: str) -> dict[str, Any] | None:
    """The stored result when it graded *head*, else None."""
    stored = story.get("local_checks")
    if not isinstance(stored, dict) or stored.get("sha") != head:
        return None
    if stored.get("state") not in _VERDICT_STATES:
        return None
    return stored


def _verdict(story: dict[str, Any], head: str) -> dict[str, Any] | None:
    result = _result_for(story, head)
    if result is not None and result.get("state") == "fail":
        return {
            "action": "park",
            "reason": f"high risk held: local checks red @ {head[:7]}",
        }
    return None


def _poll(
    plan_name: str,
    key: str,
    story: dict[str, Any],
    head: str,
    summary: dict[str, Any],
) -> dict[str, Any] | None:
    job = story.get("merge_local_checks_job")
    if job and job.get("sha") != head:
        # The branch moved since this job started: its verdict grades a commit
        # that is no longer the one being ruled on.
        cancel_merge_reverify(job)
        story.pop("merge_local_checks_job", None)
        job = None
    if job is None:
        try:
            started = start_merge_reverify(
                plan_name,
                key,
                story["worktree"],
                build_host_state().job_state_root(),
                kind=_JOB_KIND,
                with_lint=True,
            )
        except (ValueError, OSError) as exc:
            # Class name only: exception text can carry a path.
            story["local_checks"] = _record(
                head,
                "error",
                {},
                "local checks could not start: " + type(exc).__name__,
            )
            return None
        story["merge_local_checks_job"] = {**started, "sha": head}
        summary.setdefault("reverify_pending", []).append(key)
        return {"action": "wait"}

    res = collect_merge_reverify(job, reverify_timeout())
    if res is None:
        summary.setdefault("reverify_pending", []).append(key)
        return {"action": "wait"}
    story.pop("merge_local_checks_job", None)
    story["local_checks"] = _from_job(res, head)
    return _verdict(story, head)


def _from_job(res: dict[str, Any], head: str) -> dict[str, Any]:
    """Record a finished job's result against the commit it was started for.

    A harness error keeps the job's own error text. A result that does not name
    *head* (missing or another SHA) can never authorise a pass: it is an error.
    """
    if res.get("state") == "error":
        return _record(head, "error", {}, str(res.get("error") or "unknown"))
    if res.get("sha") != head:
        return _record(head, "error", {}, "local checks graded a different commit")
    checks = res.get("checks") if isinstance(res.get("checks"), dict) else {}
    return _record(head, str(res.get("state")), checks, str(res.get("error") or ""))


def _inline(plan_name: str, key: str, story: dict[str, Any], head: str) -> dict[str, Any]:
    """Run the same checks the detached job runs, synchronously (detached off)."""
    worktree = story["worktree"]
    try:
        settings = settings_for_plan(plan_name)
        checks = {"tests": "skipped", "build": "skipped", "lint": "skipped"}
        acc = _reverify_acceptance(story, worktree, key, settings=settings)
        if acc.get("state") == "fail":
            checks["tests"] = "fail"
            return _record(head, "fail", checks, "acceptance reverify fail")
        checks["tests"] = "pass" if acc.get("state") == "pass" else "skipped"

        build = _reverify_build(worktree, settings=settings)
        if build.get("state") == "fail":
            checks["build"] = "fail"
            return _record(head, "fail", checks, "build reverify fail")
        checks["build"] = "pass" if build.get("state") == "pass" else "skipped"

        lint = _run_lint_gate(Path(worktree), dict(os.environ))
        if lint is None:
            checks["lint"] = "not configured"
            return _record(head, "pass", checks, "")
        if lint.get("returncode") != 0:
            checks["lint"] = "fail"
            return _record(head, "fail", checks, "lint reverify fail")
        checks["lint"] = "pass"
        return _record(head, "pass", checks, "")
    except Exception as exc:  # noqa: BLE001 - a check that cannot run is never a pass
        return _record(head, "error", {}, f"local checks raised {type(exc).__name__}")
