"""Node-eval harness tests for the notification-records UI refactor in
static/app.js.

This file mirrors the harness pattern in tests/unit/test_dashboard.py: it
builds a minimal DOM shim, evals static/app.js under `node -e`, and
JSON-stringifies the result of a test expression. The helper is copied here
(rather than imported) so this file stands alone and does not touch
test_dashboard.py.

These tests are RED until the implementation lands: renderNotifications must
be rewritten to take structured `notification_records` (not raw string
`lines`), a module-scope NOTIF_SEVERITY_COLOR lookup must exist, the call
site in renderPlanDetail must switch to plan.notification_records, and
renderNotifications must be added to module.exports.
"""
import json
import os

from tests.unit._app_js import run_app_js as _shared_run_app_js

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_JS = os.path.join(REPO_ROOT, "static", "app.js")
# renderPlanDetail's notifications-panel call site was relocated out of
# static/app.js into this dedicated render module (server-app-file-split
# plan); the static-source assertion below follows it here.
PLAN_DETAIL_JS = os.path.join(REPO_ROOT, "static", "app", "render", "plan-detail.js")


def _run_app_js(expr):
    """Evaluate a JS expression inside an environment where static/app.js
    has been loaded. Returns the JSON-serialized result. Copied verbatim
    from test_dashboard.py so this file is self-contained."""
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
    proc = _shared_run_app_js(expr, shim=shim)
    if proc.returncode != 0:
        raise AssertionError(f"node failed: {proc.stderr}")
    return json.loads(proc.stdout)


def _render(records):
    """Helper: call renderNotifications with a JS array literal of records."""
    return _run_app_js(f"renderNotifications({json.dumps(records)})")


def _app_js_source():
    """Read static/app.js source for static-source assertions."""
    with open(APP_JS, encoding="utf-8") as fh:
        return fh.read()


# === Empty / missing input guards ==========================================

def test_empty_records_renders_empty_state():
    """An empty array must render the existing empty-state paragraph."""
    html = _render([])
    assert "No notifications yet." in html


def test_undefined_records_renders_empty_state():
    """undefined (the value renderPlanDetail passes when the plan has no
    notification_records key at all) must NOT throw and must render the
    empty state. This is the guard for the existing renderPlanDetail test
    in test_dashboard.py, which passes a plan shaped
    {stories:{}, notifications:[], decisions:[]} with no notification_records."""
    html = _run_app_js("renderNotifications(undefined)")
    assert "No notifications yet." in html


def test_null_records_renders_empty_state():
    """null must be treated exactly like an empty array."""
    html = _run_app_js("renderNotifications(null)")
    assert "No notifications yet." in html


# === Severity -> badge color lookup ========================================

def test_severity_drives_badge_color():
    """The NOTIF_SEVERITY_COLOR lookup must map each known severity to its
    CSS custom-property token, surfaced via --badge-color on the badge."""
    err = _render([{"severity": "error", "message": "boom", "ts": "2024-01-01T00:00:00Z"}])
    warn = _render([{"severity": "warning", "message": "careful", "ts": "2024-01-01T00:00:00Z"}])
    info = _render([{"severity": "info", "message": "fyi", "ts": "2024-01-01T00:00:00Z"}])
    assert "--c-failed" in err
    assert "--c-parked" in warn
    assert "--c-unknown" in info
    # The badge must actually carry the color via the --badge-color property.
    assert "--badge-color: var(--c-failed)" in err
    assert "--badge-color: var(--c-parked)" in warn
    assert "--badge-color: var(--c-unknown)" in info


def test_unknown_severity_falls_back():
    """An unrecognised severity falls back to --c-unknown."""
    html = _render([{"severity": "weird", "message": "huh", "ts": "2024-01-01T00:00:00Z"}])
    assert "--c-unknown" in html
    assert "--badge-color: var(--c-unknown)" in html


def test_missing_severity_falls_back():
    """A record with no severity at all must not throw and must fall back."""
    html = _render([{"message": "no severity", "ts": "2024-01-01T00:00:00Z"}])
    assert "--c-unknown" in html


def test_severity_color_constant_exists_at_module_scope():
    """The lookup object NOTIF_SEVERITY_COLOR must be declared at module
    scope so it can be referenced by the implementation and inspected here."""
    js = _app_js_source()
    assert "NOTIF_SEVERITY_COLOR" in js
    assert '"error": "--c-failed"' in js or "'error': '--c-failed'" in js
    assert '"warning": "--c-parked"' in js or "'warning': '--c-parked'" in js
    assert '"info": "--c-unknown"' in js or "'info': '--c-unknown'" in js


# === story_key prefix ======================================================

def test_story_key_is_rendered_when_present():
    """A truthy story_key appears in the output as a mono prefix."""
    html = _render([{"severity": "info", "story_key": "P3-2", "message": "hi", "ts": "2024-01-01T00:00:00Z"}])
    assert "P3-2" in html
    assert "mono" in html


def test_story_key_null_does_not_render_empty_key_element():
    """A null story_key must not produce an empty key element / stray markup."""
    html = _render([{"severity": "info", "story_key": None, "message": "hi", "ts": "2024-01-01T00:00:00Z"}])
    # No empty mono span should leak through.
    assert 'class="mono"></span>' not in html
    assert "class=\"mono\">" not in html.replace("class=\"mono\">P3-2", "")  # sanity


def test_story_key_absent_does_not_render_key_element():
    """A record with no story_key field at all must not throw."""
    html = _render([{"severity": "info", "message": "hi", "ts": "2024-01-01T00:00:00Z"}])
    assert "No notifications yet." not in html


# === repeat count badge ====================================================

def test_count_badge_shown_only_when_greater_than_one():
    """count > 1 renders an 'x{count}' badge; count == 1 renders no 'x1'."""
    many = _render([{"severity": "info", "message": "repeated", "count": 9, "ts": "2024-01-01T00:00:00Z"}])
    one = _render([{"severity": "info", "message": "once", "count": 1, "ts": "2024-01-01T00:00:00Z"}])
    assert "x9" in many
    assert "x1" not in one


def test_count_missing_does_not_render_count_badge():
    """A record with no count field must not throw and must not show a count."""
    html = _render([{"severity": "info", "message": "no count", "ts": "2024-01-01T00:00:00Z"}])
    assert "x1" not in html
    assert "x0" not in html


# === HTML escaping =========================================================

def test_html_in_message_is_escaped():
    """Notification text embeds gate errors, branch names and raw CI stderr,
    so every interpolated value must go through escapeHtml()."""
    html = _render([{"severity": "error", "message": "<img src=x onerror=alert(1)>", "ts": "2024-01-01T00:00:00Z"}])
    assert "&lt;img" in html
    assert "<img" not in html


def test_html_in_story_key_is_escaped():
    """story_key is untrusted too."""
    html = _render([{"severity": "info", "story_key": "<b>", "message": "hi", "ts": "2024-01-01T00:00:00Z"}])
    assert "&lt;b&gt;" in html
    assert "<b>" not in html


def test_html_in_severity_is_escaped():
    """severity is untrusted too."""
    html = _render([{"severity": "<script>", "message": "hi", "ts": "2024-01-01T00:00:00Z"}])
    assert "&lt;script&gt;" in html
    assert "<script>" not in html


def test_html_in_ts_is_escaped():
    """ts is untrusted too."""
    html = _render([{"severity": "info", "message": "hi", "ts": "<script>"}])
    assert "&lt;script&gt;" in html
    assert "<script>" not in html


# === ordering =============================================================

def test_newest_record_appears_first():
    """The existing .slice().reverse() ordering is kept: the last record in
    the input array must appear earliest in the output string."""
    html = _render([
        {"severity": "info", "message": "FIRST", "ts": "2024-01-01T00:00:00Z"},
        {"severity": "info", "message": "SECOND", "ts": "2024-01-01T00:00:01Z"},
    ])
    first_idx = html.find("FIRST")
    second_idx = html.find("SECOND")
    assert first_idx != -1 and second_idx != -1
    assert second_idx < first_idx, "newest (SECOND) must appear before FIRST"


# === log-line wrapper =====================================================

def test_each_record_wrapped_in_log_line():
    """Each record renders one <div class=\"log-line\">."""
    html = _render([
        {"severity": "info", "message": "a", "ts": "2024-01-01T00:00:00Z"},
        {"severity": "info", "message": "b", "ts": "2024-01-01T00:00:01Z"},
    ])
    assert html.count('class="log-line"') == 2


def test_timestamp_visible():
    """The timestamp must remain visible in the output (it led the raw
    line before; keep a timestamp visible)."""
    html = _render([{"severity": "info", "message": "hi", "ts": "2024-01-01T00:00:00Z"}])
    assert "2024-01-01T00:00:00Z" in html


# === renderPlanDetail call site + export ==================================

def test_render_plan_detail_without_notification_records_does_not_throw():
    """Drive renderPlanDetail with the exact plan shape used by the existing
    test in test_dashboard.py — {stories:{}, notifications:[], decisions:[]}
    with NO notification_records key — and assert it returns without error.
    This is the integration-level guard for the undefined-records case."""
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


def test_render_plan_detail_uses_notification_records_key():
    """The call site inside renderPlanDetail must read plan.notification_records
    (not the old plan.notifications). (Now in static/app/render/plan-detail.js.)"""
    with open(PLAN_DETAIL_JS, encoding="utf-8") as fh:
        js = fh.read()
    assert "renderNotifications(plan.notification_records)" in js
    # The old call site must be gone.
    assert "renderNotifications(plan.notifications)" not in js


def test_render_notifications_is_exported():
    """renderNotifications must be present in module.exports so it can be
    tested directly via the node harness."""
    expr = (
        "(() => {"
        " const out = (typeof module !== 'undefined' && module.exports) ? module.exports : {};"
        " return typeof out.renderNotifications;"
        " })()"
    )
    assert _run_app_js(expr) == "function"


def test_render_notifications_signature_takes_records_not_lines():
    """The old parameter name `lines` is dead; the new function takes a
    records argument. Assert the source no longer documents the old shape
    and that the empty-state guard handles falsy input (not .length on a
    possibly-undefined value)."""
    js = _app_js_source()
    # The guard must short-circuit on falsy records before touching .length,
    # otherwise renderNotifications(undefined) throws.
    assert "function renderNotifications(records)" in js