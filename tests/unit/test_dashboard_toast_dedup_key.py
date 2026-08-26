"""Regression tests for the toast-notification "never fires" bug.

ROOT CAUSE (confirmed live against the running dashboard): the toast
pipeline required every notification record to carry a non-null
`dedup_key` before it would ever be considered "new" and toasted, but the
large majority of backend `_notify_user()` call sites never pass one
(defaulting to `dedup_key=None`). `static/app/render/notifications.js`'s
own `_diffNotificationsPanel` (the separate notifications LOG panel, not
the toast stack) already handles this correctly with a fallback:
`const key = r.dedup_key || (r.ts + '-' + r.message);` — this fix mirrors
that exact fallback into a shared `notificationKey` helper used by the
toast pipeline's `pickNewNotifications` (static/app/render/notifications.js)
and by `refresh()`'s seed/toast bookkeeping (static/app/main.js).

Scope is exactly 2 files: static/app/render/notifications.js and
static/app/main.js. `_diffNotificationsPanel` itself must NOT change.

This file mirrors the node-eval harness pattern used throughout
tests/unit/test_dashboard_notification_filter.py and
tests/unit/test_dashboard_notifications_ui.py: it builds a minimal DOM
shim, evals static/app.js (which re-exports static/app/main.js) under
`node -e` via the shared harness, and JSON-stringifies the result of a
test expression. The shim is copied here (rather than imported) so this
file stands alone.

These tests are RED until the implementation lands: `notificationKey`
does not exist yet, `pickNewNotifications` still drops any record whose
`dedup_key` is null/undefined, and main.js's seed/toast bookkeeping still
gates on `dedup_key != null`.
"""
import json
import os
import re

from tests.unit._app_js import run_app_js as _shared_run_app_js

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_JS = os.path.join(REPO_ROOT, "static", "app.js")
NOTIFICATIONS_JS = os.path.join(REPO_ROOT, "static", "app", "render", "notifications.js")
MAIN_JS = os.path.join(REPO_ROOT, "static", "app", "main.js")


_SHIM = r"""
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


def _run_app_js(expr):
    """Evaluate a JS expression inside an environment where static/app.js
    has been loaded. Returns the JSON-serialized result."""
    proc = _shared_run_app_js(expr, shim=_SHIM)
    if proc.returncode != 0:
        raise AssertionError(f"node failed: {proc.stderr}")
    return json.loads(proc.stdout)


def _pick(plans, seen_map_entries):
    """Helper: call pickNewNotifications(plans, seenMap) where seenMap is
    built from a list of (planName, seenKey) pairs."""
    entries_js = json.dumps(seen_map_entries)
    expr = f"pickNewNotifications({json.dumps(plans)}, new Map({entries_js}))"
    return _run_app_js(expr)


def _notifications_js_source():
    with open(NOTIFICATIONS_JS, encoding="utf-8") as fh:
        return fh.read()


def _main_js_source():
    with open(MAIN_JS, encoding="utf-8") as fh:
        return fh.read()


# === 1: null dedup_key, empty seen map -> returned as new =================
# Exact fixture from the live bug: a real notification observed via
# `curl /api/plans` had `"dedup_key": null`.

def test_null_dedup_key_returned_as_new_with_empty_seen_map():
    plans = [{
        "name": "demo-plan",
        "latest_notification": {
            "dedup_key": None,
            "ts": "2026-01-01T00:00:00Z",
            "message": "x failed",
            "severity": "error",
            "story_key": "S1",
        },
    }]
    res = _pick(plans, [])
    assert len(res) == 1, (
        "a notification record with dedup_key=null must still be treated as "
        "new when nothing has been seen for its plan yet — this is the "
        "toaster-never-fires bug"
    )
    assert res[0]["plan"]["name"] == "demo-plan"
    assert res[0]["record"]["dedup_key"] is None


# === 2: same record's derived key already seen -> suppressed ==============

def test_null_dedup_key_suppressed_when_derived_key_already_seen():
    record = {
        "dedup_key": None,
        "ts": "2026-01-01T00:00:00Z",
        "message": "x failed",
        "severity": "error",
        "story_key": "S1",
    }
    plans = [{"name": "demo-plan", "latest_notification": record}]
    derived_key = record["ts"] + "-" + record["message"]
    res = _pick(plans, [["demo-plan", derived_key]])
    assert res == [], (
        "once the derived key (ts + '-' + message) for this exact record has "
        "been recorded as seen, the same record must not be re-toasted"
    )


# === 3: regression — a record WITH a real dedup_key still behaves as before

def test_real_dedup_key_returned_once_then_suppressed_on_second_call():
    plans = [{
        "name": "demo-plan",
        "latest_notification": {
            "dedup_key": "abc123",
            "ts": "2026-01-01T00:00:00Z",
            "message": "y failed",
            "severity": "error",
            "story_key": "S2",
        },
    }]
    first = _pick(plans, [])
    assert len(first) == 1
    assert first[0]["record"]["dedup_key"] == "abc123"

    second = _pick(plans, [["demo-plan", "abc123"]])
    assert second == [], (
        "a record with a real dedup_key must still be suppressed once that "
        "exact dedup_key has been seen (pre-existing behavior must survive "
        "the fix)"
    )


# === Formula fidelity: the fallback must be exactly ts + '-' + message ====

def test_derived_key_requires_exact_ts_hyphen_message_format():
    record = {
        "dedup_key": None,
        "ts": "2026-02-02T00:00:00Z",
        "message": "z failed",
        "severity": "warning",
        "story_key": "S3",
    }
    plans = [{"name": "demo-plan", "latest_notification": record}]

    # A wrongly-formatted "seen" key (no hyphen separator) must NOT suppress.
    wrong_key = record["ts"] + record["message"]
    res_wrong = _pick(plans, [["demo-plan", wrong_key]])
    assert len(res_wrong) == 1, (
        "a seen-map entry that doesn't match the exact "
        "`ts + '-' + message` fallback format must not suppress the toast"
    )

    # The correctly-formatted key DOES suppress.
    correct_key = record["ts"] + "-" + record["message"]
    res_correct = _pick(plans, [["demo-plan", correct_key]])
    assert res_correct == []


# === Negative / boundary cases =============================================

def test_plan_with_no_latest_notification_is_skipped():
    """Boundary: a plan with no latest_notification at all must remain
    silently skipped — the fix must not treat `null` as a notification
    record to synthesize a key for."""
    plans = [{"name": "demo-plan", "latest_notification": None}]
    res = _pick(plans, [])
    assert res == []


def test_empty_plans_list_returns_empty_list():
    """Boundary: zero-element input collection."""
    res = _pick([], [])
    assert res == []


def test_missing_ts_field_does_not_throw():
    """Malformed/boundary input: a record missing `ts` entirely must not
    raise inside pickNewNotifications (Node would otherwise throw evaluating
    the expression, which the harness surfaces as a non-zero exit code)."""
    plans = [{
        "name": "demo-plan",
        "latest_notification": {
            "dedup_key": None,
            "message": "no ts here",
            "severity": "info",
            "story_key": "S4",
        },
    }]
    res = _pick(plans, [])
    assert len(res) == 1


# === Structural: static/app/render/notifications.js ========================

_NOTIFICATIONS_KEY_AND_PICK_AFTER = """function notificationKey(rec) {
  return rec.dedup_key || (rec.ts + '-' + rec.message);
}

function pickNewNotifications(plans, seenMap) {
  const newNotifs = [];
  for (const plan of plans) {
    const rec = plan.latest_notification;
    if (!rec) continue;
    const key = notificationKey(rec);
    const seen = seenMap.get(plan.name);
    if (seen !== key) {
      newNotifs.push({ plan, record: rec });
    }
  }
  return newNotifs;
}"""


def test_notification_key_and_pick_new_notifications_rewritten_exactly():
    """The prescribed rewrite of pickNewNotifications plus the new
    notificationKey helper must appear verbatim in notifications.js."""
    js = _notifications_js_source()
    assert _NOTIFICATIONS_KEY_AND_PICK_AFTER in js, (
        "expected the exact notificationKey + pickNewNotifications rewrite "
        "to be present in static/app/render/notifications.js"
    )


def test_old_dedup_key_null_guard_removed_from_notifications_js():
    js = _notifications_js_source()
    assert "if (!rec || rec.dedup_key == null) continue;" not in js, (
        "the old guard that dropped any record with a null dedup_key must "
        "be removed"
    )


def test_notification_key_added_to_export_block_without_removing_existing_exports():
    js = _notifications_js_source()
    match = re.search(r"export \{(.*?)\};", js, re.DOTALL)
    assert match, "notifications.js must have a final export { ... }; block"
    block = match.group(1)
    for name in [
        "initNotifications", "NOTIF_SEVERITY_COLOR", "pickNewNotifications",
        "pushToast", "notifSeverityFilter", "setNotifSeverityFilter",
        "filterNotifications", "decodeHtmlEntities", "renderNotifications",
        "_diffNotificationsPanel", "notificationKey",
    ]:
        assert name in block, (
            f"expected '{name}' to remain (or be added) in notifications.js's "
            f"export block, got: {block}"
        )


def test_diff_notifications_panel_function_left_untouched():
    """_diffNotificationsPanel must not be modified — its own inline
    fallback is the pattern being mirrored, not replaced."""
    js = _notifications_js_source()
    assert "const key = r.dedup_key || (r.ts + '-' + r.message);" in js, (
        "_diffNotificationsPanel's own fallback line must remain unchanged"
    )
    match = re.search(
        r"function _diffNotificationsPanel\(panelBodyEl, records\) \{(.*?)\n\}",
        js, re.DOTALL,
    )
    assert match, "_diffNotificationsPanel's body must still be present verbatim"
    body = match.group(1)
    assert "notificationKey" not in body, (
        "_diffNotificationsPanel must not be rewired to call notificationKey "
        "— it must be left completely untouched"
    )


# === Structural: static/app/main.js ========================================

_MAIN_JS_TOAST_BLOCK_AFTER = """  // Toast handling: pick new notifications and push toasts
  if (!hasSeededNotifications) {
    // Seed seen map without pushing toasts
    for (const plan of plans) {
      const rec = plan.latest_notification;
      if (rec) {
        lastSeenNotificationByPlan.set(plan.name, notificationKey(rec));
      }
    }
    hasSeededNotifications = true;
  } else {
    const newNotifs = pickNewNotifications(plans, lastSeenNotificationByPlan);
    for (const { plan, record } of newNotifs) {
      pushToast({ severity: record.severity, planName: plan.name, storyKey: record.story_key, message: record.message });
      lastSeenNotificationByPlan.set(plan.name, notificationKey(record));
    }
  }"""


def test_main_js_toast_block_rewritten_exactly():
    js = _main_js_source()
    assert _MAIN_JS_TOAST_BLOCK_AFTER in js, (
        "expected the exact prescribed toast-handling block (using "
        "notificationKey for both the seed path and the push-toast path) to "
        "be present verbatim in static/app/main.js"
    )


def test_main_js_old_dedup_key_checks_removed():
    js = _main_js_source()
    assert "rec.dedup_key != null" not in js, (
        "the old null-dedup_key seeding gate must be removed from main.js"
    )
    assert "lastSeenNotificationByPlan.set(plan.name, rec.dedup_key);" not in js, (
        "the old dedup_key-only seed assignment must be removed"
    )
    assert "lastSeenNotificationByPlan.set(plan.name, record.dedup_key);" not in js, (
        "the old dedup_key-only toast-tracking assignment must be removed"
    )


def test_main_js_imports_notification_key_in_existing_import_statement():
    js = _main_js_source()
    match = re.search(
        r'import\s*\{([^}]*)\}\s*from\s*"\./render/notifications\.js";',
        js,
    )
    assert match, (
        "main.js must import from './render/notifications.js' via an "
        "import statement"
    )
    imported_names = match.group(1)
    assert "notificationKey" in imported_names, (
        "notificationKey must be added to main.js's existing import list "
        "from './render/notifications.js'"
    )
    for name in [
        "initNotifications", "NOTIF_SEVERITY_COLOR", "pickNewNotifications",
        "pushToast", "setNotifSeverityFilter", "renderNotifications",
        "_diffNotificationsPanel", "filterNotifications",
    ]:
        assert name in imported_names, (
            f"expected '{name}' to remain in main.js's import list from "
            f"'./render/notifications.js', got: {imported_names}"
        )


def test_main_js_has_exactly_one_import_statement_for_notifications_module():
    """main.js must not gain a second `import { ... } from
    './render/notifications.js'` statement — notificationKey must be added
    to the ONE existing import line. (The trailing `export { ... } from
    "./render/notifications.js";` re-export block is a separate statement
    kind and is not counted here.)"""
    js = _main_js_source()
    import_matches = re.findall(
        r'import\s*\{[^}]*\}\s*from\s*"\./render/notifications\.js";',
        js,
    )
    assert len(import_matches) == 1, (
        "expected exactly one import statement pulling from "
        f"'./render/notifications.js', found {len(import_matches)}"
    )
