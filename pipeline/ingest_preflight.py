"""Per-story preflight phases of ``pipeline.ingest._ingest_plan_impl``.

Runs before any write: the plan-level ``preflight_override`` check and the
loop that validates each story's backend, ``files``, ``blocked_by``,
acceptance fixtures and ``Preflight:`` gate. Each returns a refusal
dictionary or the computed value; the orchestrator keeps the order.

Names the test suite monkeypatches on :mod:`pipeline.ingest` are reached
through that module at call time (``_home``); it is imported inside each
function because ``pipeline.ingest`` imports this module.
"""

from __future__ import annotations

from typing import Any

from . import external_refs

# (story key, preflight status, override reason) for each gated story the
# plan-level override admitted.
Override = tuple[Any, str, str]


def preflight_override_refusal(
    plan: dict[str, Any],
) -> tuple[str | None, dict[str, Any] | None]:
    """Validate the plan-level override; return ``(override, refusal)``."""
    # A plan-level preflight_override admits non-Claude stories whose brief
    # lacks a real `Preflight:` line (.claude/rules/local-dispatch-preflight.md).
    # Validated once, up front, before any side effect: a present-but-invalid
    # value must fail the ingest even when every story is Claude-provider.
    override = plan.get("preflight_override")
    if "preflight_override" in plan and (
        not isinstance(override, str) or not override.strip()
    ):
        return override, {
            "ok": False,
            "error": "preflight_override must be a non-empty reason string",
        }
    return override, None


def _story_refusal(
    story: dict[str, Any],
    plan: dict[str, Any],
    override: str | None,
    repo_root: str,
    overrides: list[Override],
) -> dict[str, Any] | None:
    from . import ingest as _home

    story_backend = story.get("backend")
    if story_backend is not None and story_backend not in _home._VALID_STORY_BACKENDS:
        return {
            "ok": False,
            "error": (
                f"Story {story.get('summary', '?')!r} has unknown "
                f"backend {story_backend!r}. Valid values: "
                f"{sorted(_home._VALID_STORY_BACKENDS)}"
            ),
        }
    files_error = _home._files_field_error(story) or _home._test_scope_error(
        story, f"story {story.get('summary')!r}"
    )
    if files_error is not None:
        return {"ok": False, "error": files_error}
    blocked_by_error = external_refs.blocked_by_error(
        story, _home._blocked_by_error
    )
    if blocked_by_error is not None:
        return {"ok": False, "error": blocked_by_error}
    acceptance_error = _home._acceptance_field_error(story)
    if acceptance_error is not None:
        return {"ok": False, "error": acceptance_error}
    # .claude/rules/local-dispatch-preflight.md: a story dispatched to
    # a non-Claude provider must carry a real `Preflight:` line (a
    # conflict/impact run against the base commit), so the executor
    # does not discover a stale base or a conflicting sibling change
    # mid-dispatch. Claude-provider stories are never gated.
    provider = _home._preflight_dispatch_provider(story, plan.get("role_config"))
    if provider != "claude":
        status = _home._preflight_status(story.get("agent_instructions"))
        if status != "ok":
            if override is None:
                phrase = (
                    "says `Preflight: NOT RUN`"
                    if status == "not_run"
                    else "has no `Preflight:` line"
                )
                return {
                    "ok": False,
                    "error": (
                        f"story {story.get('summary')!r} is gated for "
                        f"provider {provider!r}: it {phrase}; see "
                        f".claude/rules/local-dispatch-preflight.md; set "
                        f'a plan-level "preflight_override": "<reason>" '
                        f"for emergencies"
                    ),
                }
            overrides.append((story.get("key"), status, override))
    # OPSA-8: lint the .py acceptance fixture sources upfront, before
    # any Plane side effect. A lint-violating fixture is a read-only
    # oracle the dispatched agent can never fix (PR #235), so it must
    # not reach dispatch - and it must not leave orphaned epics or
    # stories behind when the ingest is rejected. Gating on "finding"
    # only, never on "skipped": a broken or absent lint tool can never
    # block a known-good plan.
    kind, lint_msg = _home._lint_acceptance_fixtures(story, repo_root)
    if kind == "finding":
        return {"ok": False, "error": lint_msg}
    return None


def story_preflight_refusal(
    plan: dict[str, Any],
    only_epics: list[str] | None,
    override: str | None,
    repo_root: str,
) -> tuple[list[Override], dict[str, Any] | None]:
    """Check every selected story; return ``(overrides, refusal)``."""
    # Validate story["backend"] upfront, before any Plane side effects, so a
    # typo'd provider name fails closed here rather than surfacing as a
    # NotImplementedError deep inside get_backend at dispatch time.
    overrides: list[Override] = []
    for epic in plan["epics"]:
        if only_epics and epic["summary"] not in only_epics:
            continue
        for story in epic.get("stories", []):
            refusal = _story_refusal(story, plan, override, repo_root, overrides)
            if refusal is not None:
                return overrides, refusal
    return overrides, None
