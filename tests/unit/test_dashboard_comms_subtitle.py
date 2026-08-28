"""Node-eval harness tests for the live Comms header subtitle.

The Comms view header currently shows a hardcoded "chat" subtitle
(static/index.html's `<div class="comms-sub" id="comms-sub">chat</div>`).
This story makes it live: static/app/comms.js gains a new exported async
function `updateCommsSubtitle()` that fetches GET /api/config, finds the
entry in `cfg.roles` where `role === 'chat'`, and sets `#comms-sub`'s
textContent to `chat -> <provider>/<model>` (with a real U+2192 arrow
glyph) - falling back to the literal string "chat" on any error, missing
role, or malformed response. static/app/main.js wires it: the import line
from "./comms.js" gains the new name, and `selectComms()` calls
`updateCommsSubtitle()` (fire-and-forget, not awaited) right after
`state.selectedPlan = null;` and before `updateHash();`.

This file follows the harness pattern from
tests/unit/test_dashboard_comms_send.py (itself copied from
test_dashboard_comms_nav.py / test_dashboard.py) so it stands alone, but
loads static/app/comms.js DIRECTLY via _app_js.py's `run_app_js(app_js=...)`
override rather than through static/app.js. Per the brief, main.js does
NOT add updateCommsSubtitle to its own `export {...}` lists (only to its
import line from comms.js) - so `export * from "./app/main.js"` in
static/app.js never re-exposes it. comms.js's own `export {...}` statement
is the only place updateCommsSubtitle is actually exported, so loading
comms.js directly is the only way to obtain a callable global for testing.

These tests are RED until the implementation lands: updateCommsSubtitle
does not exist yet, comms.js does not import fetchJson, and main.js's
import line / selectComms body are unchanged.
"""
import json
import os

from tests.unit._app_js import run_app_js as _shared_run_app_js

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COMMS_JS = os.path.join(REPO_ROOT, "static", "app", "comms.js")
MAIN_JS = os.path.join(REPO_ROOT, "static", "app", "main.js")


_SHIM = r"""
        const noop = () => {};
        // A single persistent #comms-sub stand-in so a test can read back
        // textContent after calling updateCommsSubtitle(). Starts at "chat"
        // to mirror the static markup's current hardcoded default.
        globalThis.__commsSub = { textContent: "chat" };
        const commsSubEl = globalThis.__commsSub;
        globalThis.document = {
            addEventListener: noop,
            getElementById: function (id) {
                if (id === "comms-sub") return commsSubEl;
                return null;
            },
        };
        // state.js (imported transitively by comms.js) assigns window.state
        // and window.BACKEND_VALUES/ESCALATED_VALUES at module load time.
        globalThis.window = {
            location: { hash: "" },
            addEventListener: noop,
        };
        globalThis.localStorage = { getItem: () => null, setItem: noop };
"""

_SHIM_FETCH_DEFAULT = "globalThis.fetch = () => new Promise(() => {});"

_SHIM_TAIL = """
        process.on("unhandledRejection", () => {});
"""


def _run_comms_js(expr, fetch_impl=None, extra_setup=""):
    """Evaluate `expr` after loading static/app/comms.js directly as an ES
    module (see module docstring for why comms.js, not app.js). Mirrors
    test_dashboard_comms_send.py's `_run_app_js` shape/semantics exactly,
    just pointed at a different `app_js`.
    """
    shim_fetch_swap = (
        "globalThis.fetch = " + fetch_impl + ";"
        if fetch_impl is not None
        else ""
    )
    built_shim = _SHIM + _SHIM_FETCH_DEFAULT + _SHIM_TAIL + extra_setup
    expr = shim_fetch_swap + expr
    proc = _shared_run_app_js(expr, app_js=COMMS_JS, shim=built_shim)
    if proc.returncode != 0:
        raise AssertionError(f"node failed: {proc.stderr}")
    return json.loads(proc.stdout)


def _run_comms_js_async(expr, fetch_impl=None, extra_setup=""):
    """Like _run_comms_js but awaits the expression (updateCommsSubtitle is
    async)."""
    wrapped = "(async () => { return eval(" + json.dumps(expr) + "); })()"
    return _run_comms_js(wrapped, fetch_impl=fetch_impl, extra_setup=extra_setup)


def _comms_js_source():
    with open(COMMS_JS, encoding="utf-8") as fh:
        return fh.read()


def _main_js_source():
    with open(MAIN_JS, encoding="utf-8") as fh:
        return fh.read()


def _function_body(src, start_marker):
    """Slice `src` from `start_marker` to the next top-level function
    definition (or module.exports), whichever comes first. Same technique
    test_dashboard_comms_send.py uses to isolate a single function's body
    for source-level assertions."""
    start = src.index(start_marker)
    end_candidates = [
        src.find("\nfunction ", start + 1),
        src.find("\nasync function ", start + 1),
        src.find("\nmodule.exports", start + 1),
    ]
    end_candidates = [e for e in end_candidates if e != -1]
    end = min(end_candidates) if end_candidates else len(src)
    return src[start:end]


# === comms.js: new import line ============================================

def test_comms_js_imports_fetch_json_as_its_own_import_line():
    """comms.js must add `import { fetchJson } from "./api.js";` as its own
    new import line, without disturbing the two existing import lines."""
    src = _comms_js_source()
    assert 'import { fetchJson } from "./api.js";' in src, (
        "comms.js must add a new import line for fetchJson from ./api.js"
    )
    assert 'import { state } from "./state.js";' in src, (
        "the existing state.js import must be left unmodified"
    )
    assert 'import { escapeHtml } from "./render/board.js";' in src, (
        "the existing render/board.js import must be left unmodified"
    )


def test_comms_js_fetch_json_import_is_not_merged_into_another_module():
    """fetchJson must not be merged into the state.js or render/board.js
    import statements - it must be its own import line."""
    src = _comms_js_source()
    assert src.count('from "./api.js"') == 1, (
        "fetchJson must be imported via exactly one import line from ./api.js"
    )
    for line in src.splitlines():
        if "fetchJson" in line and "import" in line:
            assert "state.js" not in line and "render/board.js" not in line, (
                f"fetchJson import must not be merged into another module's "
                f"import statement, got line: {line!r}"
            )


# === comms.js: updateCommsSubtitle defined & exported =====================

def test_update_comms_subtitle_defined_as_function_in_comms_js():
    src = _comms_js_source()
    assert "function updateCommsSubtitle" in src, (
        "updateCommsSubtitle must be defined as a function in comms.js"
    )


def test_comms_js_export_statement_remains_singular():
    """updateCommsSubtitle must be appended to the existing export
    statement, not introduced via a second `export { ... }` statement."""
    src = _comms_js_source()
    assert src.count("export {") == 1, (
        "comms.js must still have exactly one export statement after this "
        f"change, found {src.count('export {')}"
    )


def test_comms_js_export_statement_includes_all_four_names():
    src = _comms_js_source()
    start = src.index("export {")
    end = src.index("}", start) + 1
    block = src[start:end]
    for name in (
        "renderToolTraceHtml", "appendCommsMessage", "sendCommsMessage",
        "updateCommsSubtitle",
    ):
        assert name in block, (
            f"the export statement must include {name}, got {block!r}"
        )


def test_update_comms_subtitle_is_exported_as_a_callable_function():
    result = _run_comms_js("typeof updateCommsSubtitle")
    assert result == "function", (
        f"updateCommsSubtitle must be an exported function, got {result!r}"
    )


# === updateCommsSubtitle: happy path =======================================

_CHAT_ROLE_FETCH = (
    "() => Promise.resolve({ ok: true, status: 200, "
    "json: () => Promise.resolve({ roles: ["
    "{ role: 'planner', provider: 'ollama', model: 'glm' }, "
    "{ role: 'chat', provider: 'claude', model: 'sonnet' }"
    "] }) })"
)


def test_update_comms_subtitle_happy_path_starts_with_chat():
    result = _run_comms_js_async(
        "updateCommsSubtitle().then(() => globalThis.__commsSub.textContent)",
        fetch_impl=_CHAT_ROLE_FETCH,
    )
    assert result.startswith("chat"), (
        f"subtitle must start with 'chat', got {result!r}"
    )


def test_update_comms_subtitle_happy_path_ends_with_provider_model():
    result = _run_comms_js_async(
        "updateCommsSubtitle().then(() => globalThis.__commsSub.textContent)",
        fetch_impl=_CHAT_ROLE_FETCH,
    )
    assert result.endswith("claude/sonnet"), (
        f"subtitle must end with the resolved chat role's provider/model, "
        f"got {result!r}"
    )


def test_update_comms_subtitle_finds_chat_role_by_field_not_array_order():
    """The roles array in the happy-path fixture intentionally lists a
    non-chat role FIRST, to prove the lookup is `role === 'chat'`, not
    array index/order."""
    result = _run_comms_js_async(
        "updateCommsSubtitle().then(() => globalThis.__commsSub.textContent)",
        fetch_impl=_CHAT_ROLE_FETCH,
    )
    assert "ollama" not in result and "glm" not in result, (
        f"subtitle must reflect the 'chat' role's provider/model, not another "
        f"role's, got {result!r}"
    )


# === updateCommsSubtitle: negative / boundary cases ========================

def test_update_comms_subtitle_rejected_fetch_falls_back_to_chat():
    """A network error (rejected fetch promise) must fall back to exactly
    'chat', never leave the element blank."""
    result = _run_comms_js_async(
        "updateCommsSubtitle().then(() => globalThis.__commsSub.textContent)",
        fetch_impl="() => Promise.reject(new Error('network down'))",
    )
    assert result == "chat", f"expected exact fallback 'chat', got {result!r}"


def test_update_comms_subtitle_non_2xx_falls_back_to_chat():
    fetch_impl = (
        "() => Promise.resolve({ ok: false, status: 500, "
        "json: () => Promise.resolve({ detail: 'boom' }) })"
    )
    result = _run_comms_js_async(
        "updateCommsSubtitle().then(() => globalThis.__commsSub.textContent)",
        fetch_impl=fetch_impl,
    )
    assert result == "chat", (
        f"expected exact fallback 'chat' on non-2xx response, got {result!r}"
    )


def test_update_comms_subtitle_missing_chat_role_falls_back_to_chat():
    fetch_impl = (
        "() => Promise.resolve({ ok: true, status: 200, "
        "json: () => Promise.resolve({ roles: ["
        "{ role: 'planner', provider: 'ollama', model: 'glm' }"
        "] }) })"
    )
    result = _run_comms_js_async(
        "updateCommsSubtitle().then(() => globalThis.__commsSub.textContent)",
        fetch_impl=fetch_impl,
    )
    assert result == "chat", (
        f"expected exact fallback 'chat' when no 'chat' role entry exists, "
        f"got {result!r}"
    )


def test_update_comms_subtitle_empty_roles_array_falls_back_to_chat():
    """Boundary: an empty roles collection."""
    fetch_impl = (
        "() => Promise.resolve({ ok: true, status: 200, "
        "json: () => Promise.resolve({ roles: [] }) })"
    )
    result = _run_comms_js_async(
        "updateCommsSubtitle().then(() => globalThis.__commsSub.textContent)",
        fetch_impl=fetch_impl,
    )
    assert result == "chat", (
        f"expected exact fallback 'chat' on empty roles array, got {result!r}"
    )


def test_update_comms_subtitle_missing_roles_field_falls_back_to_chat():
    """Boundary: the config response has no `roles` field at all."""
    fetch_impl = (
        "() => Promise.resolve({ ok: true, status: 200, "
        "json: () => Promise.resolve({}) })"
    )
    result = _run_comms_js_async(
        "updateCommsSubtitle().then(() => globalThis.__commsSub.textContent)",
        fetch_impl=fetch_impl,
    )
    assert result == "chat", (
        f"expected exact fallback 'chat' when roles field is absent, got {result!r}"
    )


def test_update_comms_subtitle_malformed_roles_field_falls_back_to_chat():
    """`roles` present but not an array (malformed input) must not throw
    and must fall back to 'chat'."""
    fetch_impl = (
        "() => Promise.resolve({ ok: true, status: 200, "
        "json: () => Promise.resolve({ roles: { oops: true } }) })"
    )
    result = _run_comms_js_async(
        "updateCommsSubtitle().then(() => globalThis.__commsSub.textContent)",
        fetch_impl=fetch_impl,
    )
    assert result == "chat", (
        f"expected exact fallback 'chat' on malformed (non-array) roles, "
        f"got {result!r}"
    )


def test_update_comms_subtitle_chat_role_missing_provider_falls_back_to_chat():
    """Boundary: the chat role entry exists but is missing `provider` - must
    not render a malformed subtitle like 'chat -> undefined/sonnet'."""
    fetch_impl = (
        "() => Promise.resolve({ ok: true, status: 200, "
        "json: () => Promise.resolve({ roles: [{ role: 'chat', model: 'sonnet' }] }) })"
    )
    result = _run_comms_js_async(
        "updateCommsSubtitle().then(() => globalThis.__commsSub.textContent)",
        fetch_impl=fetch_impl,
    )
    assert result == "chat", (
        f"expected exact fallback 'chat' when provider is missing, got {result!r}"
    )


def test_update_comms_subtitle_chat_role_missing_model_falls_back_to_chat():
    fetch_impl = (
        "() => Promise.resolve({ ok: true, status: 200, "
        "json: () => Promise.resolve({ roles: [{ role: 'chat', provider: 'claude' }] }) })"
    )
    result = _run_comms_js_async(
        "updateCommsSubtitle().then(() => globalThis.__commsSub.textContent)",
        fetch_impl=fetch_impl,
    )
    assert result == "chat", (
        f"expected exact fallback 'chat' when model is missing, got {result!r}"
    )


def test_update_comms_subtitle_never_throws_uncaught_on_rejection():
    """A rejected fetch must not produce an unhandled rejection / thrown
    error that crashes the caller - updateCommsSubtitle must catch it."""
    result = _run_comms_js_async(
        "updateCommsSubtitle().then(() => 'survived')",
        fetch_impl="() => Promise.reject(new Error('boom'))",
    )
    assert result == "survived", (
        "updateCommsSubtitle must catch the fetch rejection and not propagate it"
    )


def test_update_comms_subtitle_missing_element_is_a_silent_noop():
    """If #comms-sub is not present in the DOM, updateCommsSubtitle must
    return early without throwing (guard clause), even with a fetch that
    would otherwise succeed."""
    extra_setup = "globalThis.document.getElementById = function (id) { return null; };"
    result = _run_comms_js_async(
        "updateCommsSubtitle().then(() => 'survived')",
        fetch_impl=_CHAT_ROLE_FETCH,
        extra_setup=extra_setup,
    )
    assert result == "survived", (
        "updateCommsSubtitle must not throw when #comms-sub is missing from the DOM"
    )


# === main.js: import line wiring ===========================================

def test_main_js_import_line_includes_update_comms_subtitle():
    src = _main_js_source()
    assert (
        'import { renderToolTraceHtml, appendCommsMessage, sendCommsMessage, '
        'updateCommsSubtitle, resetCommsThread } from "./comms.js";' in src
    ), (
        "main.js's existing import line from ./comms.js must be extended "
        "with updateCommsSubtitle"
    )


def test_main_js_has_exactly_one_import_from_comms_js():
    """updateCommsSubtitle must be added to the SAME existing import
    statement, not a new second import line for comms.js."""
    src = _main_js_source()
    assert src.count('from "./comms.js"') == 1, (
        "there must be exactly one import statement from ./comms.js"
    )


# === main.js: selectComms wiring ===========================================

def test_select_comms_calls_update_comms_subtitle():
    src = _main_js_source()
    body = _function_body(src, "function selectComms() {")
    assert "updateCommsSubtitle()" in body, (
        f"selectComms must call updateCommsSubtitle(), got body={body!r}"
    )


def test_select_comms_calls_update_comms_subtitle_in_correct_order():
    """updateCommsSubtitle() must be called after
    `state.selectedPlan = null;` and before `updateHash();`."""
    src = _main_js_source()
    body = _function_body(src, "function selectComms() {")
    idx_selected_plan = body.index("state.selectedPlan = null;")
    idx_update = body.index("updateCommsSubtitle()")
    idx_hash = body.index("updateHash();")
    assert idx_selected_plan < idx_update < idx_hash, (
        "updateCommsSubtitle() must be called after 'state.selectedPlan = null;' "
        f"and before 'updateHash();', got body={body!r}"
    )


def test_select_comms_does_not_await_update_comms_subtitle():
    src = _main_js_source()
    body = _function_body(src, "function selectComms() {")
    assert "await updateCommsSubtitle" not in body, (
        "selectComms must call updateCommsSubtitle() fire-and-forget, not await it"
    )


def test_select_comms_remains_a_synchronous_non_async_function():
    src = _main_js_source()
    assert "async function selectComms" not in src, (
        "selectComms must remain synchronous - it must not become async"
    )
    assert "function selectComms() {" in src, (
        "selectComms's signature must remain unchanged"
    )
