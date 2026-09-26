"""Per-repo record of the last security audit.

The periodic security-audit reminder needs to know, for each repository, which
commit was last security-audited and when. This module owns only that record:
where it lives on disk, how it is read back, and how it is written.

State lives in one JSON file per repository inside a caller-supplied state
directory, named after a digest of the normalized repo root so that
``/repos/app`` and ``/repos/app/`` share a file while distinct repositories
never collide. Reads are forgiving - a missing, unreadable, malformed or
wrong-shaped file is treated as "no audit recorded" - while writes are strict
and atomic, so a crash can never leave a half-written record behind.

The module also owns the pure decision logic for "is this repo due an audit":
the two thresholds read from the environment, the count of commits since the
last audited commit, and the reason (if any) the repo is due. The decision
itself takes no environment, clock, git or file access, so it is trivially
testable; the scheduler wiring lives in a sibling module.
"""

import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

_SHA_PATTERN = re.compile(r"[0-9a-f]{7,64}")

COMMITS_ENV = "PIPELINE_SECURITY_AUDIT_EVERY_COMMITS"
DAYS_ENV = "PIPELINE_SECURITY_AUDIT_EVERY_DAYS"

_GIT_TIMEOUT_SECONDS = 30


def state_path(state_dir, repo_root) -> Path:
    """Return the state file path for ``repo_root`` inside ``state_dir``.

    The file name is derived from the sha256 of the normalized repo root, so
    trailing separators do not produce a second file for the same repository.
    """
    digest = hashlib.sha256(os.path.normpath(repo_root).encode("utf-8")).hexdigest()[:16]
    return Path(state_dir) / f"security-audit.{digest}.json"


def read_audit_state(state_dir, repo_root) -> dict | None:
    """Return the recorded audit state for ``repo_root``, or ``None``.

    ``None`` means "no usable record": the file is missing, unreadable, not
    valid JSON, not a JSON object, or lacks a string ``last_audited_sha``. A
    file that exists but cannot be used is logged once at WARNING level - the
    file name only, never its contents - and then treated as absent.
    """
    path = state_path(state_dir, repo_root)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        logger.warning("security audit state file %s could not be read", path.name)
        return None

    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("security audit state file %s is not valid JSON", path.name)
        return None

    if not isinstance(data, dict) or not isinstance(data.get("last_audited_sha"), str):
        logger.warning("security audit state file %s has an unexpected shape", path.name)
        return None

    return data


def record_audit(state_dir, repo_root, sha, now: datetime) -> dict:
    """Record ``sha`` as the last security-audited commit for ``repo_root``.

    Validation happens before anything touches the filesystem, so a rejected
    call never truncates an existing record and never leaves a temp file
    behind. The write itself is atomic: the state is written to a temp file in
    the state directory and then moved into place with ``os.replace``.
    """
    if not repo_root:
        raise ValueError(f"repo_root must be a non-empty string (got {repo_root!r})")
    if not isinstance(sha, str) or _SHA_PATTERN.fullmatch(sha) is None:
        raise ValueError(f"sha must be 7-64 lowercase hex characters (got {sha!r})")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware (naive datetime has no timezone)")

    state = {
        "repo_root": repo_root,
        "last_audited_sha": sha,
        "last_audited_at": now.isoformat(),
    }

    fd, tmp_name = tempfile.mkstemp(dir=str(state_dir), prefix=".security-audit-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.replace(tmp_name, state_path(state_dir, repo_root))
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    return state


def audit_thresholds(environ: Mapping) -> tuple[int, int]:
    """Return ``(every_commits, every_days)`` read from ``environ``.

    A value that is not a non-negative integer - absent, empty, negative or
    fractional - yields 0, which means "that check is off". Both checks off is
    the default, so an unconfigured pipeline never reports a repo as due.
    """

    def _read(name: str) -> int:
        raw = environ.get(name)
        if not isinstance(raw, str) or not raw.isdecimal():
            return 0
        return int(raw)

    return _read(COMMITS_ENV), _read(DAYS_ENV)


def commits_since(repo_root, sha) -> int | None:
    """Return the number of commits in ``repo_root`` after ``sha``.

    ``None`` means the count could not be determined: ``sha`` is unknown, the
    directory is not a git repository, git is unavailable, or it did not
    finish within the timeout.
    """
    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", f"{sha}..HEAD"],
            cwd=repo_root,
            capture_output=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning("could not count commits since %s in %s", sha, repo_root)
        return None

    if result.returncode != 0:
        logger.warning("could not count commits since %s in %s", sha, repo_root)
        return None

    try:
        return int(result.stdout.decode("utf-8", errors="replace").strip())
    except ValueError:
        logger.warning("unexpected git output counting commits since %s in %s", sha, repo_root)
        return None


def audit_due_reason(state, commits, now, every_commits, every_days) -> str | None:
    """Return why the repo is due a security audit, or ``None`` if it is not.

    Pure: it reads no environment, clock, git or files. ``state`` is the record
    written by :func:`record_audit` (or ``None``), ``commits`` the count from
    :func:`commits_since` (or ``None``), and the thresholds come from
    :func:`audit_thresholds`. Both thresholds are inclusive.
    """
    if every_commits <= 0 and every_days <= 0:
        return None
    if state is None:
        return "no security audit on record for this repo"
    if commits is None:
        return "the last audited commit is no longer in this repo's history"
    if every_commits > 0 and commits >= every_commits:
        return f"{commits} commits since the last audit (threshold {every_commits})"
    if every_days > 0:
        try:
            audited_at = datetime.fromisoformat(state["last_audited_at"])
        except (KeyError, TypeError, ValueError):
            return "no security audit on record for this repo"
        if audited_at.tzinfo is None or audited_at.utcoffset() is None:
            return "no security audit on record for this repo"
        elapsed = now - audited_at
        if elapsed >= timedelta(days=every_days):
            return f"{elapsed.days} days since the last audit (threshold {every_days})"
    return None
