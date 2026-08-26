"""Tests for three toast fixes bundled into one story:

1. pushToast() set `--stripe` to a bare CSS custom-property NAME (e.g.
   "--c-failed") instead of a var() reference. style.css's `.toast` rule
   reads `border-left: 3px solid var(--stripe, var(--text-muted))` -- a
   bare name there produces invalid CSS (`3px solid --c-failed`), so the
   whole declaration is dropped and toasts never show a severity color.
   The fix wraps the stored value: `var(${stripe})`.
2. The `.toast-key` badge showed the plan name instead of the story key.
   The fix prefers `storyKey`, falling back to `planName` only when
   `storyKey` is falsy/empty so the badge is never left blank.
3. The Ask button's label changes from "Ask" to "Ask Tower".
4. style.css: `.toast-key` must pick up the stripe color (falling back to
   `--text-muted`), and `.toast-ask` must gain mono/uppercase/letter-spacing
   styling to match the pill-button design.

Mirrors the node-eval harness pattern in tests/unit/test_dashboard_toast_behavior.py:
builds a minimal DOM shim, evals static/app.js under `node -e`, and
JSON-stringifies the result of a test expression. The shim is copied +
trimmed here (rather than imported) so this file stands alone.

These tests are RED until the implementation lands: `--stripe` is still set
to a bare custom-property name, `.toast-key` still always shows planName,
the Ask button still reads "Ask", and style.css's `.toast-key`/`.toast-ask`
rules haven't been updated.
"""
import json
import os
from pathlib import Path

import pytest

from tests.unit._app_js import run_app_js as _shared_run_app_js

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STATIC_DIR = Path(REPO_ROOT) / "static"
STYLE_CSS = STATIC_DIR / "style.css"


_SHIM = r"""
    const noop = () => {};
    let __testIdCounter = 0;

    function __parseHtml(html, parent) {
        parent.__children = [];
        if (!html) return;
        const tokenRe = /<(\/?)([a-zA-Z]+)((?:\s+[a-zA-Z-]+(?:="[^"]*")?)*)\s*(\/?)>/g;
        const are = /([a-zA-Z-]+)="([^"]*)"/g;
        const stack = [parent];
        let lastIndex = 0;
        let m;
        while ((m = tokenRe.exec(html)) !== null) {
            if (m.index > lastIndex) {
                const top = stack[stack.length - 1];
                top.__ownText = (top.__ownText || "") + html.slice(lastIndex, m.index);
            }
            lastIndex = tokenRe.lastIndex;
            const isClose = m[1] === "/";
            const tag = m[2];
            if (isClose) {
                for (let i = stack.length - 1; i >= 1; i--) {
                    if (stack[i].__tag === tag) { stack.length = i; break; }
                }
                continue;
            }
            const selfClosing = m[4] === "/";
            const el = __newEl(tag);
            are.lastIndex = 0;
            let am;
            while ((am = are.exec(m[3] || "")) !== null) {
                if (am[1] === "class") el.className = am[2];
                else if (am[1] === "title") el.title = am[2];
                else if (am[1] === "type") el.type = am[2];
                else if (am[1].startsWith("aria-")) el.setAttribute(am[1], am[2]);
                else el.setAttribute(am[1], am[2]);
            }
            const top = stack[stack.length - 1];
            top.__children.push(el);
            el.__parent = top;
            if (!selfClosing) stack.push(el);
        }
        if (lastIndex < html.length) {
            const top = stack[stack.length - 1];
            top.__ownText = (top.__ownText || "") + html.slice(lastIndex);
        }
        const fillText = (node) => {
            let text = node.__ownText || "";
            for (const child of node.__children) {
                fillText(child);
                text += child.__text;
            }
            node.__text = text;
        };
        fillText(parent);
    }

    function __newEl(tag) {
        const el = {
            __testId: ++__testIdCounter,
            __tag: tag || "div",
            __children: [],
            __parent: null,
            __text: "",
            __listeners: {},
            tagName: (tag || "div").toUpperCase(),
            innerHTML: "",
            textContent: "",
            className: "",
            title: "",
            type: "",
            value: "",
            checked: false,
            style: {},
            dataset: {},
            classList: {
                _classes: () => (el.className || "").split(/\s+/).filter(Boolean),
                add: (...c) => { const s = new Set(el.classList._classes()); c.forEach(x => s.add(x)); el.className = [...s].join(" "); },
                remove: (...c) => { const s = new Set(el.classList._classes()); c.forEach(x => s.delete(x)); el.className = [...s].join(" "); },
                toggle: (c, f) => { const s = new Set(el.classList._classes()); if (f === undefined) f = !s.has(c); if (f) s.add(c); else s.delete(c); el.className = [...s].join(" "); },
                contains: (c) => el.classList._classes().includes(c),
            },
            addEventListener: (type, fn) => {
                if (!el.__listeners[type]) el.__listeners[type] = [];
                el.__listeners[type].push(fn);
            },
            removeEventListener: noop,
            __fire: (type, evt) => {
                for (const fn of (el.__listeners[type] || [])) fn(evt || {});
            },
            setAttribute: (k, v) => {
                if (k === "class") el.className = String(v);
                else if (k === "title") el.title = String(v);
                else if (k === "type") el.type = String(v);
                else if (k === "style") { /* store raw style string; ignore */ }
                else if (k.startsWith("data-")) el.dataset[k.slice(5)] = String(v);
                else el[k] = String(v);
            },
            getAttribute: (k) => {
                if (k === "class") return el.className;
                if (k === "style") return el.__styleStr || "";
                if (k.startsWith("data-")) return el.dataset[k.slice(5)];
                return el[k] != null ? String(el[k]) : null;
            },
            appendChild: (child) => {
                if (child.__parent) {
                    const i = child.__parent.__children.indexOf(child);
                    if (i >= 0) child.__parent.__children.splice(i, 1);
                }
                el.__children.push(child);
                child.__parent = el;
                return child;
            },
            removeChild: (child) => {
                const i = el.__children.indexOf(child);
                if (i >= 0) el.__children.splice(i, 1);
                child.__parent = null;
                return child;
            },
            remove: () => {
                if (el.__parent) el.__parent.removeChild(el);
            },
            querySelector: (sel) => el.__queryAll(sel)[0] || null,
            querySelectorAll: (sel) => el.__queryAll(sel),
            focus: noop,
            get children() { return el.__children; },
            __queryAll: (sel) => {
                const out = [];
                const walk = (node) => {
                    for (const c of node.__children) {
                        if (__matches(c, sel)) out.push(c);
                        walk(c);
                    }
                };
                walk(el);
                return out;
            },
        };
        Object.defineProperty(el, "innerHTML", {
            get: () => el.__innerHTMLRaw || "",
            set: (v) => {
                el.__innerHTMLRaw = String(v);
                __parseHtml(String(v), el);
            },
            configurable: true,
        });
        Object.defineProperty(el, "textContent", {
            get: () => el.__text || "",
            set: (v) => { el.__text = String(v); el.__children = []; el.__innerHTMLRaw = String(v); },
            configurable: true,
        });
        el.style = {
            __props: {},
            setProperty: (k, v) => { el.style.__props[k] = String(v); },
            getPropertyValue: (k) => (el.style.__props[k] != null ? String(el.style.__props[k]) : ""),
            removeProperty: (k) => { const v = el.style.__props[k]; delete el.style.__props[k]; return v != null ? String(v) : ""; },
        };
        return el;
    }

    function __matches(el, sel) {
        if (!sel) return false;
        if (sel.startsWith(".")) {
            const cls = sel.slice(1);
            return el.classList.contains(cls);
        }
        if (sel.startsWith("[data-")) {
            const key = sel.slice(6, sel.indexOf("=") > 0 ? sel.indexOf("=") - 6 : sel.indexOf("]"));
            const val = sel.indexOf("=") > 0 ? sel.slice(sel.indexOf("=") + 2, sel.indexOf("]")) : null;
            if (val != null) return el.dataset[key] === val;
            return el.dataset[key] != null;
        }
        return el.__tag === sel;
    }

    const __nav = __newEl("div");
    const __detail = __newEl("div");
    const __toastStack = __newEl("div");
    const __commsInput = __newEl("input");
    const __byId = { "plan-list": __nav, "plan-detail": __detail, "toast-stack": __toastStack, "comms-input": __commsInput };
    globalThis.document = {
        addEventListener: noop,
        documentElement: { dataset: {} },
        activeElement: null,
        body: __newEl("body"),
        hidden: false,
        getElementById: (id) => __byId[id] || __newEl("div"),
        createElement: (tag) => __newEl(tag),
        querySelector: (sel) => null,
        querySelectorAll: (sel) => [],
    };
    globalThis.window = {
        location: { hash: "" },
        addEventListener: noop,
    };
    globalThis.localStorage = { getItem: () => null, setItem: noop };
    globalThis.fetch = () => new Promise(() => {});
    process.on("unhandledRejection", () => {});
    globalThis.setInterval = () => 0;
    globalThis.__pendingTimers = [];
    globalThis.setTimeout = (fn, ms) => {
        globalThis.__pendingTimers.push({ fn, ms });
        return globalThis.__pendingTimers.length;
    };
    globalThis.__runTimers = () => {
        const timers = globalThis.__pendingTimers;
        globalThis.__pendingTimers = [];
        for (const t of timers) t.fn();
    };
"""


def _run_app_js(expr):
    """Evaluate a JS expression inside an environment where static/app.js
    has been loaded. Returns the JSON-serialized result."""
    proc = _shared_run_app_js(expr, shim=_SHIM)
    if proc.returncode != 0:
        raise AssertionError(f"node failed: {proc.stderr}")
    return json.loads(proc.stdout)


def _push_and_get_stripe(severity):
    expr = (
        "(() => {"
        f" pushToast({{ severity: '{severity}', planName: 'demo', storyKey: 'S1', message: 'x' }});"
        " const stack = document.getElementById('toast-stack');"
        " const node = stack.__children[0];"
        " return { stripe: node.style.getPropertyValue('--stripe') };"
        " })()"
    )
    return _run_app_js(expr)["stripe"]


def _push_and_get_toast_key(story_key, plan_name):
    story_key_js = json.dumps(story_key)
    plan_name_js = json.dumps(plan_name)
    expr = (
        "(() => {"
        f" pushToast({{ severity: 'info', planName: {plan_name_js}, storyKey: {story_key_js}, message: 'x' }});"
        " const stack = document.getElementById('toast-stack');"
        " const node = stack.__children[0];"
        " const keyEl = node.querySelector('.toast-key');"
        " return { text: keyEl ? keyEl.textContent : null };"
        " })()"
    )
    return _run_app_js(expr)["text"]


# === Fix 1: --stripe must be a var() reference, not a bare custom-prop name


def test_push_toast_error_stripe_is_var_reference():
    assert _push_and_get_stripe("error") == "var(--c-failed)"


def test_push_toast_warning_stripe_is_var_reference():
    assert _push_and_get_stripe("warning") == "var(--c-parked)"


def test_push_toast_info_stripe_is_var_reference():
    assert _push_and_get_stripe("info") == "var(--c-unknown)"


def test_push_toast_stripe_is_not_bare_custom_property_name():
    """Negative case: the old buggy value must not reappear -- a bare name
    like '--c-failed' (no var() wrapper) produces invalid CSS and silently
    drops the whole border-left declaration."""
    stripe = _push_and_get_stripe("error")
    assert stripe != "--c-failed"
    assert stripe.startswith("var(") and stripe.endswith(")")


# === Fix 2: .toast-key shows storyKey, falling back to planName ===========


def test_push_toast_key_shows_story_key():
    assert _push_and_get_toast_key(story_key="S1", plan_name="demo") == "S1"


def test_push_toast_key_falls_back_to_plan_name_when_story_key_empty():
    """Negative/boundary case: an empty-string storyKey (e.g. a plan-level
    notification with no associated story) must fall back to planName so
    the badge is never left blank."""
    assert _push_and_get_toast_key(story_key="", plan_name="demo") == "demo"


def test_push_toast_key_falls_back_to_plan_name_when_story_key_omitted():
    """Boundary case: storyKey entirely absent from the call (undefined)
    must also fall back to planName."""
    expr = (
        "(() => {"
        " pushToast({ severity: 'info', planName: 'demo', message: 'x' });"
        " const stack = document.getElementById('toast-stack');"
        " const node = stack.__children[0];"
        " const keyEl = node.querySelector('.toast-key');"
        " return { text: keyEl ? keyEl.textContent : null };"
        " })()"
    )
    assert _run_app_js(expr)["text"] == "demo"


# === Fix 3: Ask button label changes to "Ask Tower" ========================


def test_push_toast_ask_button_says_ask_tower():
    expr = (
        "(() => {"
        " pushToast({ severity: 'error', planName: 'demo', storyKey: 'S1', message: 'boom' });"
        " const stack = document.getElementById('toast-stack');"
        " const node = stack.__children[0];"
        " const ask = node.querySelector('.toast-ask');"
        " return { askText: ask ? ask.textContent : null };"
        " })()"
    )
    res = _run_app_js(expr)
    assert res["askText"] == "Ask Tower"


def test_push_toast_ask_button_no_longer_says_bare_ask():
    """Negative case: the old bare 'Ask' label must not reappear."""
    expr = (
        "(() => {"
        " pushToast({ severity: 'warning', planName: 'demo', storyKey: 'S1', message: 'parked' });"
        " const stack = document.getElementById('toast-stack');"
        " const node = stack.__children[0];"
        " const ask = node.querySelector('.toast-ask');"
        " return { askText: ask ? ask.textContent : null };"
        " })()"
    )
    res = _run_app_js(expr)
    assert res["askText"] != "Ask"


# === Fix 4: style.css -- .toast-key stripe color, .toast-ask pill styling =


@pytest.fixture(scope="module")
def css_text():
    return STYLE_CSS.read_text()


def test_toast_key_color_uses_stripe_with_text_muted_fallback(css_text):
    idx = css_text.find(".toast-key")
    assert idx != -1, ".toast-key rule must exist in style.css"
    block = css_text[idx:idx + 300]
    assert "color: var(--stripe, var(--text-muted))" in block, (
        ".toast-key must pick up the severity stripe color, falling back "
        "to --text-muted when no stripe is set"
    )


def test_toast_key_still_backward_compatible_with_text_muted_fallback(css_text):
    """Boundary/back-compat case: the .toast-key rule must still contain
    the literal substring 'var(--text-muted)' as the fallback value, so any
    code path reusing .toast-key without a --stripe value set is unaffected."""
    idx = css_text.find(".toast-key")
    block = css_text[idx:idx + 300]
    assert "var(--text-muted)" in block


def test_toast_key_still_mono_and_small(css_text):
    idx = css_text.find(".toast-key")
    block = css_text[idx:idx + 300]
    assert "mono" in block.lower()
    assert "var(--fs-xs)" in block


def test_toast_ask_has_mono_uppercase_pill_styling(css_text):
    idx = css_text.find(".toast-ask {")
    assert idx != -1, ".toast-ask rule must exist in style.css"
    end = css_text.find("}", idx)
    assert end != -1
    block = css_text[idx:end]
    assert "font-family: var(--font-mono)" in block
    assert "font-size: var(--fs-xs)" in block
    assert "text-transform: uppercase" in block
    assert "letter-spacing: 0.04em" in block


def test_toast_ask_still_has_original_border_and_color(css_text):
    """Regression guard: the pre-existing border/color/padding declarations
    on .toast-ask must survive the styling addition, not be replaced."""
    idx = css_text.find(".toast-ask {")
    end = css_text.find("}", idx)
    block = css_text[idx:end]
    assert "border: 1px solid var(--accent)" in block
    assert "color: var(--accent)" in block
    assert "padding: var(--sp-1) var(--sp-2)" in block
    assert "border-radius: var(--radius-sm)" in block


def test_untouched_toast_rules_unaffected(css_text):
    """Sanity/boundary check: the story explicitly must not touch .toast,
    .toast-stack, .toast-row, .toast-msg, or .toast-dismiss -- assert those
    selectors are still present, unmodified in substance, as a smoke check
    against accidental drift."""
    assert ".toast {" in css_text
    assert ".toast-stack" in css_text
    assert ".toast-row" in css_text
    assert ".toast-msg" in css_text
    assert ".toast-dismiss" in css_text
