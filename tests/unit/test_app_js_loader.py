"""Acceptance tests for the shared ESM/CJS app.js loader (tests/unit/_app_js.py).

These grade the prerequisite story that makes the dashboard Node test
harness ESM-aware: the loader must load a *modular* app.js (top-level
import/export) via dynamic import() AND fall back to the legacy eval() path
for a plain global app.js, returning the same CompletedProcess shape in both.
"""
import json

from _app_js import run_app_js


def _write(path, text):
    path.write_text(text, encoding="utf-8")


def _make_modular_app(tmp_path):
    """A minimal modular app.js that imports a sibling module and exports
    a function the test expr calls."""
    app = tmp_path / "app.js"
    (tmp_path / "package.json").write_text('{"type":"module"}', encoding="utf-8")
    (tmp_path / "app").mkdir()
    _write(tmp_path / "app" / "base.js", "export const BASE = 21;\n")
    _write(app, "import { BASE } from './app/base.js';\n"
                "export function double(x) { return x * 2; }\n"
                "export function basePlus(n) { return BASE + n; }\n")
    return app


def test_loader_handles_modular_app_js(tmp_path):
    app = _make_modular_app(tmp_path)
    r = run_app_js("double(21)", app_js=str(app))
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == 42


def test_loader_resolves_sibling_imports(tmp_path):
    app = _make_modular_app(tmp_path)
    r = run_app_js("basePlus(21)", app_js=str(app))
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == 42


def test_loader_falls_back_to_cjs_eval_for_plain_app(tmp_path):
    app = tmp_path / "plain.js"
    _write(app, "function triple(x) { return x * 3; }\n")
    r = run_app_js("triple(14)", app_js=str(app))
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == 42


def test_loader_reports_error_for_missing_import(tmp_path):
    app = tmp_path / "app.js"
    (tmp_path / "package.json").write_text('{"type":"module"}', encoding="utf-8")
    _write(app, "import { NOPE } from './app/missing.js';\nexport function f(){return 1;}\n")
    r = run_app_js("f()", app_js=str(app))
    assert r.returncode != 0
    assert r.stderr != ""
