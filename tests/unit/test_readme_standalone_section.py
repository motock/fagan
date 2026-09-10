"""Tests pinning the README's standalone-mode documentation.

These tests are RED until the README gains a ``### `` subsection (under an
EXISTING ``## `` heading) documenting standalone mode:

* the dashboard exposes the same operations as the MCP tools, so the pipeline
  can run without registering an MCP server at all;
* ``scripts/standalone-setup.sh up`` as the supported path, with its main
  options and the ``~/pipeline-standalone`` default;
* ``.pipeline.env`` as the shared operator env file read by BOTH the dashboard
  and the scheduler, with ``.dashboard.env`` still working and sourced after
  it;
* ``PLAN_DIR`` must match between the two processes, ``/api/health`` reports
  ``config_mismatch``, and a non-empty ``config_mismatch`` means the UI and
  the scheduler are working different plan stores;
* the prerequisites that still apply: ``gh auth login`` for the PR/merge path
  and provider authorization for whichever backend is configured.

The landmine guard (``test_h2_headings_unchanged_regression``) must pass both
before and after the edit: the README's ``## `` heading list is pinned to
exactly 11 headings, matching tests/unit/test_readme_reference_split.py.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

README_PATH = Path(__file__).resolve().parents[2] / "README.md"

#: The exact H2 list pinned by tests/unit/test_readme_reference_split.py.
#: The standalone-mode story MUST NOT add, remove, rename or reorder any of
#: these; its content goes in a ``### `` subsection instead.
EXPECTED_H2_HEADINGS = [
    "Platform support",
    "Quickstart",
    "Components at a glance",
    "Architecture",
    "Personas (`~/.claude/agents/`)",
    "The overlord and the decision policy",
    "Reference",
    "Prerequisites",
    "Scheduler",
    "Reliability & limitations",
    "License",
]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _readme_lines() -> list[str]:
    if not README_PATH.exists():
        pytest.fail(f"README.md not found at expected location: {README_PATH}")
    text = README_PATH.read_text(encoding="utf-8")
    assert text.strip(), "README.md exists but is empty"
    return text.splitlines()


def _fenced_line_numbers(lines: list[str]) -> set[int]:
    """Line numbers inside ``` fences, so '## ' in code blocks never counts."""
    fenced: set[int] = set()
    in_fence = False
    for i, line in enumerate(lines):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            fenced.add(i)
            continue
        if in_fence:
            fenced.add(i)
    return fenced


def _headings(
    lines: list[str], fenced: set[int], prefix: str
) -> list[tuple[int, str]]:
    """(line_index, heading_text) for headings at the given level, outside fences."""
    out = []
    for i, line in enumerate(lines):
        if i in fenced:
            continue
        if line.startswith(prefix) and not line.startswith(prefix + "#"):
            out.append((i, line[len(prefix) :].strip()))
    return out


def _h2_headings(lines: list[str], fenced: set[int]) -> list[str]:
    return [text for _, text in _headings(lines, fenced, "## ")]


def _standalone_section_lines(lines: list[str], fenced: set[int]) -> list[str]:
    """The body of the H3 subsection whose heading mentions 'standalone'.

    Runs from that H3 line up to (not including) the next heading of any
    level (## or ###) outside a code fence.
    """
    h3s = _headings(lines, fenced, "### ")
    starts = [i for i, text in h3s if "standalone" in text.lower()]
    assert starts, "no '### ' heading mentioning standalone found in README.md"
    start = starts[0]
    all_heading_lines = sorted(
        [i for i, _ in h3s] + [i for i, _ in _headings(lines, fenced, "## ")]
    )
    end = next((i for i in all_heading_lines if i > start), len(lines))
    return lines[start:end]


# --------------------------------------------------------------------------
# REGRESSION: the landmine guard — must pass before AND after the edit
# --------------------------------------------------------------------------


def test_h2_headings_unchanged_regression() -> None:
    """The README's H2 heading list is pinned: exactly 11, exact order."""
    lines = _readme_lines()
    fenced = _fenced_line_numbers(lines)
    h2s = _h2_headings(lines, fenced)
    assert len(h2s) == 11, (
        f"README.md must keep exactly 11 '## ' headings (got {len(h2s)}): {h2s}"
    )
    assert h2s == EXPECTED_H2_HEADINGS, (
        "README.md '## ' headings were renamed/reordered — the pinned list "
        f"changed:\n got:      {h2s}\n expected: {EXPECTED_H2_HEADINGS}"
    )


def test_standalone_content_adds_no_h2_of_its_own() -> None:
    """The standalone subsection body must not introduce any '## ' heading."""
    lines = _readme_lines()
    fenced = _fenced_line_numbers(lines)
    section = _standalone_section_lines(lines, fenced)
    stray = [line for line in section if line.startswith("## ")]
    assert not stray, (
        f"standalone subsection introduced new '## ' headings: {stray}; "
        "add content as '### ' subsections under an existing '## ' only"
    )


# --------------------------------------------------------------------------
# Heading parser boundary cases (synthetic input, not the real README)
# --------------------------------------------------------------------------


def test_heading_parser_ignores_h2_inside_code_fence() -> None:
    """A '## ' line inside a ``` fence must not count as a heading."""
    lines = ["## Real", "```bash", "## Not A Heading", "```", "## Also Real"]
    fenced = _fenced_line_numbers(lines)
    assert _h2_headings(lines, fenced) == ["Real", "Also Real"]


def test_heading_parser_distinguishes_h2_from_h3() -> None:
    """'### ' lines must never be counted as '## ' headings (and vice versa)."""
    lines = ["## H2 One", "### H3 Under It", "## H2 Two", "### Another H3"]
    fenced = _fenced_line_numbers(lines)
    assert _h2_headings(lines, fenced) == ["H2 One", "H2 Two"]
    h3s = [text for _, text in _headings(lines, fenced, "### ")]
    assert h3s == ["H3 Under It", "Another H3"]


def test_heading_parser_on_empty_input() -> None:
    """Empty / whitespace-only input yields no headings and no crash."""
    assert _h2_headings([], set()) == []
    assert _h2_headings(["", "   "], set()) == []


# --------------------------------------------------------------------------
# The standalone-mode documentation itself (RED until the README is edited)
# --------------------------------------------------------------------------


def test_readme_has_exactly_one_standalone_h3_heading() -> None:
    """README.md contains a '### ' heading whose text mentions standalone."""
    lines = _readme_lines()
    fenced = _fenced_line_numbers(lines)
    matches = [
        text
        for _, text in _headings(lines, fenced, "### ")
        if "standalone" in text.lower()
    ]
    assert matches, (
        "README.md has no '### ' heading mentioning standalone — add e.g. "
        "'### Standalone mode (dashboard + scheduler, no MCP server)' under "
        "an existing '## ' heading"
    )
    assert len(matches) == 1, (
        f"expected exactly one standalone '### ' heading, got {matches}"
    )


def test_standalone_section_lives_under_quickstart() -> None:
    """The standalone H3 sits inside the '## Quickstart' section.

    Chosen home: '## Quickstart' — standalone mode is an alternative setup
    path, and Quickstart already hosts the sibling '### ' setup subsections
    (Provider selection & authorization, Getting-started walkthrough).
    """
    lines = _readme_lines()
    fenced = _fenced_line_numbers(lines)
    h2s = _headings(lines, fenced, "## ")
    quickstart_idx = next(
        (i for i, text in h2s if text == "Quickstart"), None
    )
    assert quickstart_idx is not None, "'## Quickstart' heading missing"
    next_h2_idx = next(
        (i for i, _ in h2s if i > quickstart_idx), len(lines)
    )
    section = _standalone_section_lines(lines, fenced)
    section_start = lines.index(section[0])
    assert quickstart_idx < section_start < next_h2_idx, (
        "the standalone '### ' subsection must live under '## Quickstart' "
        f"(between lines {quickstart_idx + 1} and {next_h2_idx + 1}); "
        f"found it starting at line {section_start + 1}"
    )


def test_standalone_section_mentions_standalone_setup_script() -> None:
    """README mentions scripts/standalone-setup.sh."""
    lines = _readme_lines()
    fenced = _fenced_line_numbers(lines)
    section = "\n".join(_standalone_section_lines(lines, fenced))
    assert "scripts/standalone-setup.sh" in section, (
        "the standalone section must reference scripts/standalone-setup.sh"
    )


def test_standalone_setup_up_is_the_supported_path() -> None:
    """'standalone-setup.sh up' is documented as the supported path."""
    lines = _readme_lines()
    fenced = _fenced_line_numbers(lines)
    section = "\n".join(_standalone_section_lines(lines, fenced))
    assert re.search(r"standalone-setup\.sh\s+up\b", section), (
        "the standalone section must document 'scripts/standalone-setup.sh up' "
        "as the supported path"
    )


def test_pipeline_standalone_default_dir_documented() -> None:
    """The ~/pipeline-standalone default install dir is documented."""
    lines = _readme_lines()
    fenced = _fenced_line_numbers(lines)
    section = "\n".join(_standalone_section_lines(lines, fenced))
    assert "~/pipeline-standalone" in section, (
        "the standalone section must document the ~/pipeline-standalone default"
    )


def test_pipeline_env_file_documented() -> None:
    """README mentions .pipeline.env as the shared operator env file."""
    lines = _readme_lines()
    fenced = _fenced_line_numbers(lines)
    section = "\n".join(_standalone_section_lines(lines, fenced))
    assert ".pipeline.env" in section, (
        "the standalone section must document .pipeline.env"
    )


def test_env_file_is_read_by_both_dashboard_and_scheduler() -> None:
    """.pipeline.env is described as read by BOTH dashboard and scheduler."""
    lines = _readme_lines()
    fenced = _fenced_line_numbers(lines)
    section = "\n".join(_standalone_section_lines(lines, fenced))
    lowered = section.lower()
    assert "dashboard" in lowered and "scheduler" in lowered, (
        "the standalone section must say .pipeline.env is read by both the "
        "dashboard and the scheduler"
    )


def test_dashboard_env_still_works_and_is_sourced_after_pipeline_env() -> None:
    """.dashboard.env still works and is sourced AFTER .pipeline.env."""
    lines = _readme_lines()
    fenced = _fenced_line_numbers(lines)
    section = "\n".join(_standalone_section_lines(lines, fenced))
    assert ".dashboard.env" in section, (
        "the standalone section must note that .dashboard.env still works"
    )
    assert section.index(".pipeline.env") < section.index(".dashboard.env"), (
        ".dashboard.env must be documented as sourced after .pipeline.env"
    )


def test_dashboard_mcp_parity_documented() -> None:
    """The dashboard exposes the same operations as the MCP tools."""
    lines = _readme_lines()
    fenced = _fenced_line_numbers(lines)
    section = "\n".join(_standalone_section_lines(lines, fenced))
    assert re.search(r"same\s+(operations|tools|actions)", section, re.IGNORECASE), (
        "the standalone section must say the dashboard exposes the same "
        "operations as the MCP tools"
    )


def test_pipeline_runs_without_registering_an_mcp_server() -> None:
    """The pipeline can run without registering an MCP server at all."""
    lines = _readme_lines()
    fenced = _fenced_line_numbers(lines)
    section = "\n".join(_standalone_section_lines(lines, fenced))
    assert re.search(
        r"without registering|no MCP server|without the MCP|without an MCP",
        section,
        re.I,
    ), (
        "the standalone section must say the pipeline can run without "
        "registering an MCP server at all"
    )


def test_plan_dir_must_match_between_processes() -> None:
    """PLAN_DIR must match between the dashboard and the scheduler."""
    lines = _readme_lines()
    fenced = _fenced_line_numbers(lines)
    section = "\n".join(_standalone_section_lines(lines, fenced))
    assert "PLAN_DIR" in section, (
        "the standalone section must warn that PLAN_DIR must match between "
        "the two processes"
    )


def test_api_health_reports_config_mismatch() -> None:
    """/api/health reports config_mismatch."""
    lines = _readme_lines()
    fenced = _fenced_line_numbers(lines)
    section = "\n".join(_standalone_section_lines(lines, fenced))
    assert "config_mismatch" in section, (
        "the standalone section must document that /api/health reports "
        "config_mismatch"
    )
    assert "/api/health" in section, (
        "the standalone section must name the /api/health endpoint"
    )


def test_nonempty_config_mismatch_means_different_plan_stores() -> None:
    """A non-empty config_mismatch means UI and scheduler use different plan stores."""
    lines = _readme_lines()
    fenced = _fenced_line_numbers(lines)
    section = "\n".join(_standalone_section_lines(lines, fenced))
    idx = section.find("config_mismatch")
    assert idx != -1, "config_mismatch missing from the standalone section"
    window = section[idx : idx + 600].lower()
    assert (
        "plan store" in window or "plan stores" in window or "plan_dir" in window
    ), (
        "the standalone section must explain that a non-empty config_mismatch "
        "means the UI and the scheduler are working different plan stores"
    )


def test_gh_auth_login_prerequisite_still_applies() -> None:
    """'gh auth login' is documented as a standalone-mode prerequisite."""
    lines = _readme_lines()
    fenced = _fenced_line_numbers(lines)
    section = "\n".join(_standalone_section_lines(lines, fenced))
    assert "gh auth login" in section, (
        "the standalone section must list 'gh auth login' as a prerequisite "
        "for the PR/merge path"
    )


def test_provider_authorization_prerequisite_still_applies() -> None:
    """Provider authorization for the configured backend is documented."""
    lines = _readme_lines()
    fenced = _fenced_line_numbers(lines)
    section = "\n".join(_standalone_section_lines(lines, fenced))
    assert re.search(r"provider", section, re.I), (
        "the standalone section must mention provider authorization"
    )
    assert re.search(r"authoriz", section, re.I), (
        "the standalone section must mention provider authorization"
    )