"""The forge protocol: the seam between the pipeline and its code host.

A *forge* is the code host that owns pull requests: GitHub today, GitLab
later. Every PR verb the pipeline needs is declared here as a
:class:`typing.Protocol`, so the rest of the pipeline depends on the shape of
the seam rather than on ``gh``'s argv. :func:`get_forge` resolves the active
adapter from ``PIPELINE_FORGE`` (or, under ``auto``, the origin remote's
hostname) and caches one instance per repo root.

This module deliberately holds no adapter implementation: importing an
adapter here would be a cycle, because ``pipeline/pr.py`` imports this module
at import time. :func:`get_forge` therefore imports the adapter modules
function-locally.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from logging import getLogger
from typing import Any, Protocol

logger = getLogger(__name__)

# ---------------------------------------------------------------------------
# Canonical CI states
# ---------------------------------------------------------------------------
# One vocabulary for every CI reader. ``UNREADABLE`` and ``NONE`` are
# deliberately distinct members: "CI exists but its status could not be read"
# must never be mistaken for "this repo has no CI", because the merge gate
# treats ``none`` as permission to merge.
PASS = "pass"
FAIL = "fail"
PENDING = "pending"
CANCELLED = "cancelled"
UNREADABLE = "unreadable"
NONE = "none"

CI_STATES = frozenset({PASS, FAIL, PENDING, CANCELLED, UNREADABLE, NONE})


class CIStatusUnreadable(Exception):
    """CI is configured, but its status could not be read.

    Adapters raise this instead of returning a state: the caller maps it to
    :data:`UNREADABLE` (never :data:`NONE`), so "could not read" can never be
    mistaken for "no CI configured" - the latter permits merging without
    checks, the former refuses the merge.
    """


class CISnapshot(dict):
    """One CI snapshot: ``{check_name: canonical_state}`` plus raw detail.

    A plain ``dict`` subclass, so an adapter's snapshot is consumed as the
    ``dict[str, str]`` the protocol promises. ``detail`` carries the host's
    own vocabulary (``"Lint: failure"``) for the rework feedback the merge
    gate surfaces; ``excerpt`` carries an optional bounded log tail. Both are
    presentation only - the state machine reads the mapping.
    """

    detail: str = ""
    excerpt: str | None = None

    def __init__(self, mapping=None, *, detail: str = "", excerpt=None):
        super().__init__(mapping or {})
        self.detail = detail
        self.excerpt = excerpt

_FORGE_ENV = "PIPELINE_FORGE"
_DEFAULT_FORGE = "github"
_KNOWN_FORGES = ("github", "gitlab")

# ``scheme://`` (optional) then ``user@`` (optional) then the host.
_HOST_RE = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*://)?(?:[^@/]+@)?([^/:]+)")


class Forge(Protocol):
    """The PR verbs the pipeline needs from a code host.

    ``checks_status`` returns ONE snapshot and never polls: the caller owns
    the retry loop. ``pr_state`` takes the opaque handle ``open_pr`` returned,
    so no caller outside the adapter parses a PR URL.
    """

    validates_pre_merge: bool
    supports_comments: bool

    def open_pr(self, worktree: str, branch: str, title: str, body: str) -> str:
        """Push ``branch`` and open a PR; return an opaque handle."""
        ...

    def update_pr_title(self, worktree: str, branch: str, title: str) -> None:
        """Set the PR title; best-effort, never raises."""
        ...

    def comment_pr(self, worktree: str, body: str) -> None:
        """Post ``body`` as a comment on the branch's PR."""
        ...

    def merge_pr(self, worktree: str, branch: str, subject: str) -> str:
        """Squash-merge the PR using ``subject`` as the commit subject."""
        ...

    def pr_title(self, worktree: str, branch: str) -> str:
        """The PR's current title, or "" when it cannot be read."""
        ...

    def pr_state(self, handle: str) -> str:
        """One of MERGED | OPEN | CLOSED | UNKNOWN for ``handle``."""
        ...

    # -- CI verbs ---------------------------------------------------------
    # The poll loop and the state machine live above the adapter (in
    # ``pipeline/ci.py``); an adapter answers exactly one question per call.
    def checks_status(self, branch: str, sha: str) -> dict[str, str]:
        """ONE CI snapshot for ``branch``/``sha``; never polls.

        Kept as the S1-era name; it delegates to :meth:`checks_snapshot` and
        returns the bare ``{check_name: canonical_state}`` mapping.
        """
        ...

    def has_ci_config(self, repo_root: str) -> bool:
        """True when the repo declares CI configuration.

        This is what keeps :data:`UNREADABLE` from collapsing into
        :data:`NONE`: a repo that declares CI but whose status cannot be read
        must refuse the merge, while a repo with no CI may merge without
        checks.
        """
        ...

    def checks_snapshot(self, branch: str, sha: str) -> CISnapshot:
        """ONE CI snapshot for ``branch``/``sha``; never polls.

        Returns ``{check_name: canonical_state}``. Raises
        :class:`CIStatusUnreadable` when the status cannot be read or its
        vocabulary is unrecognised. An empty snapshot means "CI is configured
        but no checks have registered yet" - the caller treats that as
        :data:`PENDING`, never :data:`NONE`.
        """
        ...

    def failure_excerpt(self, branch: str) -> str | None:
        """A bounded failing-test log excerpt, or None."""
        ...

    def rerun_failed(self, sha: str) -> bool:
        """Rerun the failed jobs for ``sha``; False on any failure."""
        ...

    def prepare_merge(self, worktree: str, branch: str) -> dict[str, Any]:
        """Rebase ``branch`` onto the default branch and force-push it.

        Returns ``{"ok", "error", "pushed_sha", "auto_resolved"}``; ``error``
        is the bare ``rebase: ...`` / ``push: ...`` text, never wrapped.
        """
        ...
# ---------------------------------------------------------------------------
# Remote probe
# ---------------------------------------------------------------------------
# Bound at import time on purpose. The probe is a real ``git`` call about the
# repository, not part of the PR-verb seam: tests that patch
# ``subprocess.run`` to observe the gh/git argv of the PR verbs must not see
# (or count) this probe.
_RUN = subprocess.run


def _remote_url(repo_root: str) -> str | None:
    """The origin remote URL for ``repo_root``, or None when unreadable."""
    try:
        proc = _RUN(
            ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
            check=False, capture_output=True, text=True,
        )
    except OSError:
        return None
    if proc is None or proc.returncode != 0:
        return None
    url = (proc.stdout or "").strip()
    return url or None


def _hostname(url: str) -> str | None:
    """The hostname of a git remote URL (ssh, scp-like or https)."""
    match = _HOST_RE.match(url.strip())
    return match.group(1).lower() if match else None


def resolve_forge_name(repo_root: str) -> str:
    """Resolve the active forge name from ``PIPELINE_FORGE``.

    An explicit ``github``/``gitlab`` wins and never pays for a remote probe.
    ``auto`` (the default) maps the origin remote's hostname; an unrecognised
    host, or a missing/unreadable remote, falls back to ``github`` with
    exactly one WARNING naming the host.
    """
    choice = os.environ.get(_FORGE_ENV, "auto").strip().lower()
    if choice in _KNOWN_FORGES:
        return choice
    if choice != "auto":
        raise ValueError(
            f"Unknown {_FORGE_ENV}={choice!r}; expected one of "
            f"'auto', {sorted(_KNOWN_FORGES)}."
        )

    url = _remote_url(repo_root)
    host = _hostname(url) if url else None
    if host and "gitlab" in host:
        return "gitlab"
    if host and "github" in host:
        return "github"

    logger.warning(
        "Unrecognised forge remote %r (host %r); defaulting to %s.",
        url, host, _DEFAULT_FORGE,
        extra={"repo_root": str(repo_root), "remote_host": host or ""},
    )
    return _DEFAULT_FORGE


# ---------------------------------------------------------------------------
# Adapter registry + per-repo_root cache
# ---------------------------------------------------------------------------
# Registering an adapter is one dict entry. The dict is filled lazily, inside
# get_forge, because importing an adapter at module scope would be a cycle.
_ADAPTERS: dict[str, type] = {}

_CACHE: dict[str, Forge] = {}
_CACHE_LOCK = threading.Lock()


def _register_adapters() -> None:
    """Populate ``_ADAPTERS`` on first use (function-local import: no cycle)."""
    if _ADAPTERS:
        return
    from .forge_github import GitHubForge
    from .forge_gitlab import GitLabForge

    _ADAPTERS.setdefault("github", GitHubForge)
    _ADAPTERS.setdefault("gitlab", GitLabForge)


def get_forge(repo_root: str) -> Forge:
    """The forge adapter for ``repo_root``, cached per repo root.

    The same ``repo_root`` returns the identical instance and the remote is
    read at most once per repo root per process; distinct roots get distinct
    instances.
    """
    key = str(repo_root)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            return cached

        name = resolve_forge_name(key)
        _register_adapters()
        adapter_cls = _ADAPTERS.get(name)
        if adapter_cls is None:
            raise ValueError(
                f"No forge adapter registered for {name!r}; expected one of "
                f"{sorted(_ADAPTERS)}."
            )
        adapter = adapter_cls()
        _CACHE[key] = adapter
        return adapter


__all__ = [
    "CANCELLED",
    "CI_STATES",
    "FAIL",
    "NONE",
    "PASS",
    "PENDING",
    "UNREADABLE",
    "CISnapshot",
    "CIStatusUnreadable",
    "Forge",
    "get_forge",
    "resolve_forge_name",
]
