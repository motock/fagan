"""Manifest-story construction and the descriptive (non-plan-defining) fields.

Extracted from :func:`pipeline.ingest._ingest_plan_impl` so the manifest can
stand alone as the plan record (ADR 0012 decision 5): an ingested manifest must
keep the *descriptive* content that the plan authored, so the plan document can
be rendered from the manifest alone.

The fields recorded here split into three groups:

``_INGEST_DESCRIPTIVE_STORY_FIELDS``
    The plan-authored story fields this module records verbatim: ``description``
    (``""`` when the plan omits it), ``epic`` (the summary of the epic the story
    was authored under) and ``plan_key`` (the plan's own story key, recorded only
    when the plan story had one -- it keeps the authored key visible even when a
    ticketing provider issues a different story id).

    They are deliberately *not* part of
    ``pipeline.server._INGEST_AUTHORED_STORY_FIELDS``: that tuple drives the
    authored-field schema/docs coverage and the ``risk``-pin merge rules, and
    this module keeps its own tuple so neither list silently grows the other.

Plan-level fields
    ``plan_epics`` (every epic summary, in authored order, including epics with
    no stories), plus ``preflight_override`` and ``repo_url`` recorded only when
    the plan names them -- an absent value is never written as ``None``.

Digest membership
    None of these fields is a ``canonical_digest`` member, so recording them
    cannot reset an approval (ADR 0007 / ADR 0012 Consequences). ``description``
    becomes a digest member later, in the description/team story that also bumps
    ``APPROVAL_DIGEST_VERSION`` to 2 -- do not add it to a digest tuple here.

The module must not import :mod:`pipeline.ingest` at load time (circular
import): it takes a plan-level ``plan``/``epic``/``story`` mapping and returns
plain data. The transaction-body phases at the end reach names the test suite
patches on :mod:`pipeline.ingest` through that module at call time (``_home``,
imported inside each function).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterable, Iterator
from typing import Any

from .ingest_advisories import story_advisories
from .sizing_by_class import class_aware_sizing_warning

#: Plan-authored, *descriptive* story fields the manifest now records. Kept
#: separate from ``pipeline.server._INGEST_AUTHORED_STORY_FIELDS`` on purpose.
_INGEST_DESCRIPTIVE_STORY_FIELDS = ("description", "epic", "plan_key")

#: Plan-level fields copied onto the manifest only when the plan names them.
_INGEST_OPTIONAL_PLAN_FIELDS = ("preflight_override", "repo_url")


def story_description(story: dict[str, Any]) -> str:
    """Return ``story``'s authored description, defaulting to ``""``.

    Validation lives in :func:`validate_story_descriptions` so this stays a
    plain accessor; a story that reached here has already been validated.
    """
    return story.get("description", "")


def validate_story_descriptions(plan: dict[str, Any], only_epics=None) -> dict | None:
    """Return an ``{"ok": False, ...}`` refusal for a non-string description.

    Called *before* any provider call or manifest write, so a refusal is
    non-destructive: the previously ingested manifest on disk is left exactly
    as it was. The message names the offending story by its summary (the plan's
    own identifier an author can act on), never by a provider-issued id.
    """
    for epic in plan.get("epics", []):
        if only_epics and epic.get("summary") not in only_epics:
            continue
        for story in epic.get("stories", []):
            description = story.get("description", "")
            if not isinstance(description, str):
                return {
                    "ok": False,
                    "error": (
                        f"Story {story.get('summary')!r} has a non-string "
                        f"description ({type(description).__name__}): "
                        "description must be a string"
                    ),
                }
    return None


def build_story_record(
    story: dict[str, Any], epic_summary: str, extra_fields: dict | None = None
) -> dict[str, Any]:
    """Build the manifest story dict for one plan story.

    Everything the old in-line block produced, plus the descriptive fields:
    ``description`` always (default ``""``), ``epic`` (the authored epic
    summary) and ``plan_key`` only when the plan story carried a ``key``.
    """
    record: dict[str, Any] = {
        "summary": story["summary"],
        "agent_instructions": story.get("agent_instructions", ""),
        "dependencies": story.get("dependencies", []),
        "persona": story.get("persona"),
        "model": story.get("model"),
        "acceptance": story.get("acceptance", []),
        "risk": story.get("risk", "low"),
        "backend": story.get("backend"),
        "authored_backend": story.get("backend"),
        "status": "todo",
        "description": story_description(story),
        "epic": epic_summary,
    }
    # Per-story defaults ingest.py owns (the TDD-split opt-in default, say) are
    # applied by the caller so that policy stays at the ingest call site.
    record.update(extra_fields or {})
    if "key" in story:
        record["plan_key"] = story["key"]
    if "files" in story and story["files"] is not None:
        record["files"] = list(story["files"])
    if story.get("blocked_by") is not None:
        record["blocked_by"] = list(story["blocked_by"])
    if story.get("test_scope") is not None:
        record["test_scope"] = story["test_scope"]
    return record


def build_manifest(
    plan: dict[str, Any], *, provider, only_epics=None, story_extra_fields=None
) -> dict:
    """Build the ``epics``/``stories`` half of the manifest for ``plan``.

    Moved out of ``_ingest_plan_impl`` unchanged, plus the descriptive story
    fields this module exists for. ``provider`` is the ticket provider (a
    ``NullTicketProvider`` returns ``None`` from ``create_epic``/
    ``create_story``, in which case the plan's local story keys - or a
    synthetic uuid4 - are the manifest keys).

    Dependencies expressed as local plan keys are translated to the issue ids
    minted here; anything that is not a known local key (an issue id already,
    or a typo) is left as-is.
    """
    epics: dict[str, Any] = {}
    stories: dict[str, Any] = {}
    key_to_issue_id: dict[str, str] = {}

    for epic in plan["epics"]:
        if only_epics and epic["summary"] not in only_epics:
            continue

        epic_id = provider.create_epic(epic["summary"])
        if epic_id is not None:
            epics[epic["summary"]] = epic_id

        for story in epic.get("stories", []):
            issue_id = provider.create_story(
                story["summary"],
                story.get("description", ""),
                epic_id,
                "agent-pipeline",
            )
            if issue_id is None:
                # No backend id to key on: prefer the plan's own story key
                # (keeps the manifest readable and lets key-based dependencies
                # resolve to themselves), else mint a unique synthetic key.
                issue_id = story.get("key") or str(uuid.uuid4())
            if "key" in story:
                key_to_issue_id[story["key"]] = issue_id
            extra = story_extra_fields(story) if story_extra_fields else None
            stories[issue_id] = build_story_record(story, epic["summary"], extra)

    for story in stories.values():
        story["dependencies"] = [
            key_to_issue_id.get(dep, dep) for dep in story["dependencies"]
        ]

    return {"epics": epics, "stories": stories}


def merge_descriptive_story_fields(combined: dict, new_story: dict) -> None:
    """Refresh the descriptive fields on a re-ingested story in place.

    Same merge rule as the other authored fields: a value the plan supplies is
    copied over the manifest's, and a field the plan no longer supplies is
    removed (``plan_key`` disappears when the plan story lost its ``key``).
    Every other key in ``combined`` - ``status``, ``pr_url``, attempts, ... -
    is untouched, so a re-ingest never resets runtime state.
    """
    for field in _INGEST_DESCRIPTIVE_STORY_FIELDS:
        if field in new_story:
            combined[field] = new_story[field]
        else:
            combined.pop(field, None)


def plan_level_fields(plan: dict[str, Any]) -> dict[str, Any]:
    """Return the plan-level fields the manifest records for ``plan``.

    ``plan_epics`` is always present (``[]`` for a plan with no epics) and is
    built from the plan's epic list itself, so it keeps the authored order and
    the epics that have no stories. ``preflight_override`` and ``repo_url`` are
    copied only when the plan names them.
    """
    fields: dict[str, Any] = {
        "plan_epics": [epic["summary"] for epic in plan.get("epics", [])],
    }
    for field in _INGEST_OPTIONAL_PLAN_FIELDS:
        if field in plan:
            fields[field] = plan[field]
    return fields


def merge_stories(
    prior_stories: dict[str, Any],
    new_stories: dict[str, Any],
    authored_fields: Iterable[str],
) -> dict[str, Any]:
    """Carry runtime state over from the prior manifest's stories.

    Stories only in ``prior_stories`` survive verbatim; a re-ingested story
    keeps its runtime state and takes the authored fields from the new one.
    """
    merged_stories = dict(prior_stories)
    for key, new_story in new_stories.items():
        old_story = merged_stories.get(key)
        if old_story is None:
            merged_stories[key] = new_story
            continue
        combined = dict(old_story)
        for field in authored_fields:
            if field == "risk" and old_story.get("status") != "todo":
                continue
            if field in ("files", "blocked_by", "test_scope"):
                # `files` and `blocked_by` are the authored fields
                # allowed to be absent from a re-ingested story:
                # present -> copied; absent -> the key is removed.
                if field in new_story:
                    combined[field] = new_story[field]
                else:
                    combined.pop(field, None)
                continue
            combined[field] = new_story[field]
        # The descriptive fields (description/epic/plan_key) follow the
        # same rule for merging only; they stay out of
        # _INGEST_AUTHORED_STORY_FIELDS.
        merge_descriptive_story_fields(combined, new_story)
        # Hashed by canonical_digest; the stamped `backend` is not.
        combined["authored_backend"] = new_story["authored_backend"]
        merged_stories[key] = combined
    return merged_stories


def route_oversized_stories(
    stories: dict[str, Any], repo_root: str, role_config: dict | None
) -> list[tuple[str, str, str]]:
    """Auto-route oversized on-device stories to the host's :cloud default.

    Mutates the story dicts in ``stories`` so the route is persisted and
    returns ``(key, tag, warning)`` for each routed story. The routed-key set
    is per-ingest only (a re-ingested story already on a :cloud model fails
    ``_on_device_route_target``).
    """
    from . import ingest as _home

    routed: list[tuple[str, str, str]] = []
    for key, story in stories.items():
        warning = class_aware_sizing_warning(
            story, repo_root, role_config, _home._story_sizing_warning
        )
        if warning is None:
            continue
        tag = _home._on_device_route_target(story)
        if tag is None:
            continue
        from_backend = story.get("backend")
        from_model = story.get("model")
        story["backend"] = "ollama"
        story["model"] = tag
        story["sizing_auto_routed"] = {
            "from_backend": from_backend,
            "from_model": from_model,
            "to_model": tag,
            "reason": warning,
        }
        routed.append((key, tag, warning))
    return routed


# (story key, notification body, notify kwargs, also log as a warning)
StoryNotice = tuple[str, str, dict[str, Any], bool]


def iter_story_notices(
    final_manifest: dict[str, Any], routed_keys: set[str], repo_root: str
) -> Iterator[StoryNotice]:
    """Yield each story's non-blocking advisories, in the order they fire.

    A generator, so each advisory is computed only after the previous one has
    been delivered - the same interleaving as the inline loop it replaces.
    """
    from . import ingest as _home

    for key, story in final_manifest["stories"].items():
        if key in routed_keys:
            # Already notified with the sizing_auto_routed event;
            # a second plain warning would double-notify the same story.
            warning = None
        else:
            warning = _home._story_sizing_warning(
                _home._sizing_view(story, final_manifest.get("role_config")), repo_root
            )
        if warning:
            if not os.environ.get("PIPELINE_LOCAL_MODEL_DEFAULT", "").endswith(":cloud"):
                warning += _home._AUTO_ROUTE_SKIPPED_SUFFIX
            yield key, warning, {"event": "sizing_risk", "story_key": key}, True
        for event, text in story_advisories(
            story, repo_root, final_manifest.get("repo_url"), _home._store
        ):
            yield key, text, {"event": event, "story_key": key}, True
        # Non-blocking authoring nudge: flag acceptance fixtures that
        # depend on macOS-only tooling. Dispatch, the done-bar and the
        # merge-gate reverify all run on macOS, but CI runs ubuntu-latest
        # only, so such a fixture passes every local gate and fails only
        # after the PR is open (observed live 2026-07-30). Advisory only.
        msg = _home._security_review_unenforced_warning(story)
        if msg is not None:
            yield key, msg, {"event": "security_review_unenforced", "story_key": key}, False
        platform_msg = _home._platform_locked_fixture_warning(story)
        if platform_msg is not None:
            yield key, platform_msg, {}, True
        # Non-blocking authoring nudge: flag a local-dispatch story whose
        # test_author/planner scaffolding roles resolve to a DIFFERENT
        # provider - exactly the configuration that silently dropped the
        # TDD-split crutch from two stories on 2026-07-30 (both parked).
        # Advisory only.
        story_backend = story.get("backend") or os.environ.get(
            "PIPELINE_BACKEND_DISPATCH", "claude"
        ).strip().lower()
        role_msg = _home._scaffolding_provider_mismatch_warning(
            dispatch_backend=story_backend,
            role_config=final_manifest.get("role_config"),
            registry_roles=_home.role_registry.load_registry().get("roles", {}),
        )
        if role_msg is not None:
            yield key, role_msg, {}, True
