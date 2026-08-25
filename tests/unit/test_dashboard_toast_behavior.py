"""Behavioral tests for the toast-stack wiring added to static/app.js:
`pickNewNotifications` (dedup-by-key pure helper) and `pushToast` (DOM
toast creation, dismiss, auto-fade, and the Ask-Tower jump into Comms).

Mirrors the node-eval harness pattern in tests/unit/test_dashboard_board_diff.py:
builds a minimal DOM shim, evals static/app.js under `node -e`, and
JSON-stringifies the result of a test expression. The shim is copied +
trimmed here (rather than imported) so this file stands alone.

This story's review flagged that the toast feature (pickNewNotifications,
pushToast, and the refresh() wiring that pushes/dedupes/auto-fades toasts)
shipped with zero test coverage anywhere on the branch — nothing ever
executed this code, which is how a duplicate-declaration bug slipped
through undetected. These tests close that gap.
"""
import json
import os

from _app_js import run_app_js

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_JS = os.path.join(REPO_ROOT, "static", "app.js")


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
    // Auto-fade timers are captured (not dropped) so tests can trigger them
    // deterministically instead of racing a real 6-second timeout.
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
    script = (
        _SHIM
        + "const fs = require('fs');"
        + f"eval(fs.readFileSync({json.dumps(APP_JS)}, 'utf8'));"
        + "globalThis.state = globalThis.window.state;"
        + "process.stdout.write(JSON.stringify(" + expr + "));"
    )
    proc = subprocess.run(
        ["node", "-e", script],
        check=False, capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout)


def _plan(name, dedup_key, severity="error", message="oops", story_key="S1"):
    return {
        "name": name,
        "latest_notification": {
            "dedup_key": dedup_key,
            "severity": severity,
            "message": message,
            "story_key": story_key,
        },
    }


# === pickNewNotifications: dedup-by-key ====================================

def test_pick_new_notifications_returns_unseen_record():
    plans = [_plan("demo", "k1")]
    expr = "pickNewNotifications(" + json.dumps(plans) + ", new Map())"
    res = _run_app_js(expr)
    assert len(res) == 1
    assert res[0]["plan"]["name"] == "demo"
    assert res[0]["record"]["dedup_key"] == "k1"


def test_pick_new_notifications_dedups_already_seen_key():
    """A plan whose latest dedup_key matches the seen map must NOT be
    returned again — this is the dedup-by-key contract the toast pipeline
    relies on to avoid re-toasting the same notification every poll."""
    plans = [_plan("demo", "k1")]
    expr = (
        "pickNewNotifications(" + json.dumps(plans) + ", new Map([['demo', 'k1']]))"
    )
    res = _run_app_js(expr)
    assert res == []


def test_pick_new_notifications_returns_new_key_after_previous_seen():
    """A plan advancing to a NEW dedup_key (different from what's seen) must
    be returned even though an older key for the same plan was already
    seen."""
    plans = [_plan("demo", "k2")]
    expr = (
        "pickNewNotifications(" + json.dumps(plans) + ", new Map([['demo', 'k1']]))"
    )
    res = _run_app_js(expr)
    assert len(res) == 1
    assert res[0]["record"]["dedup_key"] == "k2"


def test_pick_new_notifications_skips_plan_with_no_notification():
    """Negative/boundary case: a plan with no latest_notification (or a null
    dedup_key) must be silently skipped, not raise or produce a bogus toast."""
    plans = [{"name": "demo", "latest_notification": None}]
    expr = "pickNewNotifications(" + json.dumps(plans) + ", new Map())"
    res = _run_app_js(expr)
    assert res == []


def test_pick_new_notifications_empty_plans_returns_empty():
    expr = "pickNewNotifications([], new Map())"
    res = _run_app_js(expr)
    assert res == []


# === pushToast: DOM creation, dismiss, auto-fade, Ask button ===============

def test_push_toast_appends_node_to_stack():
    expr = (
        "(() => {"
        " pushToast({ severity: 'info', planName: 'demo', storyKey: 'S1', message: 'hello' });"
        " const stack = document.getElementById('toast-stack');"
        " return { count: stack.__children.length, cls: stack.__children[0].className };"
        " })()"
    )
    res = _run_app_js(expr)
    assert res["count"] == 1
    assert res["cls"] == "toast"


def test_push_toast_dismiss_button_removes_node():
    expr = (
        "(() => {"
        " pushToast({ severity: 'info', planName: 'demo', storyKey: 'S1', message: 'hello' });"
        " const stack = document.getElementById('toast-stack');"
        " const node = stack.__children[0];"
        " const dismiss = node.querySelector('.toast-dismiss');"
        " dismiss.__fire('click');"
        " return { countAfter: stack.__children.length };"
        " })()"
    )
    res = _run_app_js(expr)
    assert res["countAfter"] == 0


def test_push_toast_info_severity_auto_fades_after_timeout():
    """A non-error/warning toast must schedule an auto-remove timer instead
    of waiting on the user to dismiss it."""
    expr = (
        "(() => {"
        " pushToast({ severity: 'info', planName: 'demo', storyKey: 'S1', message: 'hello' });"
        " const stack = document.getElementById('toast-stack');"
        " const beforeTimers = globalThis.__pendingTimers.length;"
        " globalThis.__runTimers();"
        " return { beforeTimers, countAfter: stack.__children.length };"
        " })()"
    )
    res = _run_app_js(expr)
    assert res["beforeTimers"] == 1, "info-severity toast did not schedule an auto-fade timer"
    assert res["countAfter"] == 0, "auto-fade timer did not remove the toast node"


def test_push_toast_error_severity_does_not_auto_fade():
    """Negative/boundary case: error/warning toasts require an explicit
    dismiss or Ask click — they must NOT be silently auto-removed, since
    they represent something the user needs to act on."""
    expr = (
        "(() => {"
        " pushToast({ severity: 'error', planName: 'demo', storyKey: 'S1', message: 'boom' });"
        " return { pendingTimers: globalThis.__pendingTimers.length };"
        " })()"
    )
    res = _run_app_js(expr)
    assert res["pendingTimers"] == 0


def test_push_toast_error_severity_shows_ask_button():
    expr = (
        "(() => {"
        " pushToast({ severity: 'error', planName: 'demo', storyKey: 'S1', message: 'boom' });"
        " const stack = document.getElementById('toast-stack');"
        " const node = stack.__children[0];"
        " const ask = node.querySelector('.toast-ask');"
        " return { hasAsk: !!ask, askText: ask ? ask.textContent : null };"
        " })()"
    )
    res = _run_app_js(expr)
    assert res["hasAsk"] is True
    assert res["askText"] == "Ask"


def test_push_toast_info_severity_has_no_ask_button():
    """Negative/boundary case: routine info toasts don't warrant the
    Ask-Tower jump into Comms — only error/warning severities do."""
    expr = (
        "(() => {"
        " pushToast({ severity: 'info', planName: 'demo', storyKey: 'S1', message: 'fyi' });"
        " const stack = document.getElementById('toast-stack');"
        " const node = stack.__children[0];"
        " return { hasAsk: !!node.querySelector('.toast-ask') };"
        " })()"
    )
    res = _run_app_js(expr)
    assert res["hasAsk"] is False


def test_push_toast_ask_button_jumps_to_comms_and_fills_input():
    """Clicking Ask must switch into the Comms view (state.commsActive) and
    pre-fill the comms input with a question referencing the story key, then
    dismiss the toast."""
    expr = (
        "(() => {"
        " pushToast({ severity: 'error', planName: 'demo', storyKey: 'S1', message: 'boom' });"
        " const stack = document.getElementById('toast-stack');"
        " const node = stack.__children[0];"
        " const ask = node.querySelector('.toast-ask');"
        " ask.__fire('click');"
        " const input = document.getElementById('comms-input');"
        " return { commsActive: state.commsActive, inputValue: input.value, countAfter: stack.__children.length };"
        " })()"
    )
    res = _run_app_js(expr)
    assert res["commsActive"] is True
    assert "S1" in res["inputValue"]
    assert res["countAfter"] == 0


def test_push_toast_missing_stack_element_is_a_noop():
    """Boundary case: if #toast-stack doesn't exist in the DOM, pushToast
    must not throw."""
    expr = (
        "(() => {"
        " const original = document.getElementById;"
        " document.getElementById = (id) => (id === 'toast-stack' ? null : original(id));"
        " let threw = false;"
        " try { pushToast({ severity: 'info', planName: 'demo', storyKey: 'S1', message: 'hi' }); }"
        " catch (e) { threw = true; }"
        " document.getElementById = original;"
        " return { threw };"
        " })()"
    )
    res = _run_app_js(expr)
    assert res["threw"] is False
