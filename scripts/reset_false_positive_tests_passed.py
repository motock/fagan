#!/usr/bin/env python
"""Reset false-positive `tests_passed` stories back to `interrupted`.

The orchestrator's `tests_passed` was previously granted whenever the
test command returned 0 against the worktree, regardless of whether the
agent branch actually had new commits vs the base branch. Stories stuck
in this state are NOT evidence of work — main's own suite still passes
against an untouched worktree, so `cargo test` returns 0 even when the
agent parked mid-exploration without writing any code.

This script is a one-shot cleanup for the four e2e-decentralized-
messaging-roadmap stories that landed in this state on 2026-06-27
(2fe7bb3c, 421b308b, d51bcacb, ec34f176). It was run on 2026-06-27
and those four UUIDs are now `interrupted` in the manifest. The script
is idempotent — re-running it on the current fleet is a no-op because
the targets are no longer in `tests_passed`. It stays in the repo so
the same trap can be cleared if a future plan hits it before the gate
in pipeline_mcp_server.check_story_status ships, or for forensic use
if the gate ever regresses.

Behavior:
- For each target UUID, verify the false-positive signature by calling
  the same `_worktree_has_new_commits` helper the new gate uses. Skip
  any UUID that doesn't match (real success, or already reset).
- Set `status` to `interrupted` so the scheduler re-dispatches it.
- Clear review-loop fields (review_verdict, review_feedback,
  rework_attempts, failure_reason) and dispatch-loop fields (pid,
  dispatch_attempts) so the next dispatch starts fresh, not as a
  rework cycle.
- Leave `worktree` and `log` paths on disk. dispatch_story's resume
  logic reuses the existing worktree+branch when present, so the
  reset is cheap and the new agent starts in the same dir with the
  same empty branch — but with a fresh journal so it doesn't carry
  the previous run's stale "next_hint".

Run from the repo root:

    .venv/bin/python scripts/reset_false_positive_tests_passed.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Repo root: this file lives in scripts/ alongside the pipeline code.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pipeline_mcp_server as p

PLAN_NAME = "e2e-decentralized-messaging-roadmap"
PLAN_DIR = Path("~/.claude/plans").expanduser()
MANIFEST_PATH = PLAN_DIR / f"{PLAN_NAME}.manifest.json"

# Four UUIDs from the 2026-06-27 fleet run. Each one is in
# `tests_passed` with an empty agent branch (zero commits vs main) —
# the false-positive signature.
TARGETS = [
    "2fe7bb3c-e969-405b-9936-9ed967343fe1",  # Prekey auto-replenishment
    "421b308b-7f02-4e8c-8365-1515c9734164",  # PoW first-contact
    "d51bcacb-d4be-45cc-ac5d-1fbf67bfb64c",  # Persist identity state
    "ec34f176-ec06-43e8-8d03-e9f78fe09eb1",  # DM delivery
]

# Fields to clear on reset so the next dispatch starts clean rather
# than inheriting stale review/dispatch bookkeeping.
CLEAR_FIELDS = (
    "review_verdict",
    "review_feedback",
    "rework_attempts",
    "pid",
    "dispatch_attempts",
    "failure_reason",
    "merge_error",
    "interrupted_at",
)


def reset_story(manifest: dict, story_key: str) -> str | None:
    """Return a human-readable action description, or None to skip."""
    story = manifest["stories"].get(story_key)
    if story is None:
        return f"SKIP  {story_key}: not in manifest"
    if story.get("status") != "tests_passed":
        return (f"SKIP  {story_key}: status={story.get('status')!r} "
                "(not the false-positive status)")

    worktree = story.get("worktree")
    if not worktree:
        return f"SKIP  {story_key}: no worktree path recorded"

    # Same signature check the new gate uses.
    base = p._default_branch()
    if p._worktree_has_new_commits(Path(worktree), story_key, base_branch=base):
        return (f"SKIP  {story_key}: agent branch has new commits vs "
                f"{base} — not a false positive")

    # Genuine false-positive. Reset.
    story["status"] = "interrupted"
    for field in CLEAR_FIELDS:
        story.pop(field, None)
    return (f"RESET {story_key} ({story.get('summary', '')!r}): "
            "tests_passed -> interrupted, cleared review/dispatch fields")


def main() -> int:
    if not MANIFEST_PATH.exists():
        print(f"ERROR: manifest not found at {MANIFEST_PATH}", file=sys.stderr)
        return 1

    manifest = json.loads(MANIFEST_PATH.read_text())
    actions = [reset_story(manifest, key) for key in TARGETS]

    reset_count = sum(1 for a in actions if a.startswith("RESET"))
    if reset_count == 0:
        for a in actions:
            print(a)
        print("\nNo changes written. (Nothing matched the false-positive signature.)")
        return 0

    # Write atomically: temp file + rename, so a crash mid-write
    # doesn't corrupt the manifest.
    tmp_path = MANIFEST_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(manifest, indent=2))
    tmp_path.rename(MANIFEST_PATH)

    for a in actions:
        print(a)
    print(f"\n{reset_count}/{len(TARGETS)} stories reset. Manifest updated.")
    print("Next scheduler tick will re-dispatch the reset stories.")
    return 0


if __name__ == "__main__":
    sys.exit(main())