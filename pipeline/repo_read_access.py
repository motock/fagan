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

import logging
import os
import threading
import time

from pipeline.access_hooks import _record_refusal, _refusal_reason
from pipeline.remote_scope import clone_root_for
from pipeline.repo_clone import RepoUrlError, managed_path, parse_repo_url

LOG = logging.getLogger(__name__)

READ_ACTION = "read"
READ_RISK = "low"
UNIDENTIFIED_REASON = "repository_unidentified"

#: A failed first-read clone is not retried for this many seconds.
FAILED_CLONE_RETRY_SECONDS = 60.0

#: Monotonic clock for the failure back-off; tests replace it.
_clock = time.monotonic
#: Managed path -> monotonic time of its last failed first-read clone.
_FAILED_AT: dict[str, float] = {}
#: One lock per managed path, so concurrent first reads run one clone.
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


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
    _clone_on_first_read(repo_url, candidate)
    return {"root": candidate}


def _clone_on_first_read(repo_url: str, root: str) -> None:
    """Clone ``repo_url`` into ``root`` when no clone is there yet.

    Only called once every check in :func:`read_root_kwargs` has passed. An
    existing clone is left alone: a read never fetches. A failure never
    raises; the caller then answers a missing clone, as it always has.
    """
    if os.path.isdir(root):
        return
    with _lock_for(root):
        if os.path.isdir(root):
            return
        failed_at = _FAILED_AT.get(root)
        if failed_at is not None and _clock() - failed_at < FAILED_CLONE_RETRY_SECONDS:
            return
        error_type = _clone_error_type(repo_url)
        if error_type is None:
            _FAILED_AT.pop(root, None)
            return
        _FAILED_AT[root] = _clock()
        LOG.warning(
            "repository first-read clone failed",
            extra={
                "event": "repo_first_read_clone_failed",
                "repository": _repo_identity(repo_url),
                "error_type": error_type,
            },
        )


def _clone_error_type(repo_url: str) -> str | None:
    """Clone ``repo_url`` and return None, or the failure's class name.

    Only the class name leaves this function: git's stderr and the clone's
    message are never logged. ``clone_root_for`` reports its own refusals and
    git failures as an error dict; any other exception (a filesystem error
    while making parent directories, say) is caught here so it cannot raise
    out of a read.
    """
    try:
        _, err = clone_root_for(repo_url)
    except Exception as exc:  # noqa: BLE001 - a clone failure must answer a missing clone
        return type(exc).__name__
    if err is None:
        return None
    return "clone_refused"


def _lock_for(root: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(root, threading.Lock())


def _repo_identity(repo_url: str) -> str:
    """Return the ``host/owner/name`` form the access hook is given."""
    host, owner, name = parse_repo_url(repo_url)
    return f"{host}/{owner}/{name}"
