"""Review-comment formatting for the review orchestrator (split out to keep
pipeline/review_orchestrator.py under the 1000-line limit)."""

_SECURITY_COMMENT_MAX_CHARS = 8_000


def _format_security_comment(report: str) -> str:
    header = "## Security review (security-engineer): APPROVE\n\n"
    if len(report) > _SECURITY_COMMENT_MAX_CHARS:
        return header + report[:_SECURITY_COMMENT_MAX_CHARS] + "\n\n[report truncated]"
    return header + report
