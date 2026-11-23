"""Decide whether a repository read may serve the clone a ``repo_url`` names.

The dashboard's ``GET /api/workspace/...`` routes read the managed clone of a
repository. ``read_root_kwargs`` is the resolver they use: it applies the host
allow-list and root containment, then asks the repository access hook
(``pipeline.access_hooks``) whether the bound namespace may read that
repository. A read with no ``repo_url`` names the active workspace and is not
checked here.

Refusals are raised as two exceptions so the HTTP layer can answer each with its
own status. Neither carries the URL, the path or the hook's free text.
"""

from __future__ import annotations

import os

from pipeline.access_hooks import _record_refusal, _refusal_reason
from pipeline.repo_clone import RepoUrlError, managed_path, parse_repo_url

READ_ACTION = "read"
READ_RISK = "low"
UNIDENTIFIED_REASON = "repository_unidentified"


class RepoUrlNotAllowed(Exception):
    """The ``repo_url`` failed the host allow-list or root containment."""


class RepoReadRefused(Exception):
    """The repository access hook refused a read of the repository."""


def read_refusal(repo_url: str) -> str | None:
    """Return the refusal reason token for a read of ``repo_url``, else None.

    With no ``PIPELINE_REPO_ACCESS_PLUGIN`` selected there is no hook and the
    read may proceed. A repository identity that cannot be derived is refused
    before the hook is consulted, and that refusal is audited.
    """
    try:
        repo = _repo_identity(repo_url)
    except RepoUrlError:
        return _record_refusal(READ_ACTION, UNIDENTIFIED_REASON)
    return _refusal_reason(READ_ACTION, {"risk": READ_RISK}, lambda: repo)


def read_root_kwargs(repo_url: str | None) -> dict:
    """Name the managed clone a read serves; empty when no repo_url is given.

    ``managed_path`` is the first resolver: it applies the allow-list and the
    root-containment check. A URL that passes is then checked against the
    access hook before its clone is named.
    """
    if repo_url is None:
        return {}
    try:
        path = managed_path(repo_url)
        base = os.path.normpath(os.path.realpath(os.environ["PIPELINE_REPOS_ROOT"]))
    except (RepoUrlError, KeyError):
        raise RepoUrlNotAllowed from None
    # Re-checked here, where the sanitiser is visible to CodeQL; managed_path's
    # own containment check is the first line of defence.
    candidate = os.path.normpath(os.path.realpath(str(path)))
    if not candidate.startswith(base + os.sep):
        raise RepoUrlNotAllowed
    if read_refusal(repo_url) is not None:
        raise RepoReadRefused
    return {"root": candidate}


def _repo_identity(repo_url: str) -> str:
    """Return the ``host/owner/name`` form the access hook is given."""
    host, owner, name = parse_repo_url(repo_url)
    return f"{host}/{owner}/{name}"
