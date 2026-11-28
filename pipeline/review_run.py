"""Phases of `pipeline.review._run_reviewer`, the code-reviewer dispatch.

Moved verbatim out of `_run_reviewer` (issue lfn-01), which now only calls
these in order: resolve the model and backend, build the diff lead, build the
prompt notes, assemble the prompt, and call the backend. The prompt text, the
routing rules and the call arguments are unchanged.

Every name that tests patch on `pipeline.review` is read through that module
at call time (`_home.<name>`), never copied at import.
"""

import logging
import os
import shlex
from dataclasses import dataclass
from pathlib import Path

from . import review as _home


@dataclass(frozen=True)
class ReviewerRoute:
    """The model and backend name a review call is routed with."""

    model: str
    name_for_get_backend: str | None


@dataclass(frozen=True)
class ReviewPromptParts:
    """The independently built sections `assemble_review_prompt` joins."""

    lead: str
    no_rerun_note: str
    auto_fix_note: str
    prior_findings_note: str
    story_context_note: str
    large_diff_note: str


def resolve_reviewer_route(
    backend_name: str | None,
    plan_role_config: dict | None,
    layer_routing: dict | None,
) -> ReviewerRoute:
    """Resolve the review model and the provider name handed to get_backend."""
    resolution = _home.role_registry.resolve_role(
        "review", plan_role_config=plan_role_config,
        layer_routing=layer_routing,
        model_fallback=lambda: _home._persona_default_model("code-reviewer") or _home.DEFAULT_MODEL,
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
    if resolved_backend in _home._LOCAL_BACKEND_NAMES:
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
        model = _home._persona_default_model("code-reviewer") or _home.DEFAULT_MODEL
    # Only pass an explicit resolved name to get_backend when a plan, layer
    # or registry override actually named a provider - otherwise keep passing
    # backend_name (None in the common case) unchanged, so an unconfigured
    # install still relies on get_backend's own internal PIPELINE_BACKEND_
    # REVIEW lookup exactly as before (behaviorally identical either way,
    # but this preserves what a mocked get_backend observes). A layer
    # (role_routing) provider counts the same as the other two: without it
    # the driver fell back to claude while the model was an Ollama tag.
    plan_cfg_review = (plan_role_config or {}).get("review", {})
    layer_cfg_review = (layer_routing or {}).get("review")
    layer_review_provider = (
        layer_cfg_review.get("provider")
        if isinstance(layer_cfg_review, dict) else None
    )
    registry_review_provider = (
        _home.role_registry.load_registry().get("roles", {}).get("review", {}).get("provider")
    )
    name_for_get_backend = backend_name
    if backend_name is None and (
        plan_cfg_review.get("provider")
        or (isinstance(layer_review_provider, str) and layer_review_provider)
        or registry_review_provider
    ):
        name_for_get_backend = resolution.provider
    return ReviewerRoute(model=model, name_for_get_backend=name_for_get_backend)


def build_review_lead(
    worktree: str, branch: str, since_sha: str | None,
) -> tuple[str, str | None]:
    """Return the prompt's diff-scoping lead and the inline diff (or None)."""
    base_ref: str | None
    inline_diff: str | None
    scoped = _home.scoped_rework_diff(worktree, since_sha, None) if since_sha else None
    if scoped is not None:
        logging.getLogger(_home.__name__).info(
            "review rework scope: drift detected", extra={"drift": True},
        )
    if scoped and scoped[1]:
        base_ref, inline_diff = scoped
        lead = (
            f"Review ONLY the story's own changes since your last review. "
            f"The branch was updated from the base branch since then; the "
            f"base branch's own changes are NOT part of this review and must "
            f"not be flagged. The story's own changed files since your last "
            f"review are listed in the diff below.\n\n"
            f"--- git diff {base_ref}..HEAD (story files only) ---\n"
            f"{inline_diff}\n"
            f"--- end diff ---\n\n"
        )
    else:
        # No drift (or no scoped diff): today's exact behaviour, unchanged.
        base_ref = since_sha or _home._first_review_base(worktree)
        inline_diff = (
            _home._materialize_review_diff(worktree, base_ref) if base_ref else None
        )
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
    return lead, inline_diff


def build_auto_fix_note(risk: str) -> str:
    """The reviewer self-fix offer; empty unless opted in for a low-risk story."""
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
            f"{_home.REVIEWER_AUTO_FIX_MAX_FILES} file(s) and "
            f"{_home.REVIEWER_AUTO_FIX_MAX_LINES} changed lines, and the full "
            f"test suite must still pass - if any of that fails, this "
            f"downgrades to REQUEST_CHANGES automatically, so do not use "
            f"this path to sneak through something you are not genuinely "
            f"confident about. When in doubt, REQUEST_CHANGES instead."
        )
    else:
        auto_fix_note = ""
    return auto_fix_note


def build_prior_feedback_notes(
    prior_feedback: str | None, story_context: str | None,
) -> tuple[str, str]:
    """Return (prior_findings_note, story_context_note)."""
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
    story_context_note = (
        f"{story_context.strip()}\n\n" if story_context and story_context.strip() else ""
    )
    return prior_findings_note, story_context_note


def build_diff_notes(inline_diff: str | None) -> tuple[str, str]:
    """Return (large_diff_note, no_rerun_note) for the given inline diff."""
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
    return large_diff_note, no_rerun_note


def assemble_review_prompt(parts: ReviewPromptParts) -> str:
    """Join the lead and notes with the fixed criteria block."""
    lead = parts.lead
    no_rerun_note = parts.no_rerun_note
    auto_fix_note = parts.auto_fix_note
    prior_findings_note = parts.prior_findings_note
    story_context_note = parts.story_context_note
    large_diff_note = parts.large_diff_note
    prompt = (
        f"{lead}"
        f"{no_rerun_note}"
        f"{auto_fix_note}"
        f"{prior_findings_note}"
        f"{story_context_note}"
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
        f"test suite alone does not prove delegation is real.\n\n"
        f"(5) the diff's file inventory is part of what you are signing "
        f"off on: list every file the diff ADDS and ask whether the change "
        f"needed it. A file that exists only because the work was carried "
        f"out - a one-shot helper script, a scratch or dump file, a copy of "
        f"an existing module, an edit artifact - is Blocking: name it on a "
        f"`- Blocking: <relative/file/path>: <one-line description>` line so "
        f"it is tracked. A new module, test, or doc the change's own code or "
        f"tests depend on is expected, not a finding.\n\n"
        f"{large_diff_note}"
        f"End with your VERDICT line; if you APPROVE, "
        f"also include a PR title and body."
    )
    return prompt


def review_cell_dir(worktree: str) -> str | None:
    """The benchmark cell directory (worktree's parent), or None for live reviews."""
    # cell_dir points at the worktree's parent directory. In production
    # that's ~/.claude/worktrees/; in the benchmark it's
    # <cell>/worktrees/, which the harness preserves across all trials
    # of a cell. The driver writes a per-call token-cost sidecar there;
    # None for live reviews, where the driver silently skips the sidecar.
    if Path(worktree).parent.name == "worktrees":
        cell_dir = str(Path(worktree).resolve().parent)
    else:
        cell_dir = None
    return cell_dir
