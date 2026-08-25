"""Static checks for the ES-module wiring of the dashboard frontend.

The story scaffolds static/app/state.js, static/app/api.js, and
static/app/routing.js and wires static/app.js to import them, with the
index.html script tag switched to `type="module"`. These tests assert that
wiring is present so the new modules are live rather than dead code.
"""
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INDEX_HTML = os.path.join(REPO_ROOT, "static", "index.html")
APP_JS = os.path.join(REPO_ROOT, "static", "app.js")


def _read(path):
    with open(path) as fh:
        return fh.read()


def test_index_html_loads_app_js_as_module():
    html = _read(INDEX_HTML)
    assert '<script type="module" src="/app.js">' in html


def test_app_js_imports_state_module():
    src = _read(APP_JS)
    assert "from './app/state.js'" in src


def test_app_js_imports_api_module():
    src = _read(APP_JS)
    assert "from './app/api.js'" in src


def test_app_js_imports_routing_module():
    src = _read(APP_JS)
    assert "from './app/routing.js'" in src
