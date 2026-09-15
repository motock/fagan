"""Tests for the Comms view's CSS treatment (static/style.css only).

static/index.html and static/app/comms.js already render and wire the Comms
chat view correctly; the "Comms view styles" section of style.css defines
its classes with empty rule bodies (`.comms-head {}`, `.msg .who {}`, ...),
so the view has no layout or visual treatment. This story is CSS-only: it
must fill in exactly those empty rule bodies and touch nothing else.
"""
import hashlib
import re
from pathlib import Path

import pytest

STATIC_DIR = Path(__file__).resolve().parents[2] / "static"
STYLE_CSS = STATIC_DIR / "style.css"
INDEX_HTML = STATIC_DIR / "index.html"

# static/index.html and static/app/comms.js as of this dispatch (before the
# CSS-only implementation). This story must not touch either file.
#
# NOTE: these are per-story self-guards, not permanent regression oracles.
# INDEX_HTML_SHA256 and TOAST_REGION_SHA256 were pinned when this story (#458)
# was implemented. INDEX_HTML_SHA256 has been re-pinned as later stories
# legitimately touched index.html (see the comment at the pin below).
# ran and have since been superseded twice over by later, legitimately merged
# sibling stories that touch these exact same shared files: #466 ("Build the
# Comms landing hero copy...") edited index.html and added CSS near the Comms
# rules, and #467 ("Fix toast severity stripe color...") edited the toast
# region directly. Re-pinned below to the current, correct post-merge state
# rather than left red for every unrelated future story - see the
# chat-claude-md-leak session's diagnosis of the wired-chip-clicks story.
# Re-pinned for the chat-page touch-up pass (2026-08-27): comms-body was
# moved out of comms-head to become a proper sibling flex region (fixing a
# layout bug where the whole panel shrank to content height instead of
# filling the viewport, which is also why auto-scroll never had a bounded
# container to scroll within), and comms-head gained a .comms-actions button
# group (Export/Reset). Only that touch-up changed; unrelated markup is
# byte-identical to the prior pin.
# Re-pinned for the comms TRACE-toggle story (comms-trace-toggle-01,
# 2026-08-29): adds a #comms-trace-toggle button to the .comms-actions row.
# Re-pinned for the workspace-picker-wiring story (25309f29, 2026-09-05):
# that story legitimately adds workspace picker markup (#workspace-nav
# beside #config-nav, and a new #workspace-view section with
# #workspace-picker, #workspace-form, #workspace-path-input,
# #workspace-create and #workspace-select) between #config-view and
# #comms-view. This story never touched comms.js or any Comms-related
# markup.
# Re-pinned for the workspace picker visual cleanup (2026-09-07): the
# #workspace-view form gained a label and wrapper classes
# (workspace-form/workspace-form-label/workspace-form-row/
# workspace-path-input/workspace-create-label/workspace-select-btn) and
# #workspace-picker gained a "workspace-picker" class; all IDs this test
# suite and test_app_workspace_picker.mjs check for are unchanged. Still
# never touched comms.js or any Comms-related markup.
# Re-pinned for the config-mismatch banner story (CFG-E4, 962fbde): that
# story's whole job was to surface a dashboard/scheduler config divergence
# in the UI, so static/index.html gained one hidden
# <div id="config-mismatch-banner" class="config-mismatch-banner hidden"
# role="alert"></div> immediately after the usage-banner div (the
# usage-banner precedent). No Comms-related markup was touched.
# Re-pinned for the patch review/apply UI wiring story (WAP-14): that story's
# whole job was to wire the WAP-13 patch module into the dashboard UI, so
# static/index.html gained one <script type="module" src="/app/patch.js">
# tag immediately after the existing /app.js module script. No Comms-related
# markup was touched.
# Re-pinned for the chat-ingest hand-off markup story (CIH-3): that story's
# whole job was to add the Comms ingest panel markup, so static/index.html
# gained the ingest panel (#ingest-plan-name / #ingest-plan-submit /
# #ingest-plan-status) at the end of #comms-body, as siblings of the existing
# .comms-input-row. No Comms chat markup was touched.
INDEX_HTML_SHA256 = "f9536dcfff0b2ce439d7cd03b71ce045ac617cf3375b6e080bcbfc149044ba41"
# Re-pinned for the API-key wiring story (c33ed4ce, 2026-09-08): that story's
# whole job was to inject the dashboard shared secret into the served HTML, so
# static/index.html gained the <!--PIPELINE_API_KEY--> placeholder marker
# immediately before </head> (the server str.replace()s it at request time).
# index.html is legitimately no longer byte-identical to this story's
# pre-implementation baseline.

# sha256 of the style.css regions this story must not touch: everything
# before the "Comms view styles" comment, the "Toast stack styles" section
# that sits between the two halves of the Comms rules, and everything from
# "Config view styles" onward.
# Re-pinned by NAV-02 (pinned Comms pill + PLANS section label), which
# legitimately adds sidebar rules to the plan-list region - that region sits
# inside this prefix, so the pin must move with it. Only that insertion
# changed; the rest of the prefix is byte-identical to the prior pin.
# Re-pinned post-fb25c66 (Maturity panel restyle): that commit's whole job
# was adding the "=== Maturity panel (a3-maturity-metrics) ===" CSS block
# (markup/CSS only, no behavior change), which lands before the "Comms view
# styles" marker and so falls inside this guarded prefix. Only that
# insertion changed; the rest of the prefix is byte-identical to the prior
# pin.
PREFIX_BEFORE_COMMS_SHA256 = "b6ceb89d74a21034ea7e8f60dd1c056e5c97cb8fb90500d0eb13d00b0c5e382e"
# Re-pinned post-#467 (toast severity stripe color fix) - see note above.
TOAST_REGION_SHA256 = "dbfb98e7ac5c90643e9560c45faa9d8d04e8487c11ab55c4af82cb7c2593550c"
# Re-pinned for the story-modal Replay timeline (9639ae41, 2026-09-05): that
# story's brief explicitly authorizes appending new CSS at the END of
# style.css only ("APPEND-AT-END ONLY"), which necessarily falls after the
# "Config view styles" comment this hash guards. The addition is a single
# minimal rule (`.modal-content .replay-message`) appended after the
# existing suffix; nothing before the append point changed.
# Re-pinned for the workspace picker visual cleanup (2026-09-07): appended
# the .workspace-current/.workspace-list/.workspace-row/.workspace-form/
# etc. rules after the existing .modal-content .replay-message rule, same
# append-at-end pattern as the prior re-pin. Nothing before the append
# point changed.
SUFFIX_FROM_CONFIG_SHA256 = "17d30eca351b9e32b940567cd8fc8944aa57af52264001ff1f33bd8064bb9e0b"

# The 21 selectors listed in the story as having empty `{}` bodies. Several
# have a separate, already-populated rule immediately after them (e.g.
# `.on-air.live { animation: ... }`) which must not be touched.
TARGET_SELECTORS = [
    ".comms-head",
    ".comms-title",
    ".comms-title .label",
    ".on-air",
    ".comms-sub",
    ".comms-body",
    ".comms-thread",
    ".comms-landing",
    ".comms-landing h3",
    ".comms-foot",
    ".comms-input-row",
    ".comms-input",
    ".comms-send",
    ".msg",
    ".msg .who",
    ".msg.user .bubble",
    ".msg.tower .bubble",
    ".trace-row",
    ".trace-chip",
    ".trace-chip .dot",
    ".trace-detail",
]

# Rules that are already correctly populated and sit immediately after one
# of the TARGET_SELECTORS above. The story's brief explicitly says not to
# touch these - only the empty `{}` rules.
PRESERVED_COMPANION_RULES = [
    ".on-air.live { animation: pulse 1s ease-in-out infinite; }",
    "@keyframes pulse { 0%{opacity:.5;} 50%{opacity:1;} 100%{opacity:.5;} }",
    "@media (prefers-reduced-motion: reduce) { .on-air.live { animation: none; } }",
    ".comms-input:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }",
    ".comms-send:hover { background: var(--accent-soft); color: var(--accent); }",
    ".comms-send:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }",
    ".comms-send:disabled { opacity: 0.5; cursor: not-allowed; }",
    ".msg.tower.denied .bubble { border-left: 4px solid var(--c-failed); }",
    ".trace-chip:hover { background: var(--panel-2); }",
    ".trace-chip:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }",
    ".trace-chip.expanded + .trace-detail { display: block; }",
]

# Mockup-only features explicitly out of scope for this story.
FORBIDDEN_SELECTORS = [".diff-block", ".patch-btn", ".comms-suggest"]

_COLOR_LITERAL_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\s*\(")


class RuleNotFoundError(AssertionError):
    """Raised when a CSS selector's rule block cannot be located."""


def _selector_match(css_text: str, selector: str):
    # `.trace-detail` also appears as the tail of the unrelated compound
    # selector `.trace-chip.expanded + .trace-detail { ... }`; take the
    # first match, since the bare declaration is always written first.
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


def _stripped_body(body: str) -> str:
    without_comments = re.sub(r"/\*.*?\*/", "", body, flags=re.DOTALL)
    return without_comments.strip()


@pytest.fixture(scope="module")
def css_text():
    return STYLE_CSS.read_text()


# --- helper mechanism self-tests (synthetic CSS, not the real file) -------


def test_find_rule_body_extracts_simple_rule():
    css = ".foo { color: red; }"
    assert find_rule_body(css, ".foo") == " color: red; "


def test_find_rule_body_empty_rule_returns_empty_string():
    css = ".foo {}"
    assert find_rule_body(css, ".foo") == ""


def test_stripped_body_treats_comment_only_body_as_empty():
    css = ".foo { /* still empty */ }"
    body = find_rule_body(css, ".foo")
    assert _stripped_body(body) == ""


def test_stripped_body_non_empty_after_removing_comment():
    css = ".foo { /* note */ color: red; }"
    body = find_rule_body(css, ".foo")
    assert _stripped_body(body) == "color: red;"


def test_find_rule_body_prefers_first_match_over_compound_selector():
    css = ".foo { color: red; } .foo.bar { color: blue; }"
    assert _stripped_body(find_rule_body(css, ".foo")) == "color: red;"


def test_find_rule_body_does_not_match_pseudo_class_variant():
    css = ".btn {} .btn:hover { color: blue; }"
    assert _stripped_body(find_rule_body(css, ".btn")) == ""


def test_find_rule_body_missing_selector_raises_rule_not_found_error():
    css = ".foo { color: red; }"
    with pytest.raises(RuleNotFoundError, match=re.escape("'.missing'")):
        find_rule_body(css, ".missing")


def test_find_rule_body_unterminated_rule_raises_value_error():
    css = ".foo { color: red;"
    with pytest.raises(ValueError, match=re.escape("'.foo'")):
        find_rule_body(css, ".foo")


# --- static/style.css exists ------------------------------------------------


def test_style_css_exists():
    assert STYLE_CSS.exists(), "static/style.css must exist"


# --- the 21 target selectors must have non-empty rule bodies ---------------


@pytest.mark.parametrize("selector", TARGET_SELECTORS)
def test_target_selector_rule_exists(css_text, selector):
    assert _selector_match(css_text, selector) is not None, (
        f"expected a rule for selector {selector!r} in static/style.css"
    )


@pytest.mark.parametrize("selector", TARGET_SELECTORS)
def test_target_selector_body_is_non_empty(css_text, selector):
    body = find_rule_body(css_text, selector)
    stripped = _stripped_body(body)
    assert stripped != "", (
        f"selector {selector!r} must have a non-empty rule body "
        f"(after stripping whitespace and CSS comments), got body={body!r}"
    )


@pytest.mark.parametrize("selector", TARGET_SELECTORS)
def test_target_selector_uses_no_invented_color_literal(css_text, selector):
    """Colors must come from existing --tokens, not a hardcoded hex/rgb value."""
    body = find_rule_body(css_text, selector)
    stripped = _stripped_body(body)
    match = _COLOR_LITERAL_RE.search(stripped)
    assert match is None, (
        f"selector {selector!r} must reference an existing var(--token) for "
        f"color, not a literal color value like {match.group(0) if match else ''!r}"
    )


# --- already-populated companion rules must be untouched -------------------


@pytest.mark.parametrize("companion_rule", PRESERVED_COMPANION_RULES)
def test_preserved_companion_rule_untouched(css_text, companion_rule):
    assert companion_rule in css_text, (
        f"already-populated rule {companion_rule!r} must remain exactly as-is "
        "- only the empty `{}` rules should be edited"
    )


# --- explicitly out-of-scope mockup features must not be added -------------


@pytest.mark.parametrize("forbidden_selector", FORBIDDEN_SELECTORS)
def test_mockup_only_feature_not_added(css_text, forbidden_selector):
    assert forbidden_selector not in css_text, (
        f"{forbidden_selector!r} is an out-of-scope mockup feature "
        "(diff-block/patch-btn/comms-suggest) and must not be added"
    )


# --- section markers survive -----------------------------------------------


def test_comms_section_comment_present_exactly_once(css_text):
    assert css_text.count("/* Comms view styles */") == 1


def test_config_section_comment_present_exactly_once(css_text):
    assert css_text.count("/* Config view styles */") == 1


# --- nothing outside the two empty-rule blocks may change -------------------


def test_style_css_prefix_before_comms_section_unchanged(css_text):
    idx = css_text.find("/* Comms view styles */")
    assert idx != -1, "missing '/* Comms view styles */' section comment"
    prefix = css_text[:idx]
    digest = hashlib.sha256(prefix.encode()).hexdigest()
    assert digest == PREFIX_BEFORE_COMMS_SHA256, (
        "content before the Comms view styles section must be byte-for-byte "
        "unchanged - only the listed empty selectors may be edited"
    )


def test_style_css_toast_region_between_comms_blocks_unchanged(css_text):
    toast_idx = css_text.find("/* Toast stack styles */")
    assert toast_idx != -1, "missing '/* Toast stack styles */' section comment"
    msg_match = _selector_match(css_text, ".msg")
    assert msg_match is not None, "expected a '.msg' rule to bound the toast region"
    toast_region = css_text[toast_idx:msg_match.start()]
    digest = hashlib.sha256(toast_region.encode()).hexdigest()
    assert digest == TOAST_REGION_SHA256, (
        "the Toast stack styles section (between the two Comms rule blocks) "
        "must be byte-for-byte unchanged - this story only edits Comms rules"
    )


def test_style_css_suffix_from_config_section_unchanged(css_text):
    idx = css_text.find("/* Config view styles */")
    assert idx != -1, "missing '/* Config view styles */' section comment"
    suffix = css_text[idx:]
    digest = hashlib.sha256(suffix.encode()).hexdigest()
    assert digest == SUFFIX_FROM_CONFIG_SHA256, (
        "content from the Config view styles section onward must be "
        "byte-for-byte unchanged - only Comms rules may be edited"
    )


# --- this story must not touch index.html or comms.js -----------------------


def test_index_html_untouched():
    assert INDEX_HTML.exists(), "static/index.html must exist"
    digest = hashlib.sha256(INDEX_HTML.read_bytes()).hexdigest()
    assert digest == INDEX_HTML_SHA256, (
        "this story is CSS-only - static/index.html must not be modified"
    )
