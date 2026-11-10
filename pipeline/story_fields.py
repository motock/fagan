"""The single write path for story fields that can be written concurrently.

``pipeline/dispatch.py`` (baseline recording), ``pipeline/detached_grade.py``
(grade results) and ``pipeline/story_status.py`` (status/test-check writes) all
mutate one story in a manifest that other writers may be holding a copy of.
Each of them used to read the manifest, mutate its in-memory copy and write the
WHOLE manifest back. Whenever two of those ran interleaved - the dispatch runs
with the plan lock released (LOCKSTARVE-B3) - the later whole-manifest write
persisted a copy that predated the other writer, silently dropping its fields.
In the b3+b4 runs that lost ``story["baseline_test_check"]`` (BASELINE-1) five
times, each loss needing a manual pause/edit/resume.

Every write that can race a dispatch, a patch or a grade therefore goes through
``apply_story_fields``: it takes the plan lock, RE-READS the manifest inside the
lock, merges only the caller's fields into the fresh story dict, saves and
returns the fresh story. Callers must never pass a manifest or story copy they
read earlier - that is the very snapshot the helper exists to avoid.

This module is also where the dispatch baseline marker is parsed: the marker
carries the baseline's ``returncode`` and failing node ids so a redispatch onto
a worktree that already carries it can reconstruct the baseline without
re-running the suite, keeping ``dispatch.py`` under its line budget.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# The baseline marker's NAME lives in ``pipeline/dispatch.py``, its only writer
# (pinned by ``test_marker_name_is_the_one_dispatch_writes``): every helper
# below takes the marker path the dispatch built, so there is exactly one
# source of truth for it.


def apply_story_fields(
    store: Any,
    plan_name: str,
    story_key: str,
    fields: dict[str, Any],
    wait_seconds: float | None = None,
) -> dict[str, Any] | None:
    """Merge ``fields`` into one story of ``plan_name``'s manifest, atomically.

    Returns the fresh story dict, or ``None`` when the plan lock could not be
    acquired (nothing is written in that case) or the story does not exist.
    """
    if wait_seconds is None:
        transaction = store.transaction(plan_name)
    else:
        transaction = store.transaction(plan_name, wait_seconds)
    with transaction as acquired:
        if not acquired:
            return None
        manifest = store.get_manifest(plan_name)
        stories = manifest.get("stories") if isinstance(manifest, dict) else None
        story = stories.get(story_key) if isinstance(stories, dict) else None
        if not isinstance(story, dict):
            return None
        story.update(fields)
        store.save_manifest(plan_name, manifest)
        return story


def read_baseline_marker(marker: Path | str) -> dict[str, Any] | None:
    """The baseline the worktree's marker implies, or ``None`` when there is none.

    The marker is written by the dispatch in two shapes: the current JSON
    ``{"returncode": ..., "failed_node_ids": [...]}`` and the older bare id
    list / ``"ok"`` sentinel. A marker that carries no failing node ids implies
    nothing was failing before the dispatch, so no failing baseline is
    reconstructed for it - recording ``{"failed_node_ids": []}`` would make
    later code believe a failing baseline exists and exempt zero failures,
    turning an environmental red into an unexplained one.
    """
    try:
        payload = json.loads(Path(marker).read_text())
    except (OSError, ValueError):
        return None
    if isinstance(payload, dict):
        ids = payload.get("failed_node_ids")
    elif isinstance(payload, list):
        ids = payload
    else:
        return None
    if not isinstance(ids, list) or not ids:
        return None
    return {"returncode": 1, "failed_node_ids": [str(node_id) for node_id in ids]}


def merge_into_fresh_manifest(
    store: Any,
    plan_name: str,
    story_key: str,
    story: dict[str, Any],
    manifest: dict[str, Any] | None = None,
    drop: tuple[str, ...] = (),
    keys_at_entry: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    """Re-read the manifest and merge ONE story's fields onto the fresh copy.

    The tick-side callers of this helper already hold the plan lock and persist
    through their own seam (``_store.records().put_manifest``), so this does not
    take a second lock and does not write: it hands back a manifest whose target
    story is the FRESH one with the caller's fields merged in. Saving the
    caller's pre-read manifest instead loses every field another writer landed
    in between - the baseline the dispatch recorded (Path C), for one.
    """
    fresh = store.get_manifest(plan_name)
    if not isinstance(fresh, dict):
        return None
    stories = fresh.get("stories")
    if not isinstance(stories, dict):
        return None
    target = stories.get(story_key)
    if not isinstance(target, dict):
        return None
    if isinstance(manifest, dict):
        # Plan-level fields (role_config, escalation, ...) are the caller's;
        # only the story is merged field-by-field.
        for key, value in manifest.items():
            if key != "stories":
                fresh[key] = value
    target.update(story)
    for key in drop:
        if key not in story:
            target.pop(key, None)
    # A field the caller's read carried and then CONSUMED (popped) is a
    # deletion, not "unchanged": drop it from the fresh copy too, or the
    # bookkeeping the caller cleared comes straight back.
    for key in keys_at_entry:
        if key not in story:
            target.pop(key, None)
    return fresh


def failing_baseline(story: Any) -> dict[str, Any] | None:
    """``story``'s recorded baseline when it says the tree was ALREADY red."""
    baseline = story.get("baseline_test_check") if isinstance(story, dict) else None
    if isinstance(baseline, dict) and baseline.get("returncode") not in (None, 0):
        return baseline
    return None


def _write_baseline_marker(marker: Path, baseline: dict[str, Any] | None) -> None:
    """Record the baseline in the worktree marker as ``returncode`` + node ids."""
    payload = {
        "returncode": (baseline or {}).get("returncode", 0),
        "failed_node_ids": (baseline or {}).get("failed_node_ids") or [],
    }
    try:
        marker.write_text(json.dumps(payload))
    except OSError:
        pass


def record_dispatch_baseline(
    store: Any,
    plan_name: str,
    story_key: str,
    story: dict[str, Any],
    worktree_path: Path | str,
    *,
    marker: Path | str,
    snapshot: Any,
    rerun: bool,
) -> dict[str, Any]:
    """Record the pre-dispatch baseline for one story (BASELINE-1).

    ``snapshot(worktree_path)`` runs the worktree's test suite once; ``rerun``
    is False for a resuming dispatch (no meaningful "before" state is left).
    The snapshot is an observability hook, never a gate: a raising snapshot
    degrades to "no baseline" and the marker is still written, so it is not
    retried on every later dispatch. The marker carries the baseline's
    ``returncode`` and failing node ids, so a redispatch that finds the marker
    already there can restore the baseline instead of recording nothing.

    ``marker`` is the path the caller built from the worktree; the marker's
    name is the caller's to own, so this module never derives a second one.
    """
    marker = Path(marker)
    if rerun and not marker.exists():
        try:
            baseline = snapshot(worktree_path)
        except Exception:  # noqa: BLE001 (observability hook, never a gate)
            baseline = None
        _write_baseline_marker(marker, baseline)
        if baseline is not None and baseline.get("returncode") != 0:
            fresh = apply_story_fields(
                store, plan_name, story_key, {"baseline_test_check": baseline}
            )
            if fresh is not None:
                story.update(fresh)
    elif marker.exists():
        fresh = heal_baseline_from_marker(store, plan_name, story_key, story, marker)
        if fresh is not None:
            # Keep this dispatch's own in-memory copy in step with the manifest:
            # it is saved again later in the dispatch, and a copy that predates
            # the healed baseline would drop it again (the Path-B race).
            story.update(fresh)
    return story


def heal_baseline_from_marker(
    store: Any,
    plan_name: str,
    story_key: str,
    story: Any,
    marker: Path | str,
    wait_seconds: float | None = None,
) -> dict[str, Any] | None:
    """Persist the baseline a pre-existing marker implies, if the story lacks one.

    A redispatch onto a worktree that already carries the marker (startup
    reconcile after a scheduler restart, or a rework) used to record NOTHING:
    the recording was guarded on the marker being absent. The marker holds the
    failing node ids, so the baseline is still recoverable - record it, unless
    the story already carries a failing baseline (never overwrite it) or the
    marker implies no failure at all.

    ``marker`` is the path the dispatch built, not one derived here: the
    marker's name is owned by ``pipeline/dispatch.py``, its only writer.
    """
    if failing_baseline(story) is not None:
        return None
    baseline = read_baseline_marker(marker)
    if baseline is None:
        return None
    return apply_story_fields(
        store, plan_name, story_key, {"baseline_test_check": baseline}, wait_seconds
    )
