"""Pins the claims the standalone guide makes about the install smoke run.

The smoke script ends at ``tests_passed`` (review and merge are out of scope),
collides with a live stack on port 8443 and the ``pipeline-standalone`` compose
project, and the installer writes the notification config that
``STANDALONE_NOTIFY_CONFIG`` points at. These tests read the guide and fail if
any of that drifts.
"""

from __future__ import annotations

import re
from pathlib import Path

GUIDE = Path(__file__).resolve().parents[2] / "docs" / "DEPLOY_STANDALONE.md"

# A growth cap, not a pin: the guide is at 1251 lines and may not grow by more
# than 14 lines. This is deliberately not an exact count.
MAX_LINES = 1265


def _smoke_section(text: str) -> str:
    match = re.search(
        r"^## Standalone install smoke test\b.*?(?=^## |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, "the guide has no '## Standalone install smoke test' section"
    return match.group(0)


def _bullets(block: str) -> list[str]:
    """Group the block into bullets, keeping wrapped continuation lines."""
    bullets: list[str] = []
    current: list[str] | None = None
    for line in block.splitlines():
        if re.match(r"^[-*] ", line):
            if current is not None:
                bullets.append("\n".join(current))
            current = [line]
        elif current is None:
            continue
        elif not line.strip() or line.startswith("#"):
            bullets.append("\n".join(current))
            current = None
        else:
            current.append(line)
    if current is not None:
        bullets.append("\n".join(current))
    return bullets


def test_smoke_section_stops_at_tests_passed() -> None:
    assert "tests_passed" in _smoke_section(GUIDE.read_text(encoding="utf-8"))


def test_smoke_section_does_not_claim_a_terminal_state() -> None:
    assert "to a terminal state" not in _smoke_section(GUIDE.read_text(encoding="utf-8"))


def test_smoke_prerequisites_warn_about_the_live_stack_collision() -> None:
    section = _smoke_section(GUIDE.read_text(encoding="utf-8"))
    tokens = ("8443", "pipeline-standalone", "down -v")
    assert any(all(token in bullet for token in tokens) for bullet in _bullets(section))


def test_guide_documents_the_notification_config() -> None:
    assert "STANDALONE_NOTIFY_CONFIG" in GUIDE.read_text(encoding="utf-8")


def test_guide_has_not_grown_past_the_cap() -> None:
    assert len(GUIDE.read_text(encoding="utf-8").splitlines()) <= MAX_LINES
