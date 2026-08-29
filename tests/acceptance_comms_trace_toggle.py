"""Read-only acceptance oracle: comms tool-trace toggle is wired across
static/index.html, static/app/comms.js, and static/style.css.

Static-source assertions deliberately (same style as the existing
dashboard comms unit tests): the feature IS the wiring -- endpoint-free
UI state, so the grader must check the real files the browser loads.
Membership assertions only; no exact counts, no file hashes.
"""
import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

INDEX_HTML = os.path.join(REPO_ROOT, "static", "index.html")
COMMS_JS = os.path.join(REPO_ROOT, "static", "app", "comms.js")
STYLE_CSS = os.path.join(REPO_ROOT, "static", "style.css")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_toggle_button_present_in_comms_actions():
    html = _read(INDEX_HTML)
    assert 'id="comms-trace-toggle"' in html
    # placement: inside the .comms-actions row, existing siblings survive
    actions = re.search(r'<div class="comms-actions">(.*?)</div>', html, re.DOTALL)
    assert actions, "comms-actions row not found in index.html"
    block = actions.group(1)
    assert "comms-trace-toggle" in block
    assert "comms-export" in block, "existing export button must survive"
    assert "comms-reset" in block, "existing reset button must survive"


def test_comms_js_wires_toggle_and_body_class():
    js = _read(COMMS_JS)
    assert "comms-trace-toggle" in js
    assert "trace-off" in js
    assert "commsShowTrace" in js
    # persistence and fallback must both be guarded
    assert "localStorage.getItem" in js
    assert "localStorage.setItem" in js
    # existing rendering path must be untouched (tokens still present)
    assert "renderToolTraceHtml" in js
    assert "trace-chip" in js


def test_style_css_hides_trace_when_body_off():
    css = _read(STYLE_CSS)
    assert "body.trace-off .trace-chip" in css
    assert "body.trace-off .trace-detail" in css
    assert ".trace-chip.expanded + .trace-detail" in css, "existing expanded rule must survive"