"""Markup/CSS-only tests for the new "Comms" view.

This story adds static markup to static/index.html and static/style.css
ONLY - no JS wiring. These tests assert the new DOM elements exist and
the new CSS selectors are present, so a later story can build on them
without re-touching markup. They are intentionally membership/substring
checks (not exact-file equality) because style.css is a shared, cumulative
artifact that sibling stories also extend.
"""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "static"
INDEX_HTML = STATIC / "index.html"
STYLE_CSS = STATIC / "style.css"


def _read_index() -> str:
    assert INDEX_HTML.exists(), "static/index.html must exist"
    return INDEX_HTML.read_text(encoding="utf-8")


def _read_css() -> str:
    assert STYLE_CSS.exists(), "static/style.css must exist"
    return STYLE_CSS.read_text(encoding="utf-8")


# --- index.html: required element ids -------------------------------------

def test_index_html_has_comms_view_section():
    html = _read_index()
    assert 'id="comms-view"' in html


def test_index_html_has_comms_thread():
    html = _read_index()
    assert 'id="comms-thread"' in html


def test_index_html_has_comms_landing():
    html = _read_index()
    assert 'id="comms-landing"' in html


def test_index_html_has_comms_input():
    html = _read_index()
    assert 'id="comms-input"' in html


def test_index_html_has_comms_send():
    html = _read_index()
    assert 'id="comms-send"' in html


def test_index_html_has_on_air():
    html = _read_index()
    assert 'id="on-air"' in html


# --- index.html: comms-view starts hidden via the shared .hidden utility --

def test_comms_view_section_uses_hidden_class():
    """The #comms-view section must start hidden by reusing the existing
    `.hidden { display: none !important; }` utility class, NOT by inventing
    an inline style duplicate."""
    html = _read_index()
    # Find the section opening tag for comms-view.
    m = re.search(r'<section[^>]*id="comms-view"[^>]*>', html)
    assert m, "no <section id=\"comms-view\"> opening tag found"
    opening = m.group(0)
    assert 'class="hidden"' in opening, (
        "comms-view section must declare class=\"hidden\" so it reuses the "
        f"shared utility class; got: {opening!r}"
    )


def test_comms_view_does_not_use_inline_hidden_style():
    """Negative: the section must NOT redeclare the hidden behavior inline
    (which would duplicate the existing utility class)."""
    html = _read_index()
    m = re.search(r'<section[^>]*id="comms-view"[^>]*>', html)
    assert m, "no <section id=\"comms-view\"> opening tag found"
    opening = m.group(0)
    assert "display: none !important" not in opening, (
        "comms-view section redeclares hidden via inline style instead of "
        f"reusing the .hidden utility class; got: {opening!r}"
    )


def test_comms_view_is_sibling_of_plan_detail_inside_main():
    """The new section must live inside <main> as a sibling of the existing
    #plan-detail section (not nested inside it)."""
    html = _read_index()
    main = re.search(r"<main>(.*?)</main>", html, re.DOTALL)
    assert main, "no <main> element found in index.html"
    inner = main.group(1)
    assert 'id="plan-detail"' in inner, "plan-detail must remain inside main"
    assert 'id="comms-view"' in inner, "comms-view must be inside main"
    # comms-view must NOT be nested inside plan-detail.
    plan_detail = re.search(
        r'<section[^>]*id="plan-detail"[^>]*>(.*?)</section>', inner, re.DOTALL
    )
    assert plan_detail, "plan-detail section not found"
    assert 'id="comms-view"' not in plan_detail.group(1), (
        "comms-view must be a sibling of plan-detail, not nested inside it"
    )


# --- index.html: no hand-written pinned nav item --------------------------

def test_index_html_does_not_hand_write_comms_nav_item():
    """The pinned nav item is generated at runtime by app.js (mirroring the
    Overview item built via document.createElement), so index.html must NOT
    hand-write a comms nav item into #plan-list."""
    html = _read_index()
    nav = re.search(r'<nav[^>]*id="plan-list"[^>]*>(.*?)</nav>', html, re.DOTALL)
    assert nav, "no <nav id=\"plan-list\"> found"
    nav_inner = nav.group(1)
    assert "comms" not in nav_inner.lower(), (
        "index.html must not hand-write a comms nav item into #plan-list; "
        "the pinned item is built at runtime by app.js"
    )


# --- style.css: required selectors (membership checks) -------------------

def test_css_has_comms_head():
    assert ".comms-head" in _read_css()


def test_css_has_comms_title():
    assert ".comms-title" in _read_css()


def test_css_has_comms_title_label():
    assert ".comms-title .label" in _read_css()


def test_css_has_on_air():
    assert ".on-air" in _read_css()


def test_css_has_on_air_live():
    assert ".on-air.live" in _read_css()


def test_css_has_on_air_pulse_keyframes():
    """The .on-air.live pulse must use a @keyframes animation, guarded by
    prefers-reduced-motion (mirroring the existing header::after sweep)."""
    css = _read_css()
    assert "@keyframes" in css, "no @keyframes found in style.css"
    assert "prefers-reduced-motion" in css, (
        "prefers-reduced-motion guard missing (must guard the pulse like the "
        "existing header::after sweep animation)"
    )


def test_css_has_comms_sub():
    assert ".comms-sub" in _read_css()


def test_css_has_comms_body():
    assert ".comms-body" in _read_css()


def test_css_has_comms_thread():
    assert ".comms-thread" in _read_css()


def test_css_has_comms_landing():
    assert ".comms-landing" in _read_css()


def test_css_has_comms_landing_h3():
    assert ".comms-landing h3" in _read_css()


def test_css_has_comms_foot():
    assert ".comms-foot" in _read_css()


def test_css_has_comms_input_row():
    assert ".comms-input-row" in _read_css()


def test_css_has_comms_input():
    assert ".comms-input" in _read_css()


def test_css_has_comms_input_focus_visible():
    """The input must use a :focus-visible outline using var(--accent),
    same pattern as .filter-search."""
    css = _read_css()
    assert ".comms-input:focus-visible" in css or ".comms-input :focus-visible" in css
    # The focus rule must reference the accent token (not a color literal).
    # Find the focus-visible rule block and check it mentions --accent.
    m = re.search(r"\.comms-input:focus-visible\s*\{([^}]*)\}", css)
    assert m, "no .comms-input:focus-visible rule block found"
    assert "var(--accent)" in m.group(1), (
        ".comms-input:focus-visible must use var(--accent) for its outline"
    )


def test_css_has_comms_send():
    assert ".comms-send" in _read_css()


def test_css_has_comms_send_hover():
    assert ".comms-send:hover" in _read_css()


def test_css_has_comms_send_focus_visible():
    assert ".comms-send:focus-visible" in _read_css()


def test_css_has_comms_send_disabled():
    assert ".comms-send:disabled" in _read_css()


def test_css_has_msg():
    assert ".msg" in _read_css()


def test_css_has_msg_who():
    assert ".msg .who" in _read_css()


def test_css_has_msg_user_bubble():
    assert ".msg.user .bubble" in _read_css()


def test_css_has_msg_tower_bubble():
    assert ".msg.tower .bubble" in _read_css()


def test_css_has_msg_tower_denied_bubble():
    assert ".msg.tower.denied .bubble" in _read_css()


def test_css_msg_tower_denied_uses_failed_token():
    """The denied bubble's left-stripe must use var(--c-failed), mirroring
    the .card.stale left-stripe-plus-tint pattern."""
    css = _read_css()
    m = re.search(r"\.msg\.tower\.denied\s+\.bubble\s*\{([^}]*)\}", css)
    assert m, "no .msg.tower.denied .bubble rule block found"
    assert "var(--c-failed)" in m.group(1), (
        ".msg.tower.denied .bubble must use var(--c-failed) for its stripe"
    )


def test_css_has_trace_row():
    assert ".trace-row" in _read_css()


def test_css_has_trace_chip():
    assert ".trace-chip" in _read_css()


def test_css_has_trace_chip_hover():
    assert ".trace-chip:hover" in _read_css()


def test_css_has_trace_chip_focus_visible():
    assert ".trace-chip:focus-visible" in _read_css()


def test_css_has_trace_chip_dot():
    assert ".trace-chip .dot" in _read_css()


def test_css_has_trace_detail():
    assert ".trace-detail" in _read_css()


def test_css_has_trace_chip_expanded_plus_trace_detail():
    assert ".trace-chip.expanded + .trace-detail" in _read_css()


def test_css_trace_chip_expanded_shows_detail_block():
    """The expanded+detail rule must set display: block so the detail is
    revealed when a chip is expanded."""
    css = _read_css()
    m = re.search(
        r"\.trace-chip\.expanded\s*\+\s*\.trace-detail\s*\{([^}]*)\}", css
    )
    assert m, "no .trace-chip.expanded + .trace-detail rule block found"
    assert "display: block" in m.group(1), (
        ".trace-chip.expanded + .trace-detail must set display: block"
    )


# --- style.css: no new color literals / tokens ----------------------------

def test_css_does_not_introduce_new_color_literals_in_comms_rules():
    """Comms-related rules must use only existing var(--...) tokens, not
    raw hex/rgb color literals. We scan the comms selector rule blocks for
    bare color literals (#rrggbb / rgb()/rgba()/hsl())."""
    css = _read_css()
    comms_selectors = [
        ".comms-head", ".comms-title", ".comms-sub", ".comms-body",
        ".comms-thread", ".comms-landing", ".comms-foot", ".comms-input-row",
        ".comms-input", ".comms-send", ".msg", ".trace-row", ".trace-chip",
        ".trace-detail",
    ]
    # Collect the text of all rule blocks whose selector starts with one of
    # the comms selectors (rough scan: from the selector to the next '}').
    for sel in comms_selectors:
        for m in re.finditer(re.escape(sel) + r"[^{]*\{([^}]*)\}", css):
            block = m.group(1)
            # No hex literals.
            assert not re.search(r"#[0-9a-fA-F]{3,8}\b", block), (
                f"comms rule for {sel} introduces a hex color literal: "
                f"{block!r}"
            )
            # No rgb()/rgba()/hsl() literals.
            assert not re.search(r"\b(?:rgb|hsl)a?\s*\(", block), (
                f"comms rule for {sel} introduces an rgb/hsl color literal: "
                f"{block!r}"
            )


# --- app.js must NOT be touched by this story -----------------------------

def test_app_js_unchanged_no_comms_view_wiring():
    """This story is markup/CSS ONLY; app.js must not gain comms-view
    view-switching wiring yet (that is a dependent follow-up story). We only
    assert app.js does not reference the comms-view id as a wired view - a
    weak guard so a later story can add it without this test blocking."""
    app_js = STATIC / "app.js"
    assert app_js.exists(), "static/app.js must exist"
    js = app_js.read_text(encoding="utf-8")
    # This story must not add a show/hide switch keyed on comms-view.
    # (A later story will; that's fine - this test is scoped to THIS story.)
    assert 'getElementById("comms-view")' not in js, (
        "this story must not wire comms-view switching in app.js"
    )