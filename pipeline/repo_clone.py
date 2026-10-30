"""Clone a plan repository from an https URL into a managed root.

A standalone server has no host checkout to read, so each plan's repository is
cloned into its own volume under ``PIPELINE_REPOS_ROOT``. This module owns the
three pieces of that flow:

* :func:`parse_repo_url` validates the URL (https only, host allow-list, no
  credentials, no port/query/fragment, exactly ``/<owner>/<repo>[.git]``).
* :func:`managed_path` maps a URL to ``<root>/<host>/<owner>/<repo>`` and refuses
  any path that escapes the root.
* :func:`ensure_clone` clones when the managed path has no ``.git`` and runs
  ``git fetch --prune origin`` when it does.

Credentials never enter a URL, an argv list, ``.git/config`` or a log line: when
``PIPELINE_GIT_TOKEN_FILE`` names a private file, the token is handed to git
through a short-lived ``GIT_ASKPASS`` helper whose temp directory is removed
afterwards.

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
import subprocess
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from urllib.parse import SplitResult, urlsplit

LOG = logging.getLogger(__name__)

DEFAULT_REPO_URL_HOSTS = "github.com"
_ALLOWED_SCHEMES = ("https",)
_SEGMENT_RE = re.compile(r"[A-Za-z0-9._-]+")
_PATH_RE = re.compile(r"^/([^/]+)/([^/]+)$")
_URL_CREDENTIALS_RE = re.compile(r"https://[^\s@]*@")
_ASKPASS_SCRIPT = """#!/bin/sh
case "$1" in
  *Username*) printf '%s\\n' 'x-access-token' ;;
  *) printf '%s\\n' "$PIPELINE_GIT_ASKPASS_TOKEN" ;;
esac
"""


class RepoUrlError(ValueError):
    """A repository URL or the configuration around it is unusable."""


class RepoCloneError(RuntimeError):
    """git could not clone or fetch. The message never carries a credential."""


def _config(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def _allowed_hosts(env: Mapping[str, str]) -> frozenset[str]:
    raw = env.get("PIPELINE_REPO_URL_HOSTS", DEFAULT_REPO_URL_HOSTS)
    hosts = {entry.strip().lower() for entry in raw.split(",")}
    hosts.discard("")
    return frozenset(hosts)


def _reject_path_segment(segment: str) -> None:
    if not _SEGMENT_RE.fullmatch(segment):
        raise RepoUrlError(f"invalid path segment {segment!r}")
    if segment in (".", ".."):
        raise RepoUrlError(f"invalid path segment {segment!r}")


def parse_repo_url(url: str, environ: Mapping[str, str] | None = None) -> tuple[str, str, str]:
    """Return ``(host, owner, repo)`` for ``url`` or raise :class:`RepoUrlError`.

    Only ``https`` URLs on a host from ``PIPELINE_REPO_URL_HOSTS`` (comma
    separated, default ``github.com``) are accepted; userinfo, a port other
    than 443, a query and a fragment are all rejected. The path must be exactly
    ``/<owner>/<repo>`` or ``/<owner>/<repo>.git``.

    TEST-ONLY: when ``PIPELINE_REPO_URL_ALLOW_FILE=1`` a
    ``file:///abs/path/<owner>/<repo>.git`` URL is also accepted and reports the
    host ``local``. This exists so unit tests can drive a real bare repo; it is
    off by default and must not be enabled in production.
    """
    env = _config(environ)
    split = urlsplit(url)
    if split.scheme == "file":
        return _parse_file_url(split, env)
    if split.scheme not in _ALLOWED_SCHEMES:
        raise RepoUrlError(f"unsupported URL scheme {split.scheme!r}")
    # Credentials must be rejected before the host allow-list: ``hostname``
    # strips userinfo, so a ``user:pw@host`` URL would otherwise look clean.
    if "@" in split.netloc or split.username is not None or split.password is not None:
        raise RepoUrlError("repository URL must not carry credentials")
    if "?" in url or "#" in url:
        raise RepoUrlError("repository URL must not carry a query or fragment")
    host = (split.hostname or "").lower()
    if host not in _allowed_hosts(env):
        raise RepoUrlError(f"repository host {host!r} is not allowed")
    try:
        port = split.port
    except ValueError:
        raise RepoUrlError("repository URL has a malformed port") from None
    if port not in (None, 443):
        raise RepoUrlError(f"repository URL port {port!r} is not allowed")
    owner, repo = _split_path(split.path)
    return host, owner, repo


def _parse_file_url(split: SplitResult, env: Mapping[str, str]) -> tuple[str, str, str]:
    if env.get("PIPELINE_REPO_URL_ALLOW_FILE") != "1":
        raise RepoUrlError("file:// repository URLs are disabled")
    if split.netloc not in ("", "localhost"):
        raise RepoUrlError("file:// repository URL must not name a host")
    path = split.path
    if not path.startswith("/") or not path.endswith(".git"):
        raise RepoUrlError("file:// repository URL must be an absolute .git path")
    segments = [segment for segment in path.split("/") if segment]
    if len(segments) < 2:
        raise RepoUrlError("file:// repository URL must end in <owner>/<repo>.git")
    owner, repo = segments[-2], segments[-1].removesuffix(".git")
    _reject_path_segment(owner)
    _reject_path_segment(repo)
    return "local", owner, repo


def _split_path(path: str) -> tuple[str, str]:
    match = _PATH_RE.match(path)
    if match is None:
        raise RepoUrlError("repository URL path must be exactly /<owner>/<repo>")
    owner, repo = match.group(1), match.group(2)
    repo = repo.removesuffix(".git")
    _reject_path_segment(owner)
    _reject_path_segment(repo)
    return owner, repo


def managed_path(url: str, environ: Mapping[str, str] | None = None) -> Path:
    """Return ``<PIPELINE_REPOS_ROOT>/<host>/<owner>/<repo>`` for ``url``.

    Never creates anything. Raises :class:`RepoUrlError` when
    ``PIPELINE_REPOS_ROOT`` is unset or when the resolved target would land
    outside the resolved root (a symlinked component, say).
    """
    env = _config(environ)
    root_value = env.get("PIPELINE_REPOS_ROOT", "").strip()
    if not root_value:
        raise RepoUrlError("PIPELINE_REPOS_ROOT is not set")
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
        raise RepoUrlError(f"repository path for {url} escapes PIPELINE_REPOS_ROOT")
    return target


def _sanitize(text: str) -> str:
    """Replace any ``https://<anything>@`` credential prefix with ``https://***@``."""
    return _URL_CREDENTIALS_RE.sub("https://***@", text)


def _redact_url(url: str) -> str:
    """Drop userinfo from ``url`` so it can be named in an error or a log line."""
    return _URL_CREDENTIALS_RE.sub("https://", url)


def _read_token(token_path: Path) -> str:
    try:
        mode = token_path.stat().st_mode
    except OSError as exc:
        raise RepoCloneError(
            f"could not read PIPELINE_GIT_TOKEN_FILE ({type(exc).__name__})"
        ) from None
    if mode & 0o077:
        raise RepoCloneError(
            "PIPELINE_GIT_TOKEN_FILE must not be group or world readable"
        )
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RepoCloneError(
            f"could not read PIPELINE_GIT_TOKEN_FILE ({type(exc).__name__})"
        ) from None
    if not token:
        raise RepoCloneError("PIPELINE_GIT_TOKEN_FILE is empty")
    return token


@contextlib.contextmanager
def _askpass(env: Mapping[str, str]) -> Iterator[dict[str, str]]:
    """Yield the child-env additions that let git authenticate without a URL.

    Empty when no token file is configured. The helper script lives in a 0700
    temp dir that is removed in a ``finally``, so a failed call leaks neither
    the script nor the token.
    """
    token_value = env.get("PIPELINE_GIT_TOKEN_FILE", "").strip()
    if not token_value:
        yield {}
        return
    token = _read_token(Path(token_value))
    directory = Path(tempfile.mkdtemp(prefix="pipeline-git-askpass-"))
    try:
        directory.chmod(0o700)
        script = directory / "askpass.sh"
        script.write_text(_ASKPASS_SCRIPT, encoding="utf-8")
        script.chmod(0o700)
        yield {
            "GIT_ASKPASS": str(script),
            "GIT_TERMINAL_PROMPT": "0",
            "PIPELINE_GIT_ASKPASS_TOKEN": token,
        }
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def ensure_clone(
    url: str,
    environ: Mapping[str, str] | None = None,
    timeout: float = 600,
) -> Path:
    """Clone ``url`` into the managed path, or fetch into an existing clone.

    Returns the managed path. Raises :class:`RepoCloneError` on a non-zero git
    exit, a timeout or an exec failure; the message names the operation and the
    credential-free URL. git's stderr is never echoed verbatim - only its first
    line, with any ``https://<anything>@`` prefix redacted, is logged at WARN.
    """
    env = _config(environ)
    target = managed_path(url, env)
    safe_url = _redact_url(url)
    child_env = dict(os.environ)
    with _askpass(env) as auth_env:
        child_env.update(auth_env)
        if (target / ".git").exists():
            operation = "fetch"
            argv = ["git", "-C", str(target), "fetch", "--prune", "origin"]
        else:
            operation = "clone"
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
            argv = ["git", "clone", "--no-tags", url, str(target)]
        try:
            proc = subprocess.run(
                argv,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=child_env,
            )
        except subprocess.TimeoutExpired:
            raise RepoCloneError(
                f"git {operation} timed out after {timeout}s for {safe_url}"
            ) from None
        except OSError as exc:
            raise RepoCloneError(
                f"git {operation} could not start for {safe_url} "
                f"({type(exc).__name__})"
            ) from None
        if proc.returncode != 0:
            first_line = next(
                (line for line in (proc.stderr or "").splitlines() if line.strip()),
                "",
            )
            LOG.warning(
                "git %s failed for %s (exit %s): %s",
                operation,
                safe_url,
                proc.returncode,
                _sanitize(first_line),
            )
            raise RepoCloneError(
                f"git {operation} failed for {safe_url} (exit {proc.returncode})"
            )
    return target
