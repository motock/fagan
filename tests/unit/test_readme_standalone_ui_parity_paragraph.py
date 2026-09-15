"""Tests pinning the README's standalone-mode UI/API parity paragraph.

The paragraph under ``### Running standalone (dashboard + scheduler, no MCP
server)`` used to end its first sentence with a false conclusion::

    ... so the pipeline can run without registering an MCP server at all:
    drive it from the dashboard UI and let the scheduler advance ready
    stories on its own.

The parity claim itself is TRUE (the dashboard's HTTP API really does expose
every MCP operation) and must be preserved verbatim. What is false is the
"drive it from the dashboard UI" conclusion: the dashboard *UI* has no direct
control for save/decompose/dispatch/advance/review/approve-merge. The
replacement paragraph keeps the parity claim, relocates it to the HTTP API,
enumerates what the UI actually surfaces, names the UI-less API routes, and
points at the scheduler as the standalone driver.

These tests are RED until that paragraph is replaced. They are deliberately
scoped to the standalone section: they never pin a hash of README.md, never
pin the file's total contents, and never assert the H2/H3 heading lists
(those are pinned by tests/unit/test_readme_standalone_section.py and
tests/unit/test_readme_reference_split.py, which must stay green unmodified).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

README_PATH = Path(__file__).resolve().parents[2] / "README.md"

STANDALONE_HEADING_RE = re.compile(r"^### .*standalone.*$", re.IGNORECASE | re.MULTILINE)
NEXT_HEADING_RE = re.compile(r"^#{2,3} ", re.MULTILINE)

SUPPORTED_PATH_MARKER = "The supported path is one command:"

#: The exact replacement paragraph (whitespace-normalized; the source is
#: hard-wrapped, so line breaks are not significant).
EXPECTED_PARAGRAPH = (
    "The dashboard exposes the same operations as the MCP tools — save/ingest a "
    "plan, decompose a goal, dispatch a story, advance, review, approve merge — so "
    "the pipeline can run without registering an MCP server at all. That parity "
    "lives at the HTTP API, not in the UI: the dashboard UI directly surfaces chat "
    "(including drafting a plan), browsing plans, stories, journals and logs, the "
    "workspace picker, the worktree-patch review/apply flow, role configuration, "
    "and ingesting a saved plan. Dispatch, advance, review and approve-merge have "
    "UI-less API routes (`/api/plans/{plan_name}/stories/{story_key}/dispatch` and "
    "friends) available for scripting, and for the standalone flow the scheduler is "
    "the intended driver: draft and ingest a plan from the dashboard, then let the "
    "scheduler dispatch, advance, review and merge ready stories on its own."
)

#: The first sentence is TRUE and must survive the edit byte-for-byte.
EXPECTED_FIRST_SENTENCE = (
    "The dashboard exposes the same operations as the MCP tools — save/ingest a "
    "plan, decompose a goal, dispatch a story, advance, review, approve merge — so "
    "the pipeline can run without registering an MCP server at all."
)

#: The false conclusion that must be gone.
FALSE_UI_CLAIM = "drive it from the dashboard UI"

#: Every other paragraph of the standalone section must be left exactly as it
#: is. Asserted as substring membership (not a whole-file match) so later
#: stories may still append new paragraphs to the section.
OTHER_PARAGRAPHS = (
    (
        "`up` provisions a scratch data dir (default `~/pipeline-standalone`), writes "
        "the shared operator env file with absolute paths, starts the dashboard and the "
        "scheduler through their existing helper scripts, and then refuses to report "
        "success until `GET /api/health` answers with an empty `config_mismatch` and "
        "the intended `plan_dir`. Main options: `--data-dir DIR` (default "
        "`~/pipeline-standalone`), `--target-repo DIR` (default: a scratch repo under "
        "the data dir), `--port PORT` (default 8001), `--autonomy MODE` (default "
        "`dry-run`), plus `--repo-root` and `--force`. `down` stops both processes and "
        "leaves the scratch data in place; `status` prints the resolved paths and both "
        "processes' state."
    ),
    (
        "Both long-running processes read the same operator env file: "
        "`scripts/dashboard.sh` and `scripts/scheduler.sh` both source "
        "`.pipeline.env` (gitignored; see `.pipeline.env.example`) first, then "
        "`.dashboard.env` (gitignored; see `.dashboard.env.example`) second, so "
        "existing dashboard-only installs keep their current last-write precedence — "
        "`.dashboard.env` still works and simply overrides `.pipeline.env` where they "
        "overlap."
    ),
    (
        "Because the dashboard and the scheduler are separate processes, `PLAN_DIR` "
        "must match between the two: the scheduler writes a config fingerprint to "
        "`<plan_dir>/.scheduler_health.json`, and `/api/health` reports "
        "`config_mismatch` listing the fields where the dashboard's resolved config "
        "differs from that fingerprint. A non-empty `config_mismatch` means the UI and "
        "the scheduler are working different plan stores — check that both were "
        "started with the same `PLAN_DIR` (the standalone script writes one env file "
        "for exactly this reason, and fails hard on a non-empty `config_mismatch`)."
    ),
    (
        "The normal prerequisites still apply in standalone mode: `gh auth login` for "
        "the PR/merge path (the pipeline opens and merges PRs through the GitHub CLI), "
        "and provider authorization for whichever backend is configured — see "
        "**Provider selection & authorization** above."
    ),
)

PINNED_TOKENS = (
    ".pipeline.env",
    ".dashboard.env",
    "PLAN_DIR",
    "config_mismatch",
    "/api/health",
    "gh auth login",
    "~/pipeline-standalone",
    "scripts/standalone-setup.sh",
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _normalize(text: str) -> str:
    """Collapse all whitespace runs (incl. hard wraps) to single spaces."""
    return re.sub(r"\s+", " ", text).strip()


def _readme_text() -> str:
    if not README_PATH.exists():
        pytest.fail(f"README.md not found at expected location: {README_PATH}")
    text = README_PATH.read_text(encoding="utf-8")
    assert text.strip(), "README.md exists but is empty"
    return text


def _section_text(text: str) -> str:
    """Body of the '### ... standalone ...' subsection, up to the next heading."""
    match = STANDALONE_HEADING_RE.search(text)
    assert match, "no '### ' heading mentioning standalone found in README.md"
    rest = text[match.end():]
    nxt = NEXT_HEADING_RE.search(rest)
    return rest[: nxt.start()] if nxt else rest


def _paragraph_before_marker(section: str) -> str:
    """The paragraph preceding the marker, whitespace-normalized.

    The source is hard-wrapped, so the marker itself may be split across two
    lines; normalize before searching.
    """
    normalized = _normalize(section)
    idx = normalized.find(SUPPORTED_PATH_MARKER)
    assert idx != -1, (
        f"marker {SUPPORTED_PATH_MARKER!r} not found in the standalone section"
    )
    return normalized[:idx]


def _section() -> str:
    return _section_text(_readme_text())


def _paragraph() -> str:
    return _normalize(_paragraph_before_marker(_section()))


# --------------------------------------------------------------------------
# helper boundary cases (synthetic input, not the real README)
# --------------------------------------------------------------------------


def test_normalize_collapses_whitespace_and_strips() -> None:
    assert _normalize("  a\n\tb   c  ") == "a b c"


def test_section_extraction_stops_at_the_next_heading() -> None:
    text = (
        "## Top\n\n### Running standalone (x)\n\nBody one.\n\n"
        "### Another\n\nNot standalone.\n\n## Next\n"
    )
    section = _section_text(text)
    assert "Body one." in section
    assert "Not standalone." not in section
    assert "## Next" not in section


def test_section_extraction_requires_a_standalone_heading() -> None:
    with pytest.raises(AssertionError):
        _section_text("## Top\n\n### Something else\n\nBody.\n")


def test_paragraph_extraction_handles_hard_wrapped_text() -> None:
    section = (
        "\nThe dashboard exposes the same operations as the MCP tools —\n"
        "save/ingest a plan.\n\nThe supported path is one command:\n"
    )
    assert _normalize(_paragraph_before_marker(section)) == (
        "The dashboard exposes the same operations as the MCP tools — "
        "save/ingest a plan."
    )


def test_paragraph_extraction_requires_the_marker() -> None:
    with pytest.raises(AssertionError):
        _paragraph_before_marker("no marker here")


# --------------------------------------------------------------------------
# the paragraph itself
# --------------------------------------------------------------------------


def test_standalone_section_exists() -> None:
    assert STANDALONE_HEADING_RE.search(_readme_text()), (
        "the '### Running standalone ...' subsection must still exist"
    )


def test_standalone_paragraph_matches_expected_replacement() -> None:
    actual = _paragraph()
    assert actual == EXPECTED_PARAGRAPH, (
        "the standalone paragraph was not replaced with the specified text\n"
        f" got:      {actual}\n expected: {EXPECTED_PARAGRAPH}"
    )


def test_first_sentence_parity_claim_is_preserved() -> None:
    paragraph = _paragraph()
    assert paragraph.startswith(EXPECTED_FIRST_SENTENCE), (
        "the TRUE first sentence (MCP/dashboard parity) must be preserved "
        f"verbatim; paragraph starts: {paragraph[:160]!r}"
    )


def test_false_ui_claim_is_removed() -> None:
    section = _normalize(_section())
    assert FALSE_UI_CLAIM not in section, (
        f"the false conclusion {FALSE_UI_CLAIM!r} must be removed from the "
        "standalone section"
    )
    assert "drive it from the dashboard UI and let the scheduler" not in section


def test_parity_is_relocated_to_the_http_api_not_the_ui() -> None:
    paragraph = _paragraph()
    assert "That parity lives at the HTTP API, not in the UI" in paragraph, (
        "the paragraph must say where the parity actually lives (HTTP API, "
        "not the UI)"
    )


@pytest.mark.parametrize(
    "surface",
    [
        "chat (including drafting a plan)",
        "browsing plans, stories, journals and logs",
        "the workspace picker",
        "the worktree-patch review/apply flow",
        "role configuration",
        "ingesting a saved plan",
    ],
)
def test_ui_surfaces_are_enumerated(surface: str) -> None:
    assert surface in _paragraph(), (
        f"the paragraph must name the UI surface {surface!r}"
    )


def test_ui_less_api_routes_are_named() -> None:
    paragraph = _paragraph()
    assert "UI-less API routes" in paragraph, (
        "the paragraph must state that dispatch/advance/review/approve-merge "
        "have UI-less API routes"
    )
    assert "/api/plans/{plan_name}/stories/{story_key}/dispatch" in paragraph, (
        "the paragraph must name the dispatch route as the example"
    )
    assert "available for scripting" in paragraph


def test_scheduler_is_named_as_the_standalone_driver() -> None:
    paragraph = _paragraph()
    assert "the scheduler is the intended driver" in paragraph
    assert "draft and ingest a plan from the dashboard" in paragraph
    assert (
        "let the scheduler dispatch, advance, review and merge ready stories on "
        "its own" in paragraph
    )


def test_dispatch_advance_review_merge_are_not_claimed_mcp_only() -> None:
    section = _normalize(_section())
    for claim in ("MCP-only", "only via MCP", "only through the MCP"):
        assert claim not in section, (
            f"the section must not claim {claim!r}: dispatch/advance/review/"
            "approve-merge are reachable over the HTTP API"
        )
    assert not re.search(r"(does not|doesn't|no longer)\s+(expose|have)", section), (
        "the parity claim must not be negated"
    )


def test_supported_path_marker_immediately_follows_the_paragraph() -> None:
    section = _normalize(_section())
    assert section.startswith(f"{EXPECTED_PARAGRAPH} {SUPPORTED_PATH_MARKER}"), (
        "the replacement paragraph must be followed immediately by "
        f"{SUPPORTED_PATH_MARKER!r}"
    )


# --------------------------------------------------------------------------
# regexes the surrounding tests rely on
# --------------------------------------------------------------------------


def test_mcp_parity_regex_still_matches() -> None:
    assert re.search(r"same\s+(operations|tools|actions)", _section()), (
        "test_dashboard_mcp_parity_documented requires the section to keep "
        "matching same\\s+(operations|tools|actions)"
    )


def test_no_mcp_server_regex_still_matches() -> None:
    assert re.search(r"without registering|no MCP server", _section()), (
        "test_pipeline_runs_without_registering_an_mcp_server requires a match "
        "for without registering | no MCP server"
    )


# --------------------------------------------------------------------------
# structure guards: no new headings, nothing else touched
# --------------------------------------------------------------------------


def test_standalone_section_adds_no_new_h3_heading() -> None:
    stray = [
        line for line in _section().splitlines() if line.startswith("### ")
    ]
    assert not stray, (
        f"the standalone section must not gain a new '### ' heading: {stray}; "
        "edit the existing paragraph in place"
    )


def test_standalone_section_adds_no_new_h2_heading() -> None:
    stray = [line for line in _section().splitlines() if line.startswith("## ")]
    assert not stray, f"the standalone section must not gain a '## ' heading: {stray}"


@pytest.mark.parametrize("paragraph", OTHER_PARAGRAPHS)
def test_other_standalone_paragraphs_are_unchanged(paragraph: str) -> None:
    assert _normalize(paragraph) in _normalize(_section()), (
        "every other paragraph of the standalone section must be left exactly "
        f"as it is; missing: {paragraph[:80]!r}..."
    )


@pytest.mark.parametrize("token", PINNED_TOKENS)
def test_pinned_standalone_tokens_are_still_present(token: str) -> None:
    assert token in _section(), f"the standalone section must still mention {token!r}"


def test_standalone_setup_command_is_still_documented() -> None:
    assert "scripts/standalone-setup.sh up" in _section()
