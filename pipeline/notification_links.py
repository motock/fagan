"""Dashboard links for notifications, validated and built in one place.

Stdlib only (like ``notification_render``). Both the plan-approval notice and
story-level notifications resolve their link through this module, so the
origin and path rules cannot drift apart.

Every environment variable is read on every call (never cached at import) so
an operator can fix a bad value without restarting. A rejected value is
logged by variable name only -- the value itself never reaches a log.
"""

from __future__ import annotations

import logging
import os
import re
from urllib.parse import quote, urlsplit

logger = logging.getLogger(__name__)

PUBLIC_URL_ENV = "PIPELINE_DASHBOARD_PUBLIC_URL"
LINK_PATH_ENV = "PIPELINE_APPROVAL_LINK_PATH"
STORY_LINK_PATH_ENV = "PIPELINE_STORY_LINK_PATH"

# Core ships no review page, so the default points at the dashboard root; an
# operator who supplies a UI through the dashboard extension mount sets the
# path variables to that UI's path.
_DEFAULT_LINK_PATH = "/"

# A link path is a plain path segment sequence: no scheme, no query, no
# traversal (``..``) and no empty segment (``//``).
_LINK_PATH_RE = re.compile(r"^/[A-Za-z0-9._~/-]*$")


def _origin() -> str | None:
    """Return the validated dashboard origin, or ``None`` when unusable."""
    raw = os.environ.get(PUBLIC_URL_ENV, "")
    if not raw.strip():
        return None
    parts = urlsplit(raw.strip())
    if (
        parts.scheme != "https"
        or not parts.hostname
        or "@" in parts.netloc
        or parts.query
        or parts.fragment
        or parts.path not in ("", "/")
    ):
        logger.warning(
            "Ignoring %s: it must be a bare https origin (no path, credentials, "
            "query or fragment); omitting the dashboard link",
            PUBLIC_URL_ENV,
        )
        return None
    return f"{parts.scheme}://{parts.netloc}"


def _link_path(env: str = LINK_PATH_ENV) -> str | None:
    """Return the validated path from ``env``, or ``None`` when unusable."""
    raw = os.environ.get(env, "")
    if not raw:
        return _DEFAULT_LINK_PATH
    if not _LINK_PATH_RE.match(raw) or ".." in raw or "//" in raw:
        logger.warning(
            "Ignoring %s: it must be a plain absolute path with no traversal "
            "or empty segment; omitting the dashboard link",
            env,
        )
        return None
    return raw


def story_link(plan: str, story_key: str) -> str | None:
    """Return the dashboard link to one story, or ``None`` when unconfigured."""
    origin = _origin()
    if origin is None:
        return None
    path = _link_path(STORY_LINK_PATH_ENV)
    if path is None:
        return None
    return f"{origin}{path}?plan={quote(plan, safe='')}&story={quote(story_key, safe='')}"
