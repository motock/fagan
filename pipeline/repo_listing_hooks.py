"""List the repositories the bound namespace may work with, for ``list_repositories``.

With ``PIPELINE_REPO_LISTING_PLUGIN`` unset the answer is built from the stored
plans: the distinct repositories of the plans the caller can see (the visibility
``list_plans`` applies), read from each plan's ``repo_url``. A plan whose only
repository identity is a local ``repo_root`` path is not a ``host/owner/name``
repository and is left out. A deployment's plugin (for example an enterprise
console) may answer instead, and its answer is checked here before anything is
returned.

The seam fails closed, as ``pipeline.access_hooks`` does. A plugin that raises,
returns something other than a list, or returns an item that is malformed or
whose ``repo`` is not ``host/owner/name`` yields ``repositories_unavailable``.
One WARNING is logged with the error type only; neither the plugin's message nor
the offending item is logged or returned.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Protocol

from app.plugin_loader import (
    load_plugin,
    plugin_spec,
    require_allowlist,
    require_methods,
)
from app.policy import can_view
from pipeline import authorization, bound_records
from pipeline.actor import current_correlation_id, current_namespace, current_principal

logger = logging.getLogger("pipeline")

REPO_LISTING_PLUGIN_ENV = "PIPELINE_REPO_LISTING_PLUGIN"
MAX_REPOSITORIES = 500
UNAVAILABLE_REASON = "repositories_unavailable"

_SEAM = "repo_listing"
_SEGMENT = r"[A-Za-z0-9_][A-Za-z0-9._-]*"
# ``host/owner/name``: no scheme, credentials, port, query or whitespace.
_REPO_RE = re.compile(rf"[A-Za-z0-9][A-Za-z0-9.-]*/{_SEGMENT}/{_SEGMENT}")


class _MalformedListing(ValueError):
    """The listing is not a list of valid repository items."""


class RepoListingHook(Protocol):
    def list_repositories(self, namespace: str) -> list[dict[str, Any]]: ...


def list_repositories_result(store: Any) -> dict[str, Any]:
    """Return the ``list_repositories`` answer for the namespace bound to the call."""
    try:
        items = _listing(store)
    except Exception as exc:  # noqa: BLE001 - any failure to list must fail closed
        logger.warning(
            "repository listing failed; failing closed",
            extra={"error_type": type(exc).__name__},
        )
        return {
            "ok": False,
            "reason": UNAVAILABLE_REASON,
            "correlation_id": current_correlation_id(),
        }
    result: dict[str, Any] = {"ok": True, "repositories": items[:MAX_REPOSITORIES]}
    if len(items) > MAX_REPOSITORIES:
        result["truncated"] = True
    return result


def _listing(store: Any) -> list[dict[str, Any]]:
    """Return the checked, sorted items; raise on any failure to produce them."""
    hook = _hook()
    if hook is None:
        raw: Any = _visible_repositories(store)
    else:
        raw = hook.list_repositories(current_namespace())
    return sorted(_checked(raw), key=lambda item: item["repo"])


def _checked(raw: Any) -> list[dict[str, Any]]:
    """Return *raw* as normalised items, or raise when any item is malformed."""
    if not isinstance(raw, list):
        raise _MalformedListing("repository listing is not a list")
    items = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise _MalformedListing("repository item is not a mapping")
        repo = entry.get("repo")
        level = entry.get("level")
        state = entry.get("state")
        if not isinstance(repo, str) or not _REPO_RE.fullmatch(repo):
            raise _MalformedListing("repository item has no valid repo")
        if level is not None and not isinstance(level, str):
            raise _MalformedListing("repository level is not a string")
        if state is not None and not isinstance(state, str):
            raise _MalformedListing("repository state is not a string")
        items.append({"repo": repo, "level": level, "state": state})
    return items


def _visible_repositories(store: Any) -> list[dict[str, Any]]:
    """Return the distinct repositories of the caller's visible plans."""
    principal = current_principal()
    found: dict[str, dict[str, Any]] = {}
    for plan_name in bound_records.list_plans(store):
        manifest = bound_records.get_manifest(store, plan_name)
        if not manifest:
            continue
        if not can_view(principal, authorization.resource_for(manifest, plan_name, None)):
            continue
        repo = _listed_repo(manifest)
        if repo is not None:
            found[repo] = {"repo": repo, "level": None, "state": None}
    return list(found.values())


def _listed_repo(manifest: dict[str, Any]) -> str | None:
    """Return the plan's ``host/owner/name`` repository, or None when it has none."""
    from .ci import _repo_identity  # lazy, as pipeline.access_hooks does

    try:
        repo = _repo_identity(manifest)
    except Exception:  # noqa: BLE001 - a plan with no readable repository lists nothing
        return None
    return repo if _REPO_RE.fullmatch(repo) else None


def _hook() -> RepoListingHook | None:
    """Return the configured listing hook, or None when no plugin is selected.

    The allowlist is checked before the plugin is imported, so a refused spec
    never runs. Any error here propagates to ``list_repositories_result``, which
    fails closed.
    """
    if plugin_spec(_SEAM) is None:
        return None
    require_allowlist(_SEAM)
    hook = load_plugin(_SEAM)()
    require_methods(hook, ("list_repositories",), "repo listing hook")
    return hook
