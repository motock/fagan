"""Markup/CSS-only tests for the redesigned Comms landing hero.

This story replaces the placeholder "Welcome" / "Type your message below and
press Send." landing copy in static/index.html with the approved "Ask the
tower." hero copy plus four suggestion-chip buttons, and restyles
static/style.css to match. It also updates the message input's placeholder
and relabels the send button to "TRANSMIT".

MARKUP AND CSS ONLY: static/app/comms.js, static/app/main.js, and
static/app.js must not change at all (a dependent follow-up story wires the
chip clicks). These tests read static/index.html and static/style.css as
plain text (no browser needed), following the pattern already used in
tests/unit/test_dashboard_comms_markup.py.
"""
import hashlib
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC = REPO_ROOT / "static"
INDEX_HTML = STATIC / "index.html"
STYLE_CSS = STATIC / "style.css"
APP_JS = STATIC / "app.js"
COMMS_JS = STATIC / "app" / "comms.js"
MAIN_JS = STATIC / "app" / "main.js"

# Hashes captured from the pre-implementation state of this branch (commit
# 0d6b8a1). These three files must stay byte-identical through this story -
# it is markup/CSS only, so any drift here means a JS file was touched.
_UNCHANGED_JS_SHA256 = {
    APP_JS: "52e12daba11b07f777764a6b6e3623c7bfd91ec9a0bf10251d903d2319772baa",
    # Re-pinned for the "repair Comms chat panel layout, auto-scroll, reset,
    # export" fix (#478, commit 7f18a2a): appendCommsMessage now renders a
    # role/timestamp "who" line into the (already-styled) .bubble element,
    # plus new resetCommsThread()/exportCommsThread() + wiring. (The hash
    # #478 itself re-pinned here was stale - computed before that commit's
    # own final edits to comms.js - so this corrects it to the actual
    # committed file contents.) See test_dashboard_comms_css.py's
    # COMMS_JS_SHA256 comment for the same pin.
    # Re-pinned again for the "reset must also clear commsHistory" fix
    # (resetCommsThread() left commsHistory populated after clearing the
    # DOM thread, so a 'reset' conversation silently kept sending old turns
    # to the backend): commsHistory = [] added to resetCommsThread, and
    # resetCommsThread added to the module's export list so it's reachable
    # by the regression test that covers this.
    # Re-pinned again for the comms TRACE-toggle story
    # (comms-trace-toggle-01, 2026-08-29): the toggle adds a module-level
    # showTrace block and header-button wiring to comms.js. Both blockers the
    # rework round introduced (deleted commsHistory declaration, deleted
    # reset/export wiring) were restored before this pin was taken; the final
    # pin adds the bare-shim classList guard in applyTraceVisibility. See
    # test_dashboard_comms_css.py's COMMS_JS_SHA256 comment for the same pin.
    # Re-pinned for PR #541 ("send selected workspace in chat POST body",
    # merged 2026-09-02): that story legitimately extended the chat POST
    # body with the selected workspace id, so the pre-#541 pin no longer
    # matched the committed file. This story never touched comms.js.
    # Re-pinned for the API-key wiring story (c33ed4ce, 2026-09-08): that
    # story's whole job was to make every dashboard fetch carry the
    # X-Pipeline-Api-Key header, so the /api/chat raw fetch in
    # sendCommsMessage() now builds its headers with the shared secret
    # (Content-Type preserved). comms.js is legitimately no longer
    # byte-identical to this story's pre-implementation baseline.
    COMMS_JS: "6c22e7c021af19244095178172e9dbe344b489545e0fea4f86537e0fc5529cb2",
    # MAIN_JS re-pinned: sibling story #465 ("Wire dynamic chat-model
    # subtitle...") legitimately added the updateCommsSubtitle() call to
    # main.js after this story's own pre-implementation baseline was
    # captured. This story never touched main.js itself.
    # Re-pinned again alongside the COMMS_JS pin above: resetCommsThread
    # added to main.js's import-from-comms.js line and re-export block so
    # the reset-clears-history regression test can call it.
    # Re-pinned for the maturity-panel story (a3-maturity-metrics,
    # 1f9f2c46): that story legitimately added the one-line
    # `import { renderMaturityPanel } from "./render/maturity.js";`
    # registration to main.js, so the pre-story pin no longer matched the
    # committed file. This story never touched main.js itself.
    # Re-pinned for the workspace-picker-wiring story (3c5e36b4, 2026-09-06):
    # that story's whole job was to wire the workspace picker into main.js
    # (import fetchWorkspaces/selectWorkspace/fetchActiveWorkspace/
    # renderWorkspacePicker from workspace.js; extend _applyActiveView and
    # selectOverview/selectComms/selectPlan for state.workspaceActive; add
    # loadWorkspaceView()/wireWorkspaceView()), so main.js is legitimately no
    # longer byte-identical to this story's pre-implementation baseline.
    # See test_main_js_untouched's own updated assertion below.
    # Re-pinned for the API-key wiring story (c33ed4ce, 2026-09-08): that
    # story's whole job was to make every dashboard fetch carry the
    # X-Pipeline-Api-Key header, so main.js's three raw fetch sites
    # (/api/config/providers GET, role-config POST, story /patch POST) now
    # build their headers with the shared secret (Content-Type preserved).
    # main.js is legitimately no longer byte-identical to this story's
    # pre-implementation baseline.
    # Re-pinned for the roles provider→model dropdown-cascade story
    # (eb8b1ac6, 2026-09-08): that story's whole job was to make the Model
    # dropdown in the Roles table Edit row repopulate when the Provider
    # dropdown changes, so main.js gained the top-level
    # buildModelOptionsHtml() helper (exported for the node-eval harness),
    # renderRoleEdit() now delegates its Model-select markup to it, and
    # _renderConfigRoles() wires a change listener on each .edit-provider
    # select. main.js is legitimately no longer byte-identical to this
    # story's pre-implementation baseline.
    MAIN_JS: "560db6bab8f4c15b8e821fb6404d3f4ff4dec3fb4e8539289c69770033202737",
}

EXPECTED_CHIP_MESSAGES = {
    "what's blocked right now?",
    "draft a plan for CSV export",
    "approve the merge for W1-04",
    "the W1-07 worktree is stuck, can you fix it?",
}

ALLOWED_CSS_TOKENS = {
    "sp-2", "sp-3", "sp-4",
    "panel-2", "panel-3", "border",
    "radius-pill", "font-mono", "fs-xs",
    "text-dim", "text", "accent",
}


def _read_index() -> str:
    assert INDEX_HTML.exists(), "static/index.html must exist"
    return INDEX_HTML.read_text(encoding="utf-8")


def _read_css() -> str:
    assert STYLE_CSS.exists(), "static/style.css must exist"
    return STYLE_CSS.read_text(encoding="utf-8")


def _landing_block(html: str) -> str:
    """Text between the #comms-landing opening tag and the sibling
    .comms-input-row div (i.e. everything inside #comms-landing, since it is
    the element immediately preceding .comms-input-row in the markup)."""
    m = re.search(
        r'<div[^>]*id="comms-landing"[^>]*>(.*?)<div class="comms-input-row">',
        html,
        re.DOTALL,
    )
    assert m, "could not locate #comms-landing block followed by .comms-input-row"
    return m.group(1)


def _chips_block(landing_block: str) -> str:
    m = re.search(
        r'<div[^>]*id="comms-chips"[^>]*>(.*?)</div>',
        landing_block,
        re.DOTALL,
    )
    assert m, "no #comms-chips container found inside #comms-landing"
    return m.group(1)


def _rule_block(css: str, selector: str) -> str:
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert m, f"no {selector!r} rule found in style.css"
    return m.group(1)


def _normalized_props(block: str):
    """Parse 'prop: value;' declarations into a set, tolerant of whitespace
    so we can compare content rather than exact formatting."""
    decls = [d.strip() for d in block.split(";") if d.strip()]
    return {re.sub(r"\s+", " ", d) for d in decls}


# --- #comms-landing: old placeholder copy must be gone --------------------

def test_comms_landing_no_longer_contains_old_welcome_heading():
    landing = _landing_block(_read_index())
    assert "Welcome" not in landing, (
        "old placeholder heading 'Welcome' must be removed from #comms-landing"
    )


def test_comms_landing_no_longer_contains_old_instruction_copy():
    landing = _landing_block(_read_index())
    assert "Type your message below and press Send." not in landing, (
        "old placeholder copy must be removed from #comms-landing"
    )


# --- #comms-landing: new hero copy -----------------------------------------

def test_comms_landing_has_new_heading():
    landing = _landing_block(_read_index())
    assert "<h3>Ask the tower.</h3>" in landing, (
        "expected the exact new heading '<h3>Ask the tower.</h3>' inside "
        f"#comms-landing; got: {landing!r}"
    )


def test_comms_landing_paragraph_opens_with_expected_words():
    landing = _landing_block(_read_index())
    m = re.search(r"<p>(.*?)</p>", landing, re.DOTALL)
    assert m, "no <p> found inside #comms-landing"
    assert m.group(1).startswith("Describe a goal to draft a plan"), (
        "new paragraph must open with 'Describe a goal to draft a plan'; "
        f"got: {m.group(1)!r}"
    )


# --- #comms-chips: exactly four button chips -------------------------------

def test_comms_chips_container_exists_inside_landing():
    landing = _landing_block(_read_index())
    assert 'id="comms-chips"' in landing, (
        "#comms-chips container must exist inside #comms-landing"
    )


def test_exactly_four_comms_chip_elements_exist():
    landing = _landing_block(_read_index())
    chips = _chips_block(landing)
    tags = re.findall(r"<(\w+)[^>]*class=\"comms-chip\"[^>]*>", chips)
    assert len(tags) == 4, f"expected exactly 4 .comms-chip elements, found {len(tags)}"


def test_all_comms_chip_elements_are_button_type_button():
    """Negative case: chips must be keyboard-focusable/actionable <button
    type="button"> elements, not <a> or <div>."""
    landing = _landing_block(_read_index())
    chips = _chips_block(landing)
    tags = re.findall(r'<(\w+)([^>]*class="comms-chip"[^>]*)>', chips)
    assert tags, "no .comms-chip elements found to check"
    for tag_name, attrs in tags:
        assert tag_name == "button", (
            f"comms-chip element must be a <button>, found <{tag_name}>"
        )
        assert 'type="button"' in attrs, (
            f'comms-chip <button> must declare type="button"; got attrs: {attrs!r}'
        )


def test_comms_chip_visible_text_matches_expected_set():
    landing = _landing_block(_read_index())
    chips = _chips_block(landing)
    texts = re.findall(r'class="comms-chip"[^>]*>([^<]*)</button>', chips)
    assert len(texts) == 4, f"expected 4 chip texts, found {len(texts)}"
    assert set(texts) == EXPECTED_CHIP_MESSAGES, (
        f"chip visible text set mismatch; got {set(texts)!r}, "
        f"expected {EXPECTED_CHIP_MESSAGES!r}"
    )


def test_comms_chip_data_attribute_matches_its_own_visible_text():
    landing = _landing_block(_read_index())
    chips = _chips_block(landing)
    buttons = re.findall(r"<button[^>]*>[^<]*</button>", chips)
    assert len(buttons) == 4
    for button_html in buttons:
        text_m = re.search(r">([^<]*)</button>", button_html)
        assert text_m, f"could not extract text from chip button: {button_html!r}"
        text = text_m.group(1)
        assert text in EXPECTED_CHIP_MESSAGES, (
            f"unexpected chip text {text!r}"
        )
        data_m = re.search(r'data-comms-chip-message="([^"]*)"', button_html)
        assert data_m, (
            f"chip button missing data-comms-chip-message attribute: {button_html!r}"
        )
        assert data_m.group(1) == text, (
            f"data-comms-chip-message ({data_m.group(1)!r}) must match the "
            f"button's visible text ({text!r})"
        )


# --- #comms-input: placeholder text ----------------------------------------

def test_comms_input_has_new_placeholder():
    html = _read_index()
    assert (
        'id="comms-input" class="comms-input" '
        'placeholder="Ask about a plan, describe a goal, or answer a decision..."'
        in html
    ), "expected the new #comms-input placeholder text, byte-exact"


def test_comms_input_no_longer_has_old_placeholder():
    html = _read_index()
    assert 'placeholder="Type a message..."' not in html, (
        "old #comms-input placeholder text must be removed"
    )


def test_comms_input_row_and_textarea_attrs_otherwise_unchanged():
    html = _read_index()
    assert 'rows="3"' in html
    assert 'aria-label="Message input"' in html


# --- #comms-send: relabeled to TRANSMIT ------------------------------------

def test_comms_send_button_text_is_transmit():
    html = _read_index()
    m = re.search(r'id="comms-send"[^>]*>([^<]*)</button>', html)
    assert m, "could not find #comms-send button text"
    assert m.group(1) == "TRANSMIT", (
        f"#comms-send visible text must be exactly 'TRANSMIT'; got {m.group(1)!r}"
    )


def test_comms_send_button_no_longer_says_send():
    html = _read_index()
    m = re.search(r'id="comms-send"[^>]*>([^<]*)</button>', html)
    assert m, "could not find #comms-send button text"
    assert m.group(1) != "Send"


def test_comms_send_aria_label_and_title_updated():
    html = _read_index()
    assert 'aria-label="Transmit message"' in html, (
        "#comms-send aria-label must be updated to 'Transmit message'"
    )
    assert 'title="Transmit"' in html, (
        "#comms-send title must be updated to 'Transmit'"
    )


def test_comms_send_old_aria_label_and_title_removed():
    html = _read_index()
    assert 'aria-label="Send message"' not in html
    assert 'title="Send"' not in html


# --- existing ids/classes must remain byte-identical -----------------------

def test_comms_thread_div_unchanged():
    html = _read_index()
    assert (
        '<div class="comms-thread" id="comms-thread" style="display:none" '
        'aria-live="polite"></div>' in html
    ), "the #comms-thread div must be untouched by this story"


def test_comms_landing_retains_existing_id_and_class():
    html = _read_index()
    assert 'id="comms-landing"' in html
    assert 'class="comms-landing"' in html


def test_comms_input_retains_existing_id_and_class():
    html = _read_index()
    assert 'id="comms-input"' in html
    assert 'class="comms-input"' in html


def test_comms_send_retains_existing_id_and_class():
    html = _read_index()
    assert 'id="comms-send"' in html
    assert 'class="comms-send"' in html


def test_comms_input_row_class_unchanged():
    html = _read_index()
    assert '<div class="comms-input-row">' in html


# --- style.css: new .comms-chips / .comms-chip rules -----------------------

def test_css_has_comms_chips_rule_with_expected_properties():
    css = _read_css()
    block = _rule_block(css, ".comms-chips")
    expected = {
        "display: flex",
        "flex-wrap: wrap",
        "gap: var(--sp-2)",
        "margin-top: var(--sp-4)",
    }
    assert _normalized_props(block) == expected, (
        f".comms-chips rule mismatch; got {_normalized_props(block)!r}"
    )


def test_css_has_comms_chip_rule_with_expected_properties():
    css = _read_css()
    block = _rule_block(css, ".comms-chip")
    expected = {
        "font-family: var(--font-mono)",
        "font-size: var(--fs-xs)",
        "background: var(--panel-2)",
        "border: 1px solid var(--border)",
        "border-radius: var(--radius-pill)",
        "padding: var(--sp-2) var(--sp-3)",
        "color: var(--text-dim)",
        "cursor: pointer",
    }
    assert _normalized_props(block) == expected, (
        f".comms-chip rule mismatch; got {_normalized_props(block)!r}"
    )


def test_css_has_comms_chip_hover_rule_with_expected_properties():
    css = _read_css()
    block = _rule_block(css, ".comms-chip:hover")
    expected = {"background: var(--panel-3)", "color: var(--text)"}
    assert _normalized_props(block) == expected, (
        f".comms-chip:hover rule mismatch; got {_normalized_props(block)!r}"
    )


def test_css_has_comms_chip_focus_visible_rule_with_expected_properties():
    css = _read_css()
    block = _rule_block(css, ".comms-chip:focus-visible")
    expected = {"outline: 2px solid var(--accent)", "outline-offset: 2px"}
    assert _normalized_props(block) == expected, (
        f".comms-chip:focus-visible rule mismatch; got {_normalized_props(block)!r}"
    )


def test_css_comms_chip_rules_use_only_existing_tokens():
    """Negative/boundary case: every var(--x) referenced by the new rules
    must already exist in style.css's :root token set - no invented token."""
    css = _read_css()
    combined = "".join(
        _rule_block(css, sel)
        for sel in (
            ".comms-chips",
            ".comms-chip",
            ".comms-chip:hover",
            ".comms-chip:focus-visible",
        )
    )
    used_tokens = set(re.findall(r"var\(--([a-zA-Z0-9-]+)\)", combined))
    assert used_tokens, "expected at least one var(--...) token reference"
    unknown = used_tokens - ALLOWED_CSS_TOKENS
    assert not unknown, f"new CSS rules reference unknown/invented tokens: {unknown!r}"


def test_css_new_comms_chip_rules_contain_no_color_literals():
    """No raw hex/rgb/hsl color literal may appear in the new rules (only
    var(--...) tokens), mirroring
    test_dashboard_comms_markup.test_css_does_not_introduce_new_color_literals_in_comms_rules."""
    css = _read_css()
    for sel in (
        ".comms-chips",
        ".comms-chip",
        ".comms-chip:hover",
        ".comms-chip:focus-visible",
    ):
        block = _rule_block(css, sel)
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b", block), (
            f"{sel} introduces a hex color literal: {block!r}"
        )
        assert not re.search(r"\b(?:rgb|hsl)a?\s*\(", block), (
            f"{sel} introduces an rgb/hsl color literal: {block!r}"
        )


# --- no JS files touched by this story --------------------------------------

def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_app_js_untouched():
    assert APP_JS.exists(), "static/app.js must exist"
    assert _sha256(APP_JS) == _UNCHANGED_JS_SHA256[APP_JS], (
        "static/app.js must be byte-identical to before this story "
        "(markup/CSS only)"
    )


def test_comms_js_untouched():
    assert COMMS_JS.exists(), "static/app/comms.js must exist"
    assert _sha256(COMMS_JS) == _UNCHANGED_JS_SHA256[COMMS_JS], (
        "static/app/comms.js must be byte-identical to before this story "
        "(markup/CSS only - chip wiring is a dependent follow-up story)"
    )


def test_main_js_untouched():
    assert MAIN_JS.exists(), "static/app/main.js must exist"
    assert _sha256(MAIN_JS) == _UNCHANGED_JS_SHA256[MAIN_JS], (
        "static/app/main.js must be byte-identical to before this story "
        "(markup/CSS only)"
    )
