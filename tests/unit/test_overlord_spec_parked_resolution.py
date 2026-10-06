"""Guards for HK-2: the exported overlord policy spec must carry the live
parked-story policy, and README must not overstate the always-hold floor.

Text-only assertions: no counts, no line numbers.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC = REPO_ROOT / "docs" / "specs" / "OVERLORD_POLICY_SPEC.md"
README = REPO_ROOT / "README.md"


def _spec_text() -> str:
    return SPEC.read_text(encoding="utf-8")


def test_spec_has_parked_story_resolution_heading() -> None:
    headings = [line for line in _spec_text().splitlines() if line.startswith("#")]
    assert any("Parked-story resolution" in line for line in headings)


def test_spec_mentions_split_story() -> None:
    assert "split_story" in _spec_text()


def test_spec_mentions_patch_acceptance() -> None:
    assert "patch_acceptance" in _spec_text()


def test_spec_mentions_mark_done() -> None:
    assert "mark_done" in _spec_text()


def test_spec_mentions_post_hoc_audit() -> None:
    assert "post-hoc" in _spec_text()


def test_readme_drops_always_parked_phrase() -> None:
    assert "Always parked regardless of autonomy level" not in README.read_text(
        encoding="utf-8"
    )


def test_spec_keeps_tier_3_heading() -> None:
    assert "### 3.3 Tier 3" in _spec_text()
