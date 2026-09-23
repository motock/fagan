"""Tests for the toast long-text CSS fix (static/style.css only).

The plan-completion notification's message is a multi-line, "\\n"-joined plan
summary (pipeline/plan_summary.py's ``format_plan_summary``). It renders badly
in the 320px toast: newlines collapse into one run-on line, a long URL
overflows the box, the key column wraps into a tall stack, and ``align-items:
center`` vertically centres the short key against a tall multi-line message.

This story is CSS-only: it edits exactly three adjacent rules in the
``/* Toast stack styles */`` block of static/style.css and nothing else.
"""
import hashlib
import re
from pathlib import Path

import pytest

STATIC_DIR = Path(__file__).resolve().parents[2] / "static"
STYLE_CSS = STATIC_DIR / "style.css"
COMMS_CSS_TEST = Path(__file__).resolve().parent / "test_dashboard_comms_css.py"

# The digest this story's brief prescribes for the edited toast region. The
# region pin lives in test_dashboard_comms_css.py; this is the value it must
# carry after the re-pin.
EXPECTED_TOAST_REGION_SHA256 = (
    "ac70e1f2f7b8589264ec773f87e80e4f57b26b9c544cbf112027aebfa5b661e8"
)


class RuleNotFoundError(AssertionError):
    """Raised when a selector has no rule in the stylesheet."""


def _selector_match(css_text: str, selector: str):
    pattern = re.compile(r"(?<![\w-])" + re.escape(selector) + r"\s*\{")
    matches = list(pattern.finditer(css_text))
    return matches[0] if matches else None


def find_rule_body(css_text: str, selector: str) -> str:
    """Return the raw text between a selector's own `{` and matching `}`."""
    match = _selector_match(css_text, selector)
    if match is None:
        raise RuleNotFoundError(f"no rule found for selector {selector!r}")
    depth = 1
    i = match.end()
    n = len(css_text)
    while i < n and depth > 0:
        if css_text[i] == "{":
            depth += 1
        elif css_text[i] == "}":
            depth -= 1
        i += 1
    if depth != 0:
        raise ValueError(f"unterminated rule body for selector {selector!r}")
    return css_text[match.end():i - 1]


@pytest.fixture(scope="module")
def css_text() -> str:
    return STYLE_CSS.read_text()


# --- the three edited rules -------------------------------------------------

def test_toast_row_aligns_items_to_the_top(css_text):
    body = find_rule_body(css_text, ".toast-row")
    assert "align-items: flex-start" in body, (
        ".toast-row must align items to the top so the short key sits at the "
        "top of a tall multi-line message instead of being centred"
    )
    assert "align-items: center" not in body, (
        "the old align-items: center must be gone"
    )


def test_toast_key_does_not_wrap_or_shrink(css_text):
    body = find_rule_body(css_text, ".toast-key")
    # Pre-existing treatment must survive.
    assert "font-family: var(--font-mono)" in body
    assert "var(--fs-xs)" in body
    assert "var(--stripe, var(--text-muted))" in body
    # New: the key is a short identifier and must not be squeezed or wrapped.
    assert "flex-shrink: 0" in body
    assert "white-space: nowrap" in body


def test_toast_msg_can_shrink_and_wrap_long_tokens(css_text):
    body = find_rule_body(css_text, ".toast-msg")
    assert "flex: 1" in body
    # min-width: 0 is the actual overflow fix: a flex item's default
    # min-width: auto refuses to shrink below its longest unbreakable token.
    assert "min-width: 0" in body
    assert "overflow-wrap: anywhere" in body
    # pre-line honours the summary's newlines while collapsing space runs.
    assert "white-space: pre-line" in body
    assert "overflow-y: auto" in body
    assert "white-space: nowrap" not in body, (
        "nowrap would defeat pre-line and re-collapse the summary's newlines"
    )


def test_toast_msg_is_bounded_in_height(css_text):
    body = find_rule_body(css_text, ".toast-msg")
    assert "max-height: 40vh" in body, (
        "a long summary must scroll within the toast rather than run off screen"
    )


# --- negative controls: the fix must not leak into the survivors -------------

def test_severity_stripe_survives(css_text):
    body = find_rule_body(css_text, ".toast")
    assert "border-left: 3px solid var(--stripe, var(--text-muted))" in body
    assert "box-shadow: var(--shadow-2), -3px 0 8px -3px var(--stripe, transparent)" in body


def test_toast_stack_width_unchanged(css_text):
    body = find_rule_body(css_text, ".toast-stack")
    assert "width: 320px" in body, (
        "the fix belongs inside the toast; .toast-stack must not be widened"
    )
    assert "width: 100%" not in body, (
        "the 100% width belongs to the max-width: 640px override, not the base rule"
    )
    assert re.search(
        r"@media\s*\(max-width:\s*640px\)\s*\{[^}]*\.toast-stack\s*\{[^}]*width:\s*100%",
        css_text,
    ), "the max-width: 640px .toast-stack override must survive"


def test_toast_key_is_not_used_for_wrapping(css_text):
    body = find_rule_body(css_text, ".toast-key")
    assert "overflow-wrap" not in body, (
        "the key must not wrap; overflow-wrap belongs on .toast-msg only"
    )


def test_other_toast_rules_survive(css_text):
    assert "@keyframes toast-in" in css_text
    assert re.search(
        r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{\s*\.toast\s*\{\s*animation:\s*none",
        css_text,
    ), "the prefers-reduced-motion .toast override must survive"
    assert "cursor: pointer" in find_rule_body(css_text, ".toast-dismiss")
    assert "color: var(--accent)" in find_rule_body(css_text, ".toast-dismiss:hover")
    assert "outline: 2px solid var(--accent)" in find_rule_body(
        css_text, ".toast-dismiss:focus-visible"
    )
    assert "display: flex" in find_rule_body(css_text, ".toast-actions")
    assert "border: 1px solid var(--accent)" in find_rule_body(css_text, ".toast-ask")
    assert "background: var(--accent-soft)" in find_rule_body(css_text, ".toast-ask:hover")


# --- the re-pinned region guard ---------------------------------------------

def test_toast_region_pin_is_truthful(css_text):
    """The re-pinned digest must match the region it guards, byte for byte."""
    source = COMMS_CSS_TEST.read_text()
    match = re.search(
        r'^TOAST_REGION_SHA256\s*=\s*"([0-9a-f]{64})"', source, re.MULTILINE
    )
    assert match, "TOAST_REGION_SHA256 must still be a pinned hex digest"
    pinned = match.group(1)
    assert pinned == EXPECTED_TOAST_REGION_SHA256, (
        "the toast region pin must be re-pinned to the digest this story's "
        "CSS produces, never hand-written from memory"
    )
    # A comment line must sit directly above the constant, in the style of its
    # neighbours, noting the re-pin.
    lines = source.splitlines()
    const_idx = next(
        i for i, line in enumerate(lines) if line.startswith("TOAST_REGION_SHA256")
    )
    assert lines[const_idx - 1].lstrip().startswith("#"), (
        "the re-pinned constant needs a comment line above it"
    )
    toast_idx = css_text.find("/* Toast stack styles */")
    assert toast_idx != -1, "missing '/* Toast stack styles */' section comment"
    msg_match = _selector_match(css_text, ".msg")
    assert msg_match is not None, "expected a '.msg' rule to bound the toast region"
    region = css_text[toast_idx:msg_match.start()]
    assert hashlib.sha256(region.encode()).hexdigest() == pinned, (
        "the pinned digest must match the actual toast region byte for byte"
    )
