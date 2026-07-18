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
from pipeline_config import DEFAULT_MODEL, _LOCAL_BACKEND_NAMES
from pipeline_persona import _persona_body, _persona_default_model
from pipeline_build_detect import detect_test_command, _scope_test_cmd_to_acceptance


def _run_reviewer(
    worktree: str, branch: str, backend_name: str | None = None,
    plan_role_config: dict | None = None,
    acceptance: list[dict] | None = None,
) -> str:
    """Run the code-reviewer persona over a branch and return its raw output.

    External boundary: delegates to the configured Backend. Tests mock this
    function. backend_name lets a caller override the env-resolved default
    (e.g. review_story's rate-limit fallback routing to "local");
    get_backend already treats name=None as "use the env-resolved default".

    acceptance is the story's acceptance block (Mode 20, 2026-07-17): when
    present and the detected test command is pytest, the reviewer's test
    command is scoped to ONLY those paths, exactly like _reverify_acceptance
    scopes the pre-merge re-check. Without this, the reviewer's own free-form
    `pytest` invocation can rediscover and block on a bug in the AGENT'S OWN
    test file even when the harness's acceptance oracle already passes -
    FM-A's exact root cause (see project-benchmark-failure-modes memory),
    resurrected here because the harness test gate was scoped but the
    reviewer never was.
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
    # The reviewer model has no access to detect_test_command's Python-level
    # venv resolution, so a bare "Run the test suite" instruction leaves it
    # to guess a shell command - e.g. the relative `.venv/bin/python -m
    # pytest`, which does not exist inside a worktree (worktrees are
    # gitignored and never contain .venv). Resolve the same command
    # check_story_status's test gate trusts and hand it over verbatim. Any
    # resolution failure (nonexistent worktree, no recognized build marker)
    # must not block review - fall back to the generic instruction below.
    test_command_instruction = ""
    try:
        test_dir, test_cmd = detect_test_command(Path(worktree))
        scope_note = ""
        if acceptance:
            acceptance_paths = [
                str(test_dir / entry["path"]) for entry in acceptance
            ]
            scoped = _scope_test_cmd_to_acceptance(test_cmd, acceptance_paths, test_dir)
            if scoped is not None:
                test_cmd = scoped
                scope_note = (
                    "This story carries a harness-owned acceptance oracle; the "
                    "command below is scoped to ONLY those acceptance tests, "
                    "which are the authoritative spec for required behavior. A "
                    "failure in the implementer's OWN test file that the "
                    "acceptance oracle does not require is not sufficient "
                    "grounds for REQUEST_CHANGES on its own - note it as a "
                    "Suggestion if you notice it, but base your verdict on the "
                    "acceptance oracle plus your own code-quality/security "
                    "review, not on re-running the implementer's full test "
                    "file.\n\n"
                )
        test_command_instruction = (
            f"{scope_note}"
            f"Run the test suite with exactly this command (do not "
            f"substitute a different interpreter path): cd "
            f"{shlex.quote(str(test_dir))} && {shlex.join(test_cmd)}\n\n"
        )
    except Exception:
        pass
    prompt = (
        f"{test_command_instruction}"
        f"Review the changes on branch {branch} in this worktree against our "
        f"standards. Run the test suite. Specifically check: (1) any function "
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


def _run_security_reviewer(worktree: str, branch: str) -> str:
    """Run the security-engineer persona over a branch and return its raw output.

    External boundary: delegates to the configured Backend (always Claude —
    security-engineer is in _LOCAL_SKIP_PERSONAS). Tests mock this function.
    """
    body = _persona_body("security-engineer")
    model = _persona_default_model("security-engineer") or DEFAULT_MODEL
    prompt = (
        f"Perform a security review of the changes on branch {branch} in this "
        f"worktree. Check for OWASP issues, secrets, injection, auth/authz "
        f"bypasses, and Secure-by-Design violations. Run the test suite. "
        f"End with your VERDICT line: APPROVE or REQUEST_CHANGES."
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