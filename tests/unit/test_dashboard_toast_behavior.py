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

from tests.unit import _dom_shims
from tests.unit._app_js import run_app_js as _shared_run_app_js

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_JS = os.path.join(REPO_ROOT, "static", "app.js")


_SHIM = _dom_shims.TOAST_SHIM


def _run_app_js(expr):
    """Evaluate a JS expression inside an environment where static/app.js
    has been loaded. Returns the JSON-serialized result."""
    proc = _shared_run_app_js(expr, shim=_SHIM)
    if proc.returncode != 0:
        raise AssertionError(f"node failed: {proc.stderr}")
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
    assert res["askText"] == "Ask Tower"


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
