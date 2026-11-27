"""Pre-write validation phases of ``pipeline.ingest._ingest_plan_impl``.

Each function runs one check that must pass before the ingest writes anything
and returns a refusal dictionary (``{"ok": False, ...}``) or ``None``. The
orchestrator calls them in the order the checks always ran, so the first
refusal a plan hits is unchanged.

Names the test suite monkeypatches on :mod:`pipeline.ingest` are reached
through that module at call time (``_home``); it is imported inside each
function because ``pipeline.ingest`` imports this module.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import plan_draft
from .actor import current_namespace
from .ecosystem_detect import (
    UNSUPPORTED_MARKERS,
    detect_test_command,
    fail_loud_exit_message,
    is_unsupported_ecosystem_command,
)
from .ingest_manifest import validate_story_descriptions


def read_draft_refusal(
    plan_name: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Read the staged draft; return ``(plan, refusal)``."""
    from . import ingest as _home

    plan, refusal = plan_draft.read_plan_draft(plan_name)
    if refusal is None:
        return plan, None
    missing = f"No plan named {plan_name}"
    # A plan whose draft a full ingest consumed names the way out (ADR 0012).
    if refusal.get("error") == missing and plan_name in _home._store.records().list_plans(
        current_namespace()
    ):
        return plan, {"ok": False, "error": (
            f"plan '{plan_name}' has no staged draft: save_plan it "
            "(export_plan renders the current plan) before re-ingesting")}
    return plan, refusal


def repo_root_refusal(plan: dict[str, Any]) -> dict[str, Any] | None:
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
    return None


def plan_scope_refusal(plan: dict[str, Any]) -> dict[str, Any] | None:
    from . import ingest as _home

    # AUTH-02: the manifest records the team a policy decision is evaluated
    # against (ADR 0009: principal -> team -> repo -> plan -> story). Validate
    # it here, with the other plan-level fields and before any file is
    # written, so a typo'd slug fails closed instead of landing on disk.
    team_error = _home._team_error(plan) or _home._test_scope_error(plan)
    if team_error is not None:
        return {"ok": False, "error": team_error}
    return None


def test_command_refusal(repo_root: str) -> dict[str, Any] | None:
    # LAG-5: a repo whose ecosystem fagan cannot test (Ruby, PHP, Swift,
    # Elixir) would otherwise ingest fine and burn dispatch/rework budget on
    # a test gate that always fails with "unsupported ecosystem". Rejecting
    # here surfaces the fix (declare test_cmd in .fagan.json) before any
    # executor is dispatched. A README-only repo (no marker at all) still
    # keeps detect_test_command's no-op and ingests normally.
    #
    # detect_test_command lists repo_root (cwd.iterdir and its subdir scan),
    # which raises OSError on a directory that exists but cannot be read
    # (permissions, a stale NFS mount). The is_dir() check above only proves
    # it stat()s, so keep this rejection in the same {"ok": False} shape
    # instead of letting the OSError escape _ingest_plan_impl.
    try:
        test_dir, test_cmd = detect_test_command(Path(repo_root))
    except OSError as error:
        return {
            "ok": False,
            "error": f"Plan repo_root {repo_root!r} is not readable: {error}",
        }
    if not is_unsupported_ecosystem_command(test_cmd):
        return None
    # fail_loud_exit_message is also True for repo_config's
    # invalid_config_command: a repo_root with a malformed .fagan.json is
    # NOT an unsupported ecosystem, so its rejection must name the actual
    # RepoConfigError reason instead of a marker that does not exist.
    message = fail_loud_exit_message(test_cmd) or ""
    if message.startswith("fagan: invalid .fagan.json: "):
        reason = message[len("fagan: invalid .fagan.json: "):]
        return {
            "ok": False,
            "error": (
                f"Plan repo_root {repo_root!r} has an invalid .fagan.json: "
                f"{reason}"
            ),
        }
    marker = next(
        (m for m in UNSUPPORTED_MARKERS if (Path(test_dir) / m).exists()),
        "unsupported ecosystem",
    )
    return {
        "ok": False,
        "error": (
            f"Plan repo_root {repo_root!r} uses an unsupported ecosystem "
            f"({marker}): its test gate would exit non-zero with nothing "
            f"run. Add a .fagan.json with a test_cmd to {repo_root!r}."
        ),
    }


def merged_context_refusal(
    plan_name: str, plan: dict[str, Any], repo_root: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Resolve the plan's context; return ``(plan_context, refusal)``."""
    from . import ingest as _home

    plan_context, context_error = _home._merged_context(plan, Path(repo_root))
    if context_error is not None:
        return plan_context, {"ok": False, "error": context_error}
    org_path = os.environ.get("PIPELINE_CONTEXT_CONFIG")
    if org_path is not None and plan_context is not None:
        org_count = sum(1 for s in plan_context["sources"] if s["origin"] == "org")
        _home._notify_user(
            plan_name,
            f"context_resolved: org config {org_path} contributed {org_count} source(s)",
            event="context_resolved",
        )
    return plan_context, None


def description_refusal(
    plan: dict[str, Any], only_epics: list[str] | None
) -> dict[str, Any] | None:
    # ADR 0012 decision 5: the manifest records each story's authored
    # description, so it must be a string. Validated here - before the
    # transaction and any provider call - so a refusal writes nothing and
    # leaves the previously ingested manifest on disk untouched.
    return validate_story_descriptions(plan, only_epics)


def keyless_reingest_refusal(
    plan: dict[str, Any],
    only_epics: list[str] | None,
    already_ingested: bool,
    overwrite: bool,
) -> dict[str, Any] | None:
    from . import ingest as _home

    keyless_error = _home._keyless_reingest_error(
        plan, only_epics, already_ingested, overwrite
    )
    if keyless_error is not None:
        return {"ok": False, "error": keyless_error}
    return None


def key_conflict_refusal(
    plan_name: str, plan: dict[str, Any], only_epics: list[str] | None
) -> dict[str, Any] | None:
    from . import ingest as _home

    key_conflict_error = _home._story_key_conflict_error(plan_name, plan, only_epics)
    if key_conflict_error is not None:
        return {"ok": False, "error": key_conflict_error}
    return None
