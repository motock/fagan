"""Tests for the dismissable toast stack markup (static/index.html + style.css).

This story adds ONLY markup/CSS - no JS. The toast stack starts empty; a
later story populates it dynamically from static/app.js. These tests assert
the static container and its styling exist in the right place, without
asserting the exact total contents of the shared style.css file (membership
checks only).
"""
from pathlib import Path

import pytest

STATIC_DIR = Path(__file__).resolve().parents[2] / "static"
INDEX_HTML = STATIC_DIR / "index.html"
STYLE_CSS = STATIC_DIR / "style.css"


@pytest.fixture(scope="module")
def index_text():
    return INDEX_HTML.read_text()


@pytest.fixture(scope="module")
def css_text():
    return STYLE_CSS.read_text()


# --- index.html -----------------------------------------------------------


def test_index_html_exists(index_text):
    assert INDEX_HTML.exists(), "static/index.html must exist"


def test_toast_stack_container_present(index_text):
    """The empty toast-stack container must exist with the required attrs."""
    assert 'id="toast-stack"' in index_text
    assert 'aria-live="polite"' in index_text
    # The container element itself must carry both attributes on one element.
    assert 'class="toast-stack" id="toast-stack" aria-live="polite"' in index_text


def test_toast_stack_starts_empty(index_text):
    """The container must be empty (no child toasts baked into markup)."""
    # Find the toast-stack div and assert it has no element children.
    start = index_text.find('id="toast-stack"')
    assert start != -1
    end = index_text.find("</div>", start)
    assert end != -1
    inner = index_text[start:end]
    # No nested elements inside the container.
    assert "<" not in inner.replace("</div", "", 1).replace(">", "", 0)
    # Simpler, robust check: nothing meaningful between open tag and close.
    open_tag_end = index_text.find(">", start)
    between = index_text[open_tag_end + 1:end]
    assert between.strip() == ""


def test_toast_stack_after_header_before_main(index_text):
    """Container must sit after </header> and before <main> (not nested)."""
    header_close = index_text.find("</header>")
    main_open = index_text.find("<main")
    toast = index_text.find('id="toast-stack"')

    assert header_close != -1, "no </header> found"
    assert main_open != -1, "no <main> found"
    assert toast != -1, "no toast-stack found"

    # String-index ordering: </header> < toast-stack < <main>
    assert header_close < toast, "toast-stack must appear AFTER </header>"
    assert toast < main_open, "toast-stack must appear BEFORE <main>"


def test_toast_stack_not_nested_in_header(index_text):
    """The toast-stack must not be nested inside the header element."""
    header_open = index_text.find("<header")
    header_close = index_text.find("</header>")
    toast = index_text.find('id="toast-stack"')
    assert not (header_open < toast < header_close), (
        "toast-stack must not be nested inside <header>"
    )


def test_toast_stack_not_nested_in_main(index_text):
    """The toast-stack must not be nested inside the main element."""
    main_open = index_text.find("<main")
    # find the matching close of main (last </main> in file)
    main_close = index_text.rfind("</main>")
    toast = index_text.find('id="toast-stack"')
    assert not (main_open < toast < main_close), (
        "toast-stack must not be nested inside <main>"
    )


# --- style.css ------------------------------------------------------------


def test_style_css_exists(css_text):
    assert STYLE_CSS.exists(), "static/style.css must exist"


def test_toast_stack_selector_present(css_text):
    assert ".toast-stack" in css_text


def test_toast_stack_positioning(css_text):
    """The .toast-stack rule must fix-position the stack top-right."""
    # locate the .toast-stack rule block
    idx = css_text.find(".toast-stack")
    assert idx != -1
    block = css_text[idx:idx + 400]
    assert "position: fixed" in block
    assert "top: 60px" in block
    assert "right: 20px" in block
    assert "z-index: 10" in block
    assert "display: flex" in block
    assert "flex-direction: column" in block
    assert "gap: 8px" in block
    assert "width: 320px" in block
    assert "max-width: calc(100vw - 40px)" in block


def test_toast_stack_responsive_media_query(css_text):
    """A max-width:640px media query must narrow the stack to avoid overflow."""
    assert "@media (max-width: 640px)" in css_text
    # The media query block must reference .toast-stack
    mq = css_text.find("@media (max-width: 640px)")
    block = css_text[mq:mq + 300]
    assert ".toast-stack" in block


def test_toast_selector_present(css_text):
    assert ".toast {" in css_text


def test_toast_card_convention(css_text):
    """The .toast rule must reuse the card stripe-plus-glow convention."""
    idx = css_text.find(".toast {")
    assert idx != -1
    block = css_text[idx:idx + 500]
    assert "background: var(--panel)" in block
    assert "border: 1px solid var(--border)" in block
    assert "border-left: 3px solid var(--stripe, var(--text-muted))" in block
    assert "border-radius: var(--radius-md)" in block
    assert "box-shadow: var(--shadow-2), -3px 0 8px -3px var(--stripe, transparent)" in block
    assert "padding: 10px 12px" in block


def test_toast_entrance_animation(css_text):
    """A toast-in keyframe animation must exist (fade + translateX)."""
    assert "toast-in" in css_text
    # keyframes definition
    kf = css_text.find("@keyframes toast-in")
    assert kf != -1, "missing @keyframes toast-in"
    block = css_text[kf:kf + 300]
    assert "opacity" in block
    assert "translateX" in block


def test_toast_animation_duration(css_text):
    """The entrance animation should be ~180ms."""
    idx = css_text.find(".toast {")
    block = css_text[idx:idx + 600]
    assert "180ms" in block


def test_toast_reduced_motion_guard(css_text):
    """A prefers-reduced-motion guard must disable .toast animation."""
    guards = [
        i for i in range(len(css_text))
        if css_text.startswith("@media (prefers-reduced-motion", i)
    ]
    assert guards, "no prefers-reduced-motion media query found"
    # at least one such guard block must contain ".toast"
    found = False
    for g in guards:
        # find the closing brace of the media query
        depth = 0
        j = g
        while j < len(css_text):
            if css_text[j] == "{":
                depth += 1
            elif css_text[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        block = css_text[g:j + 1]
        if ".toast" in block:
            found = True
            break
    assert found, (
        "no prefers-reduced-motion guard block contains .toast"
    )
    # And that guard must set animation: none for .toast
    for g in guards:
        depth = 0
        j = g
        while j < len(css_text):
            if css_text[j] == "{":
                depth += 1
            elif css_text[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        block = css_text[g:j + 1]
        if ".toast" in block and "animation: none" in block:
            return
    pytest.fail(
        "prefers-reduced-motion guard for .toast must set animation: none"
    )


def test_toast_row_selector_present(css_text):
    assert ".toast-row" in css_text


def test_toast_key_selector_present(css_text):
    assert ".toast-key" in css_text


def test_toast_key_styling(css_text):
    """toast-key should be font-mono, dim, small."""
    idx = css_text.find(".toast-key")
    assert idx != -1
    block = css_text[idx:idx + 300]
    assert "mono" in block.lower()
    assert "var(--text-muted)" in block


def test_toast_msg_selector_present(css_text):
    assert ".toast-msg" in css_text


def test_toast_dismiss_selector_present(css_text):
    assert ".toast-dismiss" in css_text


def test_toast_dismiss_times_button(css_text):
    """The dismiss button is a plain &times; button with hover/focus states."""
    assert ".toast-dismiss" in css_text
    # hover and focus-visible states must be defined
    assert ".toast-dismiss:hover" in css_text
    assert ".toast-dismiss:focus-visible" in css_text


def test_toast_actions_selector_present(css_text):
    assert ".toast-actions" in css_text


def test_toast_ask_selector_present(css_text):
    assert ".toast-ask" in css_text


def test_toast_ask_hover_focus_states(css_text):
    """toast-ask must define hover and focus-visible states."""
    assert ".toast-ask:hover" in css_text
    assert ".toast-ask:focus-visible" in css_text