"""Shared-secret API key gate for the dashboard HTTP API.

The dashboard is a single-operator local tool, so authentication is a single
shared secret held in a 0600 file at the repo root rather than a user store.
`require_api_key` is invoked from `app/dashboard.py`'s HTTP middleware for
every request under the "/api/" prefix, so it covers that surface
uniformly — there is no per-route opt-in. The index route ("/") and static
assets are deliberately exempt: "/" is what hands the key to the browser in
the first place, so gating it behind the same header would be an
unreachable chicken-and-egg lock, and static assets carry no secrets.
"""
from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from fastapi import Header, HTTPException

# Same repo-root resolution as pipeline/workspace.py's REPO_ROOT.
REPO_ROOT = Path(__file__).resolve().parent.parent
API_KEY_PATH = REPO_ROOT / ".dashboard_api_key"

logger = logging.getLogger(__name__)


def get_or_create_api_key() -> str:
    """Return the dashboard's shared secret, generating it on first use."""
    if API_KEY_PATH.exists():
        # Tighten an existing file too: O_CREAT's mode applies only when the
        # file is created, so a key written before this control existed (or
        # by a looser umask) would otherwise stay group/world readable.
        os.chmod(API_KEY_PATH, 0o600)
        return API_KEY_PATH.read_text().strip()

    token = secrets.token_urlsafe(32)
    # Created 0600 up front rather than written and then tightened: a plain
    # write_text() would briefly expose the secret at the umask default.
    fd = os.open(API_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(token)
    os.chmod(API_KEY_PATH, 0o600)
    return token


def require_api_key(x_pipeline_api_key: str | None = Header(default=None)) -> None:
    """Reject any request whose X-Pipeline-Api-Key header is absent or wrong.

    Compared with `secrets.compare_digest` (never `==`) so the response time
    does not leak how much of the key a caller guessed correctly. The 401
    detail is fixed and generic: it must not reveal whether the header was
    missing or merely wrong.

    Kept as a plain callable (rather than a FastAPI `Depends`-only dependency)
    so `app/dashboard.py`'s middleware can call it directly with a header
    value read off the request; the `Header(default=None)` default still
    lets it be used as a `Depends` elsewhere (e.g. in tests) if needed.
    """
    expected = get_or_create_api_key()
    presented = x_pipeline_api_key or ""
    # Compare as bytes: compare_digest rejects non-ASCII str, and a header
    # can carry any latin-1 byte — a TypeError here would surface as a 500
    # rather than a denial.
    if not secrets.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
        logger.warning(
            "Dashboard API key rejected",
            extra={"header_present": x_pipeline_api_key is not None},
        )
        raise HTTPException(status_code=401, detail="unauthorized")
