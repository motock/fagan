"""Node-eval harness tests for the Config Roles provider→model dropdown
cascade fix in static/app/main.js.

Bug being regression-tested: `renderRoleEdit(role, providers)` builds the
`<select class="edit-provider">` and `<select class="edit-model">` markup
ONCE, using the row's current provider to look up
`providers[currentProvider].models` for the Model select's options, while
`_renderConfigRoles` only wires a `click` listener on `[data-save-role]`.
There is NO `change` listener on `.edit-provider`, so picking a different
Provider in that dropdown never touches the Model select: its `<option>`
list stays frozen to whatever the row's original provider had at render
time.

The fix shape under test:
  1. the model-`<option>`-building logic that lives inline inside
     `renderRoleEdit` is extracted into a new, pure, top-level function
     `buildModelOptionsHtml(providers, provider, currentModel)` returning the
     joined `<option value="...">...</option>` HTML string, preserving the
     exact existing fallback behavior (a non-empty `currentModel` that is not
     present in the target provider's models is prepended as an extra
     selected option; a provider that is not a key in `providers` at all is
     treated as having `{}` models instead of throwing);
  2. `renderRoleEdit` delegates to that helper for its initial Model select
     markup instead of inlining the logic;
  3. `_renderConfigRoles` adds a SECOND `querySelectorAll` loop (alongside
     the existing `[data-save-role]` loop) over
     `tbody.querySelectorAll(".edit-provider")`, attaching a `change`
     listener that rewrites the sibling Model select (found via
     `select.closest("tr").querySelector(".edit-model")`) with
     `buildModelOptionsHtml(providers, newProviderValue, "")` -- an EMPTY
     `currentModel`, letting the browser default-select the first option;
  4. `buildModelOptionsHtml` is appended to main.js's existing
     `export { loadRegistry, loadWorkspaceView, wireWorkspaceView };`
     statement so it is reachable from this Node-eval harness, while
     `renderRoleEdit` and `_renderConfigRoles` stay unexported (matching the
     existing pattern: `loadRegistry` was exported for the same reason).

This file mirrors the harness pattern in
tests/unit/test_dashboard_config_dropdowns.py verbatim: same DOM shim (copied
byte-for-byte, since static/app/main.js's top-level side effects -- theme
wiring, the story-modal close/backdrop listeners, the Config nav item,
`_wireBackendSelector`, an unawaited `refresh()` call, etc. -- all execute
the moment the ESM module loads, and this shim is already proven to satisfy
all of it), same tests.unit._app_js loader, same `_run_app_js` helper that
raises AssertionError on a nonzero node returncode and json.loads's stdout.

The harness's DOM shim cannot exercise the real event firing end-to-end (its
`querySelectorAll` always returns `[]` and `addEventListener` is a no-op), so
the change-listener wiring from fix shape (3) is graded via static-source
assertions on static/app/main.js instead of a live DOM interaction.

These tests are RED until the implementation lands: `buildModelOptionsHtml`
does not exist yet, so every functional test below observes
`{"exported": false, ...}` (guarded with `typeof buildModelOptionsHtml ===
"function"` so the suite reports a clean AssertionError instead of a
ReferenceError), and every static-source test fails on its missing
substring/regex.
"""
import json
import os
import re

from tests.unit._app_js import run_app_js as _shared_run_app_js

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_JS = os.path.join(REPO_ROOT, "static", "app.js")
MAIN_JS = os.path.join(REPO_ROOT, "static", "app", "main.js")


def _main_js_source():
    """Read static/app/main.js source for static-source (rename/removal) assertions."""
    with open(MAIN_JS, encoding="utf-8") as fh:
        return fh.read()


# === DOM shim (copied byte-for-byte from test_dashboard_config_dropdowns.py) ===

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
    (and, transitively, static/app/main.js) has been loaded. Returns the
    JSON-serialized result."""
    proc = _shared_run_app_js(expr, shim=_SHIM)
    if proc.returncode != 0:
        raise AssertionError(f"node failed: {proc.stderr}")
    return json.loads(proc.stdout)


def _call_build_model_options_html(providers, provider, current_model):
    """Call buildModelOptionsHtml(providers, provider, current_model) off the
    loaded module and return a dict:
      {"exported": bool, "threw": str|None, "result": str|None}

    `exported` is False (and `result` is None) when buildModelOptionsHtml is
    not yet reachable off the module's exports -- the expected RED state
    before the implementation lands. Guarding with
    `typeof buildModelOptionsHtml === "function"` (rather than calling the
    bare identifier directly) avoids a ReferenceError so the test always
    reports a clean, readable AssertionError instead of erroring out.

    `threw` captures an exception raised *inside* the helper so tests can
    assert the unknown-provider / malformed-input cases degrade gracefully
    (returning an option-less string) instead of throwing.
    """
    expr = (
        "(() => {"
        ' if (typeof buildModelOptionsHtml !== "function")'
        "  return { exported: false, threw: null, result: null };"
        " try {"
        "  const result = buildModelOptionsHtml("
        f"    {json.dumps(providers)}, {json.dumps(provider)}, {json.dumps(current_model)});"
        "  return { exported: true, threw: null,"
        "            result: result === undefined ? null : result };"
        " } catch (e) {"
        '  return { exported: true, threw: String((e && e.message) || e), result: null };'
        " }"
        "})()"
    )
    return _run_app_js(expr)


# === function-source extraction for the static-source assertions ==============

_TOP_LEVEL_DECL_RE = re.compile(
    r"(?m)^(?:async\s+)?function\b|^export\b|^(?:const|let|var)\b"
)


def _extract_function_source(src, name):
    """Extract the source text of a top-level `function name(...) {...}`
    declaration (with a `const name = ...` arrow fallback), bounded by the
    next top-level function / async function / export / const-let-var
    declaration (or EOF). Never asserts exact line numbers."""
    start = src.find(f"function {name}(")
    if start == -1:
        for prefix in (f"const {name} =", f"let {name} =", f"var {name} ="):
            start = src.find(prefix)
            if start != -1:
                break
    if start == -1:
        return ""
    search_from = start + len(f"function {name}(")
    m = _TOP_LEVEL_DECL_RE.search(src, search_from)
    end = m.start() if m else len(src)
    return src[start:end]


_PROVIDERS = {
    "claude": {"models": {"sonnet": {}, "opus": {}}},
    "ollama": {"models": {"glm": {}}},
}


# === buildModelOptionsHtml is reachable at all ================================

def test_build_model_options_html_is_exported_as_function():
    out = _call_build_model_options_html(_PROVIDERS, "claude", "")
    assert out["exported"] is True, (
        "typeof buildModelOptionsHtml !== 'function': buildModelOptionsHtml "
        "must be a new top-level function in static/app/main.js, appended to "
        "the existing export { loadRegistry, ... } statement"
    )


# === happy path ================================================================

def test_returns_options_for_the_given_provider_only():
    out = _call_build_model_options_html(_PROVIDERS, "claude", "")
    assert out["exported"] is True, "buildModelOptionsHtml is not exported yet"
    assert out["threw"] is None, f"buildModelOptionsHtml threw: {out['threw']}"
    html = out["result"]
    assert isinstance(html, str), f"expected an HTML string, got: {html!r}"
    assert 'value="sonnet"' in html
    assert 'value="opus"' in html
    assert 'value="glm"' not in html


def test_switching_provider_returns_the_other_providers_models():
    """The direct regression test for the reported bug: selecting a different
    provider must yield different model options."""
    claude_out = _call_build_model_options_html(_PROVIDERS, "claude", "")
    ollama_out = _call_build_model_options_html(_PROVIDERS, "ollama", "")
    assert claude_out["exported"] is True and ollama_out["exported"] is True
    claude_html = claude_out["result"]
    ollama_html = ollama_out["result"]
    assert isinstance(claude_html, str) and isinstance(ollama_html, str)
    assert claude_html != ollama_html
    assert 'value="glm"' in ollama_html
    assert 'value="sonnet"' not in ollama_html


def test_current_model_is_marked_selected_when_present():
    out = _call_build_model_options_html(_PROVIDERS, "ollama", "glm")
    assert out["exported"] is True, "buildModelOptionsHtml is not exported yet"
    html = out["result"]
    tags = [t for t in re.findall(r"<option\b[^>]*>", html) if 'value="glm"' in t]
    assert len(tags) == 1, f"expected exactly one glm option tag, got: {tags}"
    assert "selected" in tags[0]


def test_current_model_present_in_list_is_not_duplicated():
    """A currentModel that IS in the target provider's models must be marked
    selected without being prepended a second time."""
    out = _call_build_model_options_html(_PROVIDERS, "claude", "opus")
    assert out["exported"] is True, "buildModelOptionsHtml is not exported yet"
    html = out["result"]
    assert html.count('value="opus"') == 1, f"duplicated opus option: {html!r}"


def test_repeated_calls_with_the_same_args_return_identical_html():
    """The helper must be pure: same inputs, same output string."""
    first = _call_build_model_options_html(_PROVIDERS, "claude", "opus")
    second = _call_build_model_options_html(_PROVIDERS, "claude", "opus")
    assert first["exported"] is True and second["exported"] is True
    assert first["result"] == second["result"]


# === fallback: currentModel not in the target provider's models ================

def test_current_model_not_in_list_is_prepended_and_selected():
    out = _call_build_model_options_html(
        _PROVIDERS, "claude", "some-legacy-model-not-in-registry"
    )
    assert out["exported"] is True, "buildModelOptionsHtml is not exported yet"
    html = out["result"]
    tags = re.findall(r"<option\b[^>]*>", html)
    legacy = [t for t in tags if 'value="some-legacy-model-not-in-registry"' in t]
    assert len(legacy) == 1, (
        f"expected the out-of-registry currentModel to be prepended as an "
        f"option, got tags: {tags}"
    )
    assert "selected" in legacy[0]
    for name in ("sonnet", "opus"):
        matching = [t for t in tags if f'value="{name}"' in t]
        assert len(matching) == 1, f"missing {name} option, got tags: {tags}"
        assert "selected" not in matching[0], (
            f"{name} must remain an unselected option: {matching[0]!r}"
        )


# === negative / boundary cases =================================================

def test_unknown_provider_returns_empty_options_not_throw():
    """A provider that is not a key in `providers` at all must be treated as
    having {} models -- no throw (the harness call itself must succeed), and
    no <option> in the result."""
    out = _call_build_model_options_html(_PROVIDERS, "nonexistent-provider", "")
    assert out["exported"] is True, "buildModelOptionsHtml is not exported yet"
    assert out["threw"] is None, (
        f"buildModelOptionsHtml threw on an unknown provider: {out['threw']}"
    )
    assert "<option" not in (out["result"] or "")


def test_unknown_provider_with_current_model_still_prepends_it():
    out = _call_build_model_options_html(_PROVIDERS, "nonexistent-provider", "keep-me")
    assert out["exported"] is True, "buildModelOptionsHtml is not exported yet"
    assert out["threw"] is None, (
        f"buildModelOptionsHtml threw on an unknown provider: {out['threw']}"
    )
    html = out["result"]
    assert html.count("<option") == 1, (
        f"expected exactly one <option> (the prepended currentModel), got: {html!r}"
    )
    tags = re.findall(r"<option\b[^>]*>", html)
    assert 'value="keep-me"' in tags[0]
    assert "selected" in tags[0]


def test_empty_providers_object_returns_empty_options():
    out = _call_build_model_options_html({}, "claude", "")
    assert out["exported"] is True, "buildModelOptionsHtml is not exported yet"
    assert out["threw"] is None, f"buildModelOptionsHtml threw: {out['threw']}"
    assert "<option" not in (out["result"] or "")


# === static-source assertions (the harness shim cannot fire real events) =======

def test_source_wires_a_change_listener_on_edit_provider():
    src = _main_js_source()
    assert ".edit-provider" in src
    assert re.search(
        r"edit-provider[\s\S]{0,400}addEventListener\(\s*[\"']change[\"']", src
    ), (
        "no addEventListener(\"change\") wired on .edit-provider within 400 "
        "chars of it: _renderConfigRoles must attach a change listener to "
        "each tbody.querySelectorAll('.edit-provider') select"
    )


def test_source_exports_build_model_options_html():
    src = _main_js_source()
    export_stmts = re.findall(r"export\s*\{[^}]*\}", src)
    assert export_stmts, "no `export { ... }` statement found in static/app/main.js"
    load_registry_stmts = [s for s in export_stmts if "loadRegistry" in s]
    assert load_registry_stmts, "the existing loadRegistry export statement is missing"
    assert any(
        "buildModelOptionsHtml" in s for s in load_registry_stmts
    ), (
        "buildModelOptionsHtml must be appended to the existing "
        "export { loadRegistry, loadWorkspaceView, wireWorkspaceView } list, "
        "not added via a second export statement"
    )


def test_render_role_edit_and_render_config_roles_remain_unexported():
    src = _main_js_source()
    export_stmts = re.findall(r"export\s*\{[^}]*\}", src)
    joined = "\n".join(export_stmts)
    assert not re.search(r"\brenderRoleEdit\b", joined)
    assert not re.search(r"\b_renderConfigRoles\b", joined)
    assert not re.search(
        r"export\s+(?:async\s+)?function\s+(?:renderRoleEdit|_renderConfigRoles)\b", src
    )
    assert not re.search(
        r"export\s+(?:const|let|var)\s+(?:renderRoleEdit|_renderConfigRoles)\b", src
    )


def test_build_model_options_html_is_a_top_level_function_declaration():
    src = _main_js_source()
    assert re.search(
        r"(?m)^(?:export\s+)?function\s+buildModelOptionsHtml\s*\(", src
    ), (
        "buildModelOptionsHtml must be declared as a new top-level function "
        "in static/app/main.js (column-0 `function buildModelOptionsHtml(`)"
    )


def test_render_role_edit_delegates_to_the_new_helper():
    src = _main_js_source()
    body = _extract_function_source(src, "renderRoleEdit")
    assert body, "renderRoleEdit not found in static/app/main.js"
    assert "buildModelOptionsHtml(" in body, (
        "renderRoleEdit must call buildModelOptionsHtml(...) for its initial "
        "Model select markup instead of inlining the option-building logic"
    )
    assert re.search(r"buildModelOptionsHtml\([^()]*,[^()]*,[^()]*\)", body), (
        "renderRoleEdit must call buildModelOptionsHtml with all three "
        "arguments (providers, currentProvider, currentModel)"
    )
    assert "modelNames.map(" not in body, (
        "the old inline modelNames.map(...) option-building logic must be "
        "removed from renderRoleEdit, not kept alongside the new helper"
    )


def test_source_config_roles_wires_the_change_listener_with_an_empty_current_model():
    src = _main_js_source()
    body = _extract_function_source(src, "_renderConfigRoles")
    assert body, "_renderConfigRoles not found in static/app/main.js"
    provider_loop = re.search(
        r"querySelectorAll\(\s*[\"']\.edit-provider[\"']\s*\)", body
    )
    assert provider_loop, (
        "_renderConfigRoles must add a second querySelectorAll('.edit-provider') "
        "loop (alongside the existing [data-save-role] loop)"
    )
    assert re.search(
        r"addEventListener\(\s*[\"']change[\"']\s*,", body[provider_loop.start():]
    ), "the .edit-provider selects must get a change listener inside _renderConfigRoles"


def test_source_change_listener_rewrites_the_sibling_model_select():
    src = _main_js_source()
    body = _extract_function_source(src, "_renderConfigRoles")
    anchor = body.find(".edit-provider")
    tail = body[anchor:] if anchor != -1 else body
    assert re.search(r"closest\(\s*[\"']tr[\"']\s*\)", tail), (
        "the change listener must find the row via select.closest('tr')"
    )
    assert re.search(r"querySelector\(\s*[\"']\.edit-model[\"']\s*\)", tail), (
        "the change listener must locate the sibling Model select via "
        "querySelector('.edit-model')"
    )
    assert re.search(r"\.innerHTML\s*=", tail), (
        "the change listener must replace the Model select's .innerHTML"
    )
    assert re.search(r"\.value", tail), (
        "the change listener must read the provider select's new .value"
    )
    call = re.search(r"buildModelOptionsHtml\([^()]*,[^()]*,[^()]*\)", tail)
    assert call, (
        "the change listener must rebuild the Model select's options via "
        "buildModelOptionsHtml(...)"
    )
    assert re.search(r",\s*(?:\"\"|'')\s*\)", call.group(0)), (
        "the change listener must pass an EMPTY string as currentModel "
        f"(got: {call.group(0)!r})"
    )


def test_source_existing_save_role_loop_is_preserved_and_precedes_the_new_loop():
    src = _main_js_source()
    body = _extract_function_source(src, "_renderConfigRoles")
    assert body, "_renderConfigRoles not found in static/app/main.js"
    tbody_assignment = re.search(r"tbody\.innerHTML\s*=", body)
    assert tbody_assignment, "_renderConfigRoles must still assign tbody.innerHTML"
    save_loop = re.search(
        r"querySelectorAll\(\s*[\"']\[data-save-role\][\"']\s*\)", body
    )
    assert save_loop, (
        "the existing tbody.querySelectorAll('[data-save-role]') loop must be "
        "preserved unmodified"
    )
    provider_loop = re.search(
        r"querySelectorAll\(\s*[\"']\.edit-provider[\"']\s*\)", body
    )
    assert provider_loop, "_renderConfigRoles must add a querySelectorAll('.edit-provider') loop"
    assert save_loop.start() > tbody_assignment.start(), (
        "the existing [data-save-role] loop must stay after the tbody.innerHTML assignment"
    )
    assert provider_loop.start() > save_loop.start(), (
        "the .edit-provider change-listener loop must be a SECOND loop added "
        "after the existing [data-save-role] loop, not a merge into or a "
        "reorder of it"
    )