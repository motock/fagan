"""Ingest-plan implementation, extracted from pipeline/server.py.

``_ingest_plan_impl`` was moved here verbatim from ``pipeline/server.py``
(behavior-preserving refactor). It reads server-sourced module globals
(``_store``, ``_validate_key``, ``get_ticket_provider``,
``_notify_user``, ``_INGEST_AUTHORED_STORY_FIELDS``,
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
from pathlib import Path
from typing import Any, cast

from . import (
    cross_plan,
    external_refs,  # noqa: F401
    ingest_manifest,
    ingest_preflight,
    ingest_validate,
    plan_draft,
    plan_names,
    testfiles,
)
from .actor import actor_id, current_namespace
from .authorization import DEFAULT_TEAM
from .branch_names import worktree_parent
from .build_detect import _lint_acceptance_fixtures as _lint_acceptance_fixtures  # noqa: PLC0414
from .context_sources import (
    DEFAULT_MAX_TOTAL_CHARS,
    normalize_context_sources,
    validate_context_config,
)
from .ecosystem_detect import (  # noqa: F401
    UNSUPPORTED_MARKERS,
    detect_test_command,
    fail_loud_exit_message,
    is_unsupported_ecosystem_command,
)
from .ingest_advisories import story_advisories  # noqa: F401
from .ingest_manifest import (
    build_manifest,
    merge_descriptive_story_fields,  # noqa: F401
    plan_level_fields,
    validate_story_descriptions,  # noqa: F401
)
from .ingest_plan_fields import _TEAM_RE, _team_error, _test_scope_error  # noqa: F401
from .org_context_hook import merge_org_sources, plugin_org_sources
from .ownership import resolve_ownership
from .plan_approval import approval_enabled, on_ingest
from .plan_approval_notice import notify_approval_event, pending_notice_due
from .repo_config import load_repo_config
from .service import _ServerRef
from .sizing_by_class import class_aware_sizing_warning  # noqa: F401
from .sizing_class import needs_small_story_caps, sizing_applies
from .store_records import UnreadableRecordError

# Server-sourced names the function body references as free variables. Each
# resolves to the live ``pipeline.server`` binding at call time so
# ``monkeypatch.setattr(pipeline.server, "NAME", ...)`` still lands.
WORKTREE_ROOT = _ServerRef("WORKTREE_ROOT")
_store = _ServerRef("_store")
_validate_key = _ServerRef("_validate_key")
get_ticket_provider = _ServerRef("get_ticket_provider")
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
# for a story dispatched to a non-capable (on-device) executor: at most 2
# production files in scope, and no production file over ~1000 lines.
_SIZING_MAX_PRODUCTION_FILES = 2
_SIZING_MAX_FILE_LINES = 1000

# Appended to a sizing warning when the story is oversized but cannot be
# auto-routed because the host default is not a :cloud tag.
_AUTO_ROUTE_SKIPPED_SUFFIX = (
    "; auto-route skipped: PIPELINE_LOCAL_MODEL_DEFAULT is not a :cloud tag"
)

# Matches a backticked RELATIVE repo path with at least one ``/`` and a final
# component carrying a file extension: segments of ``[\w.\-]+`` separated by
# ``/``, the first character not ``/`` or ``.``, and the last segment shaped
# ``name.ext``. Deliberately language-agnostic (LAG-8): any
# ``dir/.../name.ext`` counts, not just the old hard-coded
# ``app|pipeline|static|scripts|tests|docs|src|systemd/`` prefixes. Because
# this regex now also matches test files in any language (``internal/foo_test.go``,
# ``src/main/java/com/x/FooTest.java``, ...), the regex-derived branch of
# ``_story_sizing_warning`` filters them with ``_is_test_path`` (LAG-2), the
# same exclusion the declared-``files`` branch already applies.
_BACKTICK_REPO_PATH_RE = re.compile(
    r"`((?!\.|/)[\w.\-]+(?:/[\w.\-]+)*/[\w.\-]+\.[\w.\-]+)`"
)


def _is_test_path(path) -> bool:
    """Return True if *path* looks like a test file in any supported language.

    Delegates to :func:`pipeline.testfiles.is_test_path`.  Test paths are
    excluded from story sizing entirely: the production-file caps grade
    the code a story touches, not its tests.
    """
    return testfiles.is_test_path(str(path))


def _story_sizing_warning(story: dict, repo_root: str) -> str | None:
    """Return a non-blocking warning string when a story's own
    agent_instructions names more production files than
    .claude/rules/agent-dispatch-story-sizing.md's caps for its dispatch
    tier, or names a file that already exceeds the file-size cap on disk.
    Returns None when the story is not sized at all (only the local/
    cloud-open backends are sized, so nothing that used to be silent
    starts warning; Claude-class dispatch does not need this crutch). The
    2-file cap is lifted for an entry declaring "class": "capable"; the
    file-size cap applies to every class. Never raises; a filesystem read
    failure degrades to None (this must stay an observability hook, never
    a gate -- see pipeline/dispatch.py's 'multi-model concurrent dispatch'
    warnings for the established pattern this mirrors)."""
    if not sizing_applies(story, role_registry):
        return None
    small_caps = needs_small_story_caps(story, role_registry)
    instructions = story.get("agent_instructions") or ""
    # Reuse the same backtick-quoted-repo-path convention every brief in
    # this codebase already follows (see any story's "Files:" line).
    regex_paths = sorted(set(_BACKTICK_REPO_PATH_RE.findall(instructions)))
    # A declared `files` list is authoritative when present (even empty);
    # otherwise fall back to the backtick-regex derivation above.
    files = story.get("files")
    if isinstance(files, list):
        paths = sorted({str(p) for p in files if isinstance(p, str)})
        # Test paths are ignored entirely; docs travel with code, so `.md`
        # paths never count toward the production-file COUNT cap -- but the
        # file-SIZE cap checks every non-test path, `.md` files and
        # repo-root files (README.md, pyproject.toml) included.
        production_paths = [
            p for p in paths if not _is_test_path(p) and not p.endswith(".md")
        ]
        size_paths = [p for p in paths if not _is_test_path(p)]
    else:
        production_paths = [
            p for p in regex_paths if not _is_test_path(p) and not p.endswith(".md")
        ]
        size_paths = production_paths
    reasons = []
    if small_caps and len(production_paths) > _SIZING_MAX_PRODUCTION_FILES:
        reasons.append(
            f"names {len(production_paths)} production files "
            f"({', '.join(production_paths)}), over the "
            f"{_SIZING_MAX_PRODUCTION_FILES}-file cap for a non-Claude "
            f"dispatch tier"
        )
    for p in size_paths:
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


def _on_device_route_target(story: dict) -> str | None:
    """Return the :cloud model tag an oversized on-device story should be
    routed to, or None when the story must not be re-routed.

    All four conditions must hold: the story is still ``todo`` (a running
    or finished story is never re-routed mid-flight); its backend is a
    local/on-device tier; its effective model (the story's own ``model``,
    else the host default) is not already a ``:cloud`` tag; and the host
    default ``PIPELINE_LOCAL_MODEL_DEFAULT`` IS a ``:cloud`` tag -- the
    route target is that default. Reading the env var once keeps the two
    checks from disagreeing if it changes mid-call."""
    if story.get("status") != "todo":
        return None
    backend = (story.get("backend") or "").strip().lower()
    if backend not in ("local", "ollama", "lmstudio", "mlx", "litellm"):
        return None
    default_model = os.environ.get("PIPELINE_LOCAL_MODEL_DEFAULT", "")
    effective_model = story.get("model") or default_model
    if effective_model.endswith(":cloud"):
        return None
    if not default_model.endswith(":cloud"):
        return None
    return default_model


def _preflight_status(agent_instructions: str | None) -> str:
    """Return "ok", "missing" or "not_run" for a story's Preflight: line."""
    if not agent_instructions:
        return "missing"
    for line in agent_instructions.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("Preflight:"):
            remainder = stripped[len("Preflight:"):].strip()
            if not remainder:
                return "missing"
            if remainder.upper().startswith("NOT RUN"):
                return "not_run"
            return "ok"
    return "missing"


def _preflight_dispatch_provider(story: dict, plan_role_config: dict | None) -> str:
    """Resolve the dispatch provider for a story; "claude" means not gated."""
    candidates = (
        story.get("backend"),
        ((plan_role_config or {}).get("dispatch") or {}).get("provider"),
        os.environ.get("PIPELINE_BACKEND_DISPATCH"),
    )
    for candidate in candidates:
        if candidate:
            return str(candidate).lower()
    try:
        registry = role_registry.load_registry() or {}
    except (OSError, ValueError, KeyError):
        registry = {}
    provider = ((registry.get("roles") or {}).get("dispatch") or {}).get("provider")
    if provider:
        return str(provider).lower()
    return "claude"


def _sizing_view(story: dict, plan_role_config: dict | None) -> dict:
    """Return *story* with the effective dispatch tier stamped when it declares none.

    `_story_sizing_warning` gates on ``story["backend"]``, so a story that
    inherits the process-wide tier declared no provider and was never sized.
    """
    if story.get("backend"):
        return story
    return {**story, "backend": _preflight_dispatch_provider(story, plan_role_config)}


_SECURITY_REVIEW_RE = re.compile(r"security[- ](?:engineer[- ])?review", re.IGNORECASE)


def _security_review_unenforced_warning(story) -> str | None:
    if story.get("risk") == "high":
        return None
    haystack = f"{story.get('summary', '')}\n{story.get('agent_instructions', '')}"
    if not _SECURITY_REVIEW_RE.search(haystack):
        return None
    risk = story.get("risk") or "low"
    return (
        "This brief asks for a security review, but the pipeline's "
        f"security-engineer pass only runs for risk: high (this story is risk: {risk})."
    )


_BLOCKED_BY_REF_RE = re.compile(r"^[A-Za-z0-9_.-]+:[A-Za-z0-9_.:-]+$")


def _blocked_by_error(story: dict) -> str | None:
    """Return an error string when ``story["blocked_by"]`` is invalid, else None.

    ``blocked_by`` (optional) lists cross-plan prerequisites as
    ``"<plan>:<story_key>"`` strings. Absent or ``None`` is valid. Otherwise it
    must be a list whose every entry is a string matching
    ``^[A-Za-z0-9_.-]+:[A-Za-z0-9_.:-]+$``: the plan part (before the FIRST
    colon) is non-empty and colon-free, and the key part is non-empty and may
    itself contain colons (so ``a:b:c`` is plan ``a``, key ``b:c``). Existence
    of the referenced plan/story is deliberately not checked here - it may not
    be ingested yet. The error names the story summary and the offending entry.
    """
    blocked_by = story.get("blocked_by")
    if blocked_by is None:
        return None
    summary = story.get("summary", "?")
    if not isinstance(blocked_by, list):
        return (
            f"story {summary!r}: `blocked_by` must be a list of "
            f"'<plan>:<story_key>' strings, got {blocked_by!r}"
        )
    for entry in blocked_by:
        if not isinstance(entry, str) or not _BLOCKED_BY_REF_RE.fullmatch(entry):
            return (
                f"story {summary!r}: `blocked_by` entry must be "
                f"'<plan>:<story_key>', got {entry!r}"
            )
    return None


def _files_field_error(story: dict) -> str | None:
    """Return an error string when ``story["files"]`` is invalid, else None.

    ``files`` (optional) declares the exact repo-relative POSIX paths of the
    PRODUCTION files (including docs) a story may change; test files are never
    listed and are always allowed. ``None`` when the field is absent or
    explicitly ``None`` (an empty list is VALID: it means no production file
    may change). Otherwise the value must be a ``list`` whose every entry is a
    non-empty ``str`` that does not start with ``"/"``, contains no
    ``"\\"``, and has no ``".."`` path component (components are compared
    after splitting on ``"/"``, so ``pipeline/..x.py`` is fine). Duplicates
    are rejected. The error names the story summary and the offending entry.
    """
    files = story.get("files")
    if files is None:
        return None
    if not isinstance(files, list):
        return (
            f"story {story.get('summary', '?')!r}: `files` must be a list of "
            f"repo-relative paths, got {files!r}"
        )
    seen: set[str] = set()
    for entry in files:
        if not isinstance(entry, str) or not entry:
            return (
                f"story {story.get('summary', '?')!r}: `files` entries must be "
                f"non-empty strings, got {entry!s}"
            )
        if entry.startswith("/") or "\\" in entry or ".." in entry.split("/"):
            return (
                f"story {story.get('summary', '?')!r}: `files` entry must be a "
                f"repo-relative POSIX path, got {entry!s}"
            )
        if entry in seen:
            return (
                f"story {story.get('summary', '?')!r}: `files` contains "
                f"duplicate entry {entry!s}"
            )
        seen.add(entry)
    return None


def _acceptance_field_error(story: dict) -> str | None:
    """Return an error string when ``story["acceptance"]`` is invalid, else None.

    ``acceptance`` (optional) declares the read-only grading fixtures a story
    is dispatched with. ``None`` when the field is absent or explicitly
    ``None``. Otherwise the value must be a ``list`` whose every entry is a
    ``dict`` carrying a non-empty ``str`` ``path`` and a ``str`` ``source``.
    The path must be repo-relative POSIX: it must not start with ``"/"``,
    must contain no ``"\\"``, and must have no ``".."`` path component
    (components are compared after splitting on ``"/"``, so ``..foo/x.py`` is
    fine). An absolute path or a ``..`` component would let dispatch write
    outside the worktree as the operator. The error names the story summary
    and the offending value.
    """
    acceptance = story.get("acceptance")
    if acceptance is None:
        return None
    if not isinstance(acceptance, list):
        return (
            f"story {story.get('summary', '?')!r}: malformed acceptance: "
            f"acceptance must be a list of {{'path': ..., 'source': ...}} "
            f"dicts, got {type(acceptance).__name__} ({acceptance!r})"
        )
    for index, entry in enumerate(acceptance):
        if not isinstance(entry, dict):
            return (
                f"story {story.get('summary', '?')!r}: malformed acceptance: "
                f"acceptance[{index}] must be a {{'path': ..., 'source': ...}} "
                f"dict, got {type(entry).__name__} ({entry!r})"
            )
        path = entry.get("path")
        if not isinstance(path, str) or not path:
            return (
                f"story {story.get('summary', '?')!r}: `acceptance` entry "
                f"`path` must be a non-empty string, got {path!r}"
            )
        if not isinstance(entry.get("source"), str):
            return (
                f"story {story.get('summary', '?')!r}: `acceptance` entry "
                f"`source` must be a string, got {entry.get('source')!r}"
            )
        if path.startswith("/") or "\\" in path or ".." in path.split("/"):
            return (
                f"story {story.get('summary', '?')!r}: `acceptance` entry "
                f"`path` must be a repo-relative path inside the worktree, "
                f"got {path!r}"
            )
    return None


def _neighbour_manifests(plan_name: str) -> dict[str, dict]:
    manifests = _store.records().get_manifests(current_namespace())
    return {name: manifest for name, manifest in manifests.items() if name != plan_name}


def _file_overlap_advisories(plan_name: str, stories: dict) -> list[dict]:
    """Notify (never block) for each story sharing files with another plan's in-flight story.

    Returns ``[{"story", "plan", "key", "files"}, ...]`` so the caller sees the
    collisions without reading the log. Absent ``files`` on either side means
    no advisory.
    """
    others = cross_plan.in_flight_stories(_neighbour_manifests(plan_name))
    advisories = []
    for key, story in stories.items():
        for hit in cross_plan.file_overlaps(story.get("files"), others):
            shared = sorted(set(story["files"]) & set(hit["files"]))
            message = (
                f"{key}: shares {', '.join(shared)} with in-flight "
                f"{hit['plan']}/{hit['key']}; consider splitting or sequencing"
            )
            _notify_user(plan_name, message, story_key=key, event="file_overlap")
            advisories.append(
                {"story": key, "plan": hit["plan"], "key": hit["key"], "files": shared}
            )
    return advisories


def _story_key_conflict_error(
    plan_name: str, plan: dict, only_epics: list[str] | None
) -> str | None:
    """Error text when an explicit story key is still held by another plan, else None.

    Branch (``agent/<key>``) and worktree path are keyed by the story key
    alone, so a key another plan's unfinished story (or leftover worktree)
    holds would be shared across both plans.
    """
    keys = [
        story["key"]
        for epic in plan["epics"]
        if not only_epics or epic["summary"] in only_epics
        for story in epic.get("stories", [])
        if isinstance(story.get("key"), str) and story["key"]
    ]
    conflicts = cross_plan.key_conflicts(
        keys, _neighbour_manifests(plan_name), worktree_parent(WORKTREE_ROOT._value(), current_namespace())
    )
    if not conflicts:
        return None
    first = conflicts[0]
    return (
        f"story key {first['key']!r} is already used by plan {first['plan']!r} "
        f"(story {first['other_key']!r}, status {first['status']!r}); story "
        "keys share a branch and a worktree across plans, so choose a key "
        "unique to this plan or wait for that story to finish."
    )


# Org sources are read here, with the repo and plan levels, because plan context
# resolution (``_merged_context``) is the single place the three levels merge.
def _load_org_sources() -> tuple[list[dict], str | None]:
    """Return the file sources then the org context provider's, or an error."""
    file_sources, error = _load_org_file_sources()
    if error is not None:
        return [], error
    provider_sources, error = plugin_org_sources(current_namespace())
    if error is not None:
        return [], error
    return merge_org_sources(file_sources, provider_sources)


def _load_org_file_sources() -> tuple[list[dict], str | None]:
    """Read the org context file named by ``PIPELINE_CONTEXT_CONFIG``.

    Unset means no org sources. When set, a relative path, unreadable file,
    malformed JSON, or invalid content is an error (fail closed). Returns
    ``(sources, error)``.
    """
    path = os.environ.get("PIPELINE_CONTEXT_CONFIG")
    if path is None:
        return [], None
    prefix = f"PIPELINE_CONTEXT_CONFIG {path!r}"
    if not os.path.isabs(path):
        return [], f"{prefix} must be an absolute path"
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return [], f"{prefix} is unreadable or not valid JSON: {type(error).__name__}"
    if not isinstance(data, dict) or data.keys() - {"sources"}:
        return [], f"{prefix} must be an object with only a 'sources' key"
    errors = validate_context_config(data)
    if errors:
        return [], f"{prefix} is invalid: " + "; ".join(errors)
    return data["sources"], None


def _merged_context(plan: dict, repo_root: Path) -> tuple[dict | None, str | None]:
    """Merge org and ``.fagan.json`` context sources beneath the plan's own.

    On a duplicate source ``name`` plan wins over repo, and repo over org.
    Returns ``(context, None)``, ``(None, None)`` when no level declares
    sources, or ``(None, error)`` when the merged list is invalid.
    """
    org_sources, org_error = _load_org_sources()
    if org_error is not None:
        return None, org_error
    repo_config = load_repo_config(repo_root) or {}
    repo_sources = repo_config.get("context", {}).get("sources", [])
    plan_has_context = "context" in plan
    if plan_has_context:
        errors = validate_context_config(plan["context"])
        if errors:
            return None, "; ".join(errors)
    plan_sources = plan["context"]["sources"] if plan_has_context else []
    plan_names = {source["name"] for source in plan_sources}
    repo_names = {source["name"] for source in repo_sources}
    merged = (
        [s for s in org_sources if s["name"] not in plan_names | repo_names]
        + [s for s in repo_sources if s["name"] not in plan_names]
        + plan_sources
    )
    if not merged:
        return None, None
    merged_context = {"sources": merged}
    if plan_has_context and "max_total_chars" in plan["context"]:
        merged_context["max_total_chars"] = plan["context"]["max_total_chars"]
    errors = validate_context_config(merged_context)
    if errors:
        return None, "; ".join(errors)
    sources = normalize_context_sources(merged_context)
    for source in sources:
        if source["name"] in plan_names:
            source["origin"] = "plan"
        elif source["name"] in repo_names:
            source["origin"] = "repo"
        else:
            source["origin"] = "org"
    return {
        "max_total_chars": merged_context.get("max_total_chars", DEFAULT_MAX_TOTAL_CHARS),
        "sources": sources,
    }, None


def _story_extra_fields(story: dict) -> dict:
    """Per-story manifest defaults that stay at the ingest call site.

    TDD_SPLIT_PRODUCTION_PLAN.md §2.4: explicit per-story opt-in for the
    test-author pre-executor phase. Defaults False - inferring eligibility
    from agent_instructions prose is a worse failure mode than an operator
    forgetting to opt in.
    """
    return {"tdd_split": bool(story.get("tdd_split", False))}


def _consume_plan_draft(plan_name: str) -> None:
    """Delete the staged draft: after a full ingest the manifest is the plan."""
    plan_draft._records().delete_plan_draft(current_namespace(), plan_name)


def _keyless_reingest_error(
    plan: dict, only_epics: list[str] | None, already_ingested: bool, overwrite: bool
) -> str | None:
    """Error text when re-ingesting would mint duplicate stories, else None.

    Without an explicit ``key`` a story is keyed by a fresh UUID (or a new
    ticket id) on every ingest, so re-ingesting a plan whose manifest already
    exists adds a second copy of that story next to the first, and both
    dispatch. Only ``overwrite=True`` (wholesale replace) is safe then.
    """
    if overwrite or not already_ingested:
        return None
    for epic in plan["epics"]:
        if only_epics and epic["summary"] not in only_epics:
            continue
        for story in epic.get("stories", []):
            key = story.get("key")
            if not isinstance(key, str) or not key:
                summary = story.get("summary")
                return (
                    f"story {summary!r} has no explicit `key` and plan is "
                    "already ingested: re-ingesting would mint a duplicate "
                    "story that dispatches alongside the original. Give every "
                    "story a `key`, or pass overwrite=True to replace the "
                    "manifest."
                )
    return None


def _ingest_plan_impl(
    plan_name: str,
    only_epics: list[str] | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    plan_names.validate_new_plan_name(plan_name)
    plan, refusal = ingest_validate.read_draft_refusal(plan_name)
    if refusal is not None:
        return refusal
    # read_draft_refusal returns plan=None only together with a refusal.
    plan = cast("dict[str, Any]", plan)
    refusal = ingest_validate.repo_root_refusal(plan)
    if refusal is not None:
        return refusal
    repo_root = plan["repo_root"]
    refusal = ingest_validate.plan_scope_refusal(plan)
    if refusal is not None:
        return refusal
    refusal = ingest_validate.test_command_refusal(repo_root)
    if refusal is not None:
        return refusal

    override, refusal = ingest_preflight.preflight_override_refusal(plan)
    if refusal is not None:
        return refusal
    overrides, refusal = ingest_preflight.story_preflight_refusal(
        plan, only_epics, override, repo_root
    )
    if refusal is not None:
        return refusal

    plan_context, refusal = ingest_validate.merged_context_refusal(
        plan_name, plan, repo_root
    )
    if refusal is not None:
        return refusal

    refusal = ingest_validate.description_refusal(plan, only_epics)
    if refusal is not None:
        return refusal

    already_ingested = plan_name in _store.records().list_plans(current_namespace())
    refusal = ingest_validate.keyless_reingest_refusal(
        plan, only_epics, already_ingested, overwrite
    )
    if refusal is not None:
        return refusal
    refusal = ingest_validate.key_conflict_refusal(plan_name, plan, only_epics)
    if refusal is not None:
        return refusal

    with _store.transaction(plan_name) as acquired:
        if not acquired:
            return {
                "ok": True,
                "skipped": "locked",
                "reason": "another ingest/dispatch/interrupt is in progress for this plan",
            }

        # Any caller can rewrite a plan file, so resolve ownership against the
        # manifest on disk even when overwriting, before any provider call.
        try:
            stored = _store.records().get_manifest(current_namespace(), plan_name)
        except UnreadableRecordError as exc:
            return {"ok": False, "error": str(exc)}
        # A listed plan with no readable manifest refuses rather than resets as new.
        if stored is None and already_ingested:
            return {"ok": False, "error": f"plan {plan_name!r} is unreadable"}
        saved_manifest: dict[str, Any] = stored or {}
        try:
            ownership = resolve_ownership(plan_name, plan, saved_manifest, actor_id())
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        # Epic/story construction (including the authored descriptive fields
        # and the local-key -> issue-id dependency translation) lives in
        # pipeline.ingest_manifest; when no ticketing backend is configured
        # (NullTicketProvider) create_epic/create_story are no-ops returning
        # None and that module synthesizes story keys locally instead of
        # taking them from a backend-issued id.
        manifest = build_manifest(
            plan,
            provider=get_ticket_provider(),
            only_epics=only_epics,
            story_extra_fields=_story_extra_fields,
        )

        # Merge into the existing manifest rather than replacing it (T1):
        # anything only_epics excluded this round - and, with overwrite=False,
        # the manifest's runtime state for stories re-ingested this round -
        # must survive. overwrite=True restores the old wholesale-replace
        # behavior for callers that genuinely want a clean slate.
        prior: dict[str, Any] = {} if overwrite else saved_manifest

        merged_epics = dict(prior.get("epics", {}))
        merged_epics.update(manifest["epics"])

        merged_stories = ingest_manifest.merge_stories(
            prior.get("stories", {}), manifest["stories"], _INGEST_AUTHORED_STORY_FIELDS
        )

        final_manifest = dict(prior)
        final_manifest["epics"] = merged_epics
        final_manifest["stories"] = merged_stories
        final_manifest["repo_root"] = repo_root
        # Descriptive, non-plan-defining plan fields (ADR 0012 decision 5):
        # plan_epics always, preflight_override/repo_url only when authored.
        # None of them is a canonical_digest member, so recording them cannot
        # reset an approval.
        final_manifest.update(plan_level_fields(plan))
        final_manifest["role_config"] = plan.get(
            "role_config", prior.get("role_config", {})
        )
        final_manifest["ingested_by"] = actor_id()
        # OWN-03: ownership was resolved before the provider loop, so a refusal
        # never reaches this write. Not authored: outside canonical_digest.
        final_manifest["owner"] = ownership["owner"]
        final_manifest["delegates"] = ownership["delegates"]
        final_manifest["team"] = plan.get("team") or prior.get("team") or DEFAULT_TEAM
        if plan.get("test_scope", prior.get("test_scope")) is not None:
            final_manifest["test_scope"] = plan.get("test_scope", prior.get("test_scope"))
        if plan_context is not None:
            final_manifest["context"] = plan_context

        # Auto-route oversized on-device stories to the host's :cloud default
        # model before the manifest is written: an oversized story is still
        # dispatched, just not to a local executor that would choke on it.
        routed = ingest_manifest.route_oversized_stories(
            final_manifest["stories"], repo_root, plan.get("role_config")
        )
        routed_keys = {key for key, _tag, _warning in routed}

        # Opt-in plan-approval gate (ENT-LC-01); a no-op while PIPELINE_PLAN_APPROVAL is off.
        notify_pending = False
        if approval_enabled():
            previous_approval = final_manifest.get("approval")
            final_manifest["approval"] = on_ingest(final_manifest)
            notify_pending = pending_notice_due(previous_approval, final_manifest["approval"])
        _store.records().put_manifest(current_namespace(), plan_name, final_manifest)
        # ADR 0012 decision 5: a full ingest consumes the draft. A crash between
        # this write and the delete leaves it, and the next ingest re-applies
        # identical content by explicit story key - which is idempotent.
        if only_epics is None:
            _consume_plan_draft(plan_name)
        if notify_pending:
            notify_approval_event(plan_name, final_manifest, "plan_pending_approval")

        # One notification per routed story, after the write: the route
        # notice replaces the plain advisory sizing warning for that story.
        for key, tag, warning in routed:
            message = f"{key}: auto-routed to {tag}: {warning}"
            _notify_user(plan_name, message, story_key=key, event="sizing_auto_routed")
            logging.getLogger("pipeline").warning(f"{plan_name}/{key}: {message}")

        # A plan-level preflight_override admitted these non-Claude stories
        # despite a missing or not-run `Preflight:` line
        # (.claude/rules/local-dispatch-preflight.md). Every use is notified:
        # the override is an emergency escape hatch, never a silent default.
        for key, status, reason in overrides:
            message = f"{key}: preflight gate overridden ({status}): {reason}"
            _notify_user(plan_name, message, story_key=key, event="preflight_override")
            logging.getLogger("pipeline").warning(message)

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
        for key, body, notify_kwargs, log in ingest_manifest.iter_story_notices(
            final_manifest, routed_keys, repo_root
        ):
            _notify_user(plan_name, f"{key}: {body}", **notify_kwargs)
            if log:
                logging.getLogger("pipeline").warning(f"{plan_name}/{key}: {body}")

        file_overlaps = _file_overlap_advisories(plan_name, final_manifest["stories"])

    return {
        "ok": True,
        **final_manifest,
        "file_overlaps": file_overlaps,
    }
