"""Render a plan document from an ingested manifest (ADR 0012 decision 5).

The manifest is the plan record: ingest copies the authored, descriptive
content out of the plan file and keeps runtime state (status, attempts,
pr_url, ...) alongside it. The *document* — the ``save_plan`` schema,
``{"epics": [{"summary", "stories": [...]}]}`` — is not stored anywhere: it is
rendered from the manifest on demand, when an export is wanted.

``render_plan_document`` is that render, and it is pure: it reads the manifest
and returns a fresh document, never touching the store and never mutating its
argument. What it keeps and what it drops is the whole point:

* descriptive fields are copied verbatim (summary, description,
  agent_instructions, persona, model, acceptance, risk, backend, tdd_split, and
  ``files``/``blocked_by`` when the manifest carries them);
* dependencies are translated back from manifest keys (a ticketing provider's
  issue ids) to the plan's own keys, because the plan file names the latter;
* the story key is the plan key when the manifest recorded one, else the
  manifest key, so a legacy (un-keyed) manifest still renders;
* ``context`` keeps only the sources whose ``origin`` is ``plan`` — ``repo``
  and ``org`` sources are re-merged by ingest (``_merged_context`` in
  pipeline/ingest.py), so exporting them would duplicate them on the next
  ingest and make a re-export drift;
* runtime state (status, attempts, pr_url, worktree, review_*, log, approval,
  owner, delegates, ingested_by, sizing_auto_routed, pid) is never exported: it
  is not part of the plan, so it must not reach the approval digest.

A legacy manifest — written before manifests recorded epics — has no ``epic``
on its stories and no ``plan_epics``; its stories render under one epic named
``Ingested stories``.

The round trip is what approval (ADR 0007) relies on: rendering an ingested
plan and ingesting the result again yields the same ``canonical_digest``, so an
approval still binds to the content it approved.
"""

from __future__ import annotations

from typing import Any

#: The story fields the document carries, in document order. ``key`` is
#: computed (plan key, else manifest key) and handled separately.
_STORY_FIELDS = (
    "summary",
    "description",
    "agent_instructions",
    "dependencies",
    "persona",
    "model",
    "acceptance",
    "risk",
    "backend",
    "tdd_split",
    "files",
    "blocked_by",
)

#: Fields omitted entirely when the manifest does not carry them, rather than
#: rendered as ``None``: a plan is free to leave them out.
_OPTIONAL_STORY_FIELDS = frozenset({"files", "blocked_by"})

#: The epic a legacy (pre-epic) manifest's stories render under.
LEGACY_EPIC = "Ingested stories"


def _plan_key_by_manifest_key(stories: dict[str, Any]) -> dict[str, str]:
    """Map manifest keys to the plan's own keys, for the stories that have one.

    A ticketing provider issues its own story ids, so a manifest key may not be
    the key the plan authored; ``plan_key`` records the authored one.
    """
    mapping: dict[str, str] = {}
    for manifest_key, story in stories.items():
        if not isinstance(story, dict):
            continue
        plan_key = story.get("plan_key")
        if isinstance(plan_key, str) and plan_key:
            mapping[manifest_key] = plan_key
    return mapping


def _render_story(
    manifest_key: str,
    story: dict[str, Any],
    plan_keys: dict[str, str],
) -> dict[str, Any]:
    """Render one manifest story as one plan-document story.

    Runtime fields are dropped by construction: only ``_STORY_FIELDS`` are
    copied, so a new runtime field added to the manifest can never leak here by
    default.
    """
    exported: dict[str, Any] = {"key": plan_keys.get(manifest_key, manifest_key)}
    for field in _STORY_FIELDS:
        if field in _OPTIONAL_STORY_FIELDS:
            if field in story:
                exported[field] = story[field]
            continue
        if field == "dependencies":
            dependencies = story.get("dependencies", [])
            if isinstance(dependencies, list):
                # Translate back to plan keys; an unmapped dependency is kept
                # verbatim (it may name a key the plan never recorded).
                dependencies = [plan_keys.get(dep, dep) for dep in dependencies]
            exported[field] = dependencies
            continue
        if field == "backend":
            # The author's value, not the provider the dispatcher stamped on
            # ``backend`` at runtime, so export -> ingest keeps the digest.
            exported[field] = story.get("authored_backend", story.get(field))
            continue
        exported[field] = story.get(field)
    return exported


def _render_context(manifest: dict[str, Any]) -> dict[str, Any] | None:
    """Return the plan-origin context sources, or ``None`` when there are none.

    ``repo`` and ``org`` sources are re-merged by ingest (``_merged_context``),
    so exporting them would duplicate them on the next ingest and would make a
    re-export drift. The ``origin`` marker is dropped too: the plan file does
    not carry it, ingest stamps it back on.
    """
    context = manifest.get("context")
    if not isinstance(context, dict):
        return None
    sources = context.get("sources")
    if not isinstance(sources, list):
        return None
    plan_sources = [
        {key: value for key, value in source.items() if key != "origin"}
        for source in sources
        if isinstance(source, dict) and source.get("origin") == "plan"
    ]
    if not plan_sources:
        return None
    return {"sources": plan_sources}


def _epic_order(manifest: dict[str, Any], seen: list[str]) -> list[str]:
    """Order the rendered epics: ``plan_epics`` order first, then any other.

    ``plan_epics`` records every epic the plan authored, in authored order
    *including epics with no stories*, so those are rendered (empty) rather
    than dropped: dropping one would make the re-ingested plan differ from the
    manifest it came from, and the canonical digest with it. An epic found only
    on a story keeps its first-seen position after them, so a manifest written
    before ``plan_epics`` existed still renders deterministically.
    """
    plan_epics = manifest.get("plan_epics")
    if not isinstance(plan_epics, list):
        return seen
    ordered: list[str] = []
    for name in plan_epics:
        if isinstance(name, str) and name and name not in ordered:
            ordered.append(name)
    ordered.extend(name for name in seen if name not in ordered)
    return ordered


def render_plan_document(manifest: dict) -> dict:
    """Render the ``save_plan`` document a manifest represents.

    Pure: the manifest is read, never mutated, and the store is not touched.

    Raising ``ValueError`` (rather than exporting an empty plan) when there is
    nothing to render keeps a corrupt manifest from looking like an empty one.
    """
    # ValueError, not TypeError, for the non-dict case too: the caller's reply
    # must not tell "corrupt manifest" apart from "manifest has no stories",
    # and the tool turns both into one refusal.
    if not isinstance(manifest, dict):
        raise ValueError("manifest has no stories")  # noqa: TRY004
    stories = manifest.get("stories")
    if not isinstance(stories, dict):
        raise ValueError("manifest has no stories")  # noqa: TRY004

    plan_keys = _plan_key_by_manifest_key(stories)

    document: dict[str, Any] = {
        "repo_root": manifest.get("repo_root", ""),
        "role_config": manifest.get("role_config") or {},
        "team": manifest.get("team") or "local",
    }
    for field in ("repo_url", "preflight_override"):
        if field in manifest:
            document[field] = manifest[field]

    context = _render_context(manifest)
    if context is not None:
        document["context"] = context

    stories_by_epic: dict[str, list[dict[str, Any]]] = {}
    epic_order: list[str] = []
    for manifest_key, story in stories.items():
        if not isinstance(story, dict):
            continue
        epic = story.get("epic")
        epic_name = epic if isinstance(epic, str) and epic else LEGACY_EPIC
        if epic_name not in stories_by_epic:
            stories_by_epic[epic_name] = []
            epic_order.append(epic_name)
        stories_by_epic[epic_name].append(
            _render_story(manifest_key, story, plan_keys)
        )

    document["epics"] = [
        {"summary": epic_name, "stories": stories_by_epic.get(epic_name, [])}
        for epic_name in _epic_order(manifest, epic_order)
    ]
    return document


class PlanExportMixin:
    """Mixed into ``PipelineService``: read a plan back as a plan document."""

    def export_plan(self, plan_name: str) -> dict[str, Any]:
        """Render *plan_name*'s plan document from its ingested manifest.

        The document is rendered on demand (ADR 0012 decision 5): the manifest
        is the plan record, so the ``save_plan``-shaped document is derived
        from it rather than stored a second time and left to drift. Runtime
        state never reaches the export; see this module's docstring for what is
        kept and dropped, and why the round trip matters for plan approval
        (ADR 0007).

        The plan name is validated before the manifest is read, so an invalid
        name is a validation refusal and never a store lookup.

        Args:
            plan_name: the plan's name, as passed to
                ``save_plan``/``ingest_plan`` and as returned by ``list_plans``.

        Returns:
            ``{'ok': True, 'plan': <document>}``, or ``{'ok': False, 'error':
            'Plan not found'}`` for a plan that was never ingested.
        """
        # Resolved here, not at module import: pipeline.server owns both
        # bindings, and importing module_ref at import time would import
        # pipeline.service, which imports this module (a cycle). Resolving at
        # call time also keeps tests that patch pipeline.server._store landing.
        from . import server as server_module

        server_module._validate_key(plan_name)
        manifest = server_module._store.records().get_manifest(
            "default", plan_name
        )
        if manifest is None:
            return {"ok": False, "error": "Plan not found"}
        return {"ok": True, "plan": render_plan_document(manifest)}
