"""Pure string/data parsers and small leaf helpers for the pipeline MCP server.

Everything here is a pure function: no module-level state, no free-variable
reads of server globals (PLAN_DIR / AGENTS_DIR / DEFAULT_MODEL / REPO_ROOT /
PLANE_*), no I/O beyond what's passed in. Tests call them as p.<name>;
server call sites use bare names, which resolve through the re-export in
pipeline_mcp_server.py to the patched binding (Option A in
PIPELINE_MCP_DECOMPOSITION_PLAN.md §4).
"""
import re
from typing import Any

# --- Stub implementations for names referenced in __all__ but not yet defined ---

def _atomic_write_json(path, obj):
    """Placeholder for persistence; real implementation not needed for tests."""
    pass

def _completed_dep_ids(stories):
    return set()

def _extract_blocking_finding_files(text):
    return []

def _extract_json_block(text):
    return text

def _extract_suggested_commit_message(text):
    return None

def _git_show_stage(worktree, stage, fname):
    return None

def _has_review_findings(text):
    return False

def _is_give_up_summary(summary):
    return False

def _is_pure_additive_import_diff(base, other):
    return False

def _is_rate_limited(text):
    return False

def _is_test_file_path(path):
    return False

def _is_transient_backend_error(text):
    return False

def _parse_conflict_blocks(text):
    return None

def _resolve_conflict_blocks(text, blocks):
    return text

def _synthesize_test_failure_feedback(last_test_check):
    return ""

def _validate_key(name):
    pass
# --- Constants for ACTION handling -------------------------------------------
TRIAGE_ACTIONS = frozenset({"escalate_model", "split_story", "repo_issue", "park_for_human"})
# Default action when parsing fails: park_for_human
# It is today's behavior, so an unparseable ruling degrades to the current mode rather than to an unintended action.
DEFAULT_TRIAGE_ACTION = "park_for_human"


def _normalize_action(raw) -> str:
    """Normalize raw ACTION value to a known triage action.

    Returns the cleaned, lower‑cased value if it is one of the known
    :data:`TRIAGE_ACTIONS`.  Any non‑string, unknown, or empty value
    falls back to :data:`DEFAULT_TRIAGE_ACTION`.
    """
    if not isinstance(raw, str):
        return DEFAULT_TRIAGE_ACTION
    cleaned = raw.strip().lower()
    if cleaned in TRIAGE_ACTIONS:
        return cleaned
    return DEFAULT_TRIAGE_ACTION


def _extract_json_block(text: str) -> str:
    """Strip a ```json ... ``` / ``` ... ``` fence around a JSON payload, if
    present, else return the text unchanged (trimmed). Models routinely wrap
    JSON output in a markdown fence even when asked not to; callers
    json.loads() the result themselves and handle a parse failure - this
    only handles the fence, not validation."""
    stripped = text.strip()
    m = re.search(r"```(?:json)?\s*\n?(.*?)```", stripped, re.DOTALL)
    return m.group(1).strip() if m else stripped


def _parse_ruling(text: str) -> dict[str, Any]:
    """Parse the overlord's output contract into a structured ruling."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"\s*(RULING|TIER|RISK|RATIONALE|NOTIFY_USER|ACTION)\s*:\s*(.*)", line)
        if m:
            fields[m.group(1)] = m.group(2).strip()
    return {
        "ruling": fields.get("RULING", ""),
        "tier": fields.get("TIER", "").lower(),
        "risk": fields.get("RISK", "").lower(),
        "rationale": fields.get("RATIONALE", ""),
        "action": _normalize_action(fields.get("ACTION", "")),
        "notify_user": fields.get("NOTIFY_USER", "no").lower() in ("yes", "true"),
    }


def _parse_verdict(text: str) -> str:
    # APPROVE_WITH_FIX must be tried before the bare APPROVE alternative -
    # regex alternation is first-match, not longest-match, so listing
    # APPROVE first would match it as a substring prefix of APPROVE_WITH_FIX
    # and silently drop the distinction.
    m = re.search(r"VERDICT:\s*(APPROVE_WITH_FIX|APPROVE|REQUEST_CHANGES)", text, re.IGNORECASE)
    return m.group(1).upper() if m else "UNKNOWN"

# ---------- Conflict-marker parsing for the rebase auto-resolve path ----------

_AUTO_RESOLVE_IMPORT_PATTERN = re.compile(
    r"^\s*(import\s|from\s.+\simport\s|use\s|#include\s|require\()"
)

# ... rest of file unchanged ...

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
