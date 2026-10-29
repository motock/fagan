"""Advance-owned detached review collection.

``advance.py`` used to review every ``tests_passed`` story inline, holding the
tick (and the plan lock) for a 60-400s model call. :mod:`pipeline.review_job`
runs that one review in a detached child process; this module is the caller
side - start the job on one advance tick, collect its result on a later one,
so one slow review no longer holds the advance.

Three invariants shape the caller side, all inherited from
:mod:`pipeline.advance_merge`:

* **A pending job changes nothing.**  A ``None`` from
  :func:`pipeline.review_job.collect_review_job` means "still running": the
  story stays ``tests_passed``, the handle stays on it, and the manifest is
  not rewritten.
* **Only the job's own ``done`` result ends a review.**  The result document is
  the child's verdict, never this module's guess; a missing, unparsable, or
  unrecognised result is an ``error``, which drops the handle so the next
  advance starts a fresh job and the review is retried rather than silently
  skipped.
* **The caller owns the manifest write.**  The advance holds the plan lock
  here, so the read-modify-write below is safe; the job itself never writes the
  manifest. Only the handle field is touched on disk - the review child writes
  the story's own status, and that write must survive.

Every failure is logged and notified by action, plan and story key only: an
exception's text can carry a filesystem path or a token (Secure by Design), so
only its class name is recorded.
"""

from __future__ import annotations

import json
import logging
import math
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import review_job
from .module_ref import _ModuleRef

__all__ = ["review_or_collect"]

#: Default wall-clock budget for one detached review, overridable with
#: ``PIPELINE_REVIEW_JOB_TIMEOUT_SECONDS``.
REVIEW_JOB_TIMEOUT_DEFAULT = 900.0

TIMEOUT_ENV_VAR = "PIPELINE_REVIEW_JOB_TIMEOUT_SECONDS"

# Resolved through pipeline.advance so ``monkeypatch.setattr(pipeline.advance,
# "_notify_user", ...)`` keeps landing, the same seam advance_merge uses.
_notify_user = _ModuleRef("pipeline.advance", "_notify_user")
_atomic_write_json = _ModuleRef("pipeline.advance", "_atomic_write_json")

log = logging.getLogger(__name__)


def review_or_collect(
    plan_name: str,
    story_key: str,
    story: dict[str, Any],
    manifest_path: Path,
    summary: dict[str, Any],
) -> None:
    """Advance one story's review by a tick: start it, or collect its result.

    The caller is the advance tick, under the plan lock, and only calls this
    for a story it just read as ``tests_passed`` - with one deliberate
    exception handled here: a story carrying a ``review_job`` handle but no
    longer ``tests_passed`` was moved by an operator, so its job is cancelled
    and the stale handle dropped instead of being collected.

    ``summary`` is the tick's summary dict; this function appends the
    ``review_started`` / ``review_pending`` / ``review_deferred`` keys the
    inline path also produces, and records the result in ``advanced``.
    """
    job = story.get("review_job")
    if job and story.get("status") != "tests_passed":
        # The operator moved the story on while the job was live. The job's
        # verdict would grade a review of work that is no longer waiting to be
        # reviewed, so stop it rather than let it burn a model call.
        review_job.cancel_review_job(job)
        story.pop("review_job", None)
        _drop_job(manifest_path, story_key)
        log.info(
            "review job cancelled: story moved on",
            extra={"plan": plan_name, "story": story_key},
        )
        return

    if not job:
        _start(plan_name, story_key, story, manifest_path, summary)
        return

    result = review_job.collect_review_job(job, _timeout(plan_name, story_key))
    if result is None:
        summary.setdefault("review_pending", []).append(story_key)
        return

    _apply(plan_name, story_key, story, manifest_path, summary, result)


# ---------------------------------------------------------------------------
# Internals


def _start(
    plan_name: str,
    story_key: str,
    story: dict[str, Any],
    manifest_path: Path,
    summary: dict[str, Any],
) -> None:
    """Spawn the detached job and record its handle on the story."""
    try:
        job = review_job.start_review_job(
            plan_name, story_key, _state_root(manifest_path)
        )
    except (ValueError, OSError) as exc:
        # Class name only: the exception's text can carry a path. The story is
        # untouched and still ``tests_passed``, so the next advance retries.
        log.warning(
            "review job could not start: %s",
            type(exc).__name__,
            extra={"plan": plan_name, "story": story_key},
        )
        return
    story["review_job"] = job

    def _set(stored: dict[str, Any]) -> None:
        stored["review_job"] = job

    _persist(manifest_path, story_key, _set)
    summary.setdefault("review_started", []).append(story_key)


def _apply(
    plan_name: str,
    story_key: str,
    story: dict[str, Any],
    manifest_path: Path,
    summary: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """Apply a finished job's result: the handle goes, the verdict is reported."""
    story.pop("review_job", None)
    _drop_job(manifest_path, story_key)
    if result.get("state") != "done":
        # A harness error, never a verdict: the review did not run to a
        # conclusion, so the story keeps whatever status the child left and the
        # next advance starts a fresh job. One notice per error - the handle is
        # gone, so a later tick cannot repeat it for this job.
        _notify_user(
            plan_name,
            f"{story_key} review job failed; the review will be retried on a "
            "later advance.",
            story_key=story_key,
            severity="warning",
            event="review_job_error",
            dedup_key=f"review_job_error:{plan_name}:{story_key}",
        )
        log.warning(
            "review job reported an error",
            extra={"plan": plan_name, "story": story_key},
        )
        return
    summary.setdefault("advanced", []).append({story_key: result.get("status")})
    if result.get("deferred") == "rate_limited":
        summary.setdefault("review_deferred", []).append(story_key)


def _timeout(plan_name: str, story_key: str) -> float:
    """The collect budget, falling back to the default on a bad override.

    A malformed, non-finite (``nan``/``inf``) or non-positive value can never
    be handed to the collector: ``nan`` fails every age comparison (the job
    would never time out) and ``inf`` would hand the collector an unbounded
    budget. Both fall back to :data:`REVIEW_JOB_TIMEOUT_DEFAULT` with a warning
    so an operator's typo is visible instead of silently ignored.
    """
    raw = os.environ.get(TIMEOUT_ENV_VAR)
    if raw is None:
        return REVIEW_JOB_TIMEOUT_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    if math.isfinite(value) and value > 0:
        return value
    log.warning(
        "invalid %s ignored: collecting with the %gs default",
        TIMEOUT_ENV_VAR,
        REVIEW_JOB_TIMEOUT_DEFAULT,
        extra={"plan": plan_name, "story": story_key},
    )
    return REVIEW_JOB_TIMEOUT_DEFAULT


def _state_root(manifest_path: Path) -> Path:
    """Where the job's result file lives - the merge re-verify rule.

    ``PIPELINE_STATE_DIR`` wins when set; a relative override resolves against
    the manifest's directory, never the process CWD, because the collect tick
    may run somewhere else entirely. Without an override the manifest's own
    directory is used, so a plan keeps its state next to itself.
    """
    base = Path(manifest_path).parent
    env_dir = os.environ.get("PIPELINE_STATE_DIR")
    if not env_dir:
        return base
    state_root = Path(env_dir)
    if not state_root.is_absolute():
        return base / state_root
    return state_root


def _drop_job(manifest_path: Path, story_key: str) -> None:
    def _unset(stored: dict[str, Any]) -> None:
        stored.pop("review_job", None)

    _persist(manifest_path, story_key, _unset)


def _persist(
    manifest_path: Path,
    story_key: str,
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    """Re-read the manifest, apply ``mutate`` to this story, write it atomically.

    The advance holds the plan lock here, so a read-modify-write cannot race
    another actor. Only the story being handled is touched, and only through
    ``mutate``: the review child writes that story's status on its own, and
    anything else that ran while the lock was released must survive too.
    """
    manifest = json.loads(Path(manifest_path).read_text())
    stored = manifest["stories"].get(story_key)
    if stored is None:
        # Re-ingested away between the tick's fresh read and now: nothing to
        # write, and re-creating the story would resurrect a dropped one.
        return
    mutate(stored)
    _atomic_write_json(Path(manifest_path), manifest)
