"""Clone a plan repository from an https URL into a managed root.

A standalone server has no host checkout to read, so each plan's repository is
cloned into its own volume under ``PIPELINE_REPOS_ROOT``. This module owns the
three pieces of that flow:

* :func:`parse_repo_url` validates the URL (https only, host allow-list, no
  credentials, no port/query/fragment, exactly ``/<owner>/<repo>[.git]``).
* :func:`managed_path` maps a URL to ``<root>/<host>/<owner>/<repo>`` and refuses
  any path that escapes the root.
* :func:`ensure_clone` clones into that path, or refreshes an existing clone and
  pins its worktree onto the fetched commit.

Security properties:

* A URL containing any whitespace or control character is rejected, so the
  string that is validated is the string git acts on. git receives a
  re-assembled, normalised URL (``https://<host>/<owner>/<repo>``), never the
  raw input, and ``--`` separates it from the options in the argv list.
* Credentials never enter a URL, an argv list, ``.git/config`` or a log line.
  When ``PIPELINE_GIT_TOKEN_FILE`` names a private regular file owned by the
  current user, a 0700 ``GIT_ASKPASS`` helper in a private temp directory reads
  the token from that file itself; the token is never placed in the child
  environment, so ``/proc/<pid>/environ`` cannot leak it.
* The git child gets a minimal environment (no pipeline secrets) and cannot
  read user or system git configuration: ``GIT_CONFIG_GLOBAL`` and
  ``GIT_CONFIG_SYSTEM`` point at ``/dev/null``, ``GIT_CONFIG_NOSYSTEM=1``, and
  the child's ``HOME`` is a private scratch directory. Hostile settings such as
  ``url.<base>.insteadOf`` or ``http.proxy`` therefore cannot rewrite a URL.
* A pre-existing managed clone is *not* trusted on the strength of
  ``remote.origin.url`` alone. :func:`_audit_local_config` reads every local
  config entry with ``git config --local --list`` and accepts only a
  deny-by-default allow-list whose values are re-checked; the origin URL must
  equal the canonical URL and occur exactly once. Any other key -
  ``url.*.insteadOf``, ``http.proxy``, ``core.gitproxy``, ``core.worktree``,
  ``credential.*``, ``core.hooksPath``, ... - refuses the refresh, and so does
  every ``extensions.*`` key. ``extensions.worktreeConfig`` in particular is
  never allowed: it makes git read ``<git-common-dir>/config.worktree``, a
  scope ``git config --local --list`` does not report, so the audit explicitly
  refuses a clone that carries that file.
* The fetch transport is forced off on the argv as well: ``-c http.proxy=``,
  ``-c https.proxy=`` and ``-c http.followRedirects=false`` are set alongside
  the other ``_safe_opts`` values, so a proxy or redirect value that ever
  slipped past the audit still cannot be honoured.
* ``fetch`` is given the canonical URL and an explicit refspec on the argv, so
  the ``origin`` remote configuration is never consulted for the transport.
* ``reset`` and ``clean`` run with ``-c core.worktree=<realpath(target)>`` and
  ``-c core.bare=false`` forced on the argv, so a stale ``core.worktree`` in the
  clone's own config cannot make git write or delete outside the managed root.

Environment:

* ``PIPELINE_REPO_URL_HOSTS`` - comma-separated allow-list, default
  ``github.com``.
* ``PIPELINE_REPOS_ROOT`` - required root of the managed volume.
* ``PIPELINE_GIT_TOKEN_FILE`` - optional path to a 0600 file holding a token.
* ``PIPELINE_REPO_URL_ALLOW_FILE`` - test-only escape hatch (see
  :func:`parse_repo_url`); off by default, must never be set in production.
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
from collections.abc import Iterator, Mapping
from pathlib import Path, PosixPath
from urllib.parse import SplitResult, urlsplit

LOG = logging.getLogger(__name__)

DEFAULT_REPO_URL_HOSTS = "github.com"
DEFAULT_CLONE_TIMEOUT = 600

_FILE_HOST = "local"
_ORIGIN_REFSPEC = "+refs/heads/*:refs/remotes/origin/*"

_ALLOWED_SCHEMES = ("https",)
_SEGMENT_RE = re.compile(r"[A-Za-z0-9._-]+")
_PATH_RE = re.compile(r"/([^/]+)/([^/]+)")
_URL_CREDENTIALS_RE = re.compile(r"https://[^\s@]*@")
_WHITESPACE_RE = re.compile(r"\s")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_BRANCH_REF_RE = re.compile(r"refs/heads/[A-Za-z0-9._/@+-]+")

# Local config keys a clone made by this module may legitimately carry. This is
# a deny-by-default allow-list: every other key refuses the refresh, because a
# key such as ``url.<base>.insteadOf`` rewrites the transport while
# ``remote.origin.url`` still reads the legitimate value.
_CONFIG_EXACT_KEYS = frozenset(
    {
        "core.repositoryformatversion",
        "core.filemode",
        "core.bare",
        "core.logallrefupdates",
        "core.ignorecase",
        "core.precomposeunicode",
        "core.symlinks",
        "gc.auto",
        "gc.autodetach",
    }
)
# ``extensions.*`` is deliberately *not* allow-listed, not even by prefix:
# ``extensions.worktreeConfig`` makes git additionally read
# ``<git-common-dir>/config.worktree``, and ``git config --local --list`` does
# not report that scope, so a key hidden there would never reach the audit.
# A sha256 repository (``extensions.objectFormat=sha256``) is not supported
# either; supporting one would need an exact key + allowed-value pair here.
_CONFIG_BOOLEANS = frozenset({"core.filemode", "core.logallrefupdates"})

# The helper reads the token from the file named in the child env, so the token
# itself never appears in the environment or in the script. The https username
# is fixed: the token is never put into a URL.
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


# Subclass a concrete flavour, not ``Path``: before 3.12 ``Path`` resolves a
# class-level ``_flavour`` that a direct subclass does not inherit, so
# ``ClonedRepo(...)`` raised AttributeError on every 3.11 run.
class ClonedRepo(PosixPath):
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


def _redact(text: str) -> str:
    """Replace any credential embedded in an https URL with ``***``."""
    return _URL_CREDENTIALS_RE.sub("https://***@", text)


def _sanitize(text: str) -> str:
    """Strip control characters and hide credentials in untrusted text."""
    return _redact(_CONTROL_RE.sub("", text))


def _loggable(text: str) -> str:
    """One sanitised line of untrusted text, safe to put in a log record."""
    for raw in text.splitlines():
        line = _sanitize(raw).strip()
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


def _reject_path_segment(url: str, segment: str) -> None:
    """Reject a ``.``/``..`` or otherwise unusable path segment, with an audit."""
    if segment in (".", "..") or not _SEGMENT_RE.fullmatch(segment):
        raise _deny(url, f"invalid path segment {segment!r}")


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
    off by default, must not be enabled in production, and traversal segments
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
    owner, repo = _split_path(url, split.path)
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
    _reject_path_segment(url, owner)
    _reject_path_segment(url, repo)
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


def _split_path(url: str, path: str) -> tuple[str, str]:
    match = _PATH_RE.fullmatch(path)
    if match is None:
        raise _deny(url, "repository URL path must be exactly /<owner>/<repo>")
    owner, repo = match.group(1), match.group(2)
    repo = repo.removesuffix(".git")
    _reject_path_segment(url, owner)
    _reject_path_segment(url, repo)
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

    Raises :class:`RepoUrlError` when ``PIPELINE_REPOS_ROOT`` is unset or when
    the resolved target would fall outside the resolved root (a symlinked host
    or owner directory must not redirect the clone out of the volume).
    """
    env = _config(environ)
    host, owner, repo = parse_repo_url(url, env)
    raw_root = env.get("PIPELINE_REPOS_ROOT")
    if not raw_root:
        raise RepoUrlError(
            "PIPELINE_REPOS_ROOT is required to compute a managed repository path"
        )
    root = Path(raw_root)
    target = root / host / owner / repo
    resolved_root = Path(os.path.realpath(root))
    resolved_target = Path(os.path.realpath(target))
    if resolved_target == resolved_root or not resolved_target.is_relative_to(
        resolved_root
    ):
        raise RepoUrlError(
            f"managed repository path for {_redact(url)} escapes PIPELINE_REPOS_ROOT"
        )
    return target


# --- git plumbing ----------------------------------------------------------


def _child_env(auth: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build the minimal environment handed to every git child process.

    The pipeline's own environment is never inherited wholesale, and git's user
    and system configuration are neutralised so a ``~/.gitconfig`` carrying
    ``insteadOf`` or ``http.proxy`` cannot rewrite a transport.
    """
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_SSH_COMMAND": "false",
    }
    if auth:
        env.update(auth)
    return env


def _token_file(env: Mapping[str, str]) -> Path | None:
    """Validate ``PIPELINE_GIT_TOKEN_FILE`` and return it without reading it."""
    raw = env.get("PIPELINE_GIT_TOKEN_FILE")
    if not raw:
        return None
    path = Path(raw)
    try:
        info = path.lstat()
    except OSError:
        raise RepoCloneError(
            "PIPELINE_GIT_TOKEN_FILE does not name a readable file"
        ) from None
    if not stat.S_ISREG(info.st_mode):
        raise RepoCloneError("PIPELINE_GIT_TOKEN_FILE must be a regular file")
    if info.st_mode & 0o077:
        raise RepoCloneError(
            "PIPELINE_GIT_TOKEN_FILE must not be group or world accessible"
        )
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise RepoCloneError(
            "PIPELINE_GIT_TOKEN_FILE must be owned by the current user"
        )
    if info.st_size == 0:
        raise RepoCloneError("PIPELINE_GIT_TOKEN_FILE is empty")
    return path


@contextlib.contextmanager
def _askpass(token: Path | None) -> Iterator[dict[str, str]]:
    """Yield the git credential environment; never yield the token itself."""
    directory = Path(tempfile.mkdtemp(prefix="pipeline-git-"))
    os.chmod(directory, 0o700)
    try:
        script = directory / "askpass.sh"
        script.write_text(_ASKPASS_SCRIPT)
        os.chmod(script, 0o700)
        auth = {
            "GIT_ASKPASS": str(script),
            "GIT_TERMINAL_PROMPT": "0",
            # A scratch HOME keeps user-level hooks and templates out of reach.
            "HOME": str(directory),
        }
        if token is not None:
            auth["PIPELINE_GIT_TOKEN_FILE"] = str(token)
        yield auth
    finally:
        # ``finally`` so a failed clone never leaves a live token file behind.
        shutil.rmtree(directory, ignore_errors=True)


def _safe_opts(target: Path) -> list[str]:
    """Command-line config that outranks any repository-local setting."""
    root = os.path.realpath(str(target))
    return [
        "-c",
        "credential.helper=",
        # Defence in depth for the transport: even if the config audit were
        # loosened, ``fetch`` cannot be routed through a proxy or redirected to
        # another host by a value that slipped past it.
        "-c",
        "http.proxy=",
        "-c",
        "https.proxy=",
        "-c",
        "http.followRedirects=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=",
        "-c",
        "core.sshCommand=false",
        # Defence in depth for the worktree escape: the worktree is ours, not
        # the one a stale clone's own config may name.
        "-c",
        "core.bare=false",
        "-c",
        f"core.worktree={root}",
    ]


def _run(
    argv: list[str],
    child_env: Mapping[str, str],
    timeout: float,
    operation: str,
    safe_url: str,
) -> str:
    """Run one git command from an argv list and return its stdout."""
    try:
        proc = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=dict(child_env),
        )
    except subprocess.TimeoutExpired:
        LOG.warning(
            "git command timed out",
            extra={
                "event": "repo_git_timeout",
                "operation": operation,
                "url": safe_url,
            },
        )
        raise RepoCloneError(f"git {operation} timed out for {safe_url}") from None
    except OSError as exc:
        LOG.warning(
            "git command could not start",
            extra={
                "event": "repo_git_unavailable",
                "operation": operation,
                "url": safe_url,
                "reason": type(exc).__name__,
            },
        )
        raise RepoCloneError(f"git {operation} failed for {safe_url}") from None
    if proc.returncode != 0:
        LOG.warning(
            "git command failed",
            extra={
                "event": "repo_git_failure",
                "operation": operation,
                "url": safe_url,
                # git echoes the URL from the remote it was given, so a
                # credential pasted into it must be stripped before logging.
                "stderr": _loggable(proc.stderr or proc.stdout or ""),
            },
        )
        raise RepoCloneError(f"git {operation} failed for {safe_url}")
    return proc.stdout or ""


def _try_output(
    argv: list[str], child_env: Mapping[str, str], timeout: float
) -> str | None:
    """Run a read-only git command, returning stripped stdout or ``None``."""
    try:
        proc = subprocess.run(
            argv,
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
    return (proc.stdout or "").strip()


def _config_entries(raw: str) -> list[tuple[str, str]]:
    """Split ``git config --local --list -z`` output into (key, value) pairs."""
    entries: list[tuple[str, str]] = []
    for record in raw.split("\0"):
        if not record:
            continue
        key, _, value = record.partition("\n")
        entries.append((key.strip(), value))
    return entries


def _untrusted(target: Path, key: str) -> RepoCloneError:
    LOG.warning(
        "refusing to refresh an untrusted clone",
        extra={
            "event": "repo_config_untrusted",
            "path": str(target),
            "key": _loggable(key),
        },
    )
    return RepoCloneError(
        f"git fetch refused: the existing clone at {target} carries an "
        "unexpected local git configuration"
    )


def _git_common_dir(
    target: Path, child_env: Mapping[str, str], timeout: float
) -> Path | None:
    """Return the clone's common git dir (``.git`` for a normal clone)."""
    output = _try_output(
        ["git", *_safe_opts(target), "-C", str(target), "rev-parse", "--git-common-dir"],
        child_env,
        timeout,
    )
    if not output:
        return None
    common = Path(output)
    if not common.is_absolute():
        common = target / common
    return common


def _refuse_worktree_config(
    target: Path, child_env: Mapping[str, str], timeout: float
) -> None:
    """Refuse a clone that carries a ``<git-common-dir>/config.worktree`` file.

    ``git config --local --list`` reads the local scope only, so keys in the
    worktree scope are invisible to :func:`_audit_local_config`. The file
    exists only when somebody enabled ``extensions.worktreeConfig``, which a
    clone this module creates never does; treat its mere presence as hostile.
    The check runs before the local-config audit so the refusal does not depend
    on that audit still rejecting ``extensions.*``. A clone whose common dir
    cannot even be resolved (a rewritten repository format, say) is refused the
    same way: it is not a clone this module wrote.
    """
    common = _git_common_dir(target, child_env, timeout)
    if common is not None and not os.path.lexists(str(common / "config.worktree")):
        return
    LOG.warning(
        "refusing to refresh an untrusted clone",
        extra={
            "event": "repo_config_untrusted",
            "path": str(target),
            "key": "config.worktree",
        },
    )
    raise RepoCloneError(
        f"git fetch refused: the existing clone at {target} carries a git "
        "configuration this module does not trust"
    )


def _audit_local_config(
    target: Path, canonical: str, child_env: Mapping[str, str], timeout: float
) -> None:
    """Refuse an existing clone whose local config is not exactly what we wrote.

    A clone this module created carries a handful of keys. Anything else - most
    importantly ``url.*.insteadOf`` (which rewrites the URL a separate ``git
    fetch`` resolves), ``http.proxy``/``core.gitproxy`` (which MITM it),
    ``core.worktree`` or a duplicate ``remote.origin.url`` - means the tree is
    not trustworthy, so the refresh is refused instead of fetching from it.

    A ``config.worktree`` file is refused before anything else is read, because
    its keys live in a scope ``git config --local --list`` never reports.
    """
    _refuse_worktree_config(target, child_env, timeout)
    entries = _config_entries(
        _run(
            [
                "git",
                *_safe_opts(target),
                "-C",
                str(target),
                "config",
                "--local",
                "--list",
                "-z",
            ],
            child_env,
            timeout,
            "fetch",
            str(target),
        )
    )
    origins: list[str] = []
    fetches: list[str] = []
    for key, value in entries:
        lowered = key.lower()
        if lowered == "remote.origin.url":
            origins.append(value)
            continue
        if lowered == "remote.origin.fetch":
            fetches.append(value)
            continue
        if lowered == "remote.origin.tagopt":
            # ``git clone --no-tags`` records this; anything else is unexpected.
            if value != "--no-tags":
                raise _untrusted(target, key)
            continue
        if lowered == "core.bare":
            if value.lower() != "false":
                raise _untrusted(target, key)
            continue
        if lowered in _CONFIG_EXACT_KEYS:
            if lowered in _CONFIG_BOOLEANS and value.lower() not in ("true", "false"):
                raise _untrusted(target, key)
            continue
        if lowered.startswith("branch.") and lowered.endswith(".remote"):
            if value.lower() != "origin":
                raise _untrusted(target, key)
            continue
        if lowered.startswith("branch.") and lowered.endswith(".merge"):
            if not _BRANCH_REF_RE.fullmatch(value):
                raise _untrusted(target, key)
            continue
        if lowered.startswith("extensions."):
            # ``extensions.worktreeConfig`` would make git read
            # ``config.worktree``, a scope this audit cannot see; no other
            # extension is one this module creates.
            raise _untrusted(target, key)
        raise _untrusted(target, key)
    if origins != [canonical]:
        raise _untrusted(target, "remote.origin.url")
    if fetches != [_ORIGIN_REFSPEC]:
        raise _untrusted(target, "remote.origin.fetch")


def _is_existing_clone(target: Path, safe_url: str) -> bool:
    """Return True when ``target`` holds a clone; refuse anything ambiguous."""
    git_dir = target / ".git"
    if git_dir.is_symlink():
        raise RepoCloneError(
            f"git clone refused: {target} has a symlinked .git, not a managed clone"
        )
    if git_dir.is_dir():
        return True
    if target.is_dir():
        try:
            populated = any(target.iterdir())
        except OSError:
            populated = True
        if populated:
            raise RepoCloneError(
                f"git clone refused: {target} exists and is not a managed clone "
                f"of {safe_url}"
            )
    return False


def _make_dirs(path: Path) -> None:
    """Create ``path`` and its parents with mode 0750, whatever the umask is."""
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o750)
        except FileExistsError:
            continue
        os.chmod(directory, 0o750)


def _clone(
    target: Path,
    canonical: str,
    safe_url: str,
    child_env: Mapping[str, str],
    timeout: float,
) -> None:
    _make_dirs(target.parent)
    _run(
        [
            "git",
            *_safe_opts(target),
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


def _refresh(
    target: Path,
    canonical: str,
    safe_url: str,
    child_env: Mapping[str, str],
    timeout: float,
) -> None:
    """Fetch with an explicit URL and refspec, then pin the worktree onto it."""
    _audit_local_config(target, canonical, child_env, timeout)
    _run(
        [
            "git",
            *_safe_opts(target),
            "-C",
            str(target),
            "fetch",
            "--prune",
            "--no-tags",
            "--",
            # The canonical URL and refspec are on the argv: the ``origin``
            # remote's configuration is not consulted for the transport, so an
            # ``insteadOf`` rewrite has nothing to attach to.
            canonical,
            _ORIGIN_REFSPEC,
        ],
        child_env,
        timeout,
        "fetch",
        safe_url,
    )
    commit = _fetched_commit(target, child_env, timeout)
    if not commit:
        raise RepoCloneError(f"git fetch failed for {safe_url}: no fetched commit")
    _run(
        [
            "git",
            *_safe_opts(target),
            "-C",
            str(target),
            "reset",
            "--hard",
            "--quiet",
            commit,
        ],
        child_env,
        timeout,
        "fetch",
        safe_url,
    )
    # ``-xffd`` removes untracked and ignored files so a caller never reads
    # content an agent wrote into the tree; ``core.worktree`` is forced by
    # ``_safe_opts`` so this cannot leave the managed root.
    _run(
        ["git", *_safe_opts(target), "-C", str(target), "clean", "-xffd"],
        child_env,
        timeout,
        "fetch",
        safe_url,
    )


def _fetched_commit(
    target: Path, child_env: Mapping[str, str], timeout: float
) -> str | None:
    for rev in ("refs/remotes/origin/HEAD", "FETCH_HEAD", "HEAD"):
        sha = _try_output(
            [
                "git",
                *_safe_opts(target),
                "-C",
                str(target),
                "rev-parse",
                "--verify",
                "--quiet",
                rev,
            ],
            child_env,
            timeout,
        )
        if sha:
            return sha
    return None


def _head_commit(
    target: Path, child_env: Mapping[str, str], timeout: float, safe_url: str
) -> str:
    head = _try_output(
        [
            "git",
            *_safe_opts(target),
            "-C",
            str(target),
            "rev-parse",
            "--verify",
            "HEAD",
        ],
        child_env,
        timeout,
    )
    if not head:
        raise RepoCloneError(f"git clone failed for {safe_url}: no commit found")
    return head


def ensure_clone(
    url: str, environ: Mapping[str, str] | None = None, timeout: float = 600
) -> Path:
    """Clone ``url`` into its managed path, or refresh that path, and pin it.

    Returns a :class:`ClonedRepo` whose ``commit`` is the checked-out SHA. Any
    git failure, timeout or missing binary raises :class:`RepoCloneError`; the
    message names the URL with any credential redacted and never carries git's
    stderr.
    """
    env = _config(environ)
    target = managed_path(url, env)
    canonical = _canonical_url(url, env)
    safe_url = _redact(canonical)
    token = _token_file(env)
    with _askpass(token) as auth:
        child_env = _child_env(auth)
        if _is_existing_clone(target, safe_url):
            _refresh(target, canonical, safe_url, child_env, timeout)
            operation = "fetch"
        else:
            _clone(target, canonical, safe_url, child_env, timeout)
            operation = "clone"
        commit = _head_commit(target, child_env, timeout, safe_url)
    LOG.info(
        "repository ready",
        extra={
            "event": "repo_cloned" if operation == "clone" else "repo_fetched",
            "url": safe_url,
            "path": str(target),
            "commit": commit,
        },
    )
    repo = ClonedRepo(str(target))
    repo._commit = commit
    return repo
