"""Code-reviewer and security-reviewer dispatch.

_run_reviewer runs the code-reviewer persona over a branch; _run_security_reviewer
runs the security-engineer persona (always Claude - security-engineer is in
_LOCAL_SKIP_PERSONAS). Both are patched via p.<name> by tests; server call
sites use bare names -> re-export -> patch lands.
"""

import hashlib
import logging  # noqa: F401  (re-exported name; phases live in review_run)
import os  # noqa: F401
import re
import shlex
import subprocess
from pathlib import Path

from app import backend, role_registry
from pipeline.review_base import resolve_review_base

from . import context_sources as ctxsrc
from . import review_run
from .actor import current_namespace
from .config import (
    _LOCAL_BACKEND_NAMES,  # noqa: F401  (read by review_run via this module)
    DEFAULT_MODEL,
    REVIEWER_AUTO_FIX_MAX_FILES,  # noqa: F401
    REVIEWER_AUTO_FIX_MAX_LINES,  # noqa: F401
    REVIEWER_INLINE_DIFF_MAX_CHARS,
)
from .file_size_policy_dispatch import file_size_advisory_note
from .live_ref import LiveRef
from .persona import _persona_body, _persona_default_model
from .review_rework_scope import scoped_rework_diff  # noqa: F401
from .store_records import (
    InvalidRecordKeyError,
    validate_context_digest,
)

_store = LiveRef("_store")

# Story-context injection (PLD90-W2-CTX): the reviewer prompt carries the
# story's own brief plus its siblings' status so the reviewer can tell
# out-of-scope work (assigned to a named sibling) from genuinely missing
# work. REVIEW_CONTEXT_SCOPE_RULE is appended to every non-None context
# string build_review_story_context returns - it is the rule that keeps
# sibling-assigned work out of Blocking findings without weakening the
# Blocking status of docs/comments made stale by THIS diff.
REVIEW_BRIEF_MAX_CHARS = 12000
REVIEW_SIBLINGS_MAX = 40
REVIEW_CONTEXT_SCOPE_RULE = (
    "Work that this story's brief explicitly assigns to another named sibling story "
    "(a `Deferred to <sibling> — not a finding:` line above) is out of scope for this diff: "
    "if you mention its absence at all, label it Suggestion, never Blocking. Documentation "
    "or comments made stale BY THIS DIFF remain Blocking under criterion (3)."
)

_DEFERRED_LINE_RE = re.compile(r"^Deferred to .+?\s+(?:—|--)\s+not a finding:")


def _deferred_lines(text: str) -> list[str]:
    """Return every Deferred line in `text`, stripped, in order.

    A Deferred line is one that, after stripping, starts with
    ``Deferred to <sibling> — not a finding:`` (an em dash or a double
    hyphen). Lines that merely mention "Deferred to" mid-sentence, or use a
    single hyphen, do not count. Empty or None text yields [].
    """
    if not text:
        return []
    return [
        stripped
        for line in text.splitlines()
        if (stripped := line.strip()) and _DEFERRED_LINE_RE.match(stripped)
    ]


def _read_recorded_snapshot(recorded: dict) -> str | None:
    """Return the snapshot text for a recorded digest, or None if it cannot be verified."""
    digest = recorded.get("digest") or ""
    try:
        validate_context_digest(digest)
    except InvalidRecordKeyError:
        return None
    snapshot = _store.records().get_context_snapshot(current_namespace(), digest)
    if snapshot is None:
        return None
    text = snapshot["content"]
    if "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest() != digest:
        return None
    return text


def build_phase_context_block(story_key: str, manifest: dict, phase: str) -> str:
    """Render the story's recorded context for ``phase`` ("review" or "security_review").

    Loads each snapshot by the digest recorded at dispatch and never refetches.
    A recorded source whose snapshot is missing or fails its digest check
    yields one visible line saying the review cannot verify it. Returns ""
    when the plan has no context or the story recorded no digests.
    """
    context = manifest.get("context")
    story = (manifest.get("stories") or {}).get(story_key) or {}
    recorded_all = story.get("context_digests") or {}
    if not context or not recorded_all:
        return ""
    resolved: list[dict] = []
    warnings: list[str] = []
    for source in ctxsrc.sources_for_phase(context["sources"], phase):
        recorded = recorded_all.get(source["name"])
        if not recorded:
            continue
        text = _read_recorded_snapshot(recorded)
        if text is None:
            warnings.append(
                f"[context source {source['name']} was recorded for this story but its "
                f"snapshot is unavailable; this review cannot verify it]"
            )
            continue
        resolved.append(
            {**source, "text": text, "digest": recorded["digest"], "bytes": recorded.get("bytes", len(text))}
        )
    max_total = context.get("max_total_chars", ctxsrc.DEFAULT_MAX_TOTAL_CHARS)
    block = ctxsrc.render_context_block(resolved, max_total_chars=max_total)
    return "\n".join(part for part in (block, *warnings) if part)


def build_review_story_context(story_key: str, manifest: dict) -> str | None:
    """Render the story's brief, sibling status and Deferred lines for the
    reviewer prompt.

    Returns None when the story is missing from ``manifest["stories"]`` or
    both its ``agent_instructions`` and its recorded review-phase context
    are absent - in that case the reviewer prompt is left unchanged. A story
    with context but no brief returns the context block (and scope rule).
    Otherwise returns the brief (truncated to
    REVIEW_BRIEF_MAX_CHARS), one line per sibling story (capped at
    REVIEW_SIBLINGS_MAX), the brief's own Deferred lines, and
    REVIEW_CONTEXT_SCOPE_RULE. The scope rule is appended unconditionally
    whenever a non-None string is returned: it governs how the reviewer
    treats sibling-assigned work, which matters even when this particular
    brief defers nothing (the rule also protects this diff's own stale-doc
    Blocking status). Pure function - no caching, no module state.
    """
    stories = manifest.get("stories") or {}
    story = stories.get(story_key)
    if not story:
        return None
    brief = (story.get("agent_instructions") or "").strip()
    context_block = build_phase_context_block(story_key, manifest, "review")
    if not brief and not context_block:
        return None

    sections: list[str] = []
    if brief:
        sections += [
            "--- This story's brief (what the diff was asked to do) ---",
            (
                brief[:REVIEW_BRIEF_MAX_CHARS] + "\n[... brief truncated ...]"
                if len(brief) > REVIEW_BRIEF_MAX_CHARS
                else brief
            ),
        ]

    siblings = [key for key in stories if key != story_key]
    if siblings:
        lines = []
        for key in sorted(siblings):
            sibling = stories[key]
            summary = sibling.get("summary") or key
            status = sibling.get("status") or "unknown"
            lines.append(f"- {summary} — {status}")
        shown = lines[:REVIEW_SIBLINGS_MAX]
        extra = len(lines) - len(shown)
        if extra:
            shown.append(f"- (+{extra} more)")
        sections.append("--- Sibling stories in this plan (summary — status) ---")
        sections.extend(shown)

    deferred = _deferred_lines(brief)
    if deferred:
        sections.append("--- Work this brief defers to named sibling stories ---")
        sections.extend(deferred)

    if context_block:
        sections.append(context_block)

    size_note = file_size_advisory_note(story, "reviewer")
    if size_note:
        sections.append(size_note)
    sections.append(REVIEW_CONTEXT_SCOPE_RULE)
    return "\n".join(sections)


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
    review's full diff.

    Delegates to `resolve_review_base`, which refreshes `origin` once and then
    picks the candidate closest to HEAD - across repos that differ on
    default-branch naming and on whether a remote exists at all - instead of
    the first one that resolves: in a long-lived clone a stale `origin/main`
    used to win on order alone and the review graded history from several
    stories back. The returned commit is the candidate's merge-base with HEAD,
    i.e. the fork point."""
    return resolve_review_base(worktree)


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
    layer_routing: dict | None = None,
    since_sha: str | None = None,
    risk: str = "low",
    prior_feedback: str | None = None,
    story_context: str | None = None,
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

    story_context (PLD90-W2-CTX) carries the story's own brief, its
    siblings' status, and the brief's Deferred lines (see
    build_review_story_context) into the prompt so the reviewer can tell
    work the brief assigned to a named sibling story - out of scope for
    this diff, Suggestion at most - from genuinely missing work. None or
    blank leaves the prompt unchanged.
    """
    body = _persona_body("code-reviewer")
    route = review_run.resolve_reviewer_route(
        backend_name, plan_role_config, layer_routing,
    )
    lead, inline_diff = review_run.build_review_lead(worktree, branch, since_sha)
    large_diff_note, no_rerun_note = review_run.build_diff_notes(inline_diff)
    prior_findings_note, story_context_note = review_run.build_prior_feedback_notes(
        prior_feedback, story_context,
    )
    prompt = review_run.assemble_review_prompt(review_run.ReviewPromptParts(
        lead=lead,
        no_rerun_note=no_rerun_note,
        auto_fix_note=review_run.build_auto_fix_note(risk),
        prior_findings_note=prior_findings_note,
        story_context_note=story_context_note,
        large_diff_note=large_diff_note,
    ))
    return backend.get_backend("review", name=route.name_for_get_backend).complete(
        prompt, system=body, model=route.model, allowed_tools="Bash,Read", cwd=worktree,
        cell_dir=review_run.review_cell_dir(worktree),
    )


# Findings contract for the security reviewer (srf-01). The review gate treats a
# REQUEST_CHANGES longer than UNFOCUSED_REQUEST_CHANGES_MIN_CHARS with no line of
# these forms as inconclusive, so the prompt must spell them out. The parser in
# pipeline/parsers.py is the authority; keep this text in step with it.
SECURITY_FINDINGS_CONTRACT = (
    "When your verdict is REQUEST_CHANGES, every Blocking finding must also appear "
    "as its own line of exactly this form, as plain text with no bold, heading, "
    "backticks or quotes around the path:\n"
    "- Blocking: <relative/file/path>: <one-line description>\n"
    "A Blocking finding that belongs to no single file is written as:\n"
    "- Blocking: (unscoped) <one-line description>\n"
    "Further detail (how it is exploited, the fix) may follow under each line in any "
    "form. A REQUEST_CHANGES without at least one such line is discarded as inconclusive."
)


def _run_security_reviewer(
    worktree: str, branch: str, since_sha: str | None = None,
    plan_role_config: dict | None = None,
    layer_routing: dict | None = None,
    context_block: str = "",
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
        layer_routing=layer_routing,
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
        f"and Secure-by-Design violations.\n\n"
        f"{SECURITY_FINDINGS_CONTRACT}\n\n"
        f"End with your VERDICT line: APPROVE or REQUEST_CHANGES."
    )
    if context_block:
        prompt = f"{prompt}\n\n{context_block}"
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
