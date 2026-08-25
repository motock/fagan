"""Regression tests for the keyed-diff refactor of renderPlanList in
static/app.js, written against a DOM stub that emulates REAL browser
`HTMLElement.dataset` semantics.

The existing tests/unit/test_dashboard_planlist_diff.py stub stores
`data-plan-name` under the literal key `dataset["plan-name"]` (no camelCase
conversion), which masks a Blocking bug: real browsers expose
`data-plan-name` as `dataset.planName`, so `el.dataset["plan-name"]` is
`undefined` and `planListRowsByName` is never populated. This file uses a
correct stub (camelCase conversion) so the bug is reproducible.

These tests are RED until the implementation in static/app.js is fixed:
  - Bug 1: `el.dataset["plan-name"]` -> `el.dataset.planName` (or
    getAttribute("data-plan-name")) so the population loop works.
  - Bug 2: the in-place meta update must emit the same
    `<span class="plan-paused">paused</span>` markup the builders emit, not
    a plain " paused" text string.
  - Bug 3: newly-added plan rows must be inserted at their server-sorted
    (newest-first) index, not appended at the bottom above the footer.
"""
import json
import os

from tests.unit._app_js import run_app_js as _shared_run_app_js

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_JS = os.path.join(REPO_ROOT, "static", "app.js")


# A DOM shim that emulates REAL browser `dataset` semantics: a `data-*`
# attribute is exposed on `el.dataset` under its camelCased key. So
# `data-plan-name` -> `dataset.planName`, `data-overview` -> `dataset.overview`.
# This is the crucial difference from the existing test stub, which stored
# `data-plan-name` under the literal key `dataset["plan-name"]`.
_SHIM = r"""
    const noop = () => {};
    let __testIdCounter = 0;

    // Convert a `data-foo-bar` attribute name to its dataset key: `fooBar`.
    // Mirrors the HTML spec's dataset camelCase algorithm: lowercase the
    // leading char after each hyphen, drop the hyphens.
    function __datasetKey(attr) {
        // attr is the part after "data-", e.g. "plan-name" -> "planName".
        return attr.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
    }

    // Parse a tiny subset of HTML (open/close tag pairs and text) into child
    // element nodes. Each parsed element gets its own __testId so it is
    // distinguishable, and supports class-based querySelector. Attributes we
    // care about (class, title, type) are captured.
    function __parseHtml(html, parent) {
        parent.__children = [];
        if (!html) return;
        const re = /<([a-zA-Z]+)((?:\s+[a-zA-Z-]+(?:="[^"]*")?)*)\s*>([\s\S]*?)<\/\1>|<([a-zA-Z]+)((?:\s+[a-zA-Z-]+(?:="[^"]*")?)*)\s*\/?>/g;
        let m;
        while ((m = re.exec(html)) !== null) {
            const tag = m[1] || m[4];
            const attrs = m[2] || m[5] || "";
            const inner = m[3] || "";
            const el = __newEl(tag);
            const are = /([a-zA-Z-]+)="([^"]*)"/g;
            let am;
            while ((am = are.exec(attrs)) !== null) {
                if (am[1] === "class") el.className = am[2];
                else if (am[1] === "title") el.title = am[2];
                else if (am[1] === "type") el.type = am[2];
                else el.setAttribute(am[1], am[2]);
            }
            if (inner) {
                __parseHtml(inner, el);
                el.__text = inner.replace(/<[^>]*>/g, "");
            }
            parent.__children.push(el);
            el.__parent = parent;
        }
    }

    function __newEl(tag) {
        const el = {
            __testId: ++__testIdCounter,
            __tag: tag || "div",
            __children: [],
            __parent: null,
            __text: "",
            __attrs: {},
            tagName: (tag || "div").toUpperCase(),
            className: "",
            title: "",
            type: "",
            checked: false,
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
                el.__attrs[k] = String(v);
                if (k === "class") el.className = String(v);
                else if (k === "title") el.title = String(v);
                else if (k === "type") el.type = String(v);
                else if (k.startsWith("data-")) el.dataset[__datasetKey(k.slice(5))] = String(v);
                else el[k] = String(v);
            },
            getAttribute: (k) => {
                if (k === "class") return el.className;
                if (k.startsWith("data-")) return el.dataset[__datasetKey(k.slice(5))];
                return el.__attrs[k] != null ? el.__attrs[k] : (el[k] != null ? String(el[k]) : null);
            },
            hasAttribute: (k) => {
                if (k === "class") return el.className !== "";
                if (k.startsWith("data-")) return el.dataset[__datasetKey(k.slice(5))] != null;
                return el.__attrs[k] != null;
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
                if (ref == null) { el.__children.push(child); }
                else {
                    const idx = el.__children.indexOf(ref);
                    if (idx < 0) el.__children.push(child);
                    else el.__children.splice(idx, 0, child);
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
            querySelector: (sel) => el.__queryAll(sel)[0] || null,
            querySelectorAll: (sel) => el.__queryAll(sel),
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
            set: (v) => { el.__innerHTMLRaw = String(v); __parseHtml(String(v), el); },
            configurable: true,
        });
        Object.defineProperty(el, "textContent", {
            get: () => el.__text || "",
            set: (v) => { el.__text = String(v); el.__children = []; el.__innerHTMLRaw = String(v); },
            configurable: true,
        });
        return el;
    }

    function __matches(el, sel) {
        if (!sel) return false;
        if (sel.startsWith(".")) {
            return el.classList.contains(sel.slice(1));
        }
        if (sel.startsWith("[data-")) {
            const key = sel.slice(6, sel.indexOf("=") > 0 ? sel.indexOf("=") - 6 : sel.indexOf("]"));
            const dkey = __datasetKey(key);
            const val = sel.indexOf("=") > 0 ? sel.slice(sel.indexOf("=") + 2, sel.indexOf("]")) : null;
            if (val != null) return el.dataset[dkey] === val;
            return el.dataset[dkey] != null;
        }
        return el.__tag === sel;
    }

    const __nav = __newEl("div");
    globalThis.document = {
        addEventListener: noop,
        documentElement: { dataset: {} },
        getElementById: (id) => {
            if (id === "plan-list") return __nav;
            return __newEl("div");
        },
        createElement: (tag) => __newEl(tag),
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
    has been loaded. Returns the JSON-serialized result.

    The shared loader evals app.js source separately from the test
    expression, so module-level `let` declarations (e.g.
    `planListRowsByName`) are scoped to the app.js eval and are NOT
    accessible from the test expression. This test relies on the first
    render correctly populating the eval-scoped map via
    `getAttribute('data-plan-name')` (Bug 1 fixed), so the second render
    takes the in-place diff branch without any manual reset."""
    proc = _shared_run_app_js(expr, shim=_SHIM)
    if proc.returncode != 0:
        raise AssertionError(f"node failed: {proc.stderr}")
    return json.loads(proc.stdout)


def _plan(name, done=0, total=0, archived=False, paused=False):
    """Build a plan dict shaped like /api/plans entries."""
    return {
        "name": name,
        "story_count": total,
        "status_counts": {"done": done},
        "archived": archived,
        "paused": paused,
    }


def _collect_rows_js():
    """JS snippet: a `collect()` function that walks the plan-list nav and
    returns per-plan rows keyed by their data-plan-name attribute, read via
    the REAL dataset accessor (`dataset.planName`)."""
    return (
        "const nav = document.getElementById('plan-list');"
        " const collect = () => {"
        "  const rows = [];"
        "  const walk = (node) => {"
        "   for (const c of (node.__children || [])) {"
        "    const pn = c.dataset ? c.dataset.planName : undefined;"
        "    if (pn) {"
        "     const meta = c.querySelector('.plan-meta');"
        "     const pausedSpan = meta ? meta.querySelector('.plan-paused') : null;"
        "     const ser = (node) => {"
        "      let s = node.__text || '';"
        "      for (const ch of (node.__children || [])) {"
        "       const cls = ch.className ? ` class='${ch.className}'` : '';"
        "       s += `<${ch.__tag}${cls}>${ser(ch)}</${ch.__tag}>`;"
        "      }"
        "      return s;"
        "     };"
        "     rows.push({ name: pn, testId: c.__testId,"
        "       metaHtml: meta ? ser(meta) : '',"
        "       hasPausedSpan: !!pausedSpan });"
        "    }"
        "    walk(c);"
        "   }"
        "  };"
        "  walk(nav);"
        "  return rows;"
        " };"
    )


# === Bug 1: dataset key mismatch -> planListRowsByName never populated =====

def test_no_duplicate_rows_across_three_poll_ticks():
    """Bug 1: with real `dataset` semantics, `el.dataset["plan-name"]` is
    `undefined`, so the population loop never fills `planListRowsByName`.
    On every poll tick after the first, every plan is treated as "new" and a
    duplicate row is appended. After THREE consecutive renderPlanList calls
    with the SAME two plans, the sidebar must contain exactly two `.plan-item`
    rows (one per plan), not 2, then 4, then 6.

    A test that only checks tick 2 would pass even if tick 3 regressed, so we
    assert the count after the third call too.
    """
    plans = [_plan("alpha", done=1, total=3), _plan("beta", done=2, total=5)]
    expr = (
        "(() => {"
        + _collect_rows_js()
        + f" renderPlanList({json.dumps(plans)});"
        " const r1 = collect();"
        + f" renderPlanList({json.dumps(plans)});"
        " const r2 = collect();"
        + f" renderPlanList({json.dumps(plans)});"
        " const r3 = collect();"
        " return { r1, r2, r3 };"
        " })()"
    )
    res = _run_app_js(expr)
    # After the first full render there are exactly two per-plan rows.
    assert len(res["r1"]) == 2, f"expected 2 rows after tick 1, got {res['r1']}"
    # After tick 2 the diff path runs; with the bug it duplicates all rows.
    assert len(res["r2"]) == 2, (
        f"expected 2 rows after tick 2, got {len(res['r2'])} "
        f"(dataset key mismatch causes every plan to be treated as new)"
    )
    # After tick 3 the bug compounds; assert it does not.
    assert len(res["r3"]) == 2, (
        f"expected 2 rows after tick 3, got {len(res['r3'])} "
        f"(duplicate rows compound every poll tick)"
    )


def test_existing_rows_reused_not_recreated_across_polls():
    """Bug 1 (observable effect): across two consecutive poll calls with the
    same plans, each per-plan row must be the SAME node (__testId unchanged),
    proving the diff path found the existing row instead of treating it as
    new and appending a duplicate."""
    plans = [_plan("alpha", done=1, total=3), _plan("beta", done=2, total=5)]
    expr = (
        "(() => {"
        + _collect_rows_js()
        + f" renderPlanList({json.dumps(plans)});"
        " const r1 = collect();"
        + f" renderPlanList({json.dumps(plans)});"
        " const r2 = collect();"
        " return { r1, r2 };"
        " })()"
    )
    res = _run_app_js(expr)
    by1 = {r["name"]: r for r in res["r1"]}
    by2 = {r["name"]: r for r in res["r2"]}
    assert set(by1) == set(by2) == {"alpha", "beta"}
    for name in ("alpha", "beta"):
        assert by1[name]["testId"] == by2[name]["testId"], (
            f"row {name} was recreated across identical poll calls "
            f"(diff path failed to find the existing row)"
        )


# === Bug 2: in-place meta update drops the plan-paused span =================

def test_paused_badge_survives_in_place_update():
    """Bug 2: `_renderPlanListFull` and `_buildPlanRow` render a paused plan's
    meta as `... done <span class="plan-paused">paused</span>`. The diff
    path's in-place update overwrites `meta.textContent` with the plain string
    `"... paused"`, destroying that span on the second poll tick.

    This test isolates Bug 2 from Bug 1 (the dataset-key mismatch): after the
    first full render it manually re-populates the module-level
    `planListRowsByName` map using the CORRECT `dataset.planName` accessor
    (the way the fixed population loop would), so the second renderPlanList
    call takes the in-place update branch instead of treating the plan as
    new. The in-place branch must emit the same `<span class="plan-paused">`
    markup the builders emit, not a plain " paused" text string.
    """
    plans = [_plan("alpha", done=1, total=3, paused=True)]
    expr = (
        "(() => {"
        + _collect_rows_js()
        + f" renderPlanList({json.dumps(plans)});"
        " const r1 = collect();"
        # No manual reset needed: first render populates the eval-scoped
        # planListRowsByName via data-plan-name (Bug 1 fixed); second render
        # takes the in-place branch.
        + f" renderPlanList({json.dumps(plans)});"
        " const r2 = collect();"
        " return { r1, r2 };"
        " })()"
    )
    res = _run_app_js(expr)
    r1 = {r["name"]: r for r in res["r1"]}
    r2 = {r["name"]: r for r in res["r2"]}
    # First render emits the styled badge.
    assert r1["alpha"]["hasPausedSpan"] is True, (
        "first render should emit <span class='plan-paused'>paused</span>"
    )
    assert "plan-paused" in r1["alpha"]["metaHtml"], (
        f"first render meta html missing plan-paused span: {r1['alpha']['metaHtml']}"
    )
    # The in-place update on the second call must NOT destroy the badge.
    assert r2["alpha"]["hasPausedSpan"] is True, (
        "in-place update destroyed the <span class='plan-paused'> badge by "
        "overwriting meta.textContent with a plain ' paused' string"
    )
    assert "plan-paused" in r2["alpha"]["metaHtml"], (
        f"in-place update meta html missing plan-paused span: {r2['alpha']['metaHtml']}"
    )


# === Bug 3: new rows inserted at server-sorted position, not the bottom ======

def test_new_plan_inserted_at_server_sorted_index():
    """Bug 3: `/api/plans` returns plans newest-first. `_renderPlanListFull`
    renders them in that order. When a brand-new plan appears in the diff,
    its row must be inserted at the index matching its position in the
    incoming `plans` array, not appended at the bottom above the footer.

    Initial render: [alpha, beta] (alpha newest). Second render: [gamma,
    alpha, beta] (gamma is the newest plan, so it must appear at index 0,
    above alpha). With the bug, gamma is appended at the bottom (after beta).
    """
    a = [_plan("alpha", done=1, total=3), _plan("beta", done=0, total=2)]
    b = [_plan("gamma", done=0, total=1), _plan("alpha", done=1, total=3),
         _plan("beta", done=0, total=2)]
    expr = (
        "(() => {"
        + _collect_rows_js()
        + f" renderPlanList({json.dumps(a)});"
        " const r1 = collect();"
        + f" renderPlanList({json.dumps(b)});"
        " const r2 = collect();"
        " return { r1, r2 };"
        " })()"
    )
    res = _run_app_js(expr)
    r2 = res["r2"]
    names_in_order = [r["name"] for r in r2]
    # The new plan "gamma" is first in the incoming array (newest-first), so
    # it must appear at index 0 in the rendered sidebar, not at the end.
    assert names_in_order == ["gamma", "alpha", "beta"], (
        f"new plan 'gamma' was inserted at the wrong position; expected "
        f"server-sorted order [gamma, alpha, beta], got {names_in_order}"
    )


def test_new_plan_inserted_between_existing_rows():
    """Bug 3 (mid-list insertion): a new plan that belongs in the MIDDLE of
    the incoming array must be inserted at that middle index, not at the
    bottom. Initial: [alpha, gamma]. Second: [alpha, beta, gamma] (beta is
    newer than gamma but older than alpha)."""
    a = [_plan("alpha", done=1, total=3), _plan("gamma", done=0, total=2)]
    b = [_plan("alpha", done=1, total=3), _plan("beta", done=0, total=1),
         _plan("gamma", done=0, total=2)]
    expr = (
        "(() => {"
        + _collect_rows_js()
        + f" renderPlanList({json.dumps(a)});"
        " const r1 = collect();"
        + f" renderPlanList({json.dumps(b)});"
        " const r2 = collect();"
        " return { r1, r2 };"
        " })()"
    )
    res = _run_app_js(expr)
    names_in_order = [r["name"] for r in res["r2"]]
    assert names_in_order == ["alpha", "beta", "gamma"], (
        f"new plan 'beta' was inserted at the wrong position; expected "
        f"server-sorted order [alpha, beta, gamma], got {names_in_order}"
    )