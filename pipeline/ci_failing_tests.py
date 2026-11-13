"""Failing-test ids pulled from a CI run's log for the merge-gate rework message.

Pure string handling: the forge adapter fetches the log, this module reads
pytest's ``FAILED <nodeid> - <summary>`` lines out of it. Only the node id and
the first :data:`SUMMARY_MAX_CHARS` characters of the summary leave this
module, so no other log text reaches the rework message.
"""

import re

#: Most ids named in the rework message; the rest collapse into one line.
FAILING_TESTS_CAP = 20
#: Longest error summary kept per id.
SUMMARY_MAX_CHARS = 200

_ANSI_SGR = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# ``\S+::\S+`` requires a pytest node id (``path::name``), so a stray "FAILED"
# word in build output is not mistaken for a failing test.
_FAILED_LINE = re.compile(
    r"(?:^|\s)FAILED\s+(?P<nodeid>\S+::\S+)(?:\s+-\s+(?P<summary>.*))?$"
)


def _is_repo_relative(nodeid: str) -> bool:
    """True when the node id's path stays inside the repository."""
    path = nodeid.split("::", 1)[0].replace("\\", "/")
    if not path or path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        return False
    return ".." not in path.split("/")


def extract_failing_tests(log: str) -> list[tuple[str, str]]:
    """Return ``(nodeid, summary)`` pairs in first-seen order, one per id.

    A failure repeated across matrix jobs appears once; the first occurrence's
    summary is kept.
    """
    seen: dict[str, str] = {}
    for raw_line in log.splitlines():
        match = _FAILED_LINE.search(_ANSI_SGR.sub("", raw_line).rstrip())
        if match is None:
            continue
        nodeid = match["nodeid"]
        if nodeid in seen or not _is_repo_relative(nodeid):
            continue
        summary = (match["summary"] or "").strip()[:SUMMARY_MAX_CHARS]
        seen[nodeid] = summary
    return list(seen.items())


def format_failing_tests(
    entries: list[tuple[str, str]], cap: int = FAILING_TESTS_CAP
) -> str:
    """Render ``entries`` under a ``Failing tests:`` heading, capped at ``cap``.

    Returns ``""`` when there is nothing to name, so the caller keeps its
    message unchanged.
    """
    if not entries:
        return ""
    lines = ["Failing tests:"]
    for nodeid, summary in entries[:cap]:
        lines.append(f"- {nodeid} - {summary}" if summary else f"- {nodeid}")
    overflow = len(entries) - cap
    if overflow > 0:
        lines.append(f"... and {overflow} more")
    return "\n".join(lines)
