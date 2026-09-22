from typing import Any

from .review_refs import _ServerRef

Path = _ServerRef("Path")
REVIEWER_AUTO_FIX_MAX_FILES = _ServerRef("REVIEWER_AUTO_FIX_MAX_FILES")
REVIEWER_AUTO_FIX_MAX_LINES = _ServerRef("REVIEWER_AUTO_FIX_MAX_LINES")
detect_test_command = _ServerRef("detect_test_command")
os = _ServerRef("os")
subprocess = _ServerRef("subprocess")


def _verify_reviewer_auto_fix(
    worktree: str,
    story: dict[str, Any],
    reviewer_output: str,
    before_sha: str | None,
) -> tuple[str, str]:
    """Mechanically re-verify a reviewer's self-reported APPROVE_WITH_FIX
    before review_story ever honors it like a real APPROVE (2026-07-29).

    Defense in depth: the reviewer's own "this is trivial and I'm
    confident" claim is never trusted alone. Independently re-checks (in
    order, cheapest first): the story's risk tier, that a new commit
    actually landed, that its diff stays within the configured file/line
    caps, and that the full test suite still passes. Any failure downgrades
    to REQUEST_CHANGES (fail closed) with an explanation - this folds back
    into review_story's ordinary rejection path, so an unverified self-fix
    still counts against the rework budget rather than looping forever or
    silently landing unverified code.

    Returns (verdict, feedback) where verdict is "APPROVE" or
    "REQUEST_CHANGES" - never the raw "APPROVE_WITH_FIX", so callers can
    treat the result exactly like any other reviewer verdict.
    """
    if story.get("risk", "low") != "low":
        return "REQUEST_CHANGES", (
            f"Reviewer self-fix (APPROVE_WITH_FIX) is only allowed for "
            f"risk: low stories; this story is risk: "
            f"{story.get('risk', 'low')!r}. Downgraded to REQUEST_CHANGES - "
            f"a human must review this change.\n\n{reviewer_output}"
        )
    if not before_sha:
        return "REQUEST_CHANGES", (
            "Reviewer self-fix could not be verified (no baseline commit "
            "was recorded before the review ran). Downgraded to "
            f"REQUEST_CHANGES.\n\n{reviewer_output}"
        )
    try:
        after_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, OSError) as e:
        return "REQUEST_CHANGES", (
            f"Reviewer self-fix could not be verified (git rev-parse failed: "
            f"{type(e).__name__}). Downgraded to REQUEST_CHANGES.\n\n{reviewer_output}"
        )
    if after_sha == before_sha:
        return "REQUEST_CHANGES", (
            "Reviewer reported APPROVE_WITH_FIX but no new commit was found "
            "on the branch - the claimed fix was never actually committed. "
            f"Downgraded to REQUEST_CHANGES.\n\n{reviewer_output}"
        )
    try:
        numstat = subprocess.run(
            ["git", "diff", "--numstat", before_sha, after_sha],
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (subprocess.CalledProcessError, OSError) as e:
        return "REQUEST_CHANGES", (
            f"Reviewer self-fix could not be verified (git diff failed: "
            f"{type(e).__name__}). Downgraded to REQUEST_CHANGES.\n\n{reviewer_output}"
        )
    changed_lines = [ln for ln in numstat.splitlines() if ln.strip()]
    files_changed = len(changed_lines)
    total_lines = sum(
        int(part)
        for ln in changed_lines
        for part in ln.split("\t")[:2]
        if part.isdigit()
    )
    if (
        files_changed > REVIEWER_AUTO_FIX_MAX_FILES
        or total_lines > REVIEWER_AUTO_FIX_MAX_LINES
    ):
        return "REQUEST_CHANGES", (
            f"Reviewer self-fix touched {files_changed} file(s) and "
            f"{total_lines} changed line(s), exceeding the auto-fix cap "
            f"({REVIEWER_AUTO_FIX_MAX_FILES} file(s), "
            f"{REVIEWER_AUTO_FIX_MAX_LINES} line(s)). Downgraded to "
            f"REQUEST_CHANGES - too large to trust as a mechanical, "
            f"low-risk fix; a full rework/re-review cycle is required.\n\n"
            f"{reviewer_output}"
        )
    test_dir, test_cmd = detect_test_command(Path(worktree))
    test_env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("PIPELINE_")
        and not k.startswith("LOCAL_AGENT_")
        and k != "REPO_ROOT"
    }
    test_result = subprocess.run(
        test_cmd,
        check=False,
        cwd=test_dir,
        capture_output=True,
        text=True,
        env=test_env,
    )
    if test_result.returncode != 0:
        return "REQUEST_CHANGES", (
            "Reviewer self-fix failed the full test suite after being "
            "applied. Downgraded to REQUEST_CHANGES.\n\n"
            f"Failing command: {' '.join(str(c) for c in test_cmd)}\n\n"
            f"```\n{(test_result.stdout or '')[-2000:]}\n```\n\n{reviewer_output}"
        )
    return "APPROVE", (
        f"{reviewer_output}\n\n[harness-verified self-fix: {files_changed} "
        f"file(s), {total_lines} line(s) changed, full test suite passed]"
    )


