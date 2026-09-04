"""Wedged indicator on plan-board cards (static/app/render/board.js + static/style.css).

The backend (previous story, graded by tests/unit/test_dashboard_wedge_decoration.py)
attaches ``story.wedge = {"wedged": bool, "reasons": [...], "measured": {...}}`` to
in_progress stories in GET /api/plans/{plan}. THIS story surfaces that server-derived
verdict on the board cards:

- a new pure helper ``isWedged(story)`` in board.js: true ONLY when
  ``story && story.wedge && story.wedge.wedged === true``. It must NOT re-derive
  staleness client-side, and it must NOT change ``isStaleInProgress`` / the ``.stale``
  class (last_activity-based aging keeps working exactly as today).
- a ``card-badge-wedged`` badge (title ``"Wedged: <reasons joined by ', '>"``) plus a
  bare ``wedged`` class on the card, in BOTH card-building paths: renderBoard's full
  string-build path AND the in-place update path (_tryUpdateBoardInPlace ->
  _diffBoardCards). A badge added only to the string path silently disappears on the
  second poll — the in-place path is exercised here explicitly.
- an appended ``.card-badge-wedged`` rule in static/style.css following the existing
  ``.card-badge-claude`` / ``.card-badge-escalated`` var()/color-token conventions.

Mechanism: the shared Node harness (tests/unit/_app_js.py) loads
static/app/render/board.js DIRECTLY as an ES module (it imports only ../state.js, whose
top level only assigns ``window.*`` fields, so a small DOM shim suffices) and evaluates
an expression against its named exports, exactly as test_dashboard_board_diff.py does
for renderBoard/_diffBoardCards. ``isWedged`` must therefore be EXPORTED from board.js
(the same testability requirement that file already places on ``_diffBoardCards``).

CUMULATIVE-ARTIFACT RULE: board.js and style.css are shared artifacts that other plans
and later stories also extend. These tests assert only what THIS story adds — membership
of 'card-badge-wedged', the bare 'wedged' class token, the badge title, and the CSS
rule's ordering AFTER the pre-existing badge anchors — never the total badge count, the
exact full card HTML string, any file byte/SHA hash, or the complete set of
card-badge-* classes.

These tests are RED until the implementation lands: ``isWedged`` does not exist yet
(the export test fails with "isWedged is not exported"), and no card markup carries the
wedged badge.
"""
import json
import os
import re
from datetime import datetime, timedelta, timezone

from tests.unit._app_js import run_app_js

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BOARD_JS = os.path.join(REPO_ROOT, "static", "app", "render", "board.js")
STYLE_CSS = os.path.join(REPO_ROOT, "static", "style.css")


# === Node harness: load board.js as an ES module behind a minimal DOM shim ====
#
# board.js needs, at import time, only ``window`` (state.js assigns
# window.BACKEND_VALUES / window.state) and, at render time, ``document``
# (createElement for new diff cards). The shim below implements just enough
# DOM for the two card paths: a stack-based HTML tokenizer for innerHTML
# (same approach as test_dashboard_board_diff.py's shim — a naive
# backreference regex mis-pairs same-tag nesting), unique __testId per
# element so node identity is observable, className/classList, dataset,
# style.setProperty, appendChild/insertBefore/remove, and class selectors
# for querySelector/querySelectorAll.
_SHIM = r"""
    const noop = () => {};
    let __testIdCounter = 0;

    function __newEl(tag) {
        const el = {
            __tag: String(tag),
            __testId: ++__testIdCounter,
            __children: [],
            __text: "",
            __innerHTMLRaw: "",
            __attrs: {},
            __parent: null,
            dataset: {},
        };
        let __classes = [];
        Object.defineProperty(el, "className", {
            get: () => __classes.join(" "),
            set: (v) => { __classes = String(v == null ? "" : v).split(/\s+/).filter(Boolean); },
            configurable: true,
        });
        el.classList = {
            contains: (c) => __classes.indexOf(c) !== -1,
            add: (...cs) => {
                for (const c of cs) if (__classes.indexOf(c) === -1) __classes.push(c);
            },
            remove: (...cs) => { __classes = __classes.filter((c) => cs.indexOf(c) === -1); },
            toggle: (c, force) => {
                const has = __classes.indexOf(c) !== -1;
                const want = force === undefined ? !has : !!force;
                if (want && !has) __classes.push(c);
                if (!want && has) __classes = __classes.filter((x) => x !== c);
                return want;
            },
        };
        el.setAttribute = (name, value) => {
            el.__attrs[name] = String(value);
            if (name === "class") {
                __classes = String(value).split(/\s+/).filter(Boolean);
            }
            if (name.indexOf("data-") === 0) {
                const k = name.slice(5).replace(/-([a-z])/g, (_, ch) => ch.toUpperCase());
                el.dataset[k] = String(value);
            }
        };
        el.getAttribute = (name) => (name in el.__attrs ? el.__attrs[name] : null);
        Object.defineProperty(el, "textContent", {
            get: () => el.__text || "",
            set: (v) => {
                el.__text = String(v);
                el.__children = [];
                el.__innerHTMLRaw = String(v);
            },
            configurable: true,
        });
        Object.defineProperty(el, "innerHTML", {
            get: () => el.__innerHTMLRaw || "",
            set: (v) => __parseHtml(String(v == null ? "" : v), el),
            configurable: true,
        });
        el.style = {
            __props: {},
            setProperty: (k, v) => { el.style.__props[k] = String(v); },
            getPropertyValue: (k) => (el.style.__props[k] != null ? String(el.style.__props[k]) : ""),
            removeProperty: (k) => {
                const v = el.style.__props[k];
                delete el.style.__props[k];
                return v != null ? String(v) : "";
            },
        };
        el.appendChild = (child) => {
            __detach(child);
            child.__parent = el;
            el.__children.push(child);
            return child;
        };
        el.insertBefore = (child, ref) => {
            if (!ref || el.__children.indexOf(ref) === -1) return el.appendChild(child);
            __detach(child);
            child.__parent = el;
            el.__children.splice(el.__children.indexOf(ref), 0, child);
            return child;
        };
        el.remove = () => {
            if (el.__parent && el.__parent.__children) {
                const i = el.__parent.__children.indexOf(el);
                if (i !== -1) el.__parent.__children.splice(i, 1);
            }
            el.__parent = null;
        };
        Object.defineProperty(el, "children", {
            get: () => el.__children,
            configurable: true,
        });
        el.querySelectorAll = (sel) => __qsAll(el, sel);
        el.querySelector = (sel) => {
            const r = __qsAll(el, sel);
            return r.length ? r[0] : null;
        };
        return el;
    }

    function __detach(child) {
        if (child.__parent && child.__parent.__children) {
            const i = child.__parent.__children.indexOf(child);
            if (i !== -1) child.__parent.__children.splice(i, 1);
        }
    }

    function __applyAttrs(el, attrText) {
        const are = /([a-zA-Z-]+)="([^"]*)"/g;
        let am;
        while ((am = are.exec(attrText || "")) !== null) el.setAttribute(am[1], am[2]);
    }

    // Stack-based tokenizer: a naive `<tag ...>(.*?)</tag>` match mis-pairs
    // on same-tag nesting (column > column-body > card are all divs), so
    // open tags push and close tags pop.
    function __parseHtml(html, parent) {
        parent.__children = [];
        parent.__innerHTMLRaw = html;
        if (!html) return;
        const tokenRe = /<(\/?)([a-zA-Z]+)((?:\s+[a-zA-Z-]+(?:="[^"]*")?)*)\s*(\/?)>/g;
        const stack = [parent];
        let lastIndex = 0;
        let m;
        while ((m = tokenRe.exec(html)) !== null) {
            if (m.index > lastIndex) {
                const top = stack[stack.length - 1];
                top.__text = (top.__text || "") + html.slice(lastIndex, m.index);
            }
            lastIndex = tokenRe.lastIndex;
            if (m[1] === "/") {
                for (let i = stack.length - 1; i >= 1; i--) {
                    if (stack[i].__tag === m[2]) { stack.length = i; break; }
                }
                continue;
            }
            const el = __newEl(m[2]);
            __applyAttrs(el, m[3]);
            const top = stack[stack.length - 1];
            el.__parent = top;
            top.__children.push(el);
            if (m[4] !== "/") stack.push(el);
        }
        if (lastIndex < html.length) {
            const top = stack[stack.length - 1];
            top.__text = (top.__text || "") + html.slice(lastIndex);
        }
    }

    function __walk(root, out) {
        for (const child of root.__children || []) {
            out.push(child);
            __walk(child, out);
        }
        return out;
    }

    function __qsAll(root, sel) {
        const all = __walk(root, []);
        if (sel.indexOf(".") === 0) {
            const cls = sel.slice(1);
            return all.filter((e) => e.classList && e.classList.contains(cls));
        }
        if (sel.indexOf("[data-") === 0) {
            const key = sel.slice(6, sel.indexOf("="));
            const val = sel.slice(sel.indexOf('="') + 2, sel.length - 1);
            return all.filter((e) => e.dataset[key] === val);
        }
        return all.filter((e) => e.__tag === sel);
    }

    const __nav = __newEl("div");
    const __detail = __newEl("div");
    globalThis.window = globalThis;
    globalThis.document = {
        addEventListener: noop,
        removeEventListener: noop,
        documentElement: __newEl("html"),
        activeElement: null,
        hidden: false,
        body: __newEl("body"),
        getElementById: (id) => {
            if (id === "plan-list") return __nav;
            if (id === "plan-detail") return __detail;
            return __newEl("div");
        },
        createElement: (tag) => __newEl(tag),
        querySelector: (sel) => { const r = __qsAll(__detail, sel); return r.length ? r[0] : null; },
        querySelectorAll: (sel) => __qsAll(__detail, sel),
    };
    const __store = {};
    globalThis.localStorage = {
        getItem: (k) => (k in __store ? __store[k] : null),
        setItem: (k, v) => { __store[k] = String(v); },
        removeItem: (k) => { delete __store[k]; },
    };
    globalThis.window.localStorage = globalThis.localStorage;
    globalThis.window.addEventListener = noop;
    globalThis.window.matchMedia = () => ({
        matches: false, addEventListener: noop, removeEventListener: noop,
    });
    globalThis.fetch = () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
    globalThis.requestAnimationFrame = (fn) => setTimeout(fn, 0);
"""


def _run_board_js(expr):
    """Evaluate ``expr`` with static/app/render/board.js imported as an ES
    module (its named exports are on globalThis). Returns the JSON-decoded
    value; raises AssertionError with node's stderr on failure."""
    proc = run_app_js(expr, app_js=BOARD_JS, shim=_SHIM)
    if proc.returncode != 0:
        raise AssertionError(
            "node failed while evaluating a board.js expression — if this is a "
            "'isWedged is not defined' error, the helper is missing OR not "
            f"exported from static/app/render/board.js\nstderr:\n{proc.stderr}"
        )
    return json.loads(proc.stdout)


def _run_board_js_raw(expr):
    """Same as _run_board_js but returns the raw CompletedProcess (for tests
    that assert the call did not throw rather than inspect a value)."""
    return run_app_js(expr, app_js=BOARD_JS, shim=_SHIM)


def _board_js_source():
    with open(BOARD_JS, encoding="utf-8") as fh:
        return fh.read()


def _iso_minutes_ago(minutes):
    dt = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _wedge(wedged=True, reasons=None, **extra):
    w = {"wedged": wedged}
    if reasons is not None:
        w["reasons"] = reasons
    w.update(extra)
    return w


def _story(key="S1", status="in_progress", summary="a story", wedge=None,
           backend=None, escalated=False, last_activity=None):
    s = {"summary": summary, "status": status, "persona": "tech-lead", "risk": "medium"}
    if wedge is not None:
        s["wedge"] = wedge
    if backend is not None:
        s["backend"] = backend
    if escalated:
        s["escalated"] = True
    if last_activity is not None:
        s["last_activity"] = last_activity
    return s


def _class_tokens(html):
    """All whitespace-separated class tokens appearing anywhere in an HTML
    string. The wedged BADGE carries 'card-badge-wedged' (which contains
    'wedged' as a substring), so the bare 'wedged' CARD class is asserted as
    an exact token, never as a substring."""
    tokens = set()
    for attrs in re.findall(r'class="([^"]*)"', html):
        tokens.update(attrs.split())
    return tokens


def _wedged_badge_attrs(html):
    """Attributes of the card-badge-wedged span, or None if absent."""
    m = re.search(r'<span class="card-badge card-badge-wedged"([^>]*)>', html)
    return m.group(1) if m else None


# === isWedged: export + truth table ==========================================

def test_is_wedged_is_exported_from_board_js():
    """isWedged must be a function exported from static/app/render/board.js
    (same testability requirement the repo already places on _diffBoardCards
    in test_dashboard_board_diff.py)."""
    src = _board_js_source()
    assert "function isWedged(" in src, (
        "board.js must define a function isWedged(story)"
    )
    assert _run_board_js("typeof isWedged") == "function", (
        "isWedged must be exported from static/app/render/board.js's export "
        "block so the Node harness (and these tests) can call it"
    )


def test_is_wedged_truth_table():
    """isWedged returns true ONLY for {wedge: {wedged: true}} on an
    in_progress story; false for a missing wedge field, wedged: false, a
    null/undefined story, a non-object wedge, a non-strict-true wedged value,
    and stories with any other status (mirroring isStaleInProgress's
    `!story || story.status !== "in_progress"` guard shape) — including a
    done story that still carries a stale wedge object."""
    expr = (
        "(() => {"
        " if (typeof isWedged !== 'function') return { missingExport: true };"
        " const w = (wedged, extra) => Object.assign({ wedged }, extra || {});"
        " return {"
        "  wedged_true: isWedged({ status: 'in_progress', wedge: w(true, { reasons: ['pid dead'] }) }),"
        "  no_wedge_field: isWedged({ status: 'in_progress' }),"
        "  empty_story: isWedged({}),"
        "  wedged_false: isWedged({ status: 'in_progress', wedge: w(false, { reasons: [] }) }),"
        "  wedged_string_true: isWedged({ status: 'in_progress', wedge: w('true') }),"
        "  wedged_one: isWedged({ status: 'in_progress', wedge: w(1) }),"
        "  wedge_string: isWedged({ status: 'in_progress', wedge: 'wedged' }),"
        "  wedge_null: isWedged({ status: 'in_progress', wedge: null }),"
        "  null_story: isWedged(null),"
        "  undefined_story: isWedged(undefined),"
        "  done_with_stale_wedge: isWedged({ status: 'done', wedge: w(true, { reasons: ['pid dead'] }) }),"
        "  todo_with_wedge: isWedged({ status: 'todo', wedge: w(true) }),"
        "  tests_passed_with_wedge: isWedged({ status: 'tests_passed', wedge: w(true) }),"
        " };"
        " })()"
    )
    res = _run_board_js(expr)
    assert not res.get("missingExport"), res
    expected_true = {"wedged_true"}
    got_true = {k for k, v in res.items() if v is True}
    got_false = {k for k, v in res.items() if v is False}
    assert got_true == expected_true, (
        f"isWedged must return true ONLY for an in_progress story with "
        f"wedge.wedged === true; unexpected true cases: {sorted(got_true - expected_true)}"
    )
    assert got_false == set(res) - expected_true, (
        f"isWedged must return false (not undefined/throw) for every "
        f"non-wedged case; cases not explicitly false: "
        f"{sorted(set(res) - expected_true - got_false)}"
    )


def test_is_wedged_does_not_rederive_staleness_from_last_activity():
    """isWedged must be purely server-verdict-driven: a long-idle in_progress
    story WITHOUT a wedge field is NOT wedged (that is isStaleInProgress's
    `.stale` job), and a just-polled in_progress story WITH wedge.wedged true
    IS wedged. The two signals must not be conflated."""
    old = _iso_minutes_ago(240)
    fresh = _iso_minutes_ago(1)
    expr = (
        "(() => {"
        " if (typeof isWedged !== 'function') return { missing: true };"
        " return {"
        "  old_no_wedge: isWedged({ status: 'in_progress', last_activity: " + json.dumps(old) + " }),"
        "  fresh_wedged: isWedged({ status: 'in_progress', last_activity: " + json.dumps(fresh) + ", wedge: { wedged: true, reasons: ['pid dead'] } }),"
        "  old_wedged: isWedged({ status: 'in_progress', last_activity: " + json.dumps(old) + ", wedge: { wedged: true, reasons: [] } })"
        " };"
        " })()"
    )
    res = _run_board_js(expr)
    assert res.get("old_no_wedge") is False, (
        "isWedged must not re-derive staleness from last_activity — an aged "
        "story without a server wedge verdict is not wedged"
    )
    assert res.get("fresh_wedged") is True
    assert res.get("old_wedged") is True


def test_is_stale_in_progress_and_stale_class_keep_working():
    """The pre-existing last_activity aging must be untouched: isStaleInProgress
    still returns true for an aged in_progress story, and renderBoard's string
    path still puts the bare `stale` class token on that card (and the stale
    card-age variant). Guards the 'do NOT change isStaleInProgress' rule."""
    old = _iso_minutes_ago(240)
    fresh = _iso_minutes_ago(1)
    expr = (
        "(() => {"
        " if (typeof isStaleInProgress !== 'function') return { missing: true };"
        " const aged = { status: 'in_progress', last_activity: " + json.dumps(old) + " };"
        " const fresh = { status: 'in_progress', last_activity: " + json.dumps(fresh) + " };"
        " const staleCheck = { aged: isStaleInProgress(aged), fresh: isStaleInProgress(fresh), done: isStaleInProgress({ status: 'done', last_activity: " + json.dumps(old) + " }) };"
        " const html = renderBoard({ S1: aged });"
        " return { staleCheck: staleCheck(staleCheckPlaceholder), html };"
        " })()"
    )
    # The placeholder above is replaced below; build the real expression
    # cleanly instead of relying on string surgery.
    expr = (
        "(() => {"
        " if (typeof isStaleInProgress !== 'function') return { missing: true };"
        " const aged = { status: 'in_progress', last_activity: " + json.dumps(old) + " };"
        " const fresh = { status: 'in_progress', last_activity: " + json.dumps(fresh) + " };"
        " const staleCheck = {"
        "  aged: isStaleInProgress(aged),"
        "  fresh: isStaleInProgress(fresh),"
        "  done: isStaleInProgress({ status: 'done', last_activity: " + json.dumps(old) + " })"
        " };"
        " const html = renderBoard({ S1: aged });"
        " return { staleCheck, html };"
        " })()"
    )
    res = _run_board_js(expr)
    assert res.get("staleCheck") == {"aged": True, "fresh": False, "done": False}, (
        f"isStaleInProgress behaviour changed: {res.get('staleCheck')}"
    )
    html = res.get("html") or ""
    assert "stale" in _class_tokens(html), (
        "the .stale card class must still be applied to aged in_progress cards"
    )


# === String-build path (renderBoard with no existing board) ==================

def _render_html(story):
    expr = "renderBoard({ S1: " + json.dumps(story) + " })"
    return _run_board_js(expr)


def test_render_board_string_path_renders_wedged_badge_and_class():
    """A wedged in_progress story rendered through renderBoard's full
    string-build path carries: the `card-badge card-badge-wedged` span with
    badge text `wedged`, a title of the form `Wedged: <reasons joined by
    ', '>`, and the bare `wedged` class token on the card element itself."""
    story = _story(wedge=_wedge(reasons=["pid 123 is dead", "worktree idle 45m"]))
    html = _render_html(story)
    assert isinstance(html, str) and "card" in html, f"renderBoard returned no markup: {html!r}"
    assert 'class="card-badge card-badge-wedged"' in html, (
        f"wedged badge span missing from the string-build path: {html!r}"
    )
    assert ">wedged</span>" in html, (
        "the wedged badge's visible text must be 'wedged'"
    )
    assert "wedged" in _class_tokens(html), (
        "the card element itself must carry a bare 'wedged' class (distinct "
        "from the badge's 'card-badge-wedged' token)"
    )
    assert 'title="Wedged: pid 123 is dead, worktree idle 45m"' in html, (
        "the badge title must be 'Wedged: ' + reasons joined by ', '"
    )


def test_render_board_string_path_healthy_story_has_no_wedged_badge():
    """Negative cases through the same path: no wedge field at all, and a
    present-but-healthy wedge (wedged: false — the backend sends the explicit
    false for checked-and-fine stories) must produce NO wedged badge and NO
    bare wedged class token."""
    for story in (
        _story(),
        _story(wedge=_wedge(wedged=False, reasons=[])),
        _story(status="done", wedge=_wedge(wedged=True, reasons=["pid dead"])),
    ):
        html = _render_html(story)
        assert "card-badge-wedged" not in html, (
            f"wedged badge must not render for {story!r}"
        )
        assert "wedged" not in _class_tokens(html), (
            f"bare 'wedged' class token must not appear for {story!r}"
        )


def test_render_board_wedged_badge_coexists_with_existing_badges():
    """The wedged badge is ADDED alongside the pre-existing backend/escalated
    badges (membership, not counts — other stories may carry other badges)."""
    story = _story(
        backend="claude",
        escalated=True,
        wedge=_wedge(reasons=["pid 123 is dead"]),
    )
    html = _render_html(story)
    assert "card-badge-claude" in html, "pre-existing claude badge must survive"
    assert "card-badge-escalated" in html, "pre-existing escalated badge must survive"
    assert "card-badge-wedged" in html, "wedged badge must be added alongside them"


def test_render_board_escapes_reasons_in_badge_title():
    """Reasons are interpolated into a double-quoted title attribute, so they
    must go through the module's escapeHtml: <, >, &, " and ' are escaped and
    no raw markup survives into the attribute."""
    story = _story(
        wedge=_wedge(reasons=['pid <dead> & "gone"', "it's stuck"]),
    )
    html = _render_html(story)
    expected = (
        'title="Wedged: pid &lt;dead&gt; &amp; &quot;gone&quot;, it&#39;s stuck"'
    )
    assert expected in html, (
        f"reasons must be HTML-escaped in the badge title; wanted {expected!r} "
        f"in {html!r}"
    )
    assert "<dead>" not in html, "raw angle brackets from reasons leaked into markup"


def test_render_board_empty_reasons_title_still_renders_without_undefined():
    """Boundary: an empty reasons array must still render the badge with a
    well-formed title — never the string 'undefined'."""
    html = _render_html(_story(wedge=_wedge(reasons=[])))
    assert "card-badge-wedged" in html
    assert "undefined" not in html, f"'undefined' leaked into markup: {html!r}"
    assert re.search(r'class="card-badge card-badge-wedged"\s+title="Wedged:?', html), (
        f"badge title attribute missing with empty reasons: {html!r}"
    )


def test_render_board_missing_reasons_field_renders_defensively():
    """Boundary: wedge.wedged true with NO reasons field at all (and a
    non-array reasons value) must not throw and must not render 'undefined' —
    the badge is either omitted or rendered defensively."""
    for wedge in (
        _wedge(reasons=None),
        _wedge(reasons="pid dead"),
        _wedge(reasons=None, measured={"pid_alive": False}),
    ):
        proc = run_app_js(
            "renderBoard({ S1: " + json.dumps(_story(wedge=wedge)) + " })",
            app_js=BOARD_JS, shim=_SHIM,
        )
        assert proc.returncode == 0, (
            f"renderBoard threw on wedge={wedge!r}: {proc.stderr}"
        )
        assert "undefined" not in proc.stdout, (
            f"'undefined' leaked into markup for wedge={wedge!r}: {proc.stdout!r}"
        )


def test_render_board_malformed_wedge_shapes_do_not_throw():
    """Boundary: a non-object wedge (string/number) must be treated as not
    wedged: no badge, no throw, no 'undefined' in the markup."""
    for wedge in ("wedged", 1, [], True):
        proc = run_app_js(
            "renderBoard({ S1: " + json.dumps(_story(wedge=wedge)) + " })",
            app_js=BOARD_JS, shim=_SHIM,
        )
        assert proc.returncode == 0, (
            f"renderBoard threw on wedge={wedge!r}: {proc.stderr}"
        )
        html = json.loads(proc.stdout)
        assert "card-badge-wedged" not in html
        assert "wedged" not in _class_tokens(html)


# === In-place update path (_diffBoardCards, the second-poll path) ============

def _diff_cards_expr(pairs_a, pairs_b=None):
    """Build an expr that calls _diffBoardCards once (or twice, poll-to-poll)
    against the same .column-body element and reports each .card's key, node
    id, class list and raw inner HTML."""
    collect = (
        " const collect = () => body.querySelectorAll('.card').map((c) => ({"
        " key: c.dataset.key, testId: c.__testId, cls: c.className,"
        " html: c.__innerHTMLRaw }));"
    )
    expr = (
        "(() => {"
        " const body = document.createElement('div');"
        " body.className = 'column-body';"
        + collect
        + f" _diffBoardCards(body, {json.dumps(pairs_a)});"
        + " const first = collect();"
    )
    if pairs_b is not None:
        expr += (
            f" _diffBoardCards(body, {json.dumps(pairs_b)});"
            " const second = collect();"
        )
    expr += " return { first" + (", second" if pairs_b is not None else "") + " }; })()"
    return expr


def _entry(key, story):
    return [key, story]


def test_diff_board_cards_renders_wedged_badge_and_class():
    """The in-place card builder (_diffBoardCards — what every poll after the
    first goes through) must render the wedged badge and the bare wedged
    class exactly like the string path, or the badge silently disappears on
    the second poll."""
    res = _run_board_js(_diff_cards_expr([
        _entry("S1", _story(wedge=_wedge(reasons=["pid 123 is dead"]))),
    ]))
    cards = {c["key"]: c for c in res["first"]}
    assert "S1" in cards, f"no card built: {res}"
    card = cards["S1"]
    assert "card-badge-wedged" in card["html"], (
        f"wedged badge missing from _diffBoardCards output: {card['html']!r}"
    )
    assert ">wedged</span>" in card["html"]
    assert "wedged" in set(card["cls"].split()), (
        f"bare 'wedged' class token missing on the card: {card['cls']!r}"
    )
    assert 'title="Wedged: pid 123 is dead"' in card["html"]


def test_diff_board_cards_healthy_story_has_no_wedged_badge():
    res = _run_board_js(_diff_cards_expr([
        _entry("S1", _story()),
        _entry("S2", _story(key="S2", wedge=_wedge(wedged=False, reasons=[]))),
    ]))
    cards = {c["key"]: c for c in res["first"]}
    for key in ("S1", "S2"):
        assert "card-badge-wedged" not in cards[key]["html"], (
            f"healthy story {key} must not carry a wedged badge"
        )
        assert "wedged" not in set(cards[key]["cls"].split())


def test_diff_board_cards_toggles_badge_in_place_across_polls():
    """Poll-to-poll flip: a story that becomes wedged between polls keeps the
    SAME card node (the whole point of the keyed diff) and gains the badge;
    one that becomes healthy loses it. This is the regression the two-path
    requirement exists for."""
    healthy = _entry("S1", _story())
    wedged = _entry("S1", _story(wedge=_wedge(reasons=["pid 123 is dead"])))

    res = _run_board_js(_diff_cards_expr(healthy, wedged))
    first = {c["key"]: c for c in res["first"]}
    second = {c["key"]: c for c in res["second"]}
    assert first["S1"]["testId"] == second["S1"]["testId"], (
        "card S1 was recreated when only its wedge state changed"
    )
    assert "card-badge-wedged" not in first["S1"]["html"]
    assert "card-badge-wedged" in second["S1"]["html"], (
        "the in-place update path must ADD the wedged badge on the poll where "
        "the story becomes wedged"
    )
    assert "wedged" in set(second["S1"]["cls"].split())

    res = _run_board_js(_diff_cards_expr(wedged, healthy))
    first = {c["key"]: c for c in res["first"]}
    second = {c["key"]: c for c in res["second"]}
    assert first["S1"]["testId"] == second["S1"]["testId"]
    assert "card-badge-wedged" in first["S1"]["html"]
    assert "card-badge-wedged" not in second["S1"]["html"], (
        "the in-place update path must REMOVE the wedged badge when the story "
        "is no longer wedged"
    )
    assert "wedged" not in set(second["S1"]["cls"].split())


def test_diff_board_cards_rejects_malformed_bare_pair_instead_of_reshaping_it():
    """Regression for the speculative bare-pair heuristic in _diffBoardCards
    (static/app/render/board.js, ~line 308): ``storiesForColumn`` is a
    malformed bare ``[key, story]`` pair here -- NOT wrapped in the outer
    array the function's contract requires (an array of ``[key, story]``
    pairs). This is deliberately the exact malformed shape a caller bug can
    produce (see the existing, not-to-be-modified
    ``test_diff_board_cards_toggles_badge_in_place_across_polls``, whose
    ``_diff_cards_expr(healthy, wedged)`` call passes bare pairs positionally
    instead of ``_diff_cards_expr([healthy, wedged])``).

    Once ``pairs`` is used directly as ``storiesForColumn`` (no reshaping),
    the natural ``for (const [key, s] of pairs)`` destructuring throws a
    TypeError on the malformed shape -- the loop cannot treat a bare
    ``[key, story]`` pair as a list of pairs, because iterating it yields the
    string key's characters, then a plain (non-iterable) story object.

    The heuristic being reverted-in launders this instead: because the bare
    pair happens to have ``.length === 2`` with a non-array first element, it
    gets speculatively rewrapped into ``[storiesForColumn]`` and silently
    "succeeds" -- rendering a card as if the call were valid. That silent
    success is the bug: it must fail loudly, not be reinterpreted.
    """
    proc = _run_board_js_raw(_diff_cards_expr(_entry("S1", _story())))
    assert proc.returncode != 0, (
        "a malformed bare [key, story] pair (not wrapped in an outer list of "
        "pairs) must fail rather than silently succeed; got exit code 0 with "
        f"stdout={proc.stdout!r} -- this means the speculative bare-pair "
        "reshaping heuristic in _diffBoardCards is still present and is "
        "silently reinterpreting the malformed call as valid input instead "
        "of letting it fail"
    )
    assert "not iterable" in proc.stderr, (
        "expected a TypeError from the natural for-of destructuring failure "
        "once storiesForColumn is used directly as pairs (no reshaping) -- "
        f"got stderr={proc.stderr!r}"
    )


# === Full in-place path (_tryUpdateBoardInPlace via renderBoard) =============

def _in_place_update_expr(story_a, story_b):
    """First render builds the board from a string; the second renderBoard
    call passes the live board element so _tryUpdateBoardInPlace runs. If the
    card node's __testId survives, the in-place diff genuinely served the
    update (a full rebuild would create fresh nodes)."""
    return (
        "(() => {"
        " const first = renderBoard({ S1: " + json.dumps(story_a) + " });"
        " const wrap = document.createElement('div');"
        " wrap.innerHTML = first;"
        " const before = wrap.querySelectorAll('.card').map((c) => ({"
        " key: c.dataset.key, testId: c.__testId, cls: c.className,"
        " html: c.__innerHTMLRaw }));"
        " const secondReturn = renderBoard({ S1: " + json.dumps(story_b) + " }, wrap);"
        " const after = wrap.querySelectorAll('.card').map((c) => ({"
        " key: c.dataset.key, testId: c.__testId, cls: c.className,"
        " html: c.__innerHTMLRaw }));"
        " return { first, before, secondReturn: secondReturn, after };"
        " })()"
    )


def test_try_update_board_in_place_renders_wedged_badge():
    """End-to-end in-place path: healthy -> wedged across two renderBoard
    calls against the SAME board element. The card node must be REUSED
    (proving _tryUpdateBoardInPlace/_diffBoardCards served the update, not a
    teardown) and must then carry the wedged badge and class."""
    story_a = _story()
    story_b = _story(wedge=_wedge(reasons=["pid 123 is dead"]))
    res = _run_board_js(_in_place_update_expr(story_a, story_b))
    assert res["first"], "first renderBoard call returned no markup"
    before = {c["key"]: c for c in res["before"]}
    after = {c["key"]: c for c in res["after"]}
    assert "S1" in before and "S1" in after, f"card S1 missing: {res}"
    assert after["S1"]["testId"] == before["S1"]["testId"], (
        "the card was recreated on the second poll — _tryUpdateBoardInPlace "
        "did not serve the update, so this test did not exercise the in-place "
        "path (check the DOM shim's column parsing before suspecting board.js)"
    )
    assert "card-badge-wedged" in after["S1"]["html"], (
        "the in-place update path dropped the wedged badge — it must be "
        "pushed in _diffBoardCards' badge list too, not only in the string path"
    )
    assert "wedged" in set(after["S1"]["cls"].split())
    assert 'title="Wedged: pid 123 is dead"' in after["S1"]["html"]


def test_try_update_board_in_place_healthy_story_has_no_badge():
    """Same path, both polls healthy: no wedged badge either time (guards
    against a badge that latches on once rendered)."""
    story = _story()
    res = _run_board_js(_in_place_update_expr(story, story))
    for phase in ("before", "after"):
        cards = {c["key"]: c for c in res[phase]}
        assert "card-badge-wedged" not in cards["S1"]["html"]
        assert "wedged" not in set(cards["S1"]["cls"].split())


# === static/style.css: appended .card-badge-wedged rule ======================

def test_style_css_defines_wedged_badge_rule_after_existing_badges():
    """static/style.css must gain a .card-badge-wedged rule, appended AFTER
    the pre-existing badge rules (append-at-end; the interior of the file is
    off-limits), using the same var()/color-token pattern as
    .card-badge-claude / .card-badge-escalated rather than a new token system.

    Ordering-relative assertions only: style.css is a shared artifact that
    later stories append to, so nothing here pins the file's total contents,
    length, or hash."""
    with open(STYLE_CSS, encoding="utf-8") as fh:
        css = fh.read()
    assert ".card-badge-wedged" in css, (
        "static/style.css must define a .card-badge-wedged rule"
    )
    pos = css.index(".card-badge-wedged")
    for anchor in (".card-badge {", ".card-badge-claude", ".card-badge-escalated"):
        assert anchor in css, f"expected pre-existing anchor {anchor!r} in style.css"
        assert css.index(anchor) < pos, (
            f".card-badge-wedged must be appended after the existing {anchor!r} "
            "rule, not spliced into the file's interior"
        )
    end = css.find("}", pos)
    assert end != -1, ".card-badge-wedged rule block is never closed"
    block = css[pos:end]
    assert "var(--" in block, (
        ".card-badge-wedged must reuse the existing var()/palette tokens "
        "(cf. .card-badge-claude's var(--accent*), .card-badge-escalated's "
        "var(--c-parked))"
    )
    assert "color:" in block, (
        ".card-badge-wedged must set a text color like the other card badges"
    )