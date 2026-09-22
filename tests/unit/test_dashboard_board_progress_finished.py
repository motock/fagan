"""Progress-bar honesty on plan-board cards (static/app/render/board.js).

A card's progress bar reads its ``done`` count from a ``PROGRESS:
<done>/<total>`` line the dispatched agent self-reports into its worktree
scratchpad (``app/dashboard_helpers._parse_progress``). Nothing keeps that
self-report in step with reality: the executor routinely stops bumping it while
continuing to work, so a card can read e.g. ``3/10`` on a story that has
already finished, committed and pushed. The bar stays up until the scheduler's
next tick moves the story off ``in_progress``.

The server already knows when the executor signalled completion:
``pipeline/wedge_io.collect_story_wedge_signals`` derives ``agent_done`` from
``.agent_done`` / ``.agent_done.consumed``, and ``pipeline/wedge.wedge_verdict``
forwards it to the dashboard as ``story.wedge.measured.agent_done`` whenever the
dispatch pid is dead -- exactly the window in which the stale fraction is
visible, since the driver writes the marker as its run exits and the pid is
therefore already gone.

THIS story gates the board on that flag: a new exported pure helper
``isFinished(story)`` plus a ``&& !isFinished(s)`` term on the ``progressHtml``
condition in BOTH card-building paths -- renderBoard's string-build path and the
in-place ``_diffBoardCards`` path. A gate added only to the string path would
silently un-hide on the second poll; the wedged-badge story already demonstrated
that failure mode for this same file.

CUMULATIVE-ARTIFACT RULE: board.js is a shared artifact other stories also
extend. These tests assert only what THIS story adds -- the export, the helper's
truth table, and the presence/absence of the ``card-progress`` block for a story
that has signalled completion. They never assert the complete card markup, an
exact full HTML string, or any byte/SHA hash of board.js.

RED until the implementation lands: ``isFinished`` is not exported yet, and a
story whose executor has finished still renders its stale progress bar.
"""
import json
import os

from tests.unit._app_js import run_app_js

# The DOM shim is the wedged-badge story's (~200 lines of HTML tokenizer/DOM
# stand-in). Reused rather than duplicated: both board.js stories need the same
# stand-in to drive renderBoard/_diffBoardCards under Node.
from tests.unit.test_dashboard_board_wedge_badge import _SHIM

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BOARD_JS = os.path.join(REPO_ROOT, "static", "app", "render", "board.js")


def _run_board_js(expr):
    """Evaluate ``expr`` with static/app/render/board.js imported as an ES
    module. Returns the JSON-decoded value; raises AssertionError with node's
    stderr on failure."""
    proc = run_app_js(expr, app_js=BOARD_JS, shim=_SHIM)
    if proc.returncode != 0:
        raise AssertionError(
            "node failed while evaluating a board.js expression -- if this is an "
            "'isFinished is not defined' error, the helper is missing OR not "
            "exported from static/app/render/board.js\n"
            f"stderr:\n{proc.stderr}"
        )
    return json.loads(proc.stdout)


def _running_story(key="S1", **over):
    """An in_progress story the executor is still working on: dispatch pid
    alive, no completion marker, self-reported 3/10 checklist fraction."""
    s = {
        "summary": "a running story",
        "status": "in_progress",
        "progress": {"done": 3, "total": 10},
        "wedge": {
            "wedged": False,
            "reasons": [],
            "measured": {"pid_alive": True, "activity_age_seconds": 4.0},
        },
    }
    s.update(over)
    return s


def _finished_story(key="S1", **over):
    """The same story once its executor has signalled completion: the driver
    wrote .agent_done as its run exited, so the pid is dead and wedge_verdict
    forwarded agent_done. The scratchpad still says 3/10."""
    s = _running_story(key=key)
    s["wedge"] = {
        "wedged": False,
        "reasons": [],
        "measured": {
            "pid_alive": False,
            "activity_age_seconds": 4.0,
            "agent_done": True,
        },
    }
    s.update(over)
    return s


# === the isFinished helper =================================================

def test_is_finished_is_exported_from_board_js():
    """A named export, like isWedged -- so it is directly testable."""
    assert _run_board_js("typeof isFinished") == "function"


def test_is_finished_true_once_the_server_reports_the_completion_marker():
    assert _run_board_js("isFinished(" + json.dumps(_finished_story()) + ")") is True


def test_is_finished_false_while_the_executor_is_still_running():
    assert _run_board_js("isFinished(" + json.dumps(_running_story()) + ")") is False


def test_is_finished_false_without_a_wedge_object():
    story = _running_story()
    del story["wedge"]
    assert _run_board_js("isFinished(" + json.dumps(story) + ")") is False


def test_is_finished_false_when_measured_has_no_agent_done_key():
    """The healthy-story wedge payload carries no agent_done at all."""
    story = _running_story()
    assert "agent_done" not in story["wedge"]["measured"]
    assert _run_board_js("isFinished(" + json.dumps(story) + ")") is False


def test_is_finished_false_when_the_marker_is_explicitly_absent():
    story = _running_story()
    story["wedge"]["measured"]["agent_done"] = False
    assert _run_board_js("isFinished(" + json.dumps(story) + ")") is False


def test_is_finished_false_for_a_story_that_is_no_longer_in_progress():
    assert _run_board_js(
        "isFinished(" + json.dumps(_finished_story(status="pr_open")) + ")"
    ) is False


def test_is_finished_false_for_missing_input():
    assert _run_board_js("isFinished(null)") is False
    assert _run_board_js("isFinished(undefined)") is False


# === string-build path (renderBoard) =======================================

def test_render_board_omits_the_progress_bar_once_completion_is_signalled():
    html = _run_board_js(
        "renderBoard({ S1: " + json.dumps(_finished_story()) + " })"
    )
    assert isinstance(html, str) and "card-key" in html, f"no markup: {html!r}"
    assert "card-progress" not in html, (
        f"a finished story still rendered a stale progress bar: {html!r}"
    )


def test_render_board_keeps_the_progress_bar_for_a_running_story():
    html = _run_board_js(
        "renderBoard({ S1: " + json.dumps(_running_story()) + " })"
    )
    assert "card-progress" in html, f"a running story lost its bar: {html!r}"
    assert ">3/10</span>" in html, f"the fraction is missing: {html!r}"


def test_render_board_does_not_throw_on_a_malformed_wedge_measured():
    story = _running_story()
    story["wedge"]["measured"] = "not-an-object"
    html = _run_board_js("renderBoard({ S1: " + json.dumps(story) + " })")
    assert "card-progress" in html


# === in-place update path (_diffBoardCards, the second-poll path) ==========

def _diff_cards_expr(pairs):
    """Build an expr that calls _diffBoardCards once against a .column-body
    element and reports each built card's key and raw inner HTML."""
    return (
        "(() => {"
        " const body = document.createElement('div');"
        " body.className = 'column-body';"
        " const collect = () => body.querySelectorAll('.card').map((c) => ({"
        " key: c.dataset.key, html: c.__innerHTMLRaw }));"
        f" _diffBoardCards(body, {json.dumps(pairs)});"
        " return collect(); })()"
    )


def test_diff_board_cards_omits_the_progress_bar_once_completion_is_signalled():
    """The in-place builder is what every poll after the first goes through, so
    a gate present only in the string path would un-hide on the second poll."""
    res = _run_board_js(_diff_cards_expr([["S1", _finished_story()]]))
    cards = {c["key"]: c for c in res}
    assert "S1" in cards, f"no card built: {res}"
    assert "card-progress" not in cards["S1"]["html"], (
        f"the in-place path still rendered a stale bar: {cards['S1']['html']!r}"
    )


def test_diff_board_cards_keeps_the_progress_bar_for_a_running_story():
    res = _run_board_js(_diff_cards_expr([["S1", _running_story()]]))
    cards = {c["key"]: c for c in res}
    assert "S1" in cards, f"no card built: {res}"
    assert "card-progress" in cards["S1"]["html"], (
        f"the in-place path lost the progress bar: {cards['S1']['html']!r}"
    )
