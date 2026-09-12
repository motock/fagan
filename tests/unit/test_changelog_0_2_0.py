"""Tests for the `## [0.2.0]` section of the root CHANGELOG.md.

The story brief requires a new release section documenting everything merged to
master since the `v0.1.0` tag, placed ABOVE the existing `## [0.1.0]` section
(newest first, per Keep a Changelog - which the file's own header says it
follows), with `### Added` / `### Fixed` subsections carrying bolded sub-group
labels, matching the 0.1.0 entry's structure and 80-column wrap.

CHANGELOG.md is a cumulative artifact: future releases add more sections above
this one. These tests therefore assert only what THIS story adds - membership
of the 0.2.0 heading, its subsections and its documented themes, plus the
ordering of 0.2.0 relative to the fixed 0.1.0 anchor. They never assert the
file's total section list, an exact section count, or a byte hash (see
.claude/rules/pipeline-story-schema.md on cumulative artifacts).

They are RED until the 0.2.0 section exists.
"""

from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

# The 0.1.0 release date, used as a fixed lower bound for the 0.2.0 date.
V0_1_0_DATE = _dt.date(2026, 9, 11)

# The existing 0.1.0 entry wraps at 80 columns; the new entry must match.
MAX_LINE_WIDTH = 80

# `## [0.2.0] - 2026-09-12`
HEADING_RE = re.compile(
    r"^## \[(?P<version>[^\]]+)\](?: - (?P<date>\d{4}-\d{2}-\d{2}))?\s*$"
)

# PR numbers the git log gives for each theme documented in the 0.2.0 section.
# (theme label, PR reference) - the label is only used in failure messages.
REQUIRED_PR_REFS = [
    ("CHANGELOG introduced", "#681"),
    ("ADR practice started", "#682"),
    ("streaming chat / SSE", "#685"),
    ("canonical agent.log grammar", "#687"),
    ("per-backend usage reporting", "#688"),
    ("POST /api/chat/stream endpoint", "#695"),
    ("dashboard consumes the SSE stream", "#707"),
    ("plan-completion notifications", "#710"),
    ("stale-activity watchdog floor", "#711"),
]


def _lines() -> list[str]:
    assert CHANGELOG.is_file(), (
        f"CHANGELOG.md must exist at the repo root ({REPO_ROOT}) before its "
        "0.2.0 section can be checked"
    )
    return CHANGELOG.read_text(encoding="utf-8").splitlines()


def _heading_indices(lines: list[str], version: str) -> list[int]:
    """0-based indices of `## [version]` heading lines (prefix match)."""
    prefix = f"## [{version}]"
    return [i for i, line in enumerate(lines) if line.startswith(prefix)]


def _section_lines(lines: list[str], version: str) -> list[str]:
    """Body lines of the `## [version]` section, excluding its heading.

    The section runs until the next `## [` heading (or EOF), so a later release
    appended above this one does not leak into the slice.
    """
    starts = _heading_indices(lines, version)
    assert starts, (
        f"CHANGELOG.md must contain a '## [{version}]' release heading line "
        "(matched on the prefix, not the whole line, so the date can change)"
    )
    start = starts[0]
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## ["):
            end = i
            break
    return lines[start + 1 : end]


def _section_text(lines: list[str], version: str) -> str:
    return "\n".join(_section_lines(lines, version))


def _subsection_lines(lines: list[str], version: str, name: str) -> list[str]:
    """Body lines of a `### name` subsection inside the `## [version]` section."""
    body = _section_lines(lines, version)
    starts = [i for i, line in enumerate(body) if line.strip() == f"### {name}"]
    assert starts, (
        f"the '## [{version}]' section must contain a '### {name}' subsection"
    )
    start = starts[0]
    end = len(body)
    for i in range(start + 1, len(body)):
        if body[i].startswith("### "):
            end = i
            break
    return body[start + 1 : end]


def _subsection_text(lines: list[str], version: str, name: str) -> str:
    return "\n".join(_subsection_lines(lines, version, name))


def _assert_tokens(text: str, tokens: list[str], where: str) -> None:
    missing = [token for token in tokens if token not in text]
    assert not missing, (
        f"{where} must mention {missing!r}; the git log v0.1.0..HEAD shows "
        "these landed since the 0.1.0 tag"
    )


# --------------------------------------------------------------------------
# Heading, placement and structure
# --------------------------------------------------------------------------


def test_changelog_has_exactly_one_0_2_0_heading():
    """`grep -c '^## \\[0.2.0\\]' CHANGELOG.md` must return 1."""
    lines = _lines()
    matching = [line for line in lines if line.startswith("## [0.2.0]")]
    assert len(matching) == 1, (
        "CHANGELOG.md must contain exactly one '## [0.2.0]' release heading "
        f"line; found {len(matching)}: {matching!r}"
    )


def test_0_2_0_heading_carries_a_release_date():
    """The heading must be `## [0.2.0] - YYYY-MM-DD` with a plausible date."""
    lines = _lines()
    indices = _heading_indices(lines, "0.2.0")
    assert indices, "CHANGELOG.md must contain a '## [0.2.0]' heading line"
    heading = lines[indices[0]]
    match = HEADING_RE.match(heading)
    assert match, (
        f"the 0.2.0 heading must be '## [0.2.0] - YYYY-MM-DD' (matching the "
        f"0.1.0 entry's format); got {heading!r}"
    )
    raw_date = match.group("date")
    assert raw_date, (
        f"the 0.2.0 heading must carry a release date in YYYY-MM-DD form; "
        f"got {heading!r}"
    )
    released = _dt.date.fromisoformat(raw_date)
    assert released >= V0_1_0_DATE, (
        f"the 0.2.0 release date ({released.isoformat()}) must not predate the "
        f"0.1.0 release ({V0_1_0_DATE.isoformat()})"
    )
    # One day of slack so a local-vs-UTC clock difference cannot fail this.
    latest = _dt.datetime.now(tz=_dt.timezone.utc).date() + _dt.timedelta(days=1)
    assert released <= latest, (
        f"the 0.2.0 release date ({released.isoformat()}) must be the date the "
        "work is done, not a future date"
    )


def test_0_2_0_section_is_above_the_0_1_0_section():
    """Newest first: the 0.2.0 heading must appear before the 0.1.0 heading."""
    lines = _lines()
    new = _heading_indices(lines, "0.2.0")
    old = _heading_indices(lines, "0.1.0")
    assert new, "CHANGELOG.md must contain a '## [0.2.0]' heading line"
    assert old, "CHANGELOG.md must still contain its '## [0.1.0]' heading line"
    assert new[0] < old[0], (
        "the '## [0.2.0]' section must be placed ABOVE the '## [0.1.0]' "
        f"section (newest first); 0.2.0 is at line {new[0] + 1} and 0.1.0 at "
        f"line {old[0] + 1}"
    )


def test_0_2_0_section_has_added_and_fixed_subsections():
    """The 0.2.0 section must carry both `### Added` and `### Fixed`."""
    lines = _lines()
    for name in ("Added", "Fixed"):
        body = _subsection_lines(lines, "0.2.0", name)
        assert body, f"the 0.2.0 '### {name}' subsection must not be empty"


def test_0_2_0_subsections_use_bolded_subgroup_labels():
    """House style: each subsection groups its bullets under a bolded label."""
    lines = _lines()
    for name in ("Added", "Fixed"):
        body = _subsection_lines(lines, "0.2.0", name)
        labels = [
            line for line in body if re.match(r"^\*\*[^*]+\*\*\s*$", line.strip())
        ]
        assert labels, (
            f"the 0.2.0 '### {name}' subsection must group its bullets under a "
            "bolded sub-group label (e.g. '**Orchestration**'), matching the "
            "0.1.0 entry's structure"
        )


def test_0_2_0_subsections_have_bullet_content():
    """Each subsection must actually list changes, not just a heading."""
    lines = _lines()
    for name in ("Added", "Fixed"):
        body = _subsection_lines(lines, "0.2.0", name)
        bullets = [line for line in body if line.startswith("- ")]
        assert len(bullets) >= 3, (
            f"the 0.2.0 '### {name}' subsection must list at least three "
            f"changes as '- ' bullets; found {len(bullets)}"
        )


def test_0_2_0_section_wraps_at_80_columns():
    """The 0.1.0 entry wraps at 80 columns; the new entry must match."""
    lines = _lines()
    body = _section_lines(lines, "0.2.0")
    too_long = [(i, len(line)) for i, line in enumerate(body) if len(line) > MAX_LINE_WIDTH]
    assert not too_long, (
        f"every line of the 0.2.0 section must wrap at {MAX_LINE_WIDTH} "
        f"columns like the 0.1.0 entry; over-long lines (offset, width): "
        f"{too_long}"
    )


# --------------------------------------------------------------------------
# Added: the user-facing work merged since v0.1.0
# --------------------------------------------------------------------------


def test_0_2_0_added_documents_streaming_chat():
    """SSE-00..03 + #707: the SSE endpoint and the dashboard consuming it."""
    text = _subsection_text(_lines(), "0.2.0", "Added")
    _assert_tokens(
        text,
        ["/api/chat/stream", "stream_turn", "SSE"],
        "the 0.2.0 '### Added' subsection (streaming chat)",
    )


def test_0_2_0_added_documents_per_backend_usage_reporting():
    """USE-01..03: collect_backend_status / resource_status per backend."""
    text = _subsection_text(_lines(), "0.2.0", "Added")
    _assert_tokens(
        text,
        ["collect_backend_status", "resource_status"],
        "the 0.2.0 '### Added' subsection (per-backend usage reporting)",
    )


def test_0_2_0_added_documents_plan_completion_notifications():
    """PLANNOTIFY-01..07: plan_completed -> per-plan outbox -> SMTP."""
    text = _subsection_text(_lines(), "0.2.0", "Added")
    _assert_tokens(
        text,
        ["plan_completed", "outbox", "SMTP"],
        "the 0.2.0 '### Added' subsection (plan-completion notifications)",
    )


def test_0_2_0_added_documents_canonical_agent_log_grammar():
    """LOG-01..05: agent_log_format, ClaudeCliDriver, raw NDJSON sidecar."""
    text = _subsection_text(_lines(), "0.2.0", "Added")
    _assert_tokens(
        text,
        ["agent_log_format", "ClaudeCliDriver", "NDJSON"],
        "the 0.2.0 '### Added' subsection (canonical agent.log grammar)",
    )


# --------------------------------------------------------------------------
# Fixed: the reliability work merged since v0.1.0
# --------------------------------------------------------------------------


def test_0_2_0_fixed_documents_plan_lock_starvation():
    """LOCKSTARVE-A2/A3/B1..B4/C1/D2/D3/E1/E2."""
    text = _subsection_text(_lines(), "0.2.0", "Fixed")
    _assert_tokens(
        text,
        ["dispatch lease", ".agent_done", "reconcile_fn", "launchd"],
        "the 0.2.0 '### Fixed' subsection (plan-lock starvation of the merge gate)",
    )


def test_0_2_0_fixed_documents_the_watchdog_activity_floor():
    """#711: floor the stale-activity age at the current dispatch's elapsed time."""
    text = _subsection_text(_lines(), "0.2.0", "Fixed")
    _assert_tokens(
        text,
        ["watchdog", "#711"],
        "the 0.2.0 '### Fixed' subsection (stale-activity watchdog floor)",
    )


def test_0_2_0_fixed_documents_the_done_bar_narrowing():
    """cf48767: the done-bar no longer narrows to a story's own new test file."""
    text = _subsection_text(_lines(), "0.2.0", "Fixed")
    assert "done-bar" in text or "done_bar" in text, (
        "the 0.2.0 '### Fixed' subsection must document the done-bar no longer "
        "narrowing to a story's own new test file"
    )
    assert "cf48767" in text, (
        "the 0.2.0 '### Fixed' subsection must reference commit cf48767 for the "
        "done-bar fix"
    )


# --------------------------------------------------------------------------
# PR references and the two "also worth a line" items
# --------------------------------------------------------------------------


def test_0_2_0_section_references_the_pr_numbers_from_the_log():
    """The log gives PR numbers for every theme; the entry must cite them."""
    text = _section_text(_lines(), "0.2.0")
    missing = [
        f"{pr} ({theme})" for theme, pr in REQUIRED_PR_REFS if pr not in text
    ]
    assert not missing, (
        "the 0.2.0 section must reference the PR numbers the git log "
        f"v0.1.0..HEAD gives; missing: {missing}"
    )


def test_0_2_0_section_documents_the_adr_practice():
    """#682 landed after v0.1.0: docs/adr/ index, template and first records."""
    text = _section_text(_lines(), "0.2.0")
    _assert_tokens(
        text,
        ["docs/adr", "#682"],
        "the 0.2.0 section (the ADR practice started after v0.1.0)",
    )


def test_0_2_0_section_documents_the_changelog_introduction():
    """#681 landed after v0.1.0: the CHANGELOG itself was introduced."""
    text = _section_text(_lines(), "0.2.0")
    assert "#681" in text, (
        "the 0.2.0 section must reference #681, which introduced CHANGELOG.md "
        "after the v0.1.0 tag"
    )


# --------------------------------------------------------------------------
# The existing 0.1.0 section must be untouched
# --------------------------------------------------------------------------


def test_0_1_0_section_is_preserved():
    """No part of the existing 0.1.0 section may be edited or deleted."""
    lines = _lines()
    text = _section_text(lines, "0.1.0")
    _assert_tokens(
        text,
        [
            "### Added",
            "### Known limitations",
            "**Orchestration**",
            "**Cost-tiered execution**",
            "**Quality gates**",
            "**Isolation**",
            "**Operations**",
            "MCP server exposing the pipeline to any MCP client",
            "Merge gate that rebases onto the default branch",
            "A green test suite is not proof of a correct or complete change.",
        ],
        "the existing 0.1.0 section (it must not be edited or deleted)",
    )
    assert "v0.1.0" in "\n".join(lines), (
        "CHANGELOG.md must keep its v0.1.0 release-tag link"
    )


def test_changelog_header_is_preserved():
    """The file's Keep a Changelog header must survive the new section."""
    text = "\n".join(_lines())
    assert "Keep a Changelog" in text, (
        "CHANGELOG.md must keep its 'Keep a Changelog' header reference"
    )
