"""Node-eval harness tests for the notification severity filter chip row in
static/app.js (P3-9).

This file mirrors the harness pattern in tests/unit/test_dashboard_notifications_ui.py:
it builds a minimal DOM shim, evals static/app.js under `node -e`, and
JSON-stringifies the result of a test expression. The simple-shim helper is
copied here verbatim (rather than imported) so this file stands alone.

A second, richer harness (`_run_click_flow`) is also copied-and-extended here
for the handful of tests that must actually simulate a user clicking a
notification severity chip: the chip's click handler is the *only* sanctioned
way to change the module-scope `notifSeverityFilter` variable (it is declared
with `let`, so — unlike the `function` declarations that leak out of a
non-strict direct `eval()` — it is not reachable or assignable from outside
the evaluated script). The richer harness gives `document.getElementById`
a real (if minimal) DOM: `innerHTML` assignment tokenizes the HTML string into
a tree (correctly handling same-tag nesting, e.g. <div class="column"> containing
sibling <div class="card"> elements, via an explicit stack rather than a
backreference regex), and elements support querySelectorAll(".class[attr=\"v\"]")
and a click() that fires registered listeners exactly like a browser (each
listener's exceptions are isolated so one throwing listener can't block a
sibling listener on the same element — relevant here because the existing
generic `.filter-chip` wiring in renderPlanDetail also matches our new chips
and will throw on them, which must not stop our own handler from running).

These tests are RED until the implementation lands: `filterNotifications` must
be added as a pure exported function, a `let notifSeverityFilter = "all";`
module-scope variable must exist, `renderNotifications` must render a 4-option
severity chip row and apply the filter, and `renderPlanDetail` must wire a
click handler for those chips that updates `notifSeverityFilter` and re-renders
-- all without touching localStorage, the URL hash, or the filter-contract
functions (defaultFilters/loadFilters/saveFilters/hashStateFrom/encodeHashState/parseHash).
"""
import json
import os
import subprocess

from _app_js import run_app_js

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_JS = os.path.join(REPO_ROOT, "static", "app.js")


def _app_js_source():
    """Read static/app.js source for static-source assertions."""
    with open(APP_JS, encoding="utf-8") as fh:
        return fh.read()


# === Simple harness (copied verbatim from test_dashboard_notifications_ui.py) ==

def _run_app_js(expr):
    """Evaluate a JS expression inside an environment where static/app.js
    has been loaded. Returns the JSON-serialized result."""
    shim = """
        const noop = () => {};
        const fakeEl = {
            innerHTML: "",
            classList: { add: noop, remove: noop, toggle: noop, contains: () => false },
            addEventListener: noop,
            setAttribute: noop,
            appendChild: noop,
            querySelectorAll: () => [],
            dataset: {},
        };
        globalThis.document = {
            addEventListener: noop,
            documentElement: { dataset: {} },
            getElementById: () => ({ ...fakeEl, dataset: {}, addEventListener: noop }),
            createElement: () => ({ ...fakeEl, classList: { add: noop, remove: noop, contains: () => false } }),
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
    script = (
        shim
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


def _render(records):
    """Helper: call renderNotifications with a JS array literal of records."""
    return _run_app_js(f"renderNotifications({json.dumps(records)})")


# === Richer harness: a real-enough DOM to simulate clicking a chip ============

_DOM_SHIM = """
const noop = () => {};

function attach(el) {
  el._matchSelector = function (node, className, attrPairs) {
    if (!node || !node.classList) return false;
    if (className && !node.classList.contains(className)) return false;
    return attrPairs.every(([k, v]) => node.dataset[k] === v);
  };
  el._parseSelector = function (sel) {
    const m = sel.match(/^\\.([\\w-]+)(.*)$/);
    if (!m) return null;
    const className = m[1];
    const rest = m[2] || "";
    const attrPairs = [];
    const re = /\\[([\\w-]+)="([^"]*)"\\]/g;
    let am;
    while ((am = re.exec(rest)) !== null) {
      const raw = am[1];
      const key = raw.startsWith("data-")
        ? raw.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())
        : raw;
      attrPairs.push([key, am[2]]);
    }
    return { className, attrPairs };
  };
  el.querySelector = function (sel) { return el.querySelectorAll(sel)[0] || null; };
  el.querySelectorAll = function (sel) {
    const parsed = el._parseSelector(sel);
    if (!parsed) return [];
    const out = [];
    const walk = (node) => {
      for (const c of node.children) {
        attach(c);
        if (el._matchSelector(c, parsed.className, parsed.attrPairs)) out.push(c);
        walk(c);
      }
    };
    walk(el);
    return out;
  };
  Object.defineProperty(el, "innerHTML", {
    configurable: true,
    get() { return el._innerHTML || ""; },
    set(v) { el._innerHTML = v; el.children = parseInnerHtml(v); },
  });
  return el;
}

// Stack-based tokenizer (not a backreference regex): correctly handles
// same-tag-name nesting, e.g. <div class="column"><div class="card">A</div>
// <div class="card">B</div></div>, which a lazy `<(\\w+)...>(.*?)<\\/\\1>`
// regex mis-pairs on (it closes the outer div at the FIRST </div> it sees,
// i.e. card A's, silently dropping card A as an unparsed fragment).
function parseInnerHtml(html) {
  const root = { children: [], _tag: "__root__" };
  const stack = [root];
  const tokenRe = /<\\/?(\\w+)([^>]*)>/g;
  let m;
  while ((m = tokenRe.exec(html)) !== null) {
    const isClose = m[0][1] === "/";
    const tag = m[1];
    if (isClose) {
      for (let i = stack.length - 1; i >= 1; i--) {
        if (stack[i]._tag === tag) { stack.length = i; break; }
      }
    } else {
      const attrStr = m[2] || "";
      const cls = (attrStr.match(/class="([^"]*)"/) || [, ""])[1].split(/\\s+/).filter(Boolean);
      const ds = {};
      const dsRe = /data-([\\w-]+)="([^"]*)"/g;
      let dm;
      while ((dm = dsRe.exec(attrStr)) !== null) {
        const key = dm[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase());
        ds[key] = dm[2];
      }
      const el = makeEl(tag, { classList: cls, dataset: ds });
      el._tag = tag;
      stack[stack.length - 1].children.push(el);
      stack.push(el);
    }
  }
  return root.children;
}

function makeEl(tag, attrs = {}) {
  const el = {
    tagName: (tag || "div").toUpperCase(),
    dataset: { ...(attrs.dataset || {}) },
    classList: {
      _set: new Set(attrs.classList || []),
      add(c) { this._set.add(c); },
      remove(c) { this._set.delete(c); },
      toggle: noop,
      contains(c) { return this._set.has(c); },
    },
    children: [],
    _listeners: {},
    scrollTop: 0,
    addEventListener(name, fn) { (el._listeners[name] = el._listeners[name] || []).push(fn); },
    setAttribute: noop,
    appendChild: noop,
    focus: noop,
    // Mirrors real DOM event dispatch: each listener's exceptions are
    // isolated so a throwing listener can't block a sibling listener that
    // was also registered on this same element.
    click() {
      (el._listeners.click || []).forEach((fn) => {
        try { fn({ target: el, currentTarget: el }); } catch (e) { /* isolated, like a browser */ }
      });
    },
  };
  return attach(el);
}

const planDetailEl = makeEl("section");
const setItemCalls = [];
globalThis.document = {
  addEventListener: noop,
  documentElement: { dataset: {} },
  activeElement: null,
  getElementById: (id) => (id === "plan-detail" ? planDetailEl : makeEl("div")),
  createElement: () => makeEl("div"),
};
globalThis.window = { location: { hash: "" }, addEventListener: noop };
globalThis.localStorage = {
  getItem: () => null,
  setItem: (k, v) => { setItemCalls.push([k, v]); },
};
globalThis.fetch = () => new Promise(() => {});
process.on("unhandledRejection", () => {});
globalThis.setInterval = () => 0;
globalThis.setTimeout = (fn, _ms) => { return 0; };

const fs = require('fs');
eval(fs.readFileSync(%(app_js_path)s, 'utf8'));
globalThis.state = globalThis.window.state;
"""


def _run_click_flow(records):
    """Render a plan with the given notification_records, click the
    'error' severity chip, and return a dict describing before/after state.
    Requires `errChip` to exist in the initial render (data-dim="notif-severity"
    data-value="error"); callers should design fixtures accordingly."""
    dom_shim = _DOM_SHIM % {"app_js_path": json.dumps(APP_JS)}
    script = dom_shim + f"""
const plan = {{
  name: "Test plan",
  stories: {{}},
  notifications: [],
  decisions: [],
  notification_records: {json.dumps(records)},
}};

// renderPlanDetail reuses the SAME `.plan-detail` section across same-plan
// re-renders (P3-diff-board-cards: the board's `.card` nodes must survive a
// poll instead of being torn down), rebuilding only its in-place children —
// `.plan-chrome` (header + filter-bar, above the board) and `.plan-panels`
// (Notifications + decisions, below the board) — rather than the outer
// section. This mock's innerHTML getter is a cached raw string that only
// updates on the exact element `.innerHTML =` was called on (a real
// browser's innerHTML getter re-serializes live descendants on every read,
// so this distinction doesn't exist there) — so reading the OUTER element's
// cached string after an in-place update returns stale content. Read both
// in-place children and concatenate, so callers see the full chrome+panels
// picture (chip labels AND notification content) regardless of which panel
// an assertion targets. Falls back to the outer section on the very first
// render, before either child's cache is populated.
function currentHtml() {{
  const chrome = planDetailEl.querySelectorAll('.plan-chrome')[0];
  const panels = planDetailEl.querySelectorAll('.plan-panels')[0];
  const parts = [];
  if (chrome && chrome.innerHTML) parts.push(chrome.innerHTML);
  if (panels && panels.innerHTML) parts.push(panels.innerHTML);
  return parts.length ? parts.join(' ') : planDetailEl.innerHTML;
}}

renderPlanDetail(plan);
const beforeHtml = currentHtml();
const beforeSetItemCount = setItemCalls.length;
const beforeHash = globalThis.window.location.hash;
const beforeFiltersJson = JSON.stringify(state.filters);

const errChip = planDetailEl.querySelectorAll(
  '.filter-chip[data-dim="notif-severity"][data-value="error"]')[0];
const chipFound = !!errChip;
if (errChip) errChip.click();

const afterHtml = currentHtml();
const afterSetItemCount = setItemCalls.length;
const afterHash = globalThis.window.location.hash;
const afterFiltersJson = JSON.stringify(state.filters);

const errChipAfter = planDetailEl.querySelectorAll(
  '.filter-chip[data-dim="notif-severity"][data-value="error"]')[0];
const allChipAfter = planDetailEl.querySelectorAll(
  '.filter-chip[data-dim="notif-severity"][data-value="all"]')[0];

process.stdout.write(JSON.stringify({{
  chipFound,
  beforeHtml, afterHtml,
  setItemCallsDelta: afterSetItemCount - beforeSetItemCount,
  hashChanged: beforeHash !== afterHash,
  filtersChanged: beforeFiltersJson !== afterFiltersJson,
  errChipActiveAfter: errChipAfter ? errChipAfter.classList.contains("active") : null,
  allChipActiveAfter: allChipAfter ? allChipAfter.classList.contains("active") : null,
}}));
"""
    proc = subprocess.run(
        ["node", "-e", script],
        check=False, capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout)


# === filterNotifications: pure function ========================================

def test_all_returns_every_record():
    recs = [
        {"severity": "info", "message": "a", "ts": "2024-01-01T00:00:00Z"},
        {"severity": "warning", "message": "b", "ts": "2024-01-01T00:00:01Z"},
        {"severity": "error", "message": "c", "ts": "2024-01-01T00:00:02Z"},
    ]
    out = _run_app_js(f"filterNotifications({json.dumps(recs)}, 'all')")
    assert out == recs


def test_severity_filter_selects_matching_only():
    recs = [
        {"severity": "info", "message": "a", "ts": "2024-01-01T00:00:00Z"},
        {"severity": "warning", "message": "b", "ts": "2024-01-01T00:00:01Z"},
        {"severity": "error", "message": "c", "ts": "2024-01-01T00:00:02Z"},
    ]
    out = _run_app_js(f"filterNotifications({json.dumps(recs)}, 'warning')")
    assert len(out) == 1
    assert out[0]["message"] == "b"
    assert out[0]["severity"] == "warning"


def test_unknown_severity_filter_returns_empty():
    recs = [{"severity": "info", "message": "a", "ts": "2024-01-01T00:00:00Z"}]
    out = _run_app_js(f"filterNotifications({json.dumps(recs)}, 'nope')")
    assert out == []


def test_undefined_records_returns_empty():
    out = _run_app_js("filterNotifications(undefined, 'all')")
    assert out == []


def test_null_records_returns_empty():
    out = _run_app_js("filterNotifications(null, 'all')")
    assert out == []


def test_empty_array_returns_empty():
    out = _run_app_js("filterNotifications([], 'all')")
    assert out == []


def test_empty_string_severity_treated_as_all():
    """The spec guard is `!severity || severity === "all"`; an empty string
    is falsy, so it must behave exactly like 'all', not like an unknown
    severity that matches nothing."""
    recs = [{"severity": "info", "message": "a", "ts": "2024-01-01T00:00:00Z"}]
    out = _run_app_js(f"filterNotifications({json.dumps(recs)}, '')")
    assert out == recs


def test_filter_does_not_mutate_input():
    recs = [
        {"severity": "info", "message": "a", "ts": "2024-01-01T00:00:00Z"},
        {"severity": "error", "message": "b", "ts": "2024-01-01T00:00:01Z"},
    ]
    expr = (
        "(() => {"
        f" const input = {json.dumps(recs)};"
        " const inputCopy = JSON.parse(JSON.stringify(input));"
        " filterNotifications(input, 'error');"
        " return JSON.stringify(input) === JSON.stringify(inputCopy);"
        " })()"
    )
    assert _run_app_js(expr) is True


def test_single_record_matching_severity_returns_it():
    recs = [{"severity": "error", "message": "solo", "ts": "2024-01-01T00:00:00Z"}]
    out = _run_app_js(f"filterNotifications({json.dumps(recs)}, 'error')")
    assert len(out) == 1
    assert out[0]["message"] == "solo"


def test_single_record_not_matching_severity_returns_empty():
    recs = [{"severity": "error", "message": "solo", "ts": "2024-01-01T00:00:00Z"}]
    out = _run_app_js(f"filterNotifications({json.dumps(recs)}, 'warning')")
    assert out == []


def test_filter_notifications_is_exported():
    expr = (
        "(() => {"
        " const out = (typeof module !== 'undefined' && module.exports) ? module.exports : {};"
        " return typeof out.filterNotifications;"
        " })()"
    )
    assert _run_app_js(expr) == "function"


def test_existing_exports_still_present():
    """module.exports must not lose or replace any of the exports the
    notifications-UI story (P3-8) shipped."""
    expr = (
        "(() => {"
        " const out = (typeof module !== 'undefined' && module.exports) ? module.exports : {};"
        " return {"
        "  renderNotifications: typeof out.renderNotifications,"
        "  renderPlanDetail: typeof out.renderPlanDetail,"
        "  state: typeof out.state,"
        "  renderChecklist: typeof out.renderChecklist,"
        " };"
        " })()"
    )
    result = _run_app_js(expr)
    assert result["renderNotifications"] == "function"
    assert result["renderPlanDetail"] == "function"
    assert result["state"] == "object"
    assert result["renderChecklist"] == "function"


# === module-scope notifSeverityFilter ==========================================

def test_notif_severity_filter_declared_at_module_scope():
    js = _app_js_source()
    assert 'let notifSeverityFilter = "all";' in js


def test_notif_severity_filter_kept_out_of_filter_contract_functions():
    """notifSeverityFilter must NOT be wired into defaultFilters/loadFilters/
    saveFilters/hashStateFrom/encodeHashState/parseHash — those drive the
    story board's filter contract and URL hash, covered by
    tests/unit/test_dashboard.py and tests/test_app_search.js, which this
    story must not touch or extend."""
    expr = (
        "(() => JSON.stringify({"
        "  defaultFilters: defaultFilters.toString(),"
        "  loadFilters: loadFilters.toString(),"
        "  saveFilters: saveFilters.toString(),"
        "  hashStateFrom: hashStateFrom.toString(),"
        "  encodeHashState: encodeHashState.toString(),"
        "  parseHash: parseHash.toString(),"
        "}))()"
    )
    sources = json.loads(_run_app_js(expr))
    for name, src in sources.items():
        assert "notifSeverityFilter" not in src, (
            f"{name} must not reference notifSeverityFilter, but its source does"
        )


# === chip row rendering =========================================================

def test_chip_row_renders_four_options():
    html = _render([{"severity": "info", "message": "hi", "ts": "2024-01-01T00:00:00Z"}])
    assert 'data-dim="notif-severity" data-value="all"' in html
    assert 'data-dim="notif-severity" data-value="error"' in html
    assert 'data-dim="notif-severity" data-value="warning"' in html
    assert 'data-dim="notif-severity" data-value="info"' in html


def test_chip_row_exactly_four_options():
    html = _render([{"severity": "info", "message": "hi", "ts": "2024-01-01T00:00:00Z"}])
    assert html.count('data-dim="notif-severity"') == 4


def test_chip_options_in_correct_order():
    html = _render([{"severity": "info", "message": "hi", "ts": "2024-01-01T00:00:00Z"}])
    idx_all = html.index('data-dim="notif-severity" data-value="all"')
    idx_error = html.index('data-dim="notif-severity" data-value="error"')
    idx_warning = html.index('data-dim="notif-severity" data-value="warning"')
    idx_info = html.index('data-dim="notif-severity" data-value="info"')
    assert idx_all < idx_error < idx_warning < idx_info


def test_chip_row_rendered_above_notification_list():
    html = _render([{"severity": "info", "message": "hi", "ts": "2024-01-01T00:00:00Z"}])
    idx_chip = html.index('data-dim="notif-severity"')
    idx_log_line = html.index('class="log-line"')
    assert idx_chip < idx_log_line


def test_default_active_chip_is_all():
    """notifSeverityFilter defaults to 'all', so the 'all' chip must render
    with the active class and none of the other three should."""
    html = _render([{"severity": "info", "message": "hi", "ts": "2024-01-01T00:00:00Z"}])
    assert 'class="filter-chip active" data-dim="notif-severity" data-value="all"' in html
    assert 'class="filter-chip active" data-dim="notif-severity" data-value="error"' not in html
    assert 'class="filter-chip active" data-dim="notif-severity" data-value="warning"' not in html
    assert 'class="filter-chip active" data-dim="notif-severity" data-value="info"' not in html


def test_chip_row_renders_with_empty_records():
    """The chip row is not conditional on records being non-empty — it must
    still appear (above the 'No notifications yet.' empty state)."""
    html = _render([])
    assert 'data-dim="notif-severity"' in html
    assert "No notifications yet." in html


# === distinct empty states ======================================================

def test_genuinely_empty_still_shows_original_empty_state():
    html = _render([])
    assert "No notifications yet." in html


def test_filtered_to_nothing_shows_distinct_empty_state():
    """Records exist (all severity 'info'), but clicking the 'error' chip
    filters them all out. The empty state shown must be the DISTINCT
    'filtered to nothing' message, not the genuinely-empty one — the two
    states mean different things."""
    records = [
        {"severity": "info", "message": "one", "ts": "2024-01-01T00:00:00Z"},
        {"severity": "info", "message": "two", "ts": "2024-01-01T00:00:01Z"},
    ]
    result = _run_click_flow(records)
    assert result["chipFound"], "expected an 'error' severity chip to be findable in the initial render"
    assert "No notifications at this severity." in result["afterHtml"]
    assert "No notifications yet." not in result["afterHtml"]


def test_unfiltered_initial_render_does_not_show_filtered_empty_state():
    """Sanity check on the fixture used above: before any click, with
    notifSeverityFilter still at its 'all' default, the two 'info' records
    must render normally (neither empty state applies)."""
    records = [
        {"severity": "info", "message": "one", "ts": "2024-01-01T00:00:00Z"},
        {"severity": "info", "message": "two", "ts": "2024-01-01T00:00:01Z"},
    ]
    result = _run_click_flow(records)
    assert "one" in result["beforeHtml"]
    assert "two" in result["beforeHtml"]
    assert "No notifications yet." not in result["beforeHtml"]
    assert "No notifications at this severity." not in result["beforeHtml"]


def test_active_chip_updates_after_click():
    records = [{"severity": "info", "message": "one", "ts": "2024-01-01T00:00:00Z"}]
    result = _run_click_flow(records)
    assert result["errChipActiveAfter"] is True
    assert result["allChipActiveAfter"] is False


# === renderPlanDetail guard (must still hold with the chip row added) =========

def test_undefined_records_still_renders_empty_state_without_throwing():
    html = _run_app_js("renderNotifications(undefined)")
    assert "No notifications yet." in html


def test_render_plan_detail_without_notification_records_does_not_throw():
    """This is the exact guard from test_dashboard_notifications_ui.py's
    test_render_plan_detail_without_notification_records_does_not_throw,
    re-verified here because the new chip row / filter wiring touches the
    same code path and must not regress it."""
    expr = (
        "(() => {"
        " let ok = true, err = null;"
        " try {"
        "  renderPlanDetail({ stories:{}, notifications:[], decisions:[] });"
        " } catch (e) { ok = false; err = String(e); }"
        " return { ok, err };"
        " })()"
    )
    result = _run_app_js(expr)
    assert result["ok"] is True, f"renderPlanDetail threw: {result.get('err')}"


# === KEEP THE STATE LOCAL: no persistence to localStorage or URL hash =========

def test_click_does_not_call_local_storage_setitem():
    records = [{"severity": "info", "message": "one", "ts": "2024-01-01T00:00:00Z"}]
    result = _run_click_flow(records)
    assert result["chipFound"]
    assert result["setItemCallsDelta"] == 0, (
        "clicking a notification severity chip must not persist to localStorage"
    )


def test_click_does_not_change_url_hash():
    records = [{"severity": "info", "message": "one", "ts": "2024-01-01T00:00:00Z"}]
    result = _run_click_flow(records)
    assert result["chipFound"]
    assert result["hashChanged"] is False, (
        "clicking a notification severity chip must not touch window.location.hash"
    )


def test_click_does_not_mutate_board_filters():
    records = [{"severity": "info", "message": "one", "ts": "2024-01-01T00:00:00Z"}]
    result = _run_click_flow(records)
    assert result["chipFound"]
    assert result["filtersChanged"] is False, (
        "clicking a notification severity chip must not mutate state.filters "
        "(the story board's filter contract) at all"
    )
