"""Node-eval harness tests for the top-level nav CHILD ORDER produced by
renderPlanList in static/app/render/plan-list.js.

This file mirrors the harness pattern in tests/unit/test_dashboard_planlist_diff_dataset.py:
it builds a minimal DOM shim with REAL browser `dataset` camelCase semantics
and `insertBefore` support, evals static/app.js (which re-exports
static/app/main.js, which imports renderPlanList from
static/app/render/plan-list.js) under `node --input-type=module`, and
JSON-stringifies the result of a test expression. The `_SHIM` string and the
`_run_app_js` / `_plan` helpers are copied here VERBATIM from that file (per
its own header comment: copying rather than importing is this repo's
established convention so each test file stands alone) because this file
needs the real-dataset + insertBefore shim, not the simpler no-op shim in
test_dashboard_comms_nav.py or test_dashboard_planlist_diff.py.

These tests are RED until the implementation lands in
static/app/render/plan-list.js:
  - `_renderPlanListFull` must stop clearing `nav.innerHTML` itself.
  - `renderPlanList`'s first-call branch must clear the nav, then append (in
    order) the pinned Comms item, the pinned Overview item, and a new
    `.plan-list-section-label` node with text "PLANS", BEFORE calling
    `_renderPlanListFull` (which appends the per-plan rows and the
    `.plan-list-footer` toggle last).
  - The Comms item's markup drops its `.plan-meta` subtitle and gains a
    `.icon-comms` child span; the Overview item's markup is unchanged.
"""
import json

from tests.unit._app_js import run_app_js as _shared_run_app_js

# A DOM shim that emulates REAL browser `dataset` semantics: a `data-*`
# attribute is exposed on `el.dataset` under its camelCased key. So
# `data-plan-name` -> `dataset.planName`, `data-overview` -> `dataset.overview`.
# This is the crucial difference from the simpler test stubs elsewhere, which
# store `data-plan-name` under the literal key `dataset["plan-name"]`. This
# shim also implements `insertBefore`, needed by the diff path.
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


def _markers_helper_js():
    """JS snippet: binds `nav` to the plan-list nav and defines a `markers()`
    function that walks nav's TOP-LEVEL children only (not a deep walk) and
    maps each one to a short marker string: "comms" / "overview" / "label" /
    "plan:<name>" / "footer" / "other"."""
    return (
        "const nav = document.getElementById('plan-list');"
        " const markers = () => nav.__children.map((c) => {"
        "  if (c.dataset && c.dataset.comms === 'true') return 'comms';"
        "  if (c.dataset && c.dataset.overview === 'true') return 'overview';"
        "  if (c.className && c.className.includes('plan-list-section-label')) return 'label';"
        "  if (c.dataset && c.dataset.planName) return 'plan:' + c.dataset.planName;"
        "  if (c.className && c.className.includes('plan-list-footer')) return 'footer';"
        "  return 'other';"
        " });"
    )


# === Criterion 1: top-level nav order, two plans ===========================

def test_nav_top_level_order_two_plans():
    """Top to bottom the sidebar must be: Comms, Overview, the "PLANS"
    section label, the per-plan rows in server-sorted order, then the
    "Show dismissed plans" footer."""
    plans = [_plan("alpha", done=1, total=2), _plan("beta", done=0, total=1)]
    expr = (
        "(() => {" + _markers_helper_js()
        + f" renderPlanList({json.dumps(plans)});"
        " return markers();"
        " })()"
    )
    order = _run_app_js(expr)
    assert order == ["comms", "overview", "label", "plan:alpha", "plan:beta", "footer"], (
        f"expected [comms, overview, label, plan:alpha, plan:beta, footer], got {order}"
    )


# === Criterion 2: exactly one "PLANS" section label =========================

def test_section_label_unique_and_text_is_plans():
    """Exactly one element carrying the plan-list-section-label class must
    exist in the nav, and its text must be exactly "PLANS"."""
    plans = [_plan("alpha")]
    expr = (
        "(() => {"
        " const nav = document.getElementById('plan-list');"
        f" renderPlanList({json.dumps(plans)});"
        " const labels = nav.__children.filter("
        "   (c) => c.className && c.className.includes('plan-list-section-label')"
        " );"
        " return { count: labels.length, text: labels.length ? labels[0].textContent : null };"
        " })()"
    )
    res = _run_app_js(expr)
    assert res["count"] == 1, f"expected exactly one PLANS section label, got {res['count']}"
    assert res["text"] == "PLANS", f"expected section label text 'PLANS', got {res['text']!r}"


# === Criterion 3: Comms pinned item markup ==================================

def test_comms_item_markup():
    """The Comms pinned item must carry both `plan-item` and `comms-item`
    classes, `dataset.comms === 'true'`, a non-null `.icon-comms` child, and
    `.plan-name` text containing "Comms"."""
    plans = [_plan("alpha")]
    expr = (
        "(() => {"
        " const nav = document.getElementById('plan-list');"
        f" renderPlanList({json.dumps(plans)});"
        " const comms = nav.__children.find((c) => c.dataset && c.dataset.comms === 'true');"
        " const nameEl = comms ? comms.querySelector('.plan-name') : null;"
        " return {"
        "  found: !!comms,"
        "  classHasPlanItem: comms ? comms.className.includes('plan-item') : false,"
        "  classHasCommsItem: comms ? comms.className.includes('comms-item') : false,"
        "  datasetComms: comms ? comms.dataset.comms : null,"
        "  hasIcon: comms ? !!comms.querySelector('.icon-comms') : false,"
        "  nameText: nameEl ? nameEl.textContent : null,"
        " };"
        " })()"
    )
    res = _run_app_js(expr)
    assert res["found"] is True, "no top-level nav child has dataset.comms === 'true'"
    assert res["classHasPlanItem"] is True, "Comms item className must include 'plan-item'"
    assert res["classHasCommsItem"] is True, "Comms item className must include 'comms-item'"
    assert res["datasetComms"] == "true", "Comms item must carry data-comms=\"true\""
    assert res["hasIcon"] is True, "Comms item must contain a .icon-comms child span"
    assert res["nameText"] is not None and "Comms" in res["nameText"], (
        f"Comms .plan-name text must contain 'Comms', got {res['nameText']!r}"
    )


# === Criterion 4 (negative): Comms has no subtitle; Overview unchanged =====

def test_comms_item_has_no_plan_meta_overview_unchanged():
    """The Comms pill must NOT contain a .plan-meta subtitle (removed by
    design), while the Overview item keeps its "fleet landing" subtitle and
    its overview-item class."""
    plans = [_plan("alpha")]
    expr = (
        "(() => {"
        " const nav = document.getElementById('plan-list');"
        f" renderPlanList({json.dumps(plans)});"
        " const comms = nav.__children.find((c) => c.dataset && c.dataset.comms === 'true');"
        " const overview = nav.__children.find((c) => c.dataset && c.dataset.overview === 'true');"
        " const overviewMeta = overview ? overview.querySelector('.plan-meta') : null;"
        " return {"
        "  commsHasMeta: comms ? !!comms.querySelector('.plan-meta') : null,"
        "  overviewMetaText: overviewMeta ? overviewMeta.textContent : null,"
        "  overviewClassHasOverviewItem: overview ? overview.className.includes('overview-item') : false,"
        " };"
        " })()"
    )
    res = _run_app_js(expr)
    assert res["commsHasMeta"] is False, (
        "Comms pinned item must NOT contain a .plan-meta subtitle element"
    )
    assert res["overviewMetaText"] == "fleet landing", (
        f"Overview .plan-meta text must remain 'fleet landing', got {res['overviewMetaText']!r}"
    )
    assert res["overviewClassHasOverviewItem"] is True, (
        "Overview item className must still include 'overview-item'"
    )


# === Criterion 5 (boundary): empty plans ====================================

def test_empty_plans_no_throw_and_order():
    """renderPlanList([]) must not throw, must produce zero per-plan
    markers, and the top-level order must be exactly
    [comms, overview, label, footer]."""
    expr = (
        "(() => {" + _markers_helper_js()
        + " renderPlanList([]);"
        " return markers();"
        " })()"
    )
    order = _run_app_js(expr)
    assert not any(m.startswith("plan:") for m in order), (
        f"expected zero per-plan row markers for an empty plans array, got {order}"
    )
    assert order == ["comms", "overview", "label", "footer"], (
        f"expected [comms, overview, label, footer] for an empty plans array, got {order}"
    )


# === Criterion 6: diff path (second call) doesn't duplicate pinned items ===

def test_diff_path_no_duplicate_pinned_items_and_new_row_before_footer():
    """A second renderPlanList call (the keyed-diff branch) must not
    duplicate the pinned Comms/Overview items, must keep the order starting
    [comms, overview, label, ...], and a newly-inserted plan row must land
    between the label and the footer, with the footer still last."""
    a = [_plan("alpha")]
    b = [_plan("alpha"), _plan("beta")]
    expr = (
        "(() => {" + _markers_helper_js()
        + f" renderPlanList({json.dumps(a)});"
        f" renderPlanList({json.dumps(b)});"
        " const order = markers();"
        " const commsCount = nav.__children.filter((c) => c.dataset && c.dataset.comms === 'true').length;"
        " const overviewCount = nav.__children.filter((c) => c.dataset && c.dataset.overview === 'true').length;"
        " return { order, commsCount, overviewCount };"
        " })()"
    )
    res = _run_app_js(expr)
    assert res["commsCount"] == 1, (
        f"expected exactly one pinned Comms item after a second renderPlanList call, "
        f"got {res['commsCount']} (pinned items must be built once, never duplicated)"
    )
    assert res["overviewCount"] == 1, (
        f"expected exactly one pinned Overview item after a second renderPlanList call, "
        f"got {res['overviewCount']}"
    )
    order = res["order"]
    assert order[:3] == ["comms", "overview", "label"], (
        f"expected order to still start [comms, overview, label], got {order}"
    )
    assert order[-1] == "footer", f"expected footer to remain the last marker, got {order}"
    assert "plan:beta" in order, f"expected the newly-inserted plan:beta row, got {order}"
    label_idx = order.index("label")
    beta_idx = order.index("plan:beta")
    footer_idx = order.index("footer")
    assert label_idx < beta_idx < footer_idx, (
        f"expected plan:beta inserted after 'label' and before 'footer', got order {order}"
    )
