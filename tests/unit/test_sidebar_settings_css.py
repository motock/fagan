"""Tests for the SYSTEM sidebar settings group CSS (static/style.css only).

SIDEBAR-3 styles the SYSTEM settings group in the sidebar and gives the
settings content slot (#config-view / #workspace-view) a flex:1 body so it
fills the remaining viewport height.

The rules must live in a dedicated block inserted immediately BEFORE the
existing ``/* Config view styles */`` marker. tests/unit/test_dashboard_comms_css.py
hashes everything from that marker to EOF, so appending the block at the end of
the file would break SUFFIX_FROM_CONFIG_SHA256. This test therefore anchors on
the settings-group marker first and only then looks for the Config marker, so a
block appended at EOF is rejected instead of being paired with the earlier
marker.
"""
import re
from pathlib import Path

import pytest

STATIC_DIR = Path(__file__).resolve().parents[2] / "static"
STYLE_CSS = STATIC_DIR / "style.css"

SETTINGS_MARKER = "/* Sidebar settings group (SYSTEM) */"
CONFIG_MARKER = "/* Config view styles */"

# The exact rules the story requires, whitespace-normalized.
EXPECTED_RULES = [
    ".plan-item.settings-item { color: var(--text-dim); border-bottom: none; }",
    ".plan-item.settings-item .plan-name { color: var(--text-dim); }",
    ".plan-item.settings-item.active { border-left-color: var(--accent); }",
    "#config-view, #workspace-view { flex: 1; }",
]

# Any invented color literal (rgb/rgba/hsl/hsla) is forbidden: the new rules
# must use design tokens only.
COLOR_LITERAL_RE = re.compile(r"\b(?:rgb|rgba|hsl|hsla)\s*\(", re.IGNORECASE)


def _normalize(text: str) -> str:
    """Collapse all runs of whitespace to single spaces and strip."""
    return re.sub(r"\s+", " ", text).strip()


def _settings_block(css_text: str) -> str:
    """Return the CSS between the settings marker and the following Config marker.

    The Config marker is searched for only *after* the settings marker, so a
    block appended at EOF (i.e. after the Config marker) can never satisfy this
    helper.
    """
    start = css_text.find(SETTINGS_MARKER)
    assert start != -1, (
        f"missing {SETTINGS_MARKER!r} marker in static/style.css"
    )
    body_start = start + len(SETTINGS_MARKER)
    end = css_text.find(CONFIG_MARKER, body_start)
    assert end != -1, (
        f"missing {CONFIG_MARKER!r} marker after {SETTINGS_MARKER!r}; the "
        "settings block must be inserted immediately before the Config marker, "
        "not appended at the end of the file"
    )
    return css_text[body_start:end]


@pytest.fixture(scope="module")
def css_text() -> str:
    assert STYLE_CSS.is_file(), f"missing stylesheet: {STYLE_CSS}"
    return STYLE_CSS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def settings_block(css_text: str) -> str:
    return _settings_block(css_text)


def test_style_css_exists():
    assert STYLE_CSS.is_file()


def test_settings_marker_present_exactly_once(css_text):
    assert css_text.count(SETTINGS_MARKER) == 1, (
        f"expected exactly one {SETTINGS_MARKER!r} marker"
    )


def test_settings_block_precedes_config_marker(css_text):
    settings_at = css_text.find(SETTINGS_MARKER)
    config_at = css_text.find(CONFIG_MARKER)
    assert settings_at != -1 and config_at != -1
    assert settings_at < config_at, (
        "the settings block must be inserted before the Config view styles marker"
    )


@pytest.mark.parametrize("expected_rule", EXPECTED_RULES)
def test_expected_rule_present(settings_block, expected_rule):
    assert _normalize(expected_rule) in _normalize(settings_block), (
        f"missing rule in the settings block: {expected_rule}"
    )


def test_settings_item_rule_uses_dim_token_and_no_bottom_border(settings_block):
    rule = ".plan-item.settings-item { color: var(--text-dim); border-bottom: none; }"
    assert _normalize(rule) in _normalize(settings_block)


def test_settings_item_plan_name_rule_present(settings_block):
    rule = ".plan-item.settings-item .plan-name { color: var(--text-dim); }"
    assert _normalize(rule) in _normalize(settings_block)


def test_settings_item_active_rule_present(settings_block):
    rule = ".plan-item.settings-item.active { border-left-color: var(--accent); }"
    assert _normalize(rule) in _normalize(settings_block)


def test_settings_content_slot_flex_rule_present(settings_block):
    rule = "#config-view, #workspace-view { flex: 1; }"
    assert _normalize(rule) in _normalize(settings_block)


def test_settings_block_uses_no_invented_color_literal(settings_block):
    matches = COLOR_LITERAL_RE.findall(settings_block)
    assert matches == [], (
        "the settings block must use design tokens, not invented color "
        f"literals; found: {matches}"
    )
