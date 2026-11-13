"""Mechanical guards for .github/workflows/codeql.yml.

CodeQL runs on every pull request and holds `security-events: write`, so two
properties are checked mechanically: every action is pinned to a full
40-character commit SHA (a tag is a mutable ref), and the workflow grants
exactly the three scopes code scanning needs and nothing more.

PyYAML is not a dependency of this repo, so the workflow is read as text.
"""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "codeql.yml"

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


def _permissions(text: str) -> dict[str, str]:
    lines = _uncommented(text)
    start = lines.index("permissions:")
    granted: dict[str, str] = {}
    for line in lines[start + 1:]:
        if line and not line.startswith(" "):
            break
        if ":" in line:
            key, _, value = line.strip().partition(":")
            granted[key.strip()] = value.strip()
    return granted


def test_every_action_is_pinned_to_a_full_sha() -> None:
    refs = _uses_refs(_text())
    assert refs, "codeql.yml declares no `uses:` steps"
    offenders = [ref for ref in refs if not PINNED.fullmatch(ref)]
    assert offenders == [], (
        "every action must be pinned to a full 40-char commit SHA, "
        f"offenders: {offenders}"
    )


def test_permissions_are_exactly_the_scopes_code_scanning_needs() -> None:
    assert _permissions(_text()) == {
        "contents": "read",
        "security-events": "write",
        "actions": "read",
    }


def test_it_scans_python_and_actions() -> None:
    assert re.search(r"language:\s*\[\s*python\s*,\s*actions\s*\]", _text()), (
        "codeql.yml must analyse both `python` and `actions`"
    )


def test_it_runs_on_pull_requests_master_pushes_and_a_schedule() -> None:
    on_block = _text().split("\njobs:", 1)[0]
    assert "pull_request" in on_block
    assert "master" in on_block
    assert "cron:" in on_block


def test_pin_guard_rejects_a_tag_reference() -> None:
    assert PINNED.fullmatch("github/codeql-action/init@v4") is None


def test_permissions_scanner_reports_an_extra_scope() -> None:
    synthetic = (
        "permissions:\n"
        "  contents: read\n"
        "  packages: write\n"
        "\n"
        "jobs:\n"
        "  analyze:\n"
    )
    assert _permissions(synthetic)["packages"] == "write"
