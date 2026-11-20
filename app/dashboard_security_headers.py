"""Fixed security response headers for every dashboard response.

The index page carries the shared-secret API key in an inline script and the
UI can trigger state-changing requests, so the page must never render inside
another site's frame and a response must never be content-sniffed.
"""
from __future__ import annotations

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def install(app) -> None:
    """Add the fixed headers to every response ``app`` sends."""

    @app.middleware("http")
    async def _security_headers(request, call_next):
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response
