"""Code-reviewer and security-reviewer dispatch.

_run_reviewer runs the code-reviewer persona over a branch; _run_security_reviewer
runs the security-engineer persona (always Claude - security-engineer is in
_LOCAL_SKIP_PERSONAS). Both are patched via p.<name> by tests; server call
sites use bare names -> re-export -> patch lands.
"""

import os
import shlex
from pathlib import Path

import backend
import role_registry

from .config import (
    _LOCAL_BACKEND_NAMES,
    DEFAULT_MODEL,
    REVIEWER_AUTO_FIX_MAX_FILES,
    REVIEWER_AUTO_FIX_MAX_LINES,
)
from .persona import _persona_body, _persona_default_model


def _run_reviewer(
    worktree: str, branch: str, backend_name: str | None = None,
    plan_role_config: dict | None = None,
    since_sha: str | None = None,
    risk: str = "low",
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
    if since_sha:
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
    prompt = (
        f"{lead}"
        f"{auto_fix_note}"
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
        f"but calibrate this to our Blocking-vs-Suggestion policy, don't treat every doc gap as a blocker; (4) if the change adds a module that other production files import from (a wrapper/adapter/binding shim), it must trace what that module actually calls and flag any module that reimplements logic it should delegate to as Blocking. For example, replacing a crypto/WASM/native binding with a pure-language no-op or a base64 round‑trip placeholder is not sufficient evidence; a green test suite alone does not prove delegation is real."
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
        f"correct and tested.\n\n"
        f"For large diffs: bash output is truncated to 3000 chars per call, "
        f"so a bare `git diff` may silently cut off. Start with "
        f"`git diff --stat` to see the scope, then use `git diff -- <file>` "
        f"per file (or `git diff <commit>` for a range), and `view_file` "
        f"for surrounding context. Do NOT rely on a single `git diff` for "
        f"a multi-file change. End with your VERDICT line; if you APPROVE, "
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
        max_tokens=int(os.environ.get("PIPELINE_REVIEW_MAX_TOKENS", "4096")),
        cell_dir=cell_dir,
    )


def _run_security_reviewer(
    worktree: str, branch: str, since_sha: str | None = None,
) -> str:
    """Run the security-engineer persona over a branch and return its raw output.

    External boundary: delegates to the configured Backend (always Claude -
    security-engineer is in _LOCAL_SKIP_PERSONAS). Tests mock this function.

    Like the ordinary reviewer, the security reviewer reviews the diff and
    trusts CI (tests_passed already gated entry); it does not re-run the
    test suite. since_sha scopes a rework review to the new commits since
    the last review (see _run_reviewer).
    """
    body = _persona_body("security-engineer")
    model = _persona_default_model("security-engineer") or DEFAULT_MODEL
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
    return backend.get_backend("review", name="claude").complete(
        prompt, system=body, model=model, allowed_tools="Bash,Read", cwd=worktree,
        max_tokens=int(os.environ.get("PIPELINE_SECURITY_REVIEW_MAX_TOKENS",
                                      os.environ.get("PIPELINE_REVIEW_MAX_TOKENS", "4096"))),
        cell_dir=cell_dir,
    )


__all__ = [
    "_run_reviewer",
    "_run_security_reviewer",
]