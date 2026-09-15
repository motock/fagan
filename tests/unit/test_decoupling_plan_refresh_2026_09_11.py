"""Acceptance oracle: PLATFORM_DECOUPLING_AND_SCALE_PLAN.md must carry an
accurate status line and record the config-unification workstream.

The doc's substance is sound -- W4 really is the only open workstream -- but
its status header is dated 2026-09-07 while the body was edited 2026-09-09,
and `config-unification` (17 stories, merged 2026-09-10) is absent even
though .pipeline.env, standalone mode and the config-fingerprint work sit
squarely in W3's scope.

This grades structure and evidence tokens, never exact prose.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DOC = REPO_ROOT / "docs" / "plans" / "PLATFORM_DECOUPLING_AND_SCALE_PLAN.md"


def _text() -> str:
    assert DOC.exists(), f"decoupling plan doc missing: {DOC}"
    return DOC.read_text(encoding="utf-8")


def _status_header(text: str) -> str:
    """The leading block-quote status paragraph, up to the first blank line
    that is not itself part of the quote."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("> Status:"))
    block = []
    for line in lines[start:]:
        if not line.startswith(">"):
            break
        block.append(line)
    return "\n".join(block)


# Anchors for claims the refresh must not disturb.
SURVIVING_CLAIMS = (
    "server-app-file-split",
    "workspace-picker-wiring",
    "b1-sandbox-and-harness-seam",
    "a4-non-author-usability",
    "## Workstream W4 — Local vs. enterprise",
)


class TestStatusHeaderIsCurrent:
    def test_status_header_is_current(self):
        header = _status_header(_text())
        match = re.search(r"last updated (\d{4}-\d{2}-\d{2})", header)
        assert match and match.group(1) >= "2026-09-14", (
            "the status header must carry a date current as of the 2026-09-14 "
            "refresh (a literal date pin would break on every future refresh; "
            "grade recency, not the exact date)"
        )

    def test_stale_2026_09_07_status_date_is_gone(self):
        header = _status_header(_text())
        assert "last updated 2026-09-07" not in header, (
            "the stale 2026-09-07 status date must be replaced"
        )

    def test_w4_is_still_named_as_the_only_open_workstream(self):
        header = _status_header(_text())
        assert "W4" in header, (
            "W4 is still the only open workstream and must stay named as such"
        )


class TestConfigUnificationIsRecorded:
    def test_config_unification_plan_is_named(self):
        assert "config-unification" in _text(), (
            "the config-unification plan (17 stories, 2026-09-10) is unrecorded"
        )

    def test_config_unification_records_its_story_count(self):
        text = _text()
        window = text[text.index("config-unification") - 400 :][:1200]
        assert "17 stories" in window, (
            "record config-unification's 17-story scope next to its mention"
        )

    def test_config_unification_cites_a_pr_number(self):
        text = _text()
        window = text[text.index("config-unification") - 400 :][:1200]
        assert re.search(r"#6[45][0-9]", window), (
            "cite at least one of config-unification's PRs (#643-#661)"
        )

    def test_shared_env_file_is_named(self):
        text = _text()
        assert ".pipeline.env" in text, (
            "the shared operator env file .pipeline.env must be named"
        )


class TestExistingClaimsSurvive:
    def test_completed_workstream_claims_are_not_deleted(self):
        text = _text()
        for claim in SURVIVING_CLAIMS:
            assert claim in text, (
                f"{claim!r} documents already-landed work and must survive"
            )

    def test_no_checkbox_list_is_introduced(self):
        text = _text()
        assert not re.search(r"^- \[[ xX]\] ", text, re.MULTILINE), (
            "this doc tracks status in prose, not checkboxes; do not add any"
        )
