"""One-time copy of a plan's staged draft fields into its existing manifest.

A plan's *description*, *epic* and authored *key* (and the plan-level
``plan_epics``, ``preflight_override`` and ``repo_url``) only started landing on
the manifest when the manifest-fields story shipped.  Plans ingested before it
carry the plan itself in their transient draft (``PLAN_DIR/<plan>.json``, ADR
0012 decision 3) but nothing descriptive on the manifest, so an export built
from the manifest would lose it.  This command copies those fields across.

**Run this BEFORE deploying the draft-consumption story** (which deletes drafts
once ingest has consumed them: with the draft gone there is nothing to copy) and
**BEFORE the digest-version-2 build** (which makes ``description`` a digest
member).  Under version 1 the copied fields are not digest members, so the plan's
``canonical_digest`` is unchanged and its approval still stands.  Run after the
version-2 build, every plan whose draft has a non-empty description reports
``skipped_digest`` -- correct and fail-closed, nothing is written -- and its
descriptions can then only be recorded by ``save_plan`` + re-ingest, which
resets its approval.

Safety:

* It writes only the descriptive fields named above -- never an approval, a
  decision, a journal entry or runtime story state -- so no re-approval is
  needed and the ``approval`` block stays byte-identical.
* It verifies the canonical digest *before* and *after* on an in-memory
  prospective manifest and writes only when they are equal: never
  write-then-verify, so a refused plan is left exactly as it was.
* A plan whose draft is missing, unreadable or already recorded is reported and
  skipped; a draft story with no matching manifest story is reported and not
  added.
* Idempotent: the second run reports ``unchanged`` for every plan and writes
  nothing.

Output is one JSON summary line per plan, ``{"plan": ..., "outcome": ...}`` plus
``"unmatched_stories"`` when a draft story had no manifest story to land on.
Outcomes: ``backfilled``, ``unchanged``, ``skipped_no_draft``,
``skipped_unreadable``, ``skipped_digest``, ``skipped_locked``.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .. import plan_approval
from ..concurrency import command_lock_wait_seconds
from ..store_records import DEFAULT_NAMESPACE, UnreadableRecordError

#: Fields copied from a draft story onto the manifest story of the same key.
STORY_FIELDS = ("description", "epic", "plan_key")

#: Plan-level fields copied from the draft: ``plan_epics`` always, the rest only
#: when the draft names them.
PLAN_FIELDS = ("plan_epics", "preflight_override", "repo_url")

BACKFILLED = "backfilled"
UNCHANGED = "unchanged"
SKIPPED_NO_DRAFT = "skipped_no_draft"
SKIPPED_UNREADABLE = "skipped_unreadable"
SKIPPED_DIGEST = "skipped_digest"
SKIPPED_LOCKED = "skipped_locked"

#: Outcomes meaning "a plan still needs attention" (exit code 1), as in the
#: sibling ``reset_outdated_approvals`` command.
_SKIP_OUTCOMES = (
    SKIPPED_NO_DRAFT,
    SKIPPED_UNREADABLE,
    SKIPPED_DIGEST,
    SKIPPED_LOCKED,
)


def _store() -> Any:
    """Return the live store, resolved per call (``_store`` is built at import)."""
    from .. import server

    return server._store


def _records() -> Any:
    """Return the namespaced record view of the live store."""
    return _store().records()


def _plan_names(records: Any) -> list[str]:
    """Return the sorted names of every plan that has a manifest."""
    return sorted(records.get_manifests(DEFAULT_NAMESPACE))


def _draft_story_key(story: dict) -> str | None:
    """Return the manifest key ``story`` would be stored under, if it names one."""
    key = story.get("key")
    return key if isinstance(key, str) and key else None


def _match_story(draft_story: dict, stories: dict[str, dict]) -> str | None:
    """Return the manifest story key a draft story belongs to, or ``None``.

    The manifest records the plan's own story key as ``plan_key``
    (``pipeline.ingest_manifest``), so a draft story that names a ``key`` is
    matched on that.  A keyless draft story falls back to the authored summary:
    both are plan-authored, and neither invents a story the manifest never had
    (a story with no match is reported and skipped).
    """
    key = _draft_story_key(draft_story)
    if key is not None:
        for story_key, story in stories.items():
            if isinstance(story, dict) and story.get("plan_key") == key:
                return story_key
    summary = draft_story.get("summary")
    if not isinstance(summary, str):
        return None
    for story_key, story in stories.items():
        if isinstance(story, dict) and story.get("summary") == summary:
            return story_key
    return None


def _prospective(
    manifest: dict, draft: dict
) -> tuple[dict, list[str]]:
    """Return ``(prospective_manifest, unmatched_draft_story_keys)``.

    Only the descriptive fields are copied, and only onto stories the manifest
    already has: a draft story with no manifest story is reported, never added.
    The input manifest is not mutated.
    """
    prospective = json.loads(json.dumps(manifest))
    stories = prospective.get("stories")
    if not isinstance(stories, dict):
        stories = {}
    draft_stories = draft.get("stories")
    if not isinstance(draft_stories, dict):
        # A draft may nest its stories under its epics, as a plan document does.
        draft_stories = {}
        for epic in draft.get("epics", []) or []:
            if not isinstance(epic, dict):
                continue
            for story in epic.get("stories", []) or []:
                if isinstance(story, dict):
                    draft_stories.setdefault(
                        _draft_story_key(story) or story.get("summary", ""), story
                    )
    unmatched: list[str] = []
    for draft_key, draft_story in draft_stories.items():
        if not isinstance(draft_story, dict):
            continue
        story_key = _match_story(draft_story, stories)
        if story_key is None:
            unmatched.append(
                _draft_story_key(draft_story)
                or (draft_story.get("summary") if isinstance(draft_story.get("summary"), str) else draft_key)
            )
            continue
        story = stories[story_key]
        for field in STORY_FIELDS:
            if field in draft_story:
                story[field] = draft_story[field]
        # A plan document keys its stories by an authored ``key``; the manifest
        # records that same key as ``plan_key`` (``ingest_manifest``).
        if "key" in draft_story:
            story["plan_key"] = draft_story["key"]
    # Plan-level descriptive fields, from the draft's own plan document.
    epics = draft.get("epics")
    if isinstance(epics, list):
        prospective["plan_epics"] = [
            epic.get("summary") for epic in epics if isinstance(epic, dict)
        ]
    for field in PLAN_FIELDS[1:]:
        if field in draft:
            prospective[field] = draft[field]
    return prospective, unmatched


def _backfill_one(records: Any, plan_name: str, *, dry_run: bool) -> dict[str, Any]:
    """Return the summary entry for one plan, writing only when it is safe."""
    entry: dict[str, Any] = {"plan": plan_name}
    wait_seconds = command_lock_wait_seconds()
    with records.transaction(DEFAULT_NAMESPACE, plan_name, wait_seconds) as acquired:
        if not acquired:
            entry["outcome"] = SKIPPED_LOCKED
            return entry
        try:
            draft = records.get_plan_draft(DEFAULT_NAMESPACE, plan_name)
        except UnreadableRecordError:
            entry["outcome"] = SKIPPED_UNREADABLE
            return entry
        if not isinstance(draft, dict):
            entry["outcome"] = SKIPPED_NO_DRAFT
            return entry
        manifest = records.get_manifest(DEFAULT_NAMESPACE, plan_name)
        if not isinstance(manifest, dict):
            entry["outcome"] = SKIPPED_UNREADABLE
            return entry
        prospective, unmatched = _prospective(manifest, draft)
        if unmatched:
            entry["unmatched_stories"] = unmatched
        if prospective == manifest:
            entry["outcome"] = UNCHANGED
            return entry
        # Verify the digest on the prospective document, never after a write.
        before = plan_approval.canonical_digest(manifest)
        after = plan_approval.canonical_digest(prospective)
        recorded = manifest.get("canonical_digest")
        if before != after or (recorded is not None and recorded != after):
            entry["outcome"] = SKIPPED_DIGEST
            return entry
        if not dry_run:
            records.put_manifest(DEFAULT_NAMESPACE, plan_name, prospective)
        entry["outcome"] = BACKFILLED
        return entry


def run(*, dry_run: bool = False) -> dict[str, Any]:
    """Backfill every plan's manifest; return the per-plan entries and summary.

    ``dry_run`` computes exactly the same outcomes and writes nothing: the same
    prospective manifest is built and the same digest check is made, then
    discarded.
    """
    records = _records()
    plans = [
        _backfill_one(records, plan_name, dry_run=dry_run)
        for plan_name in _plan_names(records)
    ]
    summary = {outcome: 0 for outcome in (BACKFILLED, UNCHANGED, *_SKIP_OUTCOMES)}
    for entry in plans:
        summary[entry["outcome"]] = summary.get(entry["outcome"], 0) + 1
    return {"plans": plans, "summary": summary, "dry_run": dry_run}


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: one JSON line per plan, then the summary."""
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.tools.backfill_plan_source",
        description=(
            "Copy description, epic, plan_key, plan_epics, preflight_override and "
            "repo_url from each plan's staged draft onto its existing manifest. "
            "Run it before deploying the draft-consumption story and before the "
            "digest-version-2 build. It never touches an approval."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report the outcomes without writing anything",
    )
    args = parser.parse_args(argv)

    result = run(dry_run=args.dry_run)
    for entry in result["plans"]:
        print(json.dumps(entry))
    print(json.dumps(result["summary"]))
    return 1 if any(result["summary"][outcome] for outcome in _SKIP_OUTCOMES) else 0


if __name__ == "__main__":
    sys.exit(main())
