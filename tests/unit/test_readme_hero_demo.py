"""The README's first screen shows the demo GIF and the one-line install (HERO-1)."""

import hashlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
README = REPO_ROOT / "README.md"
DEMO_GIF = REPO_ROOT / "docs" / "screenshots" / "demo.gif"
DEMO_GIF_SHA256 = "0b2bb8d7204a39b2d88d0e24e421a0248e2a54b0f97af79b82550e9d9812ed44"
INSTALL_CMD = (
    "curl -fsSL https://raw.githubusercontent.com/motock/fagan/master/"
    "scripts/remote-install.sh | bash"
)


def _first_screen() -> str:
    """README text before the first `## ` heading."""
    text = README.read_text(encoding="utf-8")
    return text.split("\n## ", 1)[0]


def test_demo_gif_is_the_staged_time_lapse():
    assert hashlib.sha256(DEMO_GIF.read_bytes()).hexdigest() == DEMO_GIF_SHA256


def test_demo_gif_is_a_gif_under_five_megabytes():
    data = DEMO_GIF.read_bytes()
    assert data[:6] == b"GIF89a"
    assert len(data) < 5 * 1024 * 1024


def test_first_screen_embeds_the_demo_gif_with_alt_text():
    screen = _first_screen()
    assert "](docs/screenshots/demo.gif)" in screen
    assert "![](docs/screenshots/demo.gif)" not in screen


def test_demo_gif_comes_right_after_the_tagline():
    screen = _first_screen()
    tagline = screen.index("**Spend tokens on judgment, not typing.**")
    gif = screen.index("](docs/screenshots/demo.gif)")
    intro = screen.index("Frontier models cost money per token")
    assert tagline < gif < intro


def test_first_screen_has_the_one_line_install():
    assert INSTALL_CMD in _first_screen()


def test_first_screen_points_to_the_quickstart():
    assert "(#quickstart)" in _first_screen()


def test_quickstart_section_is_still_present():
    text = README.read_text(encoding="utf-8")
    assert "\n## Quickstart\n" in text
    assert "\n### One-line install\n" in text
