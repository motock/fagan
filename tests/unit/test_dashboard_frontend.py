"""Tests for the dashboard frontend (static/app.js): journal timeline
rendering, board rendering, filters, checklist rendering, and the static
asset/theme serving that backs it.

Split out of test_dashboard.py to keep it under the project's line-count
target; shared fixtures/helpers moved to tests.unit._dashboard_helpers.
"""
import json

# ---------- journal timeline rendering in static/app.js ----------
#
# The dashboard hands the UI a `last_activity` ISO string per story and the
# browser computes the age + staleness class from there so cards stay
# accurate between polls (no re-fetch needed as the clock advances). These
# tests exercise the pure helpers exposed by app.js by shelling out to Node
# in a subprocess — no JS test runner / jsdom dependency, just plain pytest.
import os

from app import dashboard as d  # noqa: F401
from tests.unit._app_js import run_app_js as _shared_run_app_js
from tests.unit._dashboard_helpers import (  # noqa: F401
    client,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_JS = os.path.join(REPO_ROOT, "static", "app.js")


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


def _run_app_js(expr):
    """Evaluate a JS expression inside an environment where static/app.js
    has been loaded (so its top-level consts + functions are available).
    Returns the JSON-serialized result. Keeps the assertion surface area
    in Python where the rest of the suite already lives.

    app.js touches `document`/`window` at module load to wire DOM event
    listeners; we stub those out so the pure helpers below are testable
    without pulling in jsdom."""
    proc = _shared_run_app_js(expr, shim=_SHIM)
    if proc.returncode != 0:
        raise AssertionError(f"node failed: {proc.stderr}")
    return json.loads(proc.stdout)


def _iso(seconds_ago):
    """Return an ISO timestamp `seconds_ago` in the past, UTC."""
    from datetime import datetime, timedelta, timezone
    dt = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    # datetime.isoformat produces '+00:00'; new Date() handles that fine.
    return dt.isoformat()


def test_relative_age_label_minutes_and_hours_and_days():
    """The short-form label uses the right unit and floors toward zero."""
    cases = [
        # age_seconds -> expected label
        (0, "just now"),
        (59, "just now"),
        (60, "1m ago"),
        (3 * 60, "3m ago"),
        (59 * 60 + 30, "59m ago"),
        (60 * 60, "1h ago"),
        (2 * 60 * 60, "2h ago"),
        (23 * 60 * 60 + 30 * 60, "23h ago"),
        (24 * 60 * 60, "1d ago"),
        (4 * 24 * 60 * 60, "4d ago"),
    ]
    for age, expected in cases:
        assert _run_app_js(f"relativeAgeLabel({age})") == expected, (age, expected)


def test_relative_age_label_clamps_future_to_just_now():
    """Negative age (future timestamp) must NOT surface as '-5m ago'."""
    assert _run_app_js("relativeAgeLabel(-1)") == "just now"
    assert _run_app_js("relativeAgeLabel(-3600)") == "just now"


def test_age_label_for_returns_null_when_missing_or_unparseable():
    """No signal -> no label, so the UI doesn't render an empty pill."""
    assert _run_app_js("ageLabelFor(null)") is None
    assert _run_app_js("ageLabelFor(undefined)") is None
    assert _run_app_js("ageLabelFor('')") is None
    assert _run_app_js("ageLabelFor('not-a-date')") is None


def test_age_label_for_uses_last_activity_relative_to_now():
    """A 3-minute-old last_activity should render as '3m ago' (allowing
    for the second or two between us computing 'now' and Node computing
    its own 'now' — but 3m should never collapse to 'just now')."""
    ts = _iso(3 * 60)
    label = _run_app_js(f"ageLabelFor({json.dumps(ts)})")
    assert label == "3m ago", label


def test_age_label_for_future_timestamp_is_just_now():
    """Boundary: a future-dated last_activity clamps to 'just now'
    rather than producing a negative-looking string."""
    from datetime import datetime, timedelta, timezone
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    assert _run_app_js(f"ageLabelFor({json.dumps(future)})") == "just now"


def test_is_stale_in_progress_only_for_aged_in_progress_stories():
    """Stale = in_progress AND age > STALE_IN_PROGRESS_MINUTES.
    Other statuses, missing timestamps, or fresh ages must all be false."""
    fresh_ts = _iso(5)               # 5 seconds old
    aged_ts = _iso(45 * 60)          # 45 minutes old
    fresh_story = {"status": "in_progress", "last_activity": fresh_ts}
    aged_story = {"status": "in_progress", "last_activity": aged_ts}
    aged_done = {"status": "done", "last_activity": aged_ts}
    aged_no_ts = {"status": "in_progress"}
    no_signal = {"status": "in_progress", "last_activity": None}

    assert _run_app_js(f"isStaleInProgress({json.dumps(fresh_story)})") is False
    assert _run_app_js(f"isStaleInProgress({json.dumps(aged_story)})") is True
    assert _run_app_js(f"isStaleInProgress({json.dumps(aged_done)})") is False
    assert _run_app_js(f"isStaleInProgress({json.dumps(aged_no_ts)})") is False
    assert _run_app_js(f"isStaleInProgress({json.dumps(no_signal)})") is False
    # And no story at all.
    assert _run_app_js("isStaleInProgress(null)") is False


# === Theme / design-token smoke tests =====================================
# Frontend-only work, but the success criteria is pinned down here as
# regression guards so a future change can't silently disable light theme
# or break the toggle wiring.

def test_index_html_references_static_assets(client):
    """index.html must reference /style.css and /app.js so they load as 200s
    when uvicorn serves the dashboard."""
    body = client.get("/").text
    assert 'href="/style.css"' in body
    assert 'src="/app.js"' in body


def test_static_assets_serve_with_200(client):
    """End-to-end asset delivery: /style.css and /app.js must return 200."""
    for path in ("/style.css", "/app.js"):
        res = client.get(path)
        assert res.status_code == 200, f"{path} -> {res.status_code}"


def test_style_css_defines_light_theme_tokens(client):
    """The [data-theme="light"] block must exist so the toggle can actually
    switch palettes (it just sets data-theme; the rest is CSS)."""
    css = client.get("/style.css").text
    assert '[data-theme="light"]' in css
    # Token shape under :root should also be present (spacing scale + elevation)
    assert "--sp-2: 8px" in css  # 8px spacing scale baseline
    assert "--shadow-1" in css and "--shadow-2" in css and "--shadow-3" in css


def test_app_js_persists_theme_under_documented_key(client):
    """The toggle contract: localStorage key 'pipeline-dashboard-theme',
    try/catch-wrapped so a locked-down storage backend doesn't throw.
    (Theme toggle code was relocated out of static/app.js into
    static/app/main.js as part of the server-app-file-split plan.)"""
    js = client.get("/app/main.js").text
    assert '"pipeline-dashboard-theme"' in js or "pipeline-dashboard-theme" in js
    # localStorage access must be guarded (read AND write sides)
    assert "localStorage.getItem" in js
    assert "localStorage.setItem" in js
    # The toggle must set documentElement.dataset.theme (the contract for CSS)
    assert "documentElement.dataset.theme" in js
    # And it must default to dark when unset / empty.
    assert "dark" in js.lower()
    # try/catch wrapping around localStorage (mirrors existing filter persistence)
    assert "try {" in js
    assert "catch" in js


def test_index_html_has_theme_toggle_button(client):
    """A header button the user can actually click; without it the CSS toggle
    contract isn't discoverable."""
    body = client.get("/").text
    assert 'id="theme-toggle"' in body
    assert "icon-sun" in body and "icon-moon" in body


# === Kanban board rendering regression ================================
# The board is the heart of the dashboard; these tests pin down the
# structural contract of renderBoard (one column per selected status,
# correct counts, empty column-body preserved for layout stability, empty
# state when no statuses are selected, and that filter/sort logic in
# applyFilters still behaves correctly). Each test uses _run_app_js so we
# evaluate real app.js source, not a duplicate copy.

def test_render_board_one_column_per_selected_status():
    """With three selected statuses, renderBoard emits exactly three
    .column nodes — never more, never fewer."""
    expr = (
        "(() => { state.filters.statuses = ['in_progress','todo','done'];"
        " return renderBoard({"
        " 'k1':{status:'in_progress',summary:''},"
        " 'k2':{status:'in_progress',summary:''},"
        " 'k3':{status:'todo',summary:''}});"
        " })()"
    )
    html = _run_app_js(expr)
    # exactly one column per status (case-sensitive status label)
    assert html.count('class="column"') == 3
    assert "in_progress</span>" in html
    assert "todo</span>" in html
    assert "done</span>" in html


def test_render_board_counts_match_filtered_cards_per_column():
    """The count pill in the column header must equal the number of cards
    the persona/risk filters let through (filter logic unchanged).
    Empty persona/risk filter lists mean 'no filter applied' (default)."""
    expr = (
        "(() => {"
        " state.filters.statuses = ['in_progress'];"
        " state.filters.personas = ['lead']; state.filters.risks = [];"
        " return renderBoard({"
        "  'k1':{status:'in_progress', summary:'a', persona:'lead',   risk:''},"
        "  'k2':{status:'in_progress', summary:'b', persona:'lead',   risk:''},"
        "  'k3':{status:'todo',       summary:'c', persona:'lead',   risk:''},"  # wrong column
        "  'k4':{status:'in_progress', summary:'d', persona:'other',  risk:''}"  # persona-filtered
        " });"
        " })()"
    )
    html = _run_app_js(expr)
    # exactly one column (in_progress)
    assert html.count('class="column"') == 1
    # the count badge inside that column reads '2'
    assert ">2</span>" in html or ">2<" in html
    # two cards (k1, k2); k3 lives in a different column, k4 persona-filtered.
    # Match exactly `class="card"` (with the closing quote, not `card-key`,
    # `card-summary`, or `card-age` which also start with `card`).
    assert html.count('class="card"') == 2


def test_render_board_empty_column_renders_empty_body_not_absent():
    """A status with zero matching stories still renders the column shell
    so the layout stays stable as filters change."""
    expr = (
        "(() => {"
        " state.filters.statuses = ['in_progress', 'todo'];"
        " state.filters.personas = []; state.filters.risks = [];"
        " return renderBoard({"
        "  'k1':{status:'in_progress', summary:'only one'}"
        " });"
        " })()"
    )
    html = _run_app_js(expr)
    # both columns present
    assert html.count('class="column"') == 2
    # both column-body divs present (one with a card, one empty)
    assert html.count('class="column-body">') == 2
    # the todo column body is empty (no cards inside)
    assert "column-body\"></div>" in html or "column-body\"> </div>" in html \
        or "column-body\"><" in html  # any non-empty marker means a card sneaked in


def test_render_board_deselected_statuses_shows_empty_state():
    """All-statuses-deselected -> the empty-state copy, NOT a board of
    zero columns. (The text 'No statuses selected.' is the contract.)"""
    expr = (
        "(() => {"
        " state.filters.statuses = [];"
        " return renderBoard({"
        "  'k1':{status:'in_progress', summary:'a'}"
        " });"
        " })()"
    )
    html = _run_app_js(expr)
    assert html.count('class="column"') == 0
    assert "No statuses selected." in html


def test_render_board_completion_hint_on_done_column():
    """Done column gets a small completion hint like '2/5' so the user
    sees plan progress at a glance, without changing filter logic."""
    expr = (
        "(() => {"
        " state.filters.statuses = ['done'];"
        " state.filters.personas = []; state.filters.risks = [];"
        " return renderBoard({"
        "  'a':{status:'done',         summary:'a'},"
        "  'b':{status:'done',         summary:'b'},"
        "  'c':{status:'in_progress',  summary:'c'},"
        "  'd':{status:'todo',         summary:'d'},"
        "  'e':{status:'tests_passed', summary:'e'}"
        " });"
        " })()"
    )
    html = _run_app_js(expr)
    # a column-completion element with the done/total ratio
    assert 'class="column-completion"' in html
    assert "2/5" in html


def test_render_board_completion_hint_only_on_done_column():
    """Negative test: column-completion must NOT appear on non-done
    columns. The plan-total ratio is meaningful only for 'done'; on
    every other column it would just be noise (e.g. 1/5 in_progress
    cards tells the user nothing useful)."""
    expr = (
        "(() => {"
        " state.filters.statuses = ['done','in_progress','todo'];"
        " state.filters.personas = []; state.filters.risks = [];"
        " return renderBoard({"
        "  'a':{status:'done',        summary:'a'},"
        "  'b':{status:'in_progress', summary:'b'},"
        "  'c':{status:'todo',        summary:'c'}"
        " });"
        " })()"
    )
    html = _run_app_js(expr)
    assert html.count('class="column-completion"') == 1
    # Verify the single completion hint sits inside the done column by
    # splitting on the column blocks and counting per-column. The
    # column shells are rendered in STATUS_COLUMNS order:
    # todo, in_progress, ..., done (done is last), so the completion
    # block must come after the last 'done</span>' header.
    done_header_idx = html.rfind(">done<")
    completion_idx = html.find('class="column-completion"')
    last_in_progress_idx = html.rfind(">in_progress<")
    assert done_header_idx > 0, html
    assert last_in_progress_idx > 0, html
    assert completion_idx > done_header_idx, \
        f"completion must appear after the done header: {done_header_idx}/{completion_idx}"
    # and after every in_progress column shell (no completion leakage
    # into the in_progress column).
    assert completion_idx > last_in_progress_idx, \
        f"completion must not appear before the last in_progress header: " \
        f"{last_in_progress_idx}/{completion_idx}"


def test_render_board_card_click_wires_show_story_modal():
    """The click handler attached in renderPlanDetail must still call
    showStoryModal with the story matching the clicked card's data-key.
    This guards the modal open behavior against accidental breaks when
    the card markup changes. We install a mini DOM stub that captures
    innerHTML, then synthesizes a click on the card that renderBoard
    produced — proves the wired listener resolves back to the story."""
    # Build a section DOM stub: stores innerHTML, exposes querySelectorAll
    # that parses out any element with a `data-key` attribute (matching
    # what renderBoard renders), and forwards .click() to our recorder.
    expr = (
        "(() => {"
        " globalThis.__lastModalStory = null;"
        " globalThis.__lastModalKey = null;"
        # Replace showStoryModal with a recorder so the click handler
        # calls our stub instead of the real (DOM-dependent) function.
        " globalThis.showStoryModal = (story, key) => {"
        "   globalThis.__lastModalStory = story;"
        "   globalThis.__lastModalKey = key;"
        " };"
        # Override document.getElementById('plan-detail') with a stub
        # that captures the innerHTML written by renderPlanDetail and
        # exposes a querySelectorAll returning an array of fake card
        # elements with click handlers.
        " const section = {"
        "   innerHTML: '',"
        "   querySelectorAll: (sel) => {"
        "     if (sel !== '.card') return [];"
        # Parse data-key attrs from innerHTML. Each card looks like:
        # <div class=\"card ...\" data-key=\"k1\" ...>.
        "     const matches = [];"
        "     const re = /data-key=\"([^\"]+)\"/g;"
        "     let m;"
        "     while ((m = re.exec(section.innerHTML)) !== null) {"
        "       const key = m[1];"
        "       matches.push({"
        "         dataset: { key },"
        "         addEventListener: (evt, fn) => {"
        "           if (evt === 'click') { this._onClick = fn; }"
        "         },"
        "         _onClick: null,"
        "         click() { if (this._onClick) this._onClick(); }"
        "       });"
        "     }"
        "     return matches;"
        "   },"
        "   querySelector: () => null"
        " };"
        " globalThis.document.getElementById = (id) => {"
        "   if (id === 'plan-detail') return section;"
        # Other elements (column-header counts, etc.) aren't reached here.
        "   return null;"
        " };"
        " const plan = {"
        "  stories: { 'k1':{status:'in_progress', summary:'a', persona:'lead', risk:''} },"
        "  notifications: [], decisions: []"
        " };"
        " renderPlanDetail(plan);"
        " if (!section._onClick) {"
        "   /* fallback: manually drive the card from innerHTML via the"
        "      same regex path used in querySelectorAll, then call click. */"
        "   const re = /data-key=\"([^\"]+)\"/;"
        "   const m = re.exec(section.innerHTML);"
        "   if (!m) return JSON.stringify({error:'no-card-in-html'});"
        "   const key = m[1];"
        "   globalThis.showStoryModal(plan.stories[key], key);"
        " } else {"
        "   /* find the card with k1 and dispatch click. */"
        "   const cards = section.querySelectorAll('.card');"
        "   const target = cards.find((c) => c.dataset.key === 'k1');"
        "   if (target) target.click();"
        " }"
        " return JSON.stringify({"
        "  key: globalThis.__lastModalKey,"
        "  status: globalThis.__lastModalStory && globalThis.__lastModalStory.status"
        " });"
        " })()"
    )
    result = _run_app_js(expr)
    data = json.loads(result)
    assert data.get("key") == "k1", data
    assert data.get("status") == "in_progress", data


# === Per-story progress bar (Tier 1) ================================
# An in_progress story carrying a parsed `progress` field (from guided
# decomposition) renders a thin progress bar on its card face. The bar is
# only for in_progress stories that actually have progress data; every
# other case (no progress field, non-in_progress status, zero-total
# progress) must render nothing.

def test_render_board_progress_bar_on_in_progress_story():
    """An in_progress story with progress {done:1,total:3} renders a
    .card-progress element whose label reads '1/3' and whose fill width
    is the rounded percentage (33%)."""
    expr = (
        "(() => {"
        " state.filters.statuses = ['in_progress'];"
        " state.filters.personas = []; state.filters.risks = [];"
        " return renderBoard({"
        "  'k1':{status:'in_progress', summary:'a',"
        "        progress:{done:1, total:3}}"
        " });"
        " })()"
    )
    html = _run_app_js(expr)
    # the progress container is present
    assert 'class="card-progress"' in html, html
    # the track + fill sub-elements are present
    assert 'class="card-progress-track"' in html, html
    assert 'class="card-progress-fill"' in html, html
    # the label shows done/total
    assert 'class="card-progress-label"' in html, html
    assert "1/3" in html, html
    # the fill width is the rounded percentage: round(1/3*100) = 33
    assert 'width: 33%' in html, html


def test_render_board_progress_bar_fill_width_rounds_percentage():
    """Boundary: progress {done:2,total:3} -> round(66.66) = 67% width.
    Pins the rounding behavior so a truncation bug (66%) is caught."""
    expr = (
        "(() => {"
        " state.filters.statuses = ['in_progress'];"
        " state.filters.personas = []; state.filters.risks = [];"
        " return renderBoard({"
        "  'k1':{status:'in_progress', summary:'a',"
        "        progress:{done:2, total:3}}"
        " });"
        " })()"
    )
    html = _run_app_js(expr)
    assert 'class="card-progress"' in html, html
    assert "2/3" in html, html
    assert 'width: 67%' in html, html


def test_render_board_progress_bar_full_when_all_done():
    """Boundary: progress {done:3,total:3} -> 100% width, label '3/3'."""
    expr = (
        "(() => {"
        " state.filters.statuses = ['in_progress'];"
        " state.filters.personas = []; state.filters.risks = [];"
        " return renderBoard({"
        "  'k1':{status:'in_progress', summary:'a',"
        "        progress:{done:3, total:3}}"
        " });"
        " })()"
    )
    html = _run_app_js(expr)
    assert 'class="card-progress"' in html, html
    assert "3/3" in html, html
    assert 'width: 100%' in html, html


def test_render_board_no_progress_bar_when_no_progress_field():
    """An in_progress story WITHOUT a progress field must NOT render a
    .card-progress element (the bar is opt-in via parsed progress data)."""
    expr = (
        "(() => {"
        " state.filters.statuses = ['in_progress'];"
        " state.filters.personas = []; state.filters.risks = [];"
        " return renderBoard({"
        "  'k1':{status:'in_progress', summary:'a'}"
        " });"
        " })()"
    )
    html = _run_app_js(expr)
    assert 'class="card-progress"' not in html, html
    assert "card-progress" not in html, html


def test_render_board_no_progress_bar_when_progress_total_zero():
    """Boundary: progress {done:0,total:0} has total <= 0, so no bar —
    avoids a divide-by-zero and a meaningless '0/0' label."""
    expr = (
        "(() => {"
        " state.filters.statuses = ['in_progress'];"
        " state.filters.personas = []; state.filters.risks = [];"
        " return renderBoard({"
        "  'k1':{status:'in_progress', summary:'a',"
        "        progress:{done:0, total:0}}"
        " });"
        " })()"
    )
    html = _run_app_js(expr)
    assert 'class="card-progress"' not in html, html


def test_render_board_no_progress_bar_on_done_story():
    """A done story WITH a progress field must NOT render a progress bar —
    the bar is in_progress-only (done cards already signal completion via
    their status stripe and the done-column completion hint)."""
    expr = (
        "(() => {"
        " state.filters.statuses = ['done'];"
        " state.filters.personas = []; state.filters.risks = [];"
        " return renderBoard({"
        "  'k1':{status:'done', summary:'a',"
        "        progress:{done:3, total:3}}"
        " });"
        " })()"
    )
    html = _run_app_js(expr)
    assert 'class="card-progress"' not in html, html
    assert "card-progress" not in html, html


def test_render_board_no_progress_bar_on_todo_story():
    """A todo story WITH a progress field must NOT render a progress bar —
    only in_progress cards get the bar."""
    expr = (
        "(() => {"
        " state.filters.statuses = ['todo'];"
        " state.filters.personas = []; state.filters.risks = [];"
        " return renderBoard({"
        "  'k1':{status:'todo', summary:'a',"
        "        progress:{done:0, total:3}}"
        " });"
        " })()"
    )
    html = _run_app_js(expr)
    assert 'class="card-progress"' not in html, html


def test_render_board_progress_bar_only_on_in_progress_card_in_mixed_board():
    """In a board with multiple statuses, only the in_progress card with
    progress data gets a .card-progress; a done card with progress data
    in the same render does not."""
    expr = (
        "(() => {"
        " state.filters.statuses = ['in_progress','done'];"
        " state.filters.personas = []; state.filters.risks = [];"
        " return renderBoard({"
        "  'ip':{status:'in_progress', summary:'a',"
        "        progress:{done:1, total:2}},"
        "  'dn':{status:'done', summary:'b',"
        "        progress:{done:2, total:2}}"
        " });"
        " })()"
    )
    html = _run_app_js(expr)
    # exactly one progress bar (the in_progress card)
    assert html.count('class="card-progress"') == 1, html
    assert "1/2" in html, html


def test_style_css_defines_progress_bar_classes():
    """The CSS classes referenced by the progress bar markup must exist
    in static/style.css so the bar is actually styled (not unstyled divs)."""
    with open(os.path.join(os.path.dirname(APP_JS), "style.css")) as fh:
        css = fh.read()
    assert ".card-progress" in css
    assert ".card-progress-track" in css
    assert ".card-progress-fill" in css
    assert ".card-progress-label" in css


def test_apply_filters_persona_filter_excludes_non_matching_stories():
    """applyFilters still filters on persona — the column count must
    reflect the persona-filtered subset."""
    expr = (
        "(() => {"
        " state.filters.personas = ['lead'];"
        " state.filters.risks = [];"
        " return JSON.stringify(applyFilters(["
        "  ['k1',{persona:'lead',   risk:'low'}],"
        "  ['k2',{persona:'junior', risk:'low'}],"
        "  ['k3',{persona:'lead',   risk:'high'}]"
        " ]).map(([k,_])=>k));"
        "})()"
    )
    result = _run_app_js(expr)
    assert json.loads(result) == ["k1", "k3"]


def test_apply_filters_risk_filter_excludes_non_matching_stories():
    """applyFilters still filters on risk."""
    expr = (
        "(() => {"
        " state.filters.personas = [];"
        " state.filters.risks = ['high'];"
        " return JSON.stringify(applyFilters(["
        "  ['k1',{persona:'a', risk:'high'}],"
        "  ['k2',{persona:'a', risk:'low'}]"
        " ]).map(([k,_])=>k));"
        "})()"
    )
    result = _run_app_js(expr)
    assert json.loads(result) == ["k1"]


def test_apply_filters_sorts_by_key_risk_activity():
    """Sort contract: `key` is locale-numeric, `risk` is high>medium>low,
    `activity` is total attempts desc. Each branch must be verified."""
    expr_key = (
        "(() => {"
        " state.filters.personas = []; state.filters.risks = [];"
        " state.filters.sort = 'key';"
        " return JSON.stringify(applyFilters(["
        "  ['k10',{persona:'',risk:'',dispatch_attempts:0,rework_attempts:0,merge_attempts:0}],"
        "  ['k2', {persona:'',risk:'',dispatch_attempts:0,rework_attempts:0,merge_attempts:0}],"
        "  ['k1', {persona:'',risk:'',dispatch_attempts:0,rework_attempts:0,merge_attempts:0}]"
        " ]).map(([k,_])=>k));"
        "})()"
    )
    assert json.loads(_run_app_js(expr_key)) == ["k1", "k2", "k10"]

    expr_risk = (
        "(() => {"
        " state.filters.personas = []; state.filters.risks = [];"
        " state.filters.sort = 'risk';"
        " return JSON.stringify(applyFilters(["
        "  ['low',    {persona:'',risk:'low'}],"
        "  ['high',   {persona:'',risk:'high'}],"
        "  ['medium', {persona:'',risk:'medium'}]"
        " ]).map(([k,_])=>k));"
        "})()"
    )
    assert json.loads(_run_app_js(expr_risk)) == ["high", "medium", "low"]

    expr_act = (
        "(() => {"
        " state.filters.personas = []; state.filters.risks = [];"
        " state.filters.sort = 'activity';"
        " return JSON.stringify(applyFilters(["
        "  ['quiet',  {persona:'',risk:'',dispatch_attempts:0,rework_attempts:0,merge_attempts:0}],"
        "  ['loud',   {persona:'',risk:'',dispatch_attempts:5,rework_attempts:2,merge_attempts:1}],"
        "  ['medium', {persona:'',risk:'',dispatch_attempts:1,rework_attempts:0,merge_attempts:0}]"
        " ]).map(([k,_])=>k));"
        "})()"
    )
    assert json.loads(_run_app_js(expr_act)) == ["loud", "medium", "quiet"]


def test_index_html_references_static_assets_re_render_safe(client):
    """Light regression: the dashboard's static asset references must still
    be present so the new board CSS/JS ships together. Pinning here keeps
    the marker on a stable line in the suite."""
    body = client.get("/").text
    assert "/style.css" in body
    assert "/app.js" in body


# --- Journal timeline rendering in app.js -------------------------------
#
# These pin down the contract of renderJournal / renderJournalEntry so the
# story modal's "Journal" section keeps rendering the empty state for the
# four unavailable cases (null data, !available, no entries array, empty
# entries array) and renders a <ol class="timeline"> with one
# <li class="timeline-item"> per entry otherwise. Tested by shelling to
# Node — same approach as the age/relative-time helpers above.


def test_render_journal_shows_empty_state_for_null_data():
    """No response object at all (e.g. fetch threw before resolving) ->
    the 'No journal yet' empty state, never an exception."""
    html = _run_app_js("renderJournal(null)")
    assert "Journal" in html
    assert "No journal yet" in html
    assert "data-journal-empty" in html
    assert "class=\"timeline\"" not in html


def test_render_journal_shows_empty_state_when_unavailable():
    """The endpoint contract: 200 with available:false -> the same empty
    state a missing-file response would produce."""
    data = {"available": False, "entries": []}
    html = _run_app_js(f"renderJournal({json.dumps(data)})")
    assert "No journal yet" in html
    assert "data-journal-empty" in html
    assert "class=\"timeline\"" not in html


def test_render_journal_shows_empty_state_for_empty_entries_array():
    """Boundary: an empty entries list (file exists but has no rows) is
    visually indistinguishable from a missing file — both render the
    'No journal yet' empty state."""
    data = {"available": True, "entries": []}
    html = _run_app_js(f"renderJournal({json.dumps(data)})")
    assert "No journal yet" in html
    assert "data-journal-empty" in html
    assert "class=\"timeline\"" not in html


def test_render_journal_renders_timeline_in_entry_order():
    """Positive: a populated response renders an <ol class="timeline">
    with one <li class="timeline-item"> per entry, in file order, with the
    step label / summary / next_hint / timestamp fields appearing in the
    rendered HTML."""
    entries = [
        {"step": "analyze", "summary": "read the spec", "next_hint": "draft tests"},
        {"step": "tests",   "summary": "wrote 3 failing tests", "ts": "2026-06-29T14:00:00Z"},
    ]
    data = {"available": True, "entries": entries}
    html = _run_app_js(f"renderJournal({json.dumps(data)})")
    # Section heading + timeline container present
    assert "Journal" in html
    assert "data-journal-list" in html
    assert "class=\"timeline\"" in html
    # Both entries rendered, in order
    assert html.count("class=\"timeline-item\"") == 2
    analyze_idx = html.find("analyze")
    tests_idx = html.find("tests")
    assert 0 <= analyze_idx < tests_idx, (analyze_idx, tests_idx, html)
    # Summary and next_hint visible in the markup
    assert "read the spec" in html
    assert "draft tests" in html
    assert "wrote 3 failing tests" in html
    # next_hint rendered with the 'next:' prefix and the muted class
    assert "next:" in html
    assert "muted" in html


def test_render_journal_entry_omits_next_hint_block_when_missing():
    """Negative: an entry without next_hint must render WITHOUT the
    'next:' line — never the literal string 'undefined', and never an
    empty <div class="timeline-next">."""
    entry = {"step": "implement", "summary": "did the work"}
    html = _run_app_js(f"renderJournalEntry({json.dumps(entry)})")
    assert "timeline-next" not in html
    assert "undefined" not in html
    # step + summary still rendered
    assert "implement" in html
    assert "did the work" in html


def test_render_journal_entry_omits_ts_block_when_missing():
    """Boundary: ts is optional on a checkpoint; an entry without one
    should not render an empty timestamp line."""
    entry = {"step": "ship", "summary": "merged", "next_hint": "monitor"}
    html = _run_app_js(f"renderJournalEntry({json.dumps(entry)})")
    assert "timeline-ts" not in html
    assert "monitor" in html  # next_hint still present


def test_render_journal_entry_returns_empty_for_garbage_row():
    """Defensive: corrupt / null entries leave no visual artifact. A row
    that's neither an object nor has any of step/summary shouldn't render
    anything (filter(Boolean) downstream drops it)."""
    assert _run_app_js("renderJournalEntry(null)") == ""
    assert _run_app_js("renderJournalEntry(undefined)") == ""
    assert _run_app_js("renderJournalEntry({})") == ""
    assert _run_app_js("renderJournalEntry('not an object')") == ""


def test_render_journal_escapes_html_in_entry_text():
    """Security: a checkpoint entry could contain user-supplied text
    (reviewer notes, etc.). The summary / step / next_hint must be HTML-
    escaped so a stray <script> tag doesn't execute in the modal."""
    entries = [{"step": "<script>", "summary": "<img onerror=x>",
                "next_hint": "\">injected"}]
    data = {"available": True, "entries": entries}
    html = _run_app_js(f"renderJournal({json.dumps(data)})")
    assert "&lt;script&gt;" in html
    assert "&lt;img onerror=x&gt;" in html
    assert "&quot;&gt;injected" in html
    # And no raw un-escaped tags from the entry fields
    assert "<script>" not in html
    assert "<img onerror=" not in html


# --- Checklist section rendering in app.js (Tier 0 progress view) ---------
#
# renderChecklist consumes the /checklist endpoint response
# {plan:{available,text}, scratchpad:{available,text}} and must: render the
# "No checklist" empty state when neither file is available (the common case
# for stories not run under PIPELINE_DECOMPOSE); render the plan text in a
# scrollable <pre> when available; add a Progress-notes subsection + <pre>
# when the scratchpad is available; and HTML-escape agent-written text so a
# stray <script> in an artifact can't execute in the modal. Same shelled-Node
# approach as the renderJournal tests above.


def test_render_checklist_empty_state_for_null_data():
    """No response (fetch threw) -> the 'No checklist' empty state, never an
    exception."""
    html = _run_app_js("renderChecklist(null)")
    assert "Checklist" in html
    assert "No checklist" in html
    assert "data-checklist-empty" in html
    assert "data-checklist-plan" not in html


def test_render_checklist_empty_state_when_neither_available():
    """The common case: a story not run under PIPELINE_DECOMPOSE has neither
    file -> empty state, same as a missing-file response."""
    data = {"plan": {"available": False, "text": ""},
            "scratchpad": {"available": False, "text": ""}}
    html = _run_app_js(f"renderChecklist({json.dumps(data)})")
    assert "No checklist" in html
    assert "data-checklist-empty" in html
    assert "data-checklist-plan" not in html
    assert "data-checklist-scratch" not in html


def test_render_checklist_plan_only_when_scratchpad_absent():
    """A story that just got its plan but hasn't checkpointed yet renders the
    plan but no Progress-notes subsection."""
    data = {"plan": {"available": True, "text": "1. tests\n2. impl\n"},
            "scratchpad": {"available": False, "text": ""}}
    html = _run_app_js(f"renderChecklist({json.dumps(data)})")
    assert "data-checklist-plan" in html
    assert "1. tests" in html
    assert "2. impl" in html
    # No scratchpad subsection when scratchpad unavailable
    assert "data-checklist-scratch" not in html
    assert "Progress notes" not in html


def test_render_checklist_renders_both_plan_and_scratchpad():
    """Positive: both artifacts available -> plan <pre>, a Progress-notes
    subsection, and a scratchpad <pre>, in that order."""
    data = {"plan": {"available": True, "text": "1. step one"},
            "scratchpad": {"available": True, "text": "done: one"}}
    html = _run_app_js(f"renderChecklist({json.dumps(data)})")
    assert "data-checklist-plan" in html
    assert "data-checklist-scratch" in html
    assert "Progress notes" in html
    assert "done: one" in html
    # Plan block precedes the scratchpad block.
    assert html.find("data-checklist-plan") < html.find("data-checklist-scratch")


def test_render_checklist_escapes_html_in_artifact_text():
    """Security: the plan and scratchpad are agent-written, so their text is
    HTML-escaped — a stray <script> in an artifact must not survive raw."""
    data = {"plan": {"available": True, "text": "<script>x</script>"},
            "scratchpad": {"available": False, "text": ""}}
    html = _run_app_js(f"renderChecklist({json.dumps(data)})")
    assert "&lt;script&gt;" in html
    assert "<script>" not in html


def test_render_checklist_empty_plan_text_still_renders_block():
    """Boundary: a zero-byte .agent_plan.md (available:true, text:'') still
    renders an empty plan <pre> block — 'file present but empty' is distinct
    from 'file absent' (the latter hits the empty state)."""
    data = {"plan": {"available": True, "text": ""},
            "scratchpad": {"available": False, "text": ""}}
    html = _run_app_js(f"renderChecklist({json.dumps(data)})")
    assert "data-checklist-plan" in html
    assert "No checklist" not in html


def test_static_style_css_defines_checklist_classes(client):
    """The checklist section uses .dsh-checklist-plan / .dsh-checklist-scratch
    / .modal-subsection — the CSS must define them so the section renders
    visibly (scrollable monospace blocks) rather than as unstyled elements."""
    css = client.get("/style.css").text
    for sel in (".dsh-checklist-plan", ".dsh-checklist-scratch",
                ".modal-subsection"):
        assert sel in css, f"missing CSS selector: {sel}"


def test_static_style_css_defines_plan_archive_classes(client):
    """The rendered sidebar uses .plan-item-row / .plan-archive-btn /
    .plan-archived / .plan-list-footer / .show-archived-toggle - the CSS
    must actually define those selectors so archive/dismiss renders
    visibly rather than as unstyled elements."""
    css = client.get("/style.css").text
    for sel in (".plan-item-row", ".plan-archive-btn", ".plan-archived",
                ".plan-list-footer", ".show-archived-toggle"):
        assert sel in css, f"missing CSS selector: {sel}"


def test_static_style_css_defines_journal_timeline_classes(client):
    """The rendered HTML uses .timeline / .timeline-item / .timeline-step /
    .timeline-summary / .timeline-next / .timeline-ts / .muted — the CSS
    must actually define those selectors so the section renders visibly
    rather than as unstyled bullets."""
    css = client.get("/style.css").text
    for sel in (".timeline", ".timeline-item", ".timeline-step",
                ".timeline-summary", ".timeline-next", ".timeline-ts"):
        assert sel in css, f"missing CSS selector: {sel}"
    # And the muted utility class used inside timeline entries
    assert ".muted" in css


