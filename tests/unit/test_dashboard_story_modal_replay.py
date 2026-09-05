"""Tests for the Replay section inside the story modal
(static/app/render/story-modal.js). Exercises the pure helpers
`renderReplayEvent`/`renderReplay`, the async `loadStoryReplay` fetch
wiring, and the `_renderStoryModalBody` markup anchor, by shelling out to
Node in a subprocess.

Mirrors tests/unit/test_dashboard_story_modal_notifications.py's
`_run_app_js` harness (copied below rather than imported, per the pipeline
story schema's guidance to keep new test files self-contained), but points
Node's dynamic `import()` at static/app/render/story-modal.js DIRECTLY
instead of static/app.js. This is deliberate: static/app.js re-exports
only a curated symbol list from story-modal.js via static/app/main.js, and
this story's brief forbids touching main.js, so loadStoryReplay/renderReplay
would not be reachable through the app.js chain without that edit.
Importing the module directly sidesteps that while keeping the same
subprocess/JSON-serialization test style already established for this
module.
"""
import json
import os

from tests.unit._app_js import run_app_js as _shared_run_app_js

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STORY_MODAL_JS = os.path.join(REPO_ROOT, "static", "app", "render", "story-modal.js")


def _run_story_modal_js(expr):
    """Evaluate a JS expression inside an environment where
    static/app/render/story-modal.js has been loaded directly as an ES
    module (so its named exports are available on globalThis). Returns the
    JSON-serialized result.

    story-modal.js transitively imports state.js, which assigns onto
    `window` at module top-level, so `window`/`document`/`fetch` must be
    stubbed before the import — same shim shape as
    test_dashboard_story_modal_notifications.py's harness."""
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
        globalThis.fetch = () => new Promise(() => {}); // never resolves by default
        process.on("unhandledRejection", () => {});
        globalThis.setInterval = () => 0;
        globalThis.setTimeout = (fn, _ms) => { if (typeof fn === "function") { /* dropped */ } return 0; };
    """
    proc = _shared_run_app_js(expr, app_js=STORY_MODAL_JS, shim=shim)
    if proc.returncode != 0:
        raise AssertionError(f"node failed: {proc.stderr}")
    return json.loads(proc.stdout)


# ---------- _renderStoryModalBody markup anchor ----------

def test_render_story_modal_body_contains_replay_slot_with_empty_state():
    """The Replay slot must be anchored between the Journal and
    Notifications slots and start with the empty-state placeholder so the
    modal opens immediately before the async fetch resolves."""
    expr = (
        "(() => {"
        " const noop = () => {};"
        " const modalEl = {"
        "   innerHTML: '', dataset: {},"
        "   classList: { add: noop, remove: noop, toggle: noop, contains: () => false },"
        "   querySelector: () => null, addEventListener: noop,"
        " };"
        " const bodyEl = {"
        "   innerHTML: '', dataset: {},"
        "   classList: { add: noop, remove: noop, toggle: noop, contains: () => false },"
        "   querySelector: () => null, addEventListener: noop,"
        " };"
        " globalThis.document.getElementById = (id) => {"
        "   if (id === 'story-modal') return modalEl;"
        "   if (id === 'story-modal-body') return bodyEl;"
        "   return null;"
        " };"
        " globalThis.fetch = () => new Promise(() => {});" # never resolves
        " initStoryModal({ backendErrorEl: () => null, notifSeverityColor: {} });"
        " _renderStoryModalBody('planX', {summary: 's'}, 'K1', []);"
        " return bodyEl.innerHTML;"
        " })()"
    )
    body_html = _run_story_modal_js(expr)
    assert "data-replay-slot" in body_html, body_html
    assert "data-replay-empty" in body_html, body_html
    assert "No replay data yet." in body_html, body_html
    journal_idx = body_html.index("data-journal-slot")
    replay_idx = body_html.index("data-replay-slot")
    notif_idx = body_html.index("data-notifications-slot")
    assert journal_idx < replay_idx < notif_idx, body_html


# ---------- renderReplayEvent / renderReplay ----------

def test_render_replay_event_shows_source_badge_ts_and_escaped_message():
    event = {"ts": "2025-01-01T10:00:00Z", "source": "journal", "message": "<b>hi</b>"}
    html = _run_story_modal_js(f"renderReplayEvent({json.dumps(event)})")
    assert "journal" in html, html
    assert "2025-01-01T10:00:00Z" in html, html
    assert "&lt;b&gt;hi&lt;/b&gt;" in html, html
    assert "<b>hi</b>" not in html, html


def test_render_replay_event_null_ts_renders_no_timestamp_placeholder():
    event = {"ts": None, "source": "agent.log", "message": "line"}
    html = _run_story_modal_js(f"renderReplayEvent({json.dumps(event)})")
    assert "(no timestamp)" in html, html


def test_render_replay_event_null_element_does_not_throw():
    html = _run_story_modal_js("renderReplayEvent(null)")
    assert html == "", html


def test_render_replay_empty_state_when_unavailable():
    html = _run_story_modal_js(
        'renderReplay({available: false, events: [], sources: {"journal": false, "agent.log": false, "review.log": false}})'
    )
    assert "No replay data yet." in html, html
    assert "data-replay-empty" in html, html


def test_render_replay_empty_state_when_events_array_empty():
    html = _run_story_modal_js('renderReplay({available: true, events: []})')
    assert "No replay data yet." in html, html


def test_render_replay_renders_ordered_list_for_available_events():
    data = {
        "available": True,
        "events": [
            {"ts": None, "source": "journal", "message": "first"},
            {"ts": "2025-01-01T09:00:00Z", "source": "review.log", "message": "second"},
        ],
    }
    html = _run_story_modal_js(f"renderReplay({json.dumps(data)})")
    assert "timeline" in html, html
    first_idx = html.index("first")
    second_idx = html.index("second")
    assert first_idx < second_idx, html


def test_render_replay_event_huge_message_survives_without_throwing():
    """A pathologically long message must not crash rendering; the CSS
    (pre.mono, reused from the Journal/Log sections) handles wrapping."""
    huge = "x" * 50000
    event = {"ts": "2025-01-01T00:00:00Z", "source": "agent.log", "message": huge}
    html = _run_story_modal_js(f"renderReplayEvent({json.dumps(event)})")
    assert huge in html, "huge message content should be preserved verbatim"
    assert "mono" in html, html


# ---------- loadStoryReplay ----------

def test_load_story_replay_renders_event_into_replay_slot_on_success():
    expr = (
        "(async () => {"
        " const modalEl = { dataset: { plan: 'planX', story: 'K1' } };"
        " const slotEl = { innerHTML: 'ORIGINAL' };"
        " const container = { querySelector: (sel) => sel === '[data-replay-slot]' ? slotEl : null };"
        " globalThis.document.getElementById = (id) => id === 'story-modal' ? modalEl : null;"
        " globalThis.fetch = () => Promise.resolve({"
        "   ok: true,"
        "   json: async () => ({"
        "     available: true,"
        "     events: [{ ts: '2025-01-01T00:00:00Z', source: 'journal', message: 'hello <b>' }],"
        "     sources: { journal: true, 'agent.log': false, 'review.log': false },"
        "   }),"
        " });"
        " await loadStoryReplay('planX', 'K1', container);"
        " return slotEl.innerHTML;"
        " })()"
    )
    html = _run_story_modal_js(expr)
    assert "data-replay-list" in html, html
    assert "&lt;b&gt;" in html, html
    assert "journal" in html, html
    assert "2025-01-01T00:00:00Z" in html, html


def test_load_story_replay_fetch_url_targets_replay_endpoint():
    expr = (
        "(async () => {"
        " const modalEl = { dataset: { plan: 'planX', story: 'K1' } };"
        " const slotEl = { innerHTML: 'ORIGINAL' };"
        " const container = { querySelector: () => slotEl };"
        " globalThis.document.getElementById = (id) => id === 'story-modal' ? modalEl : null;"
        " let capturedUrl = null;"
        " globalThis.fetch = (url) => {"
        "   capturedUrl = url;"
        "   return Promise.resolve({ ok: true, json: async () => ({ available: false, events: [] }) });"
        " };"
        " await loadStoryReplay('planX', 'K1', container);"
        " return capturedUrl;"
        " })()"
    )
    url = _run_story_modal_js(expr)
    assert url == "/api/plans/planX/stories/K1/replay", url


def test_load_story_replay_leaves_empty_state_on_non_200_response():
    expr = (
        "(async () => {"
        " const modalEl = { dataset: { plan: 'planX', story: 'K1' } };"
        " const slotEl = { innerHTML: 'ORIGINAL' };"
        " const container = { querySelector: () => slotEl };"
        " globalThis.document.getElementById = (id) => id === 'story-modal' ? modalEl : null;"
        " globalThis.fetch = () => Promise.resolve({ ok: false, status: 404, json: async () => ({}) });"
        " await loadStoryReplay('planX', 'K1', container);"
        " return slotEl.innerHTML;"
        " })()"
    )
    html = _run_story_modal_js(expr)
    assert "No replay data yet." in html, html


def test_load_story_replay_leaves_empty_state_on_network_error_and_does_not_throw():
    expr = (
        "(async () => {"
        " const modalEl = { dataset: { plan: 'planX', story: 'K1' } };"
        " const slotEl = { innerHTML: 'ORIGINAL' };"
        " const container = { querySelector: () => slotEl };"
        " globalThis.document.getElementById = (id) => id === 'story-modal' ? modalEl : null;"
        " globalThis.fetch = () => Promise.reject(new Error('network down'));"
        " await loadStoryReplay('planX', 'K1', container);"
        " return slotEl.innerHTML;"
        " })()"
    )
    html = _run_story_modal_js(expr)
    assert "No replay data yet." in html, html


def test_load_story_replay_available_false_renders_empty_state():
    expr = (
        "(async () => {"
        " const modalEl = { dataset: { plan: 'planX', story: 'K1' } };"
        " const slotEl = { innerHTML: 'ORIGINAL' };"
        " const container = { querySelector: () => slotEl };"
        " globalThis.document.getElementById = (id) => id === 'story-modal' ? modalEl : null;"
        " globalThis.fetch = () => Promise.resolve({"
        "   ok: true,"
        "   json: async () => ({"
        "     available: false, events: [],"
        "     sources: { journal: false, 'agent.log': false, 'review.log': false },"
        "   }),"
        " });"
        " await loadStoryReplay('planX', 'K1', container);"
        " return slotEl.innerHTML;"
        " })()"
    )
    html = _run_story_modal_js(expr)
    assert "No replay data yet." in html, html


def test_load_story_replay_drops_stale_response_after_user_opens_different_story():
    """Mirrors loadStoryLog's dataset guard: a slow fetch that resolves
    after the user opened a different story (modal.dataset.story changed)
    must not write into the slot at all."""
    expr = (
        "(async () => {"
        " const modalEl = { dataset: { plan: 'planX', story: 'K1' } };"
        " const slotEl = { innerHTML: 'ORIGINAL' };"
        " const container = { querySelector: () => slotEl };"
        " globalThis.document.getElementById = (id) => id === 'story-modal' ? modalEl : null;"
        " globalThis.fetch = () => Promise.resolve({"
        "   ok: true,"
        "   json: async () => ({"
        "     available: true,"
        "     events: [{ ts: null, source: 'journal', message: 'late' }],"
        "   }),"
        " });"
        " const p = loadStoryReplay('planX', 'K1', container);"
        " modalEl.dataset.story = 'K2';" # user opened a different story mid-flight
        " await p;"
        " return slotEl.innerHTML;"
        " })()"
    )
    html = _run_story_modal_js(expr)
    assert html == "ORIGINAL", html


def test_load_story_replay_missing_plan_or_key_is_a_noop():
    expr = (
        "(async () => {"
        " const slotEl = { innerHTML: 'ORIGINAL' };"
        " const container = { querySelector: () => slotEl };"
        " globalThis.fetch = () => { throw new Error('should not be called'); };"
        " await loadStoryReplay('', 'K1', container);"
        " await loadStoryReplay('planX', '', container);"
        " await loadStoryReplay('planX', 'K1', null);"
        " return slotEl.innerHTML;"
        " })()"
    )
    html = _run_story_modal_js(expr)
    assert html == "ORIGINAL", html
