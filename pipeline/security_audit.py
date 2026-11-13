"""Per-repo record of the last security audit.

The periodic security-audit reminder needs to know, for each repository, which
commit was last security-audited and when. The record itself is a namespaced
record of the store (ADR 0012 decision 3): this module names the key a
repository is recorded under and reads and writes it through a records object.

The module also owns the pure decision logic for "is this repo due an audit":
the two thresholds read from the environment, the count of commits since the
last audited commit, and the reason (if any) the repo is due. The decision
itself takes no environment, clock, git or file access, so it is trivially
testable; the scheduler wiring lives in a sibling module.
"""

import hashlib
import logging
import os
import re
import subprocess
from collections.abc import Mapping
from datetime import datetime, timedelta

from .store_records import DEFAULT_NAMESPACE

logger = logging.getLogger(__name__)

_SHA_PATTERN = re.compile(r"[0-9a-f]{7,64}")

COMMITS_ENV = "PIPELINE_SECURITY_AUDIT_EVERY_COMMITS"
DAYS_ENV = "PIPELINE_SECURITY_AUDIT_EVERY_DAYS"

_GIT_TIMEOUT_SECONDS = 30


def repo_key(repo_root) -> str:
    """Return the record key for ``repo_root``: 16 hex characters of its digest.

    The digest is taken over the normalized root, so trailing separators do not
    produce a second record for the same repository.
    """
    return hashlib.sha256(os.path.normpath(repo_root).encode("utf-8")).hexdigest()[:16]


def read_audit_state(records, repo_root) -> dict | None:
    """Return the recorded audit state for ``repo_root``, or ``None``.

    ``None`` means no usable record: nothing was recorded, or the store could
    not read a usable one.
    """
    return records.get_security_audit_state(DEFAULT_NAMESPACE, repo_key(repo_root))


def record_audit(records, repo_root, sha, now: datetime) -> dict:
    """Record ``sha`` as the last security-audited commit for ``repo_root``.

    Validation happens before the store is touched, so a rejected call never
    replaces an existing record.
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
    records.put_security_audit_state(DEFAULT_NAMESPACE, repo_key(repo_root), state)
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
