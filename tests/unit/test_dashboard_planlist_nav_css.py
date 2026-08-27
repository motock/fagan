"""CSS-only tests for the pinned Comms pill / PLANS section label styling.

The prerequisite story "Reorder the sidebar so pinned Comms and Overview
render above a PLANS section label" (static/app/render/plan-list.js) already
emits `.plan-item.comms-item`, `.icon-comms`, `.plan-item.overview-item`, and
`.plan-list-section-label` markup with NO matching CSS rules yet. This story
is CSS-ONLY (static/style.css) - it must not touch plan-list.js, index.html,
or any .js file.

These tests mirror the plain-text/regex style established in
tests/unit/test_dashboard_comms_markup.py: read style.css as text, locate
rule blocks with `re.finditer(re.escape(sel) + r"[^{]*\\{([^}]*)\\}", css)`,
and assert on their bodies. No browser and no Node needed.
"""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "static"
STYLE_CSS = STATIC / "style.css"


def _read_css() -> str:
    assert STYLE_CSS.exists(), "static/style.css must exist"
    return STYLE_CSS.read_text(encoding="utf-8")


def _rule_block(css: str, selector: str):
    """Return the body of the rule block for the EXACT selector (selector
    immediately followed by optional whitespace then '{'), or None. Using a
    trailing `\\s*\\{` boundary (rather than `[^{]*`) avoids a shorter
    selector like `.plan-item.comms-item` accidentally matching the START of
    a longer one like `.plan-item.comms-item.active {`."""
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    return m.group(1) if m else None


def _selector_count(css: str, selector: str) -> int:
    return len(re.findall(re.escape(selector) + r"\s*\{", css))


# --- style.css: new selectors must not already exist (sanity/documentation) -

def test_comms_item_selector_is_not_yet_duplicated():
    """Guard against the rule being accidentally declared twice by a bad
    merge/edit; also documents that exactly one canonical rule is expected."""
    css = _read_css()
    assert _selector_count(css, ".plan-item.comms-item") == 1, (
        "expected exactly one `.plan-item.comms-item {` rule block"
    )


# --- style.css: .plan-item.comms-item (pill) --------------------------------

def test_css_has_comms_item_pill_rule():
    css = _read_css()
    block = _rule_block(css, ".plan-item.comms-item")
    assert block is not None, "no `.plan-item.comms-item {` rule block found"


def test_comms_item_pill_has_border_radius_pill_token():
    css = _read_css()
    block = _rule_block(css, ".plan-item.comms-item")
    assert block is not None
    assert "border-radius: var(--radius-pill)" in block


def test_comms_item_pill_has_accent_soft_background():
    css = _read_css()
    block = _rule_block(css, ".plan-item.comms-item")
    assert block is not None
    assert "background: var(--accent-soft)" in block


def test_comms_item_pill_has_accent_border():
    css = _read_css()
    block = _rule_block(css, ".plan-item.comms-item")
    assert block is not None
    assert "border: 1px solid var(--accent)" in block


# --- style.css: .plan-item.comms-item .plan-name ----------------------------

def test_css_has_comms_item_plan_name_rule():
    css = _read_css()
    block = _rule_block(css, ".plan-item.comms-item .plan-name")
    assert block is not None, (
        "no `.plan-item.comms-item .plan-name {` rule block found"
    )


def test_comms_item_plan_name_is_uppercase():
    """This is what renders the mockup's all-caps COMMS label from the
    mixed-case "Comms" text node emitted by plan-list.js."""
    css = _read_css()
    block = _rule_block(css, ".plan-item.comms-item .plan-name")
    assert block is not None
    assert "text-transform: uppercase" in block


def test_comms_item_plan_name_is_flex():
    css = _read_css()
    block = _rule_block(css, ".plan-item.comms-item .plan-name")
    assert block is not None
    assert "display: flex" in block


# --- style.css: .plan-item.comms-item.active --------------------------------

def test_css_has_comms_item_active_rule():
    css = _read_css()
    block = _rule_block(css, ".plan-item.comms-item.active")
    assert block is not None, (
        "no `.plan-item.comms-item.active {` rule block found"
    )


def test_comms_item_active_overrides_border_left_to_one_pixel():
    """The pill must NOT inherit .plan-item.active's 3px left border, which
    would flatten the pill's rounded left edge - it keeps its own 1px
    all-around border on the active state too."""
    css = _read_css()
    block = _rule_block(css, ".plan-item.comms-item.active")
    assert block is not None
    assert "border-left: 1px solid var(--accent)" in block


def test_comms_item_active_selector_appears_after_plan_item_active():
    """Source order must keep `.plan-item.active` before
    `.plan-item.comms-item.active` so cascade order cannot silently reverse
    the override (equal specificity ties resolve to source order)."""
    css = _read_css()
    base_idx = css.find(".plan-item.active {")
    override_idx = css.find(".plan-item.comms-item.active {")
    assert base_idx != -1, "no `.plan-item.active {` rule found"
    assert override_idx != -1, "no `.plan-item.comms-item.active {` rule found"
    assert override_idx > base_idx, (
        ".plan-item.comms-item.active must appear later in style.css than "
        ".plan-item.active so its border-left override is not reversed by "
        "cascade source order"
    )


# --- style.css: .icon-comms --------------------------------------------------

def test_css_has_icon_comms_rule():
    css = _read_css()
    assert _rule_block(css, ".icon-comms") is not None, (
        "no `.icon-comms {` rule block found"
    )


# --- style.css: .plan-list-section-label ------------------------------------

def test_css_has_plan_list_section_label_rule():
    css = _read_css()
    block = _rule_block(css, ".plan-list-section-label")
    assert block is not None, (
        "no `.plan-list-section-label {` rule block found"
    )


def test_plan_list_section_label_is_uppercase():
    css = _read_css()
    block = _rule_block(css, ".plan-list-section-label")
    assert block is not None
    assert "text-transform: uppercase" in block


def test_plan_list_section_label_uses_text_dim_color():
    css = _read_css()
    block = _rule_block(css, ".plan-list-section-label")
    assert block is not None
    assert "color: var(--text-dim)" in block


def test_plan_list_section_label_uses_mono_font():
    css = _read_css()
    block = _rule_block(css, ".plan-list-section-label")
    assert block is not None
    assert "font-family: var(--font-mono)" in block


# --- style.css: no new color literals or custom properties ------------------

_NEW_SELECTORS = [
    ".plan-item.comms-item",
    ".plan-item.comms-item .plan-name",
    ".plan-item.comms-item.active",
    ".icon-comms",
    ".plan-list-section-label",
]


def test_css_does_not_introduce_new_color_literals_in_new_nav_rules():
    """Mirrors test_css_does_not_introduce_new_color_literals_in_comms_rules
    in tests/unit/test_dashboard_comms_markup.py: the five new selectors must
    use only existing var(--...) tokens, never a raw hex/rgb/hsl literal."""
    css = _read_css()
    for sel in _NEW_SELECTORS:
        for m in re.finditer(re.escape(sel) + r"[^{]*\{([^}]*)\}", css):
            block = m.group(1)
            assert not re.search(r"#[0-9a-fA-F]{3,8}\b", block), (
                f"new nav rule for {sel} introduces a hex color literal: "
                f"{block!r}"
            )
            assert not re.search(r"\b(?:rgb|hsl)a?\s*\(", block), (
                f"new nav rule for {sel} introduces an rgb/hsl color "
                f"literal: {block!r}"
            )


def test_css_does_not_declare_new_custom_properties_in_new_nav_rules():
    """The brief requires reusing existing tokens only - none of the new
    rules may declare a fresh custom property (e.g. `--foo: ...;`)."""
    css = _read_css()
    for sel in _NEW_SELECTORS:
        for m in re.finditer(re.escape(sel) + r"[^{]*\{([^}]*)\}", css):
            block = m.group(1)
            assert not re.search(r"(?m)^\s*--[\w-]+\s*:", block), (
                f"new nav rule for {sel} declares a new custom property: "
                f"{block!r}"
            )


# --- style.css: Overview stays a plain .plan-item (no dedicated rule) -------

def test_overview_item_selector_is_not_declared():
    """Decision: Overview gets no special styling of its own, unlike the
    pinned Comms pill - it stays a plain .plan-item row."""
    css = _read_css()
    assert ".overview-item" not in css, (
        "style.css must not declare an .overview-item rule; Overview stays "
        "a plain .plan-item row by design"
    )


# --- style.css: pre-existing sidebar selectors must survive (regression) ---

def test_regression_plan_item_row_selector_present():
    assert ".plan-item-row" in _read_css()


def test_regression_plan_archive_btn_selector_present():
    assert ".plan-archive-btn" in _read_css()


def test_regression_plan_archived_selector_present():
    assert ".plan-archived" in _read_css()


def test_regression_plan_list_footer_selector_present():
    assert ".plan-list-footer" in _read_css()


def test_regression_show_archived_toggle_selector_present():
    assert ".show-archived-toggle" in _read_css()


def test_regression_plan_item_active_selector_present():
    assert ".plan-item.active" in _read_css()


def test_regression_plan_detail_id_present():
    assert "#plan-detail" in _read_css()


# --- style.css: new rules are anchored between show-archived-toggle and ----
# --- plan-detail, not appended elsewhere / reordering existing rules -------

def test_new_nav_rules_are_inserted_between_show_archived_toggle_and_plan_detail():
    css = _read_css()
    toggle_idx = css.find(".show-archived-toggle {")
    detail_idx = css.find("#plan-detail {")
    assert toggle_idx != -1, "no `.show-archived-toggle {` rule found"
    assert detail_idx != -1, "no `#plan-detail {` rule found"
    assert toggle_idx < detail_idx, (
        "unexpected file order: .show-archived-toggle must precede "
        "#plan-detail in style.css (pre-existing rule was reordered)"
    )
    for sel in _NEW_SELECTORS:
        sel_idx = css.find(sel + " {")
        assert sel_idx != -1, f"no `{sel} {{` rule found"
        assert toggle_idx < sel_idx < detail_idx, (
            f"expected `{sel}` to be inserted between .show-archived-toggle "
            f"and #plan-detail, got index {sel_idx} (toggle={toggle_idx}, "
            f"detail={detail_idx})"
        )


# --- app.js / index.html must NOT be touched by this story ------------------

def test_app_render_plan_list_js_still_declares_comms_item_class():
    """Sanity check that this CSS-only story is styling markup that already
    exists (from the prerequisite story) rather than markup it invented
    itself; this file must remain untouched by this dispatch."""
    plan_list_js = STATIC / "app" / "render" / "plan-list.js"
    assert plan_list_js.exists(), "static/app/render/plan-list.js must exist"
    js = plan_list_js.read_text(encoding="utf-8")
    assert '"plan-item comms-item"' in js
    assert '"plan-item overview-item"' in js
    assert 'class="icon-comms"' in js
    assert '"plan-list-section-label"' in js
