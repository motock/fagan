"""Chat tools that reach the team's other connected repositories (xrp-01).

A planner lists the connected repositories and reads any of them, so it can
find a cross-repository dependency before it writes a plan. Nothing here adds
an access path: every read goes through the dashboard routes
``GET /api/workspace/{file,files,search}`` with a ``repo_url``, which apply the
host allow-list, root containment and the namespace's read check, using the
caller's own credential. A plan saved from the chat still targets the
repository the turn names; these tools never change it.

``app.chat`` merges ``OTHER_REPO_TOOLS`` into ``TOOLS``. ``app.chat`` is
imported lazily here (for ``_resolve_tool_url``) to avoid an import cycle.
"""

from __future__ import annotations

import re

from pipeline.repo_clone import RepoUrlError, parse_repo_url

#: ``host/owner/name`` as ``list_repositories`` returns it. Checked in full
#: before any URL is built; ``parse_repo_url`` then applies the host allow-list.
_REPO_SHAPE = re.compile(r"[A-Za-z0-9.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
REPO_FORMAT_ERROR = "repo must be host/owner/name"


def _repo_read_params(params: dict, repo_url: str | None) -> dict:
    """Return a read tool's query params, with repo_url only when the turn has one."""
    if not repo_url:
        return params
    return {**params, "repo_url": repo_url}


def _repo_url_for(repo) -> str | None:
    """Return ``https://`` + repo when repo names a repository the clone layer accepts, else None."""
    if not isinstance(repo, str) or not _REPO_SHAPE.fullmatch(repo):
        return None
    url = "https://" + repo
    try:
        parse_repo_url(url)
    except RepoUrlError:
        return None
    return url


def _get_route(http_client, api_base_url: str, route: str, params):
    from app.chat import _resolve_tool_url

    return http_client.get(_resolve_tool_url(http_client, api_base_url, route), params=params).json()


def _list_repositories(http_client, api_base_url, **kwargs):
    return _get_route(http_client, api_base_url, "/api/repositories", None)


def _read_repository_file(http_client, api_base_url, repo, path, **kwargs):
    repo_url = _repo_url_for(repo)
    if repo_url is None:
        return {"error": REPO_FORMAT_ERROR}
    return _get_route(http_client, api_base_url, "/api/workspace/file", _repo_read_params({"path": path}, repo_url))


def _list_repository_directory(http_client, api_base_url, repo, path='', **kwargs):
    repo_url = _repo_url_for(repo)
    if repo_url is None:
        return {"error": REPO_FORMAT_ERROR}
    return _get_route(http_client, api_base_url, "/api/workspace/files", _repo_read_params({"path": path}, repo_url))


def _search_repository(http_client, api_base_url, repo, pattern, **kwargs):
    repo_url = _repo_url_for(repo)
    if repo_url is None:
        return {"error": REPO_FORMAT_ERROR}
    return _get_route(http_client, api_base_url, "/api/workspace/search", _repo_read_params({"pattern": pattern}, repo_url))


OTHER_REPO_TOOLS: dict[str, dict] = {
    "list_repositories": {
        "description": (
            "List the repositories this team has connected. Before drafting a plan that changes an interface,"
            " a version or a shared contract, call this and search the other repositories for code that depends"
            " on it; if one does, say so, draft a separate plan for that repository, copy the exact contract into"
            " that plan's story briefs, and set blocked_by on its stories"
        ),
        "params": {},
        "execute": _list_repositories,
    },
    "read_repository_file": {
        "description": "Read a file by relative path in another connected repository (repo is host/owner/name from list_repositories).",
        "params": {"repo": "str", "path": "str"},
        "execute": _read_repository_file,
    },
    "list_repository_directory": {
        "description": "List the immediate contents of a directory by relative path in another connected repository (repo is host/owner/name from list_repositories); non-recursive.",
        "params": {"repo": "str", "path": "str"},
        "execute": _list_repository_directory,
    },
    "search_repository": {
        "description": "Search for a text pattern (grep-style) in another connected repository (repo is host/owner/name from list_repositories) and return matching lines with file and line number.",
        "params": {"repo": "str", "pattern": "str"},
        "execute": _search_repository,
    },
}
