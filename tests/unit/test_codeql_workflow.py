"""Mechanical guards for .github/workflows/codeql.yml.

CodeQL runs on every pull request and needs `security-events: write`, so these
guards check three properties mechanically: every action is pinned to a full
40-character commit SHA (a tag is a mutable ref), the top level grants only
`contents: read`, and the write scope lives on the `analyze` job alone.

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


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _entries(lines: list[str], header: int) -> dict[str, str]:
    indent = _indent(lines[header])
    granted: dict[str, str] = {}
    for line in lines[header + 1:]:
        if not line.strip():
            continue
        if _indent(line) <= indent:
            break
        if ":" in line:
            key, _, value = line.strip().partition(":")
            granted[key.strip()] = value.strip()
    return granted


def _top_permissions(text: str) -> dict[str, str]:
    lines = _uncommented(text)
    return _entries(lines, lines.index("permissions:"))


def _job_permissions(text: str, job: str) -> dict[str, str]:
    lines = _uncommented(text)
    job_start = lines.index(f"  {job}:")
    for index in range(job_start + 1, len(lines)):
        line = lines[index]
        if line.strip() and _indent(line) <= 2:
            break
        if line == "    permissions:":
            return _entries(lines, index)
    return {}


def test_every_action_is_pinned_to_a_full_sha() -> None:
    refs = _uses_refs(_text())
    assert refs, "codeql.yml declares no `uses:` steps"
    offenders = [ref for ref in refs if not PINNED.fullmatch(ref)]
    assert offenders == [], (
        "every action must be pinned to a full 40-char commit SHA, "
        f"offenders: {offenders}"
    )


def test_top_level_grants_only_read_contents() -> None:
    assert _top_permissions(_text()) == {"contents": "read"}


def test_analyze_job_holds_the_code_scanning_write_scope() -> None:
    assert _job_permissions(_text(), "analyze") == {
        "contents": "read",
        "security-events": "write",
    }


def test_it_checks_out_without_persisting_credentials() -> None:
    assert "persist-credentials: false" in _text()


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


def test_top_level_permissions_scanner_reports_a_write_scope() -> None:
    synthetic = (
        "permissions:\n"
        "  contents: read\n"
        "  security-events: write\n"
        "\n"
        "jobs:\n"
        "  analyze:\n"
    )
    assert _top_permissions(synthetic)["security-events"] == "write"


def test_job_permissions_scanner_ignores_a_top_level_write_scope() -> None:
    synthetic = (
        "permissions:\n"
        "  security-events: write\n"
        "\n"
        "jobs:\n"
        "  analyze:\n"
        "    runs-on: ubuntu-latest\n"
    )
    assert _job_permissions(synthetic, "analyze") == {}
