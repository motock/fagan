"""Node-eval harness tests for the notifications-panel incremental-diff
refactor in static/app.js (`_diffNotificationsPanel`).

This file mirrors the harness pattern in tests/unit/test_dashboard_board_diff.py
and tests/unit/test_dashboard.py: it builds a minimal (but richer) DOM shim,
evals static/app.js under `node -e`, and JSON-stringifies the result of a test
expression. The shim is copied + extended here (rather than imported) so this
file stands alone and does not touch the other test files.

EXTENSION TO THE SHIM (this file only): `document.createElement` tags every
created element with a unique incrementing `__testId` number, and
`appendChild` / `innerHTML=""` actually maintain a `children` array on each
node, and `innerHTML` parses simple open/close tag pairs so
`querySelector`/`querySelectorAll` can locate `.notification` rows and
`[data-dedup-key=...]`. This lets a test detect whether a `.notification` row
node was REUSED across two `_diffNotificationsPanel` calls (same `__testId`)
or RECREATED (new `__testId`), and whether a row was appended vs. skipped.

These tests are RED until the implementation lands:
  - a new function `_diffNotificationsPanel(panelBodyEl, records)` must exist
    that appends DOM nodes only for records whose `dedup_key` is not already
    present as a rendered `.notification`-row child of `panelBodyEl` (read off
    a `data-dedup-key` attribute stamped on each rendered row),
  - `renderNotifications`'s row markup must carry the new `data-dedup-key`
    attribute on each `.notification` row,
  - `_diffNotificationsPanel` must be wired into the poll-triggered path of
    `refresh()` (the automatic update of an already-rendered plan detail), NOT
    the severity-filter click path (which keeps the full
    `renderNotifications`/`renderPlanDetail` re-render),
  - `_diffNotificationsPanel` must be added to module.exports.

`_diffNotificationsPanel` is exercised directly here by calling it twice
against the SAME container element (the contract the implementer must
satisfy). The brief says the row class is `.notification` — but the EXISTING
`renderNotifications` markup wraps each record in `<div class="log-line">`.
The implementer must add a `data-dedup-key` attribute to each rendered row;
which row class the diff keys on is an implementation detail, so these tests
locate rows by the `data-dedup-key` attribute itself (via
`querySelectorAll('[data-dedup-key]')`) rather than by a class name, and they
do NOT assert the row class name — only that the attribute is present and
that the dedup behavior holds.
"""
import json
import os

from tests.unit._app_js import run_app_js as _shared_run_app_js

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_JS = os.path.join(REPO_ROOT, "static", "app.js")
# renderPlanDetail (and the notif-severity filter-chip click handler) were
# relocated out of static/app.js into this dedicated render module
# (server-app-file-split plan); the static-source assertions below follow
# them here. renderNotifications/_diffNotificationsPanel stayed in app.js.
PLAN_DETAIL_JS = os.path.join(REPO_ROOT, "static", "app", "render", "plan-detail.js")


# A self-contained, richer DOM shim. Built once as a Python string and
# embedded in every node invocation. The shim implements just enough of the
# DOM for _diffNotificationsPanel: createElement with unique __testId,
# appendChild maintaining a children array, innerHTML parsing simple
# open/close tag pairs into child nodes, and querySelector/querySelectorAll
# matching a single class selector or a [data-*="..."] attribute selector.
_SHIM = r"""
    const noop = () => {};
    let __testIdCounter = 0;

    // Parse a tiny subset of HTML (open/close tag pairs and text) into child
    // element nodes. Each parsed element gets its own __testId so it is
    // distinguishable, and supports class-based + [data-*] querySelector.
    // Stack-based tokenizer (not a lazy backreference regex): a naive
    // `<(\w+)...>(.*?)<\/\1>` match mis-pairs on same-tag nesting. Mirrors
    // the stack-based tokenizer in test_dashboard_board_diff.py's shim.
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
            tagName: (tag || "div").toUpperCase(),
            className: "",
            title: "",
            type: "",
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
            addEventListener: noop,
            removeEventListener: noop,
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
            insertBefore: (child, ref) => {
                if (child.__parent) {
                    const i = child.__parent.__children.indexOf(child);
                    if (i >= 0) child.__parent.__children.splice(i, 1);
                }
                if (ref) {
                    const idx = el.__children.indexOf(ref);
                    if (idx >= 0) el.__children.splice(idx, 0, child);
                    else el.__children.push(child);
                } else {
                    el.__children.push(child);
                }
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
            querySelector: (sel) => {
                return el.__queryAll(sel)[0] || null;
            },
            querySelectorAll: (sel) => {
                return el.__queryAll(sel);
            },
            contains: (other) => {
                if (!other) return false;
                if (other === el) return true;
                const walk = (node) => {
                    for (const c of node.__children) {
                        if (c === other) return true;
                        if (walk(c)) return true;
                    }
                    return false;
                };
                return walk(el);
            },
            focus: noop,
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
    globalThis.document = {
        addEventListener: noop,
        documentElement: { dataset: {} },
        activeElement: null,
        body: __newEl("body"),
        hidden: false,
        getElementById: (id) => {
            if (id === "plan-list") return __nav;
            if (id === "plan-detail") return __detail;
            return __newEl("div");
        },
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
    globalThis.setTimeout = (fn, _ms) => { if (typeof fn === "function") { /* dropped */ } return 0; };
"""


def _run_app_js(expr):
    """Evaluate a JS expression inside an environment where static/app.js
    has been loaded. Returns the JSON-serialized result."""
    proc = _shared_run_app_js(expr, shim=_SHIM)
    if proc.returncode != 0:
        raise AssertionError(f"node failed: {proc.stderr}")
    return json.loads(proc.stdout)


def _app_js_source():
    """Read static/app.js source for static-source assertions."""
    with open(APP_JS, encoding="utf-8") as fh:
        return fh.read()


def _rec(dedup_key, message="msg", severity="info", ts="2024-01-01T00:00:00Z"):
    """Build a notification record dict shaped like plan.notification_records."""
    return {
        "dedup_key": dedup_key,
        "message": message,
        "severity": severity,
        "ts": ts,
    }


# === Static-source existence + signature assertions =========================

def test_diff_notifications_panel_function_exists():
    """A new function `_diffNotificationsPanel(panelBodyEl, records)` must
    exist in static/app.js. It is the incremental-append unit for the
    notifications panel body."""
    js = _app_js_source()
    assert "_diffNotificationsPanel" in js
    assert "function _diffNotificationsPanel(" in js


def test_diff_notifications_panel_signature_two_params():
    """The function must be declared with the documented two-parameter
    signature (panel body element + records array)."""
    js = _app_js_source()
    assert "function _diffNotificationsPanel(panelBodyEl, records)" in js


def test_diff_notifications_panel_is_exported():
    """`_diffNotificationsPanel` must be present in module.exports so it can
    be tested directly via the node harness."""
    expr = (
        "(() => {"
        " const out = (typeof module !== 'undefined' && module.exports) ? module.exports : {};"
        " return typeof out._diffNotificationsPanel;"
        " })()"
    )
    assert _run_app_js(expr) == "function"


def test_render_notifications_row_carries_data_dedup_key_attribute():
    """renderNotifications's per-record row markup must stamp a
    `data-dedup-key` attribute on each rendered row so the diff can read
    already-rendered keys off the DOM. The existing renderNotifications
    function (used for the FIRST render and the severity-filter re-render)
    must remain otherwise untouched."""
    js = _app_js_source()
    assert "data-dedup-key" in js


def test_render_notifications_full_function_still_exists():
    """The original full-rebuild `renderNotifications` function must remain
    (it is still used for the first render and the severity-filter-change
    re-render). This story adds a diff path, it does not remove the
    full-rebuild function."""
    js = _app_js_source()
    assert "function renderNotifications(records)" in js


def test_diff_notifications_panel_wired_into_refresh_poll_path():
    """`_diffNotificationsPanel` must be wired into the poll-triggered path
    of `refresh()` (the automatic update of an already-rendered plan
    detail), NOT only declared. The brief says this is a targeted addition
    to the poll path. Assert the call appears somewhere in the file after
    the function declaration (i.e. it is actually invoked, not just
    defined)."""
    js = _app_js_source()
    decl = js.index("function _diffNotificationsPanel(")
    # Find an invocation (a call site) after the declaration. The
    # declaration line itself contains "_diffNotificationsPanel(" so skip
    # past the signature line.
    after = js[decl + 1:]
    assert "_diffNotificationsPanel(" in after, (
        "_diffNotificationsPanel must be called somewhere (not just declared)"
    )


def test_severity_filter_click_still_calls_full_render_plan_detail():
    """The severity-filter chip click handler must continue to call the
    FULL `renderPlanDetail` re-render exactly as today — this story's diff
    path applies ONLY to the automatic poll-triggered path, not to a
    user's explicit filter click. Assert the notif-severity click handler
    still calls renderPlanDetail (unchanged). (Now in
    static/app/render/plan-detail.js.)"""
    with open(PLAN_DETAIL_JS, encoding="utf-8") as fh:
        js = fh.read()
    # The existing handler: querySelectorAll('.filter-chip[data-dim="notif-severity"]')
    # ... addEventListener("click", () => { notifSeverityFilter = ...; renderPlanDetail(plan); })
    assert 'data-dim="notif-severity"' in js
    # Locate the notif-severity handler block and confirm it still calls
    # renderPlanDetail (the full re-render), not _diffNotificationsPanel.
    anchor = js.index('data-dim="notif-severity"')
    block = js[anchor:anchor + 400]
    assert "renderPlanDetail(plan)" in block, (
        "notif-severity click handler must still call renderPlanDetail(plan)"
    )
    assert "_diffNotificationsPanel" not in block, (
        "notif-severity click handler must NOT use the diff path; it keeps "
        "the full re-render"
    )


# === Behavioral: dedup by dedup_key =========================================

def _diff_twice_expr(records_js_a, records_js_b):
    """Build a JS expression that:
      1. creates a fresh panel-body element,
      2. seeds it with the FIRST render via renderNotifications (so the
         panel starts populated exactly as it would after the first
         renderPlanDetail), capturing each rendered row's __testId,
      3. calls _diffNotificationsPanel with records_b,
      4. returns { appendedCount, beforeIds, afterIds, afterCount }.

    `appendedCount` = number of NEW child nodes appended by the diff call
    (after child count minus before child count). `beforeIds`/`afterIds`
    are the __testId values of the rows that carry a data-dedup-key
    attribute, in DOM order — so a test can assert existing rows were NOT
    recreated (same __testId) and that new rows were appended.
    """
    return (
        "(() => {"
        " const panel = document.createElement('div');"
        " panel.innerHTML = renderNotifications(" + records_js_a + ");"
        " const rowsBefore = panel.querySelectorAll('[data-dedup-key]');"
        " const beforeIds = Array.from(rowsBefore).map(r => r.__testId);"
        " const beforeCount = panel.__children.length;"
        " _diffNotificationsPanel(panel, " + records_js_b + ");"
        " const rowsAfter = panel.querySelectorAll('[data-dedup-key]');"
        " const afterIds = Array.from(rowsAfter).map(r => r.__testId);"
        " const afterCount = panel.__children.length;"
        " return {"
        "  appendedCount: afterCount - beforeCount,"
        "  beforeIds, afterIds, beforeCount, afterCount"
        " };"
        " })()"
    )


def test_same_records_second_call_appends_nothing():
    """`_diffNotificationsPanel` called once (via the seed render) with 2
    records, then again with the SAME 2 records: no new DOM nodes are
    appended on the second call. The total child count is unchanged."""
    recs = [
        _rec("k1", message="first"),
        _rec("k2", message="second"),
    ]
    result = _run_app_js(_diff_twice_expr(json.dumps(recs), json.dumps(recs)))
    assert result["appendedCount"] == 0, (
        f"expected 0 appended nodes on second call with same records, "
        f"got {result['appendedCount']}"
    )
    assert result["beforeCount"] == result["afterCount"]


def test_special_char_dedup_key_second_call_appends_nothing():
    """A dedup_key containing HTML special characters (`&`, `<`, `>`, `"`)
    must still dedup correctly: a second _diffNotificationsPanel call with the
    same records appends zero new rows. This guards against a regression that
    re-introduces escapeHtml on the comparison key (which would never match the
    unescaped getAttribute value and append duplicates)."""
    recs = [
        _rec('a&b<c>d"e', message="first"),
    ]
    result = _run_app_js(_diff_twice_expr(json.dumps(recs), json.dumps(recs)))
    assert result["appendedCount"] == 0, (
        f"expected 0 appended nodes for special-char dedup_key, "
        f"got {result['appendedCount']}"
    )
    assert result["beforeCount"] == result["afterCount"]


def test_existing_rows_not_recreated_on_second_call():
    """The 2 existing rows' `__testId` values are unchanged (not recreated)
    when the second call passes the same records. The diff must NOT remove
    or reorder existing rows."""
    recs = [
        _rec("k1", message="first"),
        _rec("k2", message="second"),
    ]
    result = _run_app_js(_diff_twice_expr(json.dumps(recs), json.dumps(recs)))
    assert result["beforeIds"] == result["afterIds"], (
        "existing rows must not be recreated or reordered on a no-op diff"
    )
    assert len(result["beforeIds"]) == 2


def test_one_new_record_appends_exactly_one_node():
    """`_diffNotificationsPanel` called again with 1 additional NEW record
    (new `dedup_key`): exactly 1 new node is appended; the 2 existing
    nodes' `__testId` values are unchanged (not recreated)."""
    recs_a = [
        _rec("k1", message="first"),
        _rec("k2", message="second"),
    ]
    recs_b = [
        _rec("k1", message="first"),
        _rec("k2", message="second"),
        _rec("k3", message="third"),
    ]
    result = _run_app_js(_diff_twice_expr(json.dumps(recs_a), json.dumps(recs_b)))
    assert result["appendedCount"] == 1, (
        f"expected exactly 1 appended node for the new record, "
        f"got {result['appendedCount']}"
    )
    # The first two rows survive with the same __testId (not recreated).
    assert result["beforeIds"] == result["afterIds"][:2], (
        "the 2 pre-existing rows must keep their __testId (not recreated)"
    )
    # A third row now exists.
    assert len(result["afterIds"]) == 3


def test_new_row_carries_its_dedup_key():
    """The newly appended row must itself carry a `data-dedup-key`
    attribute matching the new record's key, so a SUBSEQUENT diff call
    would dedup against it (the dedup set is read off the DOM, not held in
    a closure)."""
    recs_a = [_rec("k1", message="first")]
    recs_b = [_rec("k1", message="first"), _rec("k2", message="second")]
    expr = (
        "(() => {"
        " const panel = document.createElement('div');"
        " panel.innerHTML = renderNotifications(" + json.dumps(recs_a) + ");"
        " _diffNotificationsPanel(panel, " + json.dumps(recs_b) + ");"
        " const rows = panel.querySelectorAll('[data-dedup-key]');"
        " return Array.from(rows).map(r => r.getAttribute('data-dedup-key'));"
        " })()"
    )
    keys = _run_app_js(expr)
    assert "k2" in keys, "newly appended row must carry its data-dedup-key"


def test_third_call_with_already_appended_key_appends_nothing():
    """After appending k3 in the second call, a THIRD call that again
    includes k3 (and the originals) must append nothing — the dedup set is
    read off the DOM, so the previously-appended row is now dedup-able."""
    recs_a = [_rec("k1"), _rec("k2")]
    recs_b = [_rec("k1"), _rec("k2"), _rec("k3")]
    expr = (
        "(() => {"
        " const panel = document.createElement('div');"
        " panel.innerHTML = renderNotifications(" + json.dumps(recs_a) + ");"
        " _diffNotificationsPanel(panel, " + json.dumps(recs_b) + ");"
        " const before = panel.__children.length;"
        " _diffNotificationsPanel(panel, " + json.dumps(recs_b) + ");"
        " const after = panel.__children.length;"
        " return { before, after, delta: after - before };"
        " })()"
    )
    result = _run_app_js(expr)
    assert result["delta"] == 0, (
        "third call with already-appended k3 must append nothing"
    )


# === Negative / boundary ===================================================

def test_empty_records_on_empty_panel_does_not_throw_and_appends_nothing():
    """`_diffNotificationsPanel(panelBodyEl, [])` on an empty existing panel
    does not throw and appends nothing."""
    expr = (
        "(() => {"
        " const panel = document.createElement('div');"
        " let ok = true, err = null, appended = -1;"
        " try {"
        "  const before = panel.__children.length;"
        "  _diffNotificationsPanel(panel, []);"
        "  appended = panel.__children.length - before;"
        " } catch (e) { ok = false; err = String(e); }"
        " return { ok, err, appended };"
        " })()"
    )
    result = _run_app_js(expr)
    assert result["ok"] is True, f"threw: {result.get('err')}"
    assert result["appended"] == 0


def test_empty_records_on_populated_panel_appends_nothing_and_keeps_rows():
    """`_diffNotificationsPanel` with [] against an already-populated
    panel must not throw, must append nothing, and must NOT remove the
    existing rows (notifications are display-only history; the diff only
    appends, never removes)."""
    recs = [_rec("k1"), _rec("k2")]
    expr = (
        "(() => {"
        " const panel = document.createElement('div');"
        " panel.innerHTML = renderNotifications(" + json.dumps(recs) + ");"
        " const rowsBefore = panel.querySelectorAll('[data-dedup-key]').length;"
        " let ok = true, err = null;"
        " try { _diffNotificationsPanel(panel, []); } catch (e) { ok = false; err = String(e); }"
        " const rowsAfter = panel.querySelectorAll('[data-dedup-key]').length;"
        " return { ok, err, rowsBefore, rowsAfter };"
        " })()"
    )
    result = _run_app_js(expr)
    assert result["ok"] is True, f"threw: {result.get('err')}"
    assert result["rowsAfter"] == result["rowsBefore"], (
        "empty records must not remove existing rows"
    )
    assert result["rowsAfter"] == 2


def test_undefined_records_does_not_throw():
    """`_diffNotificationsPanel(panel, undefined)` must not throw (treat as
    no records)."""
    expr = (
        "(() => {"
        " const panel = document.createElement('div');"
        " let ok = true, err = null;"
        " try { _diffNotificationsPanel(panel, undefined); } catch (e) { ok = false; err = String(e); }"
        " return { ok, err };"
        " })()"
    )
    result = _run_app_js(expr)
    assert result["ok"] is True, f"threw: {result.get('err')}"


def test_null_records_does_not_throw():
    """`_diffNotificationsPanel(panel, null)` must not throw."""
    expr = (
        "(() => {"
        " const panel = document.createElement('div');"
        " let ok = true, err = null;"
        " try { _diffNotificationsPanel(panel, null); } catch (e) { ok = false; err = String(e); }"
        " return { ok, err };"
        " })()"
    )
    result = _run_app_js(expr)
    assert result["ok"] is True, f"threw: {result.get('err')}"


# === Missing / undefined dedup_key ==========================================
#
# The brief: "Records with a missing/undefined dedup_key are still appended
# once (treat as always-new, since there's nothing to dedup against) but a
# second call with the exact same missing-key record does not infinite-loop
# or throw (document your chosen behavior - either treat it as always-new
# on every call, or key by an alternate stable field like ts+message; pick
# one deterministic rule and assert it)."
#
# We assert the MINIMUM contract that any reasonable implementation must
# satisfy: a missing-key record is appended on the first call, and a second
# call with the same missing-key record does not throw and does not
# infinite-loop (terminates within the harness timeout). We do NOT pin
# whether the second call re-appends (always-new) or dedups — both are
# permitted by the brief — so we only assert termination + no-throw on the
# second call, plus that the first call appended at least one node.

def test_missing_dedup_key_appended_on_first_call():
    """A record with a missing/undefined `dedup_key` is still appended on
    the first call (treat as always-new, since there's nothing to dedup
    against)."""
    rec = {"message": "no key", "severity": "info", "ts": "2024-01-01T00:00:00Z"}
    expr = (
        "(() => {"
        " const panel = document.createElement('div');"
        " panel.innerHTML = renderNotifications([]);"  # start empty panel
        " const before = panel.__children.length;"
        " _diffNotificationsPanel(panel, " + json.dumps([rec]) + ");"
        " const after = panel.__children.length;"
        " return { appended: after - before };"
        " })()"
    )
    result = _run_app_js(expr)
    assert result["appended"] >= 1, (
        "a missing-dedup_key record must be appended on the first call"
    )


def test_missing_dedup_key_second_call_does_not_throw_or_loop():
    """A second call with the exact same missing-key record does not
    infinite-loop or throw. The brief permits either behavior (always-new
    re-append, or dedup by an alternate stable field); we only assert
    termination + no-throw, not which one."""
    rec = {"message": "no key", "severity": "info", "ts": "2024-01-01T00:00:00Z"}
    expr = (
        "(() => {"
        " const panel = document.createElement('div');"
        " panel.innerHTML = renderNotifications([]);"
        " _diffNotificationsPanel(panel, " + json.dumps([rec]) + ");"
        " let ok = true, err = null;"
        " try { _diffNotificationsPanel(panel, " + json.dumps([rec]) + "); }"
        " catch (e) { ok = false; err = String(e); }"
        " return { ok, err };"
        " })()"
    )
    result = _run_app_js(expr)
    assert result["ok"] is True, (
        f"second call with same missing-key record must not throw: {result.get('err')}"
    )


def test_undefined_dedup_key_appended_on_first_call():
    """A record with dedup_key explicitly set to undefined is still
    appended on the first call."""
    expr = (
        "(() => {"
        " const panel = document.createElement('div');"
        " panel.innerHTML = renderNotifications([]);"
        " const before = panel.__children.length;"
        " _diffNotificationsPanel(panel, [{ message: 'undef key', severity: 'info', ts: 't', dedup_key: undefined }]);"
        " const after = panel.__children.length;"
        " return { appended: after - before };"
        " })()"
    )
    result = _run_app_js(expr)
    assert result["appended"] >= 1


def test_null_dedup_key_appended_on_first_call():
    """A record with dedup_key explicitly null is still appended on the
    first call (null is not a usable dedup key)."""
    expr = (
        "(() => {"
        " const panel = document.createElement('div');"
        " panel.innerHTML = renderNotifications([]);"
        " const before = panel.__children.length;"
        " _diffNotificationsPanel(panel, [{ message: 'null key', severity: 'info', ts: 't', dedup_key: null }]);"
        " const after = panel.__children.length;"
        " return { appended: after - before };"
        " })()"
    )
    result = _run_app_js(expr)
    assert result["appended"] >= 1


# === Boundary: single record ===============================================

def test_single_record_appended_to_empty_panel():
    """A single new record appended to an empty (no-row) panel appends
    exactly one node."""
    expr = (
        "(() => {"
        " const panel = document.createElement('div');"
        " panel.innerHTML = renderNotifications([]);"
        " const before = panel.__children.length;"
        " _diffNotificationsPanel(panel, " + json.dumps([_rec("solo")]) + ");"
        " const after = panel.__children.length;"
        " return { appended: after - before };"
        " })()"
    )
    result = _run_app_js(expr)
    assert result["appended"] == 1


# === No removal / no reorder ==============================================

def test_diff_does_not_remove_rows_absent_from_new_records():
    """The diff must NOT remove rows whose dedup_key is absent from the new
    records array. Notifications are display-only history; the diff only
    appends, never removes (the brief: "Do not remove or reorder existing
    rows")."""
    recs_a = [_rec("k1"), _rec("k2"), _rec("k3")]
    recs_b = [_rec("k1")]  # k2, k3 absent
    expr = (
        "(() => {"
        " const panel = document.createElement('div');"
        " panel.innerHTML = renderNotifications(" + json.dumps(recs_a) + ");"
        " const beforeIds = Array.from(panel.querySelectorAll('[data-dedup-key]')).map(r => r.__testId);"
        " _diffNotificationsPanel(panel, " + json.dumps(recs_b) + ");"
        " const afterIds = Array.from(panel.querySelectorAll('[data-dedup-key]')).map(r => r.__testId);"
        " return { beforeIds, afterIds };"
        " })()"
    )
    result = _run_app_js(expr)
    # All three original rows must still be present (not removed).
    assert result["afterIds"][:3] == result["beforeIds"], (
        "diff must not remove rows absent from the new records"
    )
    assert len(result["afterIds"]) == 3


def test_diff_does_not_reorder_existing_rows():
    """The diff must not reorder existing rows. Append-only: existing rows
    keep their relative order; new rows land at the end."""
    recs_a = [_rec("k1"), _rec("k2")]
    recs_b = [_rec("k1"), _rec("k2"), _rec("k3")]
    expr = (
        "(() => {"
        " const panel = document.createElement('div');"
        " panel.innerHTML = renderNotifications(" + json.dumps(recs_a) + ");"
        " const beforeKeys = Array.from(panel.querySelectorAll('[data-dedup-key]')).map(r => r.getAttribute('data-dedup-key'));"
        " _diffNotificationsPanel(panel, " + json.dumps(recs_b) + ");"
        " const afterKeys = Array.from(panel.querySelectorAll('[data-dedup-key]')).map(r => r.getAttribute('data-dedup-key'));"
        " return { beforeKeys, afterKeys };"
        " })()"
    )
    result = _run_app_js(expr)
    # The first two keys must still be k1, k2 in that order (not reordered).
    assert result["afterKeys"][:2] == result["beforeKeys"], (
        "existing rows must not be reordered"
    )
    # The new key lands at the end.
    assert result["afterKeys"][2] == "k3"


# === End-to-end: renderPlanDetail poll path uses the diff ==================
#
# The brief: wire `_diffNotificationsPanel` into the notifications
# `.panel-body` specifically when `refresh()`'s poll updates an
# already-rendered plan detail. The cleanest behavioral proxy that does
# not require driving the async refresh() fetch loop is a source-text
# check that the diff call is invoked from within refresh()'s body (the
# poll path), which is distinct from the filter-click handler (already
# asserted above to keep the full re-render).

def test_diff_called_from_within_refresh_body():
    """`_diffNotificationsPanel` must be invoked from within `refresh`'s
    body (the poll-triggered path), not merely declared. Locate the
    refresh function body and assert a call site appears inside it."""
    js = _app_js_source()
    # refresh is an async function declared as `async function refresh(...)`
    # or `const refresh = async function ...` or `function refresh`. Find
    # the declaration and scan to the next top-level function/export for the
    # body extent.
    assert "function refresh" in js or "refresh =" in js, (
        "refresh function must exist in app.js"
    )
    # Find the refresh declaration start.
    idx = js.find("function refresh")
    if idx == -1:
        idx = js.find("refresh =")
    assert idx != -1
    # The call site must appear after the refresh declaration. We don't
    # need an exact end boundary: it is enough that SOME call to
    # _diffNotificationsPanel appears after refresh starts, because the
    # only other call site (if any) would be the declaration itself (which
    # is before refresh). The filter-click handler is inside renderPlanDetail
    # (also before refresh in the file), and we already asserted it does
    # NOT use the diff. So a call after refresh's start is the poll path.
    after_refresh = js[idx:]
    # Skip the declaration's own signature line by searching for a call
    # that is NOT the `function _diffNotificationsPanel(` declaration.
    # A call site looks like `_diffNotificationsPanel(` followed by
    # something that is not `panelBodyEl, records)` (the signature).
    calls = []
    search = after_refresh
    pos = 0
    while True:
        i = search.find("_diffNotificationsPanel(", pos)
        if i == -1:
            break
        # Is this the declaration signature? The signature is
        # `function _diffNotificationsPanel(panelBodyEl, records)`.
        snippet = search[i:i + 60]
        if not snippet.startswith("function _diffNotificationsPanel("):
            calls.append(i)
        pos = i + 1
    assert calls, (
        "_diffNotificationsPanel must be called from within refresh()'s "
        "poll path (a call site must appear after the refresh declaration)"
    )


def test_render_plan_detail_unchanged_beyond_targeted_addition():
    """The brief's RENAME-AND-DELEGATE NOTE: do not restructure
    renderPlanDetail or renderNotifications beyond adding the one new call
    site and the new data-dedup-key attribute; make the smallest possible
    anchored edits. Assert renderPlanDetail and renderNotifications still
    exist with their original signatures (no rename, no signature change).
    (renderPlanDetail is now in static/app/render/plan-detail.js;
    renderNotifications stayed in static/app.js.)"""
    with open(PLAN_DETAIL_JS, encoding="utf-8") as fh:
        plan_detail_js = fh.read()
    assert "function renderPlanDetail(plan)" in plan_detail_js
    js = _app_js_source()
    assert "function renderNotifications(records)" in js
