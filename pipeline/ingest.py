"""Ingest-plan implementation, extracted from pipeline/server.py.

``_ingest_plan_impl`` was moved here verbatim from ``pipeline/server.py``
(behavior-preserving refactor). It reads server-sourced module globals
(``PLAN_DIR``, ``_store``, ``_validate_key``, ``get_ticket_provider``,
``_atomic_write_json``, ``_notify_user``, ``_INGEST_AUTHORED_STORY_FIELDS``,
``_VALID_STORY_BACKENDS``, the acceptance/planner warning helpers,
``role_registry``) as free variables. The test suite monkeypatches those names
on ``pipeline.server`` (e.g. the ``plan_dir`` fixture patches
``pipeline.server.PLAN_DIR``), so each server-sourced name is bound to a
``_ServerRef`` that resolves the *live* ``pipeline.server`` binding at call
time rather than holding a copy imported at module load. This mirrors the
``_ServerRef`` pattern already used by ``pipeline/service.py`` and
``pipeline/store.py``.
"""

import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any

from .build_detect import _lint_acceptance_fixtures
from .service import _ServerRef

# Server-sourced names the function body references as free variables. Each
# resolves to the live ``pipeline.server`` binding at call time so
# ``monkeypatch.setattr(pipeline.server, "NAME", ...)`` still lands.
PLAN_DIR = _ServerRef("PLAN_DIR")
_store = _ServerRef("_store")
_validate_key = _ServerRef("_validate_key")
get_ticket_provider = _ServerRef("get_ticket_provider")
_atomic_write_json = _ServerRef("_atomic_write_json")
_notify_user = _ServerRef("_notify_user")
_INGEST_AUTHORED_STORY_FIELDS = _ServerRef("_INGEST_AUTHORED_STORY_FIELDS")
_VALID_STORY_BACKENDS = _ServerRef("_VALID_STORY_BACKENDS")
_isolation_only_acceptance_warning = _ServerRef("_isolation_only_acceptance_warning")
_platform_locked_fixture_warning = _ServerRef("_platform_locked_fixture_warning")
_scaffolding_provider_mismatch_warning = _ServerRef(
    "_scaffolding_provider_mismatch_warning"
)
role_registry = _ServerRef("role_registry")

# .claude/rules/agent-dispatch-story-sizing.md's mechanically-checkable caps
# for a story dispatched to a non-Claude executor: at most 2 production files
# in scope, and no production file over ~1000 lines.
_SIZING_MAX_PRODUCTION_FILES = 2
_SIZING_MAX_FILE_LINES = 1000


def _story_sizing_warning(story: dict, repo_root: str) -> str | None:
    """Return a non-blocking warning string when a story's own
    agent_instructions names more production files than
    .claude/rules/agent-dispatch-story-sizing.md's caps for a non-Claude
    dispatch tier, or names a file that already exceeds the file-size
    cap on disk. Returns None when the story looks fine or the backend
    isn't a non-Claude tier (only local/cloud-open-source stories are
    checked -- Claude-class dispatch doesn't need this crutch, mirroring
    the same backend gate the test-author/planner phases already use).
    Never raises; a filesystem read failure degrades to None (this must
    stay an observability hook, never a gate -- see
    pipeline/dispatch.py's 'multi-model concurrent dispatch' and 'ollama
    serving parallelism' warnings for the established pattern this
    mirrors)."""
    backend = (story.get("backend") or "").strip().lower()
    if backend not in ("local", "ollama", "lmstudio", "mlx", "litellm"):
        return None
    instructions = story.get("agent_instructions") or ""
    # Reuse the same backtick-quoted-repo-path convention every brief in
    # this codebase already follows (see any story's "Files:" line).
    paths = sorted(
        set(
            re.findall(
                r"`((?:app|pipeline|static|scripts|tests|docs|src|systemd)/[\w/.\-]+)`",
                instructions,
            )
        )
    )
    production_paths = [
        p
        for p in paths
        if not p.split("/")[-1].startswith("test_")
        and "/test" not in p
        and not p.endswith(".md")
    ]
    reasons = []
    if len(production_paths) > _SIZING_MAX_PRODUCTION_FILES:
        reasons.append(
            f"names {len(production_paths)} production files "
            f"({', '.join(production_paths)}), over the "
            f"{_SIZING_MAX_PRODUCTION_FILES}-file cap for a non-Claude "
            f"dispatch tier"
        )
    for p in production_paths:
        try:
            full = Path(repo_root) / p
            if full.is_file():
                line_count = sum(1 for _ in full.open("r", errors="replace"))
                if line_count > _SIZING_MAX_FILE_LINES:
                    reasons.append(
                        f"edits {p} ({line_count} lines), over the "
                        f"{_SIZING_MAX_FILE_LINES}-line file-size cap for "
                        f"the weakest dispatch tier"
                    )
        except OSError:
            continue
    if not reasons:
        return None
    return (
        "story sizing risk (see .claude/rules/agent-dispatch-story-sizing.md): "
        + "; ".join(reasons)
    )


def _ingest_plan_impl(
    plan_name: str,
    only_epics: list[str] | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    _validate_key(plan_name)
    path = PLAN_DIR / f"{plan_name}.json"
    if not path.exists():
        return {"ok": False, "error": f"No plan named {plan_name}"}

    plan = json.loads(path.read_text())

    # advance_all_plans() iterates every plan in shared PLAN_DIR, each
    # potentially belonging to a different repo, so a manifest without its
    # own repo_root falls back to the global REPO_ROOT - the wrong repo for
    # any plan other than the one that env var happens to be set for (or a
    # deliberately-broken sentinel, if one's configured to fail loudly
    # instead). Catching it here means a typo'd or missing path surfaces
    # immediately, not as a cryptic ENOENT after three silent merge-attempt
    # failures.
    repo_root = plan.get("repo_root")
    if not repo_root or not Path(repo_root).is_dir():
        return {
            "ok": False,
            "error": f"Plan repo_root is missing or not a directory: {repo_root!r}",
        }

    # Validate story["backend"] upfront, before any Plane side effects, so a
    # typo'd provider name fails closed here rather than surfacing as a
    # NotImplementedError deep inside get_backend at dispatch time.
    for epic in plan["epics"]:
        if only_epics and epic["summary"] not in only_epics:
            continue
        for story in epic.get("stories", []):
            story_backend = story.get("backend")
            if story_backend is not None and story_backend not in _VALID_STORY_BACKENDS:
                return {
                    "ok": False,
                    "error": (
                        f"Story {story.get('summary', '?')!r} has unknown "
                        f"backend {story_backend!r}. Valid values: "
                        f"{sorted(_VALID_STORY_BACKENDS)}"
                    ),
                }
            # OPSA-8: lint the .py acceptance fixture sources upfront, before
            # any Plane side effect. A lint-violating fixture is a read-only
            # oracle the dispatched agent can never fix (PR #235), so it must
            # not reach dispatch - and it must not leave orphaned epics or
            # stories behind when the ingest is rejected. Gating on "finding"
            # only, never on "skipped": a broken or absent lint tool can never
            # block a known-good plan.
            kind, lint_msg = _lint_acceptance_fixtures(story, repo_root)
            if kind == "finding":
                return {"ok": False, "error": lint_msg}

    manifest_path = _store.manifest_path(plan_name)

    with _store.transaction(plan_name) as acquired:
        if not acquired:
            return {
                "ok": True,
                "skipped": "locked",
                "reason": "another ingest/dispatch/interrupt is in progress for this plan",
            }

        manifest = {"epics": {}, "stories": {}, "repo_root": repo_root}

        # When no ticketing backend is configured (NullTicketProvider) the
        # manifest is the sole source of truth: create_epic/create_story are
        # no-ops returning None, and we synthesize story keys locally instead
        # of taking them from a backend-issued id.
        provider = get_ticket_provider()

        # Maps the plan's local story keys (e.g. "S1") to the manifest story keys
        # generated below (backend ids, or local keys when no backend is
        # configured), so dependencies can be translated to manifest keys.
        key_to_issue_id: dict[str, str] = {}

        for epic in plan["epics"]:
            if only_epics and epic["summary"] not in only_epics:
                continue

            epic_id = provider.create_epic(epic["summary"])
            if epic_id is not None:
                manifest["epics"][epic["summary"]] = epic_id

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
                manifest["stories"][issue_id] = {
                    "summary": story["summary"],
                    "agent_instructions": story.get("agent_instructions", ""),
                    "dependencies": story.get("dependencies", []),
                    "persona": story.get("persona"),
                    "model": story.get("model"),
                    "acceptance": story.get("acceptance", []),
                    "risk": story.get("risk", "low"),
                    "backend": story.get("backend"),
                    # TDD_SPLIT_PRODUCTION_PLAN.md §2.4: explicit per-story
                    # opt-in for the test-author pre-executor phase. Defaults
                    # False - inferring eligibility from agent_instructions
                    # prose is a worse failure mode than an operator
                    # forgetting to opt in.
                    "tdd_split": bool(story.get("tdd_split", False)),
                    "status": "todo",
                }

        # Translate dependencies expressed as local plan keys into the issue IDs
        # just created. Dependencies that don't match a known local key (e.g.
        # already an issue ID, or a typo) are left as-is.
        for story in manifest["stories"].values():
            story["dependencies"] = [
                key_to_issue_id.get(dep, dep) for dep in story["dependencies"]
            ]

        # Merge into the existing manifest rather than replacing it (T1):
        # anything only_epics excluded this round - and, with overwrite=False,
        # the manifest's runtime state for stories re-ingested this round -
        # must survive. overwrite=True restores the old wholesale-replace
        # behavior for callers that genuinely want a clean slate.
        prior: dict[str, Any] = {}
        if not overwrite and manifest_path.exists():
            prior = json.loads(manifest_path.read_text())

        merged_epics = dict(prior.get("epics", {}))
        merged_epics.update(manifest["epics"])

        merged_stories = dict(prior.get("stories", {}))
        for key, new_story in manifest["stories"].items():
            old_story = merged_stories.get(key)
            if old_story is not None:
                combined = dict(old_story)
                for field in _INGEST_AUTHORED_STORY_FIELDS:
                    if field == "risk" and old_story.get("status") != "todo":
                        continue
                    combined[field] = new_story[field]
                merged_stories[key] = combined
            else:
                merged_stories[key] = new_story

        final_manifest = dict(prior)
        final_manifest["epics"] = merged_epics
        final_manifest["stories"] = merged_stories
        final_manifest["repo_root"] = repo_root
        final_manifest["role_config"] = plan.get(
            "role_config", prior.get("role_config", {})
        )

        _atomic_write_json(manifest_path, final_manifest)

        # Non-blocking authoring nudge: flag acceptance fixtures that grade
        # only the unit in isolation while the brief requires integration
        # wiring (a call-site/registration change). A weak executor passes
        # such a fixture while skipping the ungraded wiring and ships dead
        # code (observed live 2026-07-28). Advisory only — never blocks.
        for key, story in final_manifest["stories"].items():
            msg = _isolation_only_acceptance_warning(story)
            if msg is not None:
                _notify_user(plan_name, f"{key}: {msg}")
                logging.getLogger("pipeline").warning(f"{plan_name}/{key}: {msg}")

        # Non-blocking sizing nudge: flag a story whose own brief names more
        # production files than .claude/rules/agent-dispatch-story-sizing.md
        # allows a non-Claude dispatch tier, or names a file already over the
        # file-size cap. Advisory only -- a human plan author may have
        # deliberately authorized a larger story, so this never blocks.
        for key, story in final_manifest["stories"].items():
            warning = _story_sizing_warning(story, repo_root)
            if warning:
                _notify_user(plan_name, f"{key}: {warning}")
                logging.getLogger("pipeline").warning(f"{plan_name}/{key}: {warning}")
            # Non-blocking authoring nudge: flag acceptance fixtures that
            # depend on macOS-only tooling. Dispatch, the done-bar and the
            # merge-gate reverify all run on macOS, but CI runs ubuntu-latest
            # only, so such a fixture passes every local gate and fails only
            # after the PR is open (observed live 2026-07-30). Advisory only.
            platform_msg = _platform_locked_fixture_warning(story)
            if platform_msg is not None:
                _notify_user(plan_name, f"{key}: {platform_msg}")
                logging.getLogger("pipeline").warning(f"{plan_name}/{key}: {platform_msg}")
            # Non-blocking authoring nudge: flag a local-dispatch story whose
            # test_author/planner scaffolding roles resolve to a DIFFERENT
            # provider - exactly the configuration that silently dropped the
            # TDD-split crutch from two stories on 2026-07-30 (both parked).
            # Advisory only.
            story_backend = story.get("backend") or os.environ.get(
                "PIPELINE_BACKEND_DISPATCH", "claude"
            ).strip().lower()
            role_msg = _scaffolding_provider_mismatch_warning(
                dispatch_backend=story_backend,
                role_config=final_manifest.get("role_config"),
                registry_roles=role_registry.load_registry().get("roles", {}),
            )
            if role_msg is not None:
                _notify_user(plan_name, f"{key}: {role_msg}")
                logging.getLogger("pipeline").warning(f"{plan_name}/{key}: {role_msg}")

    return {"ok": True, "manifest_path": str(manifest_path), **final_manifest}
