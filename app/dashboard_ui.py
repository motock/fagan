"""Serving the dashboard UI: the index response and the ``GET /`` registration.

The index page is the one place where the shared-secret API key is handed to
the browser, so serving the UI is a mode of its own: with
``PIPELINE_DASHBOARD_UI=off`` the dashboard is API-only and registers no UI
route at all -- ``/`` and every static asset answer 404, and the key is never
injected into anything. Any value other than ``on``/``off`` is refused at
start-up: a typo must not silently serve (or silently withhold) the UI.

The static catch-all mount deliberately does NOT live here: it must be the LAST
registration on the app, after every ``/api/`` and ``/auth/`` route, and the
host module is the only place that knows that order. ``ui_enabled()`` is
therefore evaluated once by the host and passed down, so the index and the
catch-all can never be judged two different ways.
"""
from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

UI_ENV = "PIPELINE_DASHBOARD_UI"

_ON = "on"
_OFF = "off"

# ``json.dumps`` produces correct JSON, which is not enough inside an inline
# <script>: a key containing ``</script>`` or ``<!--`` would break out of the
# element and run as markup on the dashboard origin (which holds the key). The
# key is normally ``token_urlsafe``, but the read path returns the contents of
# the key file verbatim, so escaping the HTML-significant characters is cheap
# defence in depth. The \uXXXX forms stay valid JSON *and* valid JS.
_SCRIPT_ESCAPES = str.maketrans({"<": "\\u003c", ">": "\\u003e", "&": "\\u0026"})


def ui_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the dashboard should serve its UI; refuses anything unclear.

    ``environ`` exists so callers can grade a hypothetical environment without
    mutating ``os.environ``; it defaults to ``os.environ`` when ``None``. An
    empty mapping is a valid "nothing is configured" -- so the
    ``environ or os.environ`` idiom would be wrong here -- and it means the UI
    is on, exactly like an unset variable. That default is backwards
    compatibility: existing dashboards serve the UI with no configuration, so
    ``PIPELINE_DASHBOARD_UI=off`` is the explicit opt-in to API-only mode. The
    mapping is only read, never written back: normalising the caller's
    environment would be a surprise.
    """
    source = os.environ if environ is None else environ
    raw = (source.get(UI_ENV) or "").strip().lower()
    if raw in ("", _ON):
        return True
    if raw == _OFF:
        return False
    # Never echo the value: the variable is operator-supplied text, and this
    # runs at start-up, not on a request.
    raise ValueError(f"{UI_ENV} must be on or off")


def index_response(static_dir: Path, read_key: Callable[[], str]) -> HTMLResponse:
    """Serve static/index.html with the shared-secret API key injected.

    The browser cannot know the key on its own, so the server reads the
    static file's text at request time and str.replace()s the
    ``<!--PIPELINE_API_KEY-->`` placeholder with a small inline script that
    publishes the key as ``window.__PIPELINE_API_KEY__``. The key store is
    persistent: every request serves the same stored key, so already-open
    dashboard tabs keep working. The key is never logged.

    ``read_key`` is called per request rather than by the caller: the key is
    created lazily and can be rotated, so freezing it at import (or at
    registration) would serve a stale secret.
    """
    key = read_key()
    html_text = (static_dir / "index.html").read_text()
    script = (
        "<script>window.__PIPELINE_API_KEY__="
        + json.dumps(key).translate(_SCRIPT_ESCAPES)
        + ";</script>"
    )
    return HTMLResponse(content=html_text.replace("<!--PIPELINE_API_KEY-->", script))


def register_index(
    app: FastAPI, handler: Callable[[], HTMLResponse], enabled: bool
) -> None:
    """Register ``handler`` as ``GET /`` -- and only when the UI is enabled.

    Nothing is registered when the UI is disabled: API-only mode cannot leak
    the key through a leftover route, because there is no route.

    ``enabled`` is the single decision the host module took once with
    :func:`ui_enabled`; taking it as an argument is what keeps the index and
    the static catch-all on the same verdict. The caller registers this before
    its catch-all mount, which would otherwise serve ``static/index.html``
    itself and never inject the key.
    """
    if not enabled:
        return

    app.get("/", response_class=HTMLResponse, include_in_schema=False)(handler)
