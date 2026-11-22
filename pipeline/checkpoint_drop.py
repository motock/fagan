"""Agent checkpoints dropped as a file in the worktree.

A sandboxed agent has no store credentials, so instead of journalling its
own checkpoint it writes ``.agent_checkpoint`` in its worktree. The control
plane claims that file from its worktree scan and journals it.

The file is written by untrusted code: every byte is hostile input. A drop
is claimed by rename (never opened by its own name), opened without
following links, size-capped before it is parsed, and deleted whether or
not it was accepted. Logs carry a reason code only.

Standard library only at module level: ``pipeline.checkpoint`` imports
``pipeline.git_ops``, so the journal write is imported inside ``ingest``.
"""

import contextlib
import json
import logging
import os
import re
import secrets
import stat
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

DROP_NAME = ".agent_checkpoint"
MODE_ENV = "PIPELINE_CHECKPOINT_MODE"

_CLAIMED_NAME = DROP_NAME + ".claimed"
_MAX_BYTES = 16384
_MAX_STEP = 120
_MAX_SUMMARY = 4000
_MAX_NEXT_HINT = 2000
_COMMIT_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def is_file_mode() -> bool:
    """True only when the mode variable, stripped and lowercased, is 'file'."""
    return os.environ.get(MODE_ENV, "").strip().lower() == "file"


def write(worktree, step, summary, next_hint="", commit=None) -> None:
    """Agent side: publish one checkpoint as the drop file.

    The JSON is written to a uniquely named temporary file (exclusive create,
    owner-only) and renamed onto the drop name, so the control plane never
    reads a half-written drop.
    """
    directory = os.fspath(worktree)
    payload = {"step": step, "summary": summary, "next_hint": next_hint, "commit": commit}
    data = json.dumps(payload).encode("utf-8")
    temp = os.path.join(directory, f"{DROP_NAME}.{secrets.token_hex(8)}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(temp, os.path.join(directory, DROP_NAME))
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)
        raise


def claim(worktree) -> dict | None:
    """Control-plane side: take the drop and return its validated fields.

    Returns None when there is no drop, when another scan claimed it first,
    or when the drop is refused. A refused drop is deleted and logged with a
    reason code only.
    """
    directory = os.fspath(worktree)
    drop = os.path.join(directory, DROP_NAME)
    claimed = os.path.join(directory, _CLAIMED_NAME)
    try:
        os.lstat(drop)
    except FileNotFoundError:
        return None
    try:
        os.replace(drop, claimed)
    except FileNotFoundError:
        return None
    try:
        return _read_claimed(claimed)
    finally:
        _remove(claimed)


def _read_claimed(path: str) -> dict | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return _refuse("not_regular")
    # A directory opens read-only without error, so the type is checked on
    # the open descriptor before anything is read from it.
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        return _refuse("not_regular")
    with os.fdopen(fd, "rb") as fh:
        raw = fh.read(_MAX_BYTES + 1)
    if len(raw) > _MAX_BYTES:
        return _refuse("too_large")
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except ValueError:
        return _refuse("not_json")
    return _validate(parsed)


def _validate(parsed) -> dict | None:
    if not isinstance(parsed, dict):
        return _refuse("bad_field")
    step = parsed.get("step")
    if not isinstance(step, str) or not 1 <= len(step.strip()) <= _MAX_STEP:
        return _refuse("bad_field")
    summary = parsed.get("summary", "")
    if not isinstance(summary, str) or len(summary) > _MAX_SUMMARY:
        return _refuse("bad_field")
    next_hint = parsed.get("next_hint", "")
    if not isinstance(next_hint, str) or len(next_hint) > _MAX_NEXT_HINT:
        return _refuse("bad_field")
    commit = parsed.get("commit")
    if not (isinstance(commit, str) and _COMMIT_RE.fullmatch(commit)):
        commit = None
    return {"step": step.strip(), "summary": summary, "next_hint": next_hint, "commit": commit}


def _refuse(reason: str) -> None:
    logger.warning("agent checkpoint drop refused", extra={"reason": reason})


def _remove(path: str) -> None:
    try:
        os.unlink(path)
    except IsADirectoryError:
        with contextlib.suppress(OSError):
            os.rmdir(path)
    except FileNotFoundError:
        pass


def ingest(plan_name, story_key, story) -> dict | None:
    """Journal one story's drop. None unless file mode is on and the
    story's worktree is an existing directory. Never runs git and never
    touches any other file in the worktree."""
    if not is_file_mode():
        return None
    worktree = story.get("worktree")
    if not worktree or not os.path.isdir(worktree):
        return None
    record = claim(worktree)
    if record is None:
        return None
    entry = {**record, "ts": datetime.now(UTC).isoformat()}
    from .checkpoint import _append_journal
    from .store_records import InvalidRecordKeyError

    try:
        _append_journal(plan_name, story_key, entry)
    except InvalidRecordKeyError:
        logger.warning(
            "agent checkpoint drop rejected: the story key is not a valid record key",
            extra={"plan": plan_name},
        )
        return None
    return entry


def scan(manifest, plan_name) -> int:
    """Journal the drop of every in-progress story. One story's failure is
    logged by exception type and does not stop the others."""
    if not is_file_mode():
        return 0
    journalled = 0
    for story_key, story in (manifest.get("stories") or {}).items():
        if not isinstance(story, dict) or story.get("status") != "in_progress":
            continue
        try:
            if ingest(plan_name, story_key, story) is not None:
                journalled += 1
        except Exception as exc:  # noqa: BLE001 - one story must not stop the scan
            logger.warning(
                "agent checkpoint drop scan failed for a story: %s",
                type(exc).__name__,
                extra={"plan": plan_name, "story": story_key},
            )
    return journalled
