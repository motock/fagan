"""Code-reviewer and security-reviewer dispatch.

_run_reviewer runs the code-reviewer persona over a branch; _run_security_reviewer
runs the security-engineer persona (always Claude - security-engineer is in
_LOCAL_SKIP_PERSONAS). Both are patched via p.<name> by tests; server call
sites use bare names -> re-export -> patch lands.
"""

import os
import shlex
import subprocess
from pathlib import Path

from app import backend, role_registry

from .config import (
    _LOCAL_BACKEND_NAMES,
    DEFAULT_MODEL,
    REVIEWER_AUTO_FIX_MAX_FILES,
    REVIEWER_AUTO_FIX_MAX_LINES,
    REVIEWER_INLINE_DIFF_MAX_CHARS,
)
from .persona import _persona_body, _persona_default_model


def _review_git(worktree: str, args: list[str], timeout: int = 15) -> str | None:
    """Run a read-only git command in `worktree`. Returns None on any failure
    (missing worktree, non-repo directory, git absent, non-zero exit, or
    timeout) so callers can fall back to the reviewer discovering the diff
    itself rather than crashing the review."""
    try:
        result = subprocess.run(
            ["git", *args], cwd=worktree, check=False,
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _first_review_base(worktree: str) -> str | None:
    """The commit this branch diverged from, for a first (non-rework)
    review's full diff. Tries common default-branch names/remotes in order,
    since the pipeline operates across repos that differ on default-branch
    naming and on whether a remote exists at all (mirrors rebrief.py's
    _base_commit, duplicated rather than imported to keep review.py's diff
    materialization independent of rebrief's private helpers)."""
    for candidate in ("origin/HEAD", "origin/main", "origin/master", "main", "master"):
        merge_base = _review_git(worktree, ["merge-base", "HEAD", candidate])
        if merge_base and merge_base.strip():
            return merge_base.strip()
    return None


def _materialize_review_diff(worktree: str, base_ref: str) -> str | None:
    """Return `git diff {base_ref}..HEAD`, or None if it can't be fetched,
    is empty, or exceeds REVIEWER_INLINE_DIFF_MAX_CHARS. Never truncates a
    diff that's too large - a silently cut-off diff can look complete while
    hiding changes, which is worse than falling back to letting the reviewer
    discover it itself via its existing tool-based flow."""
    diff = _review_git(worktree, ["diff", base_ref, "HEAD"])
    if not diff or not diff.strip():
        return None
    if len(diff) > REVIEWER_INLINE_DIFF_MAX_CHARS:
        return None
    return diff


def _run_reviewer(
    worktree: str, branch: str, backend_name: str | None = None,
    plan_role_config: dict | None = None,
    since_sha: str | None = None,
    risk: str = "low",
    prior_feedback: str | None = None,
) -> str:
    """Run the code-reviewer persona over a branch and return its raw output.

    External boundary: delegates to the configured Backend. Tests mock this
    function. backend_name lets a caller override the env-resolved default
    (e.g. review_story's rate-limit fallback routing to "local");
    get_backend already treats name=None as "use the env-resolved default".

    The reviewer reviews the DIFF and trusts CI: review_story only ever
    invokes it when story["status"] == "tests_passed", i.e. the full test
    suite is already green (check_story_status ran it moments earlier to
    reach that state). A reviewer-driven test-suite rerun is pure duplicate
    spend - a full agentic Bash tool-loop re-executing what CI just ran -
    so the prompt no longer instructs the reviewer to run the tests and no
    longer injects a resolved test command. This mirrors a real PR review:
    CI is the gate, the reviewer reviews the diff for correctness, security,
    and standards.

    since_sha (real-PR re-review, 2026-07-28) scopes a rework review to
    ONLY the new commits pushed since the last REQUEST_CHANGES (the commit
    last_reviewed_sha was recorded against). The reviewer does not re-read
    already-approved, unchanged files from zero each cycle - it reviews just
    `git diff {since_sha}..HEAD`, exactly as a human reviewer reviews only
    the new commits pushed to a PR after sending it back. None on a first
    review means the full branch diff.

    risk gates the reviewer self-fix option (VERDICT: APPROVE_WITH_FIX,
    2026-07-29): only offered when PIPELINE_REVIEWER_AUTO_FIX=1 AND risk is
    "low". Read live via os.environ (not a config.py-imported constant) so
    tests can toggle it per-call, matching PIPELINE_LOCAL_REVIEW_MODEL's
    convention above. This is a prompt-construction-level gate, not the only
    one - review_story's _verify_reviewer_auto_fix mechanically re-checks
    risk (and diff size, and the full suite) before honoring the verdict, so
    a reviewer that ignores this instruction and emits APPROVE_WITH_FIX
    anyway on a high-risk story still can't get it applied.

    prior_feedback (Mode 47, 2026-07-30) carries the previous cycle's
    REQUEST_CHANGES output into a re-review so each prior Blocking finding
    can be verified individually. The existing Mode 24/28 finding-target
    guard in review_story only asks "was the flagged file touched since the
    last review" - a PARTIAL fix touches the file and sails through it. Live:
    TRANSPORT-ALIAS-READERS #200 was told to migrate two env-var keys, moved
    one, and got APPROVEd because the suite was green and the file had been
    touched. Only per-finding verification catches that, and only the
    reviewer can do it - no suite-gate can, since nothing fails.
    """
    body = _persona_body("code-reviewer")
    # Provider/model fall through role_registry (PIPELINE_BACKEND_REVIEW /
    # a plan's role_config / model_registry.json's "review" entry), falling
    # back to the persona's declared tier when none of those apply - so an
    # unconfigured install resolves identically to before role_registry
    # existed. backend_name (an explicit caller override, e.g. review_story's
    # FM-B rate-limit fallback) always wins over the registry-resolved
    # provider, exactly as it already won over the plain env lookup.
    resolution = role_registry.resolve_role(
        "review", plan_role_config=plan_role_config,
        model_fallback=lambda: _persona_default_model("code-reviewer") or DEFAULT_MODEL,
    )
    model = resolution.model
    # Asymmetric review: software-engineer.md and code-reviewer.md both
    # declare `model: sonnet`, so without an override dispatch and review
    # resolve to the identical concrete local model - a model reviewing its
    # own work with identical weights. When the review backend is actually
    # local, an explicit PIPELINE_LOCAL_REVIEW_MODEL overrides the tier so
    # review can run on a different (e.g. stronger) local model - and stays
    # the top-priority override even when the registry also configures a
    # model, since it is the most specific, most recently-set knob. Gated on
    # backend == "local" so a bare Ollama tag never leaks into a cloud
    # review as a bogus --model value. backend_name may already be the
    # explicit "local" (review_story's FM-B rate-limit fallback); otherwise
    # fall back to the registry-resolved provider, mirroring how get_backend
    # itself treats name=None.
    resolved_backend = (backend_name or resolution.provider).strip().lower()
    if resolved_backend in _LOCAL_BACKEND_NAMES:
        review_model_override = os.environ.get("PIPELINE_LOCAL_REVIEW_MODEL")
        if review_model_override:
            model = review_model_override
    elif resolved_backend != resolution.provider:
        # backend_name overrode the provider away from whichever provider
        # resolution.model was actually paired with (e.g. an escalated
        # review forcing "claude" while the registry's review role is
        # ollama/glm) - resolution.model is a raw tag that belongs to the
        # DISCARDED provider and must never be passed straight through as
        # an invalid --model value to the new one.
        model = _persona_default_model("code-reviewer") or DEFAULT_MODEL
    # Only pass an explicit resolved name to get_backend when a plan/registry
    # override actually named a provider - otherwise keep passing
    # backend_name (None in the common case) unchanged, so an unconfigured
    # install still relies on get_backend's own internal PIPELINE_BACKEND_
    # REVIEW lookup exactly as before (behaviorally identical either way,
    # but this preserves what a mocked get_backend observes).
    plan_cfg_review = (plan_role_config or {}).get("review", {})
    registry_review_provider = (
        role_registry.load_registry().get("roles", {}).get("review", {}).get("provider")
    )
    name_for_get_backend = backend_name
    if backend_name is None and (plan_cfg_review.get("provider") or registry_review_provider):
        name_for_get_backend = resolution.provider

    # Diff-scoping lead. On a rework (since_sha set) the reviewer reviews
    # ONLY the new commits since the last REQUEST_CHANGES; on a first review
    # (None) it reviews the full branch diff. The substantive criteria block
    # below is shared by both.
    #
    # Diff pre-materialization: fetch the diff here, server-side, and embed
    # it directly rather than making the reviewer discover it turn-by-turn
    # (git diff --stat -> per-file git diff -> view_file). On a backend with
    # no prompt caching, each of those turns re-bills the whole growing
    # transcript from scratch - see REVIEWER_INLINE_DIFF_MAX_CHARS's config.py
    # comment for the measured cost this addresses. base_ref/inline_diff stay
    # None on any failure (bad worktree, no common base, diff over budget),
    # which falls through to the exact prior explore-yourself instructions.
    base_ref = since_sha or _first_review_base(worktree)
    inline_diff = _materialize_review_diff(worktree, base_ref) if base_ref else None
    if since_sha:
        if inline_diff:
            lead = (
                f"Review ONLY the new changes pushed since your last review "
                f"(commit {shlex.quote(since_sha)}). The implementer addressed "
                f"your prior feedback; do NOT re-review files that were already "
                f"approved and have not changed since. The full diff since your "
                f"last review is included below - do NOT re-run `git diff` to "
                f"fetch it again; Bash/view_file remain available for anything "
                f"not shown here (e.g. surrounding lines in a file, or a related "
                f"file the diff doesn't touch).\n\n"
                f"--- git diff {since_sha}..HEAD ---\n{inline_diff}\n"
                f"--- end diff ---\n\n"
            )
        else:
            lead = (
                f"Review ONLY the new changes pushed since your last review "
                f"(commit {shlex.quote(since_sha)}). The implementer addressed "
                f"your prior feedback; do NOT re-review files that were already "
                f"approved and have not changed since. See the new diff with: "
                f"git diff {shlex.quote(since_sha)}..HEAD\n"
                f"(start with `git diff --stat {shlex.quote(since_sha)}..HEAD` "
                f"for scope, then `git diff {shlex.quote(since_sha)}..HEAD -- "
                f"<file>` per file, and `view_file` for surrounding context).\n\n"
            )
    elif inline_diff:
        lead = (
            f"Review the changes on branch {branch} in this worktree against "
            f"our standards. The full diff against the branch's base is "
            f"included below - do NOT re-run `git diff` to fetch it again; "
            f"Bash/view_file remain available for anything not shown here "
            f"(e.g. surrounding lines in a file, or a related file the diff "
            f"doesn't touch).\n\n"
            f"--- git diff {base_ref}..HEAD ---\n{inline_diff}\n"
            f"--- end diff ---\n\n"
        )
    else:
        lead = (
            f"Review the changes on branch {branch} in this worktree against "
            f"our standards.\n\n"
        )
    # Reviewer self-fix (2026-07-29): only ever offered for a low-risk story
    # when the operator has explicitly opted in - never mentioned otherwise,
    # so an unconfigured install's reviewer behaves exactly as before. Even
    # when offered, this is advisory, not the only gate: review_story's
    # _verify_reviewer_auto_fix mechanically re-checks risk, diff size, and
    # the full test suite before an APPROVE_WITH_FIX is honored.
    auto_fix_enabled = os.environ.get("PIPELINE_REVIEWER_AUTO_FIX", "0") == "1"
    if auto_fix_enabled and risk == "low":
        auto_fix_note = (
            f"\n\nSelf-fix option: if the ONLY Blocking finding is a small, "
            f"mechanical, single-concern fix you are highly confident is "
            f"correct (e.g. a narrow logic/regex bug, an off-by-one, a "
            f"missing null/negative-input check) - NOT a design change, NOT "
            f"a multi-file change, NOT anything touching auth/secrets/"
            f"payments/data-access - you may apply it directly (edit the "
            f"file, run the tests, commit) and report `VERDICT: "
            f"APPROVE_WITH_FIX` instead of REQUEST_CHANGES. Describe exactly "
            f"what you changed and why you're confident it's correct and "
            f"low-risk. The harness independently re-verifies this before "
            f"trusting it: the fix must touch at most "
            f"{REVIEWER_AUTO_FIX_MAX_FILES} file(s) and "
            f"{REVIEWER_AUTO_FIX_MAX_LINES} changed lines, and the full "
            f"test suite must still pass - if any of that fails, this "
            f"downgrades to REQUEST_CHANGES automatically, so do not use "
            f"this path to sneak through something you are not genuinely "
            f"confident about. When in doubt, REQUEST_CHANGES instead."
        )
    else:
        auto_fix_note = ""
    # Mode 47: on a re-review, make the reviewer discharge its OWN prior
    # findings one at a time. A green suite is not evidence a finding was
    # resolved - the finding may name work no test grades at all, which is
    # exactly how the live green-but-incomplete merge happened.
    if prior_feedback and prior_feedback.strip():
        prior_findings_note = (
            f"\n\n--- Your prior Blocking findings on this branch ---\n"
            f"{prior_feedback.strip()}\n\n"
            f"Before any verdict, walk these one at a time and state for EACH "
            f"whether it is now fully resolved, quoting the specific diff hunk "
            f"that resolves it. Do not batch them into a single 'addressed' "
            f"claim. A finding is resolved only when EVERYTHING it asked for "
            f"landed - if it named two changes and only one was made, it is "
            f"PARTIALLY addressed, which is NOT resolved: re-raise it as "
            f"Blocking and REQUEST_CHANGES, naming the specific part still "
            f"missing. The test suite being green does NOT prove a finding is "
            f"resolved: a finding about a rename, a dead leftover name, or a "
            f"docstring typically has no test grading it at all, so a passing "
            f"suite says nothing about it. Verify against the diff itself, "
            f"not against the test results.\n\n"
        )
    else:
        prior_findings_note = ""
    # Irrelevant once the full diff is already embedded in `lead` above -
    # nothing was truncated, so there's nothing to warn about re-fetching.
    large_diff_note = "" if inline_diff else (
        "For large diffs: bash output is truncated to 3000 chars per call, "
        "so a bare `git diff` may silently cut off. Start with "
        "`git diff --stat` to see the scope, then use `git diff -- <file>` "
        "per file (or `git diff <commit>` for a range), and `view_file` "
        "for surrounding context. Do NOT rely on a single `git diff` for "
        "a multi-file change. "
    )
    if inline_diff:
        bash_purpose_note = (
            "The diff has already been provided above - do not re-fetch it. "
            "Bash/view_file are available only for context beyond it (e.g. "
            "surrounding lines in a file, or a related file the diff doesn't "
            "touch). Use them for nothing else.\n"
        )
    else:
        bash_purpose_note = (
            "Bash is provided ONLY for reading the diff: `git diff --stat`, "
            "`git diff -- <file>`, `git diff <commit> HEAD`, and `view_file` "
            "for context. Use it for nothing else.\n"
        )
    no_rerun_note = (
        "The full test suite is ALREADY green (CI ran it; that is the "
        "precondition for this review). Do NOT run the test suite, do NOT "
        "run pytest, and do NOT build or install the project. Re-running it "
        "is duplicate spend that risks exhausting your step budget without "
        "reaching a verdict.\n"
        f"{bash_purpose_note}"
        "python is NOT on PATH in this worktree (the venv interpreter lives "
        "in the venv's bin directory, not on PATH); any `python ...` command "
        "will fail with 'command not found' and waste your step budget. "
        "Do not attempt it.\n\n"
    )
    prompt = (
        f"{lead}"
        f"{no_rerun_note}"
        f"{auto_fix_note}"
        f"{prior_findings_note}"
        f"Report EVERY Blocking finding you notice in this single "
        f"pass, not just the first one - the implementer is a weak local "
        f"model and each REQUEST_CHANGES cycle is a full rework redispatch, "
        f"which costs time and is itself a fresh opportunity to regress "
        f"already-correct code. Do not hold a finding back to raise on a "
        f"later cycle just because it's secondary to the first one you "
        f"noticed. Specifically check: (1) any function "
        f"taking a mutable argument (list, dict, set) does not mutate it in "
        f"place unless that is the documented contract; (2) inputs are "
        f"validated at system boundaries, including negative/out-of-range "
        f"numeric arguments, not just the happy path; (3) documentation - "
        f"but calibrate this to our Blocking-vs-Suggestion policy, don't "
        f"treat every doc gap as a blocker. If this change alters behavior "
        f"that EXISTING callers/users already depend on (a public API "
        f"contract, configuration, CLI flag, or user-facing functionality "
        f"that predates this change) and no documentation update accompanies "
        f"it, that's a genuine problem: REQUEST_CHANGES and name the "
        f"specific doc (a README or other in-repo doc) that needs updating. "
        f"For a brand-new addition with no "
        f"existing external callers yet (e.g. a new module/class/function "
        f"nothing else in the repo calls), a missing doc update is a "
        f"Suggestion, not a blocker - note it in your summary but don't "
        f"REQUEST_CHANGES for that reason alone if the code itself is "
        f"correct and tested; (4) if the change adds a module that other "
        f"production files import from (a wrapper/adapter/binding shim), it "
        f"must trace what that module actually calls and flag any module that "
        f"reimplements logic it should delegate to as Blocking. For example, "
        f"replacing a crypto/WASM/native binding with a pure-language no-op or "
        f"a base64 round-trip placeholder is not sufficient evidence; a green "
        f"test suite alone does not prove delegation is real.\\n\\n"
        f"(5) the diff's file inventory is part of what you are signing "
        f"off on: list every file the diff ADDS and ask whether the change "
        f"needed it. A file that exists only because the work was carried "
        f"out - a one-shot helper script, a scratch or dump file, a copy of "
        f"an existing module, an edit artifact - is Blocking: name it on a "
        f"`- Blocking: <relative/file/path>: <one-line description>` line so "
        f"it is tracked. A new module, test, or doc the change's own code or "
        f"tests depend on is expected, not a finding.\\n\\n"
        f"{large_diff_note}"
        f"End with your VERDICT line; if you APPROVE, "
        f"also include a PR title and body."
    )
    # cell_dir points at the worktree's parent directory. In production
    # that's ~/.claude/worktrees/; in the benchmark it's
    # <cell>/worktrees/, which the harness preserves across all trials
    # of a cell (worktrees/<story_key>/ is removed on merge, but the
    # surrounding worktrees/ dir is not). The driver writes a per-call
    # token-cost sidecar there so the data survives the worktree
    # cleanup that wipes review.log. None for live (non-benchmark)
    # reviews whose worktree lives somewhere we shouldn't be
    # scribbling new files into: in that case the driver silently
    # skips the sidecar.
    if Path(worktree).parent.name == "worktrees":
        cell_dir = str(Path(worktree).resolve().parent)
    else:
        cell_dir = None
    return backend.get_backend("review", name=name_for_get_backend).complete(
        prompt, system=body, model=model, allowed_tools="Bash,Read", cwd=worktree,
        cell_dir=cell_dir,
    )


def _run_security_reviewer(
    worktree: str, branch: str, since_sha: str | None = None,
    plan_role_config: dict | None = None,
) -> str:
    """Run the security-engineer persona over a branch and return its raw output.

    External boundary: delegates to the configured Backend. Tests mock this
    function.

    Role-routable (2026-08-03): the security pass resolves a "security" role
    through role_registry - plan_role_config["security"] ->
    PIPELINE_BACKEND_SECURITY env -> registry roles["security"] -> Claude
    (the default_provider fallback), so an unconfigured install resolves to
    Claude exactly as the prior hardcode did, while a plan that sets
    role_config.security (e.g. ollama/glm) can clear high-risk security
    review without Claude. _LOCAL_SKIP_PERSONAS is NOT consulted here: that
    set governs *dispatch* routing (_persona_requires_claude in persona.py),
    a separate concern from this review pass; the prior "always Claude" was
    enforced solely by the hardcoded backend name, now replaced by the
    resolved provider.

    Like the ordinary reviewer, the security reviewer reviews the diff and
    trusts CI (tests_passed already gated entry); it does not re-run the
    test suite. since_sha scopes a rework review to the new commits since
    the last review (see _run_reviewer).
    """
    body = _persona_body("security-engineer")
    resolution = role_registry.resolve_role(
        "security", plan_role_config=plan_role_config,
        model_fallback=lambda: _persona_default_model("security-engineer") or DEFAULT_MODEL,
    )
    model = resolution.model
    if since_sha:
        lead = (
            f"Review ONLY the new security-relevant changes pushed since your "
            f"last review (commit {shlex.quote(since_sha)}): "
            f"git diff {shlex.quote(since_sha)}..HEAD\n\n"
        )
    else:
        lead = (
            f"Perform a security review of the changes on branch {branch} in "
            f"this worktree.\n\n"
        )
    prompt = (
        f"{lead}"
        f"Check for OWASP issues, secrets, injection, auth/authz bypasses, "
        f"and Secure-by-Design violations. End with your VERDICT line: "
        f"APPROVE or REQUEST_CHANGES."
    )
    if Path(worktree).parent.name == "worktrees":
        cell_dir = str(Path(worktree).resolve().parent)
    else:
        cell_dir = None
    return backend.get_backend("review", name=resolution.provider).complete(
        prompt, system=body, model=model, allowed_tools="Bash,Read", cwd=worktree,
        cell_dir=cell_dir,
    )


__all__ = [
    "_run_reviewer",
    "_run_security_reviewer",
]