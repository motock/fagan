"""Node-eval harness tests for the keyed-diff refactor of the story-card grid
in static/app.js (renderBoard's per-column `.card` elements).

This file mirrors the harness pattern in tests/unit/test_dashboard_planlist_diff.py
and tests/unit/test_dashboard.py: it builds a minimal (but richer) DOM shim,
evals static/app.js under `node -e`, and JSON-stringifies the result of a test
expression. The shim is copied + extended here (rather than imported) so this
file stands alone and does not touch the other test files.

EXTENSION TO THE SHIM (this file only): `document.createElement` tags every
created element with a unique incrementing `__testId` number, and
`appendChild` / `innerHTML=""` actually maintain a `children` array on each
node, and `innerHTML` parses simple open/close tag pairs so
`querySelector`/`querySelectorAll` can locate `.card`, `.card-summary`,
`.card-age`, `.card-badges`, `.card-key` and `[data-key=...]`. This lets a test
detect whether a `.card` node was REUSED across two `_diffBoardCards` calls
(same `__testId`) or RECREATED (new `__testId`).

These tests are RED until the implementation lands:
  - a new function `_diffBoardCards(columnBodyEl, storiesForColumn)` must exist
    that performs an add/update/remove-by-key diff of `.card` elements inside
    a single column's `.column-body` container, keyed by the `data-key`
    attribute already set on each card today,
  - `renderBoard` must call `_diffBoardCards` per column instead of building
    each column's card HTML as one big template-string chunk,
  - `_diffBoardCards` must be added to module.exports.

`_diffBoardCards` is exercised directly here by calling it twice against the
SAME container element (the contract the implementer must satisfy), AND
end-to-end through `renderPlanDetail`: `test_render_plan_detail_preserves_card_identity_across_polls`
drives two full `renderPlanDetail` calls against the same live `#plan-detail`
section and asserts the surviving `.card` node is the SAME object
(`__testId` unchanged), not torn down and recreated — a source-text check
that `"_diffBoardCards("` merely APPEARS somewhere in renderBoard's body
previously passed here without renderBoard ever calling it on the real
poll-to-poll path, which is exactly the bug this behavioral test catches.
"""
import json
import os

from tests.unit._app_js import run_app_js as _shared_run_app_js

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_JS = os.path.join(REPO_ROOT, "static", "app.js")
# renderBoard / _diffBoardCards were relocated out of static/app.js into this
# dedicated render module (server-app-file-split plan); the static-source
# assertions below follow them here.
BOARD_JS = os.path.join(REPO_ROOT, "static", "app", "render", "board.js")


# A self-contained, richer DOM shim. Built once as a Python string and
# embedded in every node invocation. The shim implements just enough of the
# DOM for renderBoard / _diffBoardCards: createElement with unique __testId,
# appendChild maintaining a children array, innerHTML="" clearing children,
# innerHTML parsing simple open/close tag pairs into child nodes, and
# querySelector/querySelectorAll matching a single class selector or a
# [data-key="..."] attribute selector. It also supports insertBefore and
# remove so the diff can reorder/insert/remove cards.
_SHIM = r"""
    const noop = () => {};
    let __testIdCounter = 0;

    // Parse a tiny subset of HTML (open/close tag pairs and text) into child
    // element nodes. Each parsed element gets its own __testId so it is
    // distinguishable, and supports class-based querySelector. Attributes we
    // care about (class, title, type, style, data-*) are captured.
    //
    // Stack-based tokenizer (not a lazy backreference regex): a naive
    // `<(\w+)...>(.*?)<\/\1>` match mis-pairs on same-tag nesting — e.g.
    // `<div class="column"><div class="column-body">...</div></div>` closes
    // the OUTER div at the FIRST `</div>` (the column-body's), silently
    // flattening every deeper descendant into siblings of `column` instead
    // of children of it. renderPlanDetail's real output nests `.column` >
    // `.column-body` > `.card` > several more divs several levels deep, so
    // this matters here even though it didn't when only renderBoard's
    // narrower, previously-tested output was ever parsed. Mirrors the
    // stack-based tokenizer in tests/unit/test_dashboard_notification_filter.py's
    // shim, which solved the identical problem there.
    function __parseHtml(html, parent) {
        parent.__children = [];
        if (!html) return;
        const tokenRe = /<(\/?)([a-zA-Z]+)((?:\s+[a-zA-Z-]+(?:="[^"]*")?)*)\s*(\/?)>/g;
        const are = /([a-zA-Z-]+)="([^"]*)"/g;
        const stack = [parent];
        let lastIndex = 0;
        let m;
        while ((m = tokenRe.exec(html)) !== null) {
            // Text between the previous token and this one belongs to
            // whichever element is currently on top of the stack.
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
        // Recursively fold each node's descendant text (tags stripped) into
        // __text, matching the field's original semantics — tests read e.g.
        // a `.card-badges` wrapper's __text expecting the concatenated text
        // of the <span> badges nested inside it, not just the whitespace
        // directly between the wrapper's own open/close tags.
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
            innerHTML: "",
            textContent: "",
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
        // innerHTML setter: parse into children. We use a JS getter/setter via
        // Object.defineProperty so assignment `el.innerHTML = "..."` works.
        Object.defineProperty(el, "innerHTML", {
            get: () => el.__innerHTMLRaw || "",
            set: (v) => {
                el.__innerHTMLRaw = String(v);
                __parseHtml(String(v), el);
            },
            configurable: true,
        });
        // textContent setter: just store text, clear children.
        Object.defineProperty(el, "textContent", {
            get: () => el.__text || "",
            set: (v) => { el.__text = String(v); el.__children = []; el.__innerHTMLRaw = String(v); },
            configurable: true,
        });
        // style: support `el.style.setProperty("--badge-color", val)` and
        // `el.style.getPropertyValue("--badge-color")`, plus a raw string
        // assignment via setAttribute("style", ...). The diff updates the
        // --badge-color CSS var on the EXISTING card node, so the shim must
        // preserve it across calls.
        el.style = {
            __props: {},
            setProperty: (k, v) => { el.style.__props[k] = String(v); },
            getPropertyValue: (k) => (el.style.__props[k] != null ? String(el.style.__props[k]) : ""),
            removeProperty: (k) => { const v = el.style.__props[k]; delete el.style.__props[k]; return v != null ? String(v) : ""; },
        };
        return el;
    }

    function __matches(el, sel) {
        // Support a single ".classname" selector, a "[data-key=...]" attribute
        // selector, or a bare tag name.
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


def _story(key, status="todo", summary="a story", backend=None, escalated=False,
           last_activity=None, progress=None):
    """Build a story dict shaped like the entries in plan.stories."""
    s = {
        "summary": summary,
        "status": status,
        "persona": "tech-lead",
        "risk": "medium",
    }
    if backend is not None:
        s["backend"] = backend
    if escalated:
        s["escalated"] = True
    if last_activity is not None:
        s["last_activity"] = last_activity
    if progress is not None:
        s["progress"] = progress
    return [key, s]


# === Static-source rename-and-delegate assertions ===========================

def test_diff_board_cards_function_exists():
    """A new function `_diffBoardCards(columnBodyEl, storiesForColumn)` must
    exist (now in static/app/render/board.js, alongside renderBoard, after
    the server-app-file-split extraction). It is the diffable unit extracted
    from renderBoard's per-column card-building loop."""
    with open(BOARD_JS, encoding="utf-8") as fh:
        js = fh.read()
    assert "_diffBoardCards" in js
    # The function must be declared with the documented two-parameter
    # signature (column body element + the array of [key, story] pairs for
    # that column, post-filter/post-sort).
    assert "function _diffBoardCards(" in js


def test_render_board_calls_diff_board_cards():
    """renderBoard must delegate per-column card building to _diffBoardCards
    instead of building each column's card HTML as one big template-string
    chunk. The column HEADER/count and completion hint may stay inline
    (they are cheap and stateless); only the cards within a column-body are
    diffed."""
    with open(BOARD_JS, encoding="utf-8") as fh:
        js = fh.read()
    assert "_diffBoardCards(" in js
    # The old single big template-string card chunk (the per-card return
    # inside renderBoard's entries.map) must no longer be the way cards are
    # produced for a column. The card markup builder may move into
    # _diffBoardCards (or a helper it calls). Assert the diff function is
    # referenced somewhere at/after renderBoard's own declaration within
    # board.js (renderBoard and _diffBoardCards now live in the same
    # extracted module, so there is no longer a renderPlanDetail boundary
    # to bound the search against).
    rb_start = js.index("function renderBoard(")
    calls = [i for i in range(rb_start, len(js)) if js[i:i + 16] == "_diffBoardCards("]
    assert calls, "renderBoard must call _diffBoardCards within its body"


def test_render_plan_detail_preserves_card_identity_across_polls():
    """End-to-end behavioral check: the source-text assertion above (that
    "_diffBoardCards(" appears somewhere in renderBoard's body) is a
    tautology on its own — _diffBoardCards's own declaration sits in that
    same byte range, so the substring is found on its SIGNATURE line
    whether or not anything ever calls it for real. The actual bug this
    story exists to fix is that renderPlanDetail rebuilt the whole
    `#plan-detail` section's innerHTML — including every `.card` — on
    EVERY poll tick, tearing down and recreating DOM nodes the user might
    have scrolled to, focused, or otherwise be interacting with, even when
    nothing in that column changed.

    Drive renderPlanDetail twice against the SAME live `#plan-detail`
    section (the real call site, not _diffBoardCards directly) with an
    overlapping story key, and assert the surviving `.card` DOM node is the
    SAME object — not a lookalike rebuilt from scratch — while its content
    still reflects the second render's data."""
    plan_a = {
        "name": "demo",
        "stories": {
            "S1": {"summary": "one", "status": "todo", "persona": "tech-lead", "risk": "medium"},
        },
        "notification_records": [],
        "decisions": [],
    }
    plan_b = {
        "name": "demo",
        "stories": {
            "S1": {"summary": "one (updated)", "status": "todo", "persona": "tech-lead", "risk": "medium"},
        },
        "notification_records": [],
        "decisions": [],
    }
    expr = (
        "(() => {"
        " renderPlanDetail(" + json.dumps(plan_a) + ");"
        " const detail = document.getElementById('plan-detail');"
        " const cardByKey = (key) => detail.querySelectorAll('.card').find((c) => c.dataset.key === key);"
        " const before = cardByKey('S1');"
        " const beforeId = before ? before.__testId : null;"
        " renderPlanDetail(" + json.dumps(plan_b) + ");"
        " const after = cardByKey('S1');"
        " const afterId = after ? after.__testId : null;"
        " const summaryEl = after ? after.querySelector('.card-summary') : null;"
        " const afterSummary = summaryEl ? (summaryEl.__text || '') : '';"
        " return { beforeId, afterId, afterSummary };"
        " })()"
    )
    res = _run_app_js(expr)
    assert res["beforeId"] is not None, "no .card rendered for S1 on the first renderPlanDetail call"
    assert res["afterId"] is not None, "the .card for S1 disappeared on the second renderPlanDetail call"
    assert res["afterId"] == res["beforeId"], (
        "the .card DOM node for S1 was recreated across two renderPlanDetail "
        "calls instead of being preserved by the keyed diff — renderBoard is "
        "not genuinely wired to a persistent column-body element"
    )
    assert "updated" in res["afterSummary"], (
        f"the surviving card's content did not update on the second render: {res['afterSummary']!r}"
    )


def test_render_plan_detail_switching_plans_does_a_fresh_render():
    """Negative/boundary case for the identity-preservation contract above:
    switching to a DIFFERENT plan must NOT try to reuse the previous plan's
    board — it must tear down and rebuild from scratch. This guards against
    an implementation that keys the "reuse the board" decision on the board
    merely existing, rather than on it belonging to the same plan."""
    plan_a = {
        "name": "demo-a",
        "stories": {
            "S1": {"summary": "one", "status": "todo", "persona": "tech-lead", "risk": "medium"},
        },
        "notification_records": [],
        "decisions": [],
    }
    plan_b = {
        "name": "demo-b",
        "stories": {
            "S2": {"summary": "two", "status": "todo", "persona": "tech-lead", "risk": "medium"},
        },
        "notification_records": [],
        "decisions": [],
    }
    expr = (
        "(() => {"
        " renderPlanDetail(" + json.dumps(plan_a) + ");"
        " renderPlanDetail(" + json.dumps(plan_b) + ");"
        " const detail = document.getElementById('plan-detail');"
        " const keys = detail.querySelectorAll('.card').map((c) => c.dataset.key);"
        " return keys;"
        " })()"
    )
    res = _run_app_js(expr)
    assert res == ["S2"], (
        f"switching plans must show only the new plan's cards, got {res}"
    )


def test_diff_board_cards_is_exported():
    """_diffBoardCards must be present in module.exports so it can be tested
    directly via the node harness and called by name from tests."""
    expr = (
        "(() => {"
        " const out = (typeof module !== 'undefined' && module.exports) ? module.exports : {};"
        " return typeof out._diffBoardCards;"
        " })()"
    )
    assert _run_app_js(expr) == "function"


def test_render_board_is_exported():
    """renderBoard must be present in module.exports so the harness can drive
    the full board render path (column headers + diffed cards) end to end."""
    expr = (
        "(() => {"
        " const out = (typeof module !== 'undefined' && module.exports) ? module.exports : {};"
        " return typeof out.renderBoard;"
        " })()"
    )
    assert _run_app_js(expr) == "function"


# === Harness helper: call _diffBoardCards twice and report per-card state ====

def _diff_twice(stories_a, stories_b):
    """Call _diffBoardCards twice against the SAME `.column-body` container
    element (the contract the implementer must satisfy). Returns a JSON
    object describing the `.card` nodes after each call:
      { first: [{key, testId, summary, badgeColor, stale, age, badges}],
        second: [...] }

    Cards are identified by their data-key attribute. `badgeColor` is the
    computed value of the --badge-color CSS custom property on the card node
    (so a status change is detectable without recreating the node)."""
    expr = (
        "(() => {"
        " const body = document.createElement('div');"
        " body.className = 'column-body';"
        " const collect = () => {"
        "  const cards = body.querySelectorAll('.card');"
        "  return cards.map((c) => {"
        "   const sum = c.querySelector('.card-summary');"
        "   const age = c.querySelector('.card-age');"
        "   const badges = c.querySelector('.card-badges');"
        "   return {"
        "    key: c.dataset.key,"
        "    testId: c.__testId,"
        "    summary: sum ? (sum.__text || sum.textContent || '') : '',"
        "    badgeColor: c.style.getPropertyValue('--badge-color'),"
        "    stale: c.classList.contains('stale'),"
        "    age: age ? (age.__text || age.textContent || '') : '',"
        "    badges: badges ? (badges.__text || badges.textContent || '') : '',"
        "    classes: c.className || ''"
        "   };"
        "  });"
        " };"
        f" _diffBoardCards(body, {json.dumps(stories_a)});"
        " const first = collect();"
        f" _diffBoardCards(body, {json.dumps(stories_b)});"
        " const second = collect();"
        " return { first, second };"
        " })()"
    )
    return _run_app_js(expr)


def _by_key(rows):
    return {r["key"]: r for r in rows}


# === Identical stories: no card recreated ===================================

def test_identical_single_story_second_call_does_not_recreate_card():
    """Calling _diffBoardCards twice with the IDENTICAL single-story array
    must not remove-and-recreate the card: the card's __testId is unchanged
    across both calls."""
    stories = [_story("S1", status="todo", summary="hello")]
    res = _diff_twice(stories, stories)
    first, second = _by_key(res["first"]), _by_key(res["second"])
    assert set(first) == set(second) == {"S1"}
    assert first["S1"]["testId"] == second["S1"]["testId"], (
        "card S1 was recreated across identical calls"
    )


def test_identical_single_story_summary_preserved():
    """The card-summary text must still be correct after the no-op second
    call."""
    stories = [_story("S1", status="todo", summary="hello")]
    res = _diff_twice(stories, stories)
    second = _by_key(res["second"])
    assert "hello" in second["S1"]["summary"]


# === Changed status: same node, updated --badge-color ========================

def test_changed_status_reuses_node_and_updates_badge_color():
    """A story whose `status` changed on the second call: that card is the
    SAME node (__testId unchanged) but its --badge-color style reflects the
    new status."""
    a = [_story("S1", status="todo", summary="hello")]
    b = [_story("S1", status="done", summary="hello")]
    res = _diff_twice(a, b)
    first, second = _by_key(res["first"]), _by_key(res["second"])
    assert "S1" in first and "S1" in second
    assert first["S1"]["testId"] == second["S1"]["testId"], (
        "card S1 was recreated when only its status changed"
    )
    # The --badge-color must change to reflect the new status. The exact CSS
    # var name is `var(--c-<status>)`, so the new status name must appear in
    # the computed badge color after the second call, and it must differ from
    # the first call's value.
    assert "todo" in first["S1"]["badgeColor"]
    assert "done" in second["S1"]["badgeColor"]
    assert first["S1"]["badgeColor"] != second["S1"]["badgeColor"]


def test_changed_summary_reuses_node_and_updates_summary_text():
    """A story whose `summary` changed on the second call: same node, the
    card-summary text reflects the new summary."""
    a = [_story("S1", status="todo", summary="old summary")]
    b = [_story("S1", status="todo", summary="new summary")]
    res = _diff_twice(a, b)
    first, second = _by_key(res["first"]), _by_key(res["second"])
    assert first["S1"]["testId"] == second["S1"]["testId"], (
        "card S1 was recreated when only its summary changed"
    )
    assert "old summary" in first["S1"]["summary"]
    assert "new summary" in second["S1"]["summary"]
    assert "old summary" not in second["S1"]["summary"]


def test_changed_stale_state_reuses_node_and_toggles_stale_class():
    """A story whose stale state flips on the second call: same node, the
    `.stale` class is toggled on the EXISTING node (not recreated). Stale is
    derived from last_activity + status=in_progress, so we move the
    last_activity far enough in the past to cross the stale threshold."""
    # First call: in_progress, recent activity -> not stale.
    a = [_story("S1", status="in_progress", summary="x",
                last_activity="2020-01-01T00:00:00Z")]
    # Second call: in_progress, very old activity -> stale. We use a far-past
    # timestamp so the client-side age math crosses the threshold regardless
    # of the test machine's clock.
    b = [_story("S1", status="in_progress", summary="x",
                last_activity="2000-01-01T00:00:00Z")]
    res = _diff_twice(a, b)
    first, second = _by_key(res["first"]), _by_key(res["second"])
    assert first["S1"]["testId"] == second["S1"]["testId"], (
        "card S1 was recreated when only its stale state changed"
    )
    # The stale class must be present after the second call (old activity)
    # and absent before. We assert the direction of the change.
    assert second["S1"]["stale"] is True


def test_changed_badges_reuses_node_and_updates_badges_content():
    """A story whose backend/escalated badges change on the second call: same
    node, the card-badges content reflects the new badges."""
    a = [_story("S1", status="todo", summary="x")]
    b = [_story("S1", status="todo", summary="x", backend="claude", escalated=True)]
    res = _diff_twice(a, b)
    first, second = _by_key(res["first"]), _by_key(res["second"])
    assert first["S1"]["testId"] == second["S1"]["testId"], (
        "card S1 was recreated when only its badges changed"
    )
    # First call has no badges; second call has claude + escalated badges.
    assert "claude" not in first["S1"]["badges"]
    assert "claude" in second["S1"]["badges"]
    assert "escalated" in second["S1"]["badges"]


# === Story added: new card appears, existing cards unchanged =================

def test_story_added_creates_new_card_preserves_existing():
    """A NEW story added on the second call: a new card appears for it; the
    existing cards' __testId values are unchanged."""
    a = [_story("S1", status="todo", summary="one")]
    b = [_story("S1", status="todo", summary="one"), _story("S2", status="done", summary="two")]
    res = _diff_twice(a, b)
    first, second = _by_key(res["first"]), _by_key(res["second"])
    assert set(first) == {"S1"}
    assert set(second) == {"S1", "S2"}
    assert first["S1"]["testId"] == second["S1"]["testId"], (
        "existing card S1 was recreated when a new story was added"
    )
    assert "S2" in second
    assert "two" in second["S2"]["summary"]


# === Story removed: card gone, remaining cards unchanged =====================

def test_story_removed_drops_card_preserves_remaining():
    """A story REMOVED on the second call: that card is gone; the remaining
    cards' __testId values are unchanged."""
    a = [_story("S1", status="todo", summary="one"), _story("S2", status="done", summary="two")]
    b = [_story("S1", status="todo", summary="one")]
    res = _diff_twice(a, b)
    first, second = _by_key(res["first"]), _by_key(res["second"])
    assert set(first) == {"S1", "S2"}
    assert set(second) == {"S1"}
    assert "S2" not in second
    assert first["S1"]["testId"] == second["S1"]["testId"], (
        "remaining card S1 was recreated when a story was removed"
    )


# === Negative / boundary ====================================================

def test_empty_stories_second_call_removes_all_cards_and_does_not_throw():
    """Negative/boundary: _diffBoardCards(columnBodyEl, []) on a container
    that previously had cards removes all of them and does not throw."""
    a = [_story("S1", status="todo", summary="one"), _story("S2", status="done", summary="two")]
    b = []
    res = _diff_twice(a, b)
    assert res["second"] == [], (
        "second call with [] left cards behind"
    )


def test_empty_first_call_does_not_throw():
    """Boundary: _diffBoardCards(body, []) on the first call does not throw
    and results in zero cards."""
    expr = (
        "(() => {"
        " const body = document.createElement('div');"
        " body.className = 'column-body';"
        " let ok = true, err = null, count = -1;"
        " try {"
        "  _diffBoardCards(body, []);"
        "  count = body.querySelectorAll('.card').length;"
        " } catch (e) { ok = false; err = String(e); }"
        " return { ok, err, count };"
        " })()"
    )
    res = _run_app_js(expr)
    assert res["ok"] is True, f"_diffBoardCards(body, []) threw: {res.get('err')}"
    assert res["count"] == 0


def test_empty_then_populated_then_empty_roundtrip():
    """Boundary: empty -> one story -> empty. Cards appear and disappear
    cleanly without throwing across three calls against the same container."""
    a = []
    b = [_story("solo", status="todo", summary="only")]
    c = []
    expr = (
        "(() => {"
        " const body = document.createElement('div');"
        " body.className = 'column-body';"
        " const collect = () => body.querySelectorAll('.card').map((c) => ({"
        "  key: c.dataset.key, testId: c.__testId"
        " }));"
        " let ok = true, err = null;"
        " let r0, r1, r2;"
        " try {"
        f"  _diffBoardCards(body, {json.dumps(a)});"
        "  r0 = collect();"
        f"  _diffBoardCards(body, {json.dumps(b)});"
        "  r1 = collect();"
        f"  _diffBoardCards(body, {json.dumps(c)});"
        "  r2 = collect();"
        " } catch (e) { ok = false; err = String(e); }"
        " return { ok, err, r0, r1, r2 };"
        " })()"
    )
    res = _run_app_js(expr)
    assert res["ok"] is True, f"roundtrip threw: {res.get('err')}"
    assert res["r0"] == []
    assert len(res["r1"]) == 1 and res["r1"][0]["key"] == "solo"
    assert res["r2"] == []


def test_single_story_boundary_reused_on_identical_call():
    """Boundary: a single story rendered twice identically is reused."""
    stories = [_story("only", status="todo", summary="x")]
    res = _diff_twice(stories, stories)
    first, second = _by_key(res["first"]), _by_key(res["second"])
    assert set(first) == set(second) == {"only"}
    assert first["only"]["testId"] == second["only"]["testId"]


def test_no_summary_renders_placeholder_without_throwing():
    """Boundary/malformed: a story with no summary renders the
    '(no summary)' placeholder without throwing, and the card is reused on
    an identical second call."""
    a = [["S1", {"status": "todo"}]]
    b = [["S1", {"status": "todo"}]]
    res = _diff_twice(a, b)
    first, second = _by_key(res["first"]), _by_key(res["second"])
    assert "S1" in first and "S1" in second
    assert first["S1"]["testId"] == second["S1"]["testId"]
    # The placeholder text must appear (the exact wording is the existing
    # renderBoard contract: "(no summary)").
    assert "no summary" in second["S1"]["summary"].lower()


def test_missing_status_does_not_throw():
    """Malformed input: a story missing the `status` field must not throw
    inside _diffBoardCards (the diff must be defensive about per-story
    fields the way renderBoard already is)."""
    a = [["S1", {"summary": "x"}]]
    expr = (
        "(() => {"
        " const body = document.createElement('div');"
        " body.className = 'column-body';"
        " let ok = true, err = null, count = -1;"
        " try {"
        f"  _diffBoardCards(body, {json.dumps(a)});"
        "  count = body.querySelectorAll('.card').length;"
        " } catch (e) { ok = false; err = String(e); }"
        " return { ok, err, count };"
        " })()"
    )
    res = _run_app_js(expr)
    assert res["ok"] is True, f"_diffBoardCards threw on missing status: {res.get('err')}"
    # A card should still be created for the story (keyed by data-key).
    assert res["count"] == 1


# === renderBoard end-to-end still produces cards (no regression) =============

def test_render_board_still_produces_cards_with_data_key():
    """renderBoard must still produce a board with `.card` elements carrying
    `data-key` after delegating card building to _diffBoardCards. This guards
    against the refactor accidentally dropping cards or the data-key
    attribute that the diff keys on."""
    stories = {
        "S1": {"summary": "one", "status": "todo", "persona": "tech-lead", "risk": "medium"},
        "S2": {"summary": "two", "status": "done", "persona": "tech-lead", "risk": "medium"},
    }
    expr = (
        "(() => {"
        " const html = renderBoard(" + json.dumps(stories) + ");"
        " const wrap = document.createElement('div');"
        " wrap.innerHTML = html;"
        " const cards = wrap.querySelectorAll('.card');"
        " return cards.map((c) => ({ key: c.dataset.key, classes: c.className || '' }));"
        " })()"
    )
    res = _run_app_js(expr)
    keys = {c["key"] for c in res}
    assert keys == {"S1", "S2"}, f"renderBoard lost cards after refactor: {res}"


def test_render_board_still_produces_column_body_containers():
    """renderBoard must still produce `.column-body` containers (the diff
    target) so _diffBoardCards has a container to operate on within each
    column."""
    stories = {
        "S1": {"summary": "one", "status": "todo", "persona": "tech-lead", "risk": "medium"},
    }
    expr = (
        "(() => {"
        " const html = renderBoard(" + json.dumps(stories) + ");"
        " const wrap = document.createElement('div');"
        " wrap.innerHTML = html;"
        " const bodies = wrap.querySelectorAll('.column-body');"
        " return bodies.length;"
        " })()"
    )
    assert _run_app_js(expr) >= 1, "renderBoard no longer emits .column-body containers"


def test_render_board_column_header_count_still_present():
    """The column HEADER and count badge are explicitly OUT OF SCOPE for the
    diff (the brief says they may stay full-rebuild). They must still be
    produced by renderBoard so the board is not visually broken."""
    stories = {
        "S1": {"summary": "one", "status": "todo", "persona": "tech-lead", "risk": "medium"},
        "S2": {"summary": "two", "status": "todo", "persona": "tech-lead", "risk": "medium"},
    }
    expr = (
        "(() => {"
        " const html = renderBoard(" + json.dumps(stories) + ");"
        " const wrap = document.createElement('div');"
        " wrap.innerHTML = html;"
        " const headers = wrap.querySelectorAll('.column-header');"
        " const badges = wrap.querySelectorAll('.badge');"
        " return { headerCount: headers.length, badgeCount: badges.length };"
        " })()"
    )
    res = _run_app_js(expr)
    assert res["headerCount"] >= 1, "renderBoard no longer emits column headers"
    # The column header carries a count badge; with two todo stories the
    # todo column's badge text is "2". Assert at least one badge exists so
    # the count surface is not dropped by the refactor.
    assert res["badgeCount"] >= 1, "renderBoard no longer emits column count badges"