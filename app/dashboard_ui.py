"""Serving the dashboard UI: the index handler and the static catch-all.

The index handler is the one place where the shared-secret API key is handed
to the browser, so serving the UI is a mode of its own: with
``PIPELINE_DASHBOARD_UI=off`` the dashboard is API-only and registers no UI
route at all -- ``/`` and every static asset answer 404, and the key is never
injected into anything. Any value other than ``on``/``off`` is refused at
start-up: a typo must not silently serve (or silently withhold) the UI.
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


def ui_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the dashboard should serve its UI; refuses anything unclear.

    ``environ`` exists so callers can grade a hypothetical environment without
    mutating ``os.environ``; it defaults to ``os.environ`` when ``None``. An
    empty mapping is a valid "nothing is configured" -- so the
    ``environ or os.environ`` idiom would be wrong here -- and it means the UI
    is on, exactly like an unset variable. The mapping is only read, never
    written back: normalising the caller's environment would be a surprise.
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


def build_index_handler(
    static_dir: Path, read_key: Callable[[], str]
) -> Callable[[], HTMLResponse]:
    """Build the ``GET /`` handler: static/index.html with the API key injected."""
    def dashboard_index() -> HTMLResponse:
        """Serve static/index.html with the shared-secret API key injected.

        The browser cannot know the key on its own, so the server reads the
        static file's text at request time and str.replace()s the
        ``<!--PIPELINE_API_KEY-->`` placeholder with a small inline script that
        publishes the key as ``window.__PIPELINE_API_KEY__``. The key store is
        persistent: every request serves the same stored key, so already-open
        dashboard tabs keep working. The key is never logged.
        """
        key = read_key()
        html_text = (static_dir / "index.html").read_text()
        script = f"<script>window.__PIPELINE_API_KEY__={json.dumps(key)};</script>"
        return HTMLResponse(content=html_text.replace("<!--PIPELINE_API_KEY-->", script))

    return dashboard_index


def register_index(
    app: FastAPI,
    static_dir: Path,
    read_key: Callable[[], str],
    environ: Mapping[str, str] | None = None,
) -> None:
    """Register ``GET /`` on ``app`` -- and only when the UI is enabled.

    Nothing is registered when the UI is disabled: API-only mode cannot leak
    the key through a leftover route, because there is no route.

    The catch-all mount deliberately lives with the app, not here: it must be
    the LAST registration on ``app``, after every ``/api/`` and ``/auth/``
    route, and the host module is the only place that knows that order. This
    handler must therefore be registered before that mount, which would
    otherwise serve ``static/index.html`` itself and never inject the key.

    ``read_key`` is called per request rather than here: the key is created
    lazily and can be rotated, so freezing it at import (or at registration)
    would serve a stale secret.
    """
    if not ui_enabled(environ):
        return

    app.get("/", response_class=HTMLResponse, include_in_schema=False)(
        build_index_handler(static_dir, read_key)
    )
