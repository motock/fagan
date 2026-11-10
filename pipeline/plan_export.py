"""TEMPORARY reference implementation used only to validate the new tests.

Deleted before the test suite is committed.
"""

from __future__ import annotations

from typing import Any

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

_OPTIONAL = ("files", "blocked_by")


def render_plan_document(manifest: dict) -> dict:
    if not isinstance(manifest, dict):
        raise ValueError("manifest has no stories")
    stories = manifest.get("stories")
    if not isinstance(stories, dict):
        raise ValueError("manifest has no stories")

    plan_key_by_key: dict[str, str] = {}
    for key, story in stories.items():
        if not isinstance(story, dict):
            continue
        plan_key = story.get("plan_key")
        if isinstance(plan_key, str) and plan_key:
            plan_key_by_key[key] = plan_key

    def _key_of(key: str) -> str:
        return plan_key_by_key.get(key, key)

    def _translate(deps: Any) -> Any:
        if not isinstance(deps, list):
            return deps
        return [_key_of(dep) if dep in plan_key_by_key else dep for dep in deps]

    document: dict[str, Any] = {
        "repo_root": manifest.get("repo_root", ""),
        "role_config": manifest.get("role_config") or {},
        "team": manifest.get("team") or "local",
    }
    for field in ("repo_url", "preflight_override"):
        if field in manifest:
            document[field] = manifest[field]

    context = manifest.get("context")
    if isinstance(context, dict):
        sources = context.get("sources")
        if isinstance(sources, list):
            plan_sources = [
                {k: v for k, v in source.items() if k != "origin"}
                for source in sources
                if isinstance(source, dict) and source.get("origin") == "plan"
            ]
            if plan_sources:
                document["context"] = {"sources": plan_sources}

    by_epic: dict[str, list[dict]] = {}
    order: list[str] = []
    for key, story in stories.items():
        if not isinstance(story, dict):
            continue
        epic = story.get("epic")
        epic_name = epic if isinstance(epic, str) and epic else "Ingested stories"
        if epic_name not in by_epic:
            by_epic[epic_name] = []
            order.append(epic_name)
        exported: dict[str, Any] = {"key": _key_of(key)}
        for field in _STORY_FIELDS:
            if field in _OPTIONAL:
                if field in story:
                    exported[field] = story[field]
                continue
            if field == "dependencies":
                exported[field] = _translate(story.get("dependencies", []))
                continue
            exported[field] = story.get(field)
        by_epic[epic_name].append(exported)

    plan_epics = manifest.get("plan_epics")
    if isinstance(plan_epics, list):
        known = [e for e in plan_epics if isinstance(e, str) and e in by_epic]
        extra = [e for e in order if e not in known]
        order = known + extra

    document["epics"] = [
        {"summary": name, "stories": by_epic[name]} for name in order
    ]
    return document
