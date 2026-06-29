"""Tests for the read-only monitoring dashboard's API (dashboard.py).

Exercises the FastAPI endpoints against fixture plan files on disk - the
same manifest/notifications/decisions files pipeline_mcp_server.py writes.
The dashboard never writes to PLAN_DIR itself, so these only assert reads.
"""
import json

import pytest
from fastapi.testclient import TestClient

import dashboard as d


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "PLAN_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def client():
    return TestClient(d.app)


def _write_manifest(plan_dir, name, stories, epics=None, paused=False):
    manifest = {"epics": epics or {}, "stories": stories}
    if paused:
        manifest["paused"] = True
    (plan_dir / f"{name}.manifest.json").write_text(json.dumps(manifest))


def test_health_ok(client, plan_dir):
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json()["ok"] is True


def test_list_plans_empty_when_no_manifests(client, plan_dir):
    res = client.get("/api/plans")
    assert res.status_code == 200
    assert res.json() == {"plans": []}


def test_list_plans_ignores_non_manifest_json_files(client, plan_dir):
    # A saved-but-not-ingested plan (plain <name>.json) has no manifest yet
    # and must not show up as if it had lifecycle state.
    (plan_dir / "draft-plan.json").write_text(json.dumps({"epics": []}))
    res = client.get("/api/plans")
    assert res.json() == {"plans": []}


def test_list_plans_returns_summary_with_status_counts(client, plan_dir):
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "one", "status": "done", "dependencies": []},
        "S2": {"summary": "two", "status": "in_progress", "dependencies": []},
        "S3": {"summary": "three", "status": "in_progress", "dependencies": []},
    })

    res = client.get("/api/plans")
    assert res.status_code == 200
    plans = res.json()["plans"]
    assert len(plans) == 1
    assert plans[0]["name"] == "demo"
    assert plans[0]["story_count"] == 3
    assert plans[0]["status_counts"] == {"done": 1, "in_progress": 2}
    assert plans[0]["paused"] is False


def test_list_plans_reports_paused_flag(client, plan_dir):
    _write_manifest(plan_dir, "frozen", {"S1": {"summary": "x", "status": "todo"}}, paused=True)
    res = client.get("/api/plans")
    assert res.json()["plans"][0]["paused"] is True


def test_get_plan_404_when_no_manifest(client, plan_dir):
    res = client.get("/api/plans/does-not-exist")
    assert res.status_code == 404


@pytest.fixture
def usage_state_path(tmp_path, monkeypatch):
    path = tmp_path / "usage_state.json"
    monkeypatch.setattr(d, "USAGE_STATE_PATH", path)
    return path


def test_usage_endpoint_when_no_state_file(client, usage_state_path):
    res = client.get("/api/usage")
    assert res.status_code == 200
    assert res.json()["available"] is False


def test_usage_endpoint_surfaces_gate_health(client, usage_state_path):
    usage_state_path.write_text(json.dumps({
        "session_pct": 91, "week_pct": 82, "paused": True,
        "measured_at": "2026-06-26T16:40:13+00:00",
        "gate_blind": False, "consecutive_parse_failures": 0,
    }))
    body = client.get("/api/usage").json()
    assert body["available"] is True
    assert body["session_pct"] == 91
    assert body["paused"] is True
    assert body["gate_blind"] is False


def test_usage_endpoint_reports_blind_gate(client, usage_state_path):
    usage_state_path.write_text(json.dumps({
        "session_pct": 50, "week_pct": 50, "paused": False,
        "measured_at": "2026-06-26T10:00:00+00:00",
        "gate_blind": True, "blind_since": "2026-06-26T10:30:00+00:00",
        "consecutive_parse_failures": 42,
    }))
    body = client.get("/api/usage").json()
    assert body["gate_blind"] is True
    assert body["blind_since"] == "2026-06-26T10:30:00+00:00"
    assert body["consecutive_parse_failures"] == 42


def test_get_plan_returns_stories_epics_notifications_decisions(client, plan_dir):
    _write_manifest(
        plan_dir, "demo",
        stories={"S1": {"summary": "one", "status": "done", "persona": "software-engineer"}},
        epics={"Epic A": "PIPE-1"},
    )
    (plan_dir / "demo.notifications.log").write_text(
        "2026-06-25T00:00:00+00:00 first note\n2026-06-25T00:01:00+00:00 second note\n"
    )
    decisions = [{
        "story_key": "S1", "question": "use library X or Y?",
        "options": ["X", "Y"], "ruling": "use X", "tier": "routine",
        "risk": "low", "rationale": "simpler", "notify_user": False,
        "decided_by": "overlord", "decided_at": "2026-06-25T00:02:00+00:00",
    }]
    (plan_dir / "demo.decisions.json").write_text(json.dumps(decisions))

    res = client.get("/api/plans/demo")
    assert res.status_code == 200
    body = res.json()
    assert body["name"] == "demo"
    assert body["epics"] == {"Epic A": "PIPE-1"}
    assert body["stories"]["S1"]["status"] == "done"
    assert body["notifications"] == [
        "2026-06-25T00:00:00+00:00 first note",
        "2026-06-25T00:01:00+00:00 second note",
    ]
    assert body["decisions"] == decisions


def test_get_plan_handles_missing_notifications_and_decisions(client, plan_dir):
    _write_manifest(plan_dir, "bare", {"S1": {"summary": "x", "status": "todo"}})
    res = client.get("/api/plans/bare")
    assert res.status_code == 200
    body = res.json()
    assert body["notifications"] == []
    assert body["decisions"] == []


def test_get_plan_tails_notifications_to_limit(client, plan_dir):
    _write_manifest(plan_dir, "chatty", {"S1": {"summary": "x", "status": "todo"}})
    lines = [f"2026-06-25T00:00:00+00:00 note {i}" for i in range(150)]
    (plan_dir / "chatty.notifications.log").write_text("\n".join(lines) + "\n")

    res = client.get("/api/plans/chatty")
    notifications = res.json()["notifications"]
    assert len(notifications) == 100
    assert notifications[-1] == "2026-06-25T00:00:00+00:00 note 149"
    assert notifications[0] == "2026-06-25T00:00:00+00:00 note 50"


# ---------- /api/dispatch_health: escalated-vs-completed rate by acceptance --

def test_dispatch_health_empty_when_no_manifests(client, plan_dir):
    """No plans ingested -> totals all zero, rates 0.0 (not NaN)."""
    res = client.get("/api/dispatch_health")
    assert res.status_code == 200
    body = res.json()
    assert body["totals"] == {
        "dispatched": 0, "done": 0, "escalated": 0, "stories": 0,
        "escalation_rate": 0.0, "success_rate": 0.0,
    }
    assert body["per_plan"] == {}


def test_dispatch_health_stratifies_by_acceptance_presence(client, plan_dir):
    """The headline of Fix #1's measurement: stories with `acceptance` vs
    those without, each with their own escalation/success rates."""
    _write_manifest(plan_dir, "p1", {
        # 2 dispatched, 1 escalated, 1 done — all WITHOUT acceptance
        "S1": {"summary": "a", "status": "done", "backend": "local",
               "acceptance": []},
        "S2": {"summary": "b", "status": "interrupted", "backend": "local",
               "escalated": True, "acceptance": []},
        # 2 dispatched, both done — WITH acceptance (the Fix #1 path)
        "S3": {"summary": "c", "status": "done", "backend": "local",
               "acceptance": [{"path": "t.py", "source": "x"}]},
        "S4": {"summary": "d", "status": "done", "backend": "local",
               "acceptance": [{"path": "t.py", "source": "x"}]},
    })
    # A plan with one still-todo story should NOT inflate the dispatched
    # count — denominator is dispatched, not story_count.
    _write_manifest(plan_dir, "p2", {
        "S1": {"summary": "queued", "status": "todo", "acceptance": []},
    })

    body = client.get("/api/dispatch_health").json()

    # Headline: rates computed off dispatched (not story count).
    assert body["totals"]["stories"] == 5   # 4 in p1 + 1 in p2
    assert body["totals"]["dispatched"] == 4
    assert body["totals"]["done"] == 3
    assert body["totals"]["escalated"] == 1
    assert body["totals"]["escalation_rate"] == 0.25
    assert body["totals"]["success_rate"] == 0.75

    p1 = body["per_plan"]["p1"]
    assert p1["without_acceptance"] == {
        "stories": 2, "dispatched": 2, "done": 1, "escalated": 1,
        "escalation_rate": 0.5, "success_rate": 0.5,
    }
    assert p1["with_acceptance"] == {
        "stories": 2, "dispatched": 2, "done": 2, "escalated": 0,
        "escalation_rate": 0.0, "success_rate": 1.0,
    }
    # p2's still-todo story stays out of the dispatched count.
    assert body["per_plan"]["p2"]["without_acceptance"]["dispatched"] == 0
    assert body["per_plan"]["p2"]["without_acceptance"]["escalation_rate"] == 0.0


def test_dispatch_health_counts_backend_set_as_dispatched_even_if_todo(
    client, plan_dir,
):
    """Stories whose backend field is set but status is still 'todo' (e.g.
    escalated via the local-first auto mode but not yet re-tried) should
    still count as dispatched, otherwise escalation events vanish from the
    rate until the next tick runs."""
    _write_manifest(plan_dir, "p", {
        "S1": {"summary": "x", "status": "todo", "backend": "claude",
               "escalated": True, "acceptance": []},
    })

    body = client.get("/api/dispatch_health").json()
    s = body["per_plan"]["p"]["without_acceptance"]
    assert s["dispatched"] == 1
    assert s["escalated"] == 1
    assert s["escalation_rate"] == 1.0


# ---------- last_activity derivation on /api/plans/{name} stories ----------

def _write_journal(plan_dir, plan_name, story_key, entries):
    """Write a checkpoint journal file. `entries` is a list of dicts each
    with a 'ts' (ISO timestamp) and a 'step' short id, in chronological
    order.  Only the final entry's timestamp matters for last_activity."""
    path = plan_dir / f"{plan_name}.{story_key}.journal.json"
    path.write_text(json.dumps(entries))


def test_story_last_activity_uses_interrupted_at_when_only_that_is_set(client, plan_dir):
    """(1) interrupted_at only -> last_activity == interrupted_at."""
    interrupted_at = "2026-06-25T12:00:00+00:00"
    _write_manifest(plan_dir, "demo", {
        "S1": {
            "summary": "interrupted story",
            "status": "interrupted",
            "interrupted_at": interrupted_at,
            "dependencies": [],
        },
    })
    body = client.get("/api/plans/demo").json()
    assert body["stories"]["S1"]["interrupted_at"] == interrupted_at  # preserved
    assert body["stories"]["S1"]["last_activity"] == interrupted_at


def test_story_last_activity_picks_later_of_interrupted_at_and_last_commit(client, plan_dir):
    """(2) both present -> last_activity == the later of the two."""
    earlier = "2026-06-25T10:00:00+00:00"
    later = "2026-06-25T12:00:00+00:00"
    _write_manifest(plan_dir, "demo", {
        "S1": {
            "summary": "interrupted with commit history",
            "status": "interrupted",
            "interrupted_at": earlier,
            "last_commit": later,
            "dependencies": [],
        },
        # Reverse case: last_commit is *earlier* than interrupted_at.
        "S2": {
            "summary": "commit older than interrupt",
            "status": "interrupted",
            "interrupted_at": later,
            "last_commit": earlier,
            "dependencies": [],
        },
    })
    body = client.get("/api/plans/demo").json()
    assert body["stories"]["S1"]["last_activity"] == later
    assert body["stories"]["S2"]["last_activity"] == later


def test_story_last_activity_omitted_when_no_signal(client, plan_dir):
    """(3) neither interrupted_at nor last_commit (and no journal) ->
    last_activity is None / absent so the UI doesn't render an age label."""
    _write_manifest(plan_dir, "demo", {
        "S1": {
            "summary": "todo story",
            "status": "todo",
            "dependencies": [],
        },
    })
    body = client.get("/api/plans/demo").json()
    story = body["stories"]["S1"]
    # Must exist but signal the absence clearly; UI gates on truthiness.
    assert story.get("last_activity") in (None, "")


def test_story_last_activity_uses_journal_final_timestamp_when_latest(client, plan_dir):
    """(4) journal present -> its final entry's timestamp is considered and
    wins when it's the latest signal available."""
    interrupted_at = "2026-06-25T10:00:00+00:00"
    last_commit = "2026-06-25T11:00:00+00:00"
    journal_final_ts = "2026-06-25T13:30:00+00:00"
    _write_manifest(plan_dir, "demo", {
        "S1": {
            "summary": "journal beats both",
            "status": "interrupted",
            "interrupted_at": interrupted_at,
            "last_commit": last_commit,
            "dependencies": [],
        },
    })
    _write_journal(plan_dir, "demo", "S1", [
        {"step": "a", "ts": "2026-06-25T09:00:00+00:00", "summary": "early"},
        {"step": "b", "ts": "2026-06-25T13:30:00+00:00", "summary": "final"},
    ])

    body = client.get("/api/plans/demo").json()
    assert body["stories"]["S1"]["last_activity"] == journal_final_ts


def test_story_last_activity_does_not_mutate_manifest(client, plan_dir):
    """The dashboard is read-only and must NOT modify the manifest on disk
    when deriving last_activity — re-read after the request and assert
    nothing new was written."""
    interrupted_at = "2026-06-25T12:00:00+00:00"
    _write_manifest(plan_dir, "demo", {
        "S1": {
            "summary": "no mutating writes please",
            "status": "interrupted",
            "interrupted_at": interrupted_at,
            "dependencies": [],
        },
    })
    before = json.loads((plan_dir / "demo.manifest.json").read_text())
    client.get("/api/plans/demo")
    after = json.loads((plan_dir / "demo.manifest.json").read_text())
    assert before == after


def test_story_last_activity_ignores_missing_or_empty_journal(client, plan_dir):
    """Boundary: a journal file that exists but has no parseable entries
    must not produce a last_activity; missing journal is the same."""
    interrupted_at = "2026-06-25T12:00:00+00:00"
    _write_manifest(plan_dir, "demo", {
        "S1": {
            "summary": "broken journal",
            "status": "interrupted",
            "interrupted_at": interrupted_at,
            "dependencies": [],
        },
    })
    # Empty list -> parser should fall back to manifest signals.
    (plan_dir / "demo.S1.journal.json").write_text("[]")
    body = client.get("/api/plans/demo").json()
    assert body["stories"]["S1"]["last_activity"] == interrupted_at


# ---------- client-side age/staleness helpers in static/app.js ----------
#
# The dashboard hands the UI a `last_activity` ISO string per story and the
# browser computes the age + staleness class from there so cards stay
# accurate between polls (no re-fetch needed as the clock advances). These
# tests exercise the pure helpers exposed by app.js by shelling out to Node
# in a subprocess — no JS test runner / jsdom dependency, just plain pytest.
import os  # noqa: E402
import subprocess  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
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
        + "process.stdout.write(JSON.stringify(" + expr + "));"
    )
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
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
    try/catch-wrapped so a locked-down storage backend doesn't throw."""
    js = client.get("/app.js").text
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
        " return JSON.stringify(renderBoard({"
        " 'k1':{status:'in_progress',summary:''},"
        " 'k2':{status:'in_progress',summary:''},"
        " 'k3':{status:'todo',summary:''}})); })()"
    )
    html = _run_app_js(expr)
    # exactly one column per status (case-sensitive status label)
    assert html.count('class="column"') == 3
    assert "in_progress</span>" in html
    assert "todo</span>" in html
    assert "done</span>" in html


def test_render_board_counts_match_filtered_cards_per_column():
    """The count pill in the column header must equal the number of cards
    the persona/risk filters let through (filter logic unchanged)."""
    expr = (
        "(() => {"
        " state.filters.statuses = ['in_progress'];"
        " state.filters.personas = []; state.filters.risks = [];"
        " return JSON.stringify(renderBoard({"
        "  'k1':{status:'in_progress', summary:'a'},"  # default persona/risk
        "  'k2':{status:'in_progress', summary:'b'},"  # default persona/risk
        "  'k3':{status:'todo',       summary:'c'},"  # wrong column
        "  'k4':{status:'in_progress', summary:'d', persona:'p'}"  # filtered
        " })); })()"
    )
    html = _run_app_js(expr)
    # exactly one column (in_progress)
    assert html.count('class="column"') == 1
    # the count badge inside that column reads '2'
    assert ">2</span>" in html or ">2<" in html
    # two cards (k1, k2); k3 lives in a different column, k4 persona-filtered
    assert html.count('class="card') == 2


def test_render_board_empty_column_renders_empty_body_not_absent():
    """A status with zero matching stories still renders the column shell
    so the layout stays stable as filters change."""
    expr = (
        "(() => {"
        " state.filters.statuses = ['in_progress', 'todo'];"
        " state.filters.personas = []; state.filters.risks = [];"
        " return JSON.stringify(renderBoard({"
        "  'k1':{status:'in_progress', summary:'only one'}"
        " })); })()"
    )
    html = _run_app_js(expr)
    # both columns present
    assert html.count('class="column"') == 2
    # but the 'todo' column has an empty body, not zero columns
    assert html.count('class="column-body">') == 2
    # count badge for the empty todo column reads '0'
    todo_idx = html.index("todo</span>")
    tail = html[todo_idx:todo_idx + 400]
    assert ">0<" in tail


def test_render_board_deselected_statuses_shows_empty_state():
    """All-statuses-deselected -> the empty-state copy, NOT a board of
    zero columns. (The text 'No statuses selected.' is the contract.)"""
    expr = (
        "(() => {"
        " state.filters.statuses = [];"
        " return JSON.stringify(renderBoard({"
        "  'k1':{status:'in_progress', summary:'a'}"
        " })); })()"
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
        " return JSON.stringify(renderBoard({"
        "  'a':{status:'done',         summary:'a'},"
        "  'b':{status:'done',         summary:'b'},"
        "  'c':{status:'in_progress',  summary:'c'},"
        "  'd':{status:'todo',         summary:'d'},"
        "  'e':{status:'tests_passed', summary:'e'}"
        " })); })()"
    )
    html = _run_app_js(expr)
    # a column-completion element with the done/total ratio
    assert 'class="column-completion"' in html
    assert "2/5" in html


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
