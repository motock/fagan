"""Mechanical guards for .github/workflows/dependency-review.yml.

The workflow is the check that fails a pull request for adding a dependency
with a known `high` advisory, so these guards pin its threshold, its READ-ONLY
permission set, its SHA pinning and its checkout credential handling. PyYAML is
not a dependency of this repo, so the workflow is read as text.
"""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOW = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "workflows"
    / "dependency-review.yml"
)

PINNED = re.compile(r"[^@\s]+@[0-9a-f]{40}")
USES = re.compile(r"^\s*(?:- )?uses:\s*(\S+)\s*$")


def _uncommented(text: str) -> list[str]:
    return [re.sub(r"\s+#.*$", "", line) for line in text.splitlines()]


def _text() -> str:
    assert WORKFLOW.is_file(), f"missing {WORKFLOW}"
    return WORKFLOW.read_text(encoding="utf-8")


def _uses_refs(text: str) -> list[str]:
    refs = []
    for line in _uncommented(text):
        match = USES.match(line)
        if match:
            refs.append(match.group(1))
    return refs


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _top_permissions(text: str) -> dict[str, str]:
    lines = _uncommented(text)
    header = lines.index("permissions:")
    granted: dict[str, str] = {}
    for line in lines[header + 1:]:
        if not line.strip():
            continue
        if _indent(line) == 0:
            break
        if ":" in line:
            key, _, value = line.strip().partition(":")
            granted[key.strip()] = value.strip()
    return granted


def test_every_action_is_pinned_to_a_full_sha() -> None:
    refs = _uses_refs(_text())
    assert refs, "dependency-review.yml declares no `uses:` steps"
    offenders = [ref for ref in refs if not PINNED.fullmatch(ref)]
    assert offenders == [], (
        "every action must be pinned to a full 40-char commit SHA, "
        f"offenders: {offenders}"
    )


def test_it_fails_on_high_severity() -> None:
    assert "fail-on-severity: high" in _text()


def test_it_grants_read_only_contents() -> None:
    assert _top_permissions(_text()) == {"contents": "read"}


def test_it_checks_out_without_persisting_credentials() -> None:
    assert "persist-credentials: false" in _text()


def test_it_runs_on_pull_requests_only() -> None:
    on_block = _text().split("\njobs:", 1)[0]
    assert "pull_request" in on_block
    assert "push:" not in on_block, (
        "dependency review grades a base...head diff; a push run has none"
    )


def test_pin_guard_rejects_a_tag_reference() -> None:
    assert PINNED.fullmatch("actions/dependency-review-action@v5") is None


def test_permissions_scanner_reports_a_write_scope() -> None:
    synthetic = (
        "permissions:\n"
        "  contents: read\n"
        "  pull-requests: write\n"
        "\n"
        "jobs:\n"
        "  review:\n"
    )
    assert _top_permissions(synthetic)["pull-requests"] == "write"
