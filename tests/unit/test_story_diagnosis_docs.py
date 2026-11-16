"""Pin the prose that documents the story diagnosis tools.

The permission table in ``docs/DEPLOY_STANDALONE.md``, the counts in
``docs/specs/IDENTITY_AND_POLICY.md`` and the README mention are all copies of
what ``pipeline.remote_tool_policy`` decides. These tests read the docs and the
module, so a tool added to (or removed from) either policy set, or a count that
drifts from the set, fails here instead of going stale silently.
"""

from __future__ import annotations

import re
from pathlib import Path

from pipeline.remote_tool_policy import REMOTE_ENFORCED, REMOTE_READ

REPO = Path(__file__).resolve().parents[2]
DEPLOY_DOC = REPO / "docs" / "DEPLOY_STANDALONE.md"
IDENTITY_DOC = REPO / "docs" / "specs" / "IDENTITY_AND_POLICY.md"
README = REPO / "README.md"

NEW_READ_TOOLS = (
    "get_story_log",
    "get_story_journal",
    "get_story_worktree",
    "get_story_events",
)
NEW_ENFORCED_TOOL = "unblock_story"

_UNITS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}
_TENS = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}

# High-confidence secret shapes only: a PEM private-key block, a GitHub or
# OpenAI style token, an AWS access key id, or an HTTP bearer credential.
_SECRET = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,})"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\bBearer\s+\S{20,}",
)


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _table_block(path: Path, needle: str) -> list[str]:
    """The contiguous ``|``-prefixed block of lines that contains *needle*."""
    lines = _lines(path)
    anchors = [
        index
        for index, line in enumerate(lines)
        if needle in line and line.lstrip().startswith("|")
    ]
    assert anchors, f"{path.name} has no table row containing {needle!r}"
    first = last = anchors[0]
    while first > 0 and lines[first - 1].lstrip().startswith("|"):
        first -= 1
    while last + 1 < len(lines) and lines[last + 1].lstrip().startswith("|"):
        last += 1
    return lines[first : last + 1]


def _line_with(path: Path, needle: str) -> str:
    matches = [line for line in _lines(path) if needle in line]
    assert len(matches) == 1, f"{path.name}: expected one line with {needle!r}"
    return matches[0]


def _paragraph_block(path: Path, needle: str) -> str:
    """The contiguous non-blank block of lines that contains *needle*."""
    lines = _lines(path)
    anchors = [index for index, line in enumerate(lines) if needle in line]
    assert anchors, f"{path.name} never mentions {needle!r}"
    first = last = anchors[0]
    while first > 0 and lines[first - 1].strip():
        first -= 1
    while last + 1 < len(lines) and lines[last + 1].strip():
        last += 1
    return " ".join(lines[first : last + 1])


def _number(word: str) -> int:
    """Read a count written as digits, ``six`` or ``twenty-six``."""
    text = word.strip().lower()
    if text.isdigit():
        return int(text)
    total = 0
    for part in text.replace(" and ", "-").split("-"):
        if part in _UNITS:
            total += _UNITS[part]
        elif part in _TENS:
            total += _TENS[part]
        else:  # a form this parser does not know is a failure, not a zero
            raise AssertionError(f"cannot read the number word {part!r}")
    return total


def test_permission_table_names_every_policy_tool() -> None:
    table = "\n".join(_table_block(DEPLOY_DOC, "get_story_diff"))
    missing = sorted(name for name in REMOTE_READ | REMOTE_ENFORCED if name not in table)
    assert not missing, f"docs/DEPLOY_STANDALONE.md permission table omits {missing}"


def test_story_diagnosis_rows_describe_reads_and_the_unblock_mutation() -> None:
    block = _table_block(DEPLOY_DOC, "get_story_diff")
    for name in NEW_READ_TOOLS:
        rows = [line for line in block if name in line]
        assert rows, f"{name} has no row in the permission table"
        row = " ".join(rows).lower()
        assert "redact" in row, f"{name} row does not say the read is redacted"
    unblock = [line for line in block if NEW_ENFORCED_TOOL in line]
    assert unblock, f"{NEW_ENFORCED_TOOL} has no row in the permission table"
    joined = " ".join(unblock).lower()
    assert "approval" in joined, "unblock_story row does not say it resets plan approval"
    prose = "\n".join(block).lower()
    assert "takes a path argument" in prose, "the table does not say the tools take no path"


def test_identity_counts_match_policy_sets() -> None:
    line = _line_with(IDENTITY_DOC, "Remote MCP transport hardening")
    match = re.search(
        r"of those reads\s+([A-Za-z0-9-]+)\s+are allowed and the mutating\s+([A-Za-z0-9-]+)\s+all consult",
        line,
    )
    assert match, "the hardening row no longer states its read and mutating counts"
    stated_reads = _number(match.group(1))
    stated_mutating = _number(match.group(2))
    assert stated_reads == len(REMOTE_READ), (
        f"docs state {stated_reads} allowed reads, the policy has {len(REMOTE_READ)}: "
        f"{sorted(REMOTE_READ)}"
    )
    assert stated_mutating == len(REMOTE_ENFORCED), (
        f"docs state {stated_mutating} enforced mutations, the policy has "
        f"{len(REMOTE_ENFORCED)}: {sorted(REMOTE_ENFORCED)}"
    )


def test_readme_mention_covers_the_story_diagnosis_tools() -> None:
    paragraph = _paragraph_block(README, "get_story_diff")
    missing = [name for name in NEW_READ_TOOLS + (NEW_ENFORCED_TOOL,) if name not in paragraph]
    assert not missing, f"the README paragraph mentioning get_story_diff omits {missing}"
    lowered = paragraph.lower()
    assert "redact" in lowered, "the README paragraph does not say the reads are redacted"
    assert "path argument" in lowered, "the README paragraph does not say the tools take no path"
    assert "approval" in lowered, "the README paragraph does not say unblock_story resets approval"


def test_documented_text_holds_no_secret_values() -> None:
    documented = "\n".join(
        [
            "\n".join(_table_block(DEPLOY_DOC, "get_story_diff")),
            _line_with(IDENTITY_DOC, "Remote MCP transport hardening"),
            _paragraph_block(README, "get_story_diff"),
        ]
    )
    found = _SECRET.findall(documented)
    assert not found, f"documented text carries secret-looking values: {found}"
