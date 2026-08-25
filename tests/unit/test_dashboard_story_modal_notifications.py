"""Tests for the per-story notifications panel inside the story modal
(static/app.js). Exercises the pure helpers `filterStoryNotifications` and
`renderStoryModalNotifications`, plus the end-to-end wiring through
`showStoryModal` -> `_renderStoryModalBody`, by shelling out to Node in a
subprocess. This mirrors tests/unit/test_dashboard.py's `_run_app_js`
harness (copied below rather than imported, per the pipeline story schema's
guidance to keep new test files self-contained).

These tests are written against static/app.js BEFORE the implementation
exists (P3-12 depends on P3-11's rename-and-delegate split, which is
already merged; the notifications wiring itself is not). They are expected
to fail with a ReferenceError (undefined function) or an assertion
mismatch against the current 3-arg showStoryModal signature until a later
dispatch implements the four anchored edits described in the story.
"""
import json
import os
import subprocess

from _app_js import run_app_js

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_JS = os.path.join(REPO_ROOT, "static", "app.js")


def _run_app_js(expr):
    """Evaluate a JS expression inside an environment where static/app.js
    has been loaded (so its top-level consts + functions are available).
    Returns the JSON-serialized result. Keeps the assertion surface area
    in Python where the rest of the suite already lives.

    app.js touches `document`/`window` at module load to wire DOM event
    listeners; we stub those out so the pure helpers below are testable
    without pulling in jsdom."""
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
            // app.js reads window.location.hash at boot (applyHashToState) and
            // assigns it back; stub a plain location with an empty hash. The
            // hashchange listener is wired at module load too.
            location: { hash: "" },
            addEventListener: noop,
        };
        globalThis.localStorage = { getItem: () => null, setItem: noop };
        globalThis.fetch = () => new Promise(() => {}); // never resolves
        process.on("unhandledRejection", () => {});
        // app.js calls setInterval(refresh, 4000) at module load. In Node
        // that keeps the event loop alive after we've printed the result;
        // override so the process can exit naturally.
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


# ---------- filterStoryNotifications ----------

def test_filter_story_notifications_selects_matching_story_key():
    records = [
        {"story_key": "P3-1", "message": "a"},
        {"story_key": "P3-2", "message": "b"},
        {"story_key": "P3-2", "message": "c"},
    ]
    expr = f"filterStoryNotifications({json.dumps(records)}, 'P3-2')"
    result = _run_app_js(expr)
    assert [r["message"] for r in result] == ["b", "c"], result


def test_filter_story_notifications_undefined_records_returns_empty():
    result = _run_app_js("filterStoryNotifications(undefined, 'P3-2')")
    assert result == []


def test_filter_story_notifications_does_not_mutate_input():
    records = [
        {"story_key": "P3-1", "message": "a"},
        {"story_key": "P3-2", "message": "b"},
    ]
    expr = (
        "(() => {"
        f" const records = {json.dumps(records)};"
        " filterStoryNotifications(records, 'P3-2');"
        " return records;"
        " })()"
    )
    result = _run_app_js(expr)
    assert result == records, result


# ---------- renderStoryModalNotifications ----------

def test_render_story_modal_notifications_empty_state():
    html = _run_app_js("renderStoryModalNotifications([])")
    assert "No notifications for this story." in html


def test_render_story_modal_notifications_undefined_does_not_throw():
    html = _run_app_js("renderStoryModalNotifications(undefined)")
    assert "No notifications for this story." in html


def test_render_story_modal_notifications_shows_severity_badge_and_message():
    records = [{"severity": "warning", "message": "x"}]
    html = _run_app_js(f"renderStoryModalNotifications({json.dumps(records)})")
    assert "--c-parked" in html, html
    assert "x" in html, html


def test_render_story_modal_notifications_shows_count_badge_when_greater_than_one():
    records = [{"severity": "info", "message": "m", "count": 4}]
    html = _run_app_js(f"renderStoryModalNotifications({json.dumps(records)})")
    assert "x4" in html, html

    records_singleton = [{"severity": "info", "message": "m", "count": 1}]
    html_singleton = _run_app_js(f"renderStoryModalNotifications({json.dumps(records_singleton)})")
    assert "x1" not in html_singleton, html_singleton

    records_no_count = [{"severity": "info", "message": "m"}]
    html_no_count = _run_app_js(f"renderStoryModalNotifications({json.dumps(records_no_count)})")
    assert "x0" not in html_no_count, html_no_count
    assert "xundefined" not in html_no_count, html_no_count


def test_render_story_modal_notifications_escapes_html_in_message():
    records = [{"severity": "error", "message": "<img src=x onerror=alert(1)>"}]
    html = _run_app_js(f"renderStoryModalNotifications({json.dumps(records)})")
    assert "&lt;img" in html, html
    assert "<img" not in html, html


def test_render_story_modal_notifications_handles_null_array_element():
    """A null element in the records array must not crash the render and
    must not print the literal string "undefined" for the missing message."""
    expr = "renderStoryModalNotifications([null, {severity: 'info', message: 'ok'}])"
    html = _run_app_js(expr)
    assert "undefined" not in html, html
    assert "ok" in html, html


def test_render_story_modal_notifications_newest_first():
    records = [
        {"severity": "info", "message": "first-chronologically"},
        {"severity": "info", "message": "second-chronologically"},
    ]
    html = _run_app_js(f"renderStoryModalNotifications({json.dumps(records)})")
    first_idx = html.index("first-chronologically")
    second_idx = html.index("second-chronologically")
    assert second_idx < first_idx, html


# ---------- end-to-end wiring: showStoryModal -> notifications slot ----------

def test_show_story_modal_wires_notifications_into_the_dom():
    """Proves the wiring landed end-to-end: showStoryModal must pass the
    filtered records down into _renderStoryModalBody, which must render
    them into a `data-notifications-slot` block inside the modal body's
    innerHTML. Mirrors the DOM-stub pattern used in
    test_dashboard.py's test_render_board_card_click_wires_show_story_modal,
    but here overriding document.getElementById so 'story-modal' and
    'story-modal-body' resolve to the SAME stable object across repeated
    calls (mutable innerHTML string, plain dataset object, no-op classList)."""
    expr = (
        "(() => {"
        " const noop = () => {};"
        " const modalEl = {"
        "   innerHTML: '',"
        "   dataset: {},"
        "   classList: { add: noop, remove: noop, toggle: noop, contains: () => false },"
        "   querySelector: () => null,"
        "   addEventListener: noop,"
        " };"
        " const bodyEl = {"
        "   innerHTML: '',"
        "   dataset: {},"
        "   classList: { add: noop, remove: noop, toggle: noop, contains: () => false },"
        "   querySelector: () => null,"
        "   addEventListener: noop,"
        " };"
        " globalThis.document.getElementById = (id) => {"
        "   if (id === 'story-modal') return modalEl;"
        "   if (id === 'story-modal-body') return bodyEl;"
        "   return null;"
        " };"
        " const records = [{story_key: 'P3-2', severity: 'warning', message: 'm', count: 1}];"
        " showStoryModal('planX', {summary: 's'}, 'P3-2', records);"
        " return bodyEl.innerHTML;"
        " })()"
    )
    body_html = _run_app_js(expr)
    assert "data-notifications-slot" in body_html, body_html
    assert "m" in body_html, body_html
