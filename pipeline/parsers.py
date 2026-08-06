"""Pure string/data parsers and small leaf helpers for the pipeline MCP server.

Everything here is a pure function: no module-level state, no free-variable
reads of server globals (PLAN_DIR / AGENTS_DIR / DEFAULT_MODEL / REPO_ROOT /
PLANE_*), no I/O beyond what's passed in. Tests call them as p.<name>;
server call sites use bare names, which resolve through the re-export in
pipeline_mcp_server.py to the patched binding (Option A in
PIPELINE_MCP_DECOMPOSITION_PLAN.md §4).

_atomic_write_json is monkeypatched by tests (p._atomic_write_json); the
server's _append_journal / _append_decision / _write_usage_state callers stay
in the server module and call it as a free variable, so the patch lands on
the re-exported binding as long as those callers don't move with it.
"""
import difflib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any


def _extract_json_block(text: str) -> str:
    """Strip a ```json ... ``` / ``` ... ``` fence around a JSON payload, if
    present, else return the text unchanged (trimmed). Models routinely wrap
    JSON output in a markdown fence even when asked not to; callers
    json.loads() the result themselves and handle a parse failure - this
    only handles the fence, not validation."""
    stripped = text.strip()
    m = re.search(r"```(?:json)?\s*\n?(.*?)```", stripped, re.DOTALL)
    return m.group(1).strip() if m else stripped

_SUGGESTED_COMMIT_MESSAGE_RE = re.compile(
    r"`((?:feat|fix|chore|refactor|test|docs|ci|perf|style|build)"
    r"(?:\([^)]*\))?!?:\s+\S[^`\n]*)`"
)
def _parse_ruling(text: str) -> dict[str, Any]:
    """Parse the overlord's output contract into a structured ruling."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"\s*(RULING|TIER|RISK|RATIONALE|NOTIFY_USER)\s*:\s*(.*)", line)
        if m:
            fields[m.group(1)] = m.group(2).strip()
    return {
        "ruling": fields.get("RULING", ""),
        "tier": fields.get("TIER", "").lower(),
        "risk": fields.get("RISK", "").lower(),
        "rationale": fields.get("RATIONALE", ""),
        "notify_user": fields.get("NOTIFY_USER", "no").lower() in ("yes", "true"),
    }


def _parse_verdict(text: str) -> str:
    # APPROVE_WITH_FIX must be tried before the bare APPROVE alternative -
    # regex alternation is first-match, not longest-match, so listing
    # APPROVE first would match it as a substring prefix of APPROVE_WITH_FIX
    # and silently drop the distinction.
    m = re.search(r"VERDICT:\s*(APPROVE_WITH_FIX|APPROVE|REQUEST_CHANGES)", text, re.IGNORECASE)
    return m.group(1).upper() if m else "UNKNOWN"


# T11: a REQUEST_CHANGES response with no substantive findings text - just
# the VERDICT line itself, or whitespace around it - gives a redispatched
# agent nothing to act on. Checked only when _parse_verdict returns
# REQUEST_CHANGES, mirroring how _is_rate_limited/_is_transient_backend_error
# are checked only after UNKNOWN. Deliberately a bare emptiness check, not a
# length floor: this codebase's own reviewer-stub convention (see
# test_review_story_parks_after_rework_budget_exhausted and its siblings, all
# using "still bad\nVERDICT: REQUEST_CHANGES") treats even a terse one-line
# finding as genuine, so any non-whitespace content beyond the verdict line
# must count.
def _has_review_findings(text: str) -> bool:
    """True when `text` contains findings beyond the bare VERDICT line."""
    stripped = re.sub(r"VERDICT:\s*(APPROVE|REQUEST_CHANGES)", "", text, flags=re.IGNORECASE)
    return bool(stripped.strip())


# Anchors that identify an infrastructure rate-limit response, not a genuine
# review. Checked only when _parse_verdict returns UNKNOWN (i.e. no VERDICT
# line) so that a review discussing rate-limiting code is never misclassified.
# Deliberately specific to the backend's own rate-limit banner phrasing —
# generic terms like "429" or "resets" are excluded because a review of
# rate-limiter code (e.g. this repo's own token_bucket benchmark task) can
# legitimately contain them, which would misfire this check on a truncated
# but otherwise genuine review.
_RATE_LIMIT_PATTERNS = [
    r"hit your session limit",
    r"usage limit reached",
    r"out_of_credits",
    r"overageDisabledReason",
]


def _is_rate_limited(text: str) -> bool:
    """True when `text` looks like an infra rate-limit message, not a review.

    Intentionally called only after _parse_verdict returns UNKNOWN, so a
    reviewer discussing rate-limit handling in the diff (which ends with a real
    VERDICT line) is never mistaken for a rate-limited call.
    """
    return any(re.search(pat, text, re.IGNORECASE) for pat in _RATE_LIMIT_PATTERNS)


# Transient-backend-error signatures distinct from rate-limiting. Like
# _RATE_LIMIT_PATTERNS, checked only when _parse_verdict returns UNKNOWN (no
# VERDICT line) so a review discussing HTTP 500 handling is never misclassified.
_TRANSIENT_BACKEND_PATTERNS = [
    r"500\s+internal\s+server\s+error",
    r"internal\s+server\s+error",
    r"connection\s+reset",
    r"connection\s+refused",
]


def _is_transient_backend_error(text: str) -> bool:
    """True when `text` looks like a transient backend error (HTTP 500,
    connection-reset/refused), not a rate-limit message or a genuine review.

    Intentionally called only after _parse_verdict returns UNKNOWN, so a
    reviewer discussing 500-handling code (which ends with a real VERDICT
    line) is never mistaken for a transient backend failure.
    """
    return any(re.search(pat, text, re.IGNORECASE) for pat in _TRANSIENT_BACKEND_PATTERNS)


# ---------- Conflict-marker parsing for the rebase auto-resolve path ----------

_AUTO_RESOLVE_IMPORT_PATTERN = re.compile(
    r"^\s*(import\s|from\s.+\simport\s|use\s|#include\s|require\()"
)


def _parse_conflict_blocks(text: str) -> list[tuple[int, int, list[str], list[str]]] | None:
    """Parse every ``<<<<<<<``/``=======``/``>>>>>>>`` block in `text`.

    Returns a list of (start_line, end_line, ours_lines, theirs_lines) - line
    indices into ``text.splitlines(keepends=True)`` spanning the whole marker
    block (inclusive) - or None if the file has no conflict markers at all,
    or has malformed/unterminated markers (never guess in that case; the
    caller disqualifies the whole rebase step)."""
    lines = text.splitlines(keepends=True)
    blocks: list[tuple[int, int, list[str], list[str]]] = []
    i = 0
    n = len(lines)
    found_any = False
    while i < n:
        if lines[i].startswith("<<<<<<<"):
            found_any = True
            start = i
            ours: list[str] = []
            i += 1
            while i < n and not lines[i].startswith("======="):
                ours.append(lines[i])
                i += 1
            if i >= n:
                return None
            i += 1  # skip the "=======" separator itself
            theirs: list[str] = []
            while i < n and not lines[i].startswith(">>>>>>>"):
                theirs.append(lines[i])
                i += 1
            if i >= n:
                return None
            end = i
            blocks.append((start, end, ours, theirs))
            i += 1
        else:
            i += 1
    return blocks if found_any else None


def _resolve_conflict_blocks(text: str, blocks: list[tuple[int, int, list[str], list[str]]]) -> str:
    """Replace each conflict-marker block with the union of both sides' added
    lines: ours followed by theirs, verbatim, no reordering/dedup/editing."""
    lines = text.splitlines(keepends=True)
    for start, end, ours, theirs in reversed(blocks):  # back-to-front: indices stay valid
        lines[start:end + 1] = ours + theirs
    return "".join(lines)


def _git_show_stage(worktree: str, stage: int, fname: str) -> str | None:
    """Read a file's content at conflict stage 1 (merge base)/2 (ours)/3
    (theirs) from the index. None on any failure (missing stage - e.g. a
    rename/delete conflict has no stage-1 entry - or a git/OSError), which
    the caller treats as "can't verify, disqualify"."""
    try:
        r = subprocess.run(["git", "show", f":{stage}:{fname}"], check=False, cwd=worktree,
                           capture_output=True, text=True)
    except OSError:
        return None
    return r.stdout if r.returncode == 0 else None


def _is_pure_additive_import_diff(base: str, other: str) -> bool:
    """True iff `other` differs from `base` by pure line insertions only (no
    deletion or modification of any base line), and every non-blank inserted
    line matches the conservative import/use pattern."""
    base_lines = base.splitlines(keepends=True)
    other_lines = other.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(a=base_lines, b=other_lines, autojunk=False)
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            return False
        if tag == "insert":
            for line in other_lines[j1:j2]:
                if line.strip() and not _AUTO_RESOLVE_IMPORT_PATTERN.match(line):
                    return False
    return True


# ---------- Atomic JSON writes + key validation ----------

def _atomic_write_json(path: Path, obj: Any) -> None:
    """Write *obj* as JSON to *path* atomically via a same-directory temp file.

    Uses os.replace() (POSIX-atomic on the same filesystem) so a crash or
    concurrent reader never observes a partial write. Raises on I/O error and
    leaves *path* untouched.
    """
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    try:
        tmp.write_text(json.dumps(obj, indent=2))
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


_KEY_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _validate_key(name: str) -> None:
    """Raise ValueError if *name* could be used for path traversal.

    plan_name and story_key flow into filesystem paths; this boundary check
    rejects anything containing path separators, null bytes, or characters
    outside the safe alphanumeric-plus-symbols set.
    """
    if not _KEY_RE.match(name):
        raise ValueError(f"invalid plan/story key {name!r}: only [A-Za-z0-9._-] allowed")


# ---------- Story-set / DONE-summary helpers ----------

def _completed_dep_ids(stories: dict[str, Any]) -> set[str]:
    """Identifiers a dependency string may legitimately reference for a *done*
    story, covering both forms a dependency can take.

    Ingest only rewrites a summary-string dependency to a manifest key when the
    source story carried a local `key` (see ingest_plan); plans whose stories
    have no key — and which therefore express dependencies as the prerequisite's
    exact summary string, per the documented save_plan schema — keep those
    summary deps verbatim while the manifest itself is keyed by UUID. Matching a
    dependency against both done keys and done summaries resolves it regardless
    of which form it took, so a dependent story is never stranded as unready."""
    done_keys = {k for k, v in stories.items() if v["status"] == "done"}
    done_summaries = {v["summary"] for v in stories.values() if v["status"] == "done"}
    return done_keys | done_summaries


# Literal, narrow phrases only - broad keyword matching would false-positive
# on legitimate completion summaries that happen to mention difficulty
# encountered along the way.
_GIVE_UP_PHRASES = (
    "i can't complete this task",
    "i cannot complete this task",
    "i'm unable to complete this task",
    "i am unable to complete this task",
    "i give up",
)


def _is_give_up_summary(summary: str) -> bool:
    """Whether a DONE summary reads as an explicit surrender rather than a
    genuine completion claim (2026-07-07 web-client-epic retro §3.2: the
    WASM story's second attempt called done with "I'm sorry, I can't
    complete this task" after real research, zero commits)."""
    lowered = summary.lower()
    return any(phrase in lowered for phrase in _GIVE_UP_PHRASES)

def _is_test_file_path(path: str) -> bool:
    """True iff `path`'s basename follows this repo's test-file naming
    convention (test_*.py or *_test.py), regardless of directory."""
    name = path.rsplit("/", 1)[-1]
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def _extract_blocking_finding_files(text: str) -> list[str]:
    """Extract file paths from Blocking findings in reviewer output."""
    pattern = r'^\s*-?\s*Blocking:\s*(\S+):'
    matches = re.findall(pattern, text, flags=re.MULTILINE | re.IGNORECASE)
    seen = set()
    result: list[str] = []
    for path in matches:
        if path not in seen:
            seen.add(path)
            result.append(path)
    return result


def _extract_suggested_commit_message(text: str) -> str | None:
    """Return the first backtick-quoted Conventional Commit header in text, or None."""
    m = _SUGGESTED_COMMIT_MESSAGE_RE.search(text)
    return m.group(1) if m else None


_PYTEST_FAILED_RE = re.compile(r"^FAILED\s+(\S+?)(?:::\S+)?(?:\s|$)", re.MULTILINE)


def _synthesize_test_failure_feedback(last_test_check: dict) -> str:
    """Build REQUEST_CHANGES reviewer_output directly from a known-failing
    last_test_check, for review_story to use INSTEAD of calling the LLM
    reviewer (Mode 40: a submission that doesn't even pass its own detected
    test command can't be meaningfully correctness-reviewed - a live
    incident sent exactly this shape of red submission to a full reviewer
    call whose principal finding just restated the failing-test list the
    gate had already recorded in last_test_check).

    Output is in the same format _parse_verdict/_extract_blocking_finding_files
    already expect (a VERDICT line, "- Blocking: <path>: <desc>" lines) so
    downstream handling (rework routing, Mode 24/28's finding-target
    tracking) works unchanged on a synthesized review same as a real one.
    """
    cmd = " ".join(last_test_check.get("cmd") or [])
    tail = ((last_test_check.get("stdout_tail") or "")
            + (last_test_check.get("stderr_tail") or ""))[-1500:]
    files = list(dict.fromkeys(_PYTEST_FAILED_RE.findall(tail)))  # dedup, order preserved

    lines = [
        "## Gate-synthesized review (LLM reviewer skipped)",
        "",
        ("The test command detected for this story failed - a submission "
        "that does not pass its own tests cannot be meaningfully "
        "correctness-reviewed, so this feedback is generated directly from "
        "the failing test run rather than spending a reviewer call "
        "restating it."),
        "",
        f"Failing command: `{cmd}`",
        "",
        "```",
        tail,
        "```",
        "",
        "### Blocking",
    ]
    if files:
        for f in files:
            lines.append(f"- Blocking: {f}: one or more tests fail in this "
                         f"file; see the failing test output above")
    else:
        lines.append("- Blocking: (unscoped) the detected test command "
                      "exited non-zero; see the failing test output above")
    lines += ["", "VERDICT: REQUEST_CHANGES"]
    return "\n".join(lines)
_SUGGESTED_COMMIT_MESSAGE_RE = re.compile(
    r"`((?:feat|fix|chore|refactor|test|docs|ci|perf|style|build)"
    r"(?:\([^)]*\))?!?:\s+\S[^`\n]*)`"
)

__all__ = [
    "_AUTO_RESOLVE_IMPORT_PATTERN",
    "_RATE_LIMIT_PATTERNS",
    "_TRANSIENT_BACKEND_PATTERNS",
    "_atomic_write_json",
    "_completed_dep_ids",
    "_extract_blocking_finding_files",
    "_extract_json_block",
    "_extract_suggested_commit_message",
    "_git_show_stage",
    "_has_review_findings",
    "_is_give_up_summary",
    "_is_pure_additive_import_diff",
    "_is_rate_limited",
    "_is_test_file_path",
    "_is_transient_backend_error",
    "_parse_conflict_blocks",
    "_parse_ruling",
    "_parse_verdict",
    "_resolve_conflict_blocks",
    "_synthesize_test_failure_feedback",
    "_validate_key",
]
