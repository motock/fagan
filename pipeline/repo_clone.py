"""Clone a plan repository from an https URL into a managed root.

A standalone server has no host checkout to read, so each plan's repository is
cloned into its own volume under ``PIPELINE_REPOS_ROOT``. This module owns the
three pieces of that flow:

* :func:`parse_repo_url` validates the URL (https only, host allow-list, no
  credentials, no port/query/fragment, exactly ``/<owner>/<repo>[.git]``).
* :func:`managed_path` maps a URL to ``<root>/<host>/<owner>/<repo>`` and refuses
  any path that escapes the root.
* :func:`ensure_clone` clones into that path, or re-verifies an existing clone,
  fast-forwards it and pins the worktree onto the fetched commit.

Security properties:

* A URL containing any whitespace or control character is rejected, so the
  string that is validated is the string git acts on. git receives a
  re-assembled, normalised URL (``https://<host>/<owner>/<repo>``), never the
  raw input, and ``--`` separates it from the options in the argv list.
* Credentials never enter a URL, an argv list, ``.git/config`` or a log line:
  when ``PIPELINE_GIT_TOKEN_FILE`` names a private regular file owned by the
  current user, a 0700 ``GIT_ASKPASS`` helper in a private temp directory reads
  the token from that file itself. The token is never in the child environment,
  so ``/proc/<pid>/environ`` cannot leak it.
* The git child gets a minimal environment (``PATH``, ``HOME``, ``LANG``,
  ``TMPDIR`` plus git plumbing) rather than the pipeline's own, so provider keys
  and dashboard secrets are not exposed to repository-supplied helpers.
* An existing managed clone is only trusted after ``.git`` is confirmed to be a
  real directory (not a symlink) and ``remote.origin.url`` is confirmed to match
  the requested repository; the worktree is then reset onto the fetched commit
  and cleaned, so callers never read content from an unverified origin.

Environment:

* ``PIPELINE_REPO_URL_HOSTS`` - comma-separated allow-list, default
  ``github.com``.
* ``PIPELINE_REPOS_ROOT`` - required root of the managed volume.
* ``PIPELINE_GIT_TOKEN_FILE`` - optional path to a 0600 file holding a token.
* ``PIPELINE_REPO_URL_ALLOW_FILE`` - test-only escape hatch (see
  :func:`parse_repo_url`); off by default.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from urllib.parse import SplitResult, urlsplit

LOG = logging.getLogger(__name__)

DEFAULT_REPO_URL_HOSTS = "github.com"
DEFAULT_CLONE_TIMEOUT = 600
_FILE_HOST = "local"

_ALLOWED_SCHEMES = ("https",)
_SEGMENT_RE = re.compile(r"[A-Za-z0-9._-]+")
_PATH_RE = re.compile(r"/([^/]+)/([^/]+)")
_URL_CREDENTIALS_RE = re.compile(r"https://[^\s@]*@")
_WHITESPACE_RE = re.compile(r"\s")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_CHILD_ENV_KEYS = ("PATH", "HOME", "LANG", "TMPDIR")
# Defence in depth against repository-controlled configuration: no credential
# helper, hooks or filesystem monitor from a tree this module did not create.
_SAFE_GIT_OPTS = (
    "-c",
    "credential.helper=",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=",
)
# The helper reads the token from the file named in the child env, so the token
# itself never appears in the environment or in the script.
_ASKPASS_SCRIPT = """#!/bin/sh
case "$1" in
  *Username*) printf '%s\\n' 'x-access-token' ;;
  *) cat "$PIPELINE_GIT_TOKEN_FILE" ;;
esac
"""


class RepoUrlError(ValueError):
    """A repository URL or the configuration around it is unusable."""


class RepoCloneError(RuntimeError):
    """git could not clone or fetch. The message never carries a credential."""


class ClonedRepo(Path):
    """A managed repository path that also reports the commit it was pinned to.

    :func:`ensure_clone` returns one of these so a caller can audit exactly which
    commit it is about to read while still treating the result as a
    :class:`~pathlib.Path`.
    """

    _commit = ""

    @property
    def commit(self) -> str:
        """The full commit SHA the worktree is checked out at."""
        return self._commit


def _config(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def _loggable(text: str) -> str:
    """One sanitised line of untrusted text, safe to put in a log record."""
    for raw in text.splitlines():
        line = _sanitize(_CONTROL_RE.sub("", raw)).strip()
        if line:
            return line[:200]
    return ""


def _deny(url: str, reason: str) -> RepoUrlError:
    """Log a denied URL (credentials stripped) and build the exception to raise."""
    LOG.warning(
        "repository URL rejected",
        extra={
            "event": "repo_url_rejected",
            "url": _loggable(url),
            "reason": reason,
        },
    )
    return RepoUrlError(reason)


def _reject_path_segment(segment: str) -> None:
    if not _SEGMENT_RE.fullmatch(segment):
        raise RepoUrlError(f"invalid path segment {segment!r}")
    if segment in (".", ".."):
        raise RepoUrlError(f"invalid path segment {segment!r}")


def parse_repo_url(
    url: str, environ: Mapping[str, str] | None = None
) -> tuple[str, str, str]:
    """Return ``(host, owner, repo)`` for ``url`` or raise :class:`RepoUrlError`.

    Only ``https`` URLs on a host from ``PIPELINE_REPO_URL_HOSTS`` (comma
    separated, default ``github.com``) are accepted; userinfo, a port other
    than 443, a query and a fragment are all rejected. The path must be exactly
    ``/<owner>/<repo>`` or ``/<owner>/<repo>.git``.

    TEST-ONLY: when ``PIPELINE_REPO_URL_ALLOW_FILE=1`` a
    ``file:///abs/path/<owner>/<repo>.git`` URL is also accepted and reports the
    host ``local``. This exists so unit tests can drive a real bare repo; it is
    off by default and must not be enabled in production. Traversal segments
    (``.``/``..``) are still rejected in that branch.
    """
    env = _config(environ)
    if not isinstance(url, str) or not url:
        raise _deny("", "repository URL must be a non-empty string")
    # urlsplit() silently drops CR/LF/TAB, so a trailing newline would otherwise
    # be validated in one shape and handed to git in another.
    if _WHITESPACE_RE.search(url) or _CONTROL_RE.search(url):
        raise _deny(
            url, "repository URL must not contain whitespace or control characters"
        )
    split = urlsplit(url)
    if split.scheme == "file":
        return _parse_file_url(url, split, env)
    if split.scheme not in _ALLOWED_SCHEMES:
        raise _deny(url, f"unsupported URL scheme {split.scheme!r}")
    # Credentials must be rejected before the host allow-list: ``hostname``
    # strips userinfo, so a ``user:pw@host`` URL would otherwise look clean.
    if "@" in split.netloc or split.username is not None or split.password is not None:
        raise _deny(url, "repository URL must not carry credentials")
    if split.query or split.fragment or "?" in url or "#" in url:
        raise _deny(url, "repository URL must not carry a query or fragment")
    host = (split.hostname or "").lower()
    if not host:
        raise _deny(url, "repository URL must name a host")
    if host not in _allowed_hosts(env):
        raise _deny(url, f"repository host {host!r} is not allowed")
    try:
        port = split.port
    except ValueError:
        raise _deny(url, "repository URL has a malformed port") from None
    if port not in (None, 443):
        raise _deny(url, f"repository URL port {port!r} is not allowed")
    owner, repo = _split_path(split.path)
    return host, owner, repo


def _parse_file_url(
    url: str, split: SplitResult, env: Mapping[str, str]
) -> tuple[str, str, str]:
    if env.get("PIPELINE_REPO_URL_ALLOW_FILE") != "1":
        raise _deny(url, "file:// repository URLs are disabled")
    if split.netloc not in ("", "localhost"):
        raise _deny(url, "file:// repository URL must not name a host")
    if split.query or split.fragment:
        raise _deny(url, "repository URL must not carry a query or fragment")
    segments = _file_segments(url, split.path)
    owner, repo = segments[-2], segments[-1].removesuffix(".git")
    _reject_path_segment(owner)
    _reject_path_segment(repo)
    return _FILE_HOST, owner, repo


def _file_segments(url: str, path: str) -> list[str]:
    """Validate a test-only ``file://`` path and return its segments."""
    if not path.startswith("/") or not path.endswith(".git"):
        raise _deny(url, "file:// repository URL must be an absolute .git path")
    segments = [segment for segment in path.split("/") if segment]
    if len(segments) < 2:
        raise _deny(url, "file:// repository URL must end in <owner>/<repo>.git")
    for segment in segments:
        # No ``..``/``.`` collapse and no odd segments: with the escape hatch
        # enabled a traversing path would read any local repository.
        if segment in (".", "..") or not _SEGMENT_RE.fullmatch(segment):
            raise _deny(url, f"invalid path segment {segment!r}")
    return segments


def _allowed_hosts(env: Mapping[str, str]) -> frozenset[str]:
    raw = env.get("PIPELINE_REPO_URL_HOSTS", DEFAULT_REPO_URL_HOSTS)
    hosts = {entry.strip().lower() for entry in raw.split(",")}
    hosts.discard("")
    return frozenset(hosts)


def _split_path(path: str) -> tuple[str, str]:
    match = _PATH_RE.fullmatch(path)
    if match is None:
        raise RepoUrlError("repository URL path must be exactly /<owner>/<repo>")
    owner, repo = match.group(1), match.group(2)
    repo = repo.removesuffix(".git")
    _reject_path_segment(owner)
    _reject_path_segment(repo)
    return owner, repo


def _canonical_url(url: str, env: Mapping[str, str]) -> str:
    """Return the normalised URL handed to git instead of the raw input."""
    host, owner, repo = parse_repo_url(url, env)
    if host == _FILE_HOST:
        path = "/" + "/".join(_file_segments(url, urlsplit(url).path))
        return f"file://{path}"
    return f"https://{host}/{owner}/{repo}"


def managed_path(url: str, environ: Mapping[str, str] | None = None) -> Path:
    """Return ``<PIPELINE_REPOS_ROOT>/<host>/<owner>/<repo>`` for ``url``.

    Never creates anything. Raises :class:`RepoUrlError` when
    ``PIPELINE_REPOS_ROOT`` is unset or when the resolved target would land
    outside the resolved root (a symlinked component, say).
    """
    env = _config(environ)
    root_value = env.get("PIPELINE_REPOS_ROOT", "").strip()
    if not root_value:
        raise _deny(url, "PIPELINE_REPOS_ROOT is not set")
    host, owner, repo = parse_repo_url(url, env)
    root = Path(root_value)
    target = root / host / owner / repo
    root_resolved = os.path.realpath(root)
    target_resolved = os.path.realpath(target)
    try:
        inside = os.path.commonpath([root_resolved, target_resolved]) == root_resolved
    except ValueError:  # different drives (Windows); treat as outside
        inside = False
    if not inside or target_resolved == root_resolved:
        raise _deny(
            url,
            f"repository path for {_redact_url(url)} escapes PIPELINE_REPOS_ROOT",
        )
    return target


def _sanitize(text: str) -> str:
    """Replace any ``https://<anything>@`` credential prefix with ``https://***@``."""
    return _URL_CREDENTIALS_RE.sub("https://***@", text)


def _redact_url(url: str) -> str:
    """Drop userinfo from ``url`` so it can be named in an error or a log line."""
    return _sanitize(_CONTROL_RE.sub("", url))


def _token_file(env: Mapping[str, str]) -> Path | None:
    """Validate ``PIPELINE_GIT_TOKEN_FILE`` and return it, or ``None`` when unset.

    The file must be a regular file owned by the current user and closed to
    group and world access; anything else is refused before git runs.
    """
    raw = env.get("PIPELINE_GIT_TOKEN_FILE", "").strip()
    if not raw:
        return None
    path = Path(raw)
    try:
        info = path.stat()
    except OSError as exc:
        raise RepoCloneError(
            f"could not read PIPELINE_GIT_TOKEN_FILE ({type(exc).__name__})"
        ) from None
    if not stat.S_ISREG(info.st_mode):
        # A FIFO or device would block, or stream forever.
        raise RepoCloneError("PIPELINE_GIT_TOKEN_FILE must be a regular file")
    if info.st_mode & 0o077:
        raise RepoCloneError(
            "PIPELINE_GIT_TOKEN_FILE must not be group or world accessible"
        )
    getuid = getattr(os, "getuid", None)
    if getuid is not None and info.st_uid != getuid():
        raise RepoCloneError(
            "PIPELINE_GIT_TOKEN_FILE must be owned by the current user"
        )
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RepoCloneError(
            f"could not read PIPELINE_GIT_TOKEN_FILE ({type(exc).__name__})"
        ) from None
    if not token:
        raise RepoCloneError("PIPELINE_GIT_TOKEN_FILE is empty")
    return path


def _child_env(
    env: Mapping[str, str], extra: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Build the minimal environment handed to git.

    The pipeline's own environment (provider keys, dashboard keys) is never
    copied: only the keys git needs to run, plus the askpass plumbing.
    """
    child: dict[str, str] = {}
    for key in _CHILD_ENV_KEYS:
        value = env.get(key) or os.environ.get(key)
        if value:
            child[key] = value
    child.setdefault("PATH", os.defpath)
    if "HOME" not in child:
        child["HOME"] = str(Path.home())
    child["GIT_TERMINAL_PROMPT"] = "0"
    child["GIT_CONFIG_NOSYSTEM"] = "1"
    if extra:
        child.update(extra)
    return child


@contextlib.contextmanager
def _askpass(token_path: Path | None) -> Iterator[dict[str, str]]:
    """Yield the child-env entries that let git ask for credentials.

    The 0700 helper script lives in a private temp directory, reads the token
    from ``PIPELINE_GIT_TOKEN_FILE`` (never from its own environment) and the
    directory is removed again on both the success and the failure path.
    """
    extra: dict[str, str] = {"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"}
    if token_path is None:
        yield extra
        return
    directory = Path(tempfile.mkdtemp(prefix="pipeline-askpass-"))
    try:
        directory.chmod(0o700)
        script = directory / "askpass.sh"
        script.write_text(_ASKPASS_SCRIPT, encoding="utf-8")
        script.chmod(0o700)
        extra["GIT_ASKPASS"] = str(script)
        extra["PIPELINE_GIT_TOKEN_FILE"] = str(token_path)
        yield extra
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _run(
    argv: Sequence[str],
    child_env: Mapping[str, str],
    timeout: float,
    operation: str,
    safe_url: str,
) -> str:
    """Run one git command and translate every failure into RepoCloneError."""
    try:
        proc = subprocess.run(
            list(argv),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=dict(child_env),
        )
    except subprocess.TimeoutExpired:
        raise RepoCloneError(
            f"git {operation} timed out after {timeout:g}s for {safe_url}"
        ) from None
    except OSError as exc:
        raise RepoCloneError(
            f"git {operation} could not start for {safe_url} ({type(exc).__name__})"
        ) from None
    if proc.returncode != 0:
        LOG.warning(
            "git command failed",
            extra={
                "event": "repo_git_failure",
                "operation": operation,
                "url": safe_url,
                "stderr": _loggable(proc.stderr),
            },
        )
        raise RepoCloneError(f"git {operation} failed for {safe_url}")
    return proc.stdout


def _try_output(
    argv: Sequence[str], child_env: Mapping[str, str], timeout: float
) -> str | None:
    """Return stripped stdout, or ``None`` when git exits non-zero."""
    try:
        proc = subprocess.run(
            list(argv),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=dict(child_env),
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _url_key(url: str) -> tuple[str, str, str]:
    """Comparison key for a URL: scheme, host and path without ``.git``/slash."""
    split = urlsplit(url.strip())
    path = split.path.rstrip("/").removesuffix(".git")
    return split.scheme.lower(), (split.hostname or "").lower(), path


def _verify_existing(target: Path, canonical: str, safe_url: str) -> None:
    """Refuse an existing managed path that is not a genuine clone of ``canonical``."""
    git_dir = target / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        raise RepoCloneError(
            f"git fetch refused: {target} is not a managed clone of {safe_url}"
        )
    origin = _try_output(
        [
            "git",
            *_SAFE_GIT_OPTS,
            "-C",
            str(target),
            "config",
            "--get",
            "remote.origin.url",
        ],
        _child_env({}),
        30,
    )
    if origin is None or _url_key(origin) != _url_key(canonical):
        LOG.warning(
            "refusing to fetch from a substituted origin",
            extra={
                "event": "repo_origin_mismatch",
                "url": safe_url,
                "path": str(target),
            },
        )
        raise RepoCloneError(
            f"git fetch refused: existing clone at {target} does not track {safe_url}"
        )


def _resolve_fetched_commit(
    target: Path, child_env: Mapping[str, str], timeout: float, safe_url: str
) -> str:
    """Return the commit the fetched ``origin/HEAD`` (or ``HEAD``) points at."""
    for rev in ("refs/remotes/origin/HEAD", "HEAD"):
        sha = _try_output(
            ["git", *_SAFE_GIT_OPTS, "-C", str(target), "rev-parse", "--verify", rev],
            child_env,
            timeout,
        )
        if sha:
            return sha
    LOG.warning(
        "could not resolve the fetched commit",
        extra={"event": "repo_resolve_failed", "url": safe_url, "path": str(target)},
    )
    raise RepoCloneError(f"git fetch failed for {safe_url}: no fetched commit found")


def _head_commit(
    target: Path, child_env: Mapping[str, str], timeout: float, safe_url: str
) -> str:
    """Return the checked-out commit of ``target`` or raise RepoCloneError."""
    head = _try_output(
        ["git", *_SAFE_GIT_OPTS, "-C", str(target), "rev-parse", "--verify", "HEAD"],
        child_env,
        timeout,
    )
    if not head:
        raise RepoCloneError(f"git clone failed for {safe_url}: no commit found")
    return head


def ensure_clone(
    url: str,
    environ: Mapping[str, str] | None = None,
    timeout: float = DEFAULT_CLONE_TIMEOUT,
) -> ClonedRepo:
    """Clone ``url`` into its managed path, or refresh and pin an existing clone.

    Returns the managed path as a :class:`ClonedRepo`, whose ``commit`` is the
    full SHA the worktree is checked out at. Raises :class:`RepoUrlError` for an
    unacceptable URL or configuration and :class:`RepoCloneError` when git
    fails; neither message ever carries a credential.
    """
    env = _config(environ)
    canonical = _canonical_url(url, env)
    target = managed_path(url, env)
    safe_url = _redact_url(canonical)
    token_path = _token_file(env)
    with _askpass(token_path) as extra_env:
        child_env = _child_env(env, extra_env)
        if (target / ".git").exists():
            _verify_existing(target, canonical, safe_url)
            _run(
                ["git", *_SAFE_GIT_OPTS, "-C", str(target), "fetch", "--prune", "origin"],
                child_env,
                timeout,
                "fetch",
                safe_url,
            )
            commit = _resolve_fetched_commit(target, child_env, timeout, safe_url)
            _run(
                ["git", *_SAFE_GIT_OPTS, "-C", str(target), "reset", "--hard", commit],
                child_env,
                timeout,
                "reset",
                safe_url,
            )
            _run(
                ["git", *_SAFE_GIT_OPTS, "-C", str(target), "clean", "-xffd"],
                child_env,
                timeout,
                "clean",
                safe_url,
            )
            LOG.info(
                "fetched plan repository",
                extra={
                    "event": "repo_fetched",
                    "url": safe_url,
                    "path": str(target),
                    "commit": commit,
                },
            )
        else:
            if target.exists() and any(target.iterdir()):
                # Not a managed clone and not empty: never let git write into a
                # tree somebody else prepared at the canonical path.
                raise RepoCloneError(
                    f"git clone refused: {target} exists but is not a managed "
                    f"clone of {safe_url}"
                )
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
            _run(
                [
                    "git",
                    *_SAFE_GIT_OPTS,
                    "clone",
                    "--no-tags",
                    "--",
                    canonical,
                    str(target),
                ],
                child_env,
                timeout,
                "clone",
                safe_url,
            )
            commit = _head_commit(target, child_env, timeout, safe_url)
            LOG.info(
                "cloned plan repository",
                extra={
                    "event": "repo_cloned",
                    "url": safe_url,
                    "path": str(target),
                    "commit": commit,
                },
            )
    repo = ClonedRepo(target)
    repo._commit = commit
    return repo
