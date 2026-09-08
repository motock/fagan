"""Server-side injection of the dashboard shared-secret API key into index.html.

Story: the route that serves static/index.html must read the file's text,
str.replace() the ``<!--PIPELINE_API_KEY-->`` placeholder with
``<script>window.__PIPELINE_API_KEY__="<key>";</script>``, and serve the
result. The browser side (static/app/api.js fetchJson/postJson, plus every
raw ``fetch(`` call site) must attach the key as the ``X-Pipeline-Api-Key``
header.

This repo has no configured JS test runner (static/package.json is only
``{"type":"module"}`` with no test script and there is no jest/vitest config),
so per the story brief the JS behavior is graded here via static source
assertions on the .js files, and the serving behavior via a FastAPI
TestClient against app.dashboard.

Note: the interactive browser path (open dashboard, chat/dispatch still work)
cannot be covered by an automated test in this repo; it must be smoke-tested
manually in a browser and recorded in the PR description.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth import get_or_create_api_key

REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = REPO_ROOT / "static"
INDEX_PATH = STATIC_DIR / "index.html"
API_JS_PATH = STATIC_DIR / "app" / "api.js"
DASHBOARD_PY = REPO_ROOT / "app" / "dashboard.py"
REQUIREMENTS_TXT = REPO_ROOT / "requirements.txt"

MARKER = "<!--PIPELINE_API_KEY-->"
HEADER = "X-Pipeline-Api-Key"
GLOBAL_NAME = "window.__PIPELINE_API_KEY__"


def _client() -> TestClient:
    from app.dashboard import app

    return TestClient(app)


def _auth_headers() -> dict[str, str]:
    """Header map carrying the real shared secret from get_or_create_api_key()."""
    return {HEADER: get_or_create_api_key()}


def _index_text() -> str:
    return INDEX_PATH.read_text()


def _fn_body(source: str, name: str) -> str:
    """Return the source text of ``async function <name>(...)`` in api.js."""
    match = re.search(rf"async function {name}\(", source)
    assert match, f"async function {name}() missing from static/app/api.js"
    rest = source[match.end():]
    nxt = re.search(r"\n(?:async function|function|export)\b", rest)
    return rest[: nxt.start()] if nxt else rest


# --------------------------------------------------------------------------
# Serving: the key is injected server-side into the served HTML
# --------------------------------------------------------------------------

def test_index_serves_html_with_key_injected():
    key = get_or_create_api_key()
    resp = _client().get("/", headers=_auth_headers())
    assert resp.status_code == 200, resp.text
    assert "text/html" in resp.headers.get("content-type", "")

    body = resp.text
    # Exact injection format from the story brief.
    script = f'<script>{GLOBAL_NAME}="{key}";</script>'
    assert script in body, (
        "served index.html must contain the injected "
        f"<script>{GLOBAL_NAME}=...</script> tag"
    )
    # Injected exactly once, and the raw placeholder is fully replaced.
    assert body.count(script) == 1
    assert MARKER not in body, "placeholder marker must not survive into the served HTML"
    assert body.count(key) == 1


def test_served_body_equals_static_file_with_marker_replaced():
    """The mechanism is plain str.replace() on the static file's text."""
    key = get_or_create_api_key()
    resp = _client().get("/", headers=_auth_headers())
    assert resp.status_code == 200
    expected = _index_text().replace(MARKER, f'<script>{GLOBAL_NAME}="{key}";</script>')
    assert resp.text.strip() == expected.strip(), (
        "served HTML must be static/index.html with <!--PIPELINE_API_KEY--> "
        "replaced by the key script tag (plain str.replace, no other rewrite)"
    )


def test_placeholder_marker_exists_in_source_before_head_close():
    """static/index.html carries the <!--PIPELINE_API_KEY--> marker before </head>."""
    text = _index_text()
    assert MARKER in text, f"static/index.html must contain the {MARKER} marker"
    assert text.index(MARKER) < text.index("</head>"), (
        "the marker must sit before </head> so the injected script lands in <head>"
    )


# --------------------------------------------------------------------------
# Serving: auth still applies globally to the dashboard route
# --------------------------------------------------------------------------

def test_index_without_key_header_is_rejected():
    resp = _client().get("/")
    assert resp.status_code == 401


def test_index_with_wrong_key_is_rejected():
    resp = _client().get("/", headers={HEADER: "not-the-real-key"})
    assert resp.status_code == 401


def test_index_with_empty_key_is_rejected():
    resp = _client().get("/", headers={HEADER: ""})
    assert resp.status_code == 401


def test_other_static_assets_still_served_with_valid_key():
    """Only the index route is rewritten; the StaticFiles mount still works."""
    resp = _client().get("/app/api.js", headers=_auth_headers())
    assert resp.status_code == 200


# --------------------------------------------------------------------------
# static/app/api.js: fetchJson and postJson attach the header
# --------------------------------------------------------------------------

def test_api_js_fetchjson_reads_global_and_attaches_header():
    body = _fn_body(API_JS_PATH.read_text(), "fetchJson")
    assert GLOBAL_NAME in body, "fetchJson must read window.__PIPELINE_API_KEY__"
    assert HEADER in body, "fetchJson must send the X-Pipeline-Api-Key header"


def test_api_js_postjson_reads_global_and_attaches_header():
    body = _fn_body(API_JS_PATH.read_text(), "postJson")
    assert GLOBAL_NAME in body, "postJson must read window.__PIPELINE_API_KEY__"
    assert HEADER in body, "postJson must send the X-Pipeline-Api-Key header"


def test_api_js_still_exports_the_original_helpers():
    """Existing exports survive the edit (membership, not exact list)."""
    source = API_JS_PATH.read_text()
    export_match = re.search(r"export\s*\{([^}]*)\}", source)
    assert export_match, "api.js must keep its export statement"
    exported = export_match.group(1)
    for name in ("fetchJson", "postJson", "fetchPlanMetrics", "fetchGuardLiveness"):
        assert name in exported, f"api.js must still export {name}"


# --------------------------------------------------------------------------
# Raw fetch( call sites outside api.js attach the header too
# --------------------------------------------------------------------------

RAW_FETCH = re.compile(r"\bfetch\(")


KNOWN_APP_JS = [
    "api.js",
    "comms.js",
    "main.js",
    "workspace.js",
    "render/story-modal.js",
]


def test_known_fetch_call_site_files_exist():
    """Sanity guard so the header tests below cannot pass vacuously."""
    for rel in KNOWN_APP_JS:
        assert (STATIC_DIR / "app" / rel).exists(), f"missing static/app/{rel}"


def test_every_file_with_raw_fetch_attaches_the_header():
    """Every raw ``fetch(`` call site must send the key header.

    A file satisfies this either by attaching X-Pipeline-Api-Key directly at
    the raw call site (the story's prescribed edit) or by no longer making raw
    calls at all (routed through fetchJson/postJson, which attach the header).
    api.js is excluded: its raw fetches ARE fetchJson/postJson, graded above.
    """
    offenders = []
    for path in sorted((STATIC_DIR / "app").rglob("*.js")):
        if path.name == "api.js":
            continue
        text = path.read_text()
        if RAW_FETCH.search(text) and HEADER not in text:
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, (
        "files with raw fetch( calls that do not attach the "
        f"{HEADER} header (and are not routed through fetchJson/postJson): "
        f"{offenders}"
    )


def test_comms_js_chat_call_is_covered():
    """The named call site: static/app/comms.js /api/chat raw fetch."""
    text = (STATIC_DIR / "app" / "comms.js").read_text()
    # Either the raw call was edited directly to attach the header, or it was
    # routed through fetchJson/postJson (which attach it) and no raw call remains.
    assert HEADER in text or not RAW_FETCH.search(text), (
        "static/app/comms.js /api/chat fetch must send the X-Pipeline-Api-Key header"
    )


# --------------------------------------------------------------------------
# Dependency audit: no Jinja2 added for one variable
# --------------------------------------------------------------------------

def test_no_jinja2_dependency_added_for_the_injection():
    req = REQUIREMENTS_TXT.read_text().lower()
    if "jinja2" in req:
        pytest.skip("jinja2 was already a project dependency before this story")
    assert "jinja2" not in DASHBOARD_PY.read_text().lower(), (
        "the index.html injection must use plain str.replace templating, "
        "not a newly added Jinja2 dependency"
    )