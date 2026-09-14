"""CSS treatment for markdown rendered inside Comms chat bubbles.

The prerequisite story (``static/app/render/markdown.js``) emits BARE semantic
elements - no classes, no inline styles - inside the existing ``.msg .bubble``
container:

    p h1 h2 h3 h4 ul ol li pre code table thead tbody tr th td blockquote
    strong em a

This story is CSS-only: it appends scoped rules to ``static/style.css`` so
those elements are readable in the chat panel. Every selector must be scoped
under ``.msg .bubble`` because the renderer emits no classes to hook onto.

These tests read ``static/style.css`` as plain text, following the established
pattern in ``tests/unit/test_dashboard_comms_css.py`` and
``tests/unit/test_comms_landing_redesign.py``.

CUMULATIVE-ARTIFACT RULE: ``static/style.css`` is a shared file that many
stories append to. These tests assert MEMBERSHIP of the selectors this story
adds and the properties those rules must declare - never a full-file hash,
exact length, or byte-content equality.
"""
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
STYLE_CSS = REPO_ROOT / "static" / "style.css"

# Exactly the selectors this story introduces, all scoped under .msg .bubble.
SCOPED_SELECTORS = [
    ".msg .bubble h1",
    ".msg .bubble h2",
    ".msg .bubble h3",
    ".msg .bubble h4",
    ".msg .bubble ul",
    ".msg .bubble ol",
    ".msg .bubble li",
    ".msg .bubble pre",
    ".msg .bubble code",
    ".msg .bubble table",
    ".msg .bubble thead",
    ".msg .bubble th",
    ".msg .bubble td",
    ".msg .bubble blockquote",
    ".msg .bubble a",
]

# A pre-existing rule near the end of the Comms section; new rules are appended
# after it (the story's hard editing constraint is end-of-file appends only).
_APPEND_ANCHOR = ".msg.tower.denied .bubble"

_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}", re.DOTALL)
_HEX_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b")

_BORDER_PROPS = (
    "border",
    "border-left",
    "border-right",
    "border-top",
    "border-bottom",
    "border-width",
    "border-style",
    "border-color",
)


def _css_source():
    assert STYLE_CSS.exists(), "static/style.css must exist"
    return STYLE_CSS.read_text(encoding="utf-8")


def _normalize(selector):
    return re.sub(r"\s+", " ", selector.strip())


def _rules(css):
    """Yield (normalized selector list, declaration body) for every flat rule."""
    for match in _RULE_RE.finditer(_COMMENT_RE.sub("", css)):
        yield _normalize(match.group(1)), match.group(2)


def _selector_bodies(css, selector):
    """Declaration bodies of every rule whose selector list contains `selector`."""
    target = _normalize(selector)
    return [
        body
        for sel, body in _rules(css)
        if target in [_normalize(part) for part in sel.split(",")]
    ]


def _body(css, selector):
    return "\n".join(_selector_bodies(css, selector))


def _has_selector(css, selector):
    return bool(_selector_bodies(css, selector))


def _decl(body, prop):
    """Last declared value for `prop` (exact name, not a prefix), or None."""
    match = re.findall(
        r"(?<![\w-])" + re.escape(prop) + r"\s*:\s*([^;}]*)", body, re.IGNORECASE
    )
    return match[-1].strip() if match else None


def _has_prop(body, prop):
    return _decl(body, prop) is not None


def _has_prop_prefix(body, prefix):
    return re.search(
        r"(?<![\w-])" + re.escape(prefix) + r"[a-z-]*\s*:", body, re.IGNORECASE
    ) is not None


def _has_background(body):
    return _has_prop(body, "background") or _has_prop(body, "background-color")


def _has_border(body):
    return any(_has_prop(body, prop) for prop in _BORDER_PROPS)


def _is_mono(body):
    value = _decl(body, "font-family")
    return bool(value and "--font-mono" in value)


def _scrolls_x(body):
    value = _decl(body, "overflow-x")
    if value and value.strip().lower() == "auto":
        return True
    value = _decl(body, "overflow")
    return bool(value and "auto" in value.lower())


@pytest.fixture(scope="module")
def css():
    return _css_source()


@pytest.mark.parametrize("selector", SCOPED_SELECTORS)
def test_scoped_selector_is_present(css, selector):
    """Every markdown element the renderer emits gets a .msg .bubble rule."""
    assert _has_selector(css, selector), (
        f"static/style.css must define a {selector!r} rule"
    )


@pytest.mark.parametrize("tag", ["h1", "h2", "h3", "h4"])
def test_headings_get_size_and_weight(css, tag):
    selector = f".msg .bubble {tag}"
    body = _body(css, selector)
    assert body, f"{selector} rule must exist"
    assert _has_prop(body, "font-size"), f"{selector} must set font-size"
    assert _has_prop(body, "font-weight"), f"{selector} must set font-weight"


@pytest.mark.parametrize("tag", ["ul", "ol"])
def test_lists_get_markers_and_left_padding(css, tag):
    selector = f".msg .bubble {tag}"
    body = _body(css, selector)
    assert body, f"{selector} rule must exist"
    assert _has_prop_prefix(body, "list-style"), (
        f"{selector} must declare a list-style marker"
    )
    assert _has_prop(body, "padding-left") or _has_prop(body, "padding"), (
        f"{selector} must set left padding"
    )


def test_fenced_code_block_is_monospace_bordered_and_scrollable(css):
    selector = ".msg .bubble pre"
    body = _body(css, selector)
    assert body, f"{selector} rule must exist"
    assert _is_mono(body), f"{selector} must use var(--font-mono)"
    assert _has_background(body), f"{selector} must set a distinct background"
    assert _has_border(body), f"{selector} must set a border"
    assert _scrolls_x(body), f"{selector} must set overflow-x: auto"


def test_inline_code_is_monospace_with_subtle_background(css):
    selector = ".msg .bubble code"
    body = _body(css, selector)
    assert body, f"{selector} rule must exist"
    assert _is_mono(body), f"{selector} must use var(--font-mono)"
    assert _has_background(body), f"{selector} must set a subtle background"


def test_table_borders_collapse_and_scrolls_horizontally(css):
    selector = ".msg .bubble table"
    body = _body(css, selector)
    assert body, f"{selector} rule must exist"
    assert _has_border(body), f"{selector} must set a border"
    assert (_decl(body, "border-collapse") or "").lower() == "collapse", (
        f"{selector} must set border-collapse: collapse"
    )
    assert (_decl(body, "display") or "").lower() == "block", (
        f"{selector} must set display: block"
    )
    assert _scrolls_x(body), f"{selector} must set overflow-x: auto"


def test_table_header_and_cells_are_bordered(css):
    th = _body(css, ".msg .bubble th")
    assert th, ".msg .bubble th rule must exist"
    assert _has_background(th), ".msg .bubble th must set a header background"
    assert _has_border(th), ".msg .bubble th must set a border"
    td = _body(css, ".msg .bubble td")
    assert td, ".msg .bubble td rule must exist"
    assert _has_border(td), ".msg .bubble td must set a border"
    assert _has_selector(css, ".msg .bubble thead"), (
        ".msg .bubble thead rule must exist"
    )


def test_blockquote_has_left_border_and_indent(css):
    selector = ".msg .bubble blockquote"
    body = _body(css, selector)
    assert body, f"{selector} rule must exist"
    assert _has_prop(body, "border-left") or _has_border(body), (
        f"{selector} must set a left border"
    )
    assert any(
        _has_prop(body, prop)
        for prop in ("padding-left", "margin-left", "padding", "margin")
    ), f"{selector} must indent its content"


def test_links_use_the_existing_accent_color(css):
    selector = ".msg .bubble a"
    body = _body(css, selector)
    assert body, f"{selector} rule must exist"
    color = _decl(body, "color")
    assert color and "--accent" in color, (
        f"{selector} must use the existing accent link color"
    )


@pytest.mark.parametrize("selector", SCOPED_SELECTORS)
def test_new_rules_reuse_existing_tokens_not_new_hex(css, selector):
    """The palette already defines every needed token; no new hex literals."""
    body = _body(css, selector)
    assert body, f"{selector} rule must exist"
    assert not _HEX_RE.search(body), (
        f"{selector} must reuse existing CSS variables, not a new hex color"
    )


def test_new_rules_are_appended_after_existing_bubble_rules(css):
    assert _APPEND_ANCHOR in css, (
        f"expected pre-existing anchor {_APPEND_ANCHOR!r} in style.css"
    )
    anchor_index = css.index(_APPEND_ANCHOR)
    for selector in SCOPED_SELECTORS:
        index = css.find(selector)
        assert index != -1, f"{selector} must be present in style.css"
        assert index > anchor_index, (
            f"{selector} must be appended after the existing "
            f"{_APPEND_ANCHOR!r} rule"
        )
