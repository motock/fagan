"""Authenticated extension paths that run without a bound namespace.
PIPELINE_TENANT_DATA_EXEMPT_PATHS is a comma-separated list of dashboard-extension paths
(under /api/ext). A request to a listed path, or below one at a '/' boundary, still has to
authenticate, but core does not bind it to a namespace. Nothing is bound, so
pipeline.actor.current_namespace() would answer "default": a handler on an exempt path must
not read namespace-scoped data. Only extension paths may be listed, so core's own data routes
can never be exempted. Unset or blank exempts nothing.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from app.dashboard_mount import EXT_API_PREFIX

EXEMPT_PATHS_ENV = "PIPELINE_TENANT_DATA_EXEMPT_PATHS"
MAX_EXEMPT_PATHS = 16
_SEGMENT = re.compile(r"[a-z0-9][a-z0-9_.-]*")


class ExemptPathsError(ValueError):
    """A listed exempt path is not a valid extension path."""


def parse_exempt_paths(raw: str) -> tuple[str, ...]:
    """Parse a comma-separated list of extension paths that skip binding."""
    if raw.strip() == "":
        return ()
    entries = [entry.strip() for entry in raw.split(",")]
    # Count first: an over-long list is refused as such, whatever its entries.
    if len(entries) > MAX_EXEMPT_PATHS:
        raise ExemptPathsError(
            f"{EXEMPT_PATHS_ENV}: at most {MAX_EXEMPT_PATHS} entries"
        )
    prefix = EXT_API_PREFIX + "/"
    seen: set[str] = set()
    for index, entry in enumerate(entries, start=1):
        if not entry:
            raise ExemptPathsError(f"{EXEMPT_PATHS_ENV}: entry {index} is empty")
        if not entry.startswith(prefix):
            raise ExemptPathsError(
                f"{EXEMPT_PATHS_ENV}: entry {index} is not under {EXT_API_PREFIX}"
            )
        for part in entry[len(prefix) :].split("/"):
            if _SEGMENT.fullmatch(part) is None:
                raise ExemptPathsError(
                    f"{EXEMPT_PATHS_ENV}: entry {index} is not a valid path"
                )
        if entry in seen:
            raise ExemptPathsError(
                f"{EXEMPT_PATHS_ENV}: entry {index} repeats an earlier entry"
            )
        seen.add(entry)
    return tuple(entries)


def load_exempt_paths(environ: Mapping[str, str]) -> tuple[str, ...]:
    """Read the exempt paths from an environment mapping."""
    return parse_exempt_paths(environ.get(EXEMPT_PATHS_ENV, ""))


def is_exempt(path: str, exempt: tuple[str, ...]) -> bool:
    """Is ``path`` a listed exempt path, or below one at a '/' boundary?"""
    return any(path == p or path.startswith(p + "/") for p in exempt)
